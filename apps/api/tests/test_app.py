from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from work_retrieval_api import create_app
from work_retrieval_api.jobs import DatabaseUnavailableError, JobRecord
from work_retrieval_core import SearchQuery, SearchUnavailableError


class FakeEngine:
    def __init__(self, result: tuple[str, ...] = ("2", "1")) -> None:
        self.result = result
        self.queries: list[tuple[SearchQuery, int]] = []
        self.closed = False
        self.error: Exception | None = None

    def search(self, query: SearchQuery, *, limit: int) -> tuple[str, ...]:
        self.queries.append((query, limit))
        if self.error is not None:
            raise self.error
        return self.result

    def close(self) -> None:
        self.closed = True


class FakeJobRepository:
    def __init__(self) -> None:
        self.record = JobRecord("53256270", {"職務名稱": "口譯人員"})
        self.closed = False
        self.error: Exception | None = None

    def get(self, job_id: str) -> JobRecord | None:
        if self.error is not None:
            raise self.error
        return self.record if job_id == self.record.job_id else None

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def client(engine: FakeEngine) -> Callable[[], TestClient]:
    def build() -> TestClient:
        return TestClient(
            create_app(lambda: engine, FakeJobRepository),
            raise_server_exceptions=False,
        )

    return build


def test_valid_request_maps_to_engine_and_returns_closed_shape(
    client: Callable[[], TestClient], engine: FakeEngine
) -> None:
    with client() as http:
        response = http.post(
            "/api/v1/jobs/search",
            json={
                "query": "  後端工程師  ",
                "location_code": ["100100", "100100"],
                "duty_code": ["140200"],
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"request_id", "result"}
    assert body["request_id"].startswith("req_")
    assert body["result"] == [
        {"job_id": "2", "rank": 1},
        {"job_id": "1", "rank": 2},
    ]
    assert response.headers["X-Request-Id"] == body["request_id"]
    assert engine.queries == [(SearchQuery("後端工程師", ("100100",), ("140200",)), 10)]
    assert engine.closed


def test_more_than_fifty_codes_are_accepted(client: Callable[[], TestClient]) -> None:
    codes = [f"duty-{index}" for index in range(60)]
    with client() as http:
        response = http.post(
            "/api/v1/jobs/search",
            json={"query": "工程師", "duty_code": codes},
        )
    assert response.status_code == 200


def test_job_import_is_not_a_public_api(engine: FakeEngine) -> None:
    with TestClient(create_app(lambda: engine, FakeJobRepository)) as http:
        response = http.post("/api/v1/jobs/pull", json={"job_id": "53256270"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_persisted_job_can_be_read_by_numeric_id(engine: FakeEngine) -> None:
    with TestClient(create_app(lambda: engine, FakeJobRepository)) as http:
        response = http.get("/api/v1/job-details/53256270")

    assert response.status_code == 200
    assert response.json() == {
        "job_id": "53256270",
        "details": {"職務名稱": "口譯人員"},
    }


def test_missing_and_unavailable_job_data_use_safe_errors(engine: FakeEngine) -> None:
    repository = FakeJobRepository()
    with TestClient(create_app(lambda: engine, lambda: repository)) as http:
        missing = http.get("/api/v1/job-details/1")
        repository.error = DatabaseUnavailableError("private database detail")
        unavailable = http.get("/api/v1/job-details/53256270")

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "job_not_found"
    assert unavailable.status_code == 503
    assert unavailable.json()["error"] == {
        "code": "job_data_unavailable",
        "message": "Job details are temporarily unavailable.",
        "details": [],
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"query": " "},
        {"query": "x" * 513},
        {"query": "工程師", "location_code": None},
        {"query": "工程師", "duty_code": [""]},
        {"query": "工程師", "ks": "legacy"},
        {"query": "工程師", "c0": []},
        {"query": "工程師", "d0": []},
        {"query": "工程師", "empStr": []},
    ],
)
def test_invalid_contract_returns_422(
    client: Callable[[], TestClient], payload: dict[str, object]
) -> None:
    with client() as http:
        response = http.post("/api/v1/jobs/search", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_invalid_json_wrong_media_type_and_oversize_body(
    client: Callable[[], TestClient],
) -> None:
    with client() as http:
        invalid_json = http.post(
            "/api/v1/jobs/search",
            content="{",
            headers={"content-type": "application/json"},
        )
        wrong_type = http.post(
            "/api/v1/jobs/search",
            content="query=test",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        oversized = http.post(
            "/api/v1/jobs/search",
            content=json.dumps({"query": "x" * (16 * 1024)}),
            headers={"content-type": "application/json"},
        )

    assert invalid_json.status_code == 422
    assert wrong_type.status_code == 415
    assert oversized.status_code == 413


def test_chunked_body_is_rejected_before_unbounded_buffering(
    client: Callable[[], TestClient],
) -> None:
    def chunks() -> Iterator[bytes]:
        yield b'{"query":"'
        yield b"x" * (16 * 1024)
        yield b'"}'

    with client() as http:
        response = http.post(
            "/api/v1/jobs/search",
            content=chunks(),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


@pytest.mark.parametrize("method,status", [("get", 405), ("delete", 405)])
def test_method_and_path_errors_use_error_envelope(
    client: Callable[[], TestClient], method: str, status: int
) -> None:
    with client() as http:
        method_response = getattr(http, method)("/api/v1/jobs/search")
        missing_response = http.get("/missing")
    assert method_response.status_code == status
    assert method_response.json()["error"]["code"] == "method_not_allowed"
    assert missing_response.status_code == 404
    assert missing_response.json()["error"]["code"] == "not_found"


def test_unavailable_and_contract_violations_fail_closed(
    client: Callable[[], TestClient], engine: FakeEngine
) -> None:
    engine.error = SearchUnavailableError("private artifact path")
    with client() as http:
        unavailable = http.post("/api/v1/jobs/search", json={"query": "工程師"})
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["message"] == (
        "The search engine is temporarily unavailable."
    )

    engine.error = None
    engine.result = ("1", "1")
    with client() as http:
        invalid = http.post("/api/v1/jobs/search", json={"query": "工程師"})
    assert invalid.status_code == 500
    assert invalid.json()["error"]["message"] == "The request could not be completed."


@pytest.mark.parametrize(
    "invalid_result",
    [
        ["job-1"],
        tuple(f"job-{index}" for index in range(11)),
        ("",),
        ("job-1",),
        ("\uff11\uff12\uff13",),
        (1,),
    ],
)
def test_every_invalid_engine_result_fails_closed(
    invalid_result: object,
) -> None:
    class InvalidEngine(FakeEngine):
        def search(self, query: SearchQuery, *, limit: int) -> tuple[str, ...]:
            del query, limit
            return invalid_result  # type: ignore[return-value]

    with TestClient(
        create_app(InvalidEngine, FakeJobRepository),
        raise_server_exceptions=False,
    ) as http:
        response = http.post("/api/v1/jobs/search", json={"query": "工程師"})

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"


def test_factory_is_required_and_startup_errors_propagate() -> None:
    with pytest.raises(TypeError):
        create_app()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        create_app(FakeEngine)  # type: ignore[call-arg]

    def fail() -> FakeEngine:
        raise RuntimeError("artifacts are missing")

    with (
        pytest.raises(RuntimeError, match="artifacts are missing"),
        TestClient(create_app(fail, FakeJobRepository)),
    ):
        pass

    class InvalidEngine:
        def close(self) -> None:
            pass

    with (
        pytest.raises(TypeError, match="engine_factory must return a SearchEngine"),
        TestClient(create_app(InvalidEngine, FakeJobRepository)),  # type: ignore[arg-type]
    ):
        pass

    with (
        pytest.raises(TypeError, match="job_repository_factory must return a JobRepository"),
        TestClient(create_app(FakeEngine, InvalidEngine)),  # type: ignore[arg-type]
    ):
        pass

    engine = FakeEngine()

    def fail_repository() -> FakeJobRepository:
        raise RuntimeError("database configuration is missing")

    with (
        pytest.raises(RuntimeError, match="database configuration is missing"),
        TestClient(create_app(lambda: engine, fail_repository)),
    ):
        pass
    assert engine.closed


def test_access_log_does_not_include_query(
    client: Callable[[], TestClient], caplog: pytest.LogCaptureFixture
) -> None:
    secret_query = "不可寫入日誌的搜尋字串"
    caplog.set_level(logging.INFO, logger="work_retrieval.access")
    with client() as http:
        response = http.post("/api/v1/jobs/search", json={"query": secret_query})
    assert response.status_code == 200
    access_records = [
        record.message for record in caplog.records if record.name == "work_retrieval.access"
    ]
    assert len(access_records) == 1
    assert secret_query not in access_records[0]
    parsed = json.loads(access_records[0])
    assert parsed["query_len"] == len(secret_query)


def test_internal_error_logs_are_structured_and_sanitized(
    client: Callable[[], TestClient],
    engine: FakeEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_query = "exception 不可洩漏此查詢"
    engine.error = RuntimeError(secret_query)
    caplog.set_level(logging.INFO, logger="work_retrieval")

    with client() as http:
        response = http.post("/api/v1/jobs/search", json={"query": secret_query})

    assert response.status_code == 500
    records = [
        record.message for record in caplog.records if record.name.startswith("work_retrieval")
    ]
    assert len(records) == 2
    assert secret_query not in "".join(records)
    assert all(isinstance(json.loads(message), dict) for message in records)
