from unittest.mock import MagicMock

import pytest
import work_retrieval_database.repository as repository
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool
from work_retrieval_database import (
    DatabaseSettings,
    JobStoreUnavailableError,
    SqlAlchemyJobReader,
)


def _session() -> MagicMock:
    session = MagicMock(spec=Session)
    session.__enter__.return_value = session
    session.__exit__.return_value = None
    return session


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


def test_reader_connections_use_null_pool_require_tls_and_bound_query_time(
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
    assert create_engine.call_args.kwargs["poolclass"] is NullPool


def test_reader_uses_one_session_per_call_and_orders_seed_ids_by_lineage() -> None:
    engine = MagicMock(spec=Engine)
    session = _session()
    session.scalars.return_value.all.return_value = ["job-1", "job-2"]
    reader = SqlAlchemyJobReader(engine, session_factory=lambda: session)

    assert reader.first_job_ids(limit=2) == ("job-1", "job-2")
    statement = session.scalars.call_args.args[0]
    assert "ORDER BY jobs.source_row" in str(statement)
    assert "LIMIT" in str(statement)


def test_reader_wraps_database_errors_and_disposes_its_engine() -> None:
    engine = MagicMock(spec=Engine)
    session = _session()
    session.scalars.side_effect = OperationalError("private SQL", {}, RuntimeError("secret"))
    reader = SqlAlchemyJobReader(engine, session_factory=lambda: session)

    with pytest.raises(JobStoreUnavailableError, match="PostgreSQL job lookup failed") as caught:
        reader.first_job_ids(limit=10)
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
