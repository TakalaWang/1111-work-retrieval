#!/usr/bin/env python3
"""Promote one verified, immutable retrieval release to the runtime bucket."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from time import sleep
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

AWS_ACCOUNT = "378849533305"
AWS_PROFILE = "competition"
AWS_REGION = "us-west-2"
SOURCE_BUCKET = "jobbank-data-bucket"
DESTINATION_BUCKET = "workretrievaldata-runtimebucket404c5ee4-hkvrjx5fbkij"

APPROVED_WHOLE_MANIFEST_SHA256 = "a02a23655fe8e5cc6b08afde35e93898ff94c62b88bbf7522e09f2c15378715c"
APPROVED_MODEL = "Qwen/Qwen3-Embedding-8B"
APPROVED_MODEL_REVISION = "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"
APPROVED_WHOLE_DIMENSION = 4096
APPROVED_MULTIVIEW_DIMENSION = 1024
APPROVED_MULTIVIEW_KINDS = ["occupation", "skill", "requirement", "content"]
DEMO_AS_OF = "2026-06-08T23:59:59.999+08:00"
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
    "query_neighbor_history",
    "behavior_prior",
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


def validate_relative_path(path: str, kind: str | None = None) -> None:
    candidate = PurePosixPath(path)
    raw_parts = path.split("/")
    root = ARTIFACT_ROOTS.get(kind) if kind is not None else None
    if (
        not path
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in raw_parts)
        or candidate.parts[0] not in set(ARTIFACT_ROOTS.values())
        or (root is not None and candidate.parts[0] != root)
        or (candidate.parts[0] == "evidence" and not path.endswith(".json"))
    ):
        raise RuntimeError(f"unsafe runtime artifact path: {path!r}")


def _validate_source_path(path: str) -> None:
    parts = path.split("/")
    if not path or path.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise RuntimeError(f"unsafe source artifact path: {path!r}")


def canonical_bytes(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


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
        validate_relative_path(f"{destination_prefix}placeholder", kind)
        rules.append(cast(dict[str, str], raw))
    for index, first in enumerate(rules):
        for second in rules[index + 1 :]:
            if first["source_prefix"].startswith(second["source_prefix"]) or second[
                "source_prefix"
            ].startswith(first["source_prefix"]):
                raise RuntimeError("release selection source prefixes overlap")
            if first["destination_prefix"].startswith(second["destination_prefix"]) or second[
                "destination_prefix"
            ].startswith(first["destination_prefix"]):
                raise RuntimeError("release selection destination prefixes overlap")
    return rules


def select_artifacts(
    source: Mapping[str, object], spec: Mapping[str, object]
) -> list[dict[str, object]]:
    inventory = _parse_source_manifest(source)
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
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"component manifest is invalid JSON: {path}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"component manifest is not an object: {path}")
    return cast(dict[str, object], value)


def _require_equal(
    component: str,
    expected: Mapping[str, object],
    actual: Mapping[str, object],
) -> None:
    mismatches = [key for key, value in expected.items() if actual.get(key) != value]
    if mismatches:
        raise RuntimeError(f"{component} component manifest differs in: {', '.join(mismatches)}")


def _validate_component_manifests(
    manifest: Mapping[str, object], documents: Mapping[str, bytes]
) -> None:
    artifacts = cast(Mapping[str, object], manifest["artifacts"])
    incumbents = cast(Mapping[str, Mapping[str, object]], manifest["incumbents"])
    whole = incumbents["whole_embedding"]
    whole_path = _artifact_reference(artifacts, whole, "embedding")
    if whole.get("manifest_sha256") != APPROVED_WHOLE_MANIFEST_SHA256:
        raise RuntimeError("whole embedding manifest is not the verified production cache")
    whole_document = _json_document(whole_path, artifacts, documents)
    shards = whole_document.get("shards")
    dimensions = (
        {shard.get("dimension") for shard in shards if isinstance(shard, dict)}
        if isinstance(shards, list)
        else set()
    )
    if dimensions != {APPROVED_WHOLE_DIMENSION}:
        raise RuntimeError("whole embedding component dimensions differ")
    _require_equal(
        "whole embedding",
        {
            "complete": True,
            "model": APPROVED_MODEL,
            "revision": APPROVED_MODEL_REVISION,
            "dtype": "float16",
            "normalized": True,
            "rows": whole.get("rows"),
            "dataset_sha256": whole.get("dataset_sha256"),
            "jobs_sha256": whole.get("jobs_sha256"),
            "job_row_order_sha256": whole.get("job_row_order_sha256"),
            "document_policy_version": whole.get("document_policy_version"),
            "document_template_sha256": whole.get("document_template_sha256"),
        },
        whole_document,
    )
    if not any(
        isinstance(value, dict) and value.get("kind") == "model" for value in artifacts.values()
    ):
        raise RuntimeError("verified Qwen model snapshot is missing")

    temporal = incumbents["temporal_tantivy"]
    temporal_path = _artifact_reference(artifacts, temporal, "index")
    temporal_document = _json_document(temporal_path, artifacts, documents)
    _require_equal(
        "temporal Tantivy",
        {
            "complete": True,
            "engine": temporal.get("engine"),
            "jobs_sha256": whole.get("jobs_sha256"),
            "job_row_order_sha256": whole.get("job_row_order_sha256"),
            "index_sha256": temporal.get("index_sha256"),
            "updated_at_field": "updated_at_epoch_ms",
            "temporal_filter_semantics": (
                "updated_at <= as_of AND updated_at >= as_of - 180 days before Top-K"
            ),
        },
        temporal_document,
    )
    filter_semantics = temporal_document.get("filter_semantics")
    if not isinstance(filter_semantics, str) or not all(
        token in filter_semantics for token in ("location", "duty", "before Top-K")
    ):
        raise RuntimeError("temporal Tantivy hard-filter contract differs")

    challengers = cast(Mapping[str, Mapping[str, object]], manifest["challengers"])
    multiview = challengers["multiview_embedding"]
    if multiview.get("enabled") is True:
        path = _artifact_reference(artifacts, multiview, "embedding")
        component = _json_document(path, artifacts, documents)
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
                "reference_dimension": APPROVED_WHOLE_DIMENSION,
            },
            component_evidence,
        )

    graph = challengers["skill_graph"]
    if graph.get("enabled") is True:
        path = _artifact_reference(artifacts, graph, "graph")
        component = _json_document(path, artifacts, documents)
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
            },
            component,
        )

    for name, challenger in challengers.items():
        if name in {"multiview_embedding", "skill_graph"} or challenger.get("enabled") is not True:
            continue
        kind = "ranker" if name in {"semantic_reranker", "learning_to_rank"} else "evidence"
        path = _artifact_reference(artifacts, challenger, kind)
        component = _json_document(path, artifacts, documents)
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


def _forbid_credentials(value: object) -> None:
    forbidden_keys = {"password", "secret", "token", "access_key", "secret_access_key"}
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in forbidden_keys or normalized.endswith(
                ("_password", "_secret", "_token", "_access_key")
            ):
                raise RuntimeError(f"runtime manifest contains forbidden credential field: {key}")
            _forbid_credentials(child)
    elif isinstance(value, list):
        for child in value:
            _forbid_credentials(child)


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


def _validate_promotion_evidence(evidence: object, artifacts: Mapping[str, object]) -> None:
    if not isinstance(evidence, dict):
        raise RuntimeError("enabled challenger requires promotion evidence")
    if (
        evidence.get("decision") != "accepted"
        or not isinstance(evidence.get("absolute_delta"), (int, float))
        or float(cast(float, evidence["absolute_delta"])) <= 0
    ):
        raise RuntimeError("enabled challenger promotion evidence was not accepted")
    report = {
        "manifest_path": evidence.get("report_path"),
        "manifest_sha256": evidence.get("report_sha256"),
    }
    _artifact_reference(artifacts, report, "evidence")


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
        "future_jobs": "exclude",
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
        validate_relative_path(path, kind)
        _require_sha256(f"artifact SHA-256 for {path}", raw.get("sha256"))
        if type(raw.get("size_bytes")) is not int or cast(int, raw["size_bytes"]) < 0:
            raise RuntimeError(f"runtime artifact size is invalid: {path}")
    graph = challengers["skill_graph"]
    if graph.get("enabled") is True:
        cutoff = _timestamp(graph.get("train_cutoff_exclusive"), "Graph train cutoff")
        demo_as_of = _timestamp(
            cast(Mapping[str, object], retrieval["as_of"]).get("demo_reference"),
            "Demo as_of",
        )
        if cutoff > demo_as_of:
            raise RuntimeError("Graph train cutoff must not exceed Demo as_of")
        max_source_timestamp = _timestamp(
            graph.get("max_source_timestamp"), "Graph maximum source timestamp"
        )
        if max_source_timestamp >= cutoff:
            raise RuntimeError("Graph source timestamp must precede its exclusive train cutoff")
        _require_sha256("Graph source JD SHA-256", graph.get("source_jd_sha256"))
        if graph.get("source_policy") != "train_jd_only" or graph.get("test_jd_used") is not False:
            raise RuntimeError("Graph is not proven train-only")
    for name, challenger in challengers.items():
        enabled = challenger.get("enabled")
        if enabled is False:
            if set(challenger) != {"enabled"}:
                raise RuntimeError(f"disabled challenger carries unverified metadata: {name}")
            continue
        if enabled is not True:
            raise RuntimeError(f"challenger enabled flag is invalid: {name}")
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
                or evidence.get("reference_dimension") != APPROVED_WHOLE_DIMENSION
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
        else:
            _validate_promotion_evidence(challenger.get("promotion_evidence"), artifacts)
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
    _forbid_credentials(manifest)
    _validate_component_manifests(manifest, documents)
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
        value = json.loads(payload)
    except json.JSONDecodeError as error:
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


def _component_paths(runtime: Mapping[str, object]) -> set[str]:
    try:
        incumbents = cast(Mapping[str, Mapping[str, object]], runtime["incumbents"])
        challengers = cast(Mapping[str, Mapping[str, object]], runtime["challengers"])
    except (KeyError, TypeError) as error:
        raise RuntimeError("release runtime component contract is incomplete") from error
    paths = {cast(str, value["manifest_path"]) for value in incumbents.values()}
    paths.update(
        cast(str, value["manifest_path"])
        for value in challengers.values()
        if value.get("enabled") is True
    )
    return paths


def load_component_documents(
    spec: Mapping[str, object],
    items: Sequence[Mapping[str, object]],
    local_root: Path | None = None,
) -> dict[str, bytes]:
    runtime = spec.get("runtime")
    if not isinstance(runtime, dict):
        raise RuntimeError("release spec runtime contract is missing")
    paths = _component_paths(runtime)
    by_destination = {cast(str, item["path"]): item for item in items}
    source_key, _ = _source_manifest_contract(spec)
    source_root = source_key.removesuffix("manifest.json")
    documents: dict[str, bytes] = {}
    for path in paths:
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


def _list_destination(prefix: str) -> dict[str, int]:
    continuation: str | None = None
    result: dict[str, int] = {}
    while True:
        arguments = [
            "s3api",
            "list-objects-v2",
            "--bucket",
            DESTINATION_BUCKET,
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
        help="offline dry-run source inventory; forbidden with --execute",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        help="offline dry-run artifact root; forbidden with --execute",
    )
    parser.add_argument("--execute", action="store_true", help="perform server-side S3 copies")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    if (args.source_manifest_file is None) != (args.source_root is None):
        raise RuntimeError("offline dry-run requires both --source-manifest-file and --source-root")
    if args.execute and args.source_manifest_file is not None:
        raise RuntimeError("--execute requires the pinned S3 source manifest")
    spec, release_spec_sha = load_release_spec(args.release_spec)
    if args.source_manifest_file is None:
        verify_account()
    source = load_source_manifest(spec, args.source_manifest_file)
    items = select_artifacts(source, spec)
    documents = load_component_documents(spec, items, args.source_root)
    manifest, items = build_manifest(source, spec, documents, release_spec_sha)
    payload = canonical_bytes(manifest)
    manifest_sha = hashlib.sha256(payload).hexdigest()
    source_key, _ = _source_manifest_contract(spec)
    if args.execute:
        publish_release(items, payload, manifest_sha, source_key.removesuffix("manifest.json"))
    print(
        json.dumps(
            {
                "executed": args.execute,
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
