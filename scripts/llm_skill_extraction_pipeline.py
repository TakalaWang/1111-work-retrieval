#!/usr/bin/env python3
"""Prepare train-only JDs and resumably extract evidence-locked skills with Bedrock."""

from __future__ import annotations

import argparse
import csv
import hashlib
import heapq
import json
import math
import os
import shutil
import unicodedata
from collections import Counter
from collections.abc import Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from pipeline_contract import (
    atomic_json,
    canonical_json,
    exact_keys,
    read_json_object,
    require_sha256,
    sha256_file,
)
from skill_graph_pipeline import load_split_manifest
from work_retrieval_core.serialization import (
    DOCUMENT_POLICY_VERSION,
    FULL_JOB_FIELDS,
    canonical_code,
    canonical_text,
    document_template_sha256,
    serialize_full_job,
)

PROMPT_VERSION = "2026-08-02-open-surface-evidence-tool-64k-v1"
CANONICALIZATION_POLICY = "open_surface_per_jd_llm_canonicalization_v1"
OOV_POLICY = "accept_open_surface_with_exact_train_jd_evidence"
SYSTEM_PROMPT = """You extract a train-only job skill graph from exactly one supplied JD.
Call extract_job_skill_graph exactly once with exactly two arrays: skills and relations.
Each skill has canonical_name, surface, category, evidence_span.
Each relation has source, type, target, evidence_span; type is one of USED_WITH,
SPECIALIZATION_OF, RELATED_TO. surface must be an exact substring of evidence_span and both must
be exact substrings of the supplied JD. Use an open vocabulary: preserve a new/OOV surface.
canonical_name normalizes only a genuine spelling variant or synonym evidenced within this JD.
Never infer unsupported skills, and never use query logs, qrels, behavior, test data, or outside
facts. Do not return text."""
PROMPT_SHA256 = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()
JOB_ID_FIELD = "職缺編號"
DEFAULT_DUTY_FIELD = "職務小類"
DEFAULT_MODIFIED_AT_FIELD = "職缺最後修改時間"
DEFAULT_SOURCE_TIMEZONE = "Asia/Taipei"
SOURCE_TIMESTAMP_POLICY = "localize_naive_source_with_explicit_iana_timezone_v1"
DEFAULT_SAMPLE_LIMIT = 5_000
MAX_SAMPLE_LIMIT = 10_000
BEDROCK_TOTAL_MAX_ATTEMPTS = 4
BEDROCK_MAX_OUTPUT_TOKENS = 64_000
SAMPLING_POLICY = "duty_stratified_sqrt_support_stable_hash_v1"
RELATION_TYPES = {"USED_WITH", "SPECIALIZATION_OF", "RELATED_TO"}
TOOL_NAME = "extract_job_skill_graph"
# Verified Converse runtime shape; botocore's optional type enum currently drifts from this value.
TOOL_USE_TYPE = "tool_use"
TOOL_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["skills", "relations"],
    "properties": {
        "skills": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["canonical_name", "surface", "category", "evidence_span"],
                "properties": {
                    "canonical_name": {"type": "string", "minLength": 1},
                    "surface": {"type": "string", "minLength": 1},
                    "category": {"type": "string", "minLength": 1},
                    "evidence_span": {"type": "string", "minLength": 1},
                },
            },
        },
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source", "type", "target", "evidence_span"],
                "properties": {
                    "source": {"type": "string", "minLength": 1},
                    "type": {"type": "string", "enum": sorted(RELATION_TYPES)},
                    "target": {"type": "string", "minLength": 1},
                    "evidence_span": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}
