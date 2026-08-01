from collections.abc import Mapping, Sequence
from datetime import date
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Code = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]
Query = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=512),
]
JobId = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9]+$")]
MIN_SEARCH_DATE = date(1, 7, 1)
MAX_SEARCH_DATE = date(9999, 12, 30)
SearchDate = Annotated[
    date,
    Field(
        strict=True,
        ge=MIN_SEARCH_DATE,
        le=MAX_SEARCH_DATE,
        description="ISO date from 0001-07-01 through 9999-12-30 inclusive.",
    ),
]


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Query
    search_date: SearchDate | None = None
    location_code: list[Code] = Field(default_factory=list)
    duty_code: list[Code] = Field(default_factory=list)

    @field_validator("search_date", mode="before")
    @classmethod
    def parse_iso_date(cls, value: object) -> date | None:
        if value is None:
            return value
        if not isinstance(value, str):
            raise ValueError("search_date must be an ISO date")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("search_date must be an ISO date") from error
        if parsed.isoformat() != value:
            raise ValueError("search_date must be an ISO date")
        return parsed

    @field_validator("location_code", "duty_code")
    @classmethod
    def deduplicate_codes(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))


class SearchResultItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: JobId
    rank: Annotated[int, Field(ge=1, le=10)]


class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    result: Annotated[list[SearchResultItem], Field(max_length=10)]


class JobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: JobId
    details: dict[str, str | None]


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


def validation_details(errors: Sequence[Mapping[str, Any]]) -> list[ErrorDetail]:
    return [
        ErrorDetail(
            field=".".join(str(part) for part in error["loc"] if part != "body"),
            code=str(error["type"]),
            message=str(error["msg"]),
        )
        for error in errors
    ]
