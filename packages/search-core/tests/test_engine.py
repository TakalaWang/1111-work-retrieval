from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta
from threading import Lock

import pytest
from work_retrieval_core import (
    CandidateEvidence,
    CandidateRequest,
    CompiledQuery,
    JobMetadata,
    ProductionSearchEngine,
    QueryRewrite,
    RetrievalPorts,
    RuntimeManifest,
    SearchQuery,
    SearchUnavailableError,
)
from work_retrieval_core.manifest import (
    WHOLE_DOCUMENT_POLICY_VERSION,
    WHOLE_DOCUMENT_TEMPLATE_SHA256,
)

DEMO_AS_OF = datetime(2026, 6, 8, tzinfo=UTC)
HEX = "a" * 64


def _manifest(*, multiview: bool = False) -> dict[str, object]:
    whole_path = "embeddings/qwen3-embedding-8b/whole/manifest.json"
    tantivy_path = "indexes/tantivy-bm25-temporal-v2/manifest.json"
    artifacts: dict[str, object] = {
        whole_path: {"kind": "embedding", "sha256": "b" * 64, "size_bytes": 84},
        tantivy_path: {"kind": "index", "sha256": "c" * 64, "size_bytes": 42},
    }
    challengers: dict[str, object] = {
        name: {"enabled": False}
        for name in (
            "multiview_embedding",
            "skill_graph",
            "semantic_reranker",
            "learning_to_rank",
            "guardrails",
        )
    }
    if multiview:
        path = "embeddings/qwen3-embedding-8b/multiview/manifest.json"
        artifacts[path] = {"kind": "embedding", "sha256": "d" * 64, "size_bytes": 21}
        challengers["multiview_embedding"] = {
            "enabled": True,
            "complete": True,
            "publication_allowed": True,
            "manifest_path": path,
            "manifest_sha256": "d" * 64,
        }
    inventory_sha = hashlib.sha256(
        json.dumps(artifacts, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return {
        "schema_version": 2,
        "release": {
            "complete": True,
            "publication_allowed": True,
            "release_spec_sha256": HEX,
            "source_manifest_sha256": HEX,
            "selected_inventory_sha256": HEX,
            "artifact_inventory_sha256": inventory_sha,
            "object_count": len(artifacts),
            "size_bytes": sum(item["size_bytes"] for item in artifacts.values()),
        },
        "retrieval_policy": {
            "as_of": {
                "production_mode": "request_time",
                "demo_reference": "2026-06-08T23:59:59.999+08:00",
            },
            "eligibility": {
                "updated_within_days": 180,
                "future_jobs": "retained_with_zero_freshness",
                "stale_jobs": "exclude",
                "applied_before_retrieval": True,
            },
        },
        "incumbents": {
            "whole_embedding": {
                "manifest_path": whole_path,
                "manifest_sha256": "b" * 64,
                "complete": True,
                "model": "Qwen/Qwen3-Embedding-8B",
                "revision": "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af",
                "source_dimension": 4096,
                "dimension": 1024,
                "projection": "mrl_prefix_then_l2_normalize",
                "dtype": "float16",
                "normalized": True,
                "rows": 3,
                "dataset_sha256": HEX,
                "jobs_sha256": HEX,
                "job_row_order_sha256": HEX,
                "document_policy_version": WHOLE_DOCUMENT_POLICY_VERSION,
                "document_template_sha256": WHOLE_DOCUMENT_TEMPLATE_SHA256,
            },
            "temporal_tantivy": {
                "manifest_path": tantivy_path,
                "manifest_sha256": "c" * 64,
                "complete": True,
                "index_sha256": HEX,
                "engine": "tantivy v0.26.0, index_format v7",
                "jobs_sha256": HEX,
                "job_row_order_sha256": HEX,
                "updated_at_field": "updated_at_epoch_ms",
                "hard_filters": True,
                "temporal_filter_semantics": (
                    "updated_at >= as_of - 180 days before Top-K; future rows retained"
                ),
            },
        },
        "challengers": challengers,
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


class StubMetadata:
    def __init__(self, records: tuple[JobMetadata, ...] = ()) -> None:
        self.records = {record.job_id: record for record in records}
        self.closed = False

    def get_many(self, job_ids: tuple[str, ...]) -> tuple[JobMetadata, ...]:
        return tuple(self.records[job_id] for job_id in job_ids if job_id in self.records)

    def close(self) -> None:
        self.closed = True


class StubQueryCompiler:
    def compile(self, text: str) -> CompiledQuery:
        return CompiledQuery(
            (text, "kubernetes"),
            (QueryRewrite("kuberntes", "kubernetes", "train_jd_corpus_v1"),),
        )


def _candidate(job_id: str, score: float, rank: int) -> CandidateEvidence:
    return CandidateEvidence(job_id, score, rank)


def _metadata(job_id: str, days_ago: int) -> JobMetadata:
    return JobMetadata(
        job_id,
        DEMO_AS_OF - timedelta(days=days_ago),
        ("100100",),
        ("140200",),
    )


def test_search_query_is_immutable() -> None:
    query = SearchQuery("後端工程師", ("100100",), ("140200",))
    with pytest.raises(FrozenInstanceError):
        query.text = "changed"  # type: ignore[misc]


def test_manifest_rejects_unknown_keys_and_incompatible_future_policy() -> None:
    unknown = _manifest()
    unknown["fallback"] = "legacy"
    with pytest.raises(RuntimeError, match="missing or unknown keys"):
        RuntimeManifest.from_dict(unknown)

    incompatible = _manifest()
    incompatible["retrieval_policy"]["eligibility"]["future_jobs"] = "exclude"  # type: ignore[index]
    with pytest.raises(RuntimeError, match="eligibility policy"):
        RuntimeManifest.from_dict(incompatible)


def test_manifest_requires_both_incumbents_and_selects_only_their_prefixes() -> None:
    parsed = RuntimeManifest.from_dict(_manifest(multiview=True))
    assert [
        path
        for path, _artifact in parsed.required_artifacts(
            include_dense=False, include_multiview=False
        )
    ] == [
        "indexes/tantivy-bm25-temporal-v2/manifest.json",
    ]
    assert [
        path
        for path, _artifact in parsed.required_artifacts(
            include_dense=True, include_multiview=False
        )
    ] == [
        "embeddings/qwen3-embedding-8b/whole/manifest.json",
        "indexes/tantivy-bm25-temporal-v2/manifest.json",
    ]

    missing = _manifest()
    missing["incumbents"]["temporal_tantivy"]["manifest_path"] = (  # type: ignore[index]
        "indexes/missing/manifest.json"
    )
    with pytest.raises(RuntimeError, match="absent"):
        RuntimeManifest.from_dict(missing)


def test_multiview_requires_both_manifest_artifact_and_port() -> None:
    ports = RetrievalPorts(StubRetriever(), StubRetriever(), StubMetadata())
    with pytest.raises(RuntimeError, match="manifest artifact"):
        ProductionSearchEngine(
            RuntimeManifest.from_dict(_manifest()),
            ports,
            enable_multiview_maxsim=True,
            multiview_artifact_key="embeddings/qwen3-embedding-8b/multiview/manifest.json",
            clock=lambda: DEMO_AS_OF,
        )
    with pytest.raises(RuntimeError, match="configured retrieval port"):
        ProductionSearchEngine(
            RuntimeManifest.from_dict(_manifest(multiview=True)),
            ports,
            enable_multiview_maxsim=True,
            multiview_artifact_key="embeddings/qwen3-embedding-8b/multiview/manifest.json",
            clock=lambda: DEMO_AS_OF,
        )


def test_dynamic_as_of_filters_before_top_k_and_retains_future_rows() -> None:
    lexical = StubRetriever((_candidate("1", 10.0, 1), _candidate("2", 9.0, 2)))
    dense = StubRetriever((_candidate("3", 0.8, 1), _candidate("1", 0.7, 2)))
    metadata = StubMetadata((_metadata("1", 0), _metadata("2", -1), _metadata("3", 30)))
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(lexical, dense, metadata),
        enable_dense_shadow=True,
        clock=lambda: DEMO_AS_OF,
    )

    result = engine.search(SearchQuery("資料工程師", ("100100",), ("140200",)), limit=3)

    expected = CandidateRequest(
        "資料工程師",
        ("100100",),
        ("140200",),
        DEMO_AS_OF,
        DEMO_AS_OF - timedelta(days=180),
        ("資料工程師",),
    )
    assert lexical.requests == dense.requests == [(expected, 200)]
    assert result.job_ids == ("1", "2", "3")
    assert result.trace.location_filter == "verified_on_returned_candidates"
    future = next(item for item in result.trace.results if item.job_id == "2")
    assert future.freshness_score == 0 and future.future_updated_snapshot
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
    lexical, dense, metadata = StubRetriever(), StubRetriever(), StubMetadata()
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(lexical, dense, metadata),
        enable_dense_shadow=True,
        clock=lambda: next(values),
    )
    first = engine.search(SearchQuery("工程師"), limit=10)
    second = engine.search(SearchQuery("工程師"), limit=10)
    assert first.trace.as_of == DEMO_AS_OF
    assert second.trace.as_of == DEMO_AS_OF + timedelta(days=1)
    assert dense.requests[0][0].minimum_updated_at != dense.requests[1][0].minimum_updated_at
    engine.close()


def test_request_date_overrides_clock_at_taipei_end_of_day() -> None:
    lexical = StubRetriever()
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(lexical, None, StubMetadata()),
        clock=lambda: DEMO_AS_OF,
    )

    result = engine.search(SearchQuery("工程師", search_date=date(2026, 6, 9)), limit=10)

    expected = datetime(2026, 6, 9, 15, 59, 59, 999_000, tzinfo=UTC)
    assert result.trace.as_of == expected
    assert lexical.requests[0][0].as_of == expected
    engine.close()


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        (_metadata("1", 181), "temporal eligibility"),
        (JobMetadata("1", DEMO_AS_OF, ("other",), ("140200",)), "location hard filter"),
        (JobMetadata("1", DEMO_AS_OF, ("100100",), ("other",)), "duty hard filter"),
    ],
)
def test_metadata_revalidates_every_hard_filter(metadata: JobMetadata, message: str) -> None:
    lane = StubRetriever((_candidate("1", 1.0, 1),))
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(lane, StubRetriever(), StubMetadata((metadata,))),
        clock=lambda: DEMO_AS_OF,
    )
    with pytest.raises(SearchUnavailableError, match=message):
        engine.search(SearchQuery("工程師", ("100100",), ("140200",)), limit=10)
    engine.close()


