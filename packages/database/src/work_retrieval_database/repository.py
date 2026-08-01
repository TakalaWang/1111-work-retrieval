from __future__ import annotations

import os
from dataclasses import dataclass

from sqlalchemy import URL, create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from work_retrieval_database.models import Job

# Aurora Serverless v2 can need 30 seconds or longer to resume after a long idle period.
CONNECTION_TIMEOUT_SECONDS = 30
POOL_TIMEOUT_SECONDS = 5
STATEMENT_TIMEOUT_MILLISECONDS = 5_000


class DatabaseUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: str
    details: dict[str, str | None]


class PostgresJobRepository:
    def __init__(self, database_url: str | URL) -> None:
        url = make_url(database_url)
        if url.get_backend_name() != "postgresql":
            raise ValueError("job repository requires PostgreSQL")
        if url.drivername == "postgresql":
            url = url.set(drivername="postgresql+psycopg")
        elif url.drivername != "postgresql+psycopg":
            raise ValueError("job repository requires the psycopg driver")
        self._engine = create_engine(
            url,
            connect_args={
                "connect_timeout": CONNECTION_TIMEOUT_SECONDS,
                "options": f"-c statement_timeout={STATEMENT_TIMEOUT_MILLISECONDS}",
            },
            pool_pre_ping=True,
            pool_timeout=POOL_TIMEOUT_SECONDS,
        )
        try:
            with self._engine.connect():
                pass
        except SQLAlchemyError as error:
            self._engine.dispose()
            raise DatabaseUnavailableError("database initialization failed") from error

    @classmethod
    def from_environment(cls) -> PostgresJobRepository:
        names = (
            "DATABASE_HOST",
            "DATABASE_PORT",
            "DATABASE_NAME",
            "DATABASE_USER",
            "DATABASE_PASSWORD",
        )
        values = {name: os.environ.get(name) for name in names}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise DatabaseUnavailableError(f"missing database configuration: {', '.join(missing)}")
        try:
            port = int(values["DATABASE_PORT"] or "")
        except ValueError as error:
            raise DatabaseUnavailableError("DATABASE_PORT must be an integer") from error
        if not 1 <= port <= 65_535:
            raise DatabaseUnavailableError("DATABASE_PORT is outside the valid range")
        return cls(
            URL.create(
                "postgresql+psycopg",
                username=values["DATABASE_USER"],
                password=values["DATABASE_PASSWORD"],
                host=values["DATABASE_HOST"],
                port=port,
                database=values["DATABASE_NAME"],
            )
        )

    def get(self, job_id: str) -> JobRecord | None:
        try:
            with Session(self._engine) as session:
                job = session.scalar(select(Job).where(Job.job_id == job_id))
                return None if job is None else JobRecord(job.job_id, dict(job.details))
        except SQLAlchemyError as error:
            raise DatabaseUnavailableError("database read failed") from error

    def close(self) -> None:
        self._engine.dispose()
