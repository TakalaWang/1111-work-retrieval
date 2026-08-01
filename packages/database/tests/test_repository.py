from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, delete, insert
from sqlalchemy.engine import make_url
from work_retrieval_database import DatabaseUnavailableError, Job, PostgresJobRepository

TEST_JOB_ID = "999999999999999999"


def test_repository_fails_startup_when_postgresql_is_unreachable() -> None:
    with pytest.raises(DatabaseUnavailableError, match="database initialization failed"):
        PostgresJobRepository("postgresql+psycopg://postgres:postgres@127.0.0.1:1/work_retrieval")


def _local_database_url() -> str:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    host = make_url(database_url).host
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("repository integration tests require a loopback PostgreSQL host")
    return database_url


def test_repository_reads_one_job_and_reports_a_missing_job() -> None:
    database_url = _local_database_url()
    engine = create_engine(database_url)
    repository = PostgresJobRepository(database_url)
    details = {"職務名稱": "整合測試職缺", "工作技能": None}

    try:
        with engine.begin() as connection:
            connection.execute(delete(Job).where(Job.job_id == TEST_JOB_ID))
            connection.execute(insert(Job).values(job_id=TEST_JOB_ID, details=details))

        record = repository.get(TEST_JOB_ID)
        assert record is not None
        assert record.details == details
        assert repository.get("0") is None
    finally:
        repository.close()
        with engine.begin() as connection:
            connection.execute(delete(Job).where(Job.job_id == TEST_JOB_ID))
        engine.dispose()
