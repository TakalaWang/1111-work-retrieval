#!/usr/bin/env python3
"""Build and verify reproducible Qwen multi-view challenger artifacts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import os
import re
import shutil
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import boto3  # type: ignore[import-untyped]
import numpy as np
import numpy.typing as npt
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
    canonical_code,
    canonical_text,
)

MODEL = "Qwen/Qwen3-Embedding-8B"
MODEL_REVISION = "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"
MODEL_DIMENSION = 4096
OUTPUT_DIMENSION = 1024
MODEL_MAX_LENGTH = 384
MIN_CONTENT_TOKENS = 256
VIEW_KINDS = ("occupation", "skill", "requirement", "content")
VIEW_POLICY_VERSION = "2026-08-01-whole-facet-content-v2"
VIEW_FIELDS = {
    "occupation": (
        "職務名稱",
        "職務小類",
        "職務中類",
        "職務大類",
        "產業小類",
        "產業中類",
        "產業大類",
    ),
    "skill": ("電腦技能資料", "工作技能", "專業證照"),
    "requirement": ("工作經驗需求", "學歷需求", "附加條件"),
}
JOB_ID_FIELD = "職缺編號"
CONTENT_FIELD = "職務內容"
PARAGRAPH_BREAK = re.compile(
    r"(?:\r?\n)+|<\s*br\s*/?\s*>|"
    r"</?\s*(?:p|div|li|ul|ol|h[1-6])(?:\s+[^>]*)?>",
    re.IGNORECASE,
)
DEVICE = re.compile(r"^cuda:(0|[1-9][0-9]*)$")
INPUT_MANIFEST_KEYS = {
    "schema_version",
    "complete",
    "model",
    "revision",
    "tokenizer_sha256",
    "dataset_sha256",
    "jobs_path",
    "jobs_sha256",
    "job_row_order_sha256",
    "document_policy_version",
    "view_policy_version",
    "view_kinds",
    "records",
    "records_sha256",
    "content_min_tokens",
    "content_max_tokens",
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


class Tokenizer(Protocol):
    def encode(self, text: str) -> Sequence[int]: ...


def parse_devices(value: str) -> tuple[str, ...]:
    devices = tuple(part.strip() for part in value.split(",") if part.strip())
    if (
        len(devices) < 2
        or len(set(devices)) != len(devices)
        or any(not DEVICE.fullmatch(device) for device in devices)
    ):
        raise ValueError("CUDA build requires at least two unique explicit devices")
    return devices


def _serialized_fields(row: Mapping[str, str | None], fields: Sequence[str]) -> str:
    values: list[str] = []
    seen: set[str] = set()
    for field in fields:
        if field not in row:
            raise RuntimeError(f"source CSV is missing field: {field}")
        value = canonical_text(row[field])
        identity = canonical_code(value)
        if identity and identity not in seen:
            seen.add(identity)
            values.append(f"{field}: {value}")
    return "\n".join(values)


def _token_count(text: str, tokenizer: Tokenizer) -> int:
    count = len(tokenizer.encode(text))
    if text and count < 1:
        raise RuntimeError("tokenizer returned no tokens for non-empty text")
    return count


def _maximum_end(text: str, start: int, tokenizer: Tokenizer, maximum: int) -> int:
    low, high, best = start + 1, len(text), start
    while low <= high:
        middle = (low + high) // 2
        if _token_count(text[start:middle], tokenizer) <= maximum:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    if best == start:
        raise RuntimeError("one source character exceeds the tokenizer limit")
    return best


def _bounded_chunks(
    text: str,
    preferred_ends: Sequence[int],
    tokenizer: Tokenizer,
    *,
    minimum: int,
    maximum: int,
) -> tuple[str, ...]:
    if minimum < 1 or maximum < minimum or maximum > MODEL_MAX_LENGTH:
        raise ValueError("token bounds must satisfy 1 <= minimum <= maximum <= model limit")
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = _maximum_end(text, start, tokenizer, maximum)
        if end < len(text):
            preferred = [
                boundary
                for boundary in preferred_ends
                if start < boundary <= end
                and _token_count(text[start:boundary], tokenizer) >= minimum
            ]
            if preferred:
                end = preferred[-1]
        chunks.append(text[start:end])
        start = end
    if "".join(chunks) != text or any(_token_count(chunk, tokenizer) > maximum for chunk in chunks):
        raise RuntimeError("multi-view chunking lost text or exceeded model length")
    return tuple(chunks)


def _content_text(raw: str | None) -> tuple[str, tuple[int, ...]]:
    text = canonical_text(raw)
    if not text:
        return "", ()
    paragraphs = tuple(
        value
        for segment in PARAGRAPH_BREAK.split(html.unescape(raw or ""))
        if (value := canonical_text(segment))
    )
    if not paragraphs or " ".join(paragraphs) != text:
        return text, (len(text),)
    ends: list[int] = []
    position = 0
    for index, paragraph in enumerate(paragraphs):
        position += len(paragraph)
        if index + 1 < len(paragraphs):
            position += 1
        ends.append(position)
    return text, tuple(ends)


def _job_views(row: Mapping[str, str | None], tokenizer: Tokenizer) -> dict[str, tuple[str, ...]]:
    views: dict[str, tuple[str, ...]] = {}
    for kind, fields in VIEW_FIELDS.items():
        text = _serialized_fields(row, fields)
        if text:
            views[kind] = _bounded_chunks(
                text,
                tuple(match.end() for match in re.finditer("\n", text)),
                tokenizer,
                minimum=MIN_CONTENT_TOKENS,
                maximum=MODEL_MAX_LENGTH,
            )
    content, paragraph_ends = _content_text(row.get(CONTENT_FIELD))
    if content:
        views["content"] = _bounded_chunks(
            content,
            paragraph_ends,
            tokenizer,
            minimum=MIN_CONTENT_TOKENS,
            maximum=MODEL_MAX_LENGTH,
        )
    if not views:
        raise RuntimeError("job produced no non-empty multi-view text")
    return views


def load_tokenizer(model_snapshot: Path) -> Tokenizer:
    if model_snapshot.name != MODEL_REVISION or not (model_snapshot / "tokenizer.json").is_file():
        raise RuntimeError("tokenizer snapshot is not the pinned Qwen revision")
    try:
        from transformers import AutoTokenizer  # type: ignore[import-not-found]
    except ImportError as error:
        raise RuntimeError("record build requires transformers==5.14.1") from error
    tokenizer = AutoTokenizer.from_pretrained(str(model_snapshot), local_files_only=True)
    backend = cast(Any, getattr(tokenizer, "backend_tokenizer", None))
    if backend is None:
        raise RuntimeError("record build requires the pinned fast Qwen tokenizer")

    class BackendTokenizer:
        def encode(self, text: str) -> Sequence[int]:
            return cast(Sequence[int], backend.encode(text, add_special_tokens=True).ids)

    return BackendTokenizer()


def build_records(
    *,
    jobs_csv: Path,
    output: Path,
    tokenizer: Tokenizer,
    tokenizer_sha256: str,
) -> dict[str, object]:
    require_sha256(tokenizer_sha256, "tokenizer SHA-256")
    if output.exists():
        raise RuntimeError("record output already exists; builds never overwrite artifacts")
    output.parent.mkdir(parents=True, exist_ok=True)
    build_root = output.with_name(f".{output.name}.{os.getpid()}.partial")
    if build_root.exists():
        raise RuntimeError(f"partial record output already exists: {build_root}")
    build_root.mkdir()
    records_path = build_root / "records.jsonl"
    jobs_path = build_root / "jobs.jsonl"
    try:
        seen_jobs: set[str] = set()
        record_count = 0
        order_digest = hashlib.sha256()
        with (
            jobs_csv.open(encoding="utf-8-sig", newline="") as source,
            records_path.open("wb") as records_output,
            jobs_path.open("wb") as jobs_output,
        ):
            csv.field_size_limit(64 * 1024 * 1024)
            reader = csv.DictReader(source)
            required = {JOB_ID_FIELD, CONTENT_FIELD}.union(
                field for fields in VIEW_FIELDS.values() for field in fields
            )
            if reader.fieldnames is None or not required.issubset(reader.fieldnames):
                raise RuntimeError("source CSV header differs from the multi-view contract")
            for job_row, raw in enumerate(reader):
                job_id = canonical_text(raw[JOB_ID_FIELD])
                if not job_id.isascii() or not job_id.isdecimal() or job_id in seen_jobs:
                    raise RuntimeError("source CSV contains an invalid or duplicate job_id")
                seen_jobs.add(job_id)
                order_digest.update(job_id.encode() + b"\n")
                jobs_output.write(
                    json.dumps(
                        {"job_id": job_id, "job_row": job_row},
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode()
                    + b"\n"
                )
                views = _job_views(raw, tokenizer)
                for kind in VIEW_KINDS:
                    for view_index, text in enumerate(views.get(kind, ())):
                        records_output.write(
                            json.dumps(
                                {
                                    "job_id": job_id,
                                    "job_row": job_row,
                                    "kind": kind,
                                    "view_index": view_index,
                                    "text": text,
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ).encode()
                            + b"\n"
                        )
                        record_count += 1
            if not seen_jobs:
                raise RuntimeError("source CSV contains no jobs")
        for path in (records_path, jobs_path):
            with path.open("rb+") as stream:
                stream.flush()
                os.fsync(stream.fileno())
        manifest: dict[str, object] = {
            "schema_version": 1,
            "complete": True,
            "model": MODEL,
            "revision": MODEL_REVISION,
            "tokenizer_sha256": tokenizer_sha256,
            "dataset_sha256": sha256_file(jobs_csv),
            "jobs_path": jobs_path.name,
            "jobs_sha256": sha256_file(jobs_path),
            "job_row_order_sha256": order_digest.hexdigest(),
            "document_policy_version": DOCUMENT_POLICY_VERSION,
            "view_policy_version": VIEW_POLICY_VERSION,
            "view_kinds": list(VIEW_KINDS),
            "content_min_tokens": MIN_CONTENT_TOKENS,
            "content_max_tokens": MODEL_MAX_LENGTH,
            "records": record_count,
            "records_sha256": sha256_file(records_path),
        }
        atomic_json(build_root / "manifest.json", manifest)
        _input_records(records_path, build_root / "manifest.json")
        build_root.replace(output)
        return manifest
    except BaseException:
        shutil.rmtree(build_root, ignore_errors=True)
        raise


def _promotion_evidence(path: Path, expected_sha256: str) -> dict[str, object]:
    require_sha256(expected_sha256, "promotion report SHA-256")
    if sha256_file(path) != expected_sha256:
        raise RuntimeError("promotion report bytes differ from the approved SHA-256")
    report = read_json_object(path, "multi-view promotion report")
    required = {
        "schema_version",
        "complete",
        "experiment",
        "candidate_dimension",
        "baseline_dimension",
        "primary_metric",
        "absolute_delta",
        "evaluator_kind",
        "significant",
        "candidate_manifest_sha256",
        "evaluation_split_sha256",
        "baseline_run_sha256",
        "candidate_run_sha256",
    }
    exact_keys(report, required, "multi-view promotion report")
    delta = report["absolute_delta"]
    if (
        report["schema_version"] != 1
        or report["complete"] is not True
        or report["experiment"] != "Qwen3 multi-view retrieval ablation"
        or report["candidate_dimension"] != OUTPUT_DIMENSION
        or report["baseline_dimension"] != OUTPUT_DIMENSION
        or report["primary_metric"] != "ndcg_at_10"
        or report["evaluator_kind"] != "organizer"
        or report["significant"] is not True
        or isinstance(delta, bool)
        or not isinstance(delta, (int, float))
        or not math.isfinite(delta)
        or delta <= 0
    ):
        raise RuntimeError("multi-view promotion evidence did not pass")
    for name in (
        "candidate_manifest_sha256",
        "evaluation_split_sha256",
        "baseline_run_sha256",
        "candidate_run_sha256",
    ):
        require_sha256(report[name], name)
    return {
        "report_sha256": expected_sha256,
        "candidate_dimension": OUTPUT_DIMENSION,
        "baseline_dimension": OUTPUT_DIMENSION,
        "primary_metric": "ndcg_at_10",
        "absolute_delta": float(delta),
        "evaluator_kind": "organizer",
        "significant": True,
        "candidate_manifest_sha256": report["candidate_manifest_sha256"],
        "evaluation_split_sha256": report["evaluation_split_sha256"],
        "baseline_run_sha256": report["baseline_run_sha256"],
        "candidate_run_sha256": report["candidate_run_sha256"],
    }


def _input_records(records_path: Path, manifest_path: Path) -> tuple[dict[str, Any], int]:
    manifest = read_json_object(manifest_path, "multi-view input manifest")
    exact_keys(manifest, INPUT_MANIFEST_KEYS, "multi-view input manifest")
    if (
        manifest["schema_version"] != 1
        or manifest["complete"] is not True
        or manifest["model"] != MODEL
        or manifest["revision"] != MODEL_REVISION
        or manifest["document_policy_version"] != DOCUMENT_POLICY_VERSION
        or manifest["view_policy_version"] != VIEW_POLICY_VERSION
        or manifest["view_kinds"] != list(VIEW_KINDS)
        or manifest["content_min_tokens"] != MIN_CONTENT_TOKENS
        or manifest["content_max_tokens"] != MODEL_MAX_LENGTH
    ):
        raise RuntimeError("multi-view input policy differs")
    for name in (
        "tokenizer_sha256",
        "dataset_sha256",
        "jobs_sha256",
        "job_row_order_sha256",
        "records_sha256",
    ):
        require_sha256(manifest[name], name)
    if sha256_file(records_path) != manifest["records_sha256"]:
        raise RuntimeError("multi-view records bytes differ from input manifest")
    jobs_path = manifest["jobs_path"]
    if not isinstance(jobs_path, str) or Path(jobs_path).name != jobs_path:
        raise RuntimeError("multi-view jobs path is invalid")
    jobs_file = manifest_path.parent / jobs_path
    if sha256_file(jobs_file) != manifest["jobs_sha256"]:
        raise RuntimeError("multi-view jobs bytes differ from input manifest")
    order_digest = hashlib.sha256()
    for job_id, _ in _iter_jobs(jobs_file):
        order_digest.update(job_id.encode() + b"\n")
    if order_digest.hexdigest() != manifest["job_row_order_sha256"]:
        raise RuntimeError("multi-view job row order differs from input manifest")
    count = manifest["records"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise RuntimeError("multi-view input record count is invalid")

    previous: tuple[int, int, int] | None = None
    previous_view: tuple[str, str] | None = None
    previous_view_index = -1
    records = 0
    jobs = iter(_iter_jobs(jobs_file))
    expected_job = next(jobs)
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
            order = (job_row, VIEW_KINDS.index(cast(str, kind)), view_index)
            if previous is not None and order <= previous:
                raise RuntimeError("multi-view records are duplicated or not in canonical order")
            if (job_id, job_row) != expected_job:
                if previous is None or job_row != previous[0]:
                    expected_job = next(jobs)
                if (job_id, job_row) != expected_job:
                    raise RuntimeError("multi-view records differ from the pinned job-row mapping")
            view = (job_id, cast(str, kind))
            expected_view_index = previous_view_index + 1 if view == previous_view else 0
            if view_index != expected_view_index:
                raise RuntimeError(f"job {job_id} view indexes are not contiguous")
            previous_view = view
            previous_view_index = view_index
            previous = order
            records += 1
    if records != count:
        raise RuntimeError("multi-view input record count differs")
    try:
        next(jobs)
    except StopIteration:
        pass
    else:
        raise RuntimeError("multi-view records omit jobs from the pinned mapping")
    return manifest, records


def _iter_jobs(path: Path) -> Iterator[tuple[str, int]]:
    seen: set[str] = set()
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError(f"invalid jobs JSON at line {line_number}") from error
            if not isinstance(raw, dict):
                raise RuntimeError("jobs mapping row must be an object")
            exact_keys(raw, {"job_id", "job_row"}, f"jobs mapping row {line_number}")
            job_id, job_row = raw["job_id"], raw["job_row"]
            if (
                not isinstance(job_id, str)
                or not job_id.isascii()
                or not job_id.isdecimal()
                or isinstance(job_row, bool)
                or not isinstance(job_row, int)
                or job_row != line_number - 1
                or job_id in seen
            ):
                raise RuntimeError("jobs mapping violates canonical row order")
            seen.add(job_id)
            yield job_id, job_row
    if not seen:
        raise RuntimeError("jobs mapping is empty")


def _record_chunks(path: Path, size: int) -> Iterator[list[dict[str, object]]]:
    buffered: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError(f"invalid multi-view JSON at line {line_number}") from error
            if not isinstance(raw, dict):
                raise RuntimeError("multi-view record must be an object")
            buffered.append(raw)
            if len(buffered) == size:
                yield buffered
                buffered = []
    if buffered:
        yield buffered


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
        import torch  # type: ignore[import-not-found]
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
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
        model_kwargs={"dtype": torch.bfloat16},
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
            values = np.asarray(json.loads(body.read()), dtype=np.float32)
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
    output: Path,
    encoder: Encoder,
    encoder_backend: str,
    shard_size: int,
    batch_size: int,
) -> dict[str, object]:
    if shard_size < 1 or batch_size < 1:
        raise ValueError("shard_size and batch_size must be positive")
    source, record_count = _input_records(records_path, input_manifest_path)
    manifest_path = output / "manifest.json"
    mapping_path = output / "job-view-mapping.jsonl"
    shards: list[dict[str, object]] = []
    embedded = 0
    with mapping_path.open("wb") as mapping_output:
        for shard_index, rows in enumerate(_record_chunks(records_path, shard_size)):
            for offset, row in enumerate(rows):
                mapping_output.write(
                    canonical_json(
                        {
                            "embedding_row": embedded + offset,
                            **{
                                name: row[name]
                                for name in ("job_id", "job_row", "kind", "view_index")
                            },
                        }
                    )
                    + b"\n"
                )
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
            with partial.open("wb") as stream:
                np.save(stream, np.asarray(values, dtype=np.float16), allow_pickle=False)
                stream.flush()
                os.fsync(stream.fileno())
            partial.replace(target)
            shards.append(
                {
                    "path": filename,
                    "row_start": embedded,
                    "row_end": embedded + len(rows),
                    "rows": len(rows),
                    "dimension": OUTPUT_DIMENSION,
                    "sha256": sha256_file(target),
                    "size_bytes": target.stat().st_size,
                }
            )
            embedded += len(rows)
        mapping_output.flush()
        os.fsync(mapping_output.fileno())
    if embedded != record_count or sha256_file(records_path) != source["records_sha256"]:
        raise RuntimeError("multi-view source changed or was not completely embedded")
    artifacts = [artifact_entry(mapping_path, relative_to=output, kind="embedding")]
    artifacts.extend(
        artifact_entry(output / cast(str, shard["path"]), relative_to=output, kind="embedding")
        for shard in shards
    )
    report: dict[str, object] = {
        "schema_version": 1,
        "complete": True,
        "publication_allowed": False,
        "publication_gate": "pending_multiview_ablation",
        "model": MODEL,
        "revision": MODEL_REVISION,
        "model_dimension": MODEL_DIMENSION,
        "output_dimension": OUTPUT_DIMENSION,
        "reference_dimension": MODEL_DIMENSION,
        "selected_dimension": OUTPUT_DIMENSION,
        "mrl_policy": "prefix_1024_then_l2_renormalize",
        "dtype": "float16",
        "normalized": True,
        "model_max_length": MODEL_MAX_LENGTH,
        "view_policy_version": VIEW_POLICY_VERSION,
        "view_kinds": list(VIEW_KINDS),
        "dataset_sha256": source["dataset_sha256"],
        "input_manifest_sha256": sha256_file(input_manifest_path),
        "tokenizer_sha256": source["tokenizer_sha256"],
        "jobs_sha256": source["jobs_sha256"],
        "job_row_order_sha256": source["job_row_order_sha256"],
        "document_policy_version": source["document_policy_version"],
        "records": record_count,
        "records_sha256": source["records_sha256"],
        "encoder_backend": encoder_backend,
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
        "publication_gate",
        "model",
        "revision",
        "model_dimension",
        "output_dimension",
        "reference_dimension",
        "selected_dimension",
        "mrl_policy",
        "dtype",
        "normalized",
        "model_max_length",
        "view_policy_version",
        "view_kinds",
        "dataset_sha256",
        "input_manifest_sha256",
        "tokenizer_sha256",
        "jobs_sha256",
        "job_row_order_sha256",
        "document_policy_version",
        "records",
        "records_sha256",
        "encoder_backend",
        "mapping_path",
        "shards",
        "artifacts",
    }
    exact_keys(manifest, expected_keys, "multi-view embedding manifest")
    if (
        manifest["schema_version"] != 1
        or manifest["complete"] is not True
        or manifest["publication_allowed"] is not False
        or manifest["publication_gate"] != "pending_multiview_ablation"
        or manifest["model"] != MODEL
        or manifest["revision"] != MODEL_REVISION
        or manifest["model_dimension"] != MODEL_DIMENSION
        or manifest["output_dimension"] != OUTPUT_DIMENSION
        or manifest["reference_dimension"] != MODEL_DIMENSION
        or manifest["selected_dimension"] != OUTPUT_DIMENSION
        or manifest["mrl_policy"] != "prefix_1024_then_l2_renormalize"
        or manifest["dtype"] != "float16"
        or manifest["normalized"] is not True
        or manifest["view_policy_version"] != VIEW_POLICY_VERSION
        or manifest["view_kinds"] != list(VIEW_KINDS)
    ):
        raise RuntimeError("multi-view embedding contract differs")
    for name in (
        "input_manifest_sha256",
        "tokenizer_sha256",
        "dataset_sha256",
        "jobs_sha256",
        "job_row_order_sha256",
        "records_sha256",
    ):
        require_sha256(manifest[name], name)
    verify_local_inventory(output, manifest["artifacts"])
    mapping_path = output / cast(str, manifest["mapping_path"])
    mapping_rows = 0
    with mapping_path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError(f"invalid embedding mapping at line {line_number}") from error
            if not isinstance(row, dict):
                raise RuntimeError("embedding mapping row must be an object")
            exact_keys(
                row,
                {"embedding_row", "job_id", "job_row", "kind", "view_index"},
                f"embedding mapping row {line_number}",
            )
            if row["embedding_row"] != mapping_rows:
                raise RuntimeError("embedding mapping rows are not contiguous")
            mapping_rows += 1
    shards = manifest["shards"]
    if not isinstance(shards, list) or not shards:
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
    if expected_start != manifest["records"] or mapping_rows != manifest["records"]:
        raise RuntimeError("multi-view aggregate row count differs")
    return {
        "passed": True,
        "records": expected_start,
        "shards": len(shards),
        "manifest_sha256": sha256_file(output / "manifest.json"),
    }


def approve_embeddings(
    *,
    output: Path,
    promotion_report_path: Path,
    promotion_report_sha256: str,
    attestation_path: Path,
) -> dict[str, object]:
    if attestation_path.exists():
        raise RuntimeError("promotion attestation already exists; approvals never overwrite")
    validation = verify_embeddings(output)
    manifest_sha256 = cast(str, validation["manifest_sha256"])
    promotion = _promotion_evidence(promotion_report_path, promotion_report_sha256)
    if promotion["candidate_manifest_sha256"] != manifest_sha256:
        raise RuntimeError("promotion report evaluates a different embedding manifest")
    attestation: dict[str, object] = {
        "schema_version": 1,
        "complete": True,
        "publication_allowed": True,
        "candidate_manifest_sha256": manifest_sha256,
        "promotion_evidence": promotion,
    }
    atomic_json(attestation_path, attestation)
    return attestation


def verify_s3(
    output: Path,
    *,
    bucket: str,
    prefix: str,
    expected_owner: str,
    manifest_sha256: str,
    profile: str | None,
    region: str,
) -> None:
    require_sha256(manifest_sha256, "published manifest SHA-256")
    manifest_path = output / "manifest.json"
    if sha256_file(manifest_path) != manifest_sha256:
        raise RuntimeError("local manifest differs from the approved publication SHA-256")
    manifest = read_json_object(manifest_path, "multi-view embedding manifest")
    artifacts = verify_local_inventory(output, manifest["artifacts"])
    if region != "us-west-2":
        raise RuntimeError("multi-view S3 publication is pinned to us-west-2")
    session = boto3.Session(profile_name=profile, region_name=region)
    identity = cast(AwsIdentity, session.client("sts")).get_caller_identity()
    if identity.get("Account") != expected_owner:
        raise RuntimeError("AWS caller identity differs from expected S3 owner")
    s3 = session.client("s3")
    clean_prefix = prefix.strip("/")
    if not clean_prefix or clean_prefix.rsplit("/", 1)[-1] != manifest_sha256:
        raise ValueError("S3 prefix must end with the manifest SHA-256")
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


def publish_s3(
    output: Path,
    *,
    bucket: str,
    prefix: str,
    expected_owner: str,
    profile: str | None,
    region: str,
) -> dict[str, object]:
    validation = verify_embeddings(output)
    if region != "us-west-2":
        raise RuntimeError("multi-view S3 publication is pinned to us-west-2")
    session = boto3.Session(profile_name=profile, region_name=region)
    identity = cast(AwsIdentity, session.client("sts")).get_caller_identity()
    if identity.get("Account") != expected_owner:
        raise RuntimeError("AWS caller identity differs from expected S3 owner")
    manifest = read_json_object(output / "manifest.json", "multi-view embedding manifest")
    result = publish_s3_directory(
        root=output,
        bucket=bucket,
        prefix=prefix,
        expected_owner=expected_owner,
        artifacts=manifest["artifacts"],
        s3=session.client("s3"),
    )
    if result["manifest_sha256"] != validation["manifest_sha256"]:
        raise RuntimeError("published manifest identity changed after validation")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    records = commands.add_parser("build-records")
    records.add_argument("--jobs-csv", type=Path, required=True)
    records.add_argument("--output", type=Path, required=True)
    records.add_argument("--model-snapshot", type=Path, required=True)
    build = commands.add_parser("build")
    build.add_argument("--records", type=Path, required=True)
    build.add_argument("--input-manifest", type=Path, required=True)
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
    approve = commands.add_parser("approve")
    approve.add_argument("--output", type=Path, required=True)
    approve.add_argument("--promotion-report", type=Path, required=True)
    approve.add_argument("--promotion-report-sha256", required=True)
    approve.add_argument("--attestation", type=Path, required=True)
    publish = commands.add_parser("publish-s3")
    publish.add_argument("--output", type=Path, required=True)
    publish.add_argument("--bucket", required=True)
    publish.add_argument("--prefix", required=True)
    publish.add_argument("--expected-owner", default="378849533305")
    publish.add_argument("--profile")
    publish.add_argument("--region", default="us-west-2")
    s3 = commands.add_parser("verify-s3")
    s3.add_argument("--output", type=Path, required=True)
    s3.add_argument("--bucket", required=True)
    s3.add_argument("--prefix", required=True)
    s3.add_argument("--expected-owner", default="378849533305")
    s3.add_argument("--manifest-sha256", required=True)
    s3.add_argument("--profile")
    s3.add_argument("--region", default="us-west-2")
    args = parser.parse_args()
    if args.command == "build-records":
        tokenizer = load_tokenizer(args.model_snapshot)
        result = build_records(
            jobs_csv=args.jobs_csv,
            output=args.output,
            tokenizer=tokenizer,
            tokenizer_sha256=sha256_file(args.model_snapshot / "tokenizer.json"),
        )
    elif args.command == "verify":
        result = verify_embeddings(args.output)
    elif args.command == "approve":
        result = approve_embeddings(
            output=args.output,
            promotion_report_path=args.promotion_report,
            promotion_report_sha256=args.promotion_report_sha256,
            attestation_path=args.attestation,
        )
    elif args.command == "publish-s3":
        result = publish_s3(
            args.output,
            bucket=args.bucket,
            prefix=args.prefix,
            expected_owner=args.expected_owner,
            profile=args.profile,
            region=args.region,
        )
    elif args.command == "verify-s3":
        verify_s3(
            args.output,
            bucket=args.bucket,
            prefix=args.prefix,
            expected_owner=args.expected_owner,
            manifest_sha256=args.manifest_sha256,
            profile=args.profile,
            region=args.region,
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
            output=args.output,
            encoder=encoder,
            encoder_backend=backend,
            shard_size=args.shard_size,
            batch_size=args.batch_size,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
