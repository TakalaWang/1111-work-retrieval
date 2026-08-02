from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
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
from work_retrieval_core.constraints import (
    EducationConstraint,
    JobAttributeConstraint,
    ManagementConstraint,
    MonthlySalaryConstraint,
    NoExperienceConstraint,
    QueryConstraints,
    WorkShiftConstraint,
)
from work_retrieval_core.graph_policy import (
    GRAPH_SERVING_IMPLEMENTATION_SHA256,
    GRAPH_SERVING_POLICY_SHA256,
)
from work_retrieval_core.manifest import (
    WHOLE_DOCUMENT_POLICY_VERSION,
    WHOLE_DOCUMENT_TEMPLATE_SHA256,
    semantic_reranker_manifest,
)

DEMO_AS_OF = datetime(2026, 6, 8, tzinfo=UTC)
HEX = "a" * 64


def _manifest(*, multiview: bool = False, graph: bool = False) -> dict[str, object]:
    whole_path = "embeddings/qwen3-embedding-8b/whole/manifest.json"
    tantivy_path = "indexes/tantivy-bm25-temporal-v3/manifest.json"
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
    challengers["semantic_reranker"] = semantic_reranker_manifest()
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
    if graph:
        path = "graphs/skill-graph/manifest.json"
        candidate_path = "evidence/skill-graph/candidate-manifest.json"
        report_path = "evidence/skill-graph/report.json"
        attestation_path = "evidence/skill-graph/organizer-attestation.json"
        artifacts[path] = {"kind": "graph", "sha256": "e" * 64, "size_bytes": 19}
        artifacts[report_path] = {"kind": "evidence", "sha256": "f" * 64, "size_bytes": 20}
        artifacts[candidate_path] = {
            "kind": "evidence",
            "sha256": "2" * 64,
            "size_bytes": 22,
        }
        artifacts[attestation_path] = {
            "kind": "evidence",
            "sha256": "1" * 64,
            "size_bytes": 21,
        }
        challengers["skill_graph"] = {
            "enabled": True,
            "complete": True,
            "publication_allowed": True,
            "manifest_path": path,
            "manifest_sha256": "e" * 64,
            "schema_version": 1,
            "train_cutoff_exclusive": "2026-06-08T00:00:00+08:00",
            "max_source_timestamp": "2026-06-07T23:51:07.143000+08:00",
            "source_jd_sha256": "53937f7bf076789c4cd7e3be34fb89875336108d57707b5a93182181e1087089",
            "source_policy": "train_jd_only",
            "test_jd_used": False,
            "candidate_manifest_sha256": "2" * 64,
            "candidate_manifest_path": candidate_path,
            "source_ablation_report_sha256": "3" * 64,
            "serving_algorithm": "graph-conditioned-temporal-bridge-retrieval-protected-rrf-v3",
            "serving_policy_sha256": GRAPH_SERVING_POLICY_SHA256,
            "serving_implementation_sha256": GRAPH_SERVING_IMPLEMENTATION_SHA256,
            "evaluation_implementation_sha256": "4" * 64,
            "organizer_attestation_path": attestation_path,
            "organizer_attestation_sha256": "1" * 64,
            "promotion_evidence": {
                "decision": "accepted",
                "report_path": report_path,
                "report_sha256": "f" * 64,
                "evaluation_split_sha256": HEX,
                "baseline_run_sha256": HEX,
                "candidate_run_sha256": HEX,
                "primary_metric": "ndcg_at_10",
                "absolute_delta": 0.001,
            },
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
                    "updated_at >= as_of - 180 days before Top-K; "
                    "future snapshots retained with freshness 0"
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

    def preflight(self, request: CandidateRequest) -> bool:
        del request
        return True


class SequencedRetriever(StubRetriever):
    def __init__(self, responses: tuple[tuple[CandidateEvidence, ...], ...]) -> None:
        super().__init__()
        self._responses = iter(responses)

    def retrieve(self, request: CandidateRequest, *, limit: int) -> tuple[CandidateEvidence, ...]:
        super().retrieve(request, limit=limit)
        return next(self._responses)


class CapacitySkippedRetriever(StubRetriever):
    def __init__(self) -> None:
        super().__init__()
        self.preflight_requests: list[CandidateRequest] = []

    def preflight(self, request: CandidateRequest) -> bool:
        self.preflight_requests.append(request)
        return False


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


class StubReranker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.error: Exception | None = None
        self.closed = False

    def score(self, query: str, job_ids: tuple[str, ...]) -> dict[str, float]:
        self.calls.append((query, job_ids))
        if self.error is not None:
            raise self.error
        return {job_id: 0.95 + position / 1000 for position, job_id in enumerate(job_ids)}

    def close(self) -> None:
        self.closed = True


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
            include_dense=False, include_multiview=False, include_graph=False
        )
    ] == [
        "indexes/tantivy-bm25-temporal-v3/manifest.json",
    ]
    assert [
        path
        for path, _artifact in parsed.required_artifacts(
            include_dense=True, include_multiview=False, include_graph=False
        )
    ] == [
        "embeddings/qwen3-embedding-8b/whole/manifest.json",
        "indexes/tantivy-bm25-temporal-v3/manifest.json",
    ]

    missing = _manifest()
    missing["incumbents"]["temporal_tantivy"]["manifest_path"] = (  # type: ignore[index]
        "indexes/missing/manifest.json"
    )
    with pytest.raises(RuntimeError, match="absent"):
        RuntimeManifest.from_dict(missing)


