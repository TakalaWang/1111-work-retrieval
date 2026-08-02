#!/usr/bin/env python3
"""Safely materialize the fixed competition dataset from its download ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from import_jobs_to_aws import SOURCE_BYTES, SOURCE_SHA256, validate_source
from pipeline_contract import atomic_json, sha256_file
from tantivy_index_pipeline import _taxonomy_csv

REQUIRED_FILES = ("職缺.csv", "城市對照表.csv", "職務對照表.csv")
EXPECTED_FILES = {
    "職缺.csv": {"sha256": SOURCE_SHA256, "size_bytes": SOURCE_BYTES},
    "城市對照表.csv": {
        "sha256": "6fb964a02a5700df3e31235b1d9adf72f353a0c4885e52ab200e9bf0cf2bab4a",
        "size_bytes": 45_738,
    },
    "職務對照表.csv": {
        "sha256": "51654e460e17a49bde42a3a4e867a21656158799173a68330b7dcc8295a41619",
        "size_bytes": 436_511,
    },
}
MAX_EXTRACTED_BYTES = SOURCE_BYTES + 256 * 1024 * 1024


def _selected_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    selected: dict[str, zipfile.ZipInfo] = {}
    for member in archive.infolist():
        basename = PurePosixPath(member.filename).name
        if basename not in REQUIRED_FILES:
            continue
        path = PurePosixPath(member.filename)
        mode = member.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if (
            path.is_absolute()
            or ".." in path.parts
            or member.is_dir()
            or (file_type not in {0, stat.S_IFREG})
            or member.flag_bits & 1
        ):
            raise RuntimeError(f"competition ZIP contains an unsafe member: {member.filename}")
        if basename in selected:
            raise RuntimeError(f"competition ZIP must contain {basename} exactly once")
        selected[basename] = member
    missing = sorted(set(REQUIRED_FILES).difference(selected))
    if missing:
        raise RuntimeError(f"competition ZIP is missing required files: {missing}")
    if sum(member.file_size for member in selected.values()) > MAX_EXTRACTED_BYTES:
        raise RuntimeError("competition ZIP required files exceed the extraction limit")
    return selected


def _copy_member(
    source: BinaryIO,
    target: BinaryIO,
    *,
    claimed_size: int,
    extracted_before: int,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(8 * 1024 * 1024):
        next_size = size + len(chunk)
        if extracted_before + next_size > MAX_EXTRACTED_BYTES:
            raise RuntimeError("competition ZIP required files exceed the extraction limit")
        if next_size > claimed_size:
            raise RuntimeError("competition ZIP member expanded beyond its declared size")
        target.write(chunk)
        digest.update(chunk)
        size = next_size
    if size != claimed_size:
        raise RuntimeError("competition ZIP member size changed")
    return digest.hexdigest(), size


def prepare(source: Path, output: Path) -> dict[str, object]:
    if not source.is_file() or not zipfile.is_zipfile(source):
        raise RuntimeError("competition input must be a regular ZIP file")
    if output.exists():
        raise RuntimeError("dataset output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f".{output.name}.{os.getpid()}.partial")
    if partial.exists():
        raise RuntimeError("partial dataset output already exists")
    partial.mkdir()
    try:
        files: dict[str, dict[str, object]] = {}
        extracted = 0
        with zipfile.ZipFile(source) as archive:
            selected = _selected_members(archive)
            for name in REQUIRED_FILES:
                member = selected[name]
                destination = partial / name
                with archive.open(member) as compressed, destination.open("xb") as target:
                    digest, size = _copy_member(
                        compressed,
                        target,
                        claimed_size=member.file_size,
                        extracted_before=extracted,
                    )
                extracted += size
                if {"sha256": digest, "size_bytes": size} != EXPECTED_FILES[name]:
                    raise RuntimeError(
                        f"competition ZIP {name} bytes differ from the fixed snapshot"
                    )
                files[name] = {"sha256": digest, "size_bytes": size}

        validate_source(partial / "職缺.csv")
        _taxonomy_csv(partial / "城市對照表.csv", "location")
        _taxonomy_csv(partial / "職務對照表.csv", "duty")
        manifest: dict[str, object] = {
            "schema_version": 1,
            "complete": True,
            "zip_sha256": sha256_file(source),
            "files": files,
        }
        atomic_json(partial / "manifest.json", manifest)
        partial.replace(output)
        return manifest
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zip", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.zip, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
