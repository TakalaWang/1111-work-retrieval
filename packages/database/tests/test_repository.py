from datetime import datetime
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


def test_reader_fetches_exact_metadata_for_batch_revalidation() -> None:
    engine = MagicMock(spec=Engine)
    session = _session()
    timestamp = datetime(2026, 6, 8, 12, 30)
    session.execute.return_value.all.return_value = [
        ("1", "台北市", "資訊", "軟體", "後端", timestamp)
    ]
    reader = SqlAlchemyJobReader(engine, session_factory=lambda: session)

    records = reader.metadata_for_job_ids(("1", "2"))

    assert len(records) == 1
    assert records[0].job_id == "1"
    assert records[0].source_modified_at == timestamp
    statement = session.execute.call_args.args[0]
    assert "jobs.work_city" in str(statement)
    assert "jobs.source_modified_at" in str(statement)


def test_metadata_batch_rejects_invalid_identifiers_before_querying() -> None:
    engine = MagicMock(spec=Engine)
    session_factory = MagicMock()
    reader = SqlAlchemyJobReader(engine, session_factory=session_factory)

    for job_ids in (("1", "1"), ("job-1",), ("\uff11\uff12",)):
        with pytest.raises(ValueError, match="ASCII decimal"):
            reader.metadata_for_job_ids(job_ids)

    assert reader.metadata_for_job_ids(()) == ()
    session_factory.assert_not_called()


def test_reader_returns_exact_job_details_or_none() -> None:
    engine = MagicMock(spec=Engine)
    session = _session()
    job = MagicMock()
    job.title = "後端工程師"
    job.work_city = "台北市"
    job.salary_text = "月薪 55,000 元"
    job.duty_major = "資訊軟體系統類"
    job.duty_middle = "軟體工程"
    job.duty_minor = "後端開發"
    job.description = "開發 API"
    job.experience_requirement = "2 年"
    job.education_requirement = "大學"
    job.work_skills = "Python"
    job.source_modified_at = datetime(2026, 6, 8, 12, 34, 56, 789000)
    session.scalar.side_effect = [job, None]
    reader = SqlAlchemyJobReader(engine, session_factory=lambda: session)

    assert reader.job_details("53256270") == {
        "職務名稱": "後端工程師",
        "工作城市": "台北市",
        "薪資": "月薪 55,000 元",
        "職務大類": "資訊軟體系統類",
        "職務中類": "軟體工程",
        "職務小類": "後端開發",
        "職務內容": "開發 API",
        "工作經驗需求": "2 年",
        "學歷需求": "大學",
        "工作技能": "Python",
        "職缺最後修改時間": "2026-06-08T12:34:56.789",
    }
    assert reader.job_details("99999999") is None


def test_reader_wraps_database_errors_and_disposes_its_engine() -> None:
    engine = MagicMock(spec=Engine)
    session = _session()
    session.execute.side_effect = OperationalError("private SQL", {}, RuntimeError("secret"))
    reader = SqlAlchemyJobReader(engine, session_factory=lambda: session)

    with pytest.raises(
        JobStoreUnavailableError, match="PostgreSQL job metadata lookup failed"
    ) as caught:
        reader.metadata_for_job_ids(("1",))
    assert "private SQL" not in str(caught.value)

    reader.close()
    engine.dispose.assert_called_once_with()
