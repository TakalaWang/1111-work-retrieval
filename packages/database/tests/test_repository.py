from datetime import datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
import work_retrieval_database.repository as repository
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from work_retrieval_database import (
    DatabaseSettings,
    Job,
    JobSnapshot,
    JobStoreUnavailableError,
    SqlAlchemyJobReader,
)


def _session() -> MagicMock:
    session = MagicMock(spec=Session)
    session.__enter__.return_value = session
    session.__exit__.return_value = None
    return session


def _job() -> Job:
    values: dict[str, Any] = {
        column.name: f"value:{column.name}" for column in Job.__table__.columns
    }
    values.update(
        job_id="job-1",
        salary_min=Decimal("1234567890.10"),
        salary_max=Decimal("9999999999.99"),
        source_modified_at=datetime(2026, 8, 1, 12, 30, 45, 123000),
        source_row=17,
    )
    return Job(**values)


def test_database_settings_require_only_explicit_postgres_values() -> None:
    settings = DatabaseSettings.from_environment(
        {
            "DB_HOST": "db.internal",
            "DB_PORT": "5432",
            "DB_NAME": "work_retrieval",
            "DB_USER": "service",
            "DB_PASSWORD": "secret/value",
            "DATABASE_URL": "sqlite:///must-not-be-used.db",
        }
    )

    url = settings.sqlalchemy_url()
    assert url.drivername == "postgresql+psycopg"
    assert (url.host, url.port, url.database, url.username, url.password) == (
        "db.internal",
        5432,
        "work_retrieval",
        "service",
        "secret/value",
    )
    assert "secret/value" not in repr(settings)


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {
            "DB_HOST": " ",
            "DB_PORT": "5432",
            "DB_NAME": "work_retrieval",
            "DB_USER": "service",
            "DB_PASSWORD": "secret",
        },
        {
            "DB_HOST": "db.internal",
            "DB_PORT": "not-a-port",
            "DB_NAME": "work_retrieval",
            "DB_USER": "service",
            "DB_PASSWORD": "secret",
        },
        {
            "DB_HOST": "db.internal",
            "DB_PORT": "65536",
            "DB_NAME": "work_retrieval",
            "DB_USER": "service",
            "DB_PASSWORD": "secret",
        },
    ],
)
def test_database_settings_fail_closed(environment: dict[str, str]) -> None:
    with pytest.raises(RuntimeError):
        DatabaseSettings.from_environment(environment)


def test_reader_connections_require_tls_and_bound_query_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = MagicMock(spec=Engine)
    create_engine = MagicMock(return_value=engine)
    monkeypatch.setattr(repository, "create_engine", create_engine)
    settings = DatabaseSettings("db.internal", 5432, "work_retrieval", "service", "secret")

    SqlAlchemyJobReader.from_settings(settings)

    assert create_engine.call_args.kwargs["connect_args"] == {
        "connect_timeout": 30,
        "options": "-c statement_timeout=5000",
        "sslmode": "require",
    }


def test_reader_uses_one_session_per_call_and_orders_seed_ids_by_lineage() -> None:
    engine = MagicMock(spec=Engine)
    first = _session()
    first.scalars.return_value.all.return_value = ["job-1", "job-2"]
    second = _session()
    job = _job()
    second.scalar.return_value = job
    sessions = iter((first, second))
    reader = SqlAlchemyJobReader(engine, session_factory=lambda: next(sessions))

    assert reader.first_job_ids(limit=2) == ("job-1", "job-2")
    snapshot = reader.get("job-1")

    assert isinstance(snapshot, JobSnapshot)
    assert all(getattr(snapshot, name) is not None for name in snapshot.__dataclass_fields__)
    for field_name in snapshot.__dataclass_fields__:
        assert getattr(snapshot, field_name) == getattr(job, field_name)
    assert snapshot.salary_min == Decimal("1234567890.10")
    assert snapshot.salary_max == Decimal("9999999999.99")
    assert snapshot.source_modified_at == datetime(2026, 8, 1, 12, 30, 45, 123000)
    first_statement = first.scalars.call_args.args[0]
    assert "ORDER BY jobs.source_row" in str(first_statement)
    assert "LIMIT" in str(first_statement)
    second_statement = second.scalar.call_args.args[0]
    assert "WHERE jobs.job_id" in str(second_statement)


def test_reader_returns_none_for_an_unknown_job() -> None:
    engine = MagicMock(spec=Engine)
    session = _session()
    session.scalar.return_value = None
    reader = SqlAlchemyJobReader(engine, session_factory=lambda: session)

    assert reader.get("missing") is None


def test_reader_wraps_database_errors_and_disposes_its_engine() -> None:
    engine = MagicMock(spec=Engine)
    session = _session()
    session.scalar.side_effect = OperationalError("private SQL", {}, RuntimeError("secret"))
    reader = SqlAlchemyJobReader(engine, session_factory=lambda: session)

    with pytest.raises(JobStoreUnavailableError, match="PostgreSQL job lookup failed") as caught:
        reader.get("job-1")
    assert "private SQL" not in str(caught.value)

    reader.close()
    engine.dispose.assert_called_once_with()


def test_reader_rejects_non_positive_limits_without_querying() -> None:
    engine = MagicMock(spec=Engine)
    session_factory = MagicMock()
    reader = SqlAlchemyJobReader(engine, session_factory=session_factory)

    with pytest.raises(ValueError, match="positive integer"):
        reader.first_job_ids(limit=0)
    session_factory.assert_not_called()