TOOL_CONFIG = {
    "tools": [
        {
            "toolSpec": {
                "name": TOOL_NAME,
                "description": "Return the evidence-locked skill graph for the supplied JD.",
                "inputSchema": {"json": TOOL_INPUT_SCHEMA},
            }
        }
    ],
    "toolChoice": {"tool": {"name": TOOL_NAME}},
}
PREPARE_KEYS = {
    "schema_version",
    "complete",
    "source_policy",
    "split_id",
    "split_manifest_sha256",
    "train_cutoff_exclusive",
    "source_jd_sha256",
    "document_policy_version",
    "document_template_sha256",
    "document_fields",
    "job_id_field",
    "duty_field",
    "modified_at_field",
    "source_timezone",
    "source_timestamp_policy",
    "sampling_policy",
    "sample_limit",
    "eligible_train_records",
    "eligible_max_source_timestamp",
    "strata",
    "sampling_sha256",
    "records",
    "post_cutoff_skipped",
    "empty_duty_skipped",
    "max_source_timestamp",
    "requests_sha256",
}
REQUEST_KEYS = {
    "record_id",
    "job_id",
    "duty",
    "source_modified_at",
    "source_text",
    "source_text_sha256",
}
RESPONSE_KEYS = {
    "schema_version",
    "complete",
    "record_id",
    "request_sha256",
    "model_id",
    "prompt_version",
    "prompt_sha256",
    "skills",
    "relations",
    "skill_rejection_count",
    "relation_rejection_count",
    "input_tokens",
    "output_tokens",
}


class BedrockRuntime(Protocol):
    def converse(self, **kwargs: object) -> Mapping[str, object]: ...


class AwsIdentity(Protocol):
    def get_caller_identity(self) -> Mapping[str, object]: ...


