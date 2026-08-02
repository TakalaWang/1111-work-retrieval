from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import URL, Engine, create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from .models import Job


class JobStoreUnavailableError(RuntimeError):
    """PostgreSQL could not serve a job read without exposing internal details."""


@dataclass(frozen=True, slots=True)
class JobMetadataRecord:
    job_id: str
    work_city: str | None
    duty_major: str | None
    duty_middle: str | None
    duty_minor: str | None
    source_modified_at: datetime
    job_attribute: str | None
    work_hours: str | None
    experience_requirement: str | None
    management_count: str | None
    education_requirement: str | None
    salary_text: str
    salary_min: Decimal | None
    salary_max: Decimal | None


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

    def metadata_for_job_ids(self, job_ids: tuple[str, ...]) -> tuple[JobMetadataRecord, ...]:
        if len(set(job_ids)) != len(job_ids) or any(
            not job_id.isascii() or not job_id.isdecimal() for job_id in job_ids
        ):
            raise ValueError("job_ids must contain unique ASCII decimal identifiers")
        if not job_ids:
            return ()
        try:
            with self._session_factory() as session:
                statement = select(
                    Job.job_id,
                    Job.work_city,
                    Job.duty_major,
                    Job.duty_middle,
                    Job.duty_minor,
                    Job.source_modified_at,
                    Job.job_attribute,
                    Job.work_hours,
                    Job.experience_requirement,
                    Job.management_count,
                    Job.education_requirement,
                    Job.salary_text,
                    Job.salary_min,
                    Job.salary_max,
                ).where(Job.job_id.in_(job_ids))
                rows = session.execute(statement).all()
        except SQLAlchemyError as error:
            raise JobStoreUnavailableError("PostgreSQL job metadata lookup failed") from error
        return tuple(JobMetadataRecord(*row) for row in rows)

    def job_details(self, job_id: str) -> dict[str, str | None] | None:
        if not job_id.isascii() or not job_id.isdecimal():
            raise ValueError("job_id must be an ASCII decimal identifier")
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
