from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest
from work_retrieval_api import runtime as runtime_module
from work_retrieval_api.runtime import (
    PostgresSearchEngine,
    runtime_from_environment,
)
from work_retrieval_core import SearchEngine, SearchQuery, SearchUnavailableError
from work_retrieval_database import DatabaseSettings


class StubJobReader:
    def __init__(self, job_ids: tuple[str, ...]) -> None:
        self.job_ids = job_ids
        self.limits: list[int] = []
        self.closed = False

    def check_connection(self) -> None:
        self.checked = True

    def eligible_job_ids(self, *, search_date: date, limit: int) -> tuple[str, ...]:
        self.limits.append(limit)
        self.search_dates.append(search_date)
        return self.job_ids[:limit]

    def job_details(self, job_id: str) -> dict[str, str | None] | None:
        return {"職務名稱": f"職缺 {job_id}"} if job_id in self.job_ids else None

    def close(self) -> None:
        self.closed = True


def _job_ids(count: int = 10) -> tuple[str, ...]:
    return tuple(str(index + 1) for index in range(count))


def test_postgres_search_maps_as_of_date_to_reader() -> None:
    jobs = StubJobReader(_job_ids())
    jobs.search_dates = []
    jobs.checked = False
    engine = PostgresSearchEngine(jobs)

    result = engine.search(SearchQuery("backend", date(2026, 6, 8)), limit=3)

    assert jobs.checked
    assert result == ("1", "2", "3")
    assert jobs.search_dates == [date(2026, 6, 8)]
    assert engine.job_details("1") == {"職務名稱": "職缺 1"}
    assert engine.job_details("999") is None


def test_postgres_search_rejects_closed_use() -> None:
    jobs = StubJobReader(_job_ids())
    jobs.search_dates = []
    jobs.checked = False
    engine = PostgresSearchEngine(jobs)
    engine.close()
    engine.close()
    with pytest.raises(SearchUnavailableError, match="closed"):
        engine.search(SearchQuery("backend", date(2026, 6, 8)), limit=10)


def test_environment_runtime_uses_real_job_ids_and_owns_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = DatabaseSettings("db.internal", 5432, "work_retrieval", "service", "secret")
    jobs = StubJobReader(_job_ids())
    jobs.search_dates = []
    jobs.checked = False
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

    engine = runtime_from_environment()

    assert isinstance(engine, SearchEngine)
    assert jobs.checked
    assert not jobs.closed
    assert engine.search(SearchQuery("ignored", date(2026, 6, 8)), limit=10) == _job_ids()
    engine.close()
    assert jobs.closed


def test_environment_runtime_fails_closed_and_closes_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = DatabaseSettings("db.internal", 5432, "work_retrieval", "service", "secret")
    jobs = StubJobReader(_job_ids(9))
    jobs.search_dates = []
    jobs.checked = False
    jobs.check_connection = MagicMock(side_effect=RuntimeError("database unavailable"))
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

    with pytest.raises(RuntimeError, match="database unavailable"):
        runtime_from_environment()

    assert jobs.closed
