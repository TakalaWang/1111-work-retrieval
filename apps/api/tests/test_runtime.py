from __future__ import annotations

from typing import cast

import pytest
from work_retrieval_api import runtime as runtime_module
from work_retrieval_api.runtime import (
    AppRuntime,
    DeterministicSearchEngine,
    runtime_from_environment,
)
from work_retrieval_core import SearchEngine, SearchQuery, SearchUnavailableError
from work_retrieval_database import DatabaseSettings, JobSnapshot


class StubJobReader:
    def __init__(self, job_ids: tuple[str, ...]) -> None:
        self.job_ids = job_ids
        self.limits: list[int] = []
        self.closed = False

    def first_job_ids(self, *, limit: int) -> tuple[str, ...]:
        self.limits.append(limit)
        return self.job_ids[:limit]

    def get(self, job_id: str) -> JobSnapshot | None:
        del job_id
        return None

    def close(self) -> None:
        self.closed = True


def _job_ids(count: int = 10) -> tuple[str, ...]:
    return tuple(f"job-{index}" for index in range(count))


def test_deterministic_search_returns_the_same_stable_slice() -> None:
    engine = DeterministicSearchEngine(_job_ids())

    first = engine.search(SearchQuery("backend"), limit=3)
    second = engine.search(
        SearchQuery("different", location_codes=("taipei",), duty_codes=("engineering",)),
        limit=3,
    )

    assert first == second == ("job-0", "job-1", "job-2")


def test_deterministic_search_rejects_invalid_seed_and_closed_use() -> None:
    with pytest.raises(RuntimeError, match="exactly 10"):
        DeterministicSearchEngine(_job_ids(9))
    with pytest.raises(RuntimeError, match="non-empty and unique"):
        DeterministicSearchEngine((*_job_ids(9), "job-0"))

    engine = DeterministicSearchEngine(_job_ids())
    engine.close()
    engine.close()
    with pytest.raises(SearchUnavailableError, match="closed"):
        engine.search(SearchQuery("backend"), limit=10)


def test_environment_runtime_uses_real_job_ids_and_owns_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = DatabaseSettings("db.internal", 5432, "work_retrieval", "service", "secret")
    jobs = StubJobReader(_job_ids())
    monkeypatch.setattr(
        runtime_module.DatabaseSettings,
        "from_environment",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(
        runtime_module.SqlAlchemyJobReader,
        "from_settings",
        classmethod(lambda cls, actual: jobs if actual is settings else None),
    )

    runtime = runtime_from_environment()

    assert isinstance(runtime, AppRuntime)
    assert isinstance(runtime.search, SearchEngine)
    assert runtime.jobs is jobs
    assert jobs.limits == [10]
    assert runtime.search.search(SearchQuery("ignored"), limit=10) == _job_ids()


def test_environment_runtime_fails_closed_and_closes_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = DatabaseSettings("db.internal", 5432, "work_retrieval", "service", "secret")
    jobs = StubJobReader(_job_ids(9))
    monkeypatch.setattr(
        runtime_module.DatabaseSettings,
        "from_environment",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(
        runtime_module.SqlAlchemyJobReader,
        "from_settings",
        classmethod(lambda cls, actual: jobs if actual is settings else None),
    )

    with pytest.raises(RuntimeError, match="exactly 10"):
        runtime_from_environment()

    assert jobs.closed


def test_runtime_types_remain_structural() -> None:
    jobs = StubJobReader(_job_ids())
    runtime = AppRuntime(search=DeterministicSearchEngine(_job_ids()), jobs=jobs)

    assert cast(SearchEngine, runtime.search).search(SearchQuery("query"), limit=1) == ("job-0",)
