#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import tempfile
from pathlib import PurePosixPath
from time import sleep
from typing import Any, cast

AWS_ACCOUNT = "378849533305"
AWS_PROFILE = "competition"
AWS_REGION = "us-west-2"
SOURCE_BUCKET = "jobbank-data-bucket"
DESTINATION_BUCKET = "workretrievaldata-runtimebucket404c5ee4-hkvrjx5fbkij"
SOURCE_MANIFEST_KEY = (
    "one111-search/runtime/"
    "f762cc4d676e16aa04789e1573713ef30d66e72f3a7f96c5bcd7e7e6133a2adb/manifest.json"
)
SOURCE_MANIFEST_SHA256 = "f762cc4d676e16aa04789e1573713ef30d66e72f3a7f96c5bcd7e7e6133a2adb"
SOURCE_ROOT = SOURCE_MANIFEST_KEY.removesuffix("manifest.json")

EMBEDDING_PREFIX = "artifacts/experiments/qwen3-8b/full/"
MODEL_PREFIX = (
    "cache/huggingface/models--Qwen--Qwen3-Embedding-8B/snapshots/"
    "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af/"
)
INDEX_PREFIX = "artifacts/experiments/tantivy-bm25-clean-v1/"
EXPECTED_COUNTS = {"embedding": 367, "model": 17, "index": 17}
EXPECTED_SELECTED_INVENTORY_SHA256 = (
    "00b999c893b70c9558156095f71667649d070450510da2342a787c8b363b7d3d"
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


def validate_relative_path(path: str) -> None:
    candidate = PurePosixPath(path)
    raw_parts = path.split("/")
    if (
        not path
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in raw_parts)
        or candidate.parts[0] not in {"embeddings", "models", "indexes"}
    ):
        raise RuntimeError(f"unsafe runtime artifact path: {path!r}")


def canonical_bytes(manifest: dict[str, object]) -> bytes:
    return (
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()


def destination_key(manifest_sha: str, path: str) -> str:
    if len(manifest_sha) != 64 or any(c not in "0123456789abcdef" for c in manifest_sha):
        raise RuntimeError("manifest SHA-256 must be lowercase hexadecimal")
    validate_relative_path(path)
    return f"runtime/{manifest_sha}/{path}"


def _mapping(source_path: str) -> tuple[str, str] | None:
    if source_path.startswith(EMBEDDING_PREFIX):
        suffix = source_path.removeprefix(EMBEDDING_PREFIX)
        allowed = (
            suffix == "manifest.json"
            or (suffix.startswith("embeddings-") and suffix.endswith(".f16.npy"))
            or (suffix.startswith("job-ids-") and suffix.endswith(".json"))
            or (suffix.startswith("parallel/shard-") and suffix.endswith(".json"))
        )
        return ("embedding", f"embeddings/qwen3-embedding-8b/{suffix}") if allowed else None
    if source_path.startswith(MODEL_PREFIX):
        return "model", f"models/qwen3-embedding-8b/{source_path.removeprefix(MODEL_PREFIX)}"
    if source_path.startswith(INDEX_PREFIX):
        return "index", f"indexes/tantivy-bm25-clean-v1/{source_path.removeprefix(INDEX_PREFIX)}"
    return None


def build_manifest(source: dict[str, object]) -> tuple[dict[str, object], list[dict[str, object]]]:
    files = source.get("files")
    if source.get("schema_version") != 3 or not isinstance(files, list):
        raise RuntimeError("source manifest does not satisfy the verified v3 contract")
    selected: list[dict[str, object]] = []
    counts = dict.fromkeys(EXPECTED_COUNTS, 0)
    for raw in files:
        if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
            raise RuntimeError("source manifest contains an invalid file entry")
        mapping = _mapping(raw["path"])
        if mapping is None:
            continue
        kind, path = mapping
        sha256 = raw.get("sha256")
        size = raw.get("size")
        validate_relative_path(path)
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
            or not isinstance(size, int)
            or size < 0
        ):
            raise RuntimeError(f"invalid source inventory entry: {raw['path']}")
        selected.append(
            {
                "source_path": raw["path"],
                "path": path,
                "kind": kind,
                "sha256": sha256,
                "size_bytes": size,
            }
        )
        counts[kind] += 1
    if counts != EXPECTED_COUNTS:
        raise RuntimeError(f"source inventory drifted: expected {EXPECTED_COUNTS}, got {counts}")
    selected.sort(key=lambda item: cast(str, item["path"]))
    inventory_payload = json.dumps(selected, separators=(",", ":"), sort_keys=True).encode()
    if hashlib.sha256(inventory_payload).hexdigest() != EXPECTED_SELECTED_INVENTORY_SHA256:
        raise RuntimeError("selected artifact inventory does not match the approved 401 objects")
    if len({item["path"] for item in selected}) != len(selected):
        raise RuntimeError("selected artifact inventory contains duplicate destination paths")
    artifacts = {
        cast(str, item["path"]): {
            "kind": item["kind"],
            "sha256": item["sha256"],
            "size_bytes": item["size_bytes"],
        }
        for item in selected
    }
    return {"schema_version": 1, "artifacts": artifacts}, selected


