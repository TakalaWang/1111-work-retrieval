from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from work_retrieval_api.runtime import runtime_from_environment
from work_retrieval_core import (
    CandidateEvidence,
    CandidateRequest,
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


def _manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "artifacts": {
            "embeddings/whole-qwen.f16": {
                "kind": "embedding",
                "sha256": "a" * 64,
                "size_bytes": 42,
            }
        },
    }


def _write_manifest(path: Path) -> None:
    path.write_text(json.dumps(_manifest()), encoding="utf-8")


def test_environment_runtime_uses_manifest_ports_and_demo_fixture(tmp_path: Path) -> None:
    manifest_path = tmp_path / "runtime.json"
    _write_manifest(manifest_path)
    lexical = StubRetriever()
    dense = StubRetriever()
    received: list[tuple[RuntimeManifest, bool]] = []

    def port_factory(manifest: RuntimeManifest, enable_multiview: bool) -> RetrievalPorts:
        received.append((manifest, enable_multiview))
        return RetrievalPorts(lexical, dense)

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
    assert result.trace.as_of == datetime(2026, 6, 7, 16, tzinfo=UTC)
    assert lexical.requests[0].minimum_updated_at == datetime(2025, 12, 9, 16, tzinfo=UTC)
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
            port_factory=lambda manifest, enabled: RetrievalPorts(StubRetriever(), StubRetriever()),
        )


def test_multiview_feature_flag_is_strict(tmp_path: Path) -> None:
    manifest_path = tmp_path / "runtime.json"
    _write_manifest(manifest_path)

    def factory(manifest: RuntimeManifest, enabled: bool) -> RetrievalPorts:
        del manifest, enabled
        return RetrievalPorts(StubRetriever(), StubRetriever())

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
