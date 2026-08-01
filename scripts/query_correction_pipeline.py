#!/usr/bin/env python3
"""Build and promotion-gate train-only query correction candidates."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from pipeline_contract import (
    atomic_json,
    exact_keys,
    read_json_object,
    require_sha256,
    sha256_file,
)
from skill_graph_pipeline import _load_extraction, load_split_manifest
from work_retrieval_core.serialization import canonical_code

CANDIDATE_KEYS = {
    "schema_version",
    "complete",
    "publication_allowed",
    "source_policy",
    "test_jd_used",
    "uses_ground_truth",
    "uses_behavior_logs",
    "train_cutoff_exclusive",
    "max_source_timestamp",
    "source_manifest_sha256",
    "evidence_sha256",
    "minimum_support",
    "corrections",
}
REPORT_KEYS = {
    "schema_version",
    "complete",
    "experiment",
    "candidate_sha256",
    "primary_metric",
    "absolute_delta",
    "evaluator_kind",
    "significant",
    "evaluation_split_sha256",
    "baseline_run_sha256",
    "candidate_run_sha256",
}


def build_candidate(
    *,
    evidence_path: Path,
    extraction_manifest_path: Path,
    split_manifest_path: Path,
    minimum_support: int,
    output: Path,
) -> dict[str, object]:
    if minimum_support < 1:
        raise ValueError("query correction minimum support must be positive")
    if output.exists():
        raise RuntimeError("query correction candidate already exists; builds never overwrite")
    _split, cutoff = load_split_manifest(split_manifest_path)
    extraction, records = _load_extraction(evidence_path, extraction_manifest_path, cutoff)
    pairs: Counter[tuple[str, str]] = Counter()
    targets: dict[str, set[str]] = defaultdict(set)
    for record in records:
        for canonical, evidence in record["skills"].items():
            surface = canonical_code(evidence["surface"])
            target = canonical_code(canonical)
            if surface and target and surface != target:
                pairs[(surface, target)] += 1
                targets[surface].add(target)
    corrections = {
        source: target
        for (source, target), support in sorted(pairs.items())
        if support >= minimum_support and len(targets[source]) == 1
    }
    if not corrections:
        raise RuntimeError("train-only evidence produced no supported unambiguous corrections")
    candidate = {
        "schema_version": 1,
        "complete": True,
        "publication_allowed": False,
        "source_policy": "train_jd_only",
        "test_jd_used": False,
        "uses_ground_truth": False,
        "uses_behavior_logs": False,
        "train_cutoff_exclusive": extraction["train_cutoff_exclusive"],
        "max_source_timestamp": extraction["max_source_timestamp"],
        "source_manifest_sha256": sha256_file(extraction_manifest_path),
        "evidence_sha256": extraction["evidence_sha256"],
        "minimum_support": minimum_support,
        "corrections": corrections,
    }
    atomic_json(output, candidate)
    validate_candidate(output, split_manifest_path)
    return candidate


def validate_candidate(path: Path, split_manifest_path: Path) -> dict[str, object]:
    _split, cutoff = load_split_manifest(split_manifest_path)
    candidate = read_json_object(path, "query correction candidate")
    exact_keys(candidate, CANDIDATE_KEYS, "query correction candidate")
    if (
        candidate["schema_version"] != 1
        or candidate["complete"] is not True
        or candidate["publication_allowed"] is not False
        or candidate["source_policy"] != "train_jd_only"
        or candidate["test_jd_used"] is not False
        or candidate["uses_ground_truth"] is not False
        or candidate["uses_behavior_logs"] is not False
    ):
        raise RuntimeError("query correction candidate policy differs")
    from skill_graph_pipeline import _timestamp

    if (
        _timestamp(candidate["train_cutoff_exclusive"], "correction cutoff") != cutoff
        or _timestamp(candidate["max_source_timestamp"], "correction maximum") >= cutoff
    ):
        raise RuntimeError("query correction candidate includes non-train evidence")
    for name in ("source_manifest_sha256", "evidence_sha256"):
        require_sha256(candidate[name], f"query correction {name}")
    support = candidate["minimum_support"]
    if isinstance(support, bool) or not isinstance(support, int) or support < 1:
        raise RuntimeError("query correction minimum support differs")
    corrections = candidate["corrections"]
    if not isinstance(corrections, dict) or not corrections:
        raise RuntimeError("query correction candidate must be non-empty")
    for source, target in corrections.items():
        if (
            not isinstance(source, str)
            or not isinstance(target, str)
            or canonical_code(source) != source
            or canonical_code(target) != target
            or source == target
        ):
            raise RuntimeError("query correction candidate contains a non-canonical rule")
    return {
        "passed": True,
        "candidate_sha256": sha256_file(path),
        "corrections": len(corrections),
    }


def approve_candidate(
    *,
    candidate_path: Path,
    split_manifest_path: Path,
    promotion_report_path: Path,
    promotion_report_sha256: str,
    attestation_path: Path,
) -> dict[str, object]:
    if attestation_path.exists():
        raise RuntimeError("query correction attestation already exists")
    load_split_manifest(split_manifest_path)
    validation = validate_candidate(candidate_path, split_manifest_path)
    require_sha256(promotion_report_sha256, "query correction promotion report SHA-256")
    if sha256_file(promotion_report_path) != promotion_report_sha256:
        raise RuntimeError("query correction promotion report bytes differ")
    report = read_json_object(promotion_report_path, "query correction promotion report")
    exact_keys(report, REPORT_KEYS, "query correction promotion report")
    delta = report["absolute_delta"]
    if (
        report["schema_version"] != 1
        or report["complete"] is not True
        or report["experiment"] != "query correction fixed-input ablation"
        or report["candidate_sha256"] != validation["candidate_sha256"]
        or report["primary_metric"] != "ndcg_at_10"
        or report["evaluator_kind"] != "organizer"
        or report["significant"] is not True
        or report["evaluation_split_sha256"] != sha256_file(split_manifest_path)
        or isinstance(delta, bool)
        or not isinstance(delta, (int, float))
        or not math.isfinite(delta)
        or delta <= 0
    ):
        raise RuntimeError("query correction promotion evidence did not pass")
    for name in (
        "baseline_run_sha256",
        "candidate_run_sha256",
    ):
        require_sha256(report[name], f"query correction promotion {name}")
    attestation = {
        "schema_version": 1,
        "complete": True,
        "attestation_kind": "fixed-input-query-correction-promotion",
        "candidate_sha256": validation["candidate_sha256"],
        "promotion_report_sha256": promotion_report_sha256,
        "publication_allowed": True,
        "evaluator_kind": "organizer",
        "significant": True,
        "primary_metric": "ndcg_at_10",
        "absolute_delta": float(delta),
        "evaluation_split_sha256": report["evaluation_split_sha256"],
        "baseline_run_sha256": report["baseline_run_sha256"],
        "candidate_run_sha256": report["candidate_run_sha256"],
    }
    atomic_json(attestation_path, attestation)
    return attestation


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--evidence", type=Path, required=True)
    build.add_argument("--extraction-manifest", type=Path, required=True)
    build.add_argument("--split-manifest", type=Path, required=True)
    build.add_argument("--minimum-support", type=int, default=3)
    build.add_argument("--output", type=Path, required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--candidate", type=Path, required=True)
    validate.add_argument("--split-manifest", type=Path, required=True)
    approve = commands.add_parser("approve")
    approve.add_argument("--candidate", type=Path, required=True)
    approve.add_argument("--split-manifest", type=Path, required=True)
    approve.add_argument("--promotion-report", type=Path, required=True)
    approve.add_argument("--promotion-report-sha256", required=True)
    approve.add_argument("--attestation", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "build":
        result = build_candidate(
            evidence_path=args.evidence,
            extraction_manifest_path=args.extraction_manifest,
            split_manifest_path=args.split_manifest,
            minimum_support=args.minimum_support,
            output=args.output,
        )
    elif args.command == "validate":
        result = validate_candidate(args.candidate, args.split_manifest)
    else:
        result = approve_candidate(
            candidate_path=args.candidate,
            split_manifest_path=args.split_manifest,
            promotion_report_path=args.promotion_report,
            promotion_report_sha256=args.promotion_report_sha256,
            attestation_path=args.attestation,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