def test_lane_failure_and_unsorted_or_implicit_rank_fail_closed() -> None:
    for candidates in (
        (_candidate("1", 0.8, 2),),
        (_candidate("1", 0.7, 1), _candidate("2", 0.8, 2)),
    ):
        engine = ProductionSearchEngine(
            RuntimeManifest.from_dict(_manifest()),
            RetrievalPorts(
                StubRetriever(candidates),
                StubRetriever(),
                StubMetadata((_metadata("1", 0), _metadata("2", 0))),
            ),
            clock=lambda: DEMO_AS_OF,
        )
        with pytest.raises(SearchUnavailableError, match="unsorted candidate lane"):
            engine.search(SearchQuery("工程師"), limit=10)
        engine.close()

    broken = StubRetriever()
    broken.error = RuntimeError("private endpoint details")
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(broken, StubRetriever(), StubMetadata()),
        clock=lambda: DEMO_AS_OF,
    )
    with pytest.raises(SearchUnavailableError, match="required retrieval lane") as caught:
        engine.search(SearchQuery("工程師"), limit=10)
    assert "private endpoint" not in str(caught.value)
    engine.close()

    lexical = StubRetriever((_candidate("1", 1.0, 1),))
    shadow = StubRetriever()
    shadow.error = RuntimeError("shadow failed")
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(lexical, shadow, StubMetadata((_metadata("1", 0),))),
        enable_dense_shadow=True,
        clock=lambda: DEMO_AS_OF,
    )
    result = engine.search(SearchQuery("工程師"), limit=10)
    assert result.job_ids == ("1",)
    dense_lane = next(lane for lane in result.trace.lanes if lane.name == "qwen_dense_whole_jd")
    assert dense_lane.status == "failed"
    engine.close()


