#!/usr/bin/env python3
"""Promote one verified, immutable retrieval release to the runtime bucket."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import re
import subprocess
import tempfile
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from time import sleep
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from work_retrieval_core.graph_policy import (
    GRAPH_SERVING_ALGORITHM,
    GRAPH_SERVING_IMPLEMENTATION_SHA256,
    GRAPH_SERVING_POLICY_SHA256,
)
from work_retrieval_core.manifest import semantic_reranker_manifest

AWS_ACCOUNT = "378849533305"
AWS_PROFILE = "competition"
AWS_REGION = "us-west-2"
SOURCE_BUCKET = "jobbank-data-bucket"
DESTINATION_BUCKET = "workretrievaldata-runtimebucket404c5ee4-hkvrjx5fbkij"

# Sealed, independently verified EVA whole-job cache. Production derives MRL1024 shards from
# these immutable 4096d bytes and never rebuilds or overwrites the source cache.
APPROVED_WHOLE_SOURCE_MANIFEST_SHA256 = (
    "a02a23655fe8e5cc6b08afde35e93898ff94c62b88bbf7522e09f2c15378715c"
)
APPROVED_WHOLE_SOURCE_INVENTORY_SHA256 = (
    "f762cc4d676e16aa04789e1573713ef30d66e72f3a7f96c5bcd7e7e6133a2adb"
)
APPROVED_WHOLE_SOURCE_FILE_COUNT = 367
APPROVED_WHOLE_SOURCE_BYTES = 10_001_032_323
APPROVED_WHOLE_SOURCE_ROWS = 1_218_635
APPROVED_WHOLE_SOURCE_SHARDS = 122
APPROVED_JOBS_DATASET_SHA256 = "53937f7bf076789c4cd7e3be34fb89875336108d57707b5a93182181e1087089"
WHOLE_RUNTIME_PREFIX = "embeddings/qwen3-embedding-8b-clean-v1-mrl1024"
APPROVED_WHOLE_SOURCE_MANIFEST_PATH = f"{WHOLE_RUNTIME_PREFIX}/source-manifest.json"
APPROVED_WHOLE_SOURCE_INVENTORY_PATH = f"{WHOLE_RUNTIME_PREFIX}/source-inventory.json"
WHOLE_SOURCE_MANIFEST_SOURCE_PATH = "provenance/qwen3-embedding-8b-clean-v1/source-manifest.json"
WHOLE_SOURCE_INVENTORY_SOURCE_PATH = "provenance/qwen3-embedding-8b-clean-v1/source-inventory.json"
APPROVED_TANTIVY_BUILD_MANIFEST_SHA256: str | None = None
APPROVED_TANTIVY_INDEX_SHA256: str | None = None
TANTIVY_RUNTIME_PREFIX = "indexes/tantivy-bm25-temporal-v3"
APPROVED_TANTIVY_BUILD_PROVENANCE_PATH = f"{TANTIVY_RUNTIME_PREFIX}/build-manifest.json"
TANTIVY_BUILD_PROVENANCE_SOURCE_PATH = "provenance/tantivy-bm25-temporal-v3/build-manifest.json"
MATERIALIZATION_REPORT_PATH = "evidence/provenance/materialization-report.json"
TANTIVY_JOB_IDS_RUNTIME_PATH = f"{TANTIVY_RUNTIME_PREFIX}/job-ids.json"
APPROVED_MODEL = "Qwen/Qwen3-Embedding-8B"
APPROVED_MODEL_REVISION = "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"
APPROVED_SOURCE_EMBEDDING_DIMENSION = 4096
APPROVED_WHOLE_DIMENSION = 1024
APPROVED_WHOLE_PROJECTION = "mrl_prefix_then_l2_normalize"
APPROVED_DOCUMENT_POLICY_VERSION = "2026-07-24-clean-v1"
APPROVED_MULTIVIEW_DIMENSION = 1024
APPROVED_MULTIVIEW_REFERENCE_DIMENSION = 4096
APPROVED_MULTIVIEW_KINDS = ["occupation", "skill", "requirement", "content"]
APPROVED_DOCUMENT_FIELDS = [
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
]
APPROVED_DOCUMENT_TEMPLATE_SHA256 = (
    "3275f93ade6c4f043084e36303d38b33443858546a80104840f0e2b9468d2abb"
)
APPROVED_QUERY_PROMPT = (
    "Instruct: Given a job search query, retrieve relevant job postings matching "
    "the user's intent\nQuery: "
)
APPROVED_TANTIVY_SCHEMA_FIELDS = [
    "title",
    "duty",
    "skills",
    "industry",
    "body",
    "location_filter",
    "duty_filter",
    "visibility_filter",
    "education_filter",
    "job_attribute_filter",
    "work_shift_filter",
    "experience_filter",
    "management_filter",
    "updated_at_epoch_ms",
    "monthly_salary_lower_filter",
    "monthly_salary_recall_filter",
    "job_index",
]
APPROVED_TANTIVY_ENGINE = "tantivy v0.26.0, index_format v7"
APPROVED_TANTIVY_FIELD_BOOSTS = {
    "title": 15.0,
    "duty": 8.0,
    "skills": 6.0,
    "industry": 1.0,
    "body": 0.5,
}
APPROVED_LEXICAL_POLICY_VERSION = "2026-08-02-pretokenized-v3"
APPROVED_LEXICAL_POLICY_SHA256 = "adf196a92c2da9cf54b6d12cd878371f000503140df69ef69615f1171e2e7ae8"
APPROVED_TANTIVY_TOKENIZERS = {
    "title": "default",
    "duty": "default",
    "skills": "default",
    "industry": "default",
    "body": "default",
    "location_filter": "raw",
    "duty_filter": "raw",
    "visibility_filter": "raw",
    "education_filter": "raw",
    "job_attribute_filter": "raw",
    "work_shift_filter": "raw",
    "experience_filter": "raw",
    "management_filter": "raw",
}
APPROVED_TANTIVY_SOURCE_FIELDS = {
    "title": ["title"],
    "duty": ["duty_minor", "duty_middle", "duty_major"],
    "skills": ["computer_skills", "work_skills", "professional_certifications"],
    "industry": ["industry_minor", "industry_middle", "industry_major"],
    "body": [
        "salary_text",
        "job_attribute",
        "work_hours",
        "work_hours_description",
        "experience_requirement",
        "education_requirement",
        "major_requirement_1",
        "major_requirement_2",
        "major_requirement_3",
        "language_1",
        "language_1_listening",
        "language_1_speaking",
        "language_1_reading",
        "language_1_writing",
        "language_2",
        "language_2_listening",
        "language_2_speaking",
        "language_2_reading",
        "language_2_writing",
        "management_count",
        "requires_travel",
        "work_city",
        "additional_conditions",
        "description",
    ],
}
DEMO_AS_OF = "2026-06-08T23:59:59.999+08:00"
APPROVED_GRAPH_TRAIN_CUTOFF = "2026-06-08T00:00:00+08:00"
APPROVED_GRAPH_MAX_SOURCE_TIMESTAMP = "2026-06-07T23:51:07.143000+08:00"
APPROVED_GRAPH_SOURCE_JD_SHA256 = "53937f7bf076789c4cd7e3be34fb89875336108d57707b5a93182181e1087089"
GRAPH_SERVING_FILES = {
    "jobs.jsonl",
    "skills.jsonl",
    "job-skills.jsonl",
    "duty-skills.jsonl",
    "skill-relations.jsonl",
    "relation-evidence.jsonl",
}
TEMPORAL_FILTER_SEMANTICS = (
    "updated_at >= as_of - 180 days before Top-K; future snapshots retained with freshness 0"
)
TANTIVY_FILTER_SEMANTICS = (
    "visibility AND (location OR) AND (duty OR) AND optional education "
    "AND optional monthly salary/job attribute/work shift/no-experience/management, "
    "applied before Top-K"
)
ARTIFACT_ROOTS = {
    "embedding": "embeddings",
    "model": "models",
    "index": "indexes",
    "graph": "graphs",
    "ranker": "rankers",
    "evidence": "evidence",
}
CHALLENGERS = {
    "multiview_embedding",
    "skill_graph",
    "semantic_reranker",
    "learning_to_rank",
    "guardrails",
}
FORBIDDEN_PATH_PARTS = {
    "credential",
    "credentials",
    "ground-truth",
    "ground_truth",
    "gt",
    "judgment",
    "judgments",
    "log",
    "logs",
    "qrel",
    "qrels",
    "query-history",
    "query_history",
    "raw-log",
    "raw-logs",
    "raw-search-logs",
    "raw_log",
    "raw_logs",
    "secret",
    "secrets",
    "test-jd",
    "test_jd",
}
HEX = frozenset("0123456789abcdef")
RUNTIME_SCHEMA = (
    Path(__file__).parents[1] / "packages" / "contract" / "runtime-manifest.schema.json"
)


class AwsError(RuntimeError):
    def __init__(self, message: str, stderr: str) -> None:
        self.stderr = stderr
        detail = " ".join(stderr.split())[:2_000]
        super().__init__(f"{message}: {detail}" if detail else message)


def _run(arguments: list[str], *, text: bool = True) -> subprocess.CompletedProcess[Any]:
    command = [
        "aws",
        *arguments,
        "--profile",
        AWS_PROFILE,
        "--region",
        AWS_REGION,
        "--no-cli-pager",
    ]
    retryable = (
        "Connection was closed",
        "Connection reset",
        "Could not connect to the endpoint",
        "InternalError",
        "Read timeout",
        "RequestTimeout",
        "SlowDown",
    )
    for attempt in range(3):
        result = subprocess.run(command, check=False, capture_output=True, text=text)
        if not result.returncode:
            return result
        stderr = result.stderr if isinstance(result.stderr, str) else result.stderr.decode()
        if attempt == 2 or not any(marker in stderr for marker in retryable):
            raise AwsError(f"AWS CLI command failed: {' '.join(command[:3])}", stderr)
        sleep(2**attempt)
    raise AssertionError("unreachable")


def aws(arguments: list[str]) -> dict[str, object]:
    result = _run([*arguments, "--output", "json"])
    if not result.stdout.strip():
        return {}
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("AWS CLI returned an unexpected JSON shape")
    return cast(dict[str, object], value)


def verify_account() -> None:
    identity = aws(["sts", "get-caller-identity"])
    if identity.get("Account") != AWS_ACCOUNT:
        raise RuntimeError(f"AWS caller must be account {AWS_ACCOUNT}")
    region = _run(
        [
            "ec2",
            "describe-availability-zones",
            "--query",
            "AvailabilityZones[0].RegionName",
            "--output",
            "text",
        ]
    ).stdout.strip()
    if region != AWS_REGION:
        raise RuntimeError(f"AWS command region must be {AWS_REGION}, got {region}")


def _require_sha256(name: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in HEX for character in value)
    ):
        raise RuntimeError(f"{name} must be 64 lowercase hexadecimal characters")
    return value


def _reject_nonfinite_json(value: str) -> object:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def validate_relative_path(path: str, kind: str | None = None) -> None:
    candidate = PurePosixPath(path)
    raw_parts = path.split("/")
    root = ARTIFACT_ROOTS.get(kind) if kind is not None else None
    if (
        not path
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in raw_parts)
        or candidate.parts[0] not in set(ARTIFACT_ROOTS.values())
        or (kind != "evidence" and root is not None and candidate.parts[0] != root)
        or (kind == "evidence" and not path.endswith(".json"))
    ):
        raise RuntimeError(f"unsafe runtime artifact path: {path!r}")


def _validate_source_path(path: str) -> None:
    parts = path.split("/")
    if not path or path.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise RuntimeError(f"unsafe source artifact path: {path!r}")


def canonical_bytes(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def destination_key(manifest_sha: str, path: str) -> str:
    _require_sha256("manifest SHA-256", manifest_sha)
    validate_relative_path(path)
    return f"runtime/{manifest_sha}/{path}"


def _parse_source_manifest(source: Mapping[str, object]) -> dict[str, dict[str, object]]:
    files = source.get("files")
    if source.get("schema_version") != 3 or not isinstance(files, list):
        raise RuntimeError("source manifest does not satisfy the verified v3 contract")
    inventory: dict[str, dict[str, object]] = {}
    for raw in files:
        if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
            raise RuntimeError("source manifest contains an invalid file entry")
        path = raw["path"]
        _validate_source_path(path)
        sha256 = _require_sha256("source object SHA-256", raw.get("sha256"))
        size = raw.get("size")
        if type(size) is not int or size < 0:
            raise RuntimeError(f"invalid source object size: {path}")
        if path in inventory:
            raise RuntimeError(f"duplicate source object: {path}")
        inventory[path] = {"sha256": sha256, "size": size}
    return inventory


def _selection_rules(spec: Mapping[str, object]) -> list[dict[str, str]]:
    raw_rules = spec.get("selections")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise RuntimeError("release spec requires non-empty selections")
    rules: list[dict[str, str]] = []
    for raw in raw_rules:
        if not isinstance(raw, dict) or set(raw) != {
            "source_prefix",
            "destination_prefix",
            "kind",
        }:
            raise RuntimeError("invalid release selection rule")
        source_prefix = raw["source_prefix"]
        destination_prefix = raw["destination_prefix"]
        kind = raw["kind"]
        if not all(isinstance(value, str) for value in raw.values()):
            raise RuntimeError("release selection values must be strings")
        if not source_prefix.endswith("/") or not destination_prefix.endswith("/"):
            raise RuntimeError("release selection prefixes must end with slash")
        _validate_source_path(source_prefix.removesuffix("/"))
        if kind not in ARTIFACT_ROOTS:
            raise RuntimeError(f"unsupported artifact kind: {kind}")
        placeholder = "placeholder.json" if kind == "evidence" else "placeholder"
        validate_relative_path(f"{destination_prefix}{placeholder}", kind)
        rules.append(cast(dict[str, str], raw))
    for index, first in enumerate(rules):
        for second in rules[index + 1 :]:
            if first["source_prefix"].startswith(second["source_prefix"]) or second[
                "source_prefix"
            ].startswith(first["source_prefix"]):
                raise RuntimeError("release selection source prefixes overlap")
    return rules


def select_artifacts(
    source: Mapping[str, object], spec: Mapping[str, object]
) -> list[dict[str, object]]:
    inventory = _parse_source_manifest(source)
    if APPROVED_TANTIVY_BUILD_MANIFEST_SHA256 is None or APPROVED_TANTIVY_INDEX_SHA256 is None:
        raise RuntimeError("approved temporal-v3 Tantivy build lineage is not configured")
    required_provenance = {
        WHOLE_SOURCE_MANIFEST_SOURCE_PATH: (
            APPROVED_WHOLE_SOURCE_MANIFEST_SHA256,
            "approved sealed whole source manifest",
        ),
        WHOLE_SOURCE_INVENTORY_SOURCE_PATH: (
            APPROVED_WHOLE_SOURCE_INVENTORY_SHA256,
            "approved sealed whole source inventory",
        ),
        TANTIVY_BUILD_PROVENANCE_SOURCE_PATH: (
            APPROVED_TANTIVY_BUILD_MANIFEST_SHA256,
            "approved Tantivy build manifest",
        ),
    }
    for path, (expected_sha256, label) in required_provenance.items():
        provenance = inventory.get(path)
        if not isinstance(provenance, dict) or provenance.get("sha256") != expected_sha256:
            raise RuntimeError(f"source inventory does not pin the {label}")
    rules = _selection_rules(spec)
    selected: list[dict[str, object]] = []
    matched_rules: set[int] = set()
    for source_path, metadata in inventory.items():
        matches = [
            (index, rule)
            for index, rule in enumerate(rules)
            if source_path.startswith(rule["source_prefix"])
        ]
        if not matches:
            continue
        if len(matches) != 1:
            raise RuntimeError(f"source object matches multiple selections: {source_path}")
        index, rule = matches[0]
        suffix = source_path.removeprefix(rule["source_prefix"])
        if not suffix:
            raise RuntimeError(f"selection matched an empty artifact suffix: {source_path}")
        path = f"{rule['destination_prefix']}{suffix}"
        validate_relative_path(path, rule["kind"])
        selected.append(
            {
                "source_path": source_path,
                "path": path,
                "kind": rule["kind"],
                "sha256": metadata["sha256"],
                "size_bytes": metadata["size"],
            }
        )
        matched_rules.add(index)
    if matched_rules != set(range(len(rules))):
        missing = sorted(set(range(len(rules))) - matched_rules)
        raise RuntimeError(f"release selections matched no source objects: {missing}")
    selected.sort(key=lambda item: cast(str, item["path"]))
    if len({item["path"] for item in selected}) != len(selected):
        raise RuntimeError("selected artifact inventory contains duplicate destination paths")
    expected = _require_sha256("selected_inventory_sha256", spec.get("selected_inventory_sha256"))
    if _canonical_sha256(selected) != expected:
        raise RuntimeError("selected artifact inventory does not match the release spec")
    return selected


def _artifact_reference(
    artifacts: Mapping[str, object], component: Mapping[str, object], kind: str
) -> str:
    path = component.get("manifest_path")
    expected_sha = component.get("manifest_sha256")
    if not isinstance(path, str):
        raise RuntimeError("component manifest path is missing")
    validate_relative_path(path, kind)
    artifact = artifacts.get(path)
    if not isinstance(artifact, dict) or artifact.get("kind") != kind:
        raise RuntimeError(f"component manifest is not in the selected inventory: {path}")
    if artifact.get("sha256") != expected_sha:
        raise RuntimeError(f"component manifest checksum differs: {path}")
    return path


def _json_document(
    path: str,
    artifacts: Mapping[str, object],
    documents: Mapping[str, bytes],
) -> dict[str, object]:
    raw = documents.get(path)
    artifact = artifacts.get(path)
    if raw is None or not isinstance(artifact, dict):
        raise RuntimeError(f"missing component manifest body: {path}")
    if len(raw) != artifact.get("size_bytes") or hashlib.sha256(raw).hexdigest() != artifact.get(
        "sha256"
    ):
        raise RuntimeError(f"component manifest body differs: {path}")
    try:
        value = json.loads(raw, parse_constant=_reject_nonfinite_json)
    except (json.JSONDecodeError, ValueError) as error:
        raise RuntimeError(f"component manifest is invalid JSON: {path}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"component manifest is not an object: {path}")
    _forbid_sensitive_fields(value)
    return cast(dict[str, object], value)


def _require_equal(
    component: str,
    expected: Mapping[str, object],
    actual: Mapping[str, object],
) -> None:
    mismatches = [key for key, value in expected.items() if actual.get(key) != value]
    if mismatches:
        raise RuntimeError(f"{component} component manifest differs in: {', '.join(mismatches)}")


def _require_exact_keys(component: str, value: Mapping[str, object], expected: set[str]) -> None:
    if set(value) != expected:
        raise RuntimeError(f"{component} component manifest schema differs")


def _inventory_entries(
    component: str,
    document: Mapping[str, object],
    artifacts: Mapping[str, object],
    *,
    field: str = "files",
    kinds: set[str],
) -> set[str]:
    raw_entries = document.get(field)
    if not isinstance(raw_entries, list) or not raw_entries:
        raise RuntimeError(f"{component} component inventory is missing or empty")
    paths: set[str] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict) or set(raw) != {"path", "sha256", "size_bytes"}:
            raise RuntimeError(f"{component} component inventory entry differs")
        path = raw.get("path")
        if not isinstance(path, str) or path in paths:
            raise RuntimeError(f"{component} component inventory path is invalid or repeated")
        artifact = artifacts.get(path)
        if not isinstance(artifact, dict) or artifact.get("kind") not in kinds:
            raise RuntimeError(f"{component} component inventory is unreachable: {path}")
        if raw.get("sha256") != artifact.get("sha256") or raw.get("size_bytes") != artifact.get(
            "size_bytes"
        ):
            raise RuntimeError(f"{component} component inventory SHA/size differs: {path}")
        paths.add(path)
    return paths


def _component_file(
    component: str,
    path: object,
    artifacts: Mapping[str, object],
    *,
    kind: str,
    prefix: str,
) -> str:
    if not isinstance(path, str) or not path.startswith(prefix):
        raise RuntimeError(f"{component} file is outside its runtime component directory")
    artifact = artifacts.get(path)
    if not isinstance(artifact, dict) or artifact.get("kind") != kind:
        raise RuntimeError(f"{component} component inventory is missing: {path}")
    return path


def _validate_whole_shards(
    component: Mapping[str, object],
    artifacts: Mapping[str, object],
    expected_rows: object,
    prefix: str,
) -> set[str]:
    shards = component.get("shards")
    if not isinstance(shards, list) or not shards:
        raise RuntimeError("whole embedding shards are missing")
    expected_start = 0
    shard_paths: set[str] = set()
    for shard in shards:
        if not isinstance(shard, dict) or set(shard) != {
            "vectors_path",
            "vectors_sha256",
            "source_vectors_sha256",
            "row_start",
            "row_end",
            "rows",
            "dimension",
        }:
            raise RuntimeError("whole embedding shard contract differs")
        rows = shard.get("rows")
        row_start = shard.get("row_start")
        row_end = shard.get("row_end")
        if (
            type(rows) is not int
            or type(row_start) is not int
            or type(row_end) is not int
            or row_start != expected_start
            or row_end != row_start + rows
            or rows <= 0
            or shard.get("dimension") != APPROVED_WHOLE_DIMENSION
        ):
            raise RuntimeError("whole embedding shard row/dimension contract differs")
        path = _component_file(
            "whole embedding",
            shard.get("vectors_path"),
            artifacts,
            kind="embedding",
            prefix=prefix,
        )
        artifact = artifacts[path]
        if not isinstance(artifact, dict) or artifact.get("sha256") != shard.get("vectors_sha256"):
            raise RuntimeError("whole embedding derived shard SHA-256 differs")
        _require_sha256("whole source vector SHA-256", shard.get("source_vectors_sha256"))
        if path in shard_paths:
            raise RuntimeError("whole embedding shard file is repeated")
        shard_paths.add(path)
        expected_start = row_end
    if expected_start != expected_rows:
        raise RuntimeError("whole embedding shard rows differ from the runtime contract")
    return shard_paths


def _validate_query_corrections_documents(
    document: Mapping[str, object],
    attestation: Mapping[str, object],
    candidate_sha256: str,
) -> None:
    _require_exact_keys(
        "query corrections",
        document,
        {
            "schema_version",
            "complete",
            "publication_allowed",
            "source_policy",
            "test_jd_used",
            "uses_ground_truth",
            "uses_behavior_logs",
            "train_cutoff_exclusive",
            "max_source_timestamp",
            "source_manifest_sha256",
            "evidence_sha256",
            "minimum_support",
            "corrections",
        },
    )
    if (
        document.get("schema_version") != 1
        or document.get("complete") is not True
        or document.get("publication_allowed") is not False
        or document.get("source_policy") != "train_jd_only"
        or document.get("test_jd_used") is not False
        or document.get("uses_ground_truth") is not False
        or document.get("uses_behavior_logs") is not False
    ):
        raise RuntimeError("query corrections are not pinned to the train-JD corpus")
    cutoff = _timestamp(document.get("train_cutoff_exclusive"), "query correction cutoff")
    maximum = _timestamp(document.get("max_source_timestamp"), "query correction maximum")
    if maximum >= cutoff:
        raise RuntimeError("query corrections include post-cutoff source data")
    for name in ("source_manifest_sha256", "evidence_sha256"):
        _require_sha256(f"query correction {name}", document.get(name))
    minimum_support = document.get("minimum_support")
    if type(minimum_support) is not int or minimum_support < 1:
        raise RuntimeError("query correction minimum support must be positive")
    corrections = document.get("corrections")
    if not isinstance(corrections, dict) or not corrections:
        raise RuntimeError("query corrections mapping differs")
    for source, target in corrections.items():
        normalized_source = (
            " ".join(unicodedata.normalize("NFKC", source).casefold().split())
            if isinstance(source, str)
            else ""
        )
        normalized_target = (
            " ".join(unicodedata.normalize("NFKC", target).casefold().split())
            if isinstance(target, str)
            else ""
        )
        if (
            not normalized_source
            or not normalized_target
            or normalized_source != source
            or normalized_target != target
            or source == target
        ):
            raise RuntimeError("query corrections contain a non-canonical rule")
    _require_exact_keys(
        "query correction promotion attestation",
        attestation,
        {
            "schema_version",
            "complete",
            "attestation_kind",
            "candidate_sha256",
            "promotion_report_sha256",
            "publication_allowed",
            "evaluator_kind",
            "significant",
            "primary_metric",
            "absolute_delta",
            "evaluation_split_sha256",
            "baseline_run_sha256",
            "candidate_run_sha256",
        },
    )
    delta = attestation.get("absolute_delta")
    if (
        attestation.get("schema_version") != 1
        or attestation.get("complete") is not True
        or attestation.get("attestation_kind") != "fixed-input-query-correction-promotion"
        or attestation.get("candidate_sha256") != candidate_sha256
        or attestation.get("publication_allowed") is not True
        or attestation.get("evaluator_kind") != "organizer"
        or attestation.get("significant") is not True
        or attestation.get("primary_metric") != "ndcg_at_10"
        or isinstance(delta, bool)
        or not isinstance(delta, (int, float))
        or not math.isfinite(delta)
        or delta <= 0
    ):
        raise RuntimeError("query correction promotion attestation did not pass")
    for name in (
        "promotion_report_sha256",
        "evaluation_split_sha256",
        "baseline_run_sha256",
        "candidate_run_sha256",
    ):
        _require_sha256(f"query correction attestation {name}", attestation.get(name))


def _validate_component_manifests(
    manifest: Mapping[str, object], documents: Mapping[str, bytes]
) -> set[str]:
    artifacts = cast(Mapping[str, object], manifest["artifacts"])
    incumbents = cast(Mapping[str, Mapping[str, object]], manifest["incumbents"])
    reachable: set[str] = set()
    whole = incumbents["whole_embedding"]
    whole_path = _artifact_reference(artifacts, whole, "embedding")
    reachable.add(whole_path)
    _require_equal(
        "whole embedding runtime",
        {
            "complete": True,
            "model": APPROVED_MODEL,
            "revision": APPROVED_MODEL_REVISION,
            "source_dimension": APPROVED_SOURCE_EMBEDDING_DIMENSION,
            "dimension": APPROVED_WHOLE_DIMENSION,
            "projection": APPROVED_WHOLE_PROJECTION,
            "dtype": "float16",
            "normalized": True,
            "document_policy_version": APPROVED_DOCUMENT_POLICY_VERSION,
            "document_template_sha256": APPROVED_DOCUMENT_TEMPLATE_SHA256,
        },
        whole,
    )
    whole_document = _json_document(whole_path, artifacts, documents)
    _require_exact_keys(
        "whole embedding",
        whole_document,
        {
            "schema_version",
            "complete",
            "model",
            "revision",
            "source_dimension",
            "dimension",
            "projection",
            "dtype",
            "normalized",
            "rows",
            "dataset_sha256",
            "jobs_sha256",
            "job_row_order_sha256",
            "document_policy_version",
            "document_template_sha256",
            "document_fields",
            "query_prompt",
            "source_manifest_path",
            "source_manifest_sha256",
            "source_inventory_path",
            "source_inventory_sha256",
            "job_ids_path",
            "shards",
        },
    )
    _require_equal(
        "whole embedding",
        {
            "schema_version": 1,
            "complete": True,
            "model": APPROVED_MODEL,
            "revision": APPROVED_MODEL_REVISION,
            "source_dimension": APPROVED_SOURCE_EMBEDDING_DIMENSION,
            "dimension": APPROVED_WHOLE_DIMENSION,
            "projection": APPROVED_WHOLE_PROJECTION,
            "dtype": "float16",
            "normalized": True,
            "rows": whole.get("rows"),
            "dataset_sha256": whole.get("dataset_sha256"),
            "jobs_sha256": whole.get("jobs_sha256"),
            "job_row_order_sha256": whole.get("job_row_order_sha256"),
            "document_policy_version": APPROVED_DOCUMENT_POLICY_VERSION,
            "document_template_sha256": APPROVED_DOCUMENT_TEMPLATE_SHA256,
            "document_fields": APPROVED_DOCUMENT_FIELDS,
            "query_prompt": APPROVED_QUERY_PROMPT,
        },
        whole_document,
    )
    source_manifest_path = _artifact_reference(
        artifacts,
        {
            "manifest_path": whole_document.get("source_manifest_path"),
            "manifest_sha256": whole_document.get("source_manifest_sha256"),
        },
        "evidence",
    )
    source_inventory_path = _artifact_reference(
        artifacts,
        {
            "manifest_path": whole_document.get("source_inventory_path"),
            "manifest_sha256": whole_document.get("source_inventory_sha256"),
        },
        "evidence",
    )
    if (
        source_manifest_path != APPROVED_WHOLE_SOURCE_MANIFEST_PATH
        or source_inventory_path != APPROVED_WHOLE_SOURCE_INVENTORY_PATH
    ):
        raise RuntimeError("whole embedding sealed source provenance path differs")
    source_manifest = _json_document(source_manifest_path, artifacts, documents)
    _require_equal(
        "sealed whole source manifest",
        {
            "complete": True,
            "model": APPROVED_MODEL,
            "revision": APPROVED_MODEL_REVISION,
            "dataset_sha256": APPROVED_JOBS_DATASET_SHA256,
            "rows": APPROVED_WHOLE_SOURCE_ROWS,
            "dtype": "float16",
            "normalized": True,
            "document_policy_version": APPROVED_DOCUMENT_POLICY_VERSION,
            "document_template_sha256": APPROVED_DOCUMENT_TEMPLATE_SHA256,
            "document_fields": APPROVED_DOCUMENT_FIELDS,
            "job_row_order_sha256": whole.get("job_row_order_sha256"),
        },
        source_manifest,
    )
    source_shards = source_manifest.get("shards")
    if (
        not isinstance(source_shards, list)
        or len(source_shards) != APPROVED_WHOLE_SOURCE_SHARDS
        or any(
            not isinstance(shard, dict)
            or shard.get("index") != index
            or shard.get("dimension") != APPROVED_SOURCE_EMBEDDING_DIMENSION
            for index, shard in enumerate(source_shards)
        )
    ):
        raise RuntimeError("sealed whole source shard contract differs")
    source_inventory = _json_document(source_inventory_path, artifacts, documents)
    source_files = source_inventory.get("files")
    if source_inventory.get("schema_version") != 3 or not isinstance(source_files, list):
        raise RuntimeError("sealed whole source inventory schema differs")
    cache_files = [
        item
        for item in source_files
        if isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and cast(str, item["path"]).startswith("artifacts/experiments/qwen3-8b/full/")
    ]
    if (
        len(cache_files) != APPROVED_WHOLE_SOURCE_FILE_COUNT
        or sum(cast(int, item.get("size", -1)) for item in cache_files)
        != APPROVED_WHOLE_SOURCE_BYTES
        or not any(
            item.get("path") == "artifacts/experiments/qwen3-8b/full/manifest.json"
            and item.get("sha256") == APPROVED_WHOLE_SOURCE_MANIFEST_SHA256
            for item in cache_files
        )
    ):
        raise RuntimeError("sealed whole source inventory lineage differs")
    reachable.update({source_manifest_path, source_inventory_path})
    whole_prefix = str(PurePosixPath(whole_path).parent) + "/"
    reachable.add(
        _component_file(
            "whole embedding",
            whole_document.get("job_ids_path"),
            artifacts,
            kind="embedding",
            prefix=whole_prefix,
        )
    )
    reachable.update(
        _validate_whole_shards(whole_document, artifacts, whole.get("rows"), whole_prefix)
    )

    temporal = incumbents["temporal_tantivy"]
    temporal_path = _artifact_reference(artifacts, temporal, "index")
    reachable.add(temporal_path)
    _require_equal(
        "temporal Tantivy runtime",
        {
            "complete": True,
            "engine": APPROVED_TANTIVY_ENGINE,
            "updated_at_field": "updated_at_epoch_ms",
            "hard_filters": True,
            "temporal_filter_semantics": TEMPORAL_FILTER_SEMANTICS,
        },
        temporal,
    )
    temporal_document = _json_document(temporal_path, artifacts, documents)
    _require_exact_keys(
        "temporal Tantivy",
        temporal_document,
        {
            "schema_version",
            "complete",
            "engine",
            "jobs_sha256",
            "job_row_order_sha256",
            "index_sha256",
            "updated_at_field",
            "filter_semantics",
            "temporal_filter_semantics",
            "index_directory",
            "index_files",
            "taxonomy_path",
            "job_ids_path",
            "query_corrections",
            "build_manifest_path",
            "build_manifest_sha256",
            "schema_fields",
            "field_boosts",
            "lexical_policy_version",
            "lexical_policy_sha256",
            "tokenizers",
            "source_fields",
        },
    )
    _require_equal(
        "temporal Tantivy",
        {
            "schema_version": 1,
            "complete": True,
            "engine": APPROVED_TANTIVY_ENGINE,
            "jobs_sha256": whole.get("jobs_sha256"),
            "job_row_order_sha256": whole.get("job_row_order_sha256"),
            "index_sha256": temporal.get("index_sha256"),
            "updated_at_field": "updated_at_epoch_ms",
            "temporal_filter_semantics": TEMPORAL_FILTER_SEMANTICS,
            "schema_fields": APPROVED_TANTIVY_SCHEMA_FIELDS,
            "field_boosts": APPROVED_TANTIVY_FIELD_BOOSTS,
            "lexical_policy_version": APPROVED_LEXICAL_POLICY_VERSION,
            "lexical_policy_sha256": APPROVED_LEXICAL_POLICY_SHA256,
            "tokenizers": APPROVED_TANTIVY_TOKENIZERS,
            "source_fields": APPROVED_TANTIVY_SOURCE_FIELDS,
            "filter_semantics": TANTIVY_FILTER_SEMANTICS,
        },
        temporal_document,
    )
    index_directory = temporal_document.get("index_directory")
    expected_directory = str(PurePosixPath(temporal_path).parent / "index")
    if index_directory != expected_directory:
        raise RuntimeError("temporal Tantivy index directory differs")
    raw_index_files = temporal_document.get("index_files")
    if not isinstance(raw_index_files, list) or not raw_index_files:
        raise RuntimeError("temporal Tantivy index files are missing")
    index_files = {
        _component_file(
            "temporal Tantivy",
            path,
            artifacts,
            kind="index",
            prefix=f"{expected_directory}/",
        )
        for path in raw_index_files
    }
    if len(index_files) != len(raw_index_files):
        raise RuntimeError("temporal Tantivy index file is repeated")
    reachable.update(index_files)
    temporal_prefix = str(PurePosixPath(temporal_path).parent) + "/"
    reachable.add(
        _component_file(
            "temporal Tantivy",
            temporal_document.get("taxonomy_path"),
            artifacts,
            kind="index",
            prefix=temporal_prefix,
        )
    )
    job_ids_path = _component_file(
        "temporal Tantivy",
        temporal_document.get("job_ids_path"),
        artifacts,
        kind="index",
        prefix=temporal_prefix,
    )
    whole_job_ids_path = cast(str, whole_document["job_ids_path"])
    if cast(Mapping[str, object], artifacts[job_ids_path]).get("sha256") != cast(
        Mapping[str, object], artifacts[whole_job_ids_path]
    ).get("sha256"):
        raise RuntimeError("Tantivy job IDs differ from whole embedding row order")
    reachable.add(job_ids_path)
    if job_ids_path != TANTIVY_JOB_IDS_RUNTIME_PATH:
        raise RuntimeError("Tantivy job ID runtime path differs")
    tantivy_build_manifest_path = _artifact_reference(
        artifacts,
        {
            "manifest_path": temporal_document.get("build_manifest_path"),
            "manifest_sha256": temporal_document.get("build_manifest_sha256"),
        },
        "evidence",
    )
    if tantivy_build_manifest_path != APPROVED_TANTIVY_BUILD_PROVENANCE_PATH:
        raise RuntimeError("Tantivy build manifest path differs")
    reachable.add(tantivy_build_manifest_path)
    tantivy_build = _json_document(tantivy_build_manifest_path, artifacts, documents)
    _require_exact_keys(
        "Tantivy build manifest",
        tantivy_build,
        {
            "schema_version",
            "complete",
            "builder",
            "engine",
            "dataset_sha256",
            "jobs_sha256",
            "job_row_order_sha256",
            "rows",
            "index_sha256",
            "index_tree",
            "taxonomy_sha256",
            "query_corrections",
            "lexical_policy_version",
            "lexical_policy_sha256",
            "tokenizers",
            "source_fields",
            "source_csv_fields",
            "salary_filter_excluded_rows",
        },
    )
    salary_filter_excluded_rows = tantivy_build["salary_filter_excluded_rows"]
    if (
        not isinstance(salary_filter_excluded_rows, int)
        or isinstance(salary_filter_excluded_rows, bool)
        or not 0 <= salary_filter_excluded_rows <= whole.get("rows", -1)
    ):
        raise RuntimeError("Tantivy salary filter exclusion count is invalid")
    _require_equal(
        "Tantivy build manifest",
        {
            "schema_version": 1,
            "complete": True,
            "builder": "tantivy_index_pipeline.py",
            "engine": APPROVED_TANTIVY_ENGINE,
            "dataset_sha256": whole.get("dataset_sha256"),
            "jobs_sha256": whole.get("jobs_sha256"),
            "job_row_order_sha256": whole.get("job_row_order_sha256"),
            "rows": whole.get("rows"),
            "index_sha256": temporal.get("index_sha256"),
            "query_corrections": temporal_document.get("query_corrections"),
            "lexical_policy_version": APPROVED_LEXICAL_POLICY_VERSION,
            "lexical_policy_sha256": APPROVED_LEXICAL_POLICY_SHA256,
            "tokenizers": APPROVED_TANTIVY_TOKENIZERS,
            "source_fields": APPROVED_TANTIVY_SOURCE_FIELDS,
        },
        tantivy_build,
    )
    index_tree = tantivy_build.get("index_tree")
    if not isinstance(index_tree, list) or not index_tree:
        raise RuntimeError("Tantivy build index tree is missing")
    expected_tree = []
    for path in sorted(index_files):
        artifact = cast(Mapping[str, object], artifacts[path])
        expected_tree.append(
            {
                "path": path.removeprefix(f"{expected_directory}/"),
                "sha256": artifact.get("sha256"),
                "size_bytes": artifact.get("size_bytes"),
            }
        )
    if index_tree != expected_tree or _canonical_sha256(index_tree) != temporal.get("index_sha256"):
        raise RuntimeError("Tantivy build index tree differs from runtime inventory")
    taxonomy_path = cast(str, temporal_document["taxonomy_path"])
    taxonomy_artifact = cast(Mapping[str, object], artifacts[taxonomy_path])
    if taxonomy_artifact.get("sha256") != tantivy_build.get("taxonomy_sha256"):
        raise RuntimeError("Tantivy taxonomy differs from build manifest")
    correction = temporal_document.get("query_corrections")
    if correction == {"enabled": False}:
        pass
    elif isinstance(correction, dict):
        _require_exact_keys(
            "enabled Tantivy query corrections",
            correction,
            {
                "enabled",
                "artifact_path",
                "artifact_sha256",
                "promotion_attestation_path",
                "promotion_attestation_sha256",
            },
        )
        if correction.get("enabled") is not True:
            raise RuntimeError("query correction enabled flag must be boolean")
        corrections_path = _component_file(
            "temporal Tantivy",
            correction.get("artifact_path"),
            artifacts,
            kind="index",
            prefix=temporal_prefix,
        )
        attestation_path = _component_file(
            "temporal Tantivy",
            correction.get("promotion_attestation_path"),
            artifacts,
            kind="evidence",
            prefix=temporal_prefix,
        )
        corrections_artifact = cast(Mapping[str, object], artifacts[corrections_path])
        attestation_artifact = cast(Mapping[str, object], artifacts[attestation_path])
        if corrections_artifact.get("sha256") != correction.get(
            "artifact_sha256"
        ) or attestation_artifact.get("sha256") != correction.get("promotion_attestation_sha256"):
            raise RuntimeError("query correction component SHA-256 differs")
        _validate_query_corrections_documents(
            _json_document(corrections_path, artifacts, documents),
            _json_document(attestation_path, artifacts, documents),
            cast(str, correction["artifact_sha256"]),
        )
        reachable.update({corrections_path, attestation_path})
    else:
        raise RuntimeError("Tantivy query corrections must be disabled or attested")

    challengers = cast(Mapping[str, Mapping[str, object]], manifest["challengers"])
    multiview = challengers["multiview_embedding"]
    if multiview.get("enabled") is True:
        path = _artifact_reference(artifacts, multiview, "embedding")
        reachable.add(path)
        component = _json_document(path, artifacts, documents)
        _require_exact_keys(
            "multi-view embedding",
            component,
            {
                "complete",
                "publication_allowed",
                "model",
                "revision",
                "model_contract_sha256",
                "tokenizer_sha256",
                "view_policy_sha256",
                "dataset_sha256",
                "output_dimension",
                "dtype",
                "normalized",
                "mrl_report_sha256",
                "mrl_evidence",
                "view_policy",
                "files",
            },
        )
        policy = component.get("view_policy")
        included_kinds = policy.get("included_kinds") if isinstance(policy, dict) else None
        _require_equal(
            "multi-view embedding",
            {
                "complete": True,
                "publication_allowed": True,
                "model": APPROVED_MODEL,
                "revision": APPROVED_MODEL_REVISION,
                "model_contract_sha256": multiview.get("model_contract_sha256"),
                "tokenizer_sha256": multiview.get("tokenizer_sha256"),
                "view_policy_sha256": multiview.get("view_policy_sha256"),
                "dataset_sha256": whole.get("dataset_sha256"),
                "output_dimension": APPROVED_MULTIVIEW_DIMENSION,
                "dtype": "float16",
                "normalized": True,
                "mrl_report_sha256": cast(Mapping[str, object], multiview["mrl_evidence"])[
                    "report_sha256"
                ],
            },
            component,
        )
        if included_kinds != APPROVED_MULTIVIEW_KINDS:
            raise RuntimeError("multi-view embedding view policy differs")
        component_evidence = component.get("mrl_evidence")
        runtime_evidence = multiview.get("mrl_evidence")
        if not isinstance(component_evidence, dict) or not isinstance(runtime_evidence, dict):
            raise RuntimeError("multi-view MRL component evidence is missing")
        _require_equal(
            "multi-view MRL",
            {
                "report_sha256": runtime_evidence.get("report_sha256"),
                "stable_result_sha256": runtime_evidence.get("stable_result_sha256"),
                "selected_dimension": APPROVED_MULTIVIEW_DIMENSION,
                "reference_dimension": APPROVED_MULTIVIEW_REFERENCE_DIMENSION,
            },
            component_evidence,
        )
        reachable.update(
            _inventory_entries(
                "multi-view embedding", component, artifacts, kinds={"embedding", "model"}
            )
        )

    graph = challengers["skill_graph"]
    if graph.get("enabled") is True:
        path = _artifact_reference(artifacts, graph, "graph")
        reachable.add(path)
        component = _json_document(path, artifacts, documents)
        _require_exact_keys(
            "skill Graph",
            component,
            {
                "complete",
                "publication_allowed",
                "schema_version",
                "train_cutoff_exclusive",
                "max_source_timestamp",
                "source_jd_sha256",
                "source_policy",
                "test_jd_used",
                "candidate_manifest_path",
                "candidate_manifest_sha256",
                "source_ablation_report_sha256",
                "serving_algorithm",
                "serving_policy_sha256",
                "serving_implementation_sha256",
                "evaluation_implementation_sha256",
                "promotion_report_path",
                "promotion_report_sha256",
                "organizer_attestation_path",
                "organizer_attestation_sha256",
                "files",
            },
        )
        _require_equal(
            "skill Graph",
            {
                "complete": True,
                "publication_allowed": True,
                "schema_version": graph.get("schema_version"),
                "train_cutoff_exclusive": graph.get("train_cutoff_exclusive"),
                "max_source_timestamp": graph.get("max_source_timestamp"),
                "source_jd_sha256": graph.get("source_jd_sha256"),
                "source_policy": "train_jd_only",
                "test_jd_used": False,
                "candidate_manifest_path": graph.get("candidate_manifest_path"),
                "candidate_manifest_sha256": graph.get("candidate_manifest_sha256"),
                "source_ablation_report_sha256": graph.get("source_ablation_report_sha256"),
                "serving_algorithm": GRAPH_SERVING_ALGORITHM,
                "serving_policy_sha256": GRAPH_SERVING_POLICY_SHA256,
                "serving_implementation_sha256": GRAPH_SERVING_IMPLEMENTATION_SHA256,
                "evaluation_implementation_sha256": graph.get("evaluation_implementation_sha256"),
                "promotion_report_path": cast(
                    Mapping[str, object], graph.get("promotion_evidence")
                ).get("report_path"),
                "promotion_report_sha256": cast(
                    Mapping[str, object], graph.get("promotion_evidence")
                ).get("report_sha256"),
                "organizer_attestation_path": graph.get("organizer_attestation_path"),
                "organizer_attestation_sha256": graph.get("organizer_attestation_sha256"),
            },
            component,
        )
        attestation_path = _artifact_reference(
            artifacts,
            {
                "manifest_path": component.get("organizer_attestation_path"),
                "manifest_sha256": component.get("organizer_attestation_sha256"),
            },
            "evidence",
        )
        _validate_graph_attestation(
            _json_document(attestation_path, artifacts, documents),
            graph,
            artifacts,
            documents,
        )
        reachable.add(attestation_path)
        candidate_path = _artifact_reference(
            artifacts,
            {
                "manifest_path": component.get("candidate_manifest_path"),
                "manifest_sha256": component.get("candidate_manifest_sha256"),
            },
            "evidence",
        )
        graph_files = _inventory_entries("skill Graph", component, artifacts, kinds={"graph"})
        component_prefix = str(PurePosixPath(path).parent) + "/"
        if graph_files != {component_prefix + name for name in GRAPH_SERVING_FILES}:
            raise RuntimeError("skill Graph files differ from the serving contract")
        _validate_graph_candidate_inventory(
            _json_document(candidate_path, artifacts, documents),
            graph_files,
            artifacts,
        )
        reachable.add(candidate_path)
        reachable.update(graph_files)

    for name, challenger in challengers.items():
        if (
            name in {"multiview_embedding", "skill_graph", "semantic_reranker"}
            or challenger.get("enabled") is not True
        ):
            continue
        if name == "guardrails":
            raise RuntimeError("serving runtime does not parse guardrails")
        kind = "ranker"
        path = _artifact_reference(artifacts, challenger, kind)
        reachable.add(path)
        component = _json_document(path, artifacts, documents)
        _require_exact_keys(
            name,
            component,
            {"complete", "publication_allowed", "promotion_report_sha256", "files"},
        )
        evidence = challenger.get("promotion_evidence")
        evidence_sha = evidence.get("report_sha256") if isinstance(evidence, dict) else None
        _require_equal(
            name,
            {
                "complete": True,
                "publication_allowed": True,
                "promotion_report_sha256": evidence_sha,
            },
            component,
        )
        reachable.update(_inventory_entries(name, component, artifacts, kinds={kind}))
    return reachable


def _forbid_sensitive_fields(value: object) -> None:
    forbidden_keys = {"password", "secret", "token", "access_key", "secret_access_key"}
    forbidden_data_keys = {
        "ground_truth",
        "ground_truth_rows",
        "judgments",
        "qrels",
        "query_history",
        "raw_logs",
        "test_jd",
        "test_jd_rows",
    }
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in forbidden_data_keys:
                raise RuntimeError(f"runtime JSON contains forbidden evaluation data: {key}")
            if normalized in forbidden_keys or normalized.endswith(
                ("_password", "_secret", "_token", "_access_key")
            ):
                raise RuntimeError(f"runtime manifest contains forbidden credential field: {key}")
            _forbid_sensitive_fields(child)
    elif isinstance(value, list):
        for child in value:
            _forbid_sensitive_fields(child)


def _forbid_artifact_path(path: str) -> None:
    normalized = re.sub(r"[^a-z0-9]+", "_", path.casefold()).strip("_")
    for marker in FORBIDDEN_PATH_PARTS:
        canonical_marker = re.sub(r"[^a-z0-9]+", "_", marker.casefold()).strip("_")
        if re.search(rf"(?:^|_){re.escape(canonical_marker)}(?:_|$)", normalized):
            raise RuntimeError(f"runtime bundle contains forbidden artifact path: {path}")


def _timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError(f"{name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError(f"{name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise RuntimeError(f"{name} must include a timezone")
    return parsed


def _validate_materialization_lineage(
    manifest: Mapping[str, object], documents: Mapping[str, bytes]
) -> set[str]:
    if APPROVED_TANTIVY_BUILD_MANIFEST_SHA256 is None or APPROVED_TANTIVY_INDEX_SHA256 is None:
        raise RuntimeError("approved temporal-v3 Tantivy build lineage is not configured")
    artifacts = cast(Mapping[str, object], manifest["artifacts"])
    incumbents = cast(Mapping[str, Mapping[str, object]], manifest["incumbents"])
    for path, expected_sha256, label in (
        (
            APPROVED_WHOLE_SOURCE_MANIFEST_PATH,
            APPROVED_WHOLE_SOURCE_MANIFEST_SHA256,
            "whole source manifest provenance",
        ),
        (
            APPROVED_WHOLE_SOURCE_INVENTORY_PATH,
            APPROVED_WHOLE_SOURCE_INVENTORY_SHA256,
            "whole source inventory provenance",
        ),
        (
            APPROVED_TANTIVY_BUILD_PROVENANCE_PATH,
            APPROVED_TANTIVY_BUILD_MANIFEST_SHA256,
            "Tantivy build provenance",
        ),
    ):
        artifact = artifacts.get(path)
        if (
            not isinstance(artifact, dict)
            or artifact.get("kind") != "evidence"
            or artifact.get("sha256") != expected_sha256
        ):
            raise RuntimeError(f"{label} is absent from runtime selection lineage")
    report = _json_document(MATERIALIZATION_REPORT_PATH, artifacts, documents)
    _require_exact_keys(
        "materialization lineage",
        report,
        {
            "schema_version",
            "whole_source_manifest_sha256",
            "whole_source_inventory_sha256",
            "whole_runtime_manifest_sha256",
            "projection",
            "tantivy_build_manifest_sha256",
            "tantivy_runtime_manifest_sha256",
            "tantivy_index_sha256",
            "dataset_sha256",
            "jobs_sha256",
            "job_row_order_sha256",
            "rows",
            "placement",
            "query_corrections",
        },
    )
    whole = incumbents["whole_embedding"]
    temporal = incumbents["temporal_tantivy"]
    temporal_document = _json_document(cast(str, temporal["manifest_path"]), artifacts, documents)
    expected = {
        "schema_version": 1,
        "whole_source_manifest_sha256": APPROVED_WHOLE_SOURCE_MANIFEST_SHA256,
        "whole_source_inventory_sha256": APPROVED_WHOLE_SOURCE_INVENTORY_SHA256,
        "whole_runtime_manifest_sha256": whole.get("manifest_sha256"),
        "projection": APPROVED_WHOLE_PROJECTION,
        "tantivy_build_manifest_sha256": APPROVED_TANTIVY_BUILD_MANIFEST_SHA256,
        "tantivy_runtime_manifest_sha256": temporal.get("manifest_sha256"),
        "tantivy_index_sha256": APPROVED_TANTIVY_INDEX_SHA256,
        "dataset_sha256": whole.get("dataset_sha256"),
        "jobs_sha256": whole.get("jobs_sha256"),
        "job_row_order_sha256": whole.get("job_row_order_sha256"),
        "rows": whole.get("rows"),
        "placement": "copy_sha256_verified",
        "query_corrections": temporal_document.get("query_corrections"),
    }
    if report != expected or temporal.get("index_sha256") != APPROVED_TANTIVY_INDEX_SHA256:
        raise RuntimeError("materialization lineage differs from approved source artifacts")
    return {
        APPROVED_WHOLE_SOURCE_MANIFEST_PATH,
        APPROVED_WHOLE_SOURCE_INVENTORY_PATH,
        APPROVED_TANTIVY_BUILD_PROVENANCE_PATH,
        MATERIALIZATION_REPORT_PATH,
    }


def _validate_promotion_evidence(
    evidence: object,
    artifacts: Mapping[str, object],
    documents: Mapping[str, bytes],
) -> str:
    if not isinstance(evidence, dict):
        raise RuntimeError("enabled challenger requires promotion evidence")
    delta = evidence.get("absolute_delta")
    if (
        evidence.get("decision") != "accepted"
        or type(delta) not in {int, float}
        or not math.isfinite(cast(float, delta))
        or cast(float, delta) <= 0
    ):
        raise RuntimeError("enabled challenger requires a finite positive NDCG@10 delta")
    report = {
        "manifest_path": evidence.get("report_path"),
        "manifest_sha256": evidence.get("report_sha256"),
    }
    report_path = _artifact_reference(artifacts, report, "evidence")
    body = _json_document(report_path, artifacts, documents)
    expected_keys = {
        "schema_version",
        "complete",
        "publication_allowed",
        "evaluation_split_sha256",
        "baseline_run_sha256",
        "candidate_run_sha256",
        "primary_metric",
        "baseline_value",
        "candidate_value",
        "absolute_delta",
    }
    if set(body) != expected_keys or body.get("schema_version") != 1:
        raise RuntimeError("promotion report schema differs")
    if body.get("complete") is not True or body.get("publication_allowed") is not True:
        raise RuntimeError("promotion report is incomplete or not publishable")
    lineage = {
        "evaluation split": "evaluation_split_sha256",
        "baseline run": "baseline_run_sha256",
        "candidate run": "candidate_run_sha256",
        "primary metric": "primary_metric",
        "absolute delta": "absolute_delta",
    }
    for label, key in lineage.items():
        if body.get(key) != evidence.get(key):
            raise RuntimeError(f"promotion report {label} differs from runtime lineage")
    baseline = body.get("baseline_value")
    candidate = body.get("candidate_value")
    report_delta = body.get("absolute_delta")
    if (
        type(baseline) not in {int, float}
        or type(candidate) not in {int, float}
        or type(report_delta) not in {int, float}
        or not all(
            math.isfinite(cast(float, value)) for value in (baseline, candidate, report_delta)
        )
        or cast(float, report_delta) <= 0
        or not math.isclose(
            cast(float, candidate) - cast(float, baseline),
            cast(float, report_delta),
            rel_tol=0,
            abs_tol=1e-12,
        )
    ):
        raise RuntimeError(
            "promotion report requires a finite positive internally consistent delta"
        )
    return report_path


def _validate_graph_attestation(
    attestation: Mapping[str, object],
    graph: Mapping[str, object],
    artifacts: Mapping[str, object],
    documents: Mapping[str, bytes],
) -> None:
    _require_exact_keys(
        "Graph organizer attestation",
        attestation,
        {
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
        },
    )
    promotion = cast(Mapping[str, object], graph.get("promotion_evidence"))
    report_path = _artifact_reference(
        artifacts,
        {
            "manifest_path": promotion.get("report_path"),
            "manifest_sha256": promotion.get("report_sha256"),
        },
        "evidence",
    )
    report = _json_document(report_path, artifacts, documents)
    expected = {
        "schema_version": 1,
        "complete": True,
        "attestation_kind": "fixed-input-graph-promotion",
        "candidate_manifest_sha256": graph.get("candidate_manifest_sha256"),
        "ablation_report_sha256": graph.get("source_ablation_report_sha256"),
        "publication_allowed": True,
        "evaluator_kind": "organizer",
        "significant": True,
        "primary_metric": "ndcg_at_10",
        "baseline_value": report.get("baseline_value"),
        "candidate_value": report.get("candidate_value"),
        "absolute_delta": promotion.get("absolute_delta"),
        "evaluation_split_sha256": promotion.get("evaluation_split_sha256"),
        "baseline_run_sha256": promotion.get("baseline_run_sha256"),
        "candidate_run_sha256": promotion.get("candidate_run_sha256"),
        "serving_algorithm": GRAPH_SERVING_ALGORITHM,
        "serving_policy_sha256": GRAPH_SERVING_POLICY_SHA256,
        "serving_implementation_sha256": GRAPH_SERVING_IMPLEMENTATION_SHA256,
        "evaluation_implementation_sha256": graph.get("evaluation_implementation_sha256"),
    }
    evaluator_id = attestation.get("evaluator_id")
    if (
        not isinstance(evaluator_id, str)
        or not evaluator_id.strip()
        or any(attestation.get(name) != value for name, value in expected.items())
    ):
        raise RuntimeError("Graph organizer attestation differs from runtime lineage")


def _validate_graph_candidate_inventory(
    candidate: Mapping[str, object],
    serving_paths: set[str],
    artifacts: Mapping[str, object],
) -> None:
    inventory = candidate.get("artifacts")
    if not isinstance(inventory, list):
        raise RuntimeError("Graph candidate artifact inventory is missing")
    candidate_files: dict[str, tuple[str, int]] = {}
    for position, value in enumerate(inventory):
        if not isinstance(value, dict):
            raise RuntimeError("Graph candidate artifact inventory differs")
        _require_exact_keys(
            f"Graph candidate artifact {position}",
            value,
            {"path", "kind", "sha256", "size_bytes"},
        )
        path = value.get("path")
        name = PurePosixPath(path).name if isinstance(path, str) else ""
        size = value.get("size_bytes")
        if (
            value.get("kind") != "graph"
            or not name
            or path != name
            or name in candidate_files
            or type(size) is not int
            or cast(int, size) < 0
        ):
            raise RuntimeError("Graph candidate artifact inventory differs")
        candidate_files[name] = (
            _require_sha256(f"Graph candidate artifact {position}", value.get("sha256")),
            cast(int, size),
        )
    serving_files = {
        PurePosixPath(path).name: (
            cast(Mapping[str, object], artifacts[path]).get("sha256"),
            cast(Mapping[str, object], artifacts[path]).get("size_bytes"),
        )
        for path in serving_paths
    }
    if candidate_files != serving_files:
        raise RuntimeError("Graph serving files differ from the evaluated candidate inventory")


def validate_runtime_manifest(
    manifest: Mapping[str, object], documents: Mapping[str, bytes]
) -> None:
    try:
        release = cast(Mapping[str, object], manifest["release"])
        retrieval = cast(Mapping[str, object], manifest["retrieval_policy"])
        challengers = cast(Mapping[str, Mapping[str, object]], manifest["challengers"])
        artifacts = cast(Mapping[str, object], manifest["artifacts"])
    except (KeyError, TypeError) as error:
        raise RuntimeError("runtime manifest structure is incomplete") from error
    if manifest.get("schema_version") != 2:
        raise RuntimeError("runtime manifest schema version must be 2")
    if release.get("complete") is not True or release.get("publication_allowed") is not True:
        raise RuntimeError("runtime release is incomplete or publication is not allowed")
    if set(challengers) != CHALLENGERS:
        raise RuntimeError("runtime challenger flags are incomplete")
    if not isinstance(artifacts, dict) or not artifacts:
        raise RuntimeError("runtime artifact inventory is empty")
    try:
        as_of = cast(Mapping[str, object], retrieval["as_of"])
        eligibility = cast(Mapping[str, object], retrieval["eligibility"])
    except (KeyError, TypeError) as error:
        raise RuntimeError("runtime retrieval policy is incomplete") from error
    if as_of != {"production_mode": "request_time", "demo_reference": DEMO_AS_OF}:
        raise RuntimeError("runtime as_of policy differs")
    if eligibility != {
        "updated_within_days": 180,
        "future_jobs": "retained_with_zero_freshness",
        "stale_jobs": "exclude",
        "applied_before_retrieval": True,
    }:
        raise RuntimeError("runtime temporal eligibility policy differs")
    for path, raw in artifacts.items():
        if not isinstance(path, str) or not isinstance(raw, dict):
            raise RuntimeError("runtime artifact inventory is invalid")
        kind = raw.get("kind")
        if not isinstance(kind, str):
            raise RuntimeError(f"runtime artifact kind is missing: {path}")
        _forbid_artifact_path(path)
        validate_relative_path(path, kind)
        _require_sha256(f"artifact SHA-256 for {path}", raw.get("sha256"))
        if type(raw.get("size_bytes")) is not int or cast(int, raw["size_bytes"]) < 0:
            raise RuntimeError(f"runtime artifact size is invalid: {path}")
    for name in ("learning_to_rank", "guardrails"):
        if challengers[name] != {"enabled": False}:
            raise RuntimeError(f"{name} has no production adapter and must be disabled")
    evidence_paths: set[str] = set()
    for name, challenger in challengers.items():
        enabled = challenger.get("enabled")
        if enabled is False:
            if set(challenger) != {"enabled"}:
                raise RuntimeError(f"disabled challenger carries unverified metadata: {name}")
            continue
        if enabled is not True:
            raise RuntimeError(f"challenger enabled flag is invalid: {name}")
        if name == "semantic_reranker":
            if challenger != semantic_reranker_manifest():
                raise RuntimeError("semantic reranker lineage differs from the promoted profile")
            continue
        if name == "guardrails":
            raise RuntimeError("serving runtime does not parse guardrails")
        if (
            challenger.get("complete") is not True
            or challenger.get("publication_allowed") is not True
        ):
            label = "multi-view" if name == "multiview_embedding" else name
            raise RuntimeError(f"{label} challenger is incomplete or not publishable")
        if name == "multiview_embedding":
            evidence = challenger.get("mrl_evidence")
            if (
                challenger.get("output_dimension") != APPROVED_MULTIVIEW_DIMENSION
                or challenger.get("view_kinds") != APPROVED_MULTIVIEW_KINDS
                or not isinstance(evidence, dict)
                or evidence.get("decision") != "accepted"
                or evidence.get("selected_dimension") != APPROVED_MULTIVIEW_DIMENSION
                or evidence.get("reference_dimension") != APPROVED_MULTIVIEW_REFERENCE_DIMENSION
            ):
                raise RuntimeError("multi-view MRL publication gate differs")
            _artifact_reference(
                artifacts,
                {
                    "manifest_path": evidence.get("report_path"),
                    "manifest_sha256": evidence.get("report_sha256"),
                },
                "evidence",
            )
            mrl_path = cast(Mapping[str, object], evidence).get("report_path")
            if not isinstance(mrl_path, str):
                raise RuntimeError("multi-view MRL report path is missing")
            mrl_body = _json_document(mrl_path, artifacts, documents)
            if (
                mrl_body.get("stable_result_sha256") != evidence.get("stable_result_sha256")
                or mrl_body.get("selected_dimension") != APPROVED_MULTIVIEW_DIMENSION
                or mrl_body.get("reference_dimension") != APPROVED_MULTIVIEW_REFERENCE_DIMENSION
            ):
                raise RuntimeError("multi-view MRL report lineage differs")
            evidence_paths.add(mrl_path)
        evidence_paths.add(
            _validate_promotion_evidence(challenger.get("promotion_evidence"), artifacts, documents)
        )
    expected_count = len(artifacts)
    expected_size = sum(cast(int, value["size_bytes"]) for value in artifacts.values())
    expected_inventory = _canonical_sha256(artifacts)
    if (
        release.get("object_count") != expected_count
        or release.get("size_bytes") != expected_size
        or release.get("artifact_inventory_sha256") != expected_inventory
    ):
        raise RuntimeError("runtime release aggregate differs from artifact inventory")
    for name in (
        "release_spec_sha256",
        "source_manifest_sha256",
        "selected_inventory_sha256",
        "artifact_inventory_sha256",
    ):
        _require_sha256(name, release.get(name))
    _forbid_sensitive_fields(manifest)
    reachable = (
        _validate_component_manifests(manifest, documents)
        | _validate_materialization_lineage(manifest, documents)
        | evidence_paths
    )
    if reachable != set(artifacts):
        extra = sorted(set(artifacts) - reachable)
        missing = sorted(reachable - set(artifacts))
        raise RuntimeError(f"runtime artifact inventory has unreachable={extra} missing={missing}")
    try:
        schema = json.loads(RUNTIME_SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(manifest)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("runtime manifest schema could not be loaded") from error
    except ValidationError as error:
        location = "/".join(str(part) for part in error.absolute_path) or "<root>"
        raise RuntimeError(
            f"runtime manifest violates v2 schema at {location}: {error.message}"
        ) from error


def build_manifest(
    source: Mapping[str, object],
    spec: Mapping[str, object],
    component_documents: Mapping[str, bytes],
    release_spec_sha256: str,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    _require_sha256("release_spec_sha256", release_spec_sha256)
    if (
        set(spec)
        != {
            "schema_version",
            "source_manifest",
            "selected_inventory_sha256",
            "selections",
            "runtime",
        }
        or spec.get("schema_version") != 1
    ):
        raise RuntimeError("release spec contract differs")
    source_manifest = spec.get("source_manifest")
    runtime = spec.get("runtime")
    if not isinstance(source_manifest, dict) or not isinstance(runtime, dict):
        raise RuntimeError("release spec is incomplete")
    if set(runtime) != {"retrieval_policy", "incumbents", "challengers"}:
        raise RuntimeError("release spec runtime keys are invalid")
    source_manifest_sha = _require_sha256("source manifest SHA-256", source_manifest.get("sha256"))
    selected = select_artifacts(source, spec)
    artifacts = {
        cast(str, item["path"]): {
            "kind": item["kind"],
            "sha256": item["sha256"],
            "size_bytes": item["size_bytes"],
        }
        for item in selected
    }
    manifest = {
        "schema_version": 2,
        "release": {
            "complete": True,
            "publication_allowed": True,
            "release_spec_sha256": release_spec_sha256,
            "source_manifest_sha256": source_manifest_sha,
            "selected_inventory_sha256": spec["selected_inventory_sha256"],
            "artifact_inventory_sha256": _canonical_sha256(artifacts),
            "object_count": len(selected),
            "size_bytes": sum(cast(int, item["size_bytes"]) for item in selected),
        },
        **runtime,
        "artifacts": artifacts,
    }
    validate_runtime_manifest(manifest, component_documents)
    return manifest, selected


def _source_manifest_contract(spec: Mapping[str, object]) -> tuple[str, str]:
    source = spec.get("source_manifest")
    if not isinstance(source, dict) or set(source) != {"key", "sha256"}:
        raise RuntimeError("release spec source manifest contract is invalid")
    key = source.get("key")
    if not isinstance(key, str) or not key.endswith("/manifest.json"):
        raise RuntimeError("source manifest key must end with /manifest.json")
    _validate_source_path(key)
    return key, _require_sha256("source manifest SHA-256", source.get("sha256"))


def _read_json_object(payload: bytes, name: str) -> dict[str, object]:
    try:
        value = json.loads(payload, parse_constant=_reject_nonfinite_json)
    except (json.JSONDecodeError, ValueError) as error:
        raise RuntimeError(f"{name} is invalid JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{name} is not a JSON object")
    return cast(dict[str, object], value)


def load_release_spec(path: Path) -> tuple[dict[str, object], str]:
    payload = path.read_bytes()
    return _read_json_object(payload, "release spec"), hashlib.sha256(payload).hexdigest()


def load_source_manifest(
    spec: Mapping[str, object], local_path: Path | None = None
) -> dict[str, object]:
    key, expected_sha = _source_manifest_contract(spec)
    payload = (
        local_path.read_bytes()
        if local_path is not None
        else bytes(_run(["s3", "cp", f"s3://{SOURCE_BUCKET}/{key}", "-"], text=False).stdout)
    )
    if hashlib.sha256(payload).hexdigest() != expected_sha:
        raise RuntimeError("source manifest SHA-256 does not match the release spec")
    return _read_json_object(payload, "source manifest")


def _document_paths(runtime: Mapping[str, object]) -> set[str]:
    try:
        incumbents = cast(Mapping[str, Mapping[str, object]], runtime["incumbents"])
        challengers = cast(Mapping[str, Mapping[str, object]], runtime["challengers"])
    except (KeyError, TypeError) as error:
        raise RuntimeError("release runtime component contract is incomplete") from error
    paths = {
        *(cast(str, value["manifest_path"]) for value in incumbents.values()),
        MATERIALIZATION_REPORT_PATH,
        APPROVED_WHOLE_SOURCE_MANIFEST_PATH,
        APPROVED_WHOLE_SOURCE_INVENTORY_PATH,
        APPROVED_TANTIVY_BUILD_PROVENANCE_PATH,
    }
    paths.update(
        cast(str, value["manifest_path"])
        for name, value in challengers.items()
        if value.get("enabled") is True and name != "semantic_reranker"
    )
    for challenger in challengers.values():
        if challenger.get("enabled") is not True:
            continue
        promotion = challenger.get("promotion_evidence")
        if isinstance(promotion, dict) and isinstance(promotion.get("report_path"), str):
            paths.add(cast(str, promotion["report_path"]))
        mrl = challenger.get("mrl_evidence")
        if isinstance(mrl, dict) and isinstance(mrl.get("report_path"), str):
            paths.add(cast(str, mrl["report_path"]))
    return paths


def load_component_documents(
    spec: Mapping[str, object],
    items: Sequence[Mapping[str, object]],
    local_root: Path | None = None,
) -> dict[str, bytes]:
    runtime = spec.get("runtime")
    if not isinstance(runtime, dict):
        raise RuntimeError("release spec runtime contract is missing")
    paths = _document_paths(runtime)
    by_destination = {cast(str, item["path"]): item for item in items}
    source_key, _ = _source_manifest_contract(spec)
    source_root = source_key.removesuffix("manifest.json")
    documents: dict[str, bytes] = {}

    def load(path: str) -> None:
        if path in documents:
            return
        item = by_destination.get(path)
        if item is None:
            raise RuntimeError(f"component manifest is not selected: {path}")
        source_path = cast(str, item["source_path"])
        if local_root is None:
            payload = bytes(
                _run(
                    ["s3", "cp", f"s3://{SOURCE_BUCKET}/{source_root}{source_path}", "-"],
                    text=False,
                ).stdout
            )
        else:
            candidate = (local_root / source_path).resolve()
            root = local_root.resolve()
            if not candidate.is_relative_to(root):
                raise RuntimeError(f"component source path escaped local root: {source_path}")
            payload = candidate.read_bytes()
        if (
            len(payload) != item["size_bytes"]
            or hashlib.sha256(payload).hexdigest() != item["sha256"]
        ):
            raise RuntimeError(f"component source object differs: {source_path}")
        documents[path] = payload

    for path in paths:
        load(path)
    incumbents = cast(Mapping[str, Mapping[str, object]], runtime["incumbents"])
    temporal_path = cast(str, incumbents["temporal_tantivy"]["manifest_path"])
    try:
        temporal = json.loads(documents[temporal_path])
    except (KeyError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Tantivy component manifest cannot be read") from error
    if not isinstance(temporal, dict):
        raise RuntimeError("Tantivy component manifest must be an object")
    for name in ("build_manifest_path",):
        value = temporal.get(name)
        if not isinstance(value, str):
            raise RuntimeError(f"Tantivy component {name} is missing")
        load(value)
    correction = temporal.get("query_corrections")
    if correction != {"enabled": False}:
        if not isinstance(correction, dict):
            raise RuntimeError("Tantivy query correction mode must be an object")
        for name in ("artifact_path", "promotion_attestation_path"):
            value = correction.get(name)
            if not isinstance(value, str):
                raise RuntimeError(f"enabled query correction {name} is missing")
            load(value)
    return documents


def _head(bucket: str, key: str) -> dict[str, object]:
    return aws(
        [
            "s3api",
            "head-object",
            "--bucket",
            bucket,
            "--key",
            key,
            "--expected-bucket-owner",
            AWS_ACCOUNT,
            "--checksum-mode",
            "ENABLED",
        ]
    )


def _verify_destination(head: Mapping[str, object], item: Mapping[str, object]) -> None:
    checksum = base64.b64encode(bytes.fromhex(cast(str, item["sha256"]))).decode()
    if head.get("ContentLength") != item["size_bytes"] or head.get("ChecksumSHA256") != checksum:
        raise RuntimeError(f"destination object differs: {item['path']}")


def _put_source_file(path: Path, key: str, sha256: str, size: int) -> None:
    checksum = base64.b64encode(bytes.fromhex(sha256)).decode()
    if path.stat().st_size != size or _file_sha256(path) != sha256:
        raise RuntimeError(f"local source object differs: {path}")
    try:
        head = _head(SOURCE_BUCKET, key)
    except AwsError as error:
        if not any(marker in error.stderr for marker in ("(404)", "Not Found", "NoSuchKey")):
            raise
    else:
        if head.get("ContentLength") != size or head.get("ChecksumSHA256") != checksum:
            raise RuntimeError(f"existing source object differs: {key}")
        return
    try:
        aws(
            [
                "s3api",
                "put-object",
                "--bucket",
                SOURCE_BUCKET,
                "--key",
                key,
                "--body",
                str(path),
                "--checksum-algorithm",
                "SHA256",
                "--checksum-sha256",
                checksum,
                "--if-none-match",
                "*",
                "--expected-bucket-owner",
                AWS_ACCOUNT,
            ]
        )
    except AwsError as error:
        if "PreconditionFailed" not in error.stderr:
            raise
    head = _head(SOURCE_BUCKET, key)
    if head.get("ContentLength") != size or head.get("ChecksumSHA256") != checksum:
        raise RuntimeError(f"uploaded source object differs: {key}")


def stage_source(
    source: Mapping[str, object],
    spec: Mapping[str, object],
    local_root: Path,
    local_manifest: Path,
) -> None:
    root = local_root.resolve()
    manifest = local_manifest.resolve()
    if manifest != root / "manifest.json":
        raise RuntimeError("source manifest must be source-root/manifest.json")
    key, manifest_sha = _source_manifest_contract(spec)
    expected_key = f"one111-search/materialized/{manifest_sha}/manifest.json"
    if key != expected_key:
        raise RuntimeError("source manifest key is not content-addressed")
    inventory = _parse_source_manifest(source)
    prefix = key.removesuffix("manifest.json")
    expected: dict[str, int] = {}
    for path, item in sorted(inventory.items()):
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root):
            raise RuntimeError(f"source object escaped local root: {path}")
        object_key = f"{prefix}{path}"
        _put_source_file(candidate, object_key, cast(str, item["sha256"]), cast(int, item["size"]))
        expected[object_key] = cast(int, item["size"])
    manifest_size = manifest.stat().st_size
    before_commit = _list_source(prefix)
    committed = {**expected, key: manifest_size}
    if before_commit not in (expected, committed):
        raise RuntimeError("source prefix contains unexpected objects before manifest commit")
    _put_source_file(manifest, key, manifest_sha, manifest_size)
    expected[key] = manifest_size
    if _list_source(prefix) != expected:
        raise RuntimeError("source prefix key or size inventory differs")


def copy_artifacts(
    items: Sequence[Mapping[str, object]], manifest_sha: str, source_root: str
) -> None:
    for item in items:
        source_key = f"{source_root}{item['source_path']}"
        key = destination_key(manifest_sha, cast(str, item["path"]))
        source_head = _head(SOURCE_BUCKET, source_key)
        source_checksum = base64.b64encode(bytes.fromhex(cast(str, item["sha256"]))).decode()
        if (
            source_head.get("ContentLength") != item["size_bytes"]
            or source_head.get("ChecksumSHA256") != source_checksum
        ):
            raise RuntimeError(f"source object checksum drifted: {item['source_path']}")
        try:
            destination_head = _head(DESTINATION_BUCKET, key)
        except AwsError as error:
            if not any(marker in error.stderr for marker in ("(404)", "Not Found", "NoSuchKey")):
                raise
        else:
            _verify_destination(destination_head, item)
            continue
        try:
            aws(
                [
                    "s3api",
                    "copy-object",
                    "--bucket",
                    DESTINATION_BUCKET,
                    "--key",
                    key,
                    "--copy-source",
                    f"{SOURCE_BUCKET}/{source_key}",
                    "--copy-source-if-match",
                    str(source_head["ETag"]),
                    "--if-none-match",
                    "*",
                    "--checksum-algorithm",
                    "SHA256",
                    "--metadata-directive",
                    "REPLACE",
                    "--metadata",
                    f"sha256={item['sha256']}",
                    "--expected-bucket-owner",
                    AWS_ACCOUNT,
                ]
            )
        except AwsError as error:
            if "PreconditionFailed" not in error.stderr:
                raise
        _verify_destination(_head(DESTINATION_BUCKET, key), item)


def _list_bucket(bucket: str, prefix: str) -> dict[str, int]:
    continuation: str | None = None
    result: dict[str, int] = {}
    while True:
        arguments = [
            "s3api",
            "list-objects-v2",
            "--bucket",
            bucket,
            "--prefix",
            prefix,
            "--expected-bucket-owner",
            AWS_ACCOUNT,
            "--max-keys",
            "1000",
            "--no-paginate",
        ]
        if continuation is not None:
            arguments.extend(["--continuation-token", continuation])
        page = aws(arguments)
        contents = page.get("Contents", [])
        if not isinstance(contents, list):
            raise RuntimeError("destination prefix listing has invalid contents")
        for raw in contents:
            if (
                not isinstance(raw, dict)
                or not isinstance(raw.get("Key"), str)
                or type(raw.get("Size")) is not int
            ):
                raise RuntimeError("destination prefix listing has invalid object metadata")
            key = cast(str, raw["Key"])
            if key in result:
                raise RuntimeError(f"destination prefix listing repeated key: {key}")
            result[key] = cast(int, raw["Size"])
        if page.get("IsTruncated") is False:
            return result
        token = page.get("NextContinuationToken")
        if not isinstance(token, str) or not token:
            raise RuntimeError("destination prefix listing was truncated without continuation")
        continuation = token


def _list_destination(prefix: str) -> dict[str, int]:
    return _list_bucket(DESTINATION_BUCKET, prefix)


def _list_source(prefix: str) -> dict[str, int]:
    return _list_bucket(SOURCE_BUCKET, prefix)


def _read_destination_manifest(manifest_sha: str) -> bytes:
    key = f"runtime/{manifest_sha}/manifest.json"
    with tempfile.NamedTemporaryFile() as stream:
        aws(
            [
                "s3api",
                "get-object",
                "--bucket",
                DESTINATION_BUCKET,
                "--key",
                key,
                "--expected-bucket-owner",
                AWS_ACCOUNT,
                "--checksum-mode",
                "ENABLED",
                stream.name,
            ]
        )
        return Path(stream.name).read_bytes()


def put_manifest(payload: bytes, manifest_sha: str) -> None:
    key = f"runtime/{manifest_sha}/manifest.json"
    checksum = base64.b64encode(hashlib.sha256(payload).digest()).decode()
    with tempfile.NamedTemporaryFile() as stream:
        stream.write(payload)
        stream.flush()
        try:
            aws(
                [
                    "s3api",
                    "put-object",
                    "--bucket",
                    DESTINATION_BUCKET,
                    "--key",
                    key,
                    "--body",
                    stream.name,
                    "--content-type",
                    "application/json",
                    "--metadata",
                    f"sha256={manifest_sha}",
                    "--checksum-algorithm",
                    "SHA256",
                    "--checksum-sha256",
                    checksum,
                    "--if-none-match",
                    "*",
                    "--expected-bucket-owner",
                    AWS_ACCOUNT,
                ]
            )
        except AwsError as error:
            if "PreconditionFailed" not in error.stderr:
                raise
    head = _head(DESTINATION_BUCKET, key)
    metadata = head.get("Metadata")
    if (
        head.get("ContentLength") != len(payload)
        or not isinstance(metadata, dict)
        or metadata.get("sha256") != manifest_sha
        or head.get("ChecksumSHA256") != checksum
        or _read_destination_manifest(manifest_sha) != payload
    ):
        raise RuntimeError("destination manifest differs")


def audit_data_objects(
    items: Sequence[Mapping[str, object]], manifest_sha: str, payload: bytes
) -> None:
    prefix = f"runtime/{manifest_sha}/"
    manifest_key = f"{prefix}manifest.json"
    actual = _list_destination(prefix)
    prior_manifest_size = actual.pop(manifest_key, None)
    expected = {
        destination_key(manifest_sha, cast(str, item["path"])): cast(int, item["size_bytes"])
        for item in items
    }
    if actual != expected:
        raise RuntimeError("destination data-only key or size inventory differs")
    if prior_manifest_size is not None and (
        prior_manifest_size != len(payload) or _read_destination_manifest(manifest_sha) != payload
    ):
        raise RuntimeError("existing destination manifest differs during data-only audit")
    for item in items:
        _verify_destination(
            _head(DESTINATION_BUCKET, destination_key(manifest_sha, cast(str, item["path"]))),
            item,
        )


def audit_destination(
    items: Sequence[Mapping[str, object]], manifest_sha: str, manifest_bytes: int
) -> None:
    prefix = f"runtime/{manifest_sha}/"
    expected = {
        destination_key(manifest_sha, cast(str, item["path"])): cast(int, item["size_bytes"])
        for item in items
    }
    expected[f"{prefix}manifest.json"] = manifest_bytes
    if _list_destination(prefix) != expected:
        raise RuntimeError("destination prefix key or size inventory differs")
    for item in items:
        _verify_destination(
            _head(DESTINATION_BUCKET, destination_key(manifest_sha, cast(str, item["path"]))),
            item,
        )


def publish_release(
    items: Sequence[Mapping[str, object]], payload: bytes, manifest_sha: str, source_root: str
) -> None:
    copy_artifacts(items, manifest_sha, source_root)
    audit_data_objects(items, manifest_sha, payload)
    put_manifest(payload, manifest_sha)
    audit_destination(items, manifest_sha, len(payload))


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Promote a verified runtime release to immutable S3"
    )
    parser.add_argument("--release-spec", type=Path, required=True)
    parser.add_argument(
        "--source-manifest-file",
        type=Path,
        help="local source manifest for dry-run or explicit --stage-source",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        help="local artifact root for dry-run or explicit --stage-source",
    )
    parser.add_argument(
        "--approved-tantivy-build-sha256",
        help="compiled temporal-v3 Tantivy build-manifest SHA-256",
    )
    parser.add_argument(
        "--approved-tantivy-index-sha256",
        help="compiled temporal-v3 Tantivy canonical index-tree SHA-256",
    )
    parser.add_argument("--execute", action="store_true", help="perform server-side S3 copies")
    parser.add_argument(
        "--stage-source",
        action="store_true",
        help="upload the verified local source bundle before --execute",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    global APPROVED_TANTIVY_BUILD_MANIFEST_SHA256, APPROVED_TANTIVY_INDEX_SHA256

    args = _parse_args(argv)
    if (args.approved_tantivy_build_sha256 is None) != (args.approved_tantivy_index_sha256 is None):
        raise RuntimeError("Tantivy build and index approvals must be supplied together")
    if args.approved_tantivy_build_sha256 is not None:
        APPROVED_TANTIVY_BUILD_MANIFEST_SHA256 = _require_sha256(
            "approved Tantivy build SHA-256", args.approved_tantivy_build_sha256
        )
        APPROVED_TANTIVY_INDEX_SHA256 = _require_sha256(
            "approved Tantivy index SHA-256", args.approved_tantivy_index_sha256
        )
    if (args.source_manifest_file is None) != (args.source_root is None):
        raise RuntimeError("offline dry-run requires both --source-manifest-file and --source-root")
    if args.stage_source and (not args.execute or args.source_manifest_file is None):
        raise RuntimeError("--stage-source requires --execute and the local source bundle")
    if args.execute and args.source_manifest_file is not None and not args.stage_source:
        raise RuntimeError("local source execution requires explicit --stage-source")
    spec, release_spec_sha = load_release_spec(args.release_spec)
    if args.source_manifest_file is None or args.execute:
        verify_account()
    source = load_source_manifest(spec, args.source_manifest_file)
    items = select_artifacts(source, spec)
    documents = load_component_documents(spec, items, args.source_root)
    manifest, items = build_manifest(source, spec, documents, release_spec_sha)
    payload = canonical_bytes(manifest)
    manifest_sha = hashlib.sha256(payload).hexdigest()
    source_key, _ = _source_manifest_contract(spec)
    if args.execute:
        if args.stage_source:
            stage_source(source, spec, args.source_root, args.source_manifest_file)
            if load_source_manifest(spec) != source:
                raise RuntimeError("staged source manifest differs")
        publish_release(items, payload, manifest_sha, source_key.removesuffix("manifest.json"))
    print(
        json.dumps(
            {
                "executed": args.execute,
                "source_staged": args.stage_source,
                "schema_version": 2,
                "manifest_sha256": manifest_sha,
                "object_count": len(items),
                "size_bytes": sum(cast(int, item["size_bytes"]) for item in items),
                "s3_prefix": f"s3://{DESTINATION_BUCKET}/runtime/{manifest_sha}/",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
