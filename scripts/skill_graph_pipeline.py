#!/usr/bin/env python3
"""Build, validate, and trace a leakage-safe LLM evidence skill graph."""

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
from pathlib import Path
from typing import Any

from pipeline_contract import (
    artifact_entry,
    atomic_json,
    exact_keys,
    read_json_object,
    require_sha256,
    sha256_file,
    verify_local_inventory,
)

TRAIN_CUTOFF = datetime.fromisoformat("2026-06-08T00:00:00+08:00")
RELATION_TYPES = {
    "ALTERNATIVE_TO",
    "PREREQUISITE_OF",
    "REQUIRES",
    "SPECIALIZATION_OF",
    "USED_WITH",
}
EXTRACTION_MANIFEST_KEYS = {
    "schema_version",
    "complete",
    "model_id",
    "prompt_version",
    "source_policy",
    "test_jd_used",
    "uses_ground_truth",
    "uses_behavior_logs",
    "train_cutoff_exclusive",
    "max_source_timestamp",
    "source_jd_sha256",
    "evidence_sha256",
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
}
GRAPH_FILES = {
    "jobs": "jobs.jsonl",
    "skills": "skills.jsonl",
    "job_skills": "job-skills.jsonl",
    "duty_skills": "duty-skills.jsonl",
    "skill_relations": "skill-relations.jsonl",
    "relation_evidence": "relation-evidence.jsonl",
}


