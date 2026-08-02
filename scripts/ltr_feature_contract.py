#!/usr/bin/env python3
"""Build the frozen IPS LambdaRank feature arrays without training or serving a model."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import numpy as np
import numpy.typing as npt

RRF_K = 60
MINIMUM_PROPENSITY = 0.1
UNJUDGED_WEIGHT = 0.25
FEATURE_NAMES = (
    "lexical_best_rr",
    "dense_best_rr",
    "graph_best_rr",
    "source_count",
    "source_family_count",
    "concept_coverage",
    "structured_intents",
    "whole_literal",
    "title_literal",
    "graph_path_max",
    "freshness",
    "future_snapshot",
)
FEATURE_SCHEMA_SHA256 = hashlib.sha256("\n".join(FEATURE_NAMES).encode()).hexdigest()
ROW_KEYS = {
    "context_id",
    "job_index",
    "label",
    "exposure_rank",
    "lane_ranks",
    "concept_count",
    "concept_ranks",
    "structured_intents",
    "graph_path_scores",
    "freshness",
    "future_snapshot",
}
LINEAGE_KEYS = {
    "schema_version",
    "train_cutoff_exclusive",
    "split_sha256",
    "job_row_order_sha256",
    "candidate_sha256",
    "source_target_sha256",
    "uses_test_jd",
    "uses_query_history_replay",
}
SHA256 = re.compile(r"^[a-f0-9]{64}$")
RESEARCH_REPORT_SHA256 = "12760fefcb6d66f1ebd11040c511697237b30a05236bbae2073d4435d5357a72"
RESEARCH_NO_GRAPH_MANIFEST_SHA256 = (
    "4b14296945463dee0ae4367efab38d8bda026af5f5fa68a0025c80d6141f7bc0"
)
RESEARCH_WITH_GRAPH_MANIFEST_SHA256 = (
    "c7b2a99ff610b69855f81675e0e6c5735d1cb87afd6e27f8ad74e76df8705768"
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _number(value: object, name: str, *, minimum: float, maximum: float | None = None) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < minimum or (maximum is not None and result > maximum):
        raise ValueError(f"{name} is outside its finite range")
    return result


def _rank_mapping(value: object, name: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    output: dict[str, int] = {}
    for raw_key, raw_rank in value.items():
        if not isinstance(raw_key, str) or not raw_key.strip() or raw_key != raw_key.strip():
            raise ValueError(f"{name} has an invalid key")
        output[raw_key] = _integer(raw_rank, f"{name}.{raw_key}", minimum=1)
    return output


def _score_mapping(value: object, name: str) -> dict[str, float]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    output: dict[str, float] = {}
    for raw_key, raw_score in value.items():
        if not isinstance(raw_key, str) or not raw_key.strip() or raw_key != raw_key.strip():
            raise ValueError(f"{name} has an invalid key")
        output[raw_key] = _number(raw_score, f"{name}.{raw_key}", minimum=0.0)
    return output


def _best_rr(lane_ranks: Mapping[str, int], prefix: str) -> float:
    ranks = [rank for lane, rank in lane_ranks.items() if lane.startswith(prefix)]
    return 0.0 if not ranks else 1.0 / (RRF_K + min(ranks))


def _feature_row(row: Mapping[str, object]) -> npt.NDArray[np.float32]:
    lane_ranks = _rank_mapping(row["lane_ranks"], "lane_ranks")
    concept_ranks = _rank_mapping(row["concept_ranks"], "concept_ranks")
    graph_path_scores = _score_mapping(row["graph_path_scores"], "graph_path_scores")
    concept_count = _integer(row["concept_count"], "concept_count")
    if len(concept_ranks) > concept_count:
        raise ValueError("concept_ranks cannot exceed concept_count")
    source_families = {lane.split("_", 1)[0] for lane in lane_ranks}
    return np.asarray(
        (
            _best_rr(lane_ranks, "lexical"),
            _best_rr(lane_ranks, "dense"),
            _best_rr(lane_ranks, "graph"),
            len(lane_ranks),
            len(source_families),
            len(concept_ranks) / concept_count if concept_count else 0.0,
            _integer(row["structured_intents"], "structured_intents"),
            float("whole_literal" in lane_ranks),
            float("title_literal" in lane_ranks),
            max(graph_path_scores.values(), default=0.0),
            _number(row["freshness"], "freshness", minimum=0.0, maximum=1.0),
            float(cast(bool, row["future_snapshot"])),
        ),
        dtype=np.float32,
    )


def clipped_ips_weights(
    exposure_positions: npt.NDArray[np.int32],
    labels: npt.NDArray[np.int32],
) -> npt.NDArray[np.float32]:
    positions = np.asarray(exposure_positions)
    values = np.asarray(labels)
    if (
        positions.ndim != 1
        or values.shape != positions.shape
        or positions.dtype != np.int32
        or values.dtype != np.int32
        or np.any(positions < 1)
        or np.any((values < 0) | (values > 2))
    ):
        raise ValueError("invalid IPS inputs")
    propensity = np.maximum(
        1.0 / np.log2(positions.astype(np.float64) + 1.0),
        MINIMUM_PROPENSITY,
    )
    weights = 1.0 / propensity
    weights[values == 0] *= UNJUDGED_WEIGHT
    return np.asarray(weights, dtype=np.float32)


def _load_rows(path: Path, expected_sha256: str) -> list[dict[str, object]]:
    if not SHA256.fullmatch(expected_sha256) or sha256_file(path) != expected_sha256:
        raise RuntimeError("training evidence differs from its expected SHA-256")
    rows: list[dict[str, object]] = []
    seen: set[tuple[int, int]] = set()
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"training evidence line {line_number} is invalid JSON") from error
            if not isinstance(raw, dict) or set(raw) != ROW_KEYS:
                raise ValueError(f"training evidence line {line_number} violates the row schema")
            context_id = _integer(raw["context_id"], "context_id")
            job_index = _integer(raw["job_index"], "job_index")
            pair = (context_id, job_index)
            if pair in seen:
                raise ValueError(f"training evidence line {line_number} duplicates a pair")
            seen.add(pair)
            _integer(raw["label"], "label")
            if cast(int, raw["label"]) > 2:
                raise ValueError("label must be 0, 1 or 2")
            _integer(raw["exposure_rank"], "exposure_rank", minimum=1)
            if not isinstance(raw["future_snapshot"], bool):
                raise ValueError("future_snapshot must be boolean")
            _feature_row(raw)
            rows.append(cast(dict[str, object], raw))
    if not rows:
        raise ValueError("training evidence is empty")
    return sorted(rows, key=lambda row: (cast(int, row["context_id"]), cast(int, row["job_index"])))


def _load_lineage(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("LTR lineage cannot be read") from error
    if not isinstance(raw, dict) or set(raw) != LINEAGE_KEYS or raw["schema_version"] != 1:
        raise ValueError("LTR lineage violates its exact schema")
    if raw["uses_test_jd"] is not False or raw["uses_query_history_replay"] is not False:
        raise ValueError("LTR lineage must exclude test JD and query-history answer replay")
    for name in ("split_sha256", "job_row_order_sha256"):
        if not isinstance(raw[name], str) or not SHA256.fullmatch(cast(str, raw[name])):
            raise ValueError(f"LTR lineage {name} must be SHA-256")
    for name in ("candidate_sha256", "source_target_sha256"):
        values = raw[name]
        if not isinstance(values, dict) or not values:
            raise ValueError(f"LTR lineage {name} must be a non-empty object")
        if any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            or not SHA256.fullmatch(value)
            for key, value in values.items()
        ):
            raise ValueError(f"LTR lineage {name} contains an invalid entry")
    cutoff = raw["train_cutoff_exclusive"]
    if not isinstance(cutoff, str) or not cutoff:
        raise ValueError("LTR lineage train_cutoff_exclusive must be non-empty")
    return cast(dict[str, object], raw)


def _arrays(rows: Sequence[Mapping[str, object]]) -> tuple[dict[str, np.ndarray], dict[str, int]]:
    by_context: defaultdict[int, list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        by_context[cast(int, row["context_id"])].append(row)
    retained: list[Mapping[str, object]] = []
    groups: list[int] = []
    dropped_small = 0
    dropped_without_positive = 0
    for context_id in sorted(by_context):
        group = by_context[context_id]
        if len(group) < 2:
            dropped_small += 1
            continue
        if not any(cast(int, row["label"]) > 0 for row in group):
            dropped_without_positive += 1
            continue
        retained.extend(group)
        groups.append(len(group))
    if not groups:
        raise RuntimeError("no trainable LambdaRank groups")
    labels = np.asarray([row["label"] for row in retained], dtype=np.int32)
    positions = np.asarray([row["exposure_rank"] for row in retained], dtype=np.int32)
    arrays = {
        "features": np.stack([_feature_row(row) for row in retained]),
        "labels": labels,
        "groups": np.asarray(groups, dtype=np.int32),
        "weights": clipped_ips_weights(positions, labels),
        "context_ids": np.asarray([row["context_id"] for row in retained], dtype=np.int64),
        "job_indices": np.asarray([row["job_index"] for row in retained], dtype=np.int64),
        "exposure_positions": positions,
    }
    audit = {
        "contexts_considered": len(by_context),
        "groups": len(groups),
        "pairs": len(retained),
        "dropped_contexts_with_fewer_than_two_exposed_candidates": dropped_small,
        "dropped_contexts_without_positive_candidate": dropped_without_positive,
    }
    return arrays, audit


def build(input_path: Path, expected_input_sha256: str, lineage_path: Path, output: Path) -> None:
    if output.exists():
        raise RuntimeError("LTR feature output already exists")
    lineage = _load_lineage(lineage_path)
    rows = _load_rows(input_path, expected_input_sha256)
    arrays, audit = _arrays(rows)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        artifacts: dict[str, dict[str, object]] = {}
        for name, values in arrays.items():
            path = temporary / f"{name}.npy"
            with path.open("wb") as target:
                np.save(target, values, allow_pickle=False)
            artifacts[path.name] = {
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
                "shape": list(values.shape),
                "dtype": str(values.dtype),
            }
        manifest = {
            "schema_version": 1,
            "complete": True,
            "builder": "ltr_feature_contract.py",
            "objective": "lambdarank",
            "label_gain": [0, 1, 3],
            "feature_names": list(FEATURE_NAMES),
            "feature_schema_sha256": FEATURE_SCHEMA_SHA256,
            "ips": {
                "propensity": "max(1/log2(exposure_rank+1), 0.1)",
                "minimum_propensity": MINIMUM_PROPENSITY,
                "label_0_multiplier": UNJUDGED_WEIGHT,
                "warning": "heuristic examination prior; not randomized or doubly robust",
            },
            "input_sha256": expected_input_sha256,
            "lineage_sha256": sha256_file(lineage_path),
            "lineage": lineage,
            "audit": audit,
            "artifacts": artifacts,
            "runtime_activation": False,
            "promotion_allowed": False,
            "research_reference": {
                "report_sha256": RESEARCH_REPORT_SHA256,
                "no_graph_manifest_sha256": RESEARCH_NO_GRAPH_MANIFEST_SHA256,
                "with_graph_manifest_sha256": RESEARCH_WITH_GRAPH_MANIFEST_SHA256,
                "result": "failed promotion: 27 groups and 338 pairs; ranking metrics regressed",
            },
        }
        (temporary / "manifest.json").write_bytes(_canonical_json(manifest))
        temporary.replace(output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def self_check() -> None:
    row: dict[str, object] = {
        "context_id": 1,
        "job_index": 7,
        "label": 1,
        "exposure_rank": 3,
        "lane_ranks": {"lexical_whole": 1, "dense_whole": 3},
        "concept_count": 2,
        "concept_ranks": {"python": 4},
        "structured_intents": 1,
        "graph_path_scores": {"python": 0.8},
        "freshness": 0.5,
        "future_snapshot": True,
    }
    features = _feature_row(row)
    assert features.shape == (len(FEATURE_NAMES),) and features.dtype == np.float32
    assert np.isclose(features[0], 1.0 / 61.0) and np.isclose(features[1], 1.0 / 63.0)
    assert np.allclose(
        features[2:],
        np.asarray([0.0, 2.0, 2.0, 0.5, 1.0, 0.0, 0.0, 0.8, 0.5, 1.0]),
    )
    weights = clipped_ips_weights(
        np.asarray([1, 3, 1024], dtype=np.int32),
        np.asarray([0, 1, 2], dtype=np.int32),
    )
    assert np.allclose(weights, np.asarray([0.25, 2.0, 10.0], dtype=np.float32))
    with tempfile.TemporaryDirectory(prefix="ltr-feature-self-check-") as temporary_name:
        temporary = Path(temporary_name)
        input_path = temporary / "evidence.jsonl"
        second = {**row, "job_index": 8, "label": 0, "exposure_rank": 1}
        input_path.write_text(
            "".join(
                json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
                for value in (row, second)
            ),
            encoding="utf-8",
        )
        lineage_path = temporary / "lineage.json"
        lineage_path.write_bytes(
            _canonical_json(
                {
                    "schema_version": 1,
                    "train_cutoff_exclusive": "2026-06-08T00:00:00+08:00",
                    "split_sha256": "a" * 64,
                    "job_row_order_sha256": "b" * 64,
                    "candidate_sha256": {"bm25": "c" * 64},
                    "source_target_sha256": {"exposure": "d" * 64},
                    "uses_test_jd": False,
                    "uses_query_history_replay": False,
                }
            )
        )
        output = temporary / "output"
        build(input_path, sha256_file(input_path), lineage_path, output)
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        groups = np.load(output / "groups.npy", allow_pickle=False)
        assert manifest["complete"] is True and manifest["runtime_activation"] is False
        assert manifest["feature_schema_sha256"] == FEATURE_SCHEMA_SHA256
        assert groups.tolist() == [2]
    print("LTR feature and clipped-IPS self-check passed")


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("self-check")
    build_parser = commands.add_parser("build")
    build_parser.add_argument("--input", type=Path, required=True)
    build_parser.add_argument("--expected-input-sha256", required=True)
    build_parser.add_argument("--lineage", type=Path, required=True)
    build_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "self-check":
        self_check()
    else:
        build(args.input, args.expected_input_sha256, args.lineage, args.output)


if __name__ == "__main__":
    main()
