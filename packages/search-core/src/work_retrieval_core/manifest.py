from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from work_retrieval_core.serialization import (
    DOCUMENT_POLICY_VERSION,
    document_template_sha256,
)

ARTIFACT_KEY = re.compile(
    r"^(embeddings|models|indexes|graphs|rankers|evidence)/"
    r"(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+$"
)
SHA256 = re.compile(r"^[a-f0-9]{64}$")
DEMO_REFERENCE = "2026-06-08T23:59:59.999+08:00"
MODEL = "Qwen/Qwen3-Embedding-8B"
MODEL_REVISION = "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"
WHOLE_DIMENSION = 4096
CHALLENGERS = {
    "multiview_embedding",
    "skill_graph",
    "semantic_reranker",
    "learning_to_rank",
    "guardrails",
}


@dataclass(frozen=True, slots=True)
class Artifact:
    kind: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class Component:
    manifest_path: str
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class WholeEmbedding(Component):
    rows: int
    dimension: int
    dataset_sha256: str
    jobs_sha256: str
    job_row_order_sha256: str


@dataclass(frozen=True, slots=True)
class TemporalTantivy(Component):
    index_sha256: str
    jobs_sha256: str
    job_row_order_sha256: str
    temporal_filter_semantics: str


@dataclass(frozen=True, slots=True)
class RuntimeManifest:
    artifacts: tuple[tuple[str, Artifact], ...]
    whole_embedding: WholeEmbedding
    temporal_tantivy: TemporalTantivy
    multiview_embedding: Component | None

    @classmethod
    def from_path(cls, path: str | Path) -> RuntimeManifest:
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeError("runtime manifest cannot be read as UTF-8 JSON") from error
        return cls.from_dict(value)

    @classmethod
    def from_dict(cls, value: object) -> RuntimeManifest:
        root = _mapping(value, "manifest")
        _exact_keys(
            root,
            {
                "schema_version",
                "release",
                "retrieval_policy",
                "incumbents",
                "challengers",
                "artifacts",
            },
            "manifest",
        )
        if root["schema_version"] != 2:
            raise RuntimeError("runtime manifest schema_version must equal 2")
        artifacts, raw_artifacts = _artifacts(root["artifacts"])
        _release(root["release"], raw_artifacts)
        _retrieval_policy(root["retrieval_policy"])

        incumbents = _mapping(root["incumbents"], "incumbents")
        _exact_keys(incumbents, {"whole_embedding", "temporal_tantivy"}, "incumbents")
        whole = _whole_embedding(incumbents["whole_embedding"], artifacts)
        temporal = _temporal_tantivy(incumbents["temporal_tantivy"], artifacts)
        if (
            whole.jobs_sha256 != temporal.jobs_sha256
            or whole.job_row_order_sha256 != temporal.job_row_order_sha256
        ):
            raise RuntimeError("BM25 and whole-Qwen artifacts have different job lineage")

        challengers = _mapping(root["challengers"], "challengers")
        _exact_keys(challengers, CHALLENGERS, "challengers")
        multiview = _optional_component(challengers["multiview_embedding"], artifacts, "embedding")
        _validate_graph(challengers["skill_graph"])
        return cls(tuple(artifacts.items()), whole, temporal, multiview)

    def artifact(self, key: str) -> Artifact | None:
        return next((artifact for name, artifact in self.artifacts if name == key), None)

    def required_artifacts(self, *, include_multiview: bool) -> tuple[tuple[str, Artifact], ...]:
        components: list[Component] = [self.whole_embedding, self.temporal_tantivy]
        if include_multiview:
            if self.multiview_embedding is None:
                raise RuntimeError("multi-view flag requires an enabled manifest challenger")
            components.append(self.multiview_embedding)
        prefixes = tuple(str(PurePosixPath(item.manifest_path).parent) + "/" for item in components)
        selected = tuple(
            (path, artifact) for path, artifact in self.artifacts if path.startswith(prefixes)
        )
        if not selected:
            raise RuntimeError("runtime manifest selected no incumbent artifacts")
        return selected


