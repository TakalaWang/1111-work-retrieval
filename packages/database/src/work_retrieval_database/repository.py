from __future__ import annotations

import os
from calendar import monthrange
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy import URL, Engine, create_engine, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from .models import Job

ONE_DAY = timedelta(days=1)


def search_window(search_date: date) -> tuple[datetime, datetime]:
    month_index = search_date.year * 12 + search_date.month - 1 - 6
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    start_date = date(year, month, min(search_date.day, monthrange(year, month)[1]))
    return (
        datetime.combine(start_date, time.min),
        datetime.combine(search_date, time.min) + ONE_DAY,
    )


class JobStoreUnavailableError(RuntimeError):
    """PostgreSQL could not serve a job read without exposing internal details."""


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    host: str
    port: int
    name: str
    user: str
    password: str = field(repr=False)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> DatabaseSettings:
        values = os.environ if environment is None else environment
        names = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")
        missing = [
            name
            for name in names
            if not values.get(name) or (name != "DB_PASSWORD" and not values[name].strip())
        ]
        if missing:
            raise RuntimeError(f"missing required database settings: {', '.join(missing)}")

        raw_port = values["DB_PORT"]
        if not raw_port.isdecimal() or not 1 <= int(raw_port) <= 65_535:
            raise RuntimeError("DB_PORT must be an integer between 1 and 65535")

        return cls(
            host=values["DB_HOST"].strip(),
            port=int(raw_port),
            name=values["DB_NAME"].strip(),
            user=values["DB_USER"].strip(),
            password=values["DB_PASSWORD"],
        )

    def sqlalchemy_url(self) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.name,
        )


class SqlAlchemyJobReader:
    def __init__(
        self,
        engine: Engine,
        *,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self._engine = engine
        self._session_factory = session_factory or sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def from_settings(cls, settings: DatabaseSettings) -> SqlAlchemyJobReader:
        engine = create_engine(
            settings.sqlalchemy_url(),
            connect_args={
                "connect_timeout": 30,
                "options": "-c statement_timeout=5000",
                "sslmode": "require",
            },
            pool_pre_ping=True,
            poolclass=NullPool,
        )
        return cls(engine)

    def check_connection(self) -> None:
        try:
            with self._engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError as error:
            raise JobStoreUnavailableError("PostgreSQL connection check failed") from error

    def eligible_job_ids(self, *, search_date: date, limit: int) -> tuple[str, ...]:
        if isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        window_start, window_end = search_window(search_date)
        try:
            with self._session_factory() as session:
                statement = (
                    select(Job.job_id)
                    .where(
                        Job.source_modified_at >= window_start,
                        Job.source_modified_at < window_end,
                    )
                    .order_by(Job.source_row)
                    .limit(limit)
                )
                return tuple(session.scalars(statement).all())
        except SQLAlchemyError as error:
            raise JobStoreUnavailableError("PostgreSQL job lookup failed") from error

    def job_details(self, job_id: str) -> dict[str, str | None] | None:
        try:
            with self._session_factory() as session:
                job = session.scalar(select(Job).where(Job.job_id == job_id))
        except SQLAlchemyError as error:
            raise JobStoreUnavailableError("PostgreSQL job detail lookup failed") from error
        if job is None:
            return None
        return {
            "職務名稱": job.title,
            "工作城市": job.work_city,
            "薪資": job.salary_text,
            "職務大類": job.duty_major,
            "職務中類": job.duty_middle,
            "職務小類": job.duty_minor,
            "職務內容": job.description,
            "工作經驗需求": job.experience_requirement,
            "學歷需求": job.education_requirement,
            "工作技能": job.work_skills,
            "職缺最後修改時間": job.source_modified_at.isoformat(timespec="milliseconds"),
        }

    def close(self) -> None:
        self._engine.dispose()