def test_manifest_selects_graph_artifacts_only_when_enabled() -> None:
    parsed = RuntimeManifest.from_dict(_manifest(graph=True))

    assert parsed.skill_graph is not None
    assert [
        path
        for path, _artifact in parsed.required_artifacts(
            include_dense=False,
            include_multiview=False,
            include_graph=True,
        )
    ] == [
        "indexes/tantivy-bm25-temporal-v3/manifest.json",
        "graphs/skill-graph/manifest.json",
        "evidence/skill-graph/report.json",
        "evidence/skill-graph/candidate-manifest.json",
        "evidence/skill-graph/organizer-attestation.json",
    ]

    with pytest.raises(RuntimeError, match="Graph flag requires"):
        RuntimeManifest.from_dict(_manifest()).required_artifacts(
            include_dense=False,
            include_multiview=False,
            include_graph=True,
        )


def test_manifest_rejects_graph_serving_policy_drift() -> None:
    value = _manifest(graph=True)
    value["challengers"]["skill_graph"]["serving_policy_sha256"] = HEX  # type: ignore[index]

    with pytest.raises(RuntimeError, match="serving_policy_sha256"):
        RuntimeManifest.from_dict(value)


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


def test_dynamic_as_of_filters_stale_rows_and_retains_future_snapshots() -> None:
    lexical = StubRetriever((_candidate("1", 10.0, 1),))
    dense = StubRetriever(
        (_candidate("2", 0.9, 1), _candidate("3", 0.8, 2), _candidate("1", 0.7, 3))
    )
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
    assert result.job_ids == ("1",)
    assert result.trace.location_filter == "verified_on_returned_candidates"
    assert result.trace.future_rows == "retained_with_zero_freshness"
    assert [(lane.name, lane.status, lane.reason) for lane in result.trace.lanes[-5:]] == [
        ("qwen_dense_multiview_maxsim", "disabled", "feature_flag_disabled"),
        ("graph", "disabled", "feature_flag_disabled"),
        ("reranker", "disabled", "feature_flag_disabled"),
        ("ltr", "disabled", "calibration_not_approved"),
        ("guardrail", "disabled", "calibration_not_approved"),
    ]
    engine.close()


def test_enabled_graph_is_an_independent_shadow_evidence_lane() -> None:
    lexical = StubRetriever((_candidate("1", 10.0, 1),))
    graph = StubRetriever((_candidate("2", 9.0, 1),))
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest(graph=True)),
        RetrievalPorts(
            lexical,
            None,
            StubMetadata((_metadata("1", 0), _metadata("2", 0))),
            graph=graph,
        ),
        enable_graph=True,
        clock=lambda: DEMO_AS_OF,
    )

    result = engine.search(SearchQuery("Python"), limit=10)

    assert result.job_ids == ("1",)
    assert result.trace.lanes[0].as_dict() == {
        "name": "tantivy_bm25_full_jd",
        "status": "enabled",
        "reason": "top10_incumbent",
        "candidate_count": 1,
    }
    assert result.trace.lanes[1].as_dict() == {
        "name": "graph_conditioned_tantivy",
        "status": "enabled",
        "reason": "shadow_tail_only",
        "candidate_count": 1,
    }
    assert result.trace.results[0].evidence[0].ranking_contribution == 10.0
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
    assert engine.search(SearchQuery("工程師"), limit=10).job_ids == ()
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


@pytest.mark.parametrize("graph_enabled", [False, True])
def test_dense_shadow_cannot_reorder_incumbent_top_ten(graph_enabled: bool) -> None:
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
        RuntimeManifest.from_dict(_manifest(graph=graph_enabled)),
        RetrievalPorts(
            StubRetriever(lexical_candidates),
            StubRetriever(dense_candidates),
            metadata,
            graph=StubRetriever() if graph_enabled else None,
        ),
        enable_dense_shadow=True,
        enable_graph=graph_enabled,
        clock=lambda: DEMO_AS_OF,
    )

    result = engine.search(SearchQuery("工程師"), limit=10)

    assert result.job_ids == tuple(str(index) for index in range(1, 11))
    engine.close()


