from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import fields
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from work_retrieval_api import AppRuntime, create_app
from work_retrieval_core import SearchQuery, SearchUnavailableError
from work_retrieval_database import JobSnapshot, JobStoreUnavailableError


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


class FakeJobReader:
    def __init__(self) -> None:
        self.records: dict[str, JobSnapshot] = {}
        self.requested: list[str] = []
        self.closed = False
        self.error: Exception | None = None

    def first_job_ids(self, *, limit: int) -> tuple[str, ...]:
        return tuple(self.records)[:limit]

    def get(self, job_id: str) -> JobSnapshot | None:
        self.requested.append(job_id)
        if self.error is not None:
            raise self.error
        return self.records.get(job_id)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def jobs() -> FakeJobReader:
    return FakeJobReader()


@pytest.fixture
def client(engine: FakeEngine, jobs: FakeJobReader) -> Callable[[], TestClient]:
    def build() -> TestClient:
        return TestClient(
            create_app(lambda: AppRuntime(search=engine, jobs=jobs)),
            raise_server_exceptions=False,
        )

    return build


def test_valid_request_maps_to_engine_and_returns_closed_shape(
    client: Callable[[], TestClient], engine: FakeEngine, jobs: FakeJobReader
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
    assert jobs.closed


def test_more_than_fifty_codes_are_accepted(client: Callable[[], TestClient]) -> None:
    codes = [f"duty-{index}" for index in range(60)]
    with client() as http:
        response = http.post(
            "/api/v1/jobs/search",
            json={"query": "工程師", "duty_code": codes},
        )
    assert response.status_code == 200


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


def test_body_rules_apply_to_detail_get_without_breaking_bodyless_get(
    client: Callable[[], TestClient],
) -> None:
    def oversized_chunks() -> Iterator[bytes]:
        yield b"x" * (8 * 1024)
        yield b"x" * (8 * 1024 + 1)

    with client() as http:
        bodyless = http.get("/api/v1/jobs/999999")
        oversized = http.request(
            "GET",
            "/api/v1/jobs/999999",
            content=oversized_chunks(),
            headers={"content-type": "application/json"},
        )
        wrong_type = http.request(
            "GET",
            "/api/v1/jobs/999999",
            content=b"{}",
            headers={"content-type": "text/plain"},
        )

    assert bodyless.status_code == 404
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "payload_too_large"
    assert wrong_type.status_code == 415
    assert wrong_type.json()["error"]["code"] == "unsupported_media_type"


@pytest.mark.parametrize("method,status", [("put", 405), ("delete", 405)])
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
        ["1"],
        tuple(str(index) for index in range(11)),
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
        create_app(lambda: AppRuntime(search=InvalidEngine(), jobs=FakeJobReader())),
        raise_server_exceptions=False,
    ) as http:
        response = http.post("/api/v1/jobs/search", json={"query": "工程師"})

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"


def test_factory_is_required_and_startup_errors_propagate() -> None:
    with pytest.raises(TypeError):
        create_app()  # type: ignore[call-arg]

    def fail() -> AppRuntime:
        raise RuntimeError("artifacts are missing")

    with (
        pytest.raises(RuntimeError, match="artifacts are missing"),
        TestClient(create_app(fail)),
    ):
        pass

    class InvalidEngine:
        def close(self) -> None:
            pass

    jobs = FakeJobReader()
    with (
        pytest.raises(TypeError, match="runtime search must implement SearchEngine"),
        TestClient(
            create_app(lambda: AppRuntime(search=InvalidEngine(), jobs=jobs))  # type: ignore[arg-type]
        ),
    ):
        pass
    assert jobs.closed


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


def _job_snapshot() -> JobSnapshot:
    values: dict[str, Any] = {name: f"value:{name}" for name in JobSnapshot.__dataclass_fields__}
    values.update(
        job_id="123456",
        salary_min=Decimal("1234567890.10"),
        salary_max=Decimal("9999999999.99"),
        source_modified_at=datetime(2026, 8, 1, 12, 30, 45, 123000),
    )
    return JobSnapshot(**values)


def test_job_detail_returns_all_source_fields_without_lineage(
    client: Callable[[], TestClient], jobs: FakeJobReader
) -> None:
    snapshot = _job_snapshot()
    jobs.records[snapshot.job_id] = snapshot

    with client() as http:
        response = http.get("/api/v1/jobs/123456")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"request_id", "job"}
    assert response.headers["X-Request-Id"] == body["request_id"]
    assert set(body["job"]) == set(snapshot.__dataclass_fields__)
    assert len(body["job"]) == 39
    assert "source_row" not in body["job"]
    snapshot_values = {field.name: getattr(snapshot, field.name) for field in fields(snapshot)}
    expected = {
        name: (
            format(value, "f")
            if isinstance(value, Decimal)
            else value.isoformat()
            if isinstance(value, datetime)
            else value
        )
        for name, value in snapshot_values.items()
    }
    assert all(value is not None for value in snapshot_values.values())
    for field_name, expected_value in expected.items():
        assert body["job"][field_name] == expected_value
    assert body["job"]["salary_min"] == "1234567890.10"
    assert body["job"]["salary_max"] == "9999999999.99"
    assert body["job"]["source_modified_at"] == "2026-08-01T12:30:45.123000"
    assert jobs.requested == ["123456"]


def test_job_detail_not_found_uses_shared_error_envelope(
    client: Callable[[], TestClient],
) -> None:
    with client() as http:
        response = http.get("/api/v1/jobs/999999")

    body = response.json()
    assert response.status_code == 404
    assert set(body) == {"request_id", "error"}
    assert body["error"] == {
        "code": "job_not_found",
        "message": "The requested job was not found.",
        "details": [],
    }
    assert response.headers["X-Request-Id"] == body["request_id"]


def test_job_detail_database_failure_is_sanitized(
    client: Callable[[], TestClient], jobs: FakeJobReader, caplog: pytest.LogCaptureFixture
) -> None:
    private_detail = "postgresql://user:password@private-host/work_retrieval"
    jobs.error = JobStoreUnavailableError(private_detail)
    caplog.set_level(logging.INFO, logger="work_retrieval")

    with client() as http:
        response = http.get("/api/v1/jobs/123456")

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "job_store_unavailable",
        "message": "Job details are temporarily unavailable.",
        "details": [],
    }
    assert private_detail not in response.text
    assert private_detail not in "".join(record.message for record in caplog.records)


def test_job_detail_rejects_blank_job_ids(client: Callable[[], TestClient]) -> None:
    with client() as http:
        response = http.get("/api/v1/jobs/%20")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
