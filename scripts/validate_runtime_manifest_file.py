#!/usr/bin/env python3
"""Validate a downloaded immutable runtime manifest before deployment."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import cast

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA = Path(__file__).parents[1] / "packages" / "contract" / "runtime-manifest.schema.json"


def _reject_nonfinite_json(value: str) -> object:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def validate(path: Path, expected_sha256: str) -> None:
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise RuntimeError("runtime manifest body SHA-256 differs from the deployment input")
    value = json.loads(payload, parse_constant=_reject_nonfinite_json)
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"), parse_constant=_reject_nonfinite_json)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)
    if not isinstance(value, dict):
        raise RuntimeError("runtime manifest must be a JSON object")
    release = value.get("release")
    if not isinstance(release, dict):
        raise RuntimeError("runtime manifest release gate is missing")
    typed_release = cast(dict[str, object], release)
    if (
        typed_release.get("complete") is not True
        or typed_release.get("publication_allowed") is not True
    ):
        raise RuntimeError("runtime manifest is incomplete or not publishable")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    validate(args.manifest, args.expected_sha256)


if __name__ == "__main__":
    main()
