from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from sqlalchemy import URL, Engine, create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .models import Job


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


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    job_id: str
    title: str
    description: str | None
    salary_text: str
    salary_min: Decimal | None
    salary_max: Decimal | None
    duty_major: str | None
    duty_middle: str | None
    duty_minor: str | None
    job_attribute: str | None
    work_hours: str | None
    work_hours_description: str | None
    work_city: str | None
    education_requirement: str | None
    major_requirement_1: str | None
    major_requirement_2: str | None
    major_requirement_3: str | None
    experience_requirement: str | None
    language_1: str | None
    language_1_listening: str | None
    language_1_speaking: str | None
    language_1_reading: str | None
    language_1_writing: str | None
    language_2: str | None
    language_2_listening: str | None
    language_2_speaking: str | None
    language_2_reading: str | None
    language_2_writing: str | None
    computer_skills: str | None
    professional_certifications: str | None
    work_skills: str | None
    additional_conditions: str | None
    management_count: str | None
    requires_travel: str | None
    vendor_id: str
    industry_major: str | None
    industry_middle: str | None
    industry_minor: str | None
    source_modified_at: datetime


@runtime_checkable
class JobReader(Protocol):
    def first_job_ids(self, *, limit: int) -> tuple[str, ...]: ...

    def get(self, job_id: str) -> JobSnapshot | None: ...

    def close(self) -> None: ...


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
        )
        return cls(engine)

    def first_job_ids(self, *, limit: int) -> tuple[str, ...]:
        if isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        try:
            with self._session_factory() as session:
                statement = select(Job.job_id).order_by(Job.source_row).limit(limit)
                return tuple(session.scalars(statement).all())
        except SQLAlchemyError as error:
            raise JobStoreUnavailableError("PostgreSQL job lookup failed") from error

    def get(self, job_id: str) -> JobSnapshot | None:
        try:
            with self._session_factory() as session:
                job = session.scalar(select(Job).where(Job.job_id == job_id))
                return None if job is None else _snapshot(job)
        except SQLAlchemyError as error:
            raise JobStoreUnavailableError("PostgreSQL job lookup failed") from error

    def close(self) -> None:
        self._engine.dispose()


def _snapshot(job: Job) -> JobSnapshot:
    return JobSnapshot(
        job_id=job.job_id,
        title=job.title,
        description=job.description,
        salary_text=job.salary_text,
        salary_min=job.salary_min,
        salary_max=job.salary_max,
        duty_major=job.duty_major,
        duty_middle=job.duty_middle,
        duty_minor=job.duty_minor,
        job_attribute=job.job_attribute,
        work_hours=job.work_hours,
        work_hours_description=job.work_hours_description,
        work_city=job.work_city,
        education_requirement=job.education_requirement,
        major_requirement_1=job.major_requirement_1,
        major_requirement_2=job.major_requirement_2,
        major_requirement_3=job.major_requirement_3,
        experience_requirement=job.experience_requirement,
        language_1=job.language_1,
        language_1_listening=job.language_1_listening,
        language_1_speaking=job.language_1_speaking,
        language_1_reading=job.language_1_reading,
        language_1_writing=job.language_1_writing,
        language_2=job.language_2,
        language_2_listening=job.language_2_listening,
        language_2_speaking=job.language_2_speaking,
        language_2_reading=job.language_2_reading,
        language_2_writing=job.language_2_writing,
        computer_skills=job.computer_skills,
        professional_certifications=job.professional_certifications,
        work_skills=job.work_skills,
        additional_conditions=job.additional_conditions,
        management_count=job.management_count,
        requires_travel=job.requires_travel,
        vendor_id=job.vendor_id,
        industry_major=job.industry_major,
        industry_middle=job.industry_middle,
        industry_minor=job.industry_minor,
        source_modified_at=job.source_modified_at,
    )
