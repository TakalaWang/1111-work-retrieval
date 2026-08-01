from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import promote_runtime_artifacts as promotion


def test_canonical_manifest_hash_is_stable() -> None:
    first = {"schema_version": 1, "artifacts": {"models/a/file": {"kind": "model"}}}
    second = {"artifacts": {"models/a/file": {"kind": "model"}}, "schema_version": 1}

    assert promotion.canonical_bytes(first) == promotion.canonical_bytes(second)
    assert promotion.canonical_bytes(first) == (
        b'{"artifacts":{"models/a/file":{"kind":"model"}},"schema_version":1}\n'
    )
    assert hashlib.sha256(promotion.canonical_bytes(first)).hexdigest() == (
        "3b56493e6ae3748f1d68fb6b618ffe357ea8859b8efb065f572c7f829d049c71"
    )


@pytest.mark.parametrize(
    "path",
    ["/models/a", "models/../secret", "models//file", "runtime/file", "", "models/./file"],
)
def test_runtime_paths_are_strictly_relative(path: str) -> None:
    with pytest.raises(RuntimeError, match="unsafe runtime artifact path"):
        promotion.validate_relative_path(path)


def test_manifest_selects_only_the_three_approved_artifact_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(promotion, "EXPECTED_COUNTS", {"embedding": 1, "model": 1, "index": 1})
    source = {
        "schema_version": 3,
        "files": [
            {"path": f"{promotion.EMBEDDING_PREFIX}manifest.json", "sha256": "a" * 64, "size": 1},
            {"path": f"{promotion.MODEL_PREFIX}config.json", "sha256": "b" * 64, "size": 2},
            {"path": f"{promotion.INDEX_PREFIX}manifest.json", "sha256": "c" * 64, "size": 3},
            {
                "path": "artifacts/production/query-history/query-history.sqlite3",
                "sha256": "d" * 64,
                "size": 4,
            },
            {
                "path": "artifacts/production/behavior/global-job-ctr.parquet",
                "sha256": "e" * 64,
                "size": 5,
            },
        ],
    }
    selected_for_hash = [
        {
            "source_path": f"{promotion.EMBEDDING_PREFIX}manifest.json",
            "path": "embeddings/qwen3-embedding-8b/manifest.json",
            "kind": "embedding",
            "sha256": "a" * 64,
            "size_bytes": 1,
        },
        {
            "source_path": f"{promotion.INDEX_PREFIX}manifest.json",
            "path": "indexes/tantivy-bm25-clean-v1/manifest.json",
            "kind": "index",
            "sha256": "c" * 64,
            "size_bytes": 3,
        },
        {
            "source_path": f"{promotion.MODEL_PREFIX}config.json",
            "path": "models/qwen3-embedding-8b/config.json",
            "kind": "model",
            "sha256": "b" * 64,
            "size_bytes": 2,
        },
    ]
    selected_for_hash.sort(key=lambda item: item["path"])
    inventory = promotion.json.dumps(
        selected_for_hash, separators=(",", ":"), sort_keys=True
    ).encode()
    monkeypatch.setattr(
        promotion,
        "EXPECTED_SELECTED_INVENTORY_SHA256",
        hashlib.sha256(inventory).hexdigest(),
    )

    manifest, selected = promotion.build_manifest(source)

    assert len(selected) == 3
    assert {item["kind"] for item in selected} == {"embedding", "model", "index"}
    assert all(
        "sqlite" not in str(item).lower() and "behavior" not in str(item).lower()
        for item in selected
    )
    assert all(
        path.split("/", 1)[0] in {"embeddings", "models", "indexes"}
        for path in manifest["artifacts"]
    )


def test_selected_inventory_drift_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(promotion, "EXPECTED_COUNTS", {"embedding": 1, "model": 0, "index": 0})
    monkeypatch.setattr(promotion, "EXPECTED_SELECTED_INVENTORY_SHA256", "0" * 64)
    source = {
        "schema_version": 3,
        "files": [
            {
                "path": f"{promotion.EMBEDDING_PREFIX}manifest.json",
                "sha256": "a" * 64,
                "size": 1,
            }
        ],
    }

    with pytest.raises(RuntimeError, match="approved 401 objects"):
        promotion.build_manifest(source)


