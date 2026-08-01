from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol


class StreamingBody(Protocol):
    def read(self, amount: int = -1) -> bytes: ...


class S3Reader(Protocol):
    def get_object(self, **kwargs: object) -> Mapping[str, object]: ...


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.{os.getpid()}.partial")
    if partial.exists():
        raise RuntimeError(f"partial output already exists: {partial}")
    try:
        with partial.open("wb") as output:
            output.write(canonical_json(value) + b"\n")
            output.flush()
            os.fsync(output.fileno())
        partial.replace(path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def read_json_object(path: Path, name: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{name} cannot be read as UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{name} must be a JSON object")
    return value


def exact_keys(value: Mapping[str, object], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise RuntimeError(f"{name} has missing or unknown keys")


def require_sha256(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RuntimeError(f"{name} must be a lowercase SHA-256")
    return value


def artifact_entry(path: Path, *, relative_to: Path, kind: str) -> dict[str, object]:
    relative = path.resolve().relative_to(relative_to.resolve()).as_posix()
    if any(part in {"", ".", ".."} for part in Path(relative).parts):
        raise RuntimeError("artifact path is unsafe")
    return {
        "path": relative,
        "kind": kind,
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def verify_local_inventory(root: Path, artifacts: object) -> tuple[dict[str, object], ...]:
    if not isinstance(artifacts, list) or not artifacts:
        raise RuntimeError("artifact inventory must be a non-empty array")
    parsed: list[dict[str, object]] = []
    seen: set[str] = set()
    for position, raw in enumerate(artifacts):
        if not isinstance(raw, dict):
            raise RuntimeError(f"artifact {position} must be an object")
        exact_keys(raw, {"path", "kind", "sha256", "size_bytes"}, f"artifact {position}")
        path = raw["path"]
        if not isinstance(path, str) or Path(path).is_absolute():
            raise RuntimeError(f"artifact {position} path is invalid")
        target = (root / path).resolve()
        if not target.is_relative_to(root.resolve()) or path in seen:
            raise RuntimeError("artifact inventory has an unsafe or duplicate path")
        seen.add(path)
        sha256 = require_sha256(raw["sha256"], f"artifact {position} sha256")
        size = raw["size_bytes"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise RuntimeError(f"artifact {position} size is invalid")
        if not target.is_file() or target.stat().st_size != size or sha256_file(target) != sha256:
            raise RuntimeError(f"artifact {position} bytes differ from inventory")
        parsed.append(dict(raw))
    return tuple(parsed)


def verify_s3_inventory(
    *,
    bucket: str,
    prefix: str,
    expected_owner: str,
    artifacts: Iterable[Mapping[str, object]],
    s3: S3Reader,
) -> None:
    if not bucket or not prefix or not expected_owner.isdecimal() or len(expected_owner) != 12:
        raise ValueError("bucket, prefix, and 12-digit expected owner are required")
    clean_prefix = prefix.strip("/")
    if not clean_prefix:
        raise ValueError("S3 prefix must be non-empty")
    for artifact in artifacts:
        path = artifact["path"]
        expected_sha = artifact["sha256"]
        expected_size = artifact["size_bytes"]
        if not isinstance(path, str) or not isinstance(expected_sha, str):
            raise RuntimeError("S3 artifact inventory is malformed")
        response = s3.get_object(
            Bucket=bucket,
            Key=f"{clean_prefix}/{path}",
            ExpectedBucketOwner=expected_owner,
        )
        if response.get("ContentLength") != expected_size:
            raise RuntimeError(f"S3 artifact size differs: {path}")
        body = response.get("Body")
        if body is None or not hasattr(body, "read"):
            raise RuntimeError(f"S3 artifact body is missing: {path}")
        digest = hashlib.sha256()
        while chunk := body.read(8 * 1024 * 1024):  # type: ignore[union-attr]
            digest.update(chunk)
        if digest.hexdigest() != expected_sha:
            raise RuntimeError(f"S3 artifact SHA-256 differs: {path}")


def verify_s3_object(
    *,
    bucket: str,
    key: str,
    expected_owner: str,
    expected_sha256: str,
    expected_size: int,
    s3: S3Reader,
) -> None:
    require_sha256(expected_sha256, "S3 object SHA-256")
    if (
        not bucket
        or not key
        or not expected_owner.isdecimal()
        or len(expected_owner) != 12
        or expected_size < 0
    ):
        raise ValueError("complete S3 object identity is required")
    response = s3.get_object(
        Bucket=bucket,
        Key=key,
        ExpectedBucketOwner=expected_owner,
    )
    if response.get("ContentLength") != expected_size:
        raise RuntimeError(f"S3 artifact size differs: {key}")
    body = response.get("Body")
    if body is None or not hasattr(body, "read"):
        raise RuntimeError(f"S3 artifact body is missing: {key}")
    digest = hashlib.sha256()
    while chunk := body.read(8 * 1024 * 1024):  # type: ignore[union-attr]
        digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise RuntimeError(f"S3 artifact SHA-256 differs: {key}")
