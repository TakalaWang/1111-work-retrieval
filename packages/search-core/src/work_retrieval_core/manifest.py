from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

ARTIFACT_KEY = re.compile(r"^(embeddings|models|indexes)/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True, slots=True)
class Artifact:
    kind: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class RuntimeManifest:
    artifacts: tuple[tuple[str, Artifact], ...]

    @classmethod
    def from_path(cls, path: str | Path) -> RuntimeManifest:
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeError("runtime manifest cannot be read as UTF-8 JSON") from error
        return cls.from_dict(value)

    @classmethod
    def from_dict(cls, value: object) -> RuntimeManifest:
        if not isinstance(value, dict) or set(value) != {"schema_version", "artifacts"}:
            raise RuntimeError("runtime manifest must contain only schema_version and artifacts")
        if value["schema_version"] != 1:
            raise RuntimeError("runtime manifest schema_version must equal 1")
        raw_artifacts = value["artifacts"]
        if not isinstance(raw_artifacts, dict) or not raw_artifacts:
            raise RuntimeError("runtime manifest artifacts must be a non-empty object")

        artifacts: list[tuple[str, Artifact]] = []
        for key, value in raw_artifacts.items():
            if (
                not isinstance(key, str)
                or not ARTIFACT_KEY.fullmatch(key)
                or any(part in {".", ".."} for part in key.split("/"))
            ):
                raise RuntimeError("runtime manifest contains an invalid artifact key")
            if not isinstance(value, dict) or set(value) != {"kind", "sha256", "size_bytes"}:
                raise RuntimeError(f"artifact {key} has an invalid shape")
            kind = value["kind"]
            sha256 = value["sha256"]
            size_bytes = value["size_bytes"]
            if kind not in {"embedding", "model", "index"}:
                raise RuntimeError(f"artifact {key} has an invalid kind")
            if not isinstance(sha256, str) or not SHA256.fullmatch(sha256):
                raise RuntimeError(f"artifact {key} has an invalid sha256")
            if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
                raise RuntimeError(f"artifact {key} has an invalid size_bytes")
            artifacts.append((key, Artifact(kind, sha256, size_bytes)))
        return cls(tuple(artifacts))

    def artifact(self, key: str) -> Artifact | None:
        return next((artifact for name, artifact in self.artifacts if name == key), None)
