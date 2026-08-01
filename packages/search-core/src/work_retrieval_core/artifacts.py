from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

import boto3  # type: ignore[import-untyped]

from work_retrieval_core.manifest import Artifact

MIN_RUNTIME_MEMORY_BYTES = 12 * 1024**3


class S3Downloader(Protocol):
    def download_file(self, bucket: str, key: str, filename: str) -> None: ...


class RuntimeArtifactManifest(Protocol):
    def required_artifacts(
        self, *, include_multiview: bool
    ) -> tuple[tuple[str, Artifact], ...]: ...


class DiskUsage(Protocol):
    @property
    def free(self) -> int: ...


def aws_s3_client(*, region_name: str) -> S3Downloader:
    return cast(S3Downloader, boto3.client("s3", region_name=region_name))


class S3RuntimeArtifacts:
    """Materialize one immutable runtime release and verify every byte before use."""

    def __init__(
        self,
        *,
        bucket: str,
        manifest_sha256: str,
        runtime_root: Path,
        s3: S3Downloader,
        disk_usage: Callable[[Path], DiskUsage] = shutil.disk_usage,
        memory_bytes: Callable[[], int] | None = None,
    ) -> None:
        if not bucket.strip():
            raise ValueError("artifact bucket must be non-empty")
        if len(manifest_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in manifest_sha256
        ):
            raise ValueError("manifest_sha256 must be a lowercase SHA-256")
        self._bucket = bucket
        self._release_prefix = f"runtime/{manifest_sha256}"
        self._manifest_sha256 = manifest_sha256
        self._runtime_root = runtime_root.resolve()
        self._s3 = s3
        self._disk_usage = disk_usage
        self._memory_bytes = memory_bytes or available_memory_bytes

    def materialize_manifest(self, manifest_path: Path) -> Path:
        target = manifest_path.resolve()
        self._assert_inside_runtime_root(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._materialize_one(
            key=f"{self._release_prefix}/manifest.json",
            target=target,
            sha256=self._manifest_sha256,
            size_bytes=None,
        )
        return target

    def materialize_required(
        self,
        manifest: RuntimeArtifactManifest,
        *,
        include_multiview: bool,
    ) -> tuple[Path, ...]:
        required = manifest.required_artifacts(include_multiview=include_multiview)
        missing = [
            (path, artifact)
            for path, artifact in required
            if not (self._runtime_root / path).exists()
        ]
        bytes_to_download = sum(artifact.size_bytes for _, artifact in missing)
        largest_partial = max((artifact.size_bytes for _, artifact in missing), default=0)
        if self._disk_usage(self._runtime_root).free < bytes_to_download + largest_partial:
            raise RuntimeError("runtime volume cannot safely materialize immutable artifacts")
        if self._memory_bytes() < MIN_RUNTIME_MEMORY_BYTES:
            raise RuntimeError("runtime memory is below the verified serving minimum")

        materialized: list[Path] = []
        for path, artifact in required:
            target = (self._runtime_root / path).resolve()
            self._assert_inside_runtime_root(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            self._materialize_one(
                key=f"{self._release_prefix}/{path}",
                target=target,
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
            )
            materialized.append(target)
        return tuple(materialized)

    def _materialize_one(
        self,
        *,
        key: str,
        target: Path,
        sha256: str,
        size_bytes: int | None,
    ) -> None:
        if target.exists():
            _verify_file(target, sha256=sha256, size_bytes=size_bytes)
            return
        partial = target.with_name(f".{target.name}.partial")
        if partial.exists():
            raise RuntimeError("partial runtime artifact exists from an incomplete startup")
        try:
            self._s3.download_file(self._bucket, key, os.fspath(partial))
            _verify_file(partial, sha256=sha256, size_bytes=size_bytes)
            partial.replace(target)
        except Exception as error:
            partial.unlink(missing_ok=True)
            if isinstance(error, RuntimeError):
                raise
            raise RuntimeError("immutable runtime artifact download failed") from error

    def _assert_inside_runtime_root(self, path: Path) -> None:
        if not path.is_relative_to(self._runtime_root):
            raise RuntimeError("runtime artifact path escapes SEARCH_RUNTIME_ROOT")


def available_memory_bytes() -> int:
    cgroup_limit = Path("/sys/fs/cgroup/memory.max")
    try:
        raw = cgroup_limit.read_text(encoding="ascii").strip()
    except OSError:
        raw = "max"
    if raw != "max":
        try:
            return int(raw)
        except ValueError as error:
            raise RuntimeError("container memory limit is malformed") from error
    page_size = os.sysconf("SC_PAGE_SIZE")
    pages = os.sysconf("SC_PHYS_PAGES")
    return page_size * pages


def _verify_file(path: Path, *, sha256: str, size_bytes: int | None) -> None:
    stat = path.stat()
    if size_bytes is not None and stat.st_size != size_bytes:
        raise RuntimeError("runtime artifact size differs from immutable manifest")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != sha256:
        raise RuntimeError("runtime artifact SHA-256 differs from immutable manifest")
