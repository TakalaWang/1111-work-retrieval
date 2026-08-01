#!/usr/bin/env python3
"""Materialize core-exact runtime components from verified research artifacts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import tempfile
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import promote_runtime_artifacts as contract

WHOLE_DESTINATION = Path("runtime/embeddings/qwen3-embedding-8b/whole")
TANTIVY_DESTINATION = Path("runtime/indexes/tantivy-bm25-temporal-v1")
PROVENANCE_DESTINATION = Path(contract.APPROVED_WHOLE_BUILD_PROVENANCE_PATH)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for child in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        if child.is_symlink():
            raise RuntimeError("Tantivy source index contains a symbolic link")
        digest.update(child.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        with child.open("rb") as source:
            for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


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


def _place(source: Path, destination: Path, mode: str) -> None:
    if not source.is_file() or source.is_symlink():
        raise RuntimeError(f"runtime source is not a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if mode == "hardlink":
        os.link(source, destination)
    elif mode == "copy":
        shutil.copyfile(source, destination)
    else:
        raise ValueError("link mode must be hardlink or copy")


def _terms_csv(path: Path) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    with path.open(encoding="utf-8-sig", newline="") as source:
        rows = csv.DictReader(source)
        required = {"CodeNo", "CodeNameA", "CodeNameB", "CodeNameC"}
        if rows.fieldnames is None or not required.issubset(rows.fieldnames):
            raise RuntimeError(f"taxonomy CSV has incompatible columns: {path}")
        for row in rows:
            code = row["CodeNo"].strip()
            if not code.isascii() or not code.isdecimal() or code in result:
                raise RuntimeError(f"taxonomy CSV has invalid or repeated code: {code!r}")
            terms = list(
                dict.fromkeys(
                    canonical
                    for name in (row["CodeNameA"], row["CodeNameB"], row["CodeNameC"])
                    if (
                        canonical := " ".join(
                            unicodedata.normalize("NFKC", name).casefold().split()
                        )
                    )
                )
            )
            if not terms:
                raise RuntimeError(f"taxonomy CSV code has no terms: {code}")
            result[code] = terms
    if not result:
        raise RuntimeError(f"taxonomy CSV is empty: {path}")
    return result


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
    tantivy_build_root: Path,
    city_taxonomy_csv: Path,
    duty_taxonomy_csv: Path,
    output_root: Path,
    source_manifest_key: str,
    link_mode: str,
    approved_whole_build_sha256: str = contract.APPROVED_WHOLE_BUILD_MANIFEST_SHA256,
) -> None:
    if output_root.exists():
        raise RuntimeError("materialization output already exists")
    if not source_manifest_key.endswith("/manifest.json"):
        raise RuntimeError("source manifest key must end with /manifest.json")
    contract._validate_source_path(source_manifest_key)
    output_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_root.name}.", dir=output_root.parent))
    try:
        whole_source_manifest_path = whole_build_root / "manifest.json"
        if _sha256(whole_source_manifest_path) != approved_whole_build_sha256:
            raise RuntimeError("whole build manifest is not the approved EVA artifact")
        whole = _read_object(whole_source_manifest_path, "whole build manifest")
        expected_whole = {
            "complete": True,
            "model": contract.APPROVED_MODEL,
            "revision": contract.APPROVED_MODEL_REVISION,
            "dtype": "float16",
            "normalized": True,
            "document_policy_version": contract.APPROVED_DOCUMENT_POLICY_VERSION,
            "document_template_sha256": contract.APPROVED_DOCUMENT_TEMPLATE_SHA256,
            "document_fields": contract.APPROVED_DOCUMENT_FIELDS,
        }
        contract._require_equal("whole build", expected_whole, whole)
        rows = whole.get("rows")
        shards = whole.get("shards")
        if type(rows) is not int or rows < 1 or not isinstance(shards, list) or not shards:
            raise RuntimeError("whole build row/shard contract differs")

        whole_destination = temporary / WHOLE_DESTINATION
        job_ids: list[str] = []
        seen_ids: set[str] = set()
        serving_shards: list[dict[str, object]] = []
        row_order = hashlib.sha256()
        row_start = 0
        for expected_index, raw in enumerate(shards):
            if not isinstance(raw, dict) or raw.get("index") != expected_index:
                raise RuntimeError("whole build shards are not contiguous")
            shard_rows = raw.get("rows")
            if type(shard_rows) is not int or shard_rows < 1 or raw.get("dimension") != 4096:
                raise RuntimeError("whole build shard shape differs")
            ids_source = whole_build_root / f"job-ids-{expected_index:05d}.json"
            vectors_source = whole_build_root / f"embeddings-{expected_index:05d}.f16.npy"
            if _sha256(ids_source) != raw.get("job_ids_file_sha256") or _sha256(
                vectors_source
            ) != raw.get("embedding_sha256"):
                raise RuntimeError(f"whole build shard checksum differs: {expected_index}")
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
                raise RuntimeError(f"whole build shard job IDs differ: {expected_index}")
            repeated = seen_ids.intersection(raw_ids)
            if repeated:
                raise RuntimeError("whole build contains repeated job IDs")
            seen_ids.update(raw_ids)
            job_ids.extend(raw_ids)
            for job_id in raw_ids:
                row_order.update(job_id.encode())
                row_order.update(b"\n")
            vector_path = f"{WHOLE_DESTINATION.as_posix()}/shards/{expected_index:05d}.f16.npy"
            _place(
                vectors_source,
                temporary / vector_path,
                link_mode,
            )
            serving_shards.append(
                {
                    "vectors_path": vector_path.removeprefix("runtime/"),
                    "row_start": row_start,
                    "row_end": row_start + shard_rows,
                    "rows": shard_rows,
                    "dimension": 4096,
                }
            )
            row_start += shard_rows
        if (
            row_start != rows
            or row_order.hexdigest() != whole.get("job_row_order_sha256")
            or len(job_ids) != len(seen_ids)
        ):
            raise RuntimeError("whole build global job row order differs")

        job_ids_runtime_path = f"{WHOLE_DESTINATION.as_posix()}/job-ids.json".removeprefix(
            "runtime/"
        )
        (whole_destination / "job-ids.json").write_text(
            json.dumps(job_ids, ensure_ascii=True, separators=(",", ":")) + "\n",
            encoding="ascii",
        )
        whole_component = {
            "schema_version": 1,
            "complete": True,
            "model": contract.APPROVED_MODEL,
            "revision": contract.APPROVED_MODEL_REVISION,
            "dimension": 4096,
            "dtype": "float16",
            "normalized": True,
            "rows": rows,
            "dataset_sha256": whole["dataset_sha256"],
            "jobs_sha256": whole["jobs_sha256"],
            "job_row_order_sha256": whole["job_row_order_sha256"],
            "document_policy_version": contract.APPROVED_DOCUMENT_POLICY_VERSION,
            "document_template_sha256": contract.APPROVED_DOCUMENT_TEMPLATE_SHA256,
            "document_fields": contract.APPROVED_DOCUMENT_FIELDS,
            "query_prompt": contract.APPROVED_QUERY_PROMPT,
            "job_ids_path": job_ids_runtime_path,
            "shards": serving_shards,
        }
        whole_payload = _write_json(whole_destination / "manifest.json", whole_component)

        tantivy_source_manifest = _read_object(
            tantivy_build_root / "manifest.json", "Tantivy build manifest"
        )
        contract._require_equal(
            "Tantivy build",
            {
                "complete": True,
                "engine": contract.APPROVED_TANTIVY_ENGINE,
                "jobs_sha256": whole["jobs_sha256"],
                "job_row_order_sha256": whole["job_row_order_sha256"],
                "updated_at_field": "updated_at_epoch_ms",
                "filter_semantics": contract.TANTIVY_FILTER_SEMANTICS,
                "fields": contract.APPROVED_TANTIVY_FIELD_BOOSTS,
            },
            tantivy_source_manifest,
        )
        source_index = tantivy_build_root / "index"
        if _tree_sha256(source_index) != tantivy_source_manifest.get("index_sha256"):
            raise RuntimeError("Tantivy build index checksum differs")
        tantivy_destination = temporary / TANTIVY_DESTINATION
        index_files: list[str] = []
        for source in sorted(
            candidate for candidate in source_index.rglob("*") if candidate.is_file()
        ):
            relative = source.relative_to(source_index)
            destination = tantivy_destination / "index" / relative
            _place(source, destination, link_mode)
            index_files.append(
                f"{TANTIVY_DESTINATION.as_posix()}/index/{relative.as_posix()}".removeprefix(
                    "runtime/"
                )
            )
        if not index_files:
            raise RuntimeError("Tantivy build index is empty")
        taxonomy_runtime_path = (
            f"{TANTIVY_DESTINATION.as_posix()}/filter-taxonomy.json".removeprefix("runtime/")
        )
        _write_json(
            tantivy_destination / "filter-taxonomy.json",
            {
                "schema_version": 1,
                "location_code_to_terms": _terms_csv(city_taxonomy_csv),
                "duty_code_to_terms": _terms_csv(duty_taxonomy_csv),
            },
        )
        tantivy_component = {
            "schema_version": 1,
            "complete": True,
            "engine": contract.APPROVED_TANTIVY_ENGINE,
            "jobs_sha256": whole["jobs_sha256"],
            "job_row_order_sha256": whole["job_row_order_sha256"],
            "index_sha256": tantivy_source_manifest["index_sha256"],
            "index_directory": f"{TANTIVY_DESTINATION.as_posix()}/index".removeprefix("runtime/"),
            "index_files": index_files,
            "taxonomy_path": taxonomy_runtime_path,
            "schema_fields": contract.APPROVED_TANTIVY_SCHEMA_FIELDS,
            "field_boosts": contract.APPROVED_TANTIVY_FIELD_BOOSTS,
            "filter_semantics": contract.TANTIVY_FILTER_SEMANTICS,
            "updated_at_field": "updated_at_epoch_ms",
            "temporal_filter_semantics": contract.TEMPORAL_FILTER_SEMANTICS,
        }
        tantivy_payload = _write_json(tantivy_destination / "manifest.json", tantivy_component)

        provenance_path = temporary / PROVENANCE_DESTINATION
        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(whole_source_manifest_path, provenance_path)
        report_path = Path("provenance/materialization-report.json")
        _write_json(
            temporary / report_path,
            {
                "schema_version": 1,
                "whole_build_manifest_sha256": approved_whole_build_sha256,
                "whole_runtime_manifest_sha256": hashlib.sha256(whole_payload).hexdigest(),
                "tantivy_build_manifest_sha256": _sha256(tantivy_build_root / "manifest.json"),
                "tantivy_runtime_manifest_sha256": hashlib.sha256(tantivy_payload).hexdigest(),
                "dataset_sha256": whole["dataset_sha256"],
                "jobs_sha256": whole["jobs_sha256"],
                "job_row_order_sha256": whole["job_row_order_sha256"],
                "rows": rows,
                "link_mode": link_mode,
                "city_taxonomy_sha256": _sha256(city_taxonomy_csv),
                "duty_taxonomy_sha256": _sha256(duty_taxonomy_csv),
            },
        )

        source_manifest_path = Path("manifest.json")
        release_spec_path = Path("runtime-release-spec.json")
        source_manifest = {
            "schema_version": 3,
            "files": _artifact_inventory(temporary, {source_manifest_path, release_spec_path}),
        }
        source_payload = _write_json(temporary / source_manifest_path, source_manifest)
        selections = [
            {
                "source_prefix": f"{WHOLE_DESTINATION.as_posix()}/",
                "destination_prefix": "embeddings/qwen3-embedding-8b/whole/",
                "kind": "embedding",
            },
            {
                "source_prefix": f"{TANTIVY_DESTINATION.as_posix()}/",
                "destination_prefix": "indexes/tantivy-bm25-temporal-v1/",
                "kind": "index",
            },
        ]
        selected = _selected_inventory(source_manifest, selections)
        release_spec = {
            "schema_version": 1,
            "source_manifest": {
                "key": source_manifest_key,
                "sha256": hashlib.sha256(source_payload).hexdigest(),
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
                        "manifest_path": "embeddings/qwen3-embedding-8b/whole/manifest.json",
                        "manifest_sha256": hashlib.sha256(whole_payload).hexdigest(),
                        "complete": True,
                        "model": contract.APPROVED_MODEL,
                        "revision": contract.APPROVED_MODEL_REVISION,
                        "dimension": 4096,
                        "dtype": "float16",
                        "normalized": True,
                        "rows": rows,
                        "dataset_sha256": whole["dataset_sha256"],
                        "jobs_sha256": whole["jobs_sha256"],
                        "job_row_order_sha256": whole["job_row_order_sha256"],
                        "document_policy_version": contract.APPROVED_DOCUMENT_POLICY_VERSION,
                        "document_template_sha256": contract.APPROVED_DOCUMENT_TEMPLATE_SHA256,
                    },
                    "temporal_tantivy": {
                        "manifest_path": "indexes/tantivy-bm25-temporal-v1/manifest.json",
                        "manifest_sha256": hashlib.sha256(tantivy_payload).hexdigest(),
                        "complete": True,
                        "index_sha256": tantivy_source_manifest["index_sha256"],
                        "engine": contract.APPROVED_TANTIVY_ENGINE,
                        "jobs_sha256": whole["jobs_sha256"],
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
        temporary.replace(output_root)
    except Exception:
        shutil.rmtree(temporary)
        raise


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--whole-build-root", type=Path, required=True)
    parser.add_argument("--tantivy-build-root", type=Path, required=True)
    parser.add_argument("--city-taxonomy-csv", type=Path, required=True)
    parser.add_argument("--duty-taxonomy-csv", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-manifest-key", required=True)
    parser.add_argument("--link-mode", choices=("hardlink", "copy"), default="hardlink")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    materialize(
        whole_build_root=args.whole_build_root,
        tantivy_build_root=args.tantivy_build_root,
        city_taxonomy_csv=args.city_taxonomy_csv,
        duty_taxonomy_csv=args.duty_taxonomy_csv,
        output_root=args.output_root,
        source_manifest_key=args.source_manifest_key,
        link_mode=args.link_mode,
    )
    print(json.dumps({"complete": True, "output_root": str(args.output_root)}, sort_keys=True))


if __name__ == "__main__":
    main()