def test_reranker_pool_matches_sealed_four_to_one_weighted_rrf() -> None:
    lexical = StubRetriever(
        tuple(_candidate(str(index), float(20 - index), index) for index in range(1, 12))
    )
    dense = StubRetriever(
        (
            _candidate("12", 0.9, 1),
            _candidate("11", 0.8, 2),
            _candidate("13", 0.7, 3),
        )
    )
    reranker = StubReranker()
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(
            lexical,
            dense,
            StubMetadata(tuple(_metadata(str(index), 0) for index in range(1, 14))),
            reranker=reranker,
        ),
        enable_dense_shadow=True,
        reranker_mode="active",
        clock=lambda: DEMO_AS_OF,
    )

    result = engine.search(SearchQuery("工程師"), limit=3)

    assert reranker.calls == [
        (
            "工程師",
            ("11", *tuple(str(index) for index in range(1, 11))),
        )
    ]
    assert result.job_ids == ("1", "11", "2")
    reranker_lane = next(lane for lane in result.trace.lanes if lane.name == "reranker")
    assert (reranker_lane.status, reranker_lane.reason, reranker_lane.candidate_count) == (
        "enabled",
        "relevance_gated_rank_fusion_top1_protected",
        11,
    )
    engine.close()


def test_active_reranker_never_admits_a_dense_only_candidate() -> None:
    lexical = StubRetriever(
        tuple(_candidate(str(index), float(10 - index), index) for index in range(1, 5))
    )
    dense = StubRetriever(
        (
            _candidate("4", 0.9, 1),
            _candidate("5", 0.8, 2),
        )
    )
    reranker = StubReranker()
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(
            lexical,
            dense,
            StubMetadata(tuple(_metadata(str(index), 0) for index in range(1, 6))),
            reranker=reranker,
        ),
        enable_dense_shadow=True,
        reranker_mode="active",
        clock=lambda: DEMO_AS_OF,
    )

    result = engine.search(SearchQuery("工程師"), limit=5)

    assert "5" not in result.job_ids
    assert reranker.calls == [("工程師", ("4", "1", "2", "3"))]
    engine.close()


def test_active_reranker_runs_on_bm25_when_exact_dense_capacity_is_skipped() -> None:
    lexical = StubRetriever((_candidate("1", 1.0, 1),))
    dense = CapacitySkippedRetriever()
    reranker = StubReranker()
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(
            lexical,
            dense,
            StubMetadata((_metadata("1", 0),)),
            reranker=reranker,
        ),
        enable_dense_shadow=True,
        reranker_mode="active",
        clock=lambda: DEMO_AS_OF,
    )

    assert dense.preflight_requests == []
    result = engine.search(SearchQuery("工程師"), limit=1)

    assert len(dense.preflight_requests) == 1
    assert dense.requests == []
    assert reranker.calls == [("工程師", ("1",))]
    dense_lane = next(lane for lane in result.trace.lanes if lane.name == "qwen_dense_whole_jd")
    assert (dense_lane.status, dense_lane.reason, dense_lane.candidate_count) == (
        "capacity_skipped",
        "eligible_universe_exceeds_exact_scan_limit",
        0,
    )
    engine.close()


def test_dense_only_candidate_cannot_change_top_ten_when_reranker_is_disabled() -> None:
    lexical = StubRetriever(
        tuple(_candidate(str(index), float(20 - index), index) for index in range(1, 11))
    )
    dense = StubRetriever((_candidate("11", 100.0, 1),))
    reranker = StubReranker()
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(
            lexical,
            dense,
            StubMetadata(tuple(_metadata(str(index), 0) for index in range(1, 12))),
            reranker=reranker,
        ),
        enable_dense_shadow=True,
        clock=lambda: DEMO_AS_OF,
    )

    result = engine.search(SearchQuery("工程師"), limit=10)

    assert result.job_ids == tuple(str(index) for index in range(1, 11))
    assert reranker.calls == []
    engine.close()


