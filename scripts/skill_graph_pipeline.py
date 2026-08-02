#!/usr/bin/env python3
"""Reference-build, validate, and trace a leakage-safe LLM evidence graph."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast

import boto3  # type: ignore[import-untyped]
from pipeline_contract import (
    artifact_entry,
    atomic_json,
    exact_keys,
    publish_s3_directory,
    read_json_object,
    require_sha256,
    sha256_file,
    verify_local_inventory,
    verify_s3_inventory,
    verify_s3_object,
)
from work_retrieval_core.graph_policy import (
    GRAPH_SERVING_ALGORITHM,
    GRAPH_SERVING_IMPLEMENTATION_SHA256,
    GRAPH_SERVING_POLICY_SHA256,
)
from work_retrieval_core.manifest import (
    GRAPH_MAX_SOURCE_TIMESTAMP,
    GRAPH_SOURCE_JD_SHA256,
    GRAPH_TRAIN_CUTOFF,
)

RELATION_TYPES = {
    "SPECIALIZATION_OF",
    "RELATED_TO",
    "USED_WITH",
}
SPLIT_MANIFEST_KEYS = {
    "schema_version",
    "split_id",
    "train_cutoff_exclusive",
    "evaluation_start_inclusive",
    "evaluation_end_exclusive",
    "qrels_sha256",
}
EXTRACTION_MANIFEST_KEYS = {
    "schema_version",
    "complete",
    "model_id",
    "prompt_version",
    "prompt_sha256",
    "canonicalization_policy",
    "oov_policy",
    "source_policy",
    "test_jd_used",
    "uses_ground_truth",
    "uses_behavior_logs",
    "train_cutoff_exclusive",
    "max_source_timestamp",
    "source_jd_sha256",
    "requests_sha256",
    "responses_inventory_sha256",
    "evidence_sha256",
    "sampling_policy",
    "sample_limit",
    "eligible_train_records",
    "sampling_sha256",
    "source_records",
    "processed_records",
    "input_tokens",
    "output_tokens",
    "skill_rejections",
    "relation_rejections",
}
EVIDENCE_KEYS = {
    "record_id",
    "job_id",
    "duty",
    "source_modified_at",
    "source_text",
    "source_text_sha256",
    "skills",
    "relations",
    "skill_rejection_count",
    "relation_rejection_count",
}
GRAPH_FILES = {
    "jobs": "jobs.jsonl",
    "skills": "skills.jsonl",
    "job_skills": "job-skills.jsonl",
    "duty_skills": "duty-skills.jsonl",
    "skill_relations": "skill-relations.jsonl",
    "relation_evidence": "relation-evidence.jsonl",
}
GRAPH_ABLATION_REPORT_KEYS = {
    "schema_version",
    "complete",
    "experiment",
    "attestation_kind",
    "candidate_manifest_sha256",
    "serving_algorithm",
    "serving_policy_sha256",
    "serving_implementation_sha256",
    "evaluation_implementation_sha256",
    "publication_allowed",
    "official_score_claimed",
    "evaluator",
    "split_manifest_sha256",
    "qrels_sha256",
    "graph_manifest_sha256",
    "graph_off_run_sha256",
    "graph_on_run_sha256",
    "non_graph_inputs",
    "query_count",
    "graph_off",
    "graph_on",
    "delta",
    "minimum_ndcg_delta",
    "organizer_significant",
    "metric_gate_passed",
    "promotion_allowed",
    "promotion_gate",
    "controls",
}
GRAPH_ATTESTATION_KEYS = {
    "schema_version",
    "complete",
    "attestation_kind",
    "candidate_manifest_sha256",
    "ablation_report_sha256",
    "publication_allowed",
    "evaluator_id",
    "evaluator_kind",
    "significant",
    "primary_metric",
    "baseline_value",
    "candidate_value",
    "absolute_delta",
    "evaluation_split_sha256",
    "baseline_run_sha256",
    "candidate_run_sha256",
    "serving_algorithm",
    "serving_policy_sha256",
    "serving_implementation_sha256",
    "evaluation_implementation_sha256",
}
CATEGORY_RESOLUTION_POLICY = "support_majority_then_lexical_v1"
EXTRACTION_LINEAGE_FIELDS = (
    "model_id",
    "prompt_version",
    "prompt_sha256",
    "canonicalization_policy",
    "oov_policy",
    "max_source_timestamp",
    "source_jd_sha256",
    "requests_sha256",
    "responses_inventory_sha256",
    "evidence_sha256",
    "sampling_policy",
    "sample_limit",
    "eligible_train_records",
    "sampling_sha256",
    "source_records",
    "processed_records",
    "input_tokens",
    "output_tokens",
    "skill_rejections",
    "relation_rejections",
)


def _canonical_jsonl_line(value: dict[str, object]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        + b"\n"
    )


def _canonical_jsonl_sha256(values: list[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(_canonical_jsonl_line(value))
    return digest.hexdigest()


def _atomic_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    partial = path.with_name(f".{path.name}.{os.getpid()}.partial")
    if partial.exists():
        raise RuntimeError(f"partial output already exists: {partial}")
    try:
        with partial.open("wb") as output:
            for value in values:
                output.write(_canonical_jsonl_line(value))
            output.flush()
            os.fsync(output.fileno())
        partial.replace(path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def _read_jsonl(path: Path, name: str) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    try:
        with path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                value = json.loads(
                    line,
                    parse_constant=lambda constant: (_ for _ in ()).throw(
                        ValueError(f"non-finite JSON number: {constant}")
                    ),
                )
                if not isinstance(value, dict):
                    raise RuntimeError(f"{name} line {line_number} must be an object")
                values.append(value)
    except (OSError, UnicodeError, ValueError) as error:
        raise RuntimeError(f"{name} cannot be read as UTF-8 JSONL") from error
    return values


def _sorted_unique_identities(
    rows: list[dict[str, object]], fields: tuple[str, ...], name: str
) -> list[tuple[str, ...]]:
    identities = [tuple(str(row[field]) for field in fields) for row in rows]
    if identities != sorted(set(identities)):
        raise RuntimeError(f"{name} rows must be canonically sorted and unique")
    return identities


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


def _unit_weight(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= 1
    ):
        raise RuntimeError(f"{name} must be finite and inside (0, 1]")
    return float(value)


def _normalize(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"{name} must be a string")
    normalized = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    if not normalized:
        raise RuntimeError(f"{name} must be non-empty")
    return normalized


def _timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError(f"{name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError(f"{name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"{name} must include a timezone")
    return parsed


def load_split_manifest(path: Path) -> tuple[dict[str, object], datetime]:
    manifest = read_json_object(path, "evaluation split manifest")
    exact_keys(manifest, SPLIT_MANIFEST_KEYS, "evaluation split manifest")
    cutoff = _timestamp(manifest["train_cutoff_exclusive"], "train cutoff")
    evaluation_start = _timestamp(manifest["evaluation_start_inclusive"], "evaluation start")
    evaluation_end = _timestamp(manifest["evaluation_end_exclusive"], "evaluation end")
    if (
        manifest["schema_version"] != 1
        or not isinstance(manifest["split_id"], str)
        or not manifest["split_id"].strip()
        or cutoff != evaluation_start
        or evaluation_end <= evaluation_start
    ):
        raise RuntimeError("evaluation split time contract differs")
    require_sha256(manifest["qrels_sha256"], "evaluation qrels SHA-256")
    return manifest, cutoff


def _load_extraction(
    evidence_path: Path, manifest_path: Path, train_cutoff: datetime
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = read_json_object(manifest_path, "LLM extraction manifest")
    exact_keys(manifest, EXTRACTION_MANIFEST_KEYS, "LLM extraction manifest")
    if (
        manifest["schema_version"] != 1
        or manifest["complete"] is not True
        or manifest["source_policy"] != "train_jd_only"
        or manifest["test_jd_used"] is not False
        or manifest["uses_ground_truth"] is not False
        or manifest["uses_behavior_logs"] is not False
        or _timestamp(manifest["train_cutoff_exclusive"], "extraction train cutoff") != train_cutoff
    ):
        raise RuntimeError("LLM extraction leakage policy is incompatible")
    for name in ("source_jd_sha256", "evidence_sha256"):
        require_sha256(manifest[name], name)
    for name in ("prompt_sha256", "requests_sha256", "responses_inventory_sha256"):
        require_sha256(manifest[name], name)
    require_sha256(manifest["sampling_sha256"], "sampling SHA-256")
    if (
        manifest["canonicalization_policy"] != "open_surface_per_jd_llm_canonicalization_v1"
        or manifest["oov_policy"] != "accept_open_surface_with_exact_train_jd_evidence"
    ):
        raise RuntimeError("LLM extraction OOV/canonicalization policy is incompatible")
    for name in (
        "sample_limit",
        "eligible_train_records",
        "source_records",
        "processed_records",
        "input_tokens",
        "output_tokens",
    ):
        value = manifest[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError(f"{name} must be a non-negative integer")
    if (
        manifest["sampling_policy"] != "duty_stratified_sqrt_support_stable_hash_v1"
        or not 1 <= manifest["sample_limit"] <= 10_000
        or not manifest["source_records"] <= manifest["eligible_train_records"]
        or manifest["source_records"] != manifest["processed_records"]
    ):
        raise RuntimeError("LLM extraction is incomplete")
    if sha256_file(evidence_path) != manifest["evidence_sha256"]:
        raise RuntimeError("LLM evidence bytes differ from extraction manifest")
    maximum = _timestamp(manifest["max_source_timestamp"], "maximum source timestamp")
    if maximum >= train_cutoff:
        raise RuntimeError("LLM evidence contains test-period JD data")
    if not isinstance(manifest["model_id"], str) or not manifest["model_id"].strip():
        raise RuntimeError("LLM extraction model_id is missing")
    if not isinstance(manifest["prompt_version"], str) or not manifest["prompt_version"].strip():
        raise RuntimeError("LLM extraction prompt_version is missing")

    records: list[dict[str, Any]] = []
    seen_records: set[str] = set()
    seen_jobs: set[str] = set()
    observed_maximum: datetime | None = None
    with evidence_path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError(f"invalid evidence JSON at line {line_number}") from error
            if not isinstance(raw, dict):
                raise RuntimeError(f"evidence line {line_number} must be an object")
            exact_keys(raw, EVIDENCE_KEYS, f"evidence line {line_number}")
            record_id = raw["record_id"]
            job_id = raw["job_id"]
            if (
                not isinstance(record_id, str)
                or len(record_id) != 64
                or any(character not in "0123456789abcdef" for character in record_id)
                or not isinstance(job_id, str)
                or not job_id.isascii()
                or not job_id.isdecimal()
            ):
                raise RuntimeError("evidence has an invalid record_id or job_id")
            if record_id in seen_records or job_id in seen_jobs:
                raise RuntimeError("evidence has a duplicate record or job_id")
            seen_records.add(record_id)
            seen_jobs.add(job_id)
            modified_at = _timestamp(raw["source_modified_at"], "source_modified_at")
            if modified_at >= train_cutoff or modified_at > maximum:
                raise RuntimeError("evidence row exceeds the train-only time boundary")
            observed_maximum = (
                max(observed_maximum, modified_at) if observed_maximum else modified_at
            )
            source_text = raw["source_text"]
            if not isinstance(source_text, str) or not source_text.strip():
                raise RuntimeError("evidence source_text is empty")
            expected_source_sha = require_sha256(raw["source_text_sha256"], "source_text_sha256")
            if (
                hashlib.sha256(source_text.encode()).hexdigest() != expected_source_sha
                or hashlib.sha256(f"{job_id}\0{expected_source_sha}".encode()).hexdigest()
                != record_id
            ):
                raise RuntimeError("evidence source or record identity differs")
            skills = _skills(raw["skills"], source_text)
            relations = _relations(raw["relations"], source_text, set(skills))
            rejection_counts: dict[str, int] = {}
            for name in ("skill_rejection_count", "relation_rejection_count"):
                value = raw[name]
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise RuntimeError(f"{name} must be a non-negative integer")
                rejection_counts[name] = value
            records.append(
                {
                    "record_id": record_id,
                    "job_id": job_id,
                    "duty": _normalize(raw["duty"], "duty"),
                    "source_modified_at": modified_at.isoformat(),
                    "source_text_sha256": expected_source_sha,
                    "skills": skills,
                    "relations": relations,
                    **rejection_counts,
                }
            )
    if (
        not records
        or observed_maximum != maximum
        or len(records) != manifest["source_records"]
        or len(records) != manifest["processed_records"]
        or hashlib.sha256(
            json.dumps(
                [record["record_id"] for record in records],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        != manifest["sampling_sha256"]
    ):
        raise RuntimeError("evidence is empty or maximum source timestamp differs")
    rejection_totals = {
        "skill_rejections": sum(record["skill_rejection_count"] for record in records),
        "relation_rejections": sum(record["relation_rejection_count"] for record in records),
    }
    for name, total in rejection_totals.items():
        declared = manifest[name]
        if isinstance(declared, bool) or not isinstance(declared, int) or declared != total:
            raise RuntimeError(f"{name} differs from evidence records")
    return manifest, records


def _skills(value: object, source_text: str) -> dict[str, dict[str, str]]:
    if not isinstance(value, list):
        raise RuntimeError("evidence skills must be an array")
    parsed: dict[str, dict[str, str]] = {}
    for position, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise RuntimeError("skill evidence must be an object")
        exact_keys(
            raw,
            {"canonical_name", "surface", "category", "evidence_span"},
            f"skill {position}",
        )
        skill = _normalize(raw["canonical_name"], "canonical skill")
        surface_value = raw["surface"]
        surface = _normalize(surface_value, "skill surface")
        category = _normalize(raw["category"], "skill category")
        span = raw["evidence_span"]
        if (
            not isinstance(surface_value, str)
            or not surface_value
            or surface_value not in source_text
            or not isinstance(span, str)
            or not span
            or span not in source_text
            or surface_value not in span
            or skill in parsed
        ):
            raise RuntimeError("skill evidence is absent from source text or duplicated")
        parsed[skill] = {"surface": surface, "category": category, "evidence_span": span}
    return parsed


def _relations(
    value: object,
    source_text: str,
    skills: set[str],
) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise RuntimeError("relations must be an array")
    parsed: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for position, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise RuntimeError("relation evidence must be an object")
        exact_keys(raw, {"source", "type", "target", "evidence_span"}, f"relation {position}")
        source = _normalize(raw["source"], "relation source")
        target = _normalize(raw["target"], "relation target")
        relation_type = raw["type"]
        span = raw["evidence_span"]
        identity = (source, str(relation_type), target)
        if (
            source == target
            or source not in skills
            or target not in skills
            or relation_type not in RELATION_TYPES
            or not isinstance(span, str)
            or not span
            or span not in source_text
            or identity in seen
        ):
            raise RuntimeError("relation is unsupported, duplicated, or lacks source evidence")
        seen.add(identity)
        parsed.append(
            {"source": source, "type": str(relation_type), "target": target, "evidence_span": span}
        )
    return parsed


def _derive_graph_tables(
    records: list[dict[str, Any]], minimum_support: int
) -> tuple[dict[str, list[dict[str, object]]], dict[str, Counter[str]]]:
    skill_support = Counter(skill for record in records for skill in set(record["skills"]))
    promoted = {skill for skill, count in skill_support.items() if count >= minimum_support}
    if not promoted:
        raise RuntimeError("minimum_support removed every skill")
    category_support: dict[str, Counter[str]] = {skill: Counter() for skill in promoted}
    for record in records:
        for skill, evidence in record["skills"].items():
            if skill in promoted:
                category_support[skill][evidence["category"]] += 1
    categories = {
        skill: min(support, key=lambda category: (-support[category], category))
        for skill, support in category_support.items()
    }
    category_conflicts = {
        skill: support for skill, support in category_support.items() if len(support) > 1
    }
    duty_jobs = Counter(record["duty"] for record in records)
    duty_support = Counter(
        (record["duty"], skill)
        for record in records
        for skill in set(record["skills"]).intersection(promoted)
    )
    relation_support = Counter(
        (relation["source"], relation["type"], relation["target"])
        for record in records
        for relation in record["relations"]
        if relation["source"] in promoted and relation["target"] in promoted
    )
    promoted_relations = {
        relation for relation, support in relation_support.items() if support >= minimum_support
    }
    tables: dict[str, list[dict[str, object]]] = {
        "jobs": [
            {
                "job_id": record["job_id"],
                "duty": record["duty"],
                "source_modified_at": record["source_modified_at"],
                "source_text_sha256": record["source_text_sha256"],
            }
            for record in sorted(records, key=lambda item: item["job_id"])
        ],
        "skills": [
            {
                "skill": skill,
                "category": categories[skill],
                "support": skill_support[skill],
            }
            for skill in sorted(promoted)
        ],
        "job_skills": [
            {
                "job_id": record["job_id"],
                "skill": skill,
                "surface": evidence["surface"],
                "evidence_span": evidence["evidence_span"],
            }
            for record in sorted(records, key=lambda item: item["job_id"])
            for skill, evidence in sorted(record["skills"].items())
            if skill in promoted
        ],
        "duty_skills": [
            {
                "duty": duty,
                "skill": skill,
                "support": support,
                "weight": support / duty_jobs[duty],
            }
            for (duty, skill), support in sorted(duty_support.items())
        ],
        "skill_relations": [
            {
                "source": source,
                "type": relation_type,
                "target": target,
                "support": support,
                "weight": support / math.sqrt(skill_support[source] * skill_support[target]),
            }
            for (source, relation_type, target), support in sorted(relation_support.items())
            if (source, relation_type, target) in promoted_relations
        ],
        "relation_evidence": [
            {
                "job_id": record["job_id"],
                "source": relation["source"],
                "type": relation["type"],
                "target": relation["target"],
                "evidence_span": relation["evidence_span"],
            }
            for record in sorted(records, key=lambda item: item["job_id"])
            for relation in sorted(
                record["relations"],
                key=lambda item: (item["source"], item["type"], item["target"]),
            )
            if (relation["source"], relation["type"], relation["target"]) in promoted_relations
        ],
    }
    return tables, category_conflicts


def build_graph(
    *,
    evidence_path: Path,
    extraction_manifest_path: Path,
    split_manifest_path: Path,
    output: Path,
    minimum_support: int,
) -> dict[str, object]:
    if minimum_support < 1:
        raise ValueError("minimum_support must be positive")
    split, train_cutoff = load_split_manifest(split_manifest_path)
    extraction, records = _load_extraction(evidence_path, extraction_manifest_path, train_cutoff)
    if output.exists():
        raise RuntimeError("graph output already exists; builds never overwrite artifacts")
    output.parent.mkdir(parents=True, exist_ok=True)
    tables, category_conflicts = _derive_graph_tables(records, minimum_support)
    counts = {name: len(values) for name, values in tables.items()}
    build_root = output.with_name(f".{output.name}.{os.getpid()}.partial")
    if build_root.exists():
        raise RuntimeError(f"partial graph output already exists: {build_root}")
    build_root.mkdir()
    manifest_path = build_root / "manifest.json"
    graph_paths = {name: build_root / filename for name, filename in GRAPH_FILES.items()}
    report: dict[str, object] = {
        "schema_version": 1,
        "complete": True,
        "publication_allowed": False,
        "publication_gate": "pending_fixed_input_graph_ablation",
        "graph_schema_version": 1,
        "graph_kind": "llm-evidence-locked-typed-entity-graph",
        "source_policy": "train_jd_only",
        "test_jd_used": False,
        "uses_ground_truth": False,
        "uses_behavior_logs": False,
        "split_id": split["split_id"],
        "split_manifest_sha256": sha256_file(split_manifest_path),
        "train_cutoff_exclusive": train_cutoff.isoformat(),
        "max_source_timestamp": extraction["max_source_timestamp"],
        "source_jd_sha256": extraction["source_jd_sha256"],
        "evidence_sha256": extraction["evidence_sha256"],
        "model_id": extraction["model_id"],
        "prompt_version": extraction["prompt_version"],
        "prompt_sha256": extraction["prompt_sha256"],
        "canonicalization_policy": extraction["canonicalization_policy"],
        "oov_policy": extraction["oov_policy"],
        "requests_sha256": extraction["requests_sha256"],
        "responses_inventory_sha256": extraction["responses_inventory_sha256"],
        "sampling_policy": extraction["sampling_policy"],
        "sample_limit": extraction["sample_limit"],
        "eligible_train_records": extraction["eligible_train_records"],
        "sampling_sha256": extraction["sampling_sha256"],
        "source_records": extraction["source_records"],
        "processed_records": extraction["processed_records"],
        "input_tokens": extraction["input_tokens"],
        "output_tokens": extraction["output_tokens"],
        "skill_rejections": extraction["skill_rejections"],
        "relation_rejections": extraction["relation_rejections"],
        "category_resolution_policy": CATEGORY_RESOLUTION_POLICY,
        "category_conflict_skills": len(category_conflicts),
        "category_mentions_in_conflict_sets": sum(
            support.total() for support in category_conflicts.values()
        ),
        "minimum_support": minimum_support,
        "maximum_traversal_hops": 1,
        "counts": counts,
        "artifacts": [],
    }
    try:
        for name, values in tables.items():
            _atomic_jsonl(graph_paths[name], values)
        report["artifacts"] = [
            artifact_entry(graph_paths[name], relative_to=build_root, kind="graph")
            for name in sorted(graph_paths)
        ]
        atomic_json(manifest_path, report)
        validate_graph(
            build_root,
            split_manifest_path,
            evidence_path=evidence_path,
            extraction_manifest_path=extraction_manifest_path,
        )
        build_root.replace(output)
    except BaseException:
        shutil.rmtree(build_root, ignore_errors=True)
        raise
    return report


def validate_graph(
    output: Path,
    split_manifest_path: Path,
    *,
    evidence_path: Path,
    extraction_manifest_path: Path,
) -> dict[str, object]:
    split, train_cutoff = load_split_manifest(split_manifest_path)
    extraction, records = _load_extraction(evidence_path, extraction_manifest_path, train_cutoff)
    manifest = read_json_object(output / "manifest.json", "skill graph manifest")
    expected_keys = {
        "schema_version",
        "complete",
        "publication_allowed",
        "publication_gate",
        "graph_schema_version",
        "graph_kind",
        "source_policy",
        "test_jd_used",
        "uses_ground_truth",
        "uses_behavior_logs",
        "split_id",
        "split_manifest_sha256",
        "train_cutoff_exclusive",
        "max_source_timestamp",
        "source_jd_sha256",
        "evidence_sha256",
        "model_id",
        "prompt_version",
        "prompt_sha256",
        "canonicalization_policy",
        "oov_policy",
        "requests_sha256",
        "responses_inventory_sha256",
        "sampling_policy",
        "sample_limit",
        "eligible_train_records",
        "sampling_sha256",
        "source_records",
        "processed_records",
        "input_tokens",
        "output_tokens",
        "skill_rejections",
        "relation_rejections",
        "category_resolution_policy",
        "category_conflict_skills",
        "category_mentions_in_conflict_sets",
        "minimum_support",
        "maximum_traversal_hops",
        "counts",
        "artifacts",
    }
    exact_keys(manifest, expected_keys, "skill graph manifest")
    if (
        manifest["schema_version"] != 1
        or manifest["complete"] is not True
        or manifest["publication_allowed"] is not False
        or manifest["publication_gate"] != "pending_fixed_input_graph_ablation"
        or manifest["graph_schema_version"] != 1
        or manifest["graph_kind"] != "llm-evidence-locked-typed-entity-graph"
        or manifest["source_policy"] != "train_jd_only"
        or manifest["test_jd_used"] is not False
        or manifest["uses_ground_truth"] is not False
        or manifest["uses_behavior_logs"] is not False
        or manifest["split_id"] != split["split_id"]
        or manifest["split_manifest_sha256"] != sha256_file(split_manifest_path)
        or _timestamp(manifest["train_cutoff_exclusive"], "graph train cutoff") != train_cutoff
        or _timestamp(manifest["max_source_timestamp"], "max_source_timestamp") >= train_cutoff
        or manifest["maximum_traversal_hops"] != 1
        or manifest["sampling_policy"] != "duty_stratified_sqrt_support_stable_hash_v1"
        or manifest["category_resolution_policy"] != CATEGORY_RESOLUTION_POLICY
        or any(
            isinstance(manifest[name], bool)
            or not isinstance(manifest[name], int)
            or manifest[name] < 0
            for name in (
                "skill_rejections",
                "relation_rejections",
                "category_conflict_skills",
                "category_mentions_in_conflict_sets",
            )
        )
    ):
        raise RuntimeError("skill graph manifest policy differs")
    for name in (
        "split_manifest_sha256",
        "source_jd_sha256",
        "evidence_sha256",
        "prompt_sha256",
        "requests_sha256",
        "responses_inventory_sha256",
        "sampling_sha256",
    ):
        require_sha256(manifest[name], f"skill graph {name}")
    for name in (
        "sample_limit",
        "eligible_train_records",
        "source_records",
        "processed_records",
        "minimum_support",
    ):
        value = manifest[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise RuntimeError(f"skill graph {name} must be a positive integer")
    for name in ("input_tokens", "output_tokens"):
        value = manifest[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError(f"skill graph {name} must be a non-negative integer")
    if (
        manifest["sample_limit"] > 10_000
        or manifest["source_records"] > manifest["sample_limit"]
        or manifest["source_records"] > manifest["eligible_train_records"]
        or manifest["processed_records"] != manifest["source_records"]
    ):
        raise RuntimeError("skill graph sampling lineage differs")
    for name in ("model_id", "prompt_version", "canonicalization_policy", "oov_policy"):
        if not isinstance(manifest[name], str) or not manifest[name]:
            raise RuntimeError(f"skill graph {name} must be non-empty")
    if any(manifest[name] != extraction[name] for name in EXTRACTION_LINEAGE_FIELDS):
        raise RuntimeError("skill graph extraction lineage differs")
    artifacts = verify_local_inventory(output, manifest["artifacts"])
    if {artifact["path"] for artifact in artifacts} != set(GRAPH_FILES.values()):
        raise RuntimeError("skill graph artifact inventory differs from the closed schema")
    tables = {name: _read_jsonl(output / filename, name) for name, filename in GRAPH_FILES.items()}
    expected_tables, expected_category_conflicts = _derive_graph_tables(
        records, cast(int, manifest["minimum_support"])
    )
    for name, expected_rows in expected_tables.items():
        if tables[name] != expected_rows or sha256_file(
            output / GRAPH_FILES[name]
        ) != _canonical_jsonl_sha256(expected_rows):
            raise RuntimeError(f"{name} differs from the pinned LLM extraction evidence")
    if manifest["category_conflict_skills"] != len(expected_category_conflicts) or manifest[
        "category_mentions_in_conflict_sets"
    ] != sum(support.total() for support in expected_category_conflicts.values()):
        raise RuntimeError("skill graph category conflict audit differs from extraction evidence")
    schema = {
        "jobs": {"job_id", "duty", "source_modified_at", "source_text_sha256"},
        "skills": {"skill", "category", "support"},
        "job_skills": {"job_id", "skill", "surface", "evidence_span"},
        "duty_skills": {"duty", "skill", "support", "weight"},
        "skill_relations": {"source", "type", "target", "support", "weight"},
        "relation_evidence": {"job_id", "source", "type", "target", "evidence_span"},
    }
    for name, rows in tables.items():
        for position, row in enumerate(rows):
            exact_keys(row, schema[name], f"{name} row {position}")
    _sorted_unique_identities(tables["jobs"], ("job_id",), "jobs")
    _sorted_unique_identities(tables["skills"], ("skill",), "skills")
    job_skill_ids = _sorted_unique_identities(
        tables["job_skills"], ("job_id", "skill"), "job_skills"
    )
    duty_skill_ids = _sorted_unique_identities(
        tables["duty_skills"], ("duty", "skill"), "duty_skills"
    )
    relation_ids = _sorted_unique_identities(
        tables["skill_relations"], ("source", "type", "target"), "skill_relations"
    )
    relation_evidence_ids = _sorted_unique_identities(
        tables["relation_evidence"],
        ("job_id", "source", "type", "target"),
        "relation_evidence",
    )
    job_ids = [str(row["job_id"]) for row in tables["jobs"]]
    job_timestamps = [
        _timestamp(row["source_modified_at"], "graph job source timestamp")
        for row in tables["jobs"]
    ]
    skills = [str(row["skill"]) for row in tables["skills"]]
    if (
        any(
            not job_id.isascii()
            or not job_id.isdecimal()
            or int(job_id) < 1
            or job_id != str(int(job_id))
            for job_id in job_ids
        )
        or any(
            not isinstance(row["duty"], str)
            or _normalize(row["duty"], "graph job duty") != row["duty"]
            or _timestamp(row["source_modified_at"], "graph job source timestamp").isoformat()
            != row["source_modified_at"]
            for row in tables["jobs"]
        )
        or any(_normalize(skill, "graph skill") != skill for skill in skills)
        or any(
            not isinstance(row["category"], str)
            or _normalize(row["category"], "graph skill category") != row["category"]
            for row in tables["skills"]
        )
        or not job_timestamps
        or max(job_timestamps)
        != _timestamp(manifest["max_source_timestamp"], "graph maximum source timestamp")
        or any(timestamp >= train_cutoff for timestamp in job_timestamps)
        or any(
            require_sha256(row["source_text_sha256"], "graph job source text SHA-256")
            != row["source_text_sha256"]
            for row in tables["jobs"]
        )
    ):
        raise RuntimeError("skill graph entities are duplicated or not canonical")
    job_id_set = set(job_ids)
    skill_set = set(skills)
    job_skill_set = set(job_skill_ids)
    if any(
        str(row["job_id"]) not in job_id_set
        or str(row["skill"]) not in skill_set
        or _normalize(row["skill"], "job skill") != row["skill"]
        or not isinstance(row["surface"], str)
        or _normalize(row["surface"], "job skill surface") != row["surface"]
        or not isinstance(row["evidence_span"], str)
        or not row["evidence_span"]
        for row in tables["job_skills"]
    ):
        raise RuntimeError("skill graph edge references an unknown entity")
    minimum_support = cast(int, manifest["minimum_support"])
    observed_skill_support = Counter(skill for _job_id, skill in job_skill_ids)
    declared_skill_support: dict[str, int] = {}
    for row in tables["skills"]:
        skill = str(row["skill"])
        support = _positive_integer(row["support"], f"skill support {skill}")
        if support < minimum_support or support != observed_skill_support[skill]:
            raise RuntimeError("skill support differs from Job-to-Skill evidence")
        declared_skill_support[skill] = support
    if set(observed_skill_support) != skill_set:
        raise RuntimeError("skill nodes differ from Job-to-Skill evidence")

    duty_by_job = {str(row["job_id"]): str(row["duty"]) for row in tables["jobs"]}
    duty_job_counts = Counter(duty_by_job.values())
    observed_duty_support = Counter((duty_by_job[job_id], skill) for job_id, skill in job_skill_ids)
    if set(duty_skill_ids) != set(observed_duty_support):
        raise RuntimeError("Duty-to-Skill edges differ from Job-to-Skill evidence")
    for row in tables["duty_skills"]:
        duty_identity = (str(row["duty"]), str(row["skill"]))
        if (
            duty_identity[1] not in skill_set
            or _normalize(duty_identity[0], "duty skill duty") != duty_identity[0]
            or _normalize(duty_identity[1], "duty skill") != duty_identity[1]
        ):
            raise RuntimeError("skill graph edge references an unknown entity")
        support = _positive_integer(row["support"], f"duty skill support {duty_identity}")
        weight = _unit_weight(row["weight"], f"duty skill weight {duty_identity}")
        if (
            support != observed_duty_support[duty_identity]
            or weight != support / duty_job_counts[duty_identity[0]]
        ):
            raise RuntimeError("Duty-to-Skill aggregate differs from Job-to-Skill evidence")

    if any(
        str(row["source"]) not in skills
        or str(row["target"]) not in skills
        or str(row["source"]) == str(row["target"])
        or _normalize(row["source"], "relation source") != row["source"]
        or _normalize(row["target"], "relation target") != row["target"]
        or str(row["type"]) not in RELATION_TYPES
        for row in tables["skill_relations"]
    ):
        raise RuntimeError("skill relation references an unknown entity or type")
    relation_set = set(relation_ids)
    if any(
        str(row["job_id"]) not in job_id_set
        or (str(row["source"]), str(row["type"]), str(row["target"])) not in relation_set
        or (str(row["job_id"]), str(row["source"])) not in job_skill_set
        or (str(row["job_id"]), str(row["target"])) not in job_skill_set
        or not isinstance(row["evidence_span"], str)
        or not row["evidence_span"]
        for row in tables["relation_evidence"]
    ):
        raise RuntimeError("relation evidence references an unknown relation")
    observed_relation_support = Counter(identity[1:] for identity in relation_evidence_ids)
    if relation_set != set(observed_relation_support):
        raise RuntimeError("skill relations differ from relation evidence")
    for row in tables["skill_relations"]:
        relation_identity = (str(row["source"]), str(row["type"]), str(row["target"]))
        support = _positive_integer(row["support"], f"skill relation support {relation_identity}")
        weight = _unit_weight(row["weight"], f"skill relation weight {relation_identity}")
        expected_weight = support / math.sqrt(
            declared_skill_support[relation_identity[0]]
            * declared_skill_support[relation_identity[2]]
        )
        if (
            support < minimum_support
            or support != observed_relation_support[relation_identity]
            or weight != expected_weight
        ):
            raise RuntimeError("skill relation aggregate differs from relation evidence")
    counts = {name: len(rows) for name, rows in tables.items()}
    if counts != manifest["counts"] or len(job_ids) != manifest["source_records"]:
        raise RuntimeError("skill graph row counts differ from manifest")
    inventory_bytes = json.dumps(
        artifacts,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return {
        "passed": True,
        "counts": counts,
        "graph_sha256": hashlib.sha256(inventory_bytes).hexdigest(),
    }


def approve_graph(
    *,
    graph_output: Path,
    split_manifest_path: Path,
    evidence_path: Path,
    extraction_manifest_path: Path,
    ablation_report_path: Path,
    ablation_report_sha256: str,
    organizer_attestation_path: Path,
    organizer_attestation_sha256: str,
    runtime_prefix: str,
    candidate_manifest_runtime_path: str,
    promotion_report_runtime_path: str,
    organizer_attestation_runtime_path: str,
    output: Path,
) -> dict[str, object]:
    if output.exists():
        raise RuntimeError("promoted Graph output already exists; approvals never overwrite")
    validate_graph(
        graph_output,
        split_manifest_path,
        evidence_path=evidence_path,
        extraction_manifest_path=extraction_manifest_path,
    )
    for digest, name in (
        (ablation_report_sha256, "Graph ablation report"),
        (organizer_attestation_sha256, "Graph organizer attestation"),
    ):
        require_sha256(digest, f"{name} SHA-256")
    if sha256_file(ablation_report_path) != ablation_report_sha256:
        raise RuntimeError("Graph ablation report bytes differ")
    if sha256_file(organizer_attestation_path) != organizer_attestation_sha256:
        raise RuntimeError("Graph organizer attestation bytes differ")
    graph_manifest = read_json_object(graph_output / "manifest.json", "skill graph manifest")
    candidate_manifest_sha256 = sha256_file(graph_output / "manifest.json")
    if (
        graph_manifest["train_cutoff_exclusive"] != GRAPH_TRAIN_CUTOFF
        or graph_manifest["max_source_timestamp"] != GRAPH_MAX_SOURCE_TIMESTAMP
        or graph_manifest["source_jd_sha256"] != GRAPH_SOURCE_JD_SHA256
    ):
        raise RuntimeError("Graph candidate differs from the approved production source snapshot")
    report = read_json_object(ablation_report_path, "Graph ablation report")
    exact_keys(report, GRAPH_ABLATION_REPORT_KEYS, "Graph ablation report")
    evaluator = report["evaluator"]
    graph_off = report["graph_off"]
    graph_on = report["graph_on"]
    delta = report["delta"]
    if not all(isinstance(value, dict) for value in (evaluator, graph_off, graph_on, delta)):
        raise RuntimeError("Graph ablation metrics are malformed")
    baseline_value = cast(dict[str, object], graph_off).get("ndcg_at_10")
    candidate_value = cast(dict[str, object], graph_on).get("ndcg_at_10")
    absolute_delta = cast(dict[str, object], delta).get("ndcg_at_10")
    if (
        report["schema_version"] != 1
        or report["complete"] is not True
        or report["experiment"] != "fixed-input-graph-on-off-ablation"
        or report["attestation_kind"] != "fixed-input-graph-promotion"
        or report["candidate_manifest_sha256"] != candidate_manifest_sha256
        or report["graph_manifest_sha256"] != candidate_manifest_sha256
        or report["serving_algorithm"] != GRAPH_SERVING_ALGORITHM
        or report["serving_policy_sha256"] != GRAPH_SERVING_POLICY_SHA256
        or report["serving_implementation_sha256"] != GRAPH_SERVING_IMPLEMENTATION_SHA256
        or report["publication_allowed"] is not False
        or report["promotion_allowed"] is not False
        or report["promotion_gate"] != "requires_external_organizer_attestation"
        or report["metric_gate_passed"] is not True
        or report["organizer_significant"] is not True
        or cast(dict[str, object], evaluator).get("kind") != "organizer"
        or type(baseline_value) not in {int, float}
        or type(candidate_value) not in {int, float}
        or type(absolute_delta) not in {int, float}
        or not all(
            math.isfinite(cast(float, value))
            for value in (baseline_value, candidate_value, absolute_delta)
        )
        or cast(float, absolute_delta) <= 0
        or not math.isclose(
            cast(float, candidate_value) - cast(float, baseline_value),
            cast(float, absolute_delta),
            rel_tol=0,
            abs_tol=1e-12,
        )
    ):
        raise RuntimeError("Graph ablation evidence did not pass")
    require_sha256(
        report["evaluation_implementation_sha256"],
        "Graph evaluation implementation SHA-256",
    )

    attestation = read_json_object(organizer_attestation_path, "Graph organizer attestation")
    exact_keys(attestation, GRAPH_ATTESTATION_KEYS, "Graph organizer attestation")
    expected_attestation = {
        "schema_version": 1,
        "complete": True,
        "attestation_kind": "fixed-input-graph-promotion",
        "candidate_manifest_sha256": candidate_manifest_sha256,
        "ablation_report_sha256": ablation_report_sha256,
        "publication_allowed": True,
        "evaluator_id": cast(dict[str, object], evaluator).get("id"),
        "evaluator_kind": "organizer",
        "significant": True,
        "primary_metric": "ndcg_at_10",
        "baseline_value": baseline_value,
        "candidate_value": candidate_value,
        "absolute_delta": absolute_delta,
        "evaluation_split_sha256": report["split_manifest_sha256"],
        "baseline_run_sha256": report["graph_off_run_sha256"],
        "candidate_run_sha256": report["graph_on_run_sha256"],
        "serving_algorithm": GRAPH_SERVING_ALGORITHM,
        "serving_policy_sha256": GRAPH_SERVING_POLICY_SHA256,
        "serving_implementation_sha256": GRAPH_SERVING_IMPLEMENTATION_SHA256,
        "evaluation_implementation_sha256": report["evaluation_implementation_sha256"],
    }
    if attestation != expected_attestation:
        raise RuntimeError("Graph organizer attestation differs from the evaluated candidate")

    prefix = _runtime_prefix(runtime_prefix, "graphs")
    candidate_runtime_path = _runtime_json_path(candidate_manifest_runtime_path, "evidence")
    report_runtime_path = _runtime_json_path(promotion_report_runtime_path, "evidence")
    attestation_runtime_path = _runtime_json_path(organizer_attestation_runtime_path, "evidence")
    temporary = output.with_name(f".{output.name}.{os.getpid()}.partial")
    if temporary.exists():
        raise RuntimeError("partial promoted Graph output already exists")
    temporary.mkdir(parents=True)
    try:
        for filename in GRAPH_FILES.values():
            shutil.copyfile(graph_output / filename, temporary / filename)
        shutil.copyfile(graph_output / "manifest.json", temporary / "candidate-manifest.json")
        shutil.copyfile(organizer_attestation_path, temporary / "organizer-attestation.json")
        promotion_report = {
            "schema_version": 1,
            "complete": True,
            "publication_allowed": True,
            "evaluation_split_sha256": report["split_manifest_sha256"],
            "baseline_run_sha256": report["graph_off_run_sha256"],
            "candidate_run_sha256": report["graph_on_run_sha256"],
            "primary_metric": "ndcg_at_10",
            "baseline_value": baseline_value,
            "candidate_value": candidate_value,
            "absolute_delta": absolute_delta,
        }
        atomic_json(temporary / "promotion-report.json", promotion_report)
        files = [
            {
                "path": f"{prefix}/{filename}",
                "sha256": sha256_file(temporary / filename),
                "size_bytes": (temporary / filename).stat().st_size,
            }
            for filename in sorted(GRAPH_FILES.values())
        ]
        component = {
            "complete": True,
            "publication_allowed": True,
            "schema_version": 1,
            "train_cutoff_exclusive": graph_manifest["train_cutoff_exclusive"],
            "max_source_timestamp": graph_manifest["max_source_timestamp"],
            "source_jd_sha256": graph_manifest["source_jd_sha256"],
            "source_policy": "train_jd_only",
            "test_jd_used": False,
            "candidate_manifest_sha256": candidate_manifest_sha256,
            "candidate_manifest_path": candidate_runtime_path,
            "source_ablation_report_sha256": ablation_report_sha256,
            "serving_algorithm": GRAPH_SERVING_ALGORITHM,
            "serving_policy_sha256": GRAPH_SERVING_POLICY_SHA256,
            "serving_implementation_sha256": GRAPH_SERVING_IMPLEMENTATION_SHA256,
            "evaluation_implementation_sha256": report["evaluation_implementation_sha256"],
            "promotion_report_path": report_runtime_path,
            "promotion_report_sha256": sha256_file(temporary / "promotion-report.json"),
            "organizer_attestation_path": attestation_runtime_path,
            "organizer_attestation_sha256": organizer_attestation_sha256,
            "files": files,
        }
        atomic_json(temporary / "manifest.json", component)
        temporary.replace(output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {
        "passed": True,
        "manifest_sha256": sha256_file(output / "manifest.json"),
        "promotion_report_sha256": sha256_file(output / "promotion-report.json"),
        "organizer_attestation_sha256": organizer_attestation_sha256,
    }


def _runtime_prefix(value: str, root: str) -> str:
    normalized = PurePosixPath(value).as_posix()
    if (
        value != normalized
        or not normalized.startswith(f"{root}/")
        or ".." in normalized.split("/")
    ):
        raise RuntimeError(f"runtime prefix must be a canonical {root}/ path")
    return normalized


def _runtime_json_path(value: str, root: str) -> str:
    normalized = _runtime_prefix(value, root)
    if not normalized.endswith(".json"):
        raise RuntimeError(f"runtime {root} path must end in .json")
    return normalized


def trace_skill(
    output: Path,
    split_manifest_path: Path,
    evidence_path: Path,
    extraction_manifest_path: Path,
    skill: str,
    limit: int,
) -> dict[str, object]:
    if not 1 <= limit <= 100:
        raise ValueError("trace limit must be between 1 and 100")
    validate_graph(
        output,
        split_manifest_path,
        evidence_path=evidence_path,
        extraction_manifest_path=extraction_manifest_path,
    )
    canonical = _normalize(skill, "trace skill")
    job_edges = _read_jsonl(output / GRAPH_FILES["job_skills"], "job_skills")
    duty_edges = _read_jsonl(output / GRAPH_FILES["duty_skills"], "duty_skills")
    relation_edges = _read_jsonl(output / GRAPH_FILES["skill_relations"], "skill_relations")
    evidence_edges = _read_jsonl(output / GRAPH_FILES["relation_evidence"], "relation_evidence")
    jobs = [row for row in job_edges if row["skill"] == canonical][:limit]
    duties = sorted(
        (row for row in duty_edges if row["skill"] == canonical),
        key=lambda row: (-cast(float, row["weight"]), str(row["duty"])),
    )[:limit]
    relations = sorted(
        (row for row in relation_edges if row["source"] == canonical or row["target"] == canonical),
        key=lambda row: (
            -cast(float, row["weight"]),
            str(row["source"]),
            str(row["type"]),
            str(row["target"]),
        ),
    )[:limit]
    relation_evidence = {
        (str(row["source"]), str(row["type"]), str(row["target"])): [
            {"job_id": evidence["job_id"], "evidence_span": evidence["evidence_span"]}
            for evidence in evidence_edges
            if evidence["source"] == row["source"]
            and evidence["type"] == row["type"]
            and evidence["target"] == row["target"]
        ][:limit]
        for row in relations
    }
    paths = []
    for row in relations:
        related_skill = str(row["target"] if row["source"] == canonical else row["source"])
        related_jobs = [
            {
                "job_id": edge["job_id"],
                "surface": edge["surface"],
                "evidence_span": edge["evidence_span"],
            }
            for edge in job_edges
            if edge["skill"] == related_skill
        ][:limit]
        paths.append(
            {
                "anchor_skill": canonical,
                "edge": dict(row),
                "related_skill": related_skill,
                "related_jobs": related_jobs,
                "relation_evidence": relation_evidence[
                    (str(row["source"]), str(row["type"]), str(row["target"]))
                ],
            }
        )
    return {
        "anchor": canonical,
        "maximum_hops": 1,
        "jobs": [{"job_id": row["job_id"], "evidence_span": row["evidence_span"]} for row in jobs],
        "duties": [
            {"duty": row["duty"], "support": row["support"], "weight": row["weight"]}
            for row in duties
        ],
        "relations": [
            {
                "source": row["source"],
                "type": row["type"],
                "target": row["target"],
                "support": row["support"],
                "weight": row["weight"],
                "evidence": relation_evidence[
                    (str(row["source"]), str(row["type"]), str(row["target"]))
                ],
            }
            for row in relations
        ],
        "paths": paths,
    }


def _graph_s3(
    output: Path,
    split_manifest_path: Path,
    evidence_path: Path,
    extraction_manifest_path: Path,
    *,
    bucket: str,
    prefix: str,
    expected_owner: str,
    profile: str | None,
    region: str,
    publish: bool,
) -> dict[str, object]:
    validation = validate_graph(
        output,
        split_manifest_path,
        evidence_path=evidence_path,
        extraction_manifest_path=extraction_manifest_path,
    )
    if region != "us-west-2":
        raise RuntimeError("skill graph S3 publication is pinned to us-west-2")
    session = boto3.Session(profile_name=profile, region_name=region)
    identity = session.client("sts").get_caller_identity()
    if identity.get("Account") != expected_owner:
        raise RuntimeError("AWS caller identity differs from expected S3 owner")
    manifest_path = output / "manifest.json"
    manifest_sha256 = sha256_file(manifest_path)
    manifest = read_json_object(manifest_path, "skill graph manifest")
    clean_prefix = prefix.strip("/")
    if not clean_prefix or clean_prefix.rsplit("/", 1)[-1] != manifest_sha256:
        raise RuntimeError("S3 prefix must end with the manifest SHA-256")
    s3 = session.client("s3")
    if publish:
        return publish_s3_directory(
            root=output,
            bucket=bucket,
            prefix=prefix,
            expected_owner=expected_owner,
            artifacts=manifest["artifacts"],
            s3=s3,
        )
    artifacts = verify_local_inventory(output, manifest["artifacts"])
    verify_s3_inventory(
        bucket=bucket,
        prefix=prefix,
        expected_owner=expected_owner,
        artifacts=artifacts,
        s3=s3,
    )
    verify_s3_object(
        bucket=bucket,
        key=f"{clean_prefix}/manifest.json",
        expected_owner=expected_owner,
        expected_sha256=manifest_sha256,
        expected_size=manifest_path.stat().st_size,
        s3=s3,
    )
    return {
        "passed": True,
        "graph_sha256": validation["graph_sha256"],
        "manifest_sha256": manifest_sha256,
        "s3_prefix": f"s3://{bucket}/{clean_prefix}/",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--evidence", type=Path, required=True)
    build.add_argument("--extraction-manifest", type=Path, required=True)
    build.add_argument("--split-manifest", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--minimum-support", type=int, default=20)
    verify = commands.add_parser("validate")
    verify.add_argument("--output", type=Path, required=True)
    verify.add_argument("--split-manifest", type=Path, required=True)
    verify.add_argument("--evidence", type=Path, required=True)
    verify.add_argument("--extraction-manifest", type=Path, required=True)
    trace = commands.add_parser("trace")
    trace.add_argument("--output", type=Path, required=True)
    trace.add_argument("--split-manifest", type=Path, required=True)
    trace.add_argument("--evidence", type=Path, required=True)
    trace.add_argument("--extraction-manifest", type=Path, required=True)
    trace.add_argument("--skill", required=True)
    trace.add_argument("--limit", type=int, default=20)
    approve = commands.add_parser("approve")
    approve.add_argument("--graph-output", type=Path, required=True)
    approve.add_argument("--split-manifest", type=Path, required=True)
    approve.add_argument("--evidence", type=Path, required=True)
    approve.add_argument("--extraction-manifest", type=Path, required=True)
    approve.add_argument("--ablation-report", type=Path, required=True)
    approve.add_argument("--ablation-report-sha256", required=True)
    approve.add_argument("--organizer-attestation", type=Path, required=True)
    approve.add_argument("--organizer-attestation-sha256", required=True)
    approve.add_argument("--runtime-prefix", required=True)
    approve.add_argument("--candidate-manifest-runtime-path", required=True)
    approve.add_argument("--promotion-report-runtime-path", required=True)
    approve.add_argument("--organizer-attestation-runtime-path", required=True)
    approve.add_argument("--output", type=Path, required=True)
    for name in ("publish-s3", "verify-s3"):
        command = commands.add_parser(name)
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--split-manifest", type=Path, required=True)
        command.add_argument("--evidence", type=Path, required=True)
        command.add_argument("--extraction-manifest", type=Path, required=True)
        command.add_argument("--bucket", required=True)
        command.add_argument("--prefix", required=True)
        command.add_argument("--expected-owner", default="378849533305")
        command.add_argument("--profile")
        command.add_argument("--region", default="us-west-2")
    args = parser.parse_args()
    if args.command == "build":
        result = build_graph(
            evidence_path=args.evidence,
            extraction_manifest_path=args.extraction_manifest,
            split_manifest_path=args.split_manifest,
            output=args.output,
            minimum_support=args.minimum_support,
        )
    elif args.command == "validate":
        result = validate_graph(
            args.output,
            args.split_manifest,
            evidence_path=args.evidence,
            extraction_manifest_path=args.extraction_manifest,
        )
    elif args.command == "trace":
        result = trace_skill(
            args.output,
            args.split_manifest,
            args.evidence,
            args.extraction_manifest,
            args.skill,
            args.limit,
        )
    elif args.command == "approve":
        result = approve_graph(
            graph_output=args.graph_output,
            split_manifest_path=args.split_manifest,
            evidence_path=args.evidence,
            extraction_manifest_path=args.extraction_manifest,
            ablation_report_path=args.ablation_report,
            ablation_report_sha256=args.ablation_report_sha256,
            organizer_attestation_path=args.organizer_attestation,
            organizer_attestation_sha256=args.organizer_attestation_sha256,
            runtime_prefix=args.runtime_prefix,
            candidate_manifest_runtime_path=args.candidate_manifest_runtime_path,
            promotion_report_runtime_path=args.promotion_report_runtime_path,
            organizer_attestation_runtime_path=args.organizer_attestation_runtime_path,
            output=args.output,
        )
    else:
        result = _graph_s3(
            args.output,
            args.split_manifest,
            args.evidence,
            args.extraction_manifest,
            bucket=args.bucket,
            prefix=args.prefix,
            expected_owner=args.expected_owner,
            profile=args.profile,
            region=args.region,
            publish=args.command == "publish-s3",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
