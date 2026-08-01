#!/usr/bin/env python3
"""Build and verify promotion-gated Qwen multi-view embedding artifacts."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import boto3  # type: ignore[import-untyped]
import numpy as np
import numpy.typing as npt
from pipeline_contract import (
    artifact_entry,
    atomic_json,
    exact_keys,
    read_json_object,
    require_sha256,
    sha256_file,
    verify_local_inventory,
    verify_s3_inventory,
    verify_s3_object,
)

MODEL = "Qwen/Qwen3-Embedding-8B"
MODEL_REVISION = "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"
MODEL_DIMENSION = 4096
OUTPUT_DIMENSION = 1024
MODEL_MAX_LENGTH = 384
VIEW_KINDS = ("occupation", "skill", "requirement", "content")
VIEW_POLICY_VERSION = "2026-08-01-whole-facet-content-v2"
DEVICE = re.compile(r"^cuda:(0|[1-9][0-9]*)$")
INPUT_MANIFEST_KEYS = {
    "schema_version",
    "complete",
    "dataset_sha256",
    "jobs_sha256",
    "job_row_order_sha256",
    "document_policy_version",
    "view_policy_version",
    "view_kinds",
    "records",
    "records_sha256",
}
RECORD_KEYS = {"job_id", "job_row", "kind", "view_index", "text"}


class Encoder(Protocol):
    def encode(self, texts: list[str]) -> npt.NDArray[np.float32]: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class ManagedEncoder:
    encode_fn: Callable[[list[str]], npt.NDArray[np.float32]]
    close_fn: Callable[[], None]

    def encode(self, texts: list[str]) -> npt.NDArray[np.float32]:
        return self.encode_fn(texts)

    def close(self) -> None:
        self.close_fn()


class AwsIdentity(Protocol):
    def get_caller_identity(self) -> dict[str, object]: ...


class SageMakerRuntime(Protocol):
    def invoke_endpoint(self, **kwargs: object) -> dict[str, object]: ...


def parse_devices(value: str) -> tuple[str, ...]:
    devices = tuple(part.strip() for part in value.split(",") if part.strip())
    if (
        len(devices) < 2
        or len(set(devices)) != len(devices)
        or any(not DEVICE.fullmatch(device) for device in devices)
    ):
        raise ValueError("CUDA build requires at least two unique explicit devices")
    return devices


def _promotion_evidence(path: Path, expected_sha256: str) -> dict[str, object]:
    require_sha256(expected_sha256, "promotion report SHA-256")
    if sha256_file(path) != expected_sha256:
        raise RuntimeError("promotion report bytes differ from the approved SHA-256")
    report = read_json_object(path, "multi-view promotion report")
    required = {
        "schema_version",
        "complete",
        "experiment",
        "selected_dimension",
        "reference_dimension",
        "primary_metric",
        "absolute_delta",
        "evaluation_split_sha256",
        "baseline_run_sha256",
        "candidate_run_sha256",
    }
    exact_keys(report, required, "multi-view promotion report")
    delta = report["absolute_delta"]
    if (
        report["schema_version"] != 1
        or report["complete"] is not True
        or report["experiment"] != "Qwen3 multi-view MRL ablation"
        or report["selected_dimension"] != OUTPUT_DIMENSION
        or report["reference_dimension"] != MODEL_DIMENSION
        or report["primary_metric"] != "ndcg_at_10"
        or isinstance(delta, bool)
        or not isinstance(delta, (int, float))
        or not math.isfinite(delta)
        or delta <= 0
    ):
        raise RuntimeError("multi-view promotion evidence did not pass")
    for name in ("evaluation_split_sha256", "baseline_run_sha256", "candidate_run_sha256"):
        require_sha256(report[name], name)
    return {
        "report_sha256": expected_sha256,
        "selected_dimension": OUTPUT_DIMENSION,
        "reference_dimension": MODEL_DIMENSION,
        "primary_metric": "ndcg_at_10",
        "absolute_delta": float(delta),
        "evaluation_split_sha256": report["evaluation_split_sha256"],
        "baseline_run_sha256": report["baseline_run_sha256"],
        "candidate_run_sha256": report["candidate_run_sha256"],
    }


def _input_records(
    records_path: Path, manifest_path: Path
) -> tuple[dict[str, Any], list[dict[str, object]]]:
    manifest = read_json_object(manifest_path, "multi-view input manifest")
    exact_keys(manifest, INPUT_MANIFEST_KEYS, "multi-view input manifest")
    if (
        manifest["schema_version"] != 1
        or manifest["complete"] is not True
        or manifest["view_policy_version"] != VIEW_POLICY_VERSION
        or manifest["view_kinds"] != list(VIEW_KINDS)
    ):
        raise RuntimeError("multi-view input policy differs")
    for name in ("dataset_sha256", "jobs_sha256", "job_row_order_sha256", "records_sha256"):
        require_sha256(manifest[name], name)
    if sha256_file(records_path) != manifest["records_sha256"]:
        raise RuntimeError("multi-view records bytes differ from input manifest")
    count = manifest["records"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise RuntimeError("multi-view input record count is invalid")

    records: list[dict[str, object]] = []
    previous: tuple[int, int, int] | None = None
    seen: set[tuple[str, str, int]] = set()
    job_to_row: dict[str, int] = {}
    row_to_job: dict[int, str] = {}
    view_indexes: dict[tuple[str, str], list[int]] = {}
    with records_path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError(f"invalid multi-view JSON at line {line_number}") from error
            if not isinstance(raw, dict):
                raise RuntimeError("multi-view record must be an object")
            exact_keys(raw, RECORD_KEYS, f"multi-view record {line_number}")
            job_id, job_row, kind, view_index, text = (
                raw["job_id"],
                raw["job_row"],
                raw["kind"],
                raw["view_index"],
                raw["text"],
            )
            if (
                not isinstance(job_id, str)
                or not job_id.isascii()
                or not job_id.isdecimal()
                or isinstance(job_row, bool)
                or not isinstance(job_row, int)
                or job_row < 0
                or kind not in VIEW_KINDS
                or isinstance(view_index, bool)
                or not isinstance(view_index, int)
                or view_index < 0
                or not isinstance(text, str)
                or not text.strip()
            ):
                raise RuntimeError("multi-view record violates its closed contract")
            identity = (job_id, cast(str, kind), view_index)
            order = (job_row, VIEW_KINDS.index(cast(str, kind)), view_index)
            if identity in seen or (previous is not None and order <= previous):
                raise RuntimeError("multi-view records are duplicated or not in canonical order")
            if job_id in job_to_row and job_to_row[job_id] != job_row:
                raise RuntimeError("one job_id maps to multiple job rows")
            if job_row in row_to_job and row_to_job[job_row] != job_id:
                raise RuntimeError("one job row maps to multiple job_ids")
            job_to_row[job_id] = job_row
            row_to_job[job_row] = job_id
            view_indexes.setdefault((job_id, cast(str, kind)), []).append(view_index)
            seen.add(identity)
            previous = order
            records.append(dict(raw))
    if len(records) != count:
        raise RuntimeError("multi-view input record count differs")
    if sorted(row_to_job) != list(range(len(row_to_job))):
        raise RuntimeError("multi-view job rows must be contiguous from zero")
    for job_id in job_to_row:
        for kind in VIEW_KINDS:
            indexes = view_indexes.get((job_id, kind))
            if indexes is None or indexes != list(range(len(indexes))):
                raise RuntimeError("every job must have contiguous indexes for every view kind")
    return manifest, records


def _normalize_prefix(values: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
    if values.ndim != 2 or values.shape[1] < OUTPUT_DIMENSION or not np.isfinite(values).all():
        raise RuntimeError("embedding backend returned an invalid matrix")
    selected = np.asarray(values[:, :OUTPUT_DIMENSION], dtype=np.float32)
    norms = np.linalg.norm(selected, axis=1, keepdims=True)
    if np.any(norms == 0) or not np.isfinite(norms).all():
        raise RuntimeError("embedding backend returned a zero or non-finite prefix")
    return np.asarray(selected / norms, dtype=np.float32)


def local_encoder(
    *,
    model_snapshot: Path,
    devices: tuple[str, ...],
    batch_size: int,
) -> Encoder:
    if model_snapshot.name != MODEL_REVISION or not (model_snapshot / "tokenizer.json").is_file():
        raise RuntimeError("local model snapshot is not the pinned Qwen revision")
    try:
        import torch
        from sentence_transformers import SentenceTransformer
    except ImportError as error:
        raise RuntimeError(
            "local build requires torch==2.13.0 and sentence-transformers==5.6.0"
        ) from error
    if not torch.cuda.is_available() or any(
        int(device.removeprefix("cuda:")) >= torch.cuda.device_count() for device in devices
    ):
        raise RuntimeError("one or more explicit CUDA devices are unavailable")
    model = SentenceTransformer(
        str(model_snapshot),
        device=devices[0],
        model_kwargs={"torch_dtype": torch.bfloat16},
        local_files_only=True,
    )
    model.max_seq_length = MODEL_MAX_LENGTH
    pool = model.start_multi_process_pool(list(devices))

    def encode(texts: list[str]) -> npt.NDArray[np.float32]:
        values = model.encode(
            texts,
            batch_size=batch_size,
            normalize_embeddings=False,
            convert_to_numpy=True,
            show_progress_bar=False,
            truncate_dim=OUTPUT_DIMENSION,
            pool=pool,
        )
        return _normalize_prefix(np.asarray(values, dtype=np.float32))

    return ManagedEncoder(encode, lambda: model.stop_multi_process_pool(pool))


def sagemaker_encoder(
    *,
    endpoint: str,
    profile: str | None,
    region: str,
    expected_account: str,
) -> Encoder:
    if not endpoint.strip() or region != "us-west-2" or len(expected_account) != 12:
        raise ValueError("pinned endpoint, us-west-2, and expected AWS account are required")
    session = boto3.Session(profile_name=profile, region_name=region)
    identity = cast(AwsIdentity, session.client("sts")).get_caller_identity()
    if identity.get("Account") != expected_account:
        raise RuntimeError("AWS caller identity differs from the approved account")
    runtime = cast(SageMakerRuntime, session.client("sagemaker-runtime"))

    def encode(texts: list[str]) -> npt.NDArray[np.float32]:
        response = runtime.invoke_endpoint(
            EndpointName=endpoint,
            Body=json.dumps({"inputs": texts}, ensure_ascii=False).encode(),
            ContentType="application/json",
            Accept="application/json",
        )
        body = response.get("Body")
        if body is None or not hasattr(body, "read"):
            raise RuntimeError("SageMaker response body is missing")
        try:
            values = np.asarray(json.loads(body.read()), dtype=np.float32)  # type: ignore[union-attr]
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise RuntimeError("SageMaker embedding response is invalid") from error
        if values.shape != (len(texts), MODEL_DIMENSION):
            raise RuntimeError("SageMaker embedding response violates the 4096d contract")
        return _normalize_prefix(values)

    return ManagedEncoder(encode, lambda: None)


def _chunks(values: list[dict[str, object]], size: int) -> Iterator[list[dict[str, object]]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def build_embeddings(
    *,
    records_path: Path,
    input_manifest_path: Path,
    promotion_report_path: Path,
    promotion_report_sha256: str,
    output: Path,
    encoder: Encoder,
    encoder_backend: str,
    shard_size: int,
    batch_size: int,
) -> dict[str, object]:
    try:
        return _build_embeddings(
            records_path=records_path,
            input_manifest_path=input_manifest_path,
            promotion_report_path=promotion_report_path,
            promotion_report_sha256=promotion_report_sha256,
            output=output,
            encoder=encoder,
            encoder_backend=encoder_backend,
            shard_size=shard_size,
            batch_size=batch_size,
        )
    finally:
        encoder.close()


def _build_embeddings(
    *,
    records_path: Path,
    input_manifest_path: Path,
    promotion_report_path: Path,
    promotion_report_sha256: str,
    output: Path,
    encoder: Encoder,
    encoder_backend: str,
    shard_size: int,
    batch_size: int,
) -> dict[str, object]:
    if output.exists():
        raise RuntimeError("embedding output already exists; builds never overwrite artifacts")
    output.parent.mkdir(parents=True, exist_ok=True)
    build_root = output.with_name(f".{output.name}.{os.getpid()}.partial")
    if build_root.exists():
        raise RuntimeError(f"partial embedding output already exists: {build_root}")
    build_root.mkdir()
    try:
        result = _write_embeddings(
            records_path=records_path,
            input_manifest_path=input_manifest_path,
            promotion_report_path=promotion_report_path,
            promotion_report_sha256=promotion_report_sha256,
            output=build_root,
            encoder=encoder,
            encoder_backend=encoder_backend,
            shard_size=shard_size,
            batch_size=batch_size,
        )
        build_root.replace(output)
        return result
    except BaseException:
        shutil.rmtree(build_root, ignore_errors=True)
        raise


def _write_embeddings(
    *,
    records_path: Path,
    input_manifest_path: Path,
    promotion_report_path: Path,
    promotion_report_sha256: str,
    output: Path,
    encoder: Encoder,
    encoder_backend: str,
    shard_size: int,
    batch_size: int,
) -> dict[str, object]:
    if shard_size < 1 or batch_size < 1:
        raise ValueError("shard_size and batch_size must be positive")
    source, records = _input_records(records_path, input_manifest_path)
    promotion = _promotion_evidence(promotion_report_path, promotion_report_sha256)
    manifest_path = output / "manifest.json"
    mapping_path = output / "job-view-mapping.json"

    mapping = [
        {name: record[name] for name in ("job_id", "job_row", "kind", "view_index")}
        for record in records
    ]
    atomic_json(mapping_path, mapping)
    shards: list[dict[str, object]] = []
    for shard_index, rows in enumerate(_chunks(records, shard_size)):
        batches = [
            encoder.encode([cast(str, row["text"]) for row in batch])
            for batch in _chunks(rows, batch_size)
        ]
        values = np.concatenate(batches)
        if values.shape != (len(rows), OUTPUT_DIMENSION):
            raise RuntimeError("embedding shard shape differs after encoding")
        filename = f"embeddings-{shard_index:05d}.f16.npy"
        target = output / filename
        partial = output / f".{filename}.{os.getpid()}.partial"
        try:
            with partial.open("wb") as stream:
                np.save(stream, np.asarray(values, dtype=np.float16), allow_pickle=False)
                stream.flush()
                os.fsync(stream.fileno())
            partial.replace(target)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        shards.append(
            {
                "path": filename,
                "row_start": shard_index * shard_size,
                "row_end": shard_index * shard_size + len(rows),
                "rows": len(rows),
                "dimension": OUTPUT_DIMENSION,
                "sha256": sha256_file(target),
                "size_bytes": target.stat().st_size,
            }
        )
    artifacts = [artifact_entry(mapping_path, relative_to=output, kind="embedding")]
    artifacts.extend(
        artifact_entry(output / cast(str, shard["path"]), relative_to=output, kind="embedding")
        for shard in shards
    )
    report: dict[str, object] = {
        "schema_version": 1,
        "complete": True,
        "publication_allowed": True,
        "model": MODEL,
        "revision": MODEL_REVISION,
        "model_dimension": MODEL_DIMENSION,
        "output_dimension": OUTPUT_DIMENSION,
        "dtype": "float16",
        "normalized": True,
        "model_max_length": MODEL_MAX_LENGTH,
        "view_policy_version": VIEW_POLICY_VERSION,
        "view_kinds": list(VIEW_KINDS),
        "dataset_sha256": source["dataset_sha256"],
        "jobs_sha256": source["jobs_sha256"],
        "job_row_order_sha256": source["job_row_order_sha256"],
        "document_policy_version": source["document_policy_version"],
        "records": len(records),
        "records_sha256": source["records_sha256"],
        "encoder_backend": encoder_backend,
        "promotion_evidence": promotion,
        "mapping_path": mapping_path.name,
        "shards": shards,
        "artifacts": artifacts,
    }
    atomic_json(manifest_path, report)
    verify_embeddings(output)
    return report


def verify_embeddings(output: Path) -> dict[str, object]:
    manifest = read_json_object(output / "manifest.json", "multi-view embedding manifest")
    expected_keys = {
        "schema_version",
        "complete",
        "publication_allowed",
        "model",
        "revision",
        "model_dimension",
        "output_dimension",
        "dtype",
        "normalized",
        "model_max_length",
        "view_policy_version",
        "view_kinds",
        "dataset_sha256",
        "jobs_sha256",
        "job_row_order_sha256",
        "document_policy_version",
        "records",
        "records_sha256",
        "encoder_backend",
        "promotion_evidence",
        "mapping_path",
        "shards",
        "artifacts",
    }
    exact_keys(manifest, expected_keys, "multi-view embedding manifest")
    if (
        manifest["schema_version"] != 1
        or manifest["complete"] is not True
        or manifest["publication_allowed"] is not True
        or manifest["model"] != MODEL
        or manifest["revision"] != MODEL_REVISION
        or manifest["model_dimension"] != MODEL_DIMENSION
        or manifest["output_dimension"] != OUTPUT_DIMENSION
        or manifest["dtype"] != "float16"
        or manifest["normalized"] is not True
        or manifest["view_policy_version"] != VIEW_POLICY_VERSION
        or manifest["view_kinds"] != list(VIEW_KINDS)
    ):
        raise RuntimeError("multi-view embedding contract differs")
    verify_local_inventory(output, manifest["artifacts"])
    mapping = json.loads((output / cast(str, manifest["mapping_path"])).read_text(encoding="utf-8"))
    shards = manifest["shards"]
    if not isinstance(mapping, list) or not isinstance(shards, list) or not shards:
        raise RuntimeError("multi-view mapping or shards are invalid")
    expected_start = 0
    for position, shard in enumerate(shards):
        if not isinstance(shard, dict):
            raise RuntimeError("multi-view shard entry must be an object")
        exact_keys(
            shard,
            {"path", "row_start", "row_end", "rows", "dimension", "sha256", "size_bytes"},
            f"multi-view shard {position}",
        )
        if (
            shard["row_start"] != expected_start
            or shard["row_end"] != expected_start + shard["rows"]
            or shard["dimension"] != OUTPUT_DIMENSION
        ):
            raise RuntimeError("multi-view shard row layout differs")
        values = np.load(output / cast(str, shard["path"]), mmap_mode="r", allow_pickle=False)
        if values.dtype != np.float16 or values.shape != (shard["rows"], OUTPUT_DIMENSION):
            raise RuntimeError("multi-view shard bytes violate shape or dtype")
        norms = np.linalg.norm(np.asarray(values, dtype=np.float32), axis=1)
        if not np.isfinite(norms).all() or np.max(np.abs(norms - 1.0)) > 0.002:
            raise RuntimeError("multi-view shard is not normalized")
        expected_start = cast(int, shard["row_end"])
    if expected_start != manifest["records"] or len(mapping) != manifest["records"]:
        raise RuntimeError("multi-view aggregate row count differs")
    return {
        "passed": True,
        "records": expected_start,
        "shards": len(shards),
        "manifest_sha256": sha256_file(output / "manifest.json"),
    }


def verify_s3(
    output: Path,
    *,
    bucket: str,
    prefix: str,
    expected_owner: str,
    manifest_sha256: str,
) -> None:
    require_sha256(manifest_sha256, "published manifest SHA-256")
    manifest_path = output / "manifest.json"
    if sha256_file(manifest_path) != manifest_sha256:
        raise RuntimeError("local manifest differs from the approved publication SHA-256")
    manifest = read_json_object(manifest_path, "multi-view embedding manifest")
    artifacts = verify_local_inventory(output, manifest["artifacts"])
    session = boto3.Session(region_name="us-west-2")
    identity = cast(AwsIdentity, session.client("sts")).get_caller_identity()
    if identity.get("Account") != expected_owner:
        raise RuntimeError("AWS caller identity differs from expected S3 owner")
    s3 = session.client("s3")
    clean_prefix = prefix.strip("/")
    if not clean_prefix:
        raise ValueError("S3 prefix must be non-empty")
    verify_s3_object(
        bucket=bucket,
        key=f"{clean_prefix}/manifest.json",
        expected_owner=expected_owner,
        expected_sha256=manifest_sha256,
        expected_size=manifest_path.stat().st_size,
        s3=s3,
    )
    verify_s3_inventory(
        bucket=bucket,
        prefix=prefix,
        expected_owner=expected_owner,
        artifacts=artifacts,
        s3=s3,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--records", type=Path, required=True)
    build.add_argument("--input-manifest", type=Path, required=True)
    build.add_argument("--promotion-report", type=Path, required=True)
    build.add_argument("--promotion-report-sha256", required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--backend", choices=("cuda", "sagemaker"), required=True)
    build.add_argument("--devices", default="cuda:0,cuda:1")
    build.add_argument("--model-snapshot", type=Path)
    build.add_argument("--endpoint")
    build.add_argument("--profile")
    build.add_argument("--region", default="us-west-2")
    build.add_argument("--expected-account", default="378849533305")
    build.add_argument("--shard-size", type=int, default=10_000)
    build.add_argument("--batch-size", type=int, default=64)
    verify = commands.add_parser("verify")
    verify.add_argument("--output", type=Path, required=True)
    s3 = commands.add_parser("verify-s3")
    s3.add_argument("--output", type=Path, required=True)
    s3.add_argument("--bucket", required=True)
    s3.add_argument("--prefix", required=True)
    s3.add_argument("--expected-owner", default="378849533305")
    s3.add_argument("--manifest-sha256", required=True)
    args = parser.parse_args()
    if args.command == "verify":
        result = verify_embeddings(args.output)
    elif args.command == "verify-s3":
        verify_s3(
            args.output,
            bucket=args.bucket,
            prefix=args.prefix,
            expected_owner=args.expected_owner,
            manifest_sha256=args.manifest_sha256,
        )
        result = {"passed": True, "s3_prefix": args.prefix}
    else:
        if args.backend == "cuda":
            if args.model_snapshot is None:
                parser.error("CUDA backend requires --model-snapshot")
            devices = parse_devices(args.devices)
            encoder = local_encoder(
                model_snapshot=args.model_snapshot,
                devices=devices,
                batch_size=args.batch_size,
            )
            backend = "sentence-transformers-5.6.0:" + ",".join(devices)
        else:
            if args.endpoint is None:
                parser.error("SageMaker backend requires --endpoint")
            encoder = sagemaker_encoder(
                endpoint=args.endpoint,
                profile=args.profile,
                region=args.region,
                expected_account=args.expected_account,
            )
            backend = f"sagemaker:{args.region}:{args.endpoint}"
        result = build_embeddings(
            records_path=args.records,
            input_manifest_path=args.input_manifest,
            promotion_report_path=args.promotion_report,
            promotion_report_sha256=args.promotion_report_sha256,
            output=args.output,
            encoder=encoder,
            encoder_backend=backend,
            shard_size=args.shard_size,
            batch_size=args.batch_size,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
