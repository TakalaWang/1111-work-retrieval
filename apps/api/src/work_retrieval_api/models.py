from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from work_retrieval_database import JobSnapshot

Code = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]
Query = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=512),
]
JobId = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1),
]


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Query
    location_code: list[Code] = Field(default_factory=list)
    duty_code: list[Code] = Field(default_factory=list)

    @field_validator("location_code", "duty_code")
    @classmethod
    def deduplicate_codes(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))


class SearchResultItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    rank: Annotated[int, Field(ge=1, le=10)]


class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    result: list[SearchResultItem]


class JobDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    title: str
    description: str | None
    salary_text: str
    salary_min: str | None
    salary_max: str | None
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


class JobDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    job: JobDetail


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    code: str
    message: str


class ErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: list[ErrorDetail]


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    error: ErrorBody


def job_detail(snapshot: JobSnapshot) -> JobDetail:
    return JobDetail(
        job_id=snapshot.job_id,
        title=snapshot.title,
        description=snapshot.description,
        salary_text=snapshot.salary_text,
        salary_min=None if snapshot.salary_min is None else format(snapshot.salary_min, "f"),
        salary_max=None if snapshot.salary_max is None else format(snapshot.salary_max, "f"),
        duty_major=snapshot.duty_major,
        duty_middle=snapshot.duty_middle,
        duty_minor=snapshot.duty_minor,
        job_attribute=snapshot.job_attribute,
        work_hours=snapshot.work_hours,
        work_hours_description=snapshot.work_hours_description,
        work_city=snapshot.work_city,
        education_requirement=snapshot.education_requirement,
        major_requirement_1=snapshot.major_requirement_1,
        major_requirement_2=snapshot.major_requirement_2,
        major_requirement_3=snapshot.major_requirement_3,
        experience_requirement=snapshot.experience_requirement,
        language_1=snapshot.language_1,
        language_1_listening=snapshot.language_1_listening,
        language_1_speaking=snapshot.language_1_speaking,
        language_1_reading=snapshot.language_1_reading,
        language_1_writing=snapshot.language_1_writing,
        language_2=snapshot.language_2,
        language_2_listening=snapshot.language_2_listening,
        language_2_speaking=snapshot.language_2_speaking,
        language_2_reading=snapshot.language_2_reading,
        language_2_writing=snapshot.language_2_writing,
        computer_skills=snapshot.computer_skills,
        professional_certifications=snapshot.professional_certifications,
        work_skills=snapshot.work_skills,
        additional_conditions=snapshot.additional_conditions,
        management_count=snapshot.management_count,
        requires_travel=snapshot.requires_travel,
        vendor_id=snapshot.vendor_id,
        industry_major=snapshot.industry_major,
        industry_middle=snapshot.industry_middle,
        industry_minor=snapshot.industry_minor,
        source_modified_at=snapshot.source_modified_at,
    )


def validation_details(errors: Sequence[Mapping[str, Any]]) -> list[ErrorDetail]:
    return [
        ErrorDetail(
            field=".".join(str(part) for part in error["loc"] if part != "body"),
            code=str(error["type"]),
            message=str(error["msg"]),
        )
        for error in errors
    ]
