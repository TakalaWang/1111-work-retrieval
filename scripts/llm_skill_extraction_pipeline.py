#!/usr/bin/env python3
"""Prepare train-only JDs and resumably extract evidence-locked skills with Bedrock."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import unicodedata
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, cast

import boto3  # type: ignore[import-untyped]
from pipeline_contract import (
    atomic_json,
    canonical_json,
    exact_keys,
    read_json_object,
    sha256_file,
)
from skill_graph_pipeline import load_split_manifest
from work_retrieval_core.serialization import (
    DOCUMENT_POLICY_VERSION,
    FULL_JOB_FIELDS,
    canonical_text,
    document_template_sha256,
    serialize_full_job,
)

PROMPT_VERSION = "2026-08-01-open-surface-evidence-v1"
CANONICALIZATION_POLICY = "open_surface_per_jd_llm_canonicalization_v1"
OOV_POLICY = "accept_open_surface_with_exact_train_jd_evidence"
SYSTEM_PROMPT = """You extract a train-only job skill graph from exactly one supplied JD.
Return one JSON object with exactly two arrays: skills and relations.
Each skill has canonical_name, surface, category, evidence_span.
Each relation has source, type, target, evidence_span; type is one of USED_WITH,
SPECIALIZATION_OF, RELATED_TO. surface must be an exact substring of evidence_span and both must
be exact substrings of the supplied JD. Use an open vocabulary: preserve a new/OOV surface.
canonical_name normalizes only a genuine spelling variant or synonym evidenced within this JD.
Never infer unsupported skills, and never use query logs, qrels, behavior, test data, or outside
facts. Return JSON only."""
PROMPT_SHA256 = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()
JOB_ID_FIELD = "職缺編號"
DEFAULT_DUTY_FIELD = "職務小類"
DEFAULT_MODIFIED_AT_FIELD = "更新日期"
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
    "records",
    "post_cutoff_skipped",
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
RELATION_TYPES = {"USED_WITH", "SPECIALIZATION_OF", "RELATED_TO"}


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


def prepare_requests(
    *,
    jobs_csv: Path,
    split_manifest_path: Path,
    output: Path,
    duty_field: str,
    modified_at_field: str,
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
        csv.field_size_limit(64 * 1024 * 1024)
        required = {
            JOB_ID_FIELD,
            duty_field,
            modified_at_field,
            *(label for label, _field in FULL_JOB_FIELDS),
        }
        seen: set[str] = set()
        records = 0
        skipped = 0
        maximum = None
        with (
            jobs_csv.open(encoding="utf-8-sig", newline="") as source,
            requests_path.open("wb") as target,
        ):
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
                modified = _timestamp(row[modified_at_field], "source modified timestamp")
                if modified >= cutoff:
                    skipped += 1
                    continue
                values = {field: row[label] for label, field in FULL_JOB_FIELDS}
                source_text = serialize_full_job(values)
                duty = canonical_text(row[duty_field])
                if not source_text or not duty:
                    raise RuntimeError(f"train JD {job_id} has empty source text or duty")
                source_sha = hashlib.sha256(source_text.encode()).hexdigest()
                record_id = hashlib.sha256(f"{job_id}\0{source_sha}".encode()).hexdigest()
                request = {
                    "record_id": record_id,
                    "job_id": job_id,
                    "duty": duty,
                    "source_modified_at": modified.isoformat(),
                    "source_text": source_text,
                    "source_text_sha256": source_sha,
                }
                target.write(canonical_json(request) + b"\n")
                records += 1
                maximum = max(maximum, modified) if maximum else modified
            target.flush()
            os.fsync(target.fileno())
        if not records or maximum is None:
            raise RuntimeError("time cutoff produced no train JDs")
        manifest = {
            "schema_version": 1,
            "complete": True,
            "source_policy": "train_jd_only",
            "split_id": split["split_id"],
            "split_manifest_sha256": sha256_file(split_manifest_path),
            "train_cutoff_exclusive": cutoff.isoformat(),
            "source_jd_sha256": sha256_file(jobs_csv),
            "document_policy_version": DOCUMENT_POLICY_VERSION,
            "document_template_sha256": document_template_sha256(),
            "document_fields": [label for label, _field in FULL_JOB_FIELDS],
            "job_id_field": JOB_ID_FIELD,
            "duty_field": duty_field,
            "modified_at_field": modified_at_field,
            "records": records,
            "post_cutoff_skipped": skipped,
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


def _requests(path: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    seen: set[str] = set()
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
            source_text = raw["source_text"]
            if (
                not isinstance(record_id, str)
                or len(record_id) != 64
                or record_id in seen
                or not isinstance(job_id, str)
                or not job_id.isascii()
                or not job_id.isdecimal()
                or not isinstance(source_text, str)
                or not source_text
                or hashlib.sha256(source_text.encode()).hexdigest() != raw["source_text_sha256"]
                or _timestamp(raw["source_modified_at"], "request source timestamp").isoformat()
                != raw["source_modified_at"]
            ):
                raise RuntimeError("extraction request lineage differs")
            seen.add(record_id)
            values.append(raw)
    if not values:
        raise RuntimeError("extraction requests are empty")
    return values


def _prepared(
    output: Path, split_manifest_path: Path
) -> tuple[dict[str, object], list[dict[str, Any]]]:
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
    }
    if any(manifest[name] != value for name, value in expected.items()):
        raise RuntimeError("prepared extraction policy or split differs")
    if sha256_file(requests_path) != manifest["requests_sha256"]:
        raise RuntimeError("prepared extraction request bytes differ")
    requests = _requests(requests_path)
    if manifest["records"] != len(requests):
        raise RuntimeError("prepared extraction record count differs")
    maximum = max(_timestamp(row["source_modified_at"], "request timestamp") for row in requests)
    if maximum.isoformat() != manifest["max_source_timestamp"] or maximum >= cutoff:
        raise RuntimeError("prepared extraction contains non-train evidence")
    return manifest, requests


def _raw_json(response: Mapping[str, object]) -> tuple[dict[str, object], int, int]:
    output = response.get("output")
    usage = response.get("usage")
    if not isinstance(output, dict) or not isinstance(usage, dict):
        raise RuntimeError("Bedrock response is missing output or usage")
    message = output.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise RuntimeError("Bedrock response message differs")
    content = message.get("content")
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
        raise RuntimeError("Bedrock response must contain exactly one text block")
    text = content[0].get("text")
    input_tokens, output_tokens = usage.get("inputTokens"), usage.get("outputTokens")
    if (
        not isinstance(text, str)
        or isinstance(input_tokens, bool)
        or not isinstance(input_tokens, int)
        or input_tokens < 0
        or isinstance(output_tokens, bool)
        or not isinstance(output_tokens, int)
        or output_tokens < 0
    ):
        raise RuntimeError("Bedrock response text or usage differs")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError("Bedrock response is not strict JSON") from error
    if not isinstance(payload, dict):
        raise RuntimeError("Bedrock response JSON must be an object")
    return payload, input_tokens, output_tokens


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
        inferenceConfig={"temperature": 0, "maxTokens": 2048},
    )
    payload, input_tokens, output_tokens = _raw_json(response)
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
    source, requests = _prepared(prepared, split_manifest_path)
    output.mkdir(parents=True, exist_ok=True)
    responses_dir = output / "responses"
    responses_dir.mkdir(exist_ok=True)
    evidence_rows: list[dict[str, object]] = []
    response_inventory: list[dict[str, str]] = []
    input_tokens = output_tokens = skill_rejections = relation_rejections = 0
    for request in requests:
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
        response_inventory.append(
            {
                "path": response_path.relative_to(output).as_posix(),
                "sha256": sha256_file(response_path),
            }
        )
        input_tokens += cast(int, response["input_tokens"])
        output_tokens += cast(int, response["output_tokens"])
        skill_rejections += cast(int, response["skill_rejection_count"])
        relation_rejections += cast(int, response["relation_rejection_count"])
        evidence_rows.append(
            {
                **request,
                "skills": response["skills"],
                "relations": response["relations"],
                "skill_rejection_count": response["skill_rejection_count"],
                "relation_rejection_count": response["relation_rejection_count"],
            }
        )
    evidence_bytes = b"".join(canonical_json(row) + b"\n" for row in evidence_rows)
    evidence_path = output / "evidence.jsonl"
    if evidence_path.exists():
        if evidence_path.read_bytes() != evidence_bytes:
            raise RuntimeError("sealed extraction evidence differs from resumable responses")
    else:
        temporary = evidence_path.with_suffix(".jsonl.partial")
        with temporary.open("xb") as target:
            target.write(evidence_bytes)
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(evidence_path)
    inventory_sha256 = hashlib.sha256(canonical_json(response_inventory)).hexdigest()
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
        "source_records": source["records"],
        "processed_records": len(evidence_rows),
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
    return cast(BedrockRuntime, session.client("bedrock-runtime"))


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--jobs-csv", type=Path, required=True)
    prepare.add_argument("--split-manifest", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--duty-field", default=DEFAULT_DUTY_FIELD)
    prepare.add_argument("--modified-at-field", default=DEFAULT_MODIFIED_AT_FIELD)
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