def _atomic_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    partial = path.with_name(f".{path.name}.{os.getpid()}.partial")
    if partial.exists():
        raise RuntimeError(f"partial output already exists: {partial}")
    try:
        with partial.open("wb") as output:
            for value in values:
                output.write(
                    json.dumps(
                        value,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode()
                    + b"\n"
                )
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
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise RuntimeError(f"{name} line {line_number} must be an object")
                values.append(value)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{name} cannot be read as UTF-8 JSONL") from error
    return values


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


def _load_extraction(
    evidence_path: Path, manifest_path: Path
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
        or manifest["train_cutoff_exclusive"] != TRAIN_CUTOFF.isoformat()
    ):
        raise RuntimeError("LLM extraction leakage policy is incompatible")
    for name in ("source_jd_sha256", "evidence_sha256"):
        require_sha256(manifest[name], name)
    if sha256_file(evidence_path) != manifest["evidence_sha256"]:
        raise RuntimeError("LLM evidence bytes differ from extraction manifest")
    maximum = _timestamp(manifest["max_source_timestamp"], "maximum source timestamp")
    if maximum >= TRAIN_CUTOFF:
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
            record_id = _normalize(raw["record_id"], "record_id")
            job_id = raw["job_id"]
            if not isinstance(job_id, str) or not job_id.isascii() or not job_id.isdecimal():
                raise RuntimeError("evidence has an invalid job_id")
            if record_id in seen_records or job_id in seen_jobs:
                raise RuntimeError("evidence has a duplicate record or job_id")
            seen_records.add(record_id)
            seen_jobs.add(job_id)
            modified_at = _timestamp(raw["source_modified_at"], "source_modified_at")
            if modified_at >= TRAIN_CUTOFF or modified_at > maximum:
                raise RuntimeError("evidence row exceeds the train-only time boundary")
            observed_maximum = (
                max(observed_maximum, modified_at) if observed_maximum else modified_at
            )
            source_text = raw["source_text"]
            if not isinstance(source_text, str) or not source_text.strip():
                raise RuntimeError("evidence source_text is empty")
            expected_source_sha = require_sha256(raw["source_text_sha256"], "source_text_sha256")
            if hashlib.sha256(source_text.encode()).hexdigest() != expected_source_sha:
                raise RuntimeError("evidence source_text SHA-256 differs")
            skills = _skills(raw["skills"], source_text)
            relations = _relations(raw["relations"], source_text, set(skills))
            records.append(
                {
                    "record_id": record_id,
                    "job_id": job_id,
                    "duty": _normalize(raw["duty"], "duty"),
                    "source_modified_at": modified_at.isoformat(),
                    "source_text_sha256": expected_source_sha,
                    "skills": skills,
                    "relations": relations,
                }
            )
    if not records or observed_maximum != maximum:
        raise RuntimeError("evidence is empty or maximum source timestamp differs")
    return manifest, records


def _skills(value: object, source_text: str) -> dict[str, dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise RuntimeError("evidence skills must be a non-empty array")
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
        surface = _normalize(raw["surface"], "skill surface")
        category = _normalize(raw["category"], "skill category")
        span = raw["evidence_span"]
        if not isinstance(span, str) or not span or span not in source_text or skill in parsed:
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


def build_graph(
    *,
    evidence_path: Path,
    extraction_manifest_path: Path,
    output: Path,
    minimum_support: int,
) -> dict[str, object]:
    if minimum_support < 1:
        raise ValueError("minimum_support must be positive")
    extraction, records = _load_extraction(evidence_path, extraction_manifest_path)
    if output.exists():
        raise RuntimeError("graph output already exists; builds never overwrite artifacts")
    output.parent.mkdir(parents=True, exist_ok=True)
    skill_support = Counter(skill for record in records for skill in set(record["skills"]))
    promoted = {skill for skill, count in skill_support.items() if count >= minimum_support}
    if not promoted:
        raise RuntimeError("minimum_support removed every skill")
    categories: dict[str, str] = {}
    for record in records:
        for skill, evidence in record["skills"].items():
            if skill in promoted:
                category = evidence["category"]
                if skill in categories and categories[skill] != category:
                    raise RuntimeError("one canonical skill has conflicting categories")
                categories[skill] = category
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
        "graph_schema_version": 1,
        "graph_kind": "llm-evidence-locked-typed-entity-graph",
        "source_policy": "train_jd_only",
        "test_jd_used": False,
        "uses_ground_truth": False,
        "uses_behavior_logs": False,
        "train_cutoff_exclusive": TRAIN_CUTOFF.isoformat(),
        "max_source_timestamp": extraction["max_source_timestamp"],
        "source_jd_sha256": extraction["source_jd_sha256"],
        "evidence_sha256": extraction["evidence_sha256"],
        "model_id": extraction["model_id"],
        "prompt_version": extraction["prompt_version"],
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
        validate_graph(build_root)
        build_root.replace(output)
    except BaseException:
        shutil.rmtree(build_root, ignore_errors=True)
        raise
    return report


def validate_graph(output: Path) -> dict[str, object]:
    manifest = read_json_object(output / "manifest.json", "skill graph manifest")
    expected_keys = {
        "schema_version",
        "complete",
        "graph_schema_version",
        "graph_kind",
        "source_policy",
        "test_jd_used",
        "uses_ground_truth",
        "uses_behavior_logs",
        "train_cutoff_exclusive",
        "max_source_timestamp",
        "source_jd_sha256",
        "evidence_sha256",
        "model_id",
        "prompt_version",
        "minimum_support",
        "maximum_traversal_hops",
        "counts",
        "artifacts",
    }
    exact_keys(manifest, expected_keys, "skill graph manifest")
    if (
        manifest["schema_version"] != 1
        or manifest["complete"] is not True
        or manifest["source_policy"] != "train_jd_only"
        or manifest["test_jd_used"] is not False
        or manifest["uses_ground_truth"] is not False
        or manifest["uses_behavior_logs"] is not False
        or manifest["train_cutoff_exclusive"] != TRAIN_CUTOFF.isoformat()
        or _timestamp(manifest["max_source_timestamp"], "max_source_timestamp") >= TRAIN_CUTOFF
        or manifest["maximum_traversal_hops"] != 1
    ):
        raise RuntimeError("skill graph manifest policy differs")
    artifacts = verify_local_inventory(output, manifest["artifacts"])
    if {artifact["path"] for artifact in artifacts} != set(GRAPH_FILES.values()):
        raise RuntimeError("skill graph artifact inventory differs from the closed schema")
    tables = {name: _read_jsonl(output / filename, name) for name, filename in GRAPH_FILES.items()}
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
    job_ids = [str(row["job_id"]) for row in tables["jobs"]]
    skills = [str(row["skill"]) for row in tables["skills"]]
    relations = {
        (str(row["source"]), str(row["type"]), str(row["target"]))
        for row in tables["skill_relations"]
    }
    if job_ids != sorted(set(job_ids)) or skills != sorted(set(skills)):
        raise RuntimeError("skill graph entities are duplicated or not canonical")
    if any(
        str(row["job_id"]) not in job_ids or str(row["skill"]) not in skills
        for row in tables["job_skills"]
    ) or any(str(row["skill"]) not in skills for row in tables["duty_skills"]):
        raise RuntimeError("skill graph edge references an unknown entity")
    if any(
        str(row["source"]) not in skills
        or str(row["target"]) not in skills
        or str(row["type"]) not in RELATION_TYPES
        for row in tables["skill_relations"]
    ):
        raise RuntimeError("skill relation references an unknown entity or type")
    if any(
        str(row["job_id"]) not in job_ids
        or (str(row["source"]), str(row["type"]), str(row["target"])) not in relations
        for row in tables["relation_evidence"]
    ):
        raise RuntimeError("relation evidence references an unknown relation")
    counts = {name: len(rows) for name, rows in tables.items()}
    if counts != manifest["counts"]:
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


def trace_skill(output: Path, skill: str, limit: int) -> dict[str, object]:
    if not 1 <= limit <= 100:
        raise ValueError("trace limit must be between 1 and 100")
    validate_graph(output)
    canonical = _normalize(skill, "trace skill")
    job_edges = _read_jsonl(output / GRAPH_FILES["job_skills"], "job_skills")
    duty_edges = _read_jsonl(output / GRAPH_FILES["duty_skills"], "duty_skills")
    relation_edges = _read_jsonl(output / GRAPH_FILES["skill_relations"], "skill_relations")
    evidence_edges = _read_jsonl(output / GRAPH_FILES["relation_evidence"], "relation_evidence")
    jobs = [row for row in job_edges if row["skill"] == canonical][:limit]
    duties = sorted(
        (row for row in duty_edges if row["skill"] == canonical),
        key=lambda row: (-float(row["weight"]), str(row["duty"])),
    )[:limit]
    relations = sorted(
        (row for row in relation_edges if row["source"] == canonical or row["target"] == canonical),
        key=lambda row: (
            -float(row["weight"]),
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
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--evidence", type=Path, required=True)
    build.add_argument("--extraction-manifest", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--minimum-support", type=int, default=20)
    verify = commands.add_parser("validate")
    verify.add_argument("--output", type=Path, required=True)
    trace = commands.add_parser("trace")
    trace.add_argument("--output", type=Path, required=True)
    trace.add_argument("--skill", required=True)
    trace.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if args.command == "build":
        result = build_graph(
            evidence_path=args.evidence,
            extraction_manifest_path=args.extraction_manifest,
            output=args.output,
            minimum_support=args.minimum_support,
        )
    elif args.command == "validate":
        result = validate_graph(args.output)
    else:
        result = trace_skill(args.output, args.skill, args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
