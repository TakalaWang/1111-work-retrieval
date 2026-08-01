from collections.abc import Mapping, Sequence
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Code = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]
Query = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=512),
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