def _normalize(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


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


def _iana_timezone(value: object) -> ZoneInfo:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("source timezone must be an explicit IANA timezone")
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise RuntimeError("source timezone must be an explicit IANA timezone") from error


def _source_timestamp(value: object, name: str, source_timezone: ZoneInfo) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError(f"{name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError(f"{name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=source_timezone)
    return parsed


def _duty_stratum(value: str | None) -> str:
    duty = canonical_code(value)
    return "" if duty == "null" else duty


def _sample_quotas(counts: Mapping[str, int], limit: int) -> dict[str, int]:
    if not 1 <= limit <= MAX_SAMPLE_LIMIT:
        raise ValueError(f"sample limit must be between 1 and {MAX_SAMPLE_LIMIT}")
    if len(counts) > limit:
        raise RuntimeError("duty strata exceed the sample limit; increase it within the hard cap")
    target = min(limit, sum(counts.values()))
    quotas = dict.fromkeys(counts, 1)
    remaining = target - len(quotas)
    while remaining:
        active = {
            duty: count - quotas[duty] for duty, count in counts.items() if count > quotas[duty]
        }
        if not active:
            break
        weight_total = sum(math.sqrt(counts[duty]) for duty in active)
        raw = {duty: remaining * math.sqrt(counts[duty]) / weight_total for duty in active}
        additions = {
            duty: min(capacity, math.floor(raw[duty])) for duty, capacity in active.items()
        }
        allocated = sum(additions.values())
        if allocated == 0:
            duty = min(active, key=lambda value: (-raw[value], value))
            additions[duty] = 1
            allocated = 1
        for duty, addition in additions.items():
            quotas[duty] += addition
        remaining -= allocated
    if sum(quotas.values()) != target or any(
        quotas[duty] > count for duty, count in counts.items()
    ):
        raise RuntimeError("deterministic sampling quota allocation failed")
    return quotas


def prepare_requests(
    *,
    jobs_csv: Path,
    split_manifest_path: Path,
    output: Path,
    duty_field: str,
    modified_at_field: str,
    source_timezone: str,
    sample_limit: int,
) -> dict[str, object]:
    if output.exists():
        raise RuntimeError("prepared extraction output already exists; builds never overwrite")
    split, cutoff = load_split_manifest(split_manifest_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f".{output.name}.{os.getpid()}.partial")
    if partial.exists():
        raise RuntimeError(f"partial extraction preparation already exists: {partial}")
    partial.mkdir()
    requests_path = partial / "requests.jsonl"
    try:
        source_zone = _iana_timezone(source_timezone)
        csv.field_size_limit(64 * 1024 * 1024)
        dataset_sha256 = sha256_file(jobs_csv)
        required = {
            JOB_ID_FIELD,
            duty_field,
            modified_at_field,
            *(label for label, _field in FULL_JOB_FIELDS),
        }
        split_sha256 = sha256_file(split_manifest_path)
        seen: set[str] = set()
        eligible_counts: Counter[str] = Counter()
        post_cutoff_skipped = 0
        empty_duty_skipped = 0
        eligible_maximum = None
        with jobs_csv.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames is None or not required.issubset(reader.fieldnames):
                missing = sorted(required.difference(reader.fieldnames or ()))
                raise RuntimeError(f"source CSV is missing extraction fields: {missing}")
            for line_number, row in enumerate(reader, start=2):
                job_id = canonical_text(row[JOB_ID_FIELD])
                if not job_id.isascii() or not job_id.isdecimal() or job_id in seen:
                    raise RuntimeError(
                        f"source CSV line {line_number} has invalid/duplicate job_id"
                    )
                seen.add(job_id)
                modified = _source_timestamp(
                    row[modified_at_field], "source modified timestamp", source_zone
                )
                if modified >= cutoff:
                    post_cutoff_skipped += 1
                    continue
                duty = _duty_stratum(row[duty_field])
                if not duty:
                    empty_duty_skipped += 1
                    continue
                eligible_counts[duty] += 1
                eligible_maximum = max(eligible_maximum, modified) if eligible_maximum else modified
        if not eligible_counts or eligible_maximum is None:
            raise RuntimeError("time cutoff produced no train JDs")
        quotas = _sample_quotas(eligible_counts, sample_limit)
        heaps: dict[str, list[tuple[int, str, dict[str, object]]]] = {duty: [] for duty in quotas}
        with jobs_csv.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            for row in reader:
                modified = _source_timestamp(
                    row[modified_at_field], "source modified timestamp", source_zone
                )
                if modified >= cutoff:
                    continue
                job_id = canonical_text(row[JOB_ID_FIELD])
                duty = _duty_stratum(row[duty_field])
                if not duty:
                    continue
                values = {field: row[label] for label, field in FULL_JOB_FIELDS}
                source_text = serialize_full_job(values)
                if not source_text:
                    raise RuntimeError(f"train JD {job_id} has empty source text")
                source_sha = hashlib.sha256(source_text.encode()).hexdigest()
                record_id = hashlib.sha256(f"{job_id}\0{source_sha}".encode()).hexdigest()
                request: dict[str, object] = {
                    "record_id": record_id,
                    "job_id": job_id,
                    "duty": duty,
                    "source_modified_at": modified.isoformat(),
                    "source_text": source_text,
                    "source_text_sha256": source_sha,
                }
                priority = int.from_bytes(
                    hashlib.sha256(f"{split_sha256}\0{job_id}\0{source_sha}".encode()).digest(),
                    "big",
                )
                entry: tuple[int, str, dict[str, object]] = (-priority, job_id, request)
                heap = heaps[duty]
                if len(heap) < quotas[duty]:
                    heapq.heappush(heap, entry)
                elif entry[:2] > heap[0][:2]:
                    heapq.heapreplace(heap, entry)
        selected = sorted(
            (
                (-negative_priority, job_id, request)
                for heap in heaps.values()
                for negative_priority, job_id, request in heap
            ),
            key=lambda item: (canonical_code(cast(str, item[2]["duty"])), item[0], item[1]),
        )
        maximum = max(
            _timestamp(request["source_modified_at"], "selected source timestamp")
            for _priority, _job_id, request in selected
        )
        with requests_path.open("wb") as target:
            for _priority, _job_id, request in selected:
                target.write(canonical_json(request) + b"\n")
            target.flush()
            os.fsync(target.fileno())
        if sha256_file(jobs_csv) != dataset_sha256:
            raise RuntimeError("source CSV bytes changed during deterministic sampling")
        selected_ids = [request["record_id"] for _priority, _job_id, request in selected]
        strata = [
            {"duty": duty, "eligible": eligible_counts[duty], "selected": quotas[duty]}
            for duty in sorted(quotas)
        ]
        manifest = {
            "schema_version": 1,
            "complete": True,
            "source_policy": "train_jd_only",
            "split_id": split["split_id"],
            "split_manifest_sha256": split_sha256,
            "train_cutoff_exclusive": cutoff.isoformat(),
            "source_jd_sha256": dataset_sha256,
            "document_policy_version": DOCUMENT_POLICY_VERSION,
            "document_template_sha256": document_template_sha256(),
            "document_fields": [label for label, _field in FULL_JOB_FIELDS],
            "job_id_field": JOB_ID_FIELD,
            "duty_field": duty_field,
            "modified_at_field": modified_at_field,
            "source_timezone": source_timezone,
            "source_timestamp_policy": SOURCE_TIMESTAMP_POLICY,
            "sampling_policy": SAMPLING_POLICY,
            "sample_limit": sample_limit,
            "eligible_train_records": sum(eligible_counts.values()),
            "eligible_max_source_timestamp": eligible_maximum.isoformat(),
            "strata": strata,
            "sampling_sha256": hashlib.sha256(canonical_json(selected_ids)).hexdigest(),
            "records": len(selected),
            "post_cutoff_skipped": post_cutoff_skipped,
            "empty_duty_skipped": empty_duty_skipped,
            "max_source_timestamp": maximum.isoformat(),
            "requests_sha256": sha256_file(requests_path),
        }
        atomic_json(partial / "manifest.json", manifest)
        _prepared(partial, split_manifest_path)
        partial.replace(output)
        return manifest
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise


def _iter_requests(path: Path) -> Iterator[dict[str, Any]]:
    seen: set[str] = set()
    seen_jobs: set[str] = set()
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError(f"invalid extraction request at line {line_number}") from error
            if not isinstance(raw, dict):
                raise RuntimeError("extraction request must be an object")
            exact_keys(raw, REQUEST_KEYS, f"extraction request {line_number}")
            record_id = raw["record_id"]
            job_id = raw["job_id"]
            duty = raw["duty"]
            source_text = raw["source_text"]
            source_sha = raw["source_text_sha256"]
            if (
                not isinstance(record_id, str)
                or len(record_id) != 64
                or any(character not in "0123456789abcdef" for character in record_id)
                or record_id in seen
                or not isinstance(job_id, str)
                or not job_id.isascii()
                or not job_id.isdecimal()
                or job_id in seen_jobs
                or not isinstance(duty, str)
                or not duty.strip()
                or not isinstance(source_text, str)
                or not source_text
                or not isinstance(source_sha, str)
                or hashlib.sha256(source_text.encode()).hexdigest() != source_sha
                or hashlib.sha256(f"{job_id}\0{source_sha}".encode()).hexdigest() != record_id
                or _timestamp(raw["source_modified_at"], "request source timestamp").isoformat()
                != raw["source_modified_at"]
            ):
                raise RuntimeError("extraction request lineage differs")
            seen.add(record_id)
            seen_jobs.add(job_id)
            yield raw
    if not seen:
        raise RuntimeError("extraction requests are empty")


def _prepared(output: Path, split_manifest_path: Path) -> dict[str, object]:
    split, cutoff = load_split_manifest(split_manifest_path)
    manifest = read_json_object(output / "manifest.json", "prepared extraction manifest")
    exact_keys(manifest, PREPARE_KEYS, "prepared extraction manifest")
    requests_path = output / "requests.jsonl"
    expected = {
        "schema_version": 1,
        "complete": True,
        "source_policy": "train_jd_only",
        "split_id": split["split_id"],
        "split_manifest_sha256": sha256_file(split_manifest_path),
        "train_cutoff_exclusive": cutoff.isoformat(),
        "document_policy_version": DOCUMENT_POLICY_VERSION,
        "document_template_sha256": document_template_sha256(),
        "document_fields": [label for label, _field in FULL_JOB_FIELDS],
        "job_id_field": JOB_ID_FIELD,
        "source_timestamp_policy": SOURCE_TIMESTAMP_POLICY,
    }
    if any(manifest[name] != value for name, value in expected.items()):
        raise RuntimeError("prepared extraction policy or split differs")
    for name in ("source_jd_sha256", "requests_sha256", "sampling_sha256"):
        require_sha256(manifest[name], f"prepared extraction {name}")
    if (
        not isinstance(manifest["duty_field"], str)
        or not manifest["duty_field"]
        or not isinstance(manifest["modified_at_field"], str)
        or not manifest["modified_at_field"]
        or not isinstance(manifest["source_timezone"], str)
        or isinstance(manifest["post_cutoff_skipped"], bool)
        or not isinstance(manifest["post_cutoff_skipped"], int)
        or manifest["post_cutoff_skipped"] < 0
        or isinstance(manifest["empty_duty_skipped"], bool)
        or not isinstance(manifest["empty_duty_skipped"], int)
        or manifest["empty_duty_skipped"] < 0
    ):
        raise RuntimeError("prepared extraction source field contract differs")
    _iana_timezone(manifest["source_timezone"])
    if sha256_file(requests_path) != manifest["requests_sha256"]:
        raise RuntimeError("prepared extraction request bytes differ")
    sample_limit = manifest["sample_limit"]
    eligible = manifest["eligible_train_records"]
    declared_records = manifest["records"]
    if (
        isinstance(sample_limit, bool)
        or not isinstance(sample_limit, int)
        or not 1 <= sample_limit <= MAX_SAMPLE_LIMIT
        or isinstance(eligible, bool)
        or not isinstance(eligible, int)
        or isinstance(declared_records, bool)
        or not isinstance(declared_records, int)
        or declared_records < 1
        or manifest["sampling_policy"] != SAMPLING_POLICY
    ):
        raise RuntimeError("prepared extraction record count differs")
    strata = manifest["strata"]
    if not isinstance(strata, list) or not strata:
        raise RuntimeError("prepared extraction strata are missing")
    actual: Counter[str] = Counter()
    selected_hasher = hashlib.sha256()
    selected_hasher.update(b"[")
    request_count = 0
    maximum: datetime | None = None
    for row in _iter_requests(requests_path):
        if request_count:
            selected_hasher.update(b",")
        selected_hasher.update(canonical_json(row["record_id"]))
        request_count += 1
        actual[canonical_code(row["duty"])] += 1
        modified = _timestamp(row["source_modified_at"], "request timestamp")
        maximum = max(maximum, modified) if maximum else modified
    selected_hasher.update(b"]")
    if (
        declared_records != request_count
        or eligible < request_count
        or request_count > sample_limit
        or maximum is None
    ):
        raise RuntimeError("prepared extraction record count differs")
    declared_eligible = 0
    declared_selected = 0
    for position, value in enumerate(strata):
        if not isinstance(value, dict):
            raise RuntimeError("prepared extraction stratum must be an object")
        exact_keys(value, {"duty", "eligible", "selected"}, f"sampling stratum {position}")
        duty, count, selected = value["duty"], value["eligible"], value["selected"]
        if (
            not isinstance(duty, str)
            or canonical_code(duty) != duty
            or isinstance(count, bool)
            or not isinstance(count, int)
            or isinstance(selected, bool)
            or not isinstance(selected, int)
            or not 1 <= selected <= count
            or actual[duty] != selected
        ):
            raise RuntimeError("prepared extraction stratum lineage differs")
        declared_eligible += count
        declared_selected += selected
    if (
        declared_eligible != eligible
        or declared_selected != request_count
        or selected_hasher.hexdigest() != manifest["sampling_sha256"]
    ):
        raise RuntimeError("prepared extraction sampling attestation differs")
    eligible_maximum = _timestamp(
        manifest["eligible_max_source_timestamp"], "eligible train maximum"
    )
    if (
        maximum.isoformat() != manifest["max_source_timestamp"]
        or maximum >= cutoff
        or eligible_maximum >= cutoff
        or maximum > eligible_maximum
    ):
        raise RuntimeError("prepared extraction contains non-train evidence")
    return manifest


def _tool_input(response: Mapping[str, object]) -> tuple[dict[str, object], int, int]:
    output = response.get("output")
    usage = response.get("usage")
    if (
        response.get("stopReason") != "tool_use"
        or not isinstance(output, dict)
        or not isinstance(usage, dict)
    ):
        raise RuntimeError("Bedrock response did not stop for the required toolUse")
    message = output.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise RuntimeError("Bedrock toolUse response message differs")
    content = message.get("content")
    if (
        not isinstance(content, list)
        or len(content) != 1
        or not isinstance(content[0], dict)
        or set(content[0]) != {"toolUse"}
    ):
        raise RuntimeError("Bedrock response must contain exactly one toolUse block")
    tool_use = content[0]["toolUse"]
    if (
        not isinstance(tool_use, dict)
        or set(tool_use) != {"toolUseId", "name", "type", "input"}
        or not isinstance(tool_use["toolUseId"], str)
        or not tool_use["toolUseId"]
        or tool_use["name"] != TOOL_NAME
        or tool_use["type"] != TOOL_USE_TYPE
        or not isinstance(tool_use["input"], dict)
    ):
        raise RuntimeError("Bedrock response toolUse contract differs")
    input_tokens, output_tokens = usage.get("inputTokens"), usage.get("outputTokens")
    if (
        isinstance(input_tokens, bool)
        or not isinstance(input_tokens, int)
        or input_tokens < 0
        or isinstance(output_tokens, bool)
        or not isinstance(output_tokens, int)
        or output_tokens < 0
    ):
        raise RuntimeError("Bedrock response toolUse usage differs")
    return tool_use["input"], input_tokens, output_tokens


def _validated_payload(
    payload: Mapping[str, object], source_text: str
) -> tuple[list[dict[str, str]], list[dict[str, str]], int, int]:
    exact_keys(payload, {"skills", "relations"}, "Bedrock extraction payload")
    raw_skills, raw_relations = payload["skills"], payload["relations"]
    if not isinstance(raw_skills, list) or not isinstance(raw_relations, list):
        raise RuntimeError("Bedrock skills and relations must be arrays")
    skills: list[dict[str, str]] = []
    names: set[str] = set()
    skill_rejections = 0
    for raw in raw_skills:
        if not isinstance(raw, dict) or set(raw) != {
            "canonical_name",
            "surface",
            "category",
            "evidence_span",
        }:
            skill_rejections += 1
            continue
        canonical = _normalize(raw["canonical_name"])
        surface, category, span = raw["surface"], raw["category"], raw["evidence_span"]
        if (
            not canonical
            or canonical in names
            or not isinstance(surface, str)
            or not surface
            or not isinstance(category, str)
            or not category.strip()
            or not isinstance(span, str)
            or not span
            or surface not in span
            or span not in source_text
        ):
            skill_rejections += 1
            continue
        names.add(canonical)
        skills.append(
            {
                "canonical_name": canonical,
                "surface": surface,
                "category": _normalize(category),
                "evidence_span": span,
            }
        )
    relations: list[dict[str, str]] = []
    seen_relations: set[tuple[str, str, str]] = set()
    relation_rejections = 0
    for raw in raw_relations:
        if not isinstance(raw, dict) or set(raw) != {"source", "type", "target", "evidence_span"}:
            relation_rejections += 1
            continue
        source, target = _normalize(raw["source"]), _normalize(raw["target"])
        relation_type, span = raw["type"], raw["evidence_span"]
        identity = (source, str(relation_type), target)
        if (
            source == target
            or source not in names
            or target not in names
            or relation_type not in RELATION_TYPES
            or not isinstance(span, str)
            or not span
            or span not in source_text
            or identity in seen_relations
        ):
            relation_rejections += 1
            continue
        seen_relations.add(identity)
        relations.append(
            {"source": source, "type": str(relation_type), "target": target, "evidence_span": span}
        )
    return skills, relations, skill_rejections, relation_rejections


def _response(
    *, request: Mapping[str, object], model_id: str, bedrock: BedrockRuntime
) -> dict[str, object]:
    response = bedrock.converse(
        modelId=model_id,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "text": canonical_json(
                            {
                                "job_id": request["job_id"],
                                "duty": request["duty"],
                                "source_text": request["source_text"],
                            }
                        ).decode()
                    }
                ],
            }
        ],
        inferenceConfig={"temperature": 0, "maxTokens": BEDROCK_MAX_OUTPUT_TOKENS},
        toolConfig=TOOL_CONFIG,
    )
    payload, input_tokens, output_tokens = _tool_input(response)
    skills, relations, skill_rejections, relation_rejections = _validated_payload(
        payload, cast(str, request["source_text"])
    )
    return {
        "schema_version": 1,
        "complete": True,
        "record_id": request["record_id"],
        "request_sha256": hashlib.sha256(canonical_json(request)).hexdigest(),
        "model_id": model_id,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": PROMPT_SHA256,
        "skills": skills,
        "relations": relations,
        "skill_rejection_count": skill_rejections,
        "relation_rejection_count": relation_rejections,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def _validate_response(
    value: Mapping[str, object], *, request: Mapping[str, object], model_id: str
) -> dict[str, object]:
    exact_keys(value, RESPONSE_KEYS, "Bedrock extraction response")
    expected = {
        "schema_version": 1,
        "complete": True,
        "record_id": request["record_id"],
        "request_sha256": hashlib.sha256(canonical_json(request)).hexdigest(),
        "model_id": model_id,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": PROMPT_SHA256,
    }
    if any(value[name] != item for name, item in expected.items()):
        raise RuntimeError("resumable Bedrock response lineage differs")
    validated = _validated_payload(
        {"skills": value["skills"], "relations": value["relations"]},
        cast(str, request["source_text"]),
    )
    if (
        validated[2]
        or validated[3]
        or validated[0] != value["skills"]
        or validated[1] != value["relations"]
    ):
        raise RuntimeError("resumable Bedrock response evidence differs")
    for name in (
        "skill_rejection_count",
        "relation_rejection_count",
        "input_tokens",
        "output_tokens",
    ):
        count = value[name]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise RuntimeError(f"Bedrock response {name} is invalid")
    return dict(value)


def extract(
    *,
    prepared: Path,
    split_manifest_path: Path,
    output: Path,
    model_id: str,
    bedrock: BedrockRuntime,
) -> dict[str, object]:
    if not model_id.strip():
        raise ValueError("Bedrock model ID is required")
    source = _prepared(prepared, split_manifest_path)
    records = source["records"]
    if isinstance(records, bool) or not isinstance(records, int) or records > MAX_SAMPLE_LIMIT:
        raise RuntimeError("Bedrock extraction request count exceeds the hard safety cap")
    output.mkdir(parents=True, exist_ok=True)
    responses_dir = output / "responses"
    responses_dir.mkdir(exist_ok=True)
    evidence_path = output / "evidence.jsonl"
    temporary = evidence_path.with_name(f".{evidence_path.name}.{os.getpid()}.partial")
    if temporary.exists():
        raise RuntimeError(f"partial extraction evidence already exists: {temporary}")
    inventory_hasher = hashlib.sha256()
    inventory_hasher.update(b"[")
    input_tokens = output_tokens = skill_rejections = relation_rejections = 0
    processed_records = 0
    try:
        with temporary.open("xb") as evidence_output:
            for request in _iter_requests(prepared / "requests.jsonl"):
                response_path = responses_dir / f"{request['record_id']}.json"
                if response_path.exists():
                    response = _validate_response(
                        read_json_object(response_path, "Bedrock extraction response"),
                        request=request,
                        model_id=model_id,
                    )
                else:
                    response = _response(request=request, model_id=model_id, bedrock=bedrock)
                    atomic_json(response_path, response)
                inventory_entry = {
                    "path": response_path.relative_to(output).as_posix(),
                    "sha256": sha256_file(response_path),
                }
                if processed_records:
                    inventory_hasher.update(b",")
                inventory_hasher.update(canonical_json(inventory_entry))
                input_tokens += cast(int, response["input_tokens"])
                output_tokens += cast(int, response["output_tokens"])
                skill_rejections += cast(int, response["skill_rejection_count"])
                relation_rejections += cast(int, response["relation_rejection_count"])
                evidence_output.write(
                    canonical_json(
                        {
                            **request,
                            "skills": response["skills"],
                            "relations": response["relations"],
                            "skill_rejection_count": response["skill_rejection_count"],
                            "relation_rejection_count": response["relation_rejection_count"],
                        }
                    )
                    + b"\n"
                )
                processed_records += 1
            evidence_output.flush()
            os.fsync(evidence_output.fileno())
        if processed_records != records:
            raise RuntimeError("streamed extraction record count differs")
        if evidence_path.exists():
            if evidence_path.stat().st_size != temporary.stat().st_size or sha256_file(
                evidence_path
            ) != sha256_file(temporary):
                raise RuntimeError("sealed extraction evidence differs from resumable responses")
            temporary.unlink()
        else:
            temporary.replace(evidence_path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    inventory_hasher.update(b"]")
    inventory_sha256 = inventory_hasher.hexdigest()
    manifest = {
        "schema_version": 1,
        "complete": True,
        "model_id": model_id,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": PROMPT_SHA256,
        "canonicalization_policy": CANONICALIZATION_POLICY,
        "oov_policy": OOV_POLICY,
        "source_policy": "train_jd_only",
        "test_jd_used": False,
        "uses_ground_truth": False,
        "uses_behavior_logs": False,
        "train_cutoff_exclusive": source["train_cutoff_exclusive"],
        "max_source_timestamp": source["max_source_timestamp"],
        "source_jd_sha256": source["source_jd_sha256"],
        "requests_sha256": source["requests_sha256"],
        "responses_inventory_sha256": inventory_sha256,
        "evidence_sha256": sha256_file(evidence_path),
        "sampling_policy": source["sampling_policy"],
        "sample_limit": source["sample_limit"],
        "eligible_train_records": source["eligible_train_records"],
        "sampling_sha256": source["sampling_sha256"],
        "source_records": source["records"],
        "processed_records": processed_records,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "skill_rejections": skill_rejections,
        "relation_rejections": relation_rejections,
    }
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        if read_json_object(manifest_path, "LLM extraction manifest") != manifest:
            raise RuntimeError("sealed LLM extraction manifest differs")
    else:
        atomic_json(manifest_path, manifest)
    return manifest


def _bedrock(profile: str | None, region: str, expected_account: str) -> BedrockRuntime:
    if not expected_account.isdecimal() or len(expected_account) != 12:
        raise ValueError("expected AWS account must be 12 digits")
    session = boto3.Session(profile_name=profile, region_name=region)
    identity = cast(AwsIdentity, session.client("sts")).get_caller_identity()
    if identity.get("Account") != expected_account:
        raise RuntimeError("AWS caller identity differs from approved Bedrock account")
    return cast(
        BedrockRuntime,
        session.client(
            "bedrock-runtime",
            config=Config(
                retries={"mode": "standard", "total_max_attempts": BEDROCK_TOTAL_MAX_ATTEMPTS},
                connect_timeout=10,
                read_timeout=120,
                max_pool_connections=1,
            ),
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--jobs-csv", type=Path, required=True)
    prepare.add_argument("--split-manifest", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--duty-field", default=DEFAULT_DUTY_FIELD)
    prepare.add_argument("--modified-at-field", default=DEFAULT_MODIFIED_AT_FIELD)
    prepare.add_argument("--source-timezone", required=True)
    prepare.add_argument("--max-records", type=int, default=DEFAULT_SAMPLE_LIMIT)
    run = commands.add_parser("extract")
    run.add_argument("--prepared", type=Path, required=True)
    run.add_argument("--split-manifest", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--model-id", required=True)
    run.add_argument("--profile")
    run.add_argument("--region", default="us-west-2")
    run.add_argument("--expected-account", default="378849533305")
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare_requests(
            jobs_csv=args.jobs_csv,
            split_manifest_path=args.split_manifest,
            output=args.output,
            duty_field=args.duty_field,
            modified_at_field=args.modified_at_field,
            source_timezone=args.source_timezone,
            sample_limit=args.max_records,
        )
    else:
        result = extract(
            prepared=args.prepared,
            split_manifest_path=args.split_manifest,
            output=args.output,
            model_id=args.model_id,
            bedrock=_bedrock(args.profile, args.region, args.expected_account),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