def _whole_embedding(value: object, artifacts: Mapping[str, Artifact]) -> WholeEmbedding:
    raw = _mapping(value, "incumbents.whole_embedding")
    required = {
        "manifest_path",
        "manifest_sha256",
        "complete",
        "model",
        "revision",
        "dimension",
        "dtype",
        "normalized",
        "rows",
        "dataset_sha256",
        "jobs_sha256",
        "job_row_order_sha256",
        "document_policy_version",
        "document_template_sha256",
    }
    _exact_keys(raw, required, "incumbents.whole_embedding")
    expected = {
        "complete": True,
        "model": MODEL,
        "revision": MODEL_REVISION,
        "dimension": WHOLE_DIMENSION,
        "dtype": "float16",
        "normalized": True,
        "document_policy_version": DOCUMENT_POLICY_VERSION,
        "document_template_sha256": document_template_sha256(),
    }
    _equal(raw, expected, "whole-Qwen incumbent")
    path, sha256 = _component_reference(raw, artifacts, "embedding")
    rows = _positive_integer(raw["rows"], "whole embedding rows")
    order = _sha(raw["job_row_order_sha256"], "whole embedding row order")
    dataset_sha256 = _sha(raw["dataset_sha256"], "whole embedding dataset")
    jobs_sha256 = _sha(raw["jobs_sha256"], "whole embedding jobs")
    return WholeEmbedding(
        path,
        sha256,
        rows,
        WHOLE_DIMENSION,
        dataset_sha256,
        jobs_sha256,
        order,
    )


def _temporal_tantivy(value: object, artifacts: Mapping[str, Artifact]) -> TemporalTantivy:
    raw = _mapping(value, "incumbents.temporal_tantivy")
    required = {
        "manifest_path",
        "manifest_sha256",
        "complete",
        "index_sha256",
        "engine",
        "jobs_sha256",
        "job_row_order_sha256",
        "updated_at_field",
        "hard_filters",
        "temporal_filter_semantics",
    }
    _exact_keys(raw, required, "incumbents.temporal_tantivy")
    _equal(
        raw,
        {"complete": True, "updated_at_field": "updated_at_epoch_ms", "hard_filters": True},
        "temporal Tantivy incumbent",
    )
    engine = raw["engine"]
    if not isinstance(engine, str) or not engine.strip():
        raise RuntimeError("temporal Tantivy engine must be non-empty")
    semantics = raw["temporal_filter_semantics"]
    if (
        not isinstance(semantics, str)
        or "updated_at >= as_of - 180 days" not in semantics
        or "before Top-K" not in semantics
        or "updated_at <= as_of" in semantics
    ):
        raise RuntimeError("temporal Tantivy policy is incompatible with retained future rows")
    path, sha256 = _component_reference(raw, artifacts, "index")
    index_sha256 = _sha(raw["index_sha256"], "temporal Tantivy index")
    jobs_sha256 = _sha(raw["jobs_sha256"], "temporal Tantivy jobs")
    order = _sha(raw["job_row_order_sha256"], "temporal Tantivy row order")
    return TemporalTantivy(path, sha256, index_sha256, jobs_sha256, order, semantics)


def _retrieval_policy(value: object) -> None:
    policy = _mapping(value, "retrieval_policy")
    _exact_keys(policy, {"as_of", "eligibility"}, "retrieval_policy")
    as_of = _mapping(policy["as_of"], "retrieval_policy.as_of")
    eligibility = _mapping(policy["eligibility"], "retrieval_policy.eligibility")
    if as_of != {"production_mode": "request_time", "demo_reference": DEMO_REFERENCE}:
        raise RuntimeError("runtime as_of policy is incompatible")
    expected = {
        "updated_within_days": 180,
        "future_jobs": "retained_with_zero_freshness",
        "stale_jobs": "exclude",
        "applied_before_retrieval": True,
    }
    if eligibility != expected:
        raise RuntimeError("runtime eligibility policy is incompatible")


