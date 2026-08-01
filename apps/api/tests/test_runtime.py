from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
from work_retrieval_api.runtime import runtime_from_environment
from work_retrieval_core import (
    CandidateEvidence,
    CandidateRequest,
    JobMetadata,
    RetrievalPorts,
    RuntimeManifest,
    SearchEngine,
    SearchQuery,
)


class StubRetriever:
    def __init__(self) -> None:
        self.requests: list[CandidateRequest] = []
        self.closed = False

    def retrieve(self, request: CandidateRequest, *, limit: int) -> tuple[CandidateEvidence, ...]:
        del limit
        self.requests.append(request)
        return ()

    def close(self) -> None:
        self.closed = True


class StubMetadata:
    def get_many(self, job_ids: tuple[str, ...]) -> tuple[JobMetadata, ...]:
        assert not job_ids
        return ()

    def job_details(self, job_id: str) -> dict[str, str | None] | None:
        return {"職務名稱": "工程師"} if job_id == "1" else None

    def close(self) -> None:
        pass


def _manifest() -> dict[str, object]:
    from work_retrieval_core.serialization import (
        DOCUMENT_POLICY_VERSION,
        document_template_sha256,
    )

    whole = "embeddings/qwen3-embedding-8b/whole/manifest.json"
    lexical = "indexes/tantivy-bm25-temporal-v1/manifest.json"
    artifacts = {
        whole: {"kind": "embedding", "sha256": "b" * 64, "size_bytes": 42},
        lexical: {"kind": "index", "sha256": "c" * 64, "size_bytes": 42},
    }
    inventory_sha = hashlib.sha256(
        json.dumps(artifacts, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return {
        "schema_version": 2,
        "release": {
            "complete": True,
            "publication_allowed": True,
            "release_spec_sha256": "a" * 64,
            "source_manifest_sha256": "a" * 64,
            "selected_inventory_sha256": "a" * 64,
            "artifact_inventory_sha256": inventory_sha,
            "object_count": 2,
            "size_bytes": 84,
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
                "manifest_path": whole,
                "manifest_sha256": "b" * 64,
                "complete": True,
                "model": "Qwen/Qwen3-Embedding-8B",
                "revision": "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af",
                "source_dimension": 4096,
                "dimension": 1024,
                "projection": "mrl_prefix_then_l2_normalize",
                "dtype": "float16",
                "normalized": True,
                "rows": 1,
                "dataset_sha256": "a" * 64,
                "jobs_sha256": "a" * 64,
                "job_row_order_sha256": "a" * 64,
                "document_policy_version": DOCUMENT_POLICY_VERSION,
                "document_template_sha256": document_template_sha256(),
            },
            "temporal_tantivy": {
                "manifest_path": lexical,
                "manifest_sha256": "c" * 64,
                "complete": True,
                "index_sha256": "a" * 64,
                "engine": "tantivy v0.26.0, index_format v7",
                "jobs_sha256": "a" * 64,
                "job_row_order_sha256": "a" * 64,
                "updated_at_field": "updated_at_epoch_ms",
                "hard_filters": True,
                "temporal_filter_semantics": (
                    "updated_at >= as_of - 180 days before Top-K; future rows retained"
                ),
            },
        },
        "challengers": {
            name: {"enabled": False}
            for name in (
                "multiview_embedding",
                "skill_graph",
                "semantic_reranker",
                "learning_to_rank",
                "guardrails",
            )
        },
        "artifacts": artifacts,
    }


def _write_manifest(path: Path) -> None:
    path.write_text(json.dumps(_manifest()), encoding="utf-8")


def test_environment_runtime_uses_manifest_ports_and_demo_fixture(tmp_path: Path) -> None:
    manifest_path = tmp_path / "runtime.json"
    _write_manifest(manifest_path)
    lexical = StubRetriever()
    dense = StubRetriever()
    received: list[tuple[RuntimeManifest, bool, dict[str, str]]] = []

    def port_factory(
        manifest: RuntimeManifest,
        enable_multiview: bool,
        environment: Mapping[str, str],
    ) -> RetrievalPorts:
        received.append((manifest, enable_multiview, dict(environment)))
        return RetrievalPorts(lexical, dense, StubMetadata())

    engine = runtime_from_environment(
        {
            "SEARCH_RUNTIME_MANIFEST_PATH": str(manifest_path),
            "SEARCH_DEMO_AS_OF": "2026-06-08",
        },
        port_factory=port_factory,
    )
    result = engine.search(SearchQuery("工程師"), limit=10)

    assert isinstance(engine, SearchEngine)
    assert len(received) == 1 and not received[0][1]
    assert result.trace.as_of == datetime(2026, 6, 8, 15, 59, 59, 999_000, tzinfo=UTC)
    assert lexical.requests[0].minimum_updated_at == datetime(
        2025, 12, 10, 15, 59, 59, 999_000, tzinfo=UTC
    )
    assert engine.job_details("1") == {"職務名稱": "工程師"}
    assert engine.job_details("2") is None
    engine.close()
    assert lexical.closed and dense.closed


def test_environment_runtime_requires_explicit_manifest_and_port_factory(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="SEARCH_RUNTIME_MANIFEST_PATH"):
        runtime_from_environment({})

    manifest_path = tmp_path / "runtime.json"
    _write_manifest(manifest_path)
    with pytest.raises(RuntimeError, match="SEARCH_PORT_FACTORY"):
        runtime_from_environment({"SEARCH_RUNTIME_MANIFEST_PATH": str(manifest_path)})


@pytest.mark.parametrize(
    "value",
    ["not-a-date", "2026-06-08T12:30:00", "2026-06-08 12:30:00"],
)
def test_demo_as_of_rejects_ambiguous_values(tmp_path: Path, value: str) -> None:
    manifest_path = tmp_path / "runtime.json"
    _write_manifest(manifest_path)
    with pytest.raises(RuntimeError, match="SEARCH_DEMO_AS_OF"):
        runtime_from_environment(
            {
                "SEARCH_RUNTIME_MANIFEST_PATH": str(manifest_path),
                "SEARCH_DEMO_AS_OF": value,
            },
            port_factory=lambda manifest, enabled, environment: RetrievalPorts(
                StubRetriever(), StubRetriever(), StubMetadata()
            ),
        )


def test_multiview_feature_flag_is_strict(tmp_path: Path) -> None:
    manifest_path = tmp_path / "runtime.json"
    _write_manifest(manifest_path)

    def factory(
        manifest: RuntimeManifest,
        enabled: bool,
        environment: Mapping[str, str],
    ) -> RetrievalPorts:
        del manifest, enabled, environment
        return RetrievalPorts(StubRetriever(), StubRetriever(), StubMetadata())

    with pytest.raises(RuntimeError, match="must be true or false"):
        runtime_from_environment(
            {
                "SEARCH_RUNTIME_MANIFEST_PATH": str(manifest_path),
                "SEARCH_ENABLE_MULTIVIEW_MAXSIM": "1",
            },
            port_factory=factory,
        )
    with pytest.raises(RuntimeError, match="requires MaxSim"):
        runtime_from_environment(
            {
                "SEARCH_RUNTIME_MANIFEST_PATH": str(manifest_path),
                "SEARCH_MULTIVIEW_ARTIFACT_KEY": "indexes/maxsim.bin",
            },
            port_factory=factory,
        )
    with pytest.raises(RuntimeError, match="SEARCH_MULTIVIEW_ARTIFACT_KEY"):
        runtime_from_environment(
            {
                "SEARCH_RUNTIME_MANIFEST_PATH": str(manifest_path),
                "SEARCH_ENABLE_MULTIVIEW_MAXSIM": "true",
            },
            port_factory=factory,
        )
