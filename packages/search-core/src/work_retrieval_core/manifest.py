from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from work_retrieval_core.graph_policy import (
    GRAPH_SERVING_ALGORITHM,
    GRAPH_SERVING_IMPLEMENTATION_SHA256,
    GRAPH_SERVING_POLICY_SHA256,
)

ARTIFACT_KEY = re.compile(
    r"^(embeddings|models|indexes|graphs|rankers|evidence)/"
    r"(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+$"
)
SHA256 = re.compile(r"^[a-f0-9]{64}$")
DEMO_REFERENCE = "2026-06-08T23:59:59.999+08:00"
MODEL = "Qwen/Qwen3-Embedding-8B"
MODEL_REVISION = "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"
WHOLE_DIMENSION = 1024
SOURCE_EMBEDDING_DIMENSION = 4096
WHOLE_PROJECTION = "mrl_prefix_then_l2_normalize"
WHOLE_DOCUMENT_POLICY_VERSION = "2026-07-24-clean-v1"
WHOLE_DOCUMENT_TEMPLATE_SHA256 = "3275f93ade6c4f043084e36303d38b33443858546a80104840f0e2b9468d2abb"
WHOLE_DOCUMENT_FIELDS = (
    "職務名稱",
    "職務小類",
    "職務中類",
    "職務大類",
    "電腦技能資料",
    "工作技能",
    "專業證照",
    "工作經驗需求",
    "學歷需求",
    "工作城市",
    "產業小類",
    "產業中類",
    "產業大類",
    "附加條件",
    "職務內容",
)
CHALLENGERS = {
    "multiview_embedding",
    "skill_graph",
    "semantic_reranker",
    "learning_to_rank",
    "guardrails",
}
GRAPH_TRAIN_CUTOFF = "2026-06-08T00:00:00+08:00"
GRAPH_MAX_SOURCE_TIMESTAMP = "2026-06-07T23:51:07.143000+08:00"
GRAPH_SOURCE_JD_SHA256 = "53937f7bf076789c4cd7e3be34fb89875336108d57707b5a93182181e1087089"


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
class GraphPromotionEvidence:
    report_path: str
    report_sha256: str
    evaluation_split_sha256: str
    baseline_run_sha256: str
    candidate_run_sha256: str
    absolute_delta: float