def _release(value: object, artifacts: Mapping[str, object]) -> None:
    release = _mapping(value, "release")
    required = {
        "complete",
        "publication_allowed",
        "release_spec_sha256",
        "source_manifest_sha256",
        "selected_inventory_sha256",
        "artifact_inventory_sha256",
        "object_count",
        "size_bytes",
    }
    _exact_keys(release, required, "release")
    if release["complete"] is not True or release["publication_allowed"] is not True:
        raise RuntimeError("runtime release is incomplete or not publishable")
    for name in (
        "release_spec_sha256",
        "source_manifest_sha256",
        "selected_inventory_sha256",
        "artifact_inventory_sha256",
    ):
        _sha(release[name], name)
    inventory_sha = hashlib.sha256(
        json.dumps(artifacts, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    size = sum(_mapping(item, "artifact")["size_bytes"] for item in artifacts.values())
    object_count = _positive_integer(release["object_count"], "release object_count")
    size_bytes = _positive_integer(release["size_bytes"], "release size_bytes")
    if (
        object_count != len(artifacts)
        or size_bytes != size
        or release["artifact_inventory_sha256"] != inventory_sha
    ):
        raise RuntimeError("runtime release aggregate differs from artifact inventory")


def _artifacts(value: object) -> tuple[dict[str, Artifact], Mapping[str, object]]:
    raw = _mapping(value, "artifacts")
    if not raw:
        raise RuntimeError("runtime artifact inventory is empty")
    parsed: dict[str, Artifact] = {}
    for path, value in raw.items():
        if (
            not isinstance(path, str)
            or not ARTIFACT_KEY.fullmatch(path)
            or any(part in {".", ".."} for part in path.split("/"))
        ):
            raise RuntimeError("runtime manifest contains an invalid artifact path")
        artifact = _mapping(value, f"artifacts.{path}")
        _exact_keys(artifact, {"kind", "sha256", "size_bytes"}, f"artifacts.{path}")
        kind = artifact["kind"]
        if kind not in {"embedding", "model", "index", "graph", "ranker", "evidence"}:
            raise RuntimeError(f"artifact {path} has an invalid kind")
        parsed[path] = Artifact(
            str(kind),
            _sha(artifact["sha256"], f"artifact {path}"),
            _nonnegative_integer(artifact["size_bytes"], f"artifact {path} size"),
        )
    return parsed, raw


def _optional_component(
    value: object, artifacts: Mapping[str, Artifact], kind: str
) -> Component | None:
    raw = _mapping(value, "challenger")
    enabled = raw.get("enabled")
    if enabled is False:
        if set(raw) != {"enabled"}:
            raise RuntimeError("disabled challenger carries unverified metadata")
        return None
    if enabled is not True:
        raise RuntimeError("challenger enabled flag must be boolean")
    path, sha256 = _component_reference(raw, artifacts, kind)
    if raw.get("complete") is not True or raw.get("publication_allowed") is not True:
        raise RuntimeError("enabled challenger is incomplete or not publishable")
    return Component(path, sha256)


def _validate_graph(value: object) -> None:
    raw = _mapping(value, "challengers.skill_graph")
    if raw.get("enabled") is False:
        if set(raw) != {"enabled"}:
            raise RuntimeError("disabled Graph carries unverified metadata")
        return
    if raw.get("enabled") is not True:
        raise RuntimeError("Graph enabled flag must be boolean")
    if raw.get("source_policy") != "train_jd_only" or raw.get("test_jd_used") is not False:
        raise RuntimeError("Graph artifact is not proven train-only")
    cutoff = _timestamp(raw.get("train_cutoff_exclusive"), "Graph train cutoff")
    maximum = _timestamp(raw.get("max_source_timestamp"), "Graph maximum source timestamp")
    if maximum >= cutoff:
        raise RuntimeError("Graph artifact contains post-cutoff source data")


def _component_reference(
    value: Mapping[str, Any], artifacts: Mapping[str, Artifact], kind: str
) -> tuple[str, str]:
    path = value.get("manifest_path")
    sha256 = _sha(value.get("manifest_sha256"), "component manifest")
    if not isinstance(path, str):
        raise RuntimeError("component manifest_path is missing")
    artifact = artifacts.get(path)
    if artifact is None or artifact.kind != kind or artifact.sha256 != sha256:
        raise RuntimeError("component manifest is absent or differs from artifact inventory")
    return path, sha256


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{field} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    if set(value) != expected:
        raise RuntimeError(f"{field} has missing or unknown keys")


def _equal(actual: Mapping[str, Any], expected: Mapping[str, object], field: str) -> None:
    if mismatches := [name for name, value in expected.items() if actual.get(name) != value]:
        raise RuntimeError(f"{field} differs in: {', '.join(mismatches)}")


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise RuntimeError(f"{field} must be a lowercase SHA-256")
    return value


def _positive_integer(value: object, field: str) -> int:
    parsed = _nonnegative_integer(value, field)
    if parsed == 0:
        raise RuntimeError(f"{field} must be positive")
    return parsed


def _nonnegative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"{field} must be a non-negative integer")
    return value


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError(f"{field} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise RuntimeError(f"{field} must include a timezone")
    return parsed