def test_destination_prefix_is_content_addressed() -> None:
    digest = "f" * 64
    assert promotion.destination_key(digest, "models/qwen/file") == (
        f"runtime/{digest}/models/qwen/file"
    )


def test_account_and_region_are_fixed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(promotion, "aws", lambda _: {"Account": "wrong"})
    with pytest.raises(RuntimeError, match=promotion.AWS_ACCOUNT):
        promotion.verify_account()

    monkeypatch.setattr(promotion, "aws", lambda _: {"Account": promotion.AWS_ACCOUNT})
    monkeypatch.setattr(
        promotion,
        "_run",
        lambda *args, **kwargs: type("Result", (), {"stdout": "us-east-1\n"})(),
    )
    with pytest.raises(RuntimeError, match=promotion.AWS_REGION):
        promotion.verify_account()


def test_copy_requires_native_checksums_and_atomic_create(monkeypatch: pytest.MonkeyPatch) -> None:
    item = {
        "source_path": "source/file",
        "path": "models/qwen/file",
        "kind": "model",
        "sha256": "a" * 64,
        "size_bytes": 7,
    }
    checksum = promotion.base64.b64encode(bytes.fromhex("a" * 64)).decode()
    heads = iter(
        [
            {"ContentLength": 7, "ChecksumSHA256": checksum, "ETag": '"etag"'},
            promotion.AwsError("missing", "Not Found"),
            {"ContentLength": 7, "ChecksumSHA256": checksum},
        ]
    )
    calls: list[list[str]] = []

    def head(*_: object) -> dict[str, object]:
        value = next(heads)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(promotion, "_head", head)
    monkeypatch.setattr(promotion, "aws", lambda arguments: calls.append(arguments) or {})

    promotion.copy_artifacts([item], "f" * 64)

    copy = calls[0]
    assert copy[:2] == ["s3api", "copy-object"]
    assert copy[copy.index("--copy-source-if-match") + 1] == '"etag"'
    assert copy[copy.index("--if-none-match") + 1] == "*"
    assert copy[copy.index("--checksum-algorithm") + 1] == "SHA256"


def test_copy_rejects_source_checksum_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    item = {
        "source_path": "source/file",
        "path": "models/qwen/file",
        "kind": "model",
        "sha256": "a" * 64,
        "size_bytes": 7,
    }
    monkeypatch.setattr(
        promotion,
        "_head",
        lambda *_: {"ContentLength": 7, "ChecksumSHA256": "wrong", "ETag": '"etag"'},
    )

    with pytest.raises(RuntimeError, match="source object checksum drifted"):
        promotion.copy_artifacts([item], "f" * 64)


def test_audit_rejects_extra_destination_key(monkeypatch: pytest.MonkeyPatch) -> None:
    item = {
        "source_path": "source/file",
        "path": "models/qwen/file",
        "kind": "model",
        "sha256": "a" * 64,
        "size_bytes": 7,
    }
    prefix = f"runtime/{'f' * 64}/"
    monkeypatch.setattr(
        promotion,
        "aws",
        lambda _: {
            "IsTruncated": False,
            "Contents": [
                {"Key": f"{prefix}models/qwen/file", "Size": 7},
                {"Key": f"{prefix}manifest.json", "Size": 10},
                {"Key": f"{prefix}extra", "Size": 1},
            ],
        },
    )

    with pytest.raises(RuntimeError, match="key or size inventory differs"):
        promotion.audit_destination([item], "f" * 64, 10)


def test_aws_cli_retries_only_transient_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def run(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls < 3:
            return subprocess.CompletedProcess([], 1, "", "Connection was closed")
        return subprocess.CompletedProcess([], 0, "{}", "")

    monkeypatch.setattr(promotion.subprocess, "run", run)
    monkeypatch.setattr(promotion, "sleep", lambda _: None)

    assert promotion.aws(["sts", "get-caller-identity"]) == {}
    assert calls == 3