def test_active_reranker_excludes_dense_and_multiview_only_candidates() -> None:
    reranker = StubReranker()
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest(multiview=True)),
        RetrievalPorts(
            StubRetriever((_candidate("1", 1.0, 1),)),
            StubRetriever((_candidate("2", 0.9, 1),)),
            StubMetadata(tuple(_metadata(str(index), 0) for index in range(1, 4))),
            StubRetriever((_candidate("3", 0.8, 1),)),
            reranker=reranker,
        ),
        enable_dense_shadow=True,
        enable_multiview_maxsim=True,
        multiview_artifact_key="embeddings/qwen3-embedding-8b/multiview/manifest.json",
        reranker_mode="active",
        clock=lambda: DEMO_AS_OF,
    )

    result = engine.search(SearchQuery("工程師"), limit=3)

    assert reranker.calls == [("工程師", ("1",))]
    assert result.job_ids == ("1",)
    engine.close()


def test_active_reranker_failure_is_sanitized_and_fails_closed() -> None:
    reranker = StubReranker()
    reranker.error = RuntimeError("private SageMaker response")
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(
            StubRetriever((_candidate("1", 1.0, 1),)),
            StubRetriever(),
            StubMetadata((_metadata("1", 0),)),
            reranker=reranker,
        ),
        enable_dense_shadow=True,
        reranker_mode="active",
        clock=lambda: DEMO_AS_OF,
    )

    with pytest.raises(SearchUnavailableError, match="reranker failed") as caught:
        engine.search(SearchQuery("工程師"), limit=10)
    assert "private SageMaker" not in str(caught.value)
    engine.close()
    assert reranker.closed


def test_shadow_reranker_scores_without_reordering_and_failure_keeps_incumbent() -> None:
    lexical = StubRetriever((_candidate("1", 2.0, 1), _candidate("2", 1.0, 2)))
    dense = StubRetriever((_candidate("3", 0.9, 1),))
    metadata = StubMetadata(tuple(_metadata(str(index), 0) for index in range(1, 4)))
    reranker = StubReranker()
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(lexical, dense, metadata, reranker=reranker),
        enable_dense_shadow=True,
        reranker_mode="shadow",
        clock=lambda: DEMO_AS_OF,
    )
    result = engine.search(SearchQuery("工程師"), limit=3)
    assert result.job_ids == ("1", "2")
    assert reranker.calls == [("工程師", ("1", "2"))]
    trace = next(lane for lane in result.trace.lanes if lane.name == "reranker")
    assert (trace.status, trace.reason) == ("enabled", "shadow_scored")
    engine.close()

    failed = StubReranker()
    failed.error = RuntimeError("private")
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(lexical, dense, metadata, reranker=failed),
        enable_dense_shadow=True,
        reranker_mode="shadow",
        clock=lambda: DEMO_AS_OF,
    )
    result = engine.search(SearchQuery("工程師"), limit=3)
    assert result.job_ids == ("1", "2")
    trace = next(lane for lane in result.trace.lanes if lane.name == "reranker")
    assert (trace.status, trace.reason) == ("failed", "shadow_call_failed")
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


def test_zero_result_relaxes_only_query_text_constraints_once() -> None:
    lexical = SequencedRetriever(((), (_candidate("1", 1.0, 1),)))
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(
            lexical,
            None,
            StubMetadata((JobMetadata("1", DEMO_AS_OF, ("100100",), ("140200",)),)),
        ),
        clock=lambda: DEMO_AS_OF,
    )

    result = engine.search(
        SearchQuery("後端工程師 學歷大學 月薪至少50000", ("100100",), ("140200",)),
        limit=10,
    )

    assert result.job_ids == ("1",)
    assert len(lexical.requests) == 2
    (first, first_limit), (second, second_limit) = lexical.requests
    assert first.constraints.requested()
    assert second == replace(first, constraints=QueryConstraints())
    assert second.text == first.text
    assert second.lexical_texts == first.lexical_texts
    assert second.location_codes == first.location_codes == ("100100",)
    assert second.duty_codes == first.duty_codes == ("140200",)
    assert second.as_of == first.as_of
    assert second.minimum_updated_at == first.minimum_updated_at
    assert first_limit == second_limit == 200
    assert result.trace.constraint_filter == "relaxed_query_text_constraints_after_zero"
    engine.close()


def test_zero_result_without_query_text_constraints_is_not_retried() -> None:
    lexical = StubRetriever()
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(lexical, None, StubMetadata()),
        clock=lambda: DEMO_AS_OF,
    )

    result = engine.search(SearchQuery("後端工程師", ("100100",), ("140200",)), limit=10)

    assert result.job_ids == ()
    assert len(lexical.requests) == 1
    assert result.trace.constraint_filter == "not_requested"
    engine.close()


