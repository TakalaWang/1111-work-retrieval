#!/usr/bin/env python3
"""Materialize core-exact runtime components from verified research artifacts."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import cast

import numpy as np
import promote_runtime_artifacts as contract
from work_retrieval_core.adapters import CorpusQueryCompiler

WHOLE_DESTINATION = Path("runtime") / contract.WHOLE_RUNTIME_PREFIX
TANTIVY_DESTINATION = Path("runtime") / contract.TANTIVY_RUNTIME_PREFIX
PROVENANCE_DESTINATION = Path(contract.WHOLE_SOURCE_MANIFEST_SOURCE_PATH)
SOURCE_INVENTORY_DESTINATION = Path(contract.WHOLE_SOURCE_INVENTORY_SOURCE_PATH)
TANTIVY_PROVENANCE_DESTINATION = Path(contract.TANTIVY_BUILD_PROVENANCE_SOURCE_PATH)
REPORT_DESTINATION = Path("runtime") / contract.MATERIALIZATION_REPORT_PATH
SOURCE_CACHE_INVENTORY_PREFIX = "artifacts/experiments/qwen3-8b/full/"
TANTIVY_COMPONENT_KEYS = {
    "schema_version",
    "complete",
    "engine",
    "jobs_sha256",
    "job_row_order_sha256",
    "index_sha256",
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
    "filter_semantics",
    "updated_at_field",
    "temporal_filter_semantics",
}
TANTIVY_BUILD_KEYS = {
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
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree(path: Path) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    for child in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        if child.is_symlink():
            raise RuntimeError("Tantivy source index contains a symbolic link")
        values.append(
            {
                "path": child.relative_to(path).as_posix(),
                "sha256": _sha256(child),
                "size_bytes": child.stat().st_size,
            }
        )
    if not values:
        raise RuntimeError("Tantivy source index is empty")
    return values


def _tree_sha256(tree: object) -> str:
    return contract._canonical_sha256(tree)


def _read_object(path: Path, name: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{name} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{name} must be a JSON object")
    return cast(dict[str, object], value)


def _write_json(path: Path, value: object) -> bytes:
    payload = contract.canonical_bytes(cast(Mapping[str, object], value))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def _copy_verified(source: Path, destination: Path, expected_sha256: str) -> None:
    if not source.is_file() or source.is_symlink():
        raise RuntimeError(f"runtime source is not a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise RuntimeError(f"runtime destination already exists: {destination}")
    shutil.copyfile(source, destination)
    if _sha256(destination) != expected_sha256:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"copied runtime artifact checksum differs: {destination}")


def _component_source(root: Path, runtime_path: object, prefix: str, name: str) -> Path:
    if not isinstance(runtime_path, str):
        raise RuntimeError(f"{name} path must be a string")
    marker = prefix.rstrip("/") + "/"
    if not runtime_path.startswith(marker):
        raise RuntimeError(f"{name} path differs from its component prefix")
    suffix = PurePosixPath(runtime_path.removeprefix(marker))
    if (
        suffix.is_absolute()
        or not suffix.parts
        or any(part in {"", ".", ".."} for part in suffix.parts)
    ):
        raise RuntimeError(f"{name} path is unsafe")
    candidate = root.joinpath(*suffix.parts)
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise RuntimeError(f"{name} path escapes its component")
    return candidate


def _source_inventory(path: Path, whole_root: Path) -> dict[str, dict[str, object]]:
    if _sha256(path) != contract.APPROVED_WHOLE_SOURCE_INVENTORY_SHA256:
        raise RuntimeError("whole source inventory is not approved")
    value = _read_object(path, "whole source inventory")
    files = value.get("files")
    if value.get("schema_version") != 3 or not isinstance(files, list):
        raise RuntimeError("whole source inventory schema differs")
    selected: dict[str, dict[str, object]] = {}
    for raw in files:
        if not isinstance(raw, dict) or set(raw) != {"path", "sha256", "size"}:
            raise RuntimeError("whole source inventory entry differs")
        source_path = raw.get("path")
        sha256 = raw.get("sha256")
        size = raw.get("size")
        if not isinstance(source_path, str) or not source_path.startswith(
            SOURCE_CACHE_INVENTORY_PREFIX
        ):
            continue
        relative = source_path.removeprefix(SOURCE_CACHE_INVENTORY_PREFIX)
        candidate = PurePosixPath(relative)
        if (
            not relative
            or candidate.is_absolute()
            or any(part in {"", ".", ".."} for part in candidate.parts)
            or not isinstance(sha256, str)
            or len(sha256) != 64
            or type(size) is not int
            or size < 0
            or relative in selected
        ):
            raise RuntimeError("whole source inventory entry is unsafe")
        local = whole_root.joinpath(*candidate.parts)
        if (
            not local.is_file()
            or local.is_symlink()
            or local.stat().st_size != size
            or _sha256(local) != sha256
        ):
            raise RuntimeError(f"whole source cache file differs from inventory: {relative}")
        selected[relative] = cast(dict[str, object], raw)
    if (
        len(selected) != contract.APPROVED_WHOLE_SOURCE_FILE_COUNT
        or sum(cast(int, item["size"]) for item in selected.values())
        != contract.APPROVED_WHOLE_SOURCE_BYTES
    ):
        raise RuntimeError("whole source cache inventory count or bytes differ")
    local_files = {
        candidate.relative_to(whole_root).as_posix()
        for candidate in whole_root.rglob("*")
        if candidate.is_file()
    }
    if local_files != set(selected):
        raise RuntimeError("whole source cache files differ from the approved inventory")
    manifest = selected.get("manifest.json")
    if manifest is None or manifest.get("sha256") != contract.APPROVED_WHOLE_SOURCE_MANIFEST_SHA256:
        raise RuntimeError("whole source cache inventory does not pin its manifest")
    return selected


def _project_mrl_prefix(
    source: Path, destination: Path, expected_source_sha256: str, expected_rows: int
) -> None:
    if not source.is_file() or source.is_symlink():
        raise RuntimeError(f"runtime source is not a regular file: {source}")
    if _sha256(source) != expected_source_sha256:
        raise RuntimeError(f"whole build source vector checksum differs: {source}")
    try:
        source_vectors = np.load(source, mmap_mode="r", allow_pickle=False)
    except (OSError, ValueError) as error:
        raise RuntimeError(
            f"whole build source vector is not a valid NumPy array: {source}"
        ) from error
    if source_vectors.dtype != np.float16 or source_vectors.shape != (
        expected_rows,
        contract.APPROVED_SOURCE_EMBEDDING_DIMENSION,
    ):
        raise RuntimeError(f"whole build source vector shape/dtype differs: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise RuntimeError(f"runtime destination already exists: {destination}")
    output = np.lib.format.open_memmap(
        destination,
        mode="w+",
        dtype=np.float16,
        shape=(expected_rows, contract.APPROVED_WHOLE_DIMENSION),
    )
    for start in range(0, expected_rows, 4_096):
        end = min(start + 4_096, expected_rows)
        prefix = np.asarray(
            source_vectors[start:end, : contract.APPROVED_WHOLE_DIMENSION], dtype=np.float32
        )
        norms = np.linalg.norm(prefix, axis=1)
        if not np.isfinite(prefix).all() or not np.isfinite(norms).all() or np.any(norms <= 0):
            raise RuntimeError(f"whole build MRL prefix is non-finite or zero-norm: {source}")
        output[start:end] = (prefix / norms[:, None]).astype(np.float16)
    output.flush()
    del output
    projected = np.load(destination, mmap_mode="r", allow_pickle=False)
    projected_norms = np.linalg.norm(np.asarray(projected, dtype=np.float32), axis=1)
    if (
        projected.dtype != np.float16
        or projected.shape != (expected_rows, contract.APPROVED_WHOLE_DIMENSION)
        or not np.isfinite(projected_norms).all()
        or not np.allclose(projected_norms, 1.0, rtol=0, atol=2e-3)
    ):
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"materialized MRL prefix is invalid: {destination}")


def _publish_exclusive(source: Path, destination: Path) -> None:
    """Atomically publish a directory without replacing an existing destination."""

    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform == "darwin":
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(source_bytes, destination_bytes, 0x00000004)
    elif sys.platform.startswith("linux"):
        rename = libc.renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(-100, source_bytes, -100, destination_bytes, 0x00000001)
    else:
        raise RuntimeError(f"atomic exclusive directory publish is unsupported on {sys.platform}")
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise RuntimeError(f"materialization output already exists: {destination}")
    raise OSError(error_number, os.strerror(error_number), destination)


def _artifact_inventory(root: Path, excluded: set[Path]) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        relative = path.relative_to(root)
        if relative in excluded:
            continue
        files.append(
            {"path": relative.as_posix(), "sha256": _sha256(path), "size": path.stat().st_size}
        )
    return files


def _selected_inventory(
    source: Mapping[str, object], selections: list[dict[str, str]]
) -> list[dict[str, object]]:
    files = source.get("files")
    if not isinstance(files, list):
        raise RuntimeError("materialized source inventory is invalid")
    selected: list[dict[str, object]] = []
    for raw in files:
        if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
            raise RuntimeError("materialized source inventory entry is invalid")
        path = raw["path"]
        matches = [rule for rule in selections if path.startswith(rule["source_prefix"])]
        if len(matches) != 1:
            continue
        rule = matches[0]
        selected.append(
            {
                "source_path": path,
                "path": rule["destination_prefix"] + path.removeprefix(rule["source_prefix"]),
                "kind": rule["kind"],
                "sha256": raw["sha256"],
                "size_bytes": raw["size"],
            }
        )
    return sorted(selected, key=lambda item: cast(str, item["path"]))


def materialize(
    *,
    whole_build_root: Path,
    whole_source_inventory: Path,
    tantivy_build_root: Path,
    output_root: Path,
    approved_tantivy_component_sha256: str,
    approved_tantivy_build_sha256: str,
    approved_tantivy_index_sha256: str,
) -> None:
    if output_root.exists():
        raise RuntimeError("materialization output already exists")
    _source_inventory(whole_source_inventory, whole_build_root)
    output_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_root.name}.", dir=output_root.parent))
    try:
        whole_source_manifest_path = whole_build_root / "manifest.json"
        if _sha256(whole_source_manifest_path) != contract.APPROVED_WHOLE_SOURCE_MANIFEST_SHA256:
            raise RuntimeError("whole source manifest is not the approved sealed cache")
        whole = _read_object(whole_source_manifest_path, "whole source manifest")
        expected_whole = {
            "complete": True,
            "model": contract.APPROVED_MODEL,
            "revision": contract.APPROVED_MODEL_REVISION,
            "dtype": "float16",
            "normalized": True,
            "document_policy_version": contract.APPROVED_DOCUMENT_POLICY_VERSION,
            "document_template_sha256": contract.APPROVED_DOCUMENT_TEMPLATE_SHA256,
            "document_fields": contract.APPROVED_DOCUMENT_FIELDS,
            "dataset_sha256": contract.APPROVED_JOBS_DATASET_SHA256,
            "rows": contract.APPROVED_WHOLE_SOURCE_ROWS,
        }
        contract._require_equal("whole source", expected_whole, whole)
        rows = whole.get("rows")
        shards = whole.get("shards")
        if (
            type(rows) is not int
            or rows < 1
            or not isinstance(shards, list)
            or len(shards) != contract.APPROVED_WHOLE_SOURCE_SHARDS
        ):
            raise RuntimeError("whole source row/shard contract differs")

        whole_destination = temporary / WHOLE_DESTINATION
        job_ids: list[str] = []
        seen_ids: set[str] = set()
        serving_shards: list[dict[str, object]] = []
        row_order = hashlib.sha256()
        row_start = 0
        for expected_index, raw in enumerate(shards):
            if not isinstance(raw, dict) or raw.get("index") != expected_index:
                raise RuntimeError("whole source shards are not contiguous")
            shard_rows = raw.get("rows")
            if type(shard_rows) is not int or shard_rows < 1 or raw.get("dimension") != 4096:
                raise RuntimeError("whole source shard shape differs")
            ids_source = whole_build_root / f"job-ids-{expected_index:05d}.json"
            vectors_source = whole_build_root / f"embeddings-{expected_index:05d}.f16.npy"
            if _sha256(ids_source) != raw.get("job_ids_file_sha256") or _sha256(
                vectors_source
            ) != raw.get("embedding_sha256"):
                raise RuntimeError(f"whole source shard checksum differs: {expected_index}")
            raw_ids = json.loads(ids_source.read_text(encoding="utf-8"))
            if (
                not isinstance(raw_ids, list)
                or len(raw_ids) != shard_rows
                or any(
                    not isinstance(job_id, str) or not job_id.isascii() or not job_id.isdecimal()
                    for job_id in raw_ids
                )
                or hashlib.sha256("\n".join(raw_ids).encode()).hexdigest()
                != raw.get("job_ids_sha256")
            ):
                raise RuntimeError(f"whole source shard job IDs differ: {expected_index}")
            repeated = seen_ids.intersection(raw_ids)
            if repeated:
                raise RuntimeError("whole source contains repeated job IDs")
            seen_ids.update(raw_ids)
            job_ids.extend(raw_ids)
            for job_id in raw_ids:
                row_order.update(job_id.encode())
                row_order.update(b"\n")
            vector_path = f"{WHOLE_DESTINATION.as_posix()}/shards/{expected_index:05d}.f16.npy"
            _project_mrl_prefix(
                vectors_source,
                temporary / vector_path,
                cast(str, raw["embedding_sha256"]),
                shard_rows,
            )
            derived_sha256 = _sha256(temporary / vector_path)
            serving_shards.append(
                {
                    "vectors_path": vector_path.removeprefix("runtime/"),
                    "vectors_sha256": derived_sha256,
                    "source_vectors_sha256": raw["embedding_sha256"],
                    "row_start": row_start,
                    "row_end": row_start + shard_rows,
                    "rows": shard_rows,
                    "dimension": contract.APPROVED_WHOLE_DIMENSION,
                }
            )
            row_start += shard_rows
        if (
            row_start != rows
            or row_order.hexdigest() != whole.get("job_row_order_sha256")
            or len(job_ids) != len(seen_ids)
        ):
            raise RuntimeError("whole source global job row order differs")

        job_ids_runtime_path = f"{WHOLE_DESTINATION.as_posix()}/job-ids.json".removeprefix(
            "runtime/"
        )
        (whole_destination / "job-ids.json").write_text(
            json.dumps(job_ids, ensure_ascii=True, separators=(",", ":")) + "\n",
            encoding="ascii",
        )
        derived_jobs_sha256 = _sha256(whole_destination / "job-ids.json")
        whole_component = {
            "schema_version": 1,
            "complete": True,
            "model": contract.APPROVED_MODEL,
            "revision": contract.APPROVED_MODEL_REVISION,
            "source_dimension": contract.APPROVED_SOURCE_EMBEDDING_DIMENSION,
            "dimension": contract.APPROVED_WHOLE_DIMENSION,
            "projection": contract.APPROVED_WHOLE_PROJECTION,
            "dtype": "float16",
            "normalized": True,
            "rows": rows,
            "dataset_sha256": whole["dataset_sha256"],
            "jobs_sha256": derived_jobs_sha256,
            "job_row_order_sha256": whole["job_row_order_sha256"],
            "document_policy_version": contract.APPROVED_DOCUMENT_POLICY_VERSION,
            "document_template_sha256": contract.APPROVED_DOCUMENT_TEMPLATE_SHA256,
            "document_fields": contract.APPROVED_DOCUMENT_FIELDS,
            "query_prompt": contract.APPROVED_QUERY_PROMPT,
            "source_manifest_path": contract.APPROVED_WHOLE_SOURCE_MANIFEST_PATH,
            "source_manifest_sha256": contract.APPROVED_WHOLE_SOURCE_MANIFEST_SHA256,
            "source_inventory_path": contract.APPROVED_WHOLE_SOURCE_INVENTORY_PATH,
            "source_inventory_sha256": contract.APPROVED_WHOLE_SOURCE_INVENTORY_SHA256,
            "job_ids_path": job_ids_runtime_path,
            "shards": serving_shards,
        }
        whole_payload = _write_json(whole_destination / "manifest.json", whole_component)

        tantivy_source_manifest_path = tantivy_build_root / "manifest.json"
        if _sha256(tantivy_source_manifest_path) != approved_tantivy_component_sha256:
            raise RuntimeError("Tantivy component manifest is not approved")
        tantivy_component = _read_object(tantivy_source_manifest_path, "Tantivy component manifest")
        if set(tantivy_component) != TANTIVY_COMPONENT_KEYS:
            raise RuntimeError("Tantivy component manifest schema differs")
        contract._require_equal(
            "Tantivy component",
            {
                "schema_version": 1,
                "complete": True,
                "engine": contract.APPROVED_TANTIVY_ENGINE,
                "jobs_sha256": derived_jobs_sha256,
                "job_row_order_sha256": whole["job_row_order_sha256"],
                "updated_at_field": "updated_at_epoch_ms",
                "filter_semantics": contract.TANTIVY_FILTER_SEMANTICS,
                "schema_fields": contract.APPROVED_TANTIVY_SCHEMA_FIELDS,
                "field_boosts": contract.APPROVED_TANTIVY_FIELD_BOOSTS,
                "lexical_policy_version": contract.APPROVED_LEXICAL_POLICY_VERSION,
                "lexical_policy_sha256": contract.APPROVED_LEXICAL_POLICY_SHA256,
                "tokenizers": contract.APPROVED_TANTIVY_TOKENIZERS,
                "source_fields": contract.APPROVED_TANTIVY_SOURCE_FIELDS,
                "temporal_filter_semantics": contract.TEMPORAL_FILTER_SEMANTICS,
            },
            tantivy_component,
        )
        tantivy_build_manifest_path = _component_source(
            tantivy_build_root,
            tantivy_component["build_manifest_path"],
            contract.TANTIVY_RUNTIME_PREFIX,
            "Tantivy build manifest",
        )
        if (
            _sha256(tantivy_build_manifest_path) != approved_tantivy_build_sha256
            or tantivy_component["build_manifest_sha256"] != approved_tantivy_build_sha256
        ):
            raise RuntimeError("Tantivy build manifest is not approved")
        tantivy_build = _read_object(tantivy_build_manifest_path, "Tantivy build manifest")
        if set(tantivy_build) != TANTIVY_BUILD_KEYS:
            raise RuntimeError("Tantivy build manifest schema differs")
        if (
            not isinstance(tantivy_build["salary_filter_excluded_rows"], int)
            or isinstance(tantivy_build["salary_filter_excluded_rows"], bool)
            or not 0 <= tantivy_build["salary_filter_excluded_rows"] <= rows
        ):
            raise RuntimeError("Tantivy salary filter exclusion count is invalid")
        contract._require_equal(
            "Tantivy build",
            {
                "schema_version": 1,
                "complete": True,
                "builder": "tantivy_index_pipeline.py",
                "engine": contract.APPROVED_TANTIVY_ENGINE,
                "dataset_sha256": whole["dataset_sha256"],
                "jobs_sha256": derived_jobs_sha256,
                "job_row_order_sha256": whole["job_row_order_sha256"],
                "rows": rows,
                "index_sha256": approved_tantivy_index_sha256,
                "query_corrections": tantivy_component["query_corrections"],
                "lexical_policy_version": contract.APPROVED_LEXICAL_POLICY_VERSION,
                "lexical_policy_sha256": contract.APPROVED_LEXICAL_POLICY_SHA256,
                "tokenizers": contract.APPROVED_TANTIVY_TOKENIZERS,
                "source_fields": contract.APPROVED_TANTIVY_SOURCE_FIELDS,
            },
            tantivy_build,
        )
        source_index = _component_source(
            tantivy_build_root,
            tantivy_component["index_directory"],
            contract.TANTIVY_RUNTIME_PREFIX,
            "Tantivy index",
        )
        source_tree = _tree(source_index)
        expected_index_files = [
            f"{contract.TANTIVY_RUNTIME_PREFIX}/index/{item['path']}" for item in source_tree
        ]
        if (
            tantivy_component.get("index_sha256") != approved_tantivy_index_sha256
            or _tree_sha256(source_tree) != approved_tantivy_index_sha256
            or tantivy_component.get("index_files") != expected_index_files
            or tantivy_build.get("index_tree") != source_tree
        ):
            raise RuntimeError("Tantivy build index checksum differs")
        tantivy_destination = temporary / TANTIVY_DESTINATION
        for item in source_tree:
            relative = cast(str, item["path"])
            _copy_verified(
                source_index / relative,
                tantivy_destination / "index" / relative,
                cast(str, item["sha256"]),
            )
        if _tree_sha256(_tree(tantivy_destination / "index")) != approved_tantivy_index_sha256:
            raise RuntimeError("copied Tantivy index checksum differs")
        taxonomy_source = _component_source(
            tantivy_build_root,
            tantivy_component["taxonomy_path"],
            contract.TANTIVY_RUNTIME_PREFIX,
            "Tantivy taxonomy",
        )
        if _sha256(taxonomy_source) != tantivy_build.get("taxonomy_sha256"):
            raise RuntimeError("Tantivy taxonomy differs from build lineage")
        job_ids_source = _component_source(
            tantivy_build_root,
            tantivy_component["job_ids_path"],
            contract.TANTIVY_RUNTIME_PREFIX,
            "Tantivy job IDs",
        )
        whole_job_ids_path = whole_destination / "job-ids.json"
        if (
            _sha256(job_ids_source) != derived_jobs_sha256
            or job_ids_source.read_bytes() != whole_job_ids_path.read_bytes()
        ):
            raise RuntimeError("Tantivy job IDs differ from sealed whole row order")
        _copy_verified(
            taxonomy_source,
            temporary / "runtime" / cast(str, tantivy_component["taxonomy_path"]),
            _sha256(taxonomy_source),
        )
        _copy_verified(
            job_ids_source,
            temporary / "runtime" / cast(str, tantivy_component["job_ids_path"]),
            derived_jobs_sha256,
        )
        corrections = tantivy_component["query_corrections"]
        if corrections == {"enabled": False}:
            pass
        elif isinstance(corrections, dict):
            expected_correction_keys = {
                "enabled",
                "artifact_path",
                "artifact_sha256",
                "promotion_attestation_path",
                "promotion_attestation_sha256",
            }
            if (
                set(corrections) != expected_correction_keys
                or corrections.get("enabled") is not True
            ):
                raise RuntimeError("enabled Tantivy query correction contract differs")
            correction_source = _component_source(
                tantivy_build_root,
                corrections["artifact_path"],
                contract.TANTIVY_RUNTIME_PREFIX,
                "query correction candidate",
            )
            attestation_source = _component_source(
                tantivy_build_root,
                corrections["promotion_attestation_path"],
                contract.TANTIVY_RUNTIME_PREFIX,
                "query correction attestation",
            )
            if (
                _sha256(correction_source) != corrections["artifact_sha256"]
                or _sha256(attestation_source) != corrections["promotion_attestation_sha256"]
            ):
                raise RuntimeError("enabled Tantivy query correction bytes differ")
            CorpusQueryCompiler.from_promoted_paths(correction_source, attestation_source)
            _copy_verified(
                correction_source,
                temporary / "runtime" / cast(str, corrections["artifact_path"]),
                cast(str, corrections["artifact_sha256"]),
            )
            attestation_destination = (
                Path(contract.TANTIVY_BUILD_PROVENANCE_SOURCE_PATH).parent
                / PurePosixPath(cast(str, corrections["promotion_attestation_path"])).name
            )
            _copy_verified(
                attestation_source,
                temporary / attestation_destination,
                cast(str, corrections["promotion_attestation_sha256"]),
            )
        else:
            raise RuntimeError("Tantivy query corrections must be disabled or attested")
        _copy_verified(
            tantivy_source_manifest_path,
            tantivy_destination / "manifest.json",
            approved_tantivy_component_sha256,
        )
        tantivy_payload = tantivy_source_manifest_path.read_bytes()

        _copy_verified(
            whole_source_manifest_path,
            temporary / PROVENANCE_DESTINATION,
            contract.APPROVED_WHOLE_SOURCE_MANIFEST_SHA256,
        )
        _copy_verified(
            whole_source_inventory,
            temporary / SOURCE_INVENTORY_DESTINATION,
            contract.APPROVED_WHOLE_SOURCE_INVENTORY_SHA256,
        )
        _copy_verified(
            tantivy_build_manifest_path,
            temporary / TANTIVY_PROVENANCE_DESTINATION,
            approved_tantivy_build_sha256,
        )
        _write_json(
            temporary / REPORT_DESTINATION,
            {
                "schema_version": 1,
                "whole_source_manifest_sha256": contract.APPROVED_WHOLE_SOURCE_MANIFEST_SHA256,
                "whole_source_inventory_sha256": (contract.APPROVED_WHOLE_SOURCE_INVENTORY_SHA256),
                "whole_runtime_manifest_sha256": hashlib.sha256(whole_payload).hexdigest(),
                "projection": contract.APPROVED_WHOLE_PROJECTION,
                "tantivy_build_manifest_sha256": approved_tantivy_build_sha256,
                "tantivy_runtime_manifest_sha256": hashlib.sha256(tantivy_payload).hexdigest(),
                "tantivy_index_sha256": approved_tantivy_index_sha256,
                "dataset_sha256": whole["dataset_sha256"],
                "jobs_sha256": derived_jobs_sha256,
                "job_row_order_sha256": whole["job_row_order_sha256"],
                "rows": rows,
                "placement": "copy_sha256_verified",
                "query_corrections": corrections,
            },
        )

        source_manifest_path = Path("manifest.json")
        release_spec_path = Path("runtime-release-spec.json")
        source_manifest = {
            "schema_version": 3,
            "files": _artifact_inventory(temporary, {source_manifest_path, release_spec_path}),
        }
        source_payload = _write_json(temporary / source_manifest_path, source_manifest)
        source_manifest_sha256 = hashlib.sha256(source_payload).hexdigest()
        source_manifest_key = f"one111-search/materialized/{source_manifest_sha256}/manifest.json"
        selections = [
            {
                "source_prefix": f"{WHOLE_DESTINATION.as_posix()}/",
                "destination_prefix": f"{contract.WHOLE_RUNTIME_PREFIX}/",
                "kind": "embedding",
            },
            {
                "source_prefix": f"{TANTIVY_DESTINATION.as_posix()}/",
                "destination_prefix": f"{contract.TANTIVY_RUNTIME_PREFIX}/",
                "kind": "index",
            },
            {
                "source_prefix": "runtime/evidence/provenance/",
                "destination_prefix": "evidence/provenance/",
                "kind": "evidence",
            },
            {
                "source_prefix": f"{PROVENANCE_DESTINATION.parent.as_posix()}/",
                "destination_prefix": f"{contract.WHOLE_RUNTIME_PREFIX}/",
                "kind": "evidence",
            },
            {
                "source_prefix": f"{TANTIVY_PROVENANCE_DESTINATION.parent.as_posix()}/",
                "destination_prefix": f"{contract.TANTIVY_RUNTIME_PREFIX}/",
                "kind": "evidence",
            },
        ]
        selected = _selected_inventory(source_manifest, selections)
        release_spec = {
            "schema_version": 1,
            "source_manifest": {
                "key": source_manifest_key,
                "sha256": source_manifest_sha256,
            },
            "selected_inventory_sha256": contract._canonical_sha256(selected),
            "selections": selections,
            "runtime": {
                "retrieval_policy": {
                    "as_of": {
                        "production_mode": "request_time",
                        "demo_reference": contract.DEMO_AS_OF,
                    },
                    "eligibility": {
                        "updated_within_days": 180,
                        "future_jobs": "retained_with_zero_freshness",
                        "stale_jobs": "exclude",
                        "applied_before_retrieval": True,
                    },
                },
                "incumbents": {
                    "whole_embedding": {
                        "manifest_path": f"{contract.WHOLE_RUNTIME_PREFIX}/manifest.json",
                        "manifest_sha256": hashlib.sha256(whole_payload).hexdigest(),
                        "complete": True,
                        "model": contract.APPROVED_MODEL,
                        "revision": contract.APPROVED_MODEL_REVISION,
                        "dimension": contract.APPROVED_WHOLE_DIMENSION,
                        "source_dimension": contract.APPROVED_SOURCE_EMBEDDING_DIMENSION,
                        "projection": contract.APPROVED_WHOLE_PROJECTION,
                        "dtype": "float16",
                        "normalized": True,
                        "rows": rows,
                        "dataset_sha256": whole["dataset_sha256"],
                        "jobs_sha256": derived_jobs_sha256,
                        "job_row_order_sha256": whole["job_row_order_sha256"],
                        "document_policy_version": contract.APPROVED_DOCUMENT_POLICY_VERSION,
                        "document_template_sha256": contract.APPROVED_DOCUMENT_TEMPLATE_SHA256,
                    },
                    "temporal_tantivy": {
                        "manifest_path": f"{contract.TANTIVY_RUNTIME_PREFIX}/manifest.json",
                        "manifest_sha256": hashlib.sha256(tantivy_payload).hexdigest(),
                        "complete": True,
                        "index_sha256": tantivy_component["index_sha256"],
                        "engine": contract.APPROVED_TANTIVY_ENGINE,
                        "jobs_sha256": derived_jobs_sha256,
                        "job_row_order_sha256": whole["job_row_order_sha256"],
                        "updated_at_field": "updated_at_epoch_ms",
                        "hard_filters": True,
                        "temporal_filter_semantics": contract.TEMPORAL_FILTER_SEMANTICS,
                    },
                },
                "challengers": {name: {"enabled": False} for name in contract.CHALLENGERS},
            },
        }
        _write_json(temporary / release_spec_path, release_spec)
        _publish_exclusive(temporary, output_root)
    except Exception:
        shutil.rmtree(temporary)
        raise


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--whole-build-root", type=Path, required=True)
    parser.add_argument("--whole-source-inventory", type=Path, required=True)
    parser.add_argument("--tantivy-build-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--approved-tantivy-component-sha256", required=True)
    parser.add_argument("--approved-tantivy-build-sha256", required=True)
    parser.add_argument("--approved-tantivy-index-sha256", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    materialize(
        whole_build_root=args.whole_build_root,
        whole_source_inventory=args.whole_source_inventory,
        tantivy_build_root=args.tantivy_build_root,
        output_root=args.output_root,
        approved_tantivy_component_sha256=args.approved_tantivy_component_sha256,
        approved_tantivy_build_sha256=args.approved_tantivy_build_sha256,
        approved_tantivy_index_sha256=args.approved_tantivy_index_sha256,
    )
    print(json.dumps({"complete": True, "output_root": str(args.output_root)}, sort_keys=True))


if __name__ == "__main__":
    main()
