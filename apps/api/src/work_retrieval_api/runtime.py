from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from work_retrieval_core import SearchEngine, SearchQuery, SearchUnavailableError
from work_retrieval_database import (
    DatabaseSettings,
    JobStoreUnavailableError,
    SqlAlchemyJobReader,
)


class PostgresSearchEngine:
    """PostgreSQL-backed demo retrieval without query matching or ranking."""

    def __init__(self, jobs: SqlAlchemyJobReader) -> None:
        self._jobs = jobs
        self._closed = False
        jobs.check_connection()

    def search(self, query: SearchQuery, *, limit: int) -> tuple[str, ...]:
        if self._closed:
            raise SearchUnavailableError("PostgreSQL search engine is closed")
        if isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        try:
            return self._jobs.eligible_job_ids(search_date=query.search_date, limit=limit)
        except JobStoreUnavailableError as error:
            raise SearchUnavailableError("PostgreSQL search failed") from error

    def job_details(self, job_id: str) -> dict[str, str | None] | None:
        if self._closed:
            raise SearchUnavailableError("PostgreSQL search engine is closed")
        try:
            return self._jobs.job_details(job_id)
        except JobStoreUnavailableError as error:
            raise SearchUnavailableError("PostgreSQL job detail lookup failed") from error

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._jobs.close()


@runtime_checkable
class RetrievalRuntime(SearchEngine, Protocol):
    def job_details(self, job_id: str) -> dict[str, str | None] | None: ...


RuntimeFactory = Callable[[], RetrievalRuntime]


def runtime_from_environment() -> RetrievalRuntime:
    settings = DatabaseSettings.from_environment()
    jobs = SqlAlchemyJobReader.from_settings(settings)
    try:
        return PostgresSearchEngine(jobs)
    except Exception:
        jobs.close()
        raise
