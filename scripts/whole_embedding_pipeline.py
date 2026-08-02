#!/usr/bin/env python3
"""Build the production whole-JD Qwen MRL artifact from the source CSV."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

import boto3  # type: ignore[import-untyped]
import numpy as np
import numpy.typing as npt
from multiview_embedding_pipeline import (
    MODEL,
    MODEL_DIMENSION,
    MODEL_REVISION,
    OUTPUT_DIMENSION,
    Encoder,
    local_encoder,
    parse_devices,
    sagemaker_encoder,
)
from pipeline_contract import (
    artifact_entry,
    atomic_json,
    canonical_json,
    exact_keys,
    publish_s3_directory,
    read_json_object,
    require_sha256,
    sha256_file,
    verify_local_inventory,
    verify_s3_inventory,
    verify_s3_object,
)
from work_retrieval_core.serialization import (
    DOCUMENT_POLICY_VERSION,
    FULL_JOB_FIELDS,
    document_template_sha256,
    serialize_full_job,
)

JOB_ID_FIELD = "職缺編號"
QUERY_PROMPT = (
    "Instruct: Given a job search query, retrieve relevant job postings matching the user's "
    "intent\nQuery: "
)
PROJECTION = "mrl_prefix_then_l2_normalize"
DEFAULT_ARTIFACT_PREFIX = "embeddings/qwen3-embedding-8b-full-jd-v2"
COMPONENT_KEYS = {
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
    "build_manifest_path",
    "build_manifest_sha256",
    "job_ids_path",
    "shards",
}
BUILD_KEYS = {
    "schema_version",
    "complete",
    "builder",
    "model",
    "revision",
    "tokenizer_sha256",
    "source_dimension",
    "selected_dimension",
    "projection",
    "dtype",
    "normalized",
    "encoder_backend",
    "encoder_identity",
    "dataset_sha256",
    "rows",
    "jobs_sha256",
    "job_row_order_sha256",
    "document_policy_version",
    "document_template_sha256",
    "document_fields",
    "shards",
}
STATE_KEYS = BUILD_KEYS - {"complete", "shards"}
SIDECAR_KEYS = {
    "schema_version",
    "complete",
    "vectors_path",
    "vectors_sha256",
    "size_bytes",
    "row_start",
    "row_end",
    "rows",
    "dimension",
    "source_job_ids_sha256",
}
BUILD_SHARD_KEYS = SIDECAR_KEYS | {"sidecar_path", "sidecar_sha256"}


class AwsIdentity(Protocol):
    def get_caller_identity(self) -> Mapping[str, object]: ...


def _source_header(reader: csv.DictReader[str]) -> None:
    required = {JOB_ID_FIELD, *(label for label, _field in FULL_JOB_FIELDS)}
    if reader.fieldnames is None or not required.issubset(reader.fieldnames):
        missing = sorted(required.difference(reader.fieldnames or ()))
        raise RuntimeError(f"source CSV is missing full-JD fields: {missing}")


def _source_rows(path: Path) -> Iterator[tuple[str, str]]:
    csv.field_size_limit(64 * 1024 * 1024)
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        _source_header(reader)
        for line_number, row in enumerate(reader, start=2):
            job_id = row[JOB_ID_FIELD]
            if not isinstance(job_id, str) or not job_id.isascii() or not job_id.isdecimal():
                raise RuntimeError(f"source CSV line {line_number} has an invalid job_id")
            values = {field: row[label] for label, field in FULL_JOB_FIELDS}
            document = serialize_full_job(values)
            if not document:
                raise RuntimeError(f"source CSV job {job_id} has an empty full-JD document")
            yield job_id, document


def _scan_source(path: Path) -> tuple[tuple[str, ...], str]:
    job_ids: list[str] = []
    seen: set[str] = set()
    order = hashlib.sha256()
    for job_id, _document in _source_rows(path):
        if job_id in seen:
            raise RuntimeError(f"source CSV contains duplicate job_id: {job_id}")
        seen.add(job_id)
        job_ids.append(job_id)
        order.update(job_id.encode() + b"\n")
    if not job_ids:
        raise RuntimeError("source CSV contains no jobs")
    return tuple(job_ids), order.hexdigest()


def _backend_identity(value: str) -> str:
    identity = value.strip()
    if not identity:
        raise ValueError("encoder identity must be non-empty")
    return identity


def _encoded(encoder: Encoder, documents: list[str]) -> npt.NDArray[np.float32]:
    values = np.asarray(encoder.encode(documents), dtype=np.float32)
    if values.shape != (len(documents), OUTPUT_DIMENSION) or not np.isfinite(values).all():
        raise RuntimeError("encoder output violates the 1024d MRL contract")
    norms = np.linalg.norm(values, axis=1)
    if not np.allclose(norms, 1.0, atol=2e-5, rtol=0):
        raise RuntimeError("encoder output is not independently L2-normalized after MRL prefixing")
    return values


def _source_job_ids_sha256(job_ids: tuple[str, ...]) -> str:
    return hashlib.sha256(canonical_json(list(job_ids)) + b"\n").hexdigest()


def _validate_vectors(path: Path, rows: int) -> None:
    try:
        values = np.load(path, mmap_mode="r", allow_pickle=False)
    except (OSError, ValueError) as error:
        raise RuntimeError(f"whole embedding shard cannot be opened: {path.name}") from error
    if values.shape != (rows, OUTPUT_DIMENSION) or values.dtype != np.float16:
        raise RuntimeError("whole embedding shard shape or dtype differs")
    for chunk_start in range(0, rows, 16_384):
        chunk = np.asarray(values[chunk_start : chunk_start + 16_384], dtype=np.float32)
        if not np.isfinite(chunk).all() or not np.allclose(
            np.linalg.norm(chunk, axis=1), 1.0, atol=2e-3, rtol=0
        ):
            raise RuntimeError("whole embedding shard contains invalid normalized vectors")


def _expected_sidecar(
    *,
    shard_path: Path,
    artifact_prefix: str,
    row_start: int,
    row_end: int,
    job_ids: tuple[str, ...],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "complete": True,
        "vectors_path": f"{artifact_prefix}/{shard_path.name}",
        "vectors_sha256": sha256_file(shard_path),
        "size_bytes": shard_path.stat().st_size,
        "row_start": row_start,
        "row_end": row_end,
        "rows": row_end - row_start,
        "dimension": OUTPUT_DIMENSION,
        "source_job_ids_sha256": _source_job_ids_sha256(job_ids[row_start:row_end]),
    }


def _resume_shard(
    *,
    shard_path: Path,
    sidecar_path: Path,
    artifact_prefix: str,
    row_start: int,
    row_end: int,
    job_ids: tuple[str, ...],
) -> dict[str, object] | None:
    temporary = shard_path.with_suffix(shard_path.suffix + ".partial")
    temporary.unlink(missing_ok=True)
    if not shard_path.exists() and not sidecar_path.exists():
        return None
    if not shard_path.is_file():
        raise RuntimeError("resumable shard sidecar exists without vector bytes")
    _validate_vectors(shard_path, row_end - row_start)
    expected = _expected_sidecar(
        shard_path=shard_path,
        artifact_prefix=artifact_prefix,
        row_start=row_start,
        row_end=row_end,
        job_ids=job_ids,
    )
    if sidecar_path.exists():
        sidecar = read_json_object(sidecar_path, "whole embedding shard sidecar")
        exact_keys(sidecar, SIDECAR_KEYS, "whole embedding shard sidecar")
        if sidecar != expected:
            raise RuntimeError("resumable shard bytes or source slice differ from sidecar")
    else:
        atomic_json(sidecar_path, expected)
    return expected


def build_whole_embeddings(
    *,
    jobs_csv: Path,
    output: Path,
    tokenizer_sha256: str,
    encoder: Encoder,
    encoder_backend: str,
    encoder_identity: str,
    artifact_prefix: str,
    shard_size: int,
    batch_size: int,
) -> dict[str, object]:
    require_sha256(tokenizer_sha256, "tokenizer SHA-256")
    clean_prefix = PurePosixPath(artifact_prefix)
    if clean_prefix.is_absolute() or ".." in clean_prefix.parts or len(clean_prefix.parts) < 2:
        raise ValueError("artifact prefix must be a safe runtime-relative path")
    if shard_size < 1 or batch_size < 1:
        raise ValueError("shard_size and batch_size must be positive")
    if output.exists():
        raise RuntimeError("whole embedding output already exists; builds never overwrite")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f".{output.name}.partial")
    partial.mkdir(exist_ok=True)
    try:
        dataset_sha256 = sha256_file(jobs_csv)
        job_ids, row_order_sha256 = _scan_source(jobs_csv)
        job_ids_path = partial / "job-ids.json"
        expected_job_ids = canonical_json(list(job_ids)) + b"\n"
        if job_ids_path.exists():
            if job_ids_path.read_bytes() != expected_job_ids:
                raise RuntimeError("resumable job IDs differ from the current source CSV")
        else:
            temporary_ids = job_ids_path.with_suffix(".json.partial")
            with temporary_ids.open("xb") as target:
                target.write(expected_job_ids)
                target.flush()
                os.fsync(target.fileno())
            temporary_ids.replace(job_ids_path)
        jobs_sha256 = sha256_file(job_ids_path)
        state = {
            "schema_version": 1,
            "builder": "whole_embedding_pipeline.py",
            "model": MODEL,
            "revision": MODEL_REVISION,
            "tokenizer_sha256": tokenizer_sha256,
            "source_dimension": MODEL_DIMENSION,
            "selected_dimension": OUTPUT_DIMENSION,
            "projection": PROJECTION,
            "dtype": "float16",
            "normalized": True,
            "encoder_backend": encoder_backend,
            "encoder_identity": _backend_identity(encoder_identity),
            "dataset_sha256": dataset_sha256,
            "rows": len(job_ids),
            "jobs_sha256": jobs_sha256,
            "job_row_order_sha256": row_order_sha256,
            "document_policy_version": DOCUMENT_POLICY_VERSION,
            "document_template_sha256": document_template_sha256(),
            "document_fields": [label for label, _field in FULL_JOB_FIELDS],
        }
        state_path = partial / "build-state.json"
        if state_path.exists():
            existing_state = read_json_object(state_path, "whole embedding resumable state")
            exact_keys(existing_state, STATE_KEYS, "whole embedding resumable state")
            if existing_state != state:
                raise RuntimeError("resumable build state differs from the requested build")
        else:
            atomic_json(state_path, state)
        build_manifest_path = partial / "build-manifest.json"
        rows = iter(_source_rows(jobs_csv))
        shards: list[dict[str, object]] = []
        build_shards: list[dict[str, object]] = []
        for shard_index, row_start in enumerate(range(0, len(job_ids), shard_size)):
            row_end = min(row_start + shard_size, len(job_ids))
            shard_path = partial / f"embeddings-{shard_index:05d}.f16.npy"
            sidecar_path = partial / f"{shard_path.name}.manifest.json"
            resumed = _resume_shard(
                shard_path=shard_path,
                sidecar_path=sidecar_path,
                artifact_prefix=clean_prefix.as_posix(),
                row_start=row_start,
                row_end=row_end,
                job_ids=job_ids,
            )
            temporary_shard = shard_path.with_suffix(shard_path.suffix + ".partial")
            vectors: npt.NDArray[np.float16] | None = None
            if resumed is None:
                vectors = np.lib.format.open_memmap(
                    temporary_shard,
                    mode="w+",
                    dtype=np.float16,
                    shape=(row_end - row_start, OUTPUT_DIMENSION),
                )
            for batch_start in range(row_start, row_end, batch_size):
                expected = min(batch_size, row_end - batch_start)
                batch: list[str] = []
                for offset in range(expected):
                    try:
                        job_id, document = next(rows)
                    except StopIteration as error:
                        raise RuntimeError("source CSV changed during embedding build") from error
                    if job_id != job_ids[batch_start + offset]:
                        raise RuntimeError("source CSV row order changed during embedding build")
                    batch.append(document)
                if vectors is not None:
                    vectors[batch_start - row_start : batch_start - row_start + expected] = (
                        _encoded(encoder, batch).astype(np.float16)
                    )
            if vectors is not None:
                cast(np.memmap, vectors).flush()
                del vectors
                temporary_shard.replace(shard_path)
                _validate_vectors(shard_path, row_end - row_start)
                resumed = _expected_sidecar(
                    shard_path=shard_path,
                    artifact_prefix=clean_prefix.as_posix(),
                    row_start=row_start,
                    row_end=row_end,
                    job_ids=job_ids,
                )
                atomic_json(sidecar_path, resumed)
            if resumed is None:
                raise AssertionError("unreachable shard state")
            shards.append(
                {
                    "vectors_path": f"{clean_prefix.as_posix()}/{shard_path.name}",
                    "row_start": row_start,
                    "row_end": row_end,
                    "rows": row_end - row_start,
                    "dimension": OUTPUT_DIMENSION,
                }
            )
            build_shards.append(
                {
                    **resumed,
                    "sidecar_path": f"{clean_prefix.as_posix()}/{sidecar_path.name}",
                    "sidecar_sha256": sha256_file(sidecar_path),
                }
            )
        try:
            next(rows)
        except StopIteration:
            pass
        else:
            raise RuntimeError("source CSV gained rows during embedding build")
        if sha256_file(jobs_csv) != dataset_sha256:
            raise RuntimeError("source CSV bytes changed during embedding build")
        build_manifest = {**state, "complete": True, "shards": build_shards}
        if build_manifest_path.exists():
            existing_build = read_json_object(build_manifest_path, "whole embedding build manifest")
            if existing_build != build_manifest:
                raise RuntimeError("sealed whole embedding build manifest differs")
        else:
            atomic_json(build_manifest_path, build_manifest)
        component = {
            "schema_version": 1,
            "complete": True,
            "model": MODEL,
            "revision": MODEL_REVISION,
            "source_dimension": MODEL_DIMENSION,
            "dimension": OUTPUT_DIMENSION,
            "projection": PROJECTION,
            "dtype": "float16",
            "normalized": True,
            "rows": len(job_ids),
            "dataset_sha256": dataset_sha256,
            "jobs_sha256": jobs_sha256,
            "job_row_order_sha256": row_order_sha256,
            "document_policy_version": DOCUMENT_POLICY_VERSION,
            "document_template_sha256": document_template_sha256(),
            "document_fields": [label for label, _field in FULL_JOB_FIELDS],
            "query_prompt": QUERY_PROMPT,
            "build_manifest_path": f"{clean_prefix.as_posix()}/{build_manifest_path.name}",
            "build_manifest_sha256": sha256_file(build_manifest_path),
            "job_ids_path": f"{clean_prefix.as_posix()}/{job_ids_path.name}",
            "shards": shards,
        }
        component_path = partial / "manifest.json"
        if component_path.exists():
            if read_json_object(component_path, "whole embedding component") != component:
                raise RuntimeError("sealed whole embedding component differs")
        else:
            atomic_json(component_path, component)
        validate_whole_embeddings(
            partial, jobs_csv=jobs_csv, artifact_prefix=clean_prefix.as_posix()
        )
        state_path.unlink()
        partial.replace(output)
        return component
    finally:
        encoder.close()


def _local_path(output: Path, runtime_path: object, artifact_prefix: str) -> Path:
    if not isinstance(runtime_path, str):
        raise RuntimeError("whole embedding artifact path must be a string")
    prefix = artifact_prefix.rstrip("/") + "/"
    if not runtime_path.startswith(prefix):
        raise RuntimeError("whole embedding artifact path differs from its runtime prefix")
    suffix = runtime_path.removeprefix(prefix)
    if not suffix or PurePosixPath(suffix).name != suffix:
        raise RuntimeError("whole embedding artifact path is unsafe")
    return output / suffix


def _whole_inventory(
    output: Path, manifest: Mapping[str, object], artifact_prefix: str
) -> tuple[dict[str, object], ...]:
    values = [
        artifact_entry(
            _local_path(output, manifest["build_manifest_path"], artifact_prefix),
            relative_to=output,
            kind="evidence",
        ),
        artifact_entry(
            _local_path(output, manifest["job_ids_path"], artifact_prefix),
            relative_to=output,
            kind="embedding",
        ),
    ]
    shards = manifest["shards"]
    if not isinstance(shards, list):
        raise RuntimeError("whole embedding shards must be an array")
    values.extend(
        artifact_entry(
            _local_path(output, shard["vectors_path"], artifact_prefix),
            relative_to=output,
            kind="embedding",
        )
        for shard in shards
        if isinstance(shard, dict)
    )
    build = read_json_object(
        _local_path(output, manifest["build_manifest_path"], artifact_prefix),
        "whole embedding build manifest",
    )
    build_shards = build.get("shards")
    if not isinstance(build_shards, list) or len(build_shards) != len(shards):
        raise RuntimeError("whole embedding build shard inventory is malformed")
    values.extend(
        artifact_entry(
            _local_path(output, shard["sidecar_path"], artifact_prefix),
            relative_to=output,
            kind="evidence",
        )
        for shard in build_shards
        if isinstance(shard, dict)
    )
    if len(values) != len(shards) * 2 + 2:
        raise RuntimeError("whole embedding shard inventory is malformed")
    return tuple(values)


def validate_whole_embeddings(
    output: Path, *, jobs_csv: Path, artifact_prefix: str
) -> dict[str, object]:
    manifest = read_json_object(output / "manifest.json", "whole embedding component")
    exact_keys(manifest, COMPONENT_KEYS, "whole embedding component")
    clean_prefix = PurePosixPath(artifact_prefix).as_posix()
    expected = {
        "schema_version": 1,
        "complete": True,
        "model": MODEL,
        "revision": MODEL_REVISION,
        "source_dimension": MODEL_DIMENSION,
        "dimension": OUTPUT_DIMENSION,
        "projection": PROJECTION,
        "dtype": "float16",
        "normalized": True,
        "dataset_sha256": sha256_file(jobs_csv),
        "document_policy_version": DOCUMENT_POLICY_VERSION,
        "document_template_sha256": document_template_sha256(),
        "document_fields": [label for label, _field in FULL_JOB_FIELDS],
        "query_prompt": QUERY_PROMPT,
    }
    if any(manifest[name] != value for name, value in expected.items()):
        raise RuntimeError("whole embedding component policy or source lineage differs")
    rows = manifest["rows"]
    if isinstance(rows, bool) or not isinstance(rows, int) or rows < 1:
        raise RuntimeError("whole embedding rows must be positive")
    job_ids_path = _local_path(output, manifest["job_ids_path"], clean_prefix)
    try:
        job_ids = json.loads(job_ids_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("whole embedding job IDs cannot be read") from error
    if (
        not isinstance(job_ids, list)
        or len(job_ids) != rows
        or any(
            not isinstance(value, str) or not value.isascii() or not value.isdecimal()
            for value in job_ids
        )
        or len(set(job_ids)) != rows
        or sha256_file(job_ids_path) != manifest["jobs_sha256"]
    ):
        raise RuntimeError("whole embedding job IDs differ")
    order = hashlib.sha256()
    for job_id in job_ids:
        order.update(cast(str, job_id).encode() + b"\n")
    if order.hexdigest() != manifest["job_row_order_sha256"]:
        raise RuntimeError("whole embedding row-order SHA-256 differs")
    build_path = _local_path(output, manifest["build_manifest_path"], clean_prefix)
    if sha256_file(build_path) != manifest["build_manifest_sha256"]:
        raise RuntimeError("whole embedding build-manifest bytes differ")
    build = read_json_object(build_path, "whole embedding build manifest")
    exact_keys(build, BUILD_KEYS, "whole embedding build manifest")
    build_expected = {
        "schema_version": 1,
        "complete": True,
        "builder": "whole_embedding_pipeline.py",
        "model": MODEL,
        "revision": MODEL_REVISION,
        "source_dimension": MODEL_DIMENSION,
        "selected_dimension": OUTPUT_DIMENSION,
        "projection": PROJECTION,
        "dtype": "float16",
        "normalized": True,
        "dataset_sha256": manifest["dataset_sha256"],
        "rows": rows,
        "jobs_sha256": manifest["jobs_sha256"],
        "job_row_order_sha256": manifest["job_row_order_sha256"],
        "document_policy_version": DOCUMENT_POLICY_VERSION,
        "document_template_sha256": document_template_sha256(),
        "document_fields": [label for label, _field in FULL_JOB_FIELDS],
    }
    if any(build[name] != value for name, value in build_expected.items()):
        raise RuntimeError("whole embedding build lineage differs")
    require_sha256(build["tokenizer_sha256"], "whole embedding tokenizer SHA-256")
    for name in ("encoder_backend", "encoder_identity"):
        if not isinstance(build[name], str) or not build[name].strip():
            raise RuntimeError(f"whole embedding {name} is missing")
    require_sha256(manifest["build_manifest_sha256"], "build manifest SHA-256")
    require_sha256(manifest["dataset_sha256"], "dataset SHA-256")
    require_sha256(manifest["jobs_sha256"], "jobs SHA-256")
    require_sha256(manifest["job_row_order_sha256"], "row-order SHA-256")
    shards = manifest["shards"]
    build_shards = build["shards"]
    if not isinstance(shards, list) or not shards:
        raise RuntimeError("whole embedding shards must be non-empty")
    if not isinstance(build_shards, list) or len(build_shards) != len(shards):
        raise RuntimeError("whole embedding build shard evidence differs")
    expected_start = 0
    for position, raw in enumerate(shards):
        if not isinstance(raw, dict):
            raise RuntimeError("whole embedding shard must be an object")
        exact_keys(
            raw,
            {"vectors_path", "row_start", "row_end", "rows", "dimension"},
            f"whole embedding shard {position}",
        )
        start, end, shard_rows = raw["row_start"], raw["row_end"], raw["rows"]
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or isinstance(shard_rows, bool)
            or not isinstance(shard_rows, int)
            or start != expected_start
            or end <= start
            or shard_rows != end - start
            or raw["dimension"] != OUTPUT_DIMENSION
        ):
            raise RuntimeError("whole embedding shards are not contiguous 1024d rows")
        path = _local_path(output, raw["vectors_path"], clean_prefix)
        _validate_vectors(path, shard_rows)
        build_shard = build_shards[position]
        if not isinstance(build_shard, dict):
            raise RuntimeError("whole embedding build shard must be an object")
        exact_keys(build_shard, BUILD_SHARD_KEYS, f"whole embedding build shard {position}")
        sidecar_path = _local_path(output, build_shard["sidecar_path"], clean_prefix)
        sidecar = read_json_object(sidecar_path, "whole embedding shard sidecar")
        exact_keys(sidecar, SIDECAR_KEYS, "whole embedding shard sidecar")
        expected_sidecar = _expected_sidecar(
            shard_path=path,
            artifact_prefix=clean_prefix,
            row_start=start,
            row_end=end,
            job_ids=tuple(cast(list[str], job_ids)),
        )
        if (
            sidecar != expected_sidecar
            or build_shard
            != {
                **expected_sidecar,
                "sidecar_path": build_shard["sidecar_path"],
                "sidecar_sha256": sha256_file(sidecar_path),
            }
            or sha256_file(sidecar_path) != build_shard["sidecar_sha256"]
        ):
            raise RuntimeError("whole embedding shard evidence differs")
        expected_start = end
    if expected_start != rows:
        raise RuntimeError("whole embedding shard rows differ from component rows")
    return {
        "passed": True,
        "rows": rows,
        "dimension": OUTPUT_DIMENSION,
        "manifest_sha256": sha256_file(output / "manifest.json"),
        "artifacts": list(_whole_inventory(output, manifest, clean_prefix)),
    }


def _s3(
    output: Path,
    *,
    jobs_csv: Path,
    artifact_prefix: str,
    bucket: str,
    prefix: str,
    expected_owner: str,
    profile: str | None,
    region: str,
    publish: bool,
) -> dict[str, object]:
    validation = validate_whole_embeddings(
        output, jobs_csv=jobs_csv, artifact_prefix=artifact_prefix
    )
    if region != "us-west-2":
        raise RuntimeError("whole embedding S3 publication is pinned to us-west-2")
    session = boto3.Session(profile_name=profile, region_name=region)
    identity = cast(AwsIdentity, session.client("sts")).get_caller_identity()
    if identity.get("Account") != expected_owner:
        raise RuntimeError("AWS caller identity differs from expected S3 owner")
    manifest_path = output / "manifest.json"
    manifest_sha256 = sha256_file(manifest_path)
    clean_prefix = prefix.strip("/")
    if not clean_prefix or clean_prefix.rsplit("/", 1)[-1] != manifest_sha256:
        raise RuntimeError("S3 prefix must end with the manifest SHA-256")
    artifacts = validation["artifacts"]
    s3 = session.client("s3")
    if publish:
        return publish_s3_directory(
            root=output,
            bucket=bucket,
            prefix=prefix,
            expected_owner=expected_owner,
            artifacts=artifacts,
            s3=s3,
        )
    parsed = verify_local_inventory(output, artifacts)
    verify_s3_inventory(
        bucket=bucket,
        prefix=prefix,
        expected_owner=expected_owner,
        artifacts=parsed,
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
        "manifest_sha256": manifest_sha256,
        "s3_prefix": f"s3://{bucket}/{clean_prefix}/",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--jobs-csv", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--tokenizer-sha256", required=True)
    build.add_argument("--artifact-prefix", default=DEFAULT_ARTIFACT_PREFIX)
    build.add_argument("--backend", choices=("cuda", "sagemaker"), required=True)
    build.add_argument("--model-snapshot", type=Path)
    build.add_argument("--devices", default="cuda:0,cuda:1")
    build.add_argument("--endpoint")
    build.add_argument("--profile")
    build.add_argument("--region", default="us-west-2")
    build.add_argument("--expected-account", default="378849533305")
    build.add_argument("--shard-size", type=int, default=24_576)
    build.add_argument("--batch-size", type=int, default=32)
    validate = commands.add_parser("validate")
    validate.add_argument("--output", type=Path, required=True)
    validate.add_argument("--jobs-csv", type=Path, required=True)
    validate.add_argument("--artifact-prefix", default=DEFAULT_ARTIFACT_PREFIX)
    for name in ("publish-s3", "verify-s3"):
        command = commands.add_parser(name)
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--jobs-csv", type=Path, required=True)
        command.add_argument("--artifact-prefix", default=DEFAULT_ARTIFACT_PREFIX)
        command.add_argument("--bucket", required=True)
        command.add_argument("--prefix", required=True)
        command.add_argument("--expected-owner", default="378849533305")
        command.add_argument("--profile")
        command.add_argument("--region", default="us-west-2")
    args = parser.parse_args()
    if args.command == "build":
        if args.backend == "cuda":
            if args.model_snapshot is None or args.endpoint is not None:
                raise RuntimeError("CUDA build requires only --model-snapshot")
            encoder = local_encoder(
                model_snapshot=args.model_snapshot,
                devices=parse_devices(args.devices),
                batch_size=args.batch_size,
            )
            identity = f"local:{MODEL_REVISION}"
        else:
            if not args.endpoint or args.model_snapshot is not None:
                raise RuntimeError("SageMaker build requires only --endpoint")
            encoder = sagemaker_encoder(
                endpoint=args.endpoint,
                profile=args.profile,
                region=args.region,
                expected_account=args.expected_account,
            )
            identity = f"sagemaker:{args.region}:{args.endpoint}"
        result = build_whole_embeddings(
            jobs_csv=args.jobs_csv,
            output=args.output,
            tokenizer_sha256=args.tokenizer_sha256,
            encoder=encoder,
            encoder_backend=args.backend,
            encoder_identity=identity,
            artifact_prefix=args.artifact_prefix,
            shard_size=args.shard_size,
            batch_size=args.batch_size,
        )
    elif args.command == "validate":
        result = validate_whole_embeddings(
            args.output, jobs_csv=args.jobs_csv, artifact_prefix=args.artifact_prefix
        )
    else:
        result = _s3(
            args.output,
            jobs_csv=args.jobs_csv,
            artifact_prefix=args.artifact_prefix,
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
