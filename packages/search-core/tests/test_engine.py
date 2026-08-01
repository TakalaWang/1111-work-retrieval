from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from threading import Lock

import pytest
from work_retrieval_core import (
    CandidateEvidence,
    CandidateRequest,
    ProductionSearchEngine,
    RetrievalPorts,
    RuntimeManifest,
    SearchQuery,
    SearchUnavailableError,
)

DEMO_AS_OF = datetime(2026, 6, 8, tzinfo=UTC)


def _manifest(*, artifact_key: str | None = None) -> dict[str, object]:
    artifacts: dict[str, object] = {
        "embeddings/whole-qwen.f16": {
            "kind": "embedding",
            "sha256": "b" * 64,
            "size_bytes": 84,
        }
    }
    if artifact_key is not None:
        artifacts[artifact_key] = {
            "kind": "index",
            "sha256": "a" * 64,
            "size_bytes": 42,
        }
    return {
        "schema_version": 1,
        "artifacts": artifacts,
    }


class StubRetriever:
    def __init__(self, candidates: tuple[CandidateEvidence, ...] = ()) -> None:
        self.candidates = candidates
        self.requests: list[tuple[CandidateRequest, int]] = []
        self.closed = False
        self.error: Exception | None = None
        self._lock = Lock()

    def retrieve(self, request: CandidateRequest, *, limit: int) -> tuple[CandidateEvidence, ...]:
        with self._lock:
            self.requests.append((request, limit))
        if self.error is not None:
            raise self.error
        return self.candidates

    def close(self) -> None:
        self.closed = True


def _candidate(job_id: str, score: float, days_ago: int) -> CandidateEvidence:
    return CandidateEvidence(job_id, score, DEMO_AS_OF - timedelta(days=days_ago))


def test_search_query_is_immutable() -> None:
    query = SearchQuery("後端工程師", ("100100",), ("140200",))

    with pytest.raises(FrozenInstanceError):
        query.text = "changed"  # type: ignore[misc]


def test_manifest_matches_committed_artifact_contract() -> None:
    unknown = _manifest()
    unknown["fallback"] = "legacy"
    with pytest.raises(RuntimeError, match="only schema_version and artifacts"):
        RuntimeManifest.from_dict(unknown)

    invalid_hash = _manifest()
    invalid_hash["artifacts"]["embeddings/whole-qwen.f16"]["sha256"] = "mutable"  # type: ignore[index]
    with pytest.raises(RuntimeError, match="invalid sha256"):
        RuntimeManifest.from_dict(invalid_hash)


def test_multiview_requires_both_manifest_artifact_and_port() -> None:
    with pytest.raises(RuntimeError, match="manifest artifact"):
        ProductionSearchEngine(
            RuntimeManifest.from_dict(_manifest()),
            RetrievalPorts(StubRetriever(), StubRetriever(), StubRetriever()),
            enable_multiview_maxsim=True,
            multiview_artifact_key="indexes/multiview-maxsim.bin",
            clock=lambda: DEMO_AS_OF,
        )

    manifest = RuntimeManifest.from_dict(_manifest(artifact_key="indexes/multiview-maxsim.bin"))
    with pytest.raises(RuntimeError, match="requires its configured retrieval port"):
        ProductionSearchEngine(
            manifest,
            RetrievalPorts(StubRetriever(), StubRetriever()),
            enable_multiview_maxsim=True,
            multiview_artifact_key="indexes/multiview-maxsim.bin",
            clock=lambda: DEMO_AS_OF,
        )