@dataclass(frozen=True, slots=True)
class SkillGraph(Component):
    candidate_manifest_path: str
    candidate_manifest_sha256: str
    source_ablation_report_sha256: str
    evaluation_implementation_sha256: str
    organizer_attestation_path: str
    organizer_attestation_sha256: str
    promotion: GraphPromotionEvidence


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
    skill_graph: SkillGraph | None

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
        skill_graph = _skill_graph(challengers["skill_graph"], artifacts)
        for name in ("semantic_reranker", "learning_to_rank", "guardrails"):
            _disabled_challenger(challengers[name], name)
        return cls(tuple(artifacts.items()), whole, temporal, multiview, skill_graph)

    def artifact(self, key: str) -> Artifact | None:
        return next((artifact for name, artifact in self.artifacts if name == key), None)

    def required_artifacts(
        self,
        *,
        include_dense: bool,
        include_multiview: bool,
        include_graph: bool,
    ) -> tuple[tuple[str, Artifact], ...]:
        components: list[Component] = [self.temporal_tantivy]
        if include_dense:
            components.append(self.whole_embedding)
        if include_multiview:
            if self.multiview_embedding is None:
                raise RuntimeError("multi-view flag requires an enabled manifest challenger")
            components.append(self.multiview_embedding)
        if include_graph:
            if self.skill_graph is None:
                raise RuntimeError("Graph flag requires an enabled manifest challenger")
            components.append(self.skill_graph)
        prefixes = tuple(str(PurePosixPath(item.manifest_path).parent) + "/" for item in components)
        graph_evidence = (
            {
                self.skill_graph.promotion.report_path,
                self.skill_graph.candidate_manifest_path,
                self.skill_graph.organizer_attestation_path,
            }
            if include_graph and self.skill_graph is not None
            else set()
        )
        selected = tuple(
            (path, artifact)
            for path, artifact in self.artifacts
            if path.startswith(prefixes) or path in graph_evidence
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
        "source_dimension",
        "projection",
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
        "source_dimension": SOURCE_EMBEDDING_DIMENSION,
        "projection": WHOLE_PROJECTION,
        "dtype": "float16",
        "normalized": True,
        "document_policy_version": WHOLE_DOCUMENT_POLICY_VERSION,
        "document_template_sha256": WHOLE_DOCUMENT_TEMPLATE_SHA256,
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


def _skill_graph(value: object, artifacts: Mapping[str, Artifact]) -> SkillGraph | None:
    raw = _mapping(value, "challengers.skill_graph")
    if raw.get("enabled") is False:
        if set(raw) != {"enabled"}:
            raise RuntimeError("disabled Graph carries unverified metadata")
        return None
    required = {
        "enabled",
        "complete",
        "publication_allowed",
        "manifest_path",
        "manifest_sha256",
        "schema_version",
        "train_cutoff_exclusive",
        "max_source_timestamp",
        "source_jd_sha256",
        "source_policy",
        "test_jd_used",
        "candidate_manifest_sha256",
        "candidate_manifest_path",
        "source_ablation_report_sha256",
        "serving_algorithm",
        "serving_policy_sha256",
        "serving_implementation_sha256",
        "evaluation_implementation_sha256",
        "organizer_attestation_path",
        "organizer_attestation_sha256",
        "promotion_evidence",
    }
    _exact_keys(raw, required, "challengers.skill_graph")
    _equal(
        raw,
        {
            "enabled": True,
            "complete": True,
            "publication_allowed": True,
            "schema_version": 1,
            "train_cutoff_exclusive": GRAPH_TRAIN_CUTOFF,
            "max_source_timestamp": GRAPH_MAX_SOURCE_TIMESTAMP,
            "source_jd_sha256": GRAPH_SOURCE_JD_SHA256,
            "source_policy": "train_jd_only",
            "test_jd_used": False,
            "serving_algorithm": GRAPH_SERVING_ALGORITHM,
            "serving_policy_sha256": GRAPH_SERVING_POLICY_SHA256,
            "serving_implementation_sha256": GRAPH_SERVING_IMPLEMENTATION_SHA256,
        },
        "skill Graph challenger",
    )
    path, sha256 = _component_reference(raw, artifacts, "graph")
    candidate_path = _artifact_reference(
        raw["candidate_manifest_path"],
        raw["candidate_manifest_sha256"],
        artifacts,
        "evidence",
        "Graph candidate manifest",
    )
    candidate_sha256 = _sha(raw["candidate_manifest_sha256"], "Graph candidate manifest")
    ablation_sha256 = _sha(raw["source_ablation_report_sha256"], "Graph ablation report")
    evaluation_sha256 = _sha(
        raw["evaluation_implementation_sha256"], "Graph evaluation implementation"
    )
    attestation_path = _artifact_reference(
        raw["organizer_attestation_path"],
        raw["organizer_attestation_sha256"],
        artifacts,
        "evidence",
        "Graph organizer attestation",
    )
    evidence = _graph_promotion_evidence(raw["promotion_evidence"], artifacts)
    return SkillGraph(
        path,
        sha256,
        candidate_path,
        candidate_sha256,
        ablation_sha256,
        evaluation_sha256,
        attestation_path,
        _sha(raw["organizer_attestation_sha256"], "Graph organizer attestation"),
        evidence,
    )


def _graph_promotion_evidence(
    value: object, artifacts: Mapping[str, Artifact]
) -> GraphPromotionEvidence:
    raw = _mapping(value, "Graph promotion evidence")
    required = {
        "decision",
        "report_path",
        "report_sha256",
        "evaluation_split_sha256",
        "baseline_run_sha256",
        "candidate_run_sha256",
        "primary_metric",
        "absolute_delta",
    }
    _exact_keys(raw, required, "Graph promotion evidence")
    delta = raw["absolute_delta"]
    if (
        raw["decision"] != "accepted"
        or raw["primary_metric"] != "ndcg_at_10"
        or type(delta) not in {int, float}
        or not math.isfinite(delta)
        or delta <= 0
    ):
        raise RuntimeError("Graph promotion evidence is not accepted and positive")
    report_path = _artifact_reference(
        raw["report_path"],
        raw["report_sha256"],
        artifacts,
        "evidence",
        "Graph promotion report",
    )
    return GraphPromotionEvidence(
        report_path,
        _sha(raw["report_sha256"], "Graph promotion report"),
        _sha(raw["evaluation_split_sha256"], "Graph evaluation split"),
        _sha(raw["baseline_run_sha256"], "Graph baseline run"),
        _sha(raw["candidate_run_sha256"], "Graph candidate run"),
        float(delta),
    )


def _disabled_challenger(value: object, name: str) -> None:
    raw = _mapping(value, f"challengers.{name}")
    if raw != {"enabled": False}:
        raise RuntimeError(f"{name} has no production adapter and must be disabled")


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


def _artifact_reference(
    path_value: object,
    sha_value: object,
    artifacts: Mapping[str, Artifact],
    kind: str,
    field: str,
) -> str:
    path = path_value
    sha256 = _sha(sha_value, field)
    if not isinstance(path, str):
        raise RuntimeError(f"{field} path is missing")
    artifact = artifacts.get(path)
    if artifact is None or artifact.kind != kind or artifact.sha256 != sha256:
        raise RuntimeError(f"{field} is absent or differs from artifact inventory")
    return path


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
