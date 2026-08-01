from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Integer, Numeric, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    salary_text: Mapped[str] = mapped_column(Text)
    salary_min: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    salary_max: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    duty_major: Mapped[str | None] = mapped_column(Text, nullable=True)
    duty_middle: Mapped[str | None] = mapped_column(Text, nullable=True)
    duty_minor: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_attribute: Mapped[str | None] = mapped_column(Text, nullable=True)
    work_hours: Mapped[str | None] = mapped_column(Text, nullable=True)
    work_hours_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    work_city: Mapped[str | None] = mapped_column(Text, nullable=True)
    education_requirement: Mapped[str | None] = mapped_column(Text, nullable=True)
    major_requirement_1: Mapped[str | None] = mapped_column(Text, nullable=True)
    major_requirement_2: Mapped[str | None] = mapped_column(Text, nullable=True)
    major_requirement_3: Mapped[str | None] = mapped_column(Text, nullable=True)
    experience_requirement: Mapped[str | None] = mapped_column(Text, nullable=True)
    language_1: Mapped[str | None] = mapped_column(Text, nullable=True)
    language_1_listening: Mapped[str | None] = mapped_column(Text, nullable=True)
    language_1_speaking: Mapped[str | None] = mapped_column(Text, nullable=True)
    language_1_reading: Mapped[str | None] = mapped_column(Text, nullable=True)
    language_1_writing: Mapped[str | None] = mapped_column(Text, nullable=True)
    language_2: Mapped[str | None] = mapped_column(Text, nullable=True)
    language_2_listening: Mapped[str | None] = mapped_column(Text, nullable=True)
    language_2_speaking: Mapped[str | None] = mapped_column(Text, nullable=True)
    language_2_reading: Mapped[str | None] = mapped_column(Text, nullable=True)
    language_2_writing: Mapped[str | None] = mapped_column(Text, nullable=True)
    computer_skills: Mapped[str | None] = mapped_column(Text, nullable=True)
    professional_certifications: Mapped[str | None] = mapped_column(Text, nullable=True)
    work_skills: Mapped[str | None] = mapped_column(Text, nullable=True)
    additional_conditions: Mapped[str | None] = mapped_column(Text, nullable=True)
    management_count: Mapped[str | None] = mapped_column(Text, nullable=True)
    requires_travel: Mapped[str | None] = mapped_column(Text, nullable=True)
    vendor_id: Mapped[str] = mapped_column(Text)
    industry_major: Mapped[str | None] = mapped_column(Text, nullable=True)
    industry_middle: Mapped[str | None] = mapped_column(Text, nullable=True)
    industry_minor: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_modified_at: Mapped[datetime] = mapped_column(DateTime(timezone=False))
    source_row: Mapped[int] = mapped_column(Integer, unique=True)
