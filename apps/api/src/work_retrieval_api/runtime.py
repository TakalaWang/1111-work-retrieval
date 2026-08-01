from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from work_retrieval_core import SearchEngine, SearchQuery, SearchUnavailableError
from work_retrieval_database import DatabaseSettings, JobReader, SqlAlchemyJobReader

FAKE_RESULT_COUNT = 10


class DeterministicSearchEngine:
    """Temporary explicit runtime that returns one stable slice of real job IDs."""

    def __init__(self, job_ids: tuple[str, ...]) -> None:
        if len(job_ids) != FAKE_RESULT_COUNT:
            raise RuntimeError(f"deterministic search requires exactly {FAKE_RESULT_COUNT} job IDs")
        if any(not job_id.strip() for job_id in job_ids) or len(set(job_ids)) != len(job_ids):
            raise RuntimeError("deterministic search job IDs must be non-empty and unique")
        self._job_ids = job_ids
        self._closed = False

    def search(self, query: SearchQuery, *, limit: int) -> tuple[str, ...]:
        del query
        if self._closed:
            raise SearchUnavailableError("deterministic search engine is closed")
        if isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        return self._job_ids[:limit]

    def close(self) -> None:
        self._closed = True


@dataclass(frozen=True, slots=True)
class AppRuntime:
    search: SearchEngine
    jobs: JobReader


RuntimeFactory = Callable[[], AppRuntime]


def runtime_from_environment() -> AppRuntime:
    settings = DatabaseSettings.from_environment()
    jobs = SqlAlchemyJobReader.from_settings(settings)
    try:
        job_ids = jobs.first_job_ids(limit=FAKE_RESULT_COUNT)
        search = DeterministicSearchEngine(job_ids)
    except Exception:
        jobs.close()
        raise
    return AppRuntime(search=search, jobs=jobs)