def test_constraints_are_immutable_lane_inputs_audited_and_metadata_revalidated() -> None:
    lexical = StubRetriever((_candidate("1", 4.0, 1), _candidate("3", 2.0, 2)))
    metadata = StubMetadata(
        (
            JobMetadata(
                "1",
                DEMO_AS_OF,
                ("100100",),
                ("140200",),
                education_requirement="大學,碩士",
                salary_period="月薪",
                salary_min=40_000,
                salary_max=60_000,
            ),
            JobMetadata(
                "3",
                DEMO_AS_OF,
                ("100100",),
                ("140200",),
                education_requirement="不拘",
                salary_period="月薪",
                salary_min=50_000,
                salary_max=0,
            ),
        )
    )
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(lexical, None, metadata),
        clock=lambda: DEMO_AS_OF,
    )

    result = engine.search(SearchQuery("後端工程師 學歷大學 月薪五萬"), limit=10)

    request = lexical.requests[0][0]
    assert request.text == "後端工程師 學歷大學 月薪五萬"
    assert request.constraints.education == EducationConstraint("大學")
    assert request.constraints.monthly_salary == MonthlySalaryConstraint(50_000, strict=False)
    assert result.job_ids == ("1", "3")
    trace = result.trace.as_dict()
    assert trace["constraints"] == {
        "education": {
            "degree": "大學",
            "policy": "accepted_set_contains_degree_or_不拘",
        },
        "monthly_salary": {
            "minimum": 50_000,
            "strict": False,
            "confidence": "medium",
            "policy": "positive_upper_else_lower_reaches_minimum",
        },
        "job_attribute": None,
        "work_shift": None,
        "no_experience": None,
        "management": None,
    }
    assert trace["hard_filters"]["education"] == ("tantivy_pre_topk_and_postgres_revalidated")
    assert trace["hard_filters"]["monthly_salary"] == ("tantivy_pre_topk_and_postgres_revalidated")
    engine.close()


def test_strict_monthly_minimum_requires_the_advertised_lower_bound() -> None:
    lexical = StubRetriever((_candidate("2", 1.0, 1),))
    metadata = StubMetadata(
        (
            JobMetadata(
                "2",
                DEMO_AS_OF,
                (),
                (),
                salary_period="月薪",
                salary_min=50_000,
                salary_max=0,
            ),
        )
    )
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(lexical, None, metadata),
        clock=lambda: DEMO_AS_OF,
    )

    result = engine.search(SearchQuery("月薪至少50000"), limit=10)

    assert result.job_ids == ("2",)
    engine.close()


def test_constraint_metadata_drift_fails_closed() -> None:
    lexical = StubRetriever((_candidate("1", 1.0, 1),))
    metadata = StubMetadata(
        (
            JobMetadata(
                "1",
                DEMO_AS_OF,
                (),
                (),
                education_requirement="碩士",
            ),
        )
    )
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(lexical, None, metadata),
        clock=lambda: DEMO_AS_OF,
    )

    with pytest.raises(SearchUnavailableError, match="education hard filter"):
        engine.search(SearchQuery("學歷大學"), limit=10)
    engine.close()


def test_typed_job_constraints_are_audited_and_postgres_revalidated() -> None:
    lexical = StubRetriever((_candidate("1", 1.0, 1),))
    metadata = StubMetadata(
        (
            JobMetadata(
                "1",
                DEMO_AS_OF,
                (),
                (),
                job_attribute="兼職",
                work_hours="晚班,輪班",
                experience_requirement="不拘",
                management_count="需管理人數10人以下",
            ),
        )
    )
    engine = ProductionSearchEngine(
        RuntimeManifest.from_dict(_manifest()),
        RetrievalPorts(lexical, None, metadata),
        clock=lambda: DEMO_AS_OF,
    )

    result = engine.search(SearchQuery("晚班兼職 無經驗 需管理人數"), limit=10)

    constraints = lexical.requests[0][0].constraints
    assert constraints.job_attribute == JobAttributeConstraint("兼職")
    assert constraints.work_shift == WorkShiftConstraint("晚班")
    assert constraints.no_experience == NoExperienceConstraint()
    assert constraints.management == ManagementConstraint()
    assert result.job_ids == ("1",)
    hard_filters = result.trace.as_dict()["hard_filters"]
    for name in ("job_attribute", "work_shift", "no_experience", "management"):
        assert hard_filters[name] == "tantivy_pre_topk_and_postgres_revalidated"
    engine.close()


def test_manifest_rejects_reranker_lineage_drift() -> None:
    value = _manifest()
    value["challengers"]["semantic_reranker"]["candidate_depth"] = 50  # type: ignore[index]

    with pytest.raises(RuntimeError, match="semantic reranker challenger"):
        RuntimeManifest.from_dict(value)