def test_multiview_runs_only_when_explicitly_enabled() -> None:
    disabled = StubRetriever((_candidate("9", 1.0, 1),))
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(StubRetriever(), StubRetriever(), StubMetadata(), disabled),
        clock=lambda: DEMO_AS_OF,
    )
    assert engine.search(SearchQuery("工程師"), limit=10).job_ids == ()
    assert disabled.requests == []
    engine.close()

    enabled = StubRetriever((_candidate("9", 1.0, 1),))
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest(multiview=True)),
        RetrievalPorts(
            StubRetriever(),
            StubRetriever(),
            StubMetadata((_metadata("9", 1),)),
            enabled,
        ),
        enable_multiview_maxsim=True,
        multiview_artifact_key="embeddings/qwen3-embedding-8b/multiview/manifest.json",
        clock=lambda: DEMO_AS_OF,
    )
    assert engine.search(SearchQuery("工程師"), limit=10).job_ids == ("9",)
    engine.close()


def test_close_is_idempotent_and_closed_engine_fails() -> None:
    lexical, dense, metadata = StubRetriever(), StubRetriever(), StubMetadata()
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(lexical, dense, metadata),
        clock=lambda: DEMO_AS_OF,
    )
    engine.close()
    engine.close()
    assert lexical.closed and dense.closed and metadata.closed
    with pytest.raises(SearchUnavailableError, match="closed"):
        engine.search(SearchQuery("工程師"), limit=10)