def test_dynamic_as_of_filters_before_top_k_and_retains_future_rows() -> None:
    lexical = StubRetriever(
        (
            _candidate("1", 10.0, 0),
            CandidateEvidence("2", 9.0, DEMO_AS_OF + timedelta(days=1)),
        )
    )
    dense = StubRetriever((_candidate("3", 0.8, 30), _candidate("1", 0.7, 0)))
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(lexical, dense),
        clock=lambda: DEMO_AS_OF,
    )

    result = engine.search(
        SearchQuery("資料工程師", ("100100",), ("140200",)),
        limit=3,
    )

    expected_request = CandidateRequest(
        "資料工程師",
        ("100100",),
        ("140200",),
        DEMO_AS_OF,
        DEMO_AS_OF - timedelta(days=180),
    )
    assert lexical.requests == dense.requests == [(expected_request, 200)]
    assert result.job_ids == ("1", "3", "2")
    assert result.trace.as_of == DEMO_AS_OF
    assert result.trace.eligible_from == DEMO_AS_OF - timedelta(days=180)
    assert result.trace.location_filter_applied
    assert result.trace.duty_filter_applied
    assert next(item for item in result.trace.results if item.job_id == "2").freshness_score == 0
    assert [(lane.name, lane.status, lane.reason) for lane in result.trace.lanes[-5:]] == [
        ("qwen_dense_multiview_maxsim", "disabled", "feature_flag_disabled"),
        ("graph", "disabled", "ablation_not_approved"),
        ("reranker", "disabled", "calibration_not_approved"),
        ("ltr", "disabled", "calibration_not_approved"),
        ("guardrail", "disabled", "calibration_not_approved"),
    ]
    engine.close()


def test_as_of_is_evaluated_for_each_request() -> None:
    values = iter((DEMO_AS_OF, DEMO_AS_OF + timedelta(days=1)))
    lexical = StubRetriever()
    dense = StubRetriever()
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(lexical, dense),
        clock=lambda: next(values),
    )

    first = engine.search(SearchQuery("工程師"), limit=10)
    second = engine.search(SearchQuery("工程師"), limit=10)

    assert first.trace.as_of == DEMO_AS_OF
    assert second.trace.as_of == DEMO_AS_OF + timedelta(days=1)
    assert dense.requests[0][0].minimum_updated_at != dense.requests[1][0].minimum_updated_at
    engine.close()


def test_temporal_contract_violation_and_lane_failure_fail_closed() -> None:
    stale = StubRetriever((_candidate("1", 1.0, 181),))
    healthy = StubRetriever()
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(stale, healthy),
        clock=lambda: DEMO_AS_OF,
    )
    with pytest.raises(SearchUnavailableError, match="temporal eligibility"):
        engine.search(SearchQuery("工程師"), limit=10)
    engine.close()

    broken = StubRetriever()
    broken.error = RuntimeError("private endpoint details")
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(broken, StubRetriever()),
        clock=lambda: DEMO_AS_OF,
    )
    with pytest.raises(SearchUnavailableError, match="required retrieval lane") as error:
        engine.search(SearchQuery("工程師"), limit=10)
    assert "private endpoint" not in str(error.value)
    engine.close()


def test_multiview_runs_only_when_explicitly_enabled() -> None:
    disabled_multiview = StubRetriever((_candidate("9", 1.0, 1),))
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(StubRetriever(), StubRetriever(), disabled_multiview),
        clock=lambda: DEMO_AS_OF,
    )
    assert engine.search(SearchQuery("工程師"), limit=10).job_ids == ()
    assert disabled_multiview.requests == []
    engine.close()

    enabled_multiview = StubRetriever((_candidate("9", 1.0, 1),))
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest(artifact_key="indexes/multiview-maxsim.bin")),
        RetrievalPorts(StubRetriever(), StubRetriever(), enabled_multiview),
        enable_multiview_maxsim=True,
        multiview_artifact_key="indexes/multiview-maxsim.bin",
        clock=lambda: DEMO_AS_OF,
    )
    result = engine.search(SearchQuery("工程師"), limit=10)
    assert result.job_ids == ("9",)
    assert enabled_multiview.requests[0][1] == 200
    assert all(
        lane.name != "qwen_dense_multiview_maxsim" or lane.status == "enabled"
        for lane in result.trace.lanes
    )
    engine.close()


def test_close_is_idempotent_and_closed_engine_fails() -> None:
    lexical = StubRetriever()
    dense = StubRetriever()
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(lexical, dense),
        clock=lambda: DEMO_AS_OF,
    )

    engine.close()
    engine.close()

    assert lexical.closed and dense.closed
    with pytest.raises(SearchUnavailableError, match="closed"):
        engine.search(SearchQuery("工程師"), limit=10)