def load_source_manifest() -> dict[str, object]:
    result = _run(["s3", "cp", f"s3://{SOURCE_BUCKET}/{SOURCE_MANIFEST_KEY}", "-"], text=False)
    payload = bytes(result.stdout)
    if hashlib.sha256(payload).hexdigest() != SOURCE_MANIFEST_SHA256:
        raise RuntimeError("source manifest SHA-256 does not match the approved bundle")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise RuntimeError("source manifest is not a JSON object")
    return cast(dict[str, object], value)


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


def _verify_destination(head: dict[str, object], item: dict[str, object]) -> None:
    checksum = base64.b64encode(bytes.fromhex(cast(str, item["sha256"]))).decode()
    if head.get("ContentLength") != item["size_bytes"] or head.get("ChecksumSHA256") != checksum:
        raise RuntimeError(f"destination object differs: {item['path']}")


def copy_artifacts(items: list[dict[str, object]], manifest_sha: str) -> None:
    for item in items:
        source_key = f"{SOURCE_ROOT}{item['source_path']}"
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


def audit_destination(
    items: list[dict[str, object]], manifest_sha: str, manifest_bytes: int
) -> None:
    prefix = f"runtime/{manifest_sha}/"
    listed = aws(
        [
            "s3api",
            "list-objects-v2",
            "--bucket",
            DESTINATION_BUCKET,
            "--prefix",
            prefix,
            "--expected-bucket-owner",
            AWS_ACCOUNT,
            "--no-paginate",
        ]
    )
    contents = listed.get("Contents")
    if not isinstance(contents, list) or listed.get("IsTruncated") is not False:
        raise RuntimeError("destination prefix listing was missing or truncated")
    expected = {
        destination_key(manifest_sha, cast(str, item["path"])): cast(int, item["size_bytes"])
        for item in items
    }
    expected[f"{prefix}manifest.json"] = manifest_bytes
    actual = {
        item["Key"]: item["Size"]
        for item in contents
        if isinstance(item, dict) and isinstance(item.get("Key"), str)
    }
    if actual != expected:
        raise RuntimeError("destination prefix key or size inventory differs")
    for item in items:
        _verify_destination(
            _head(DESTINATION_BUCKET, destination_key(manifest_sha, cast(str, item["path"]))),
            item,
        )


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
    ):
        raise RuntimeError("destination manifest differs")


def main() -> None:
    parser = argparse.ArgumentParser(description="Promote approved search artifacts to formal S3")
    parser.add_argument("--execute", action="store_true", help="perform the server-side copies")
    args = parser.parse_args()
    verify_account()
    manifest, items = build_manifest(load_source_manifest())
    payload = canonical_bytes(manifest)
    manifest_sha = hashlib.sha256(payload).hexdigest()
    if args.execute:
        copy_artifacts(items, manifest_sha)
        put_manifest(payload, manifest_sha)
        audit_destination(items, manifest_sha, len(payload))
    print(
        json.dumps(
            {
                "executed": args.execute,
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
