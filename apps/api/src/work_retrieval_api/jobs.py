from typing import Protocol, runtime_checkable

from work_retrieval_database import DatabaseUnavailableError, JobRecord


class JobNotFoundError(LookupError):
    pass


@runtime_checkable
class JobRepository(Protocol):
    def get(self, job_id: str) -> JobRecord | None: ...

    def close(self) -> None: ...


__all__ = [
    "DatabaseUnavailableError",
    "JobNotFoundError",
    "JobRecord",
    "JobRepository",
]