def test_dense_shadow_cannot_reorder_incumbent_top_ten() -> None:
    lexical_candidates = tuple(
        _candidate(str(index), float(20 - index), index) for index in range(1, 11)
    )
    dense_candidates = (
        *(
            _candidate(str(index), float(index), rank)
            for rank, index in enumerate(range(10, 0, -1), start=1)
        ),
        _candidate("11", 0.1, 11),
    )
    metadata = StubMetadata(tuple(_metadata(str(index), 0) for index in range(1, 12)))
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(
            StubRetriever(lexical_candidates),
            StubRetriever(dense_candidates),
            metadata,
        ),
        enable_dense_shadow=True,
        clock=lambda: DEMO_AS_OF,
    )

    result = engine.search(SearchQuery("工程師"), limit=10)

    assert result.job_ids == tuple(str(index) for index in range(1, 11))
    engine.close()


def test_query_rewrite_preserves_original_and_is_audited() -> None:
    lexical = StubRetriever()
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(
            lexical,
            None,
            StubMetadata(),
            query_compiler=StubQueryCompiler(),
        ),
        clock=lambda: DEMO_AS_OF,
    )

    result = engine.search(SearchQuery("Kuberntes"), limit=10)

    assert lexical.requests[0][0].lexical_texts == ("Kuberntes", "kubernetes")
    assert result.trace.as_dict()["query_rewrites"] == [
        {
            "source": "kuberntes",
            "target": "kubernetes",
            "policy": "train_jd_corpus_v1",
        }
    ]
    engine.close()


def test_manifest_rejects_enabled_challenger_without_production_adapter() -> None:
    value = _manifest()
    value["challengers"]["skill_graph"] = {"enabled": True}  # type: ignore[index]

    with pytest.raises(RuntimeError, match="must be disabled"):
        RuntimeManifest.from_dict(value)
