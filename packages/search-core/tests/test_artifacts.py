from __future__ import annotations

import hashlib
from collections import namedtuple
from pathlib import Path

import pytest
from work_retrieval_core.artifacts import S3RuntimeArtifacts
from work_retrieval_core.manifest import Artifact

DiskUsage = namedtuple("DiskUsage", "total used free")


class FakeS3:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        assert bucket == "runtime-bucket"
        Path(filename).write_bytes(self.objects[key])


class FakeManifest:
    def __init__(self, artifacts: tuple[tuple[str, Artifact], ...]) -> None:
        self.artifacts = artifacts

    def required_artifacts(
        self, *, include_dense: bool, include_multiview: bool, include_graph: bool
    ) -> tuple[tuple[str, Artifact], ...]:
        assert include_dense and not include_multiview and include_graph
        return self.artifacts


def _runtime(tmp_path: Path, objects: dict[str, bytes], manifest_sha: str) -> S3RuntimeArtifacts:
    return S3RuntimeArtifacts(
        bucket="runtime-bucket",
        manifest_sha256=manifest_sha,
        runtime_root=tmp_path,
        s3=FakeS3(objects),
        disk_usage=lambda path: DiskUsage(10**12, 0, 10**12),
        memory_bytes=lambda: 16 * 1024**3,
    )


def test_s3_bootstrap_verifies_root_and_each_required_object(tmp_path: Path) -> None:
    root = b'{"schema_version":2}'
    payload = b"verified vectors"
    root_sha = hashlib.sha256(root).hexdigest()
    payload_sha = hashlib.sha256(payload).hexdigest()
    runtime = _runtime(
        tmp_path,
        {
            f"runtime/{root_sha}/manifest.json": root,
            f"runtime/{root_sha}/embeddings/whole/vectors.npy": payload,
        },
        root_sha,
    )

    runtime.materialize_manifest(tmp_path / "manifest.json")
    runtime.materialize_required(
        FakeManifest(
            (("embeddings/whole/vectors.npy", Artifact("embedding", payload_sha, len(payload))),)
        ),
        include_dense=True,
        include_multiview=False,
        include_graph=True,
    )

    assert (tmp_path / "manifest.json").read_bytes() == root
    assert (tmp_path / "embeddings/whole/vectors.npy").read_bytes() == payload


def test_s3_mismatch_and_partial_startup_fail_closed(tmp_path: Path) -> None:
    expected_sha = hashlib.sha256(b"expected").hexdigest()
    runtime = _runtime(
        tmp_path,
        {f"runtime/{expected_sha}/manifest.json": b"corrupt"},
        expected_sha,
    )
    with pytest.raises(RuntimeError, match="SHA-256"):
        runtime.materialize_manifest(tmp_path / "manifest.json")
    assert not (tmp_path / ".manifest.json.partial").exists()

    partial = tmp_path / ".manifest.json.partial"
    partial.write_bytes(b"interrupted")
    with pytest.raises(RuntimeError, match="partial runtime artifact"):
        runtime.materialize_manifest(tmp_path / "manifest.json")


def test_existing_artifact_mismatch_is_not_overwritten(tmp_path: Path) -> None:
    root_sha = hashlib.sha256(b"expected").hexdigest()
    target = tmp_path / "manifest.json"
    target.write_bytes(b"local-corruption")
    runtime = _runtime(
        tmp_path,
        {f"runtime/{root_sha}/manifest.json": b"expected"},
        root_sha,
    )

    with pytest.raises(RuntimeError, match="SHA-256"):
        runtime.materialize_manifest(target)

    assert target.read_bytes() == b"local-corruption"
