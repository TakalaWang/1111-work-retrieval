#!/usr/bin/env python3
"""Fail-closed verifier for fixed-339 temporal-v3 promotion evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
from pathlib import Path
from typing import cast

from pipeline_contract import (
    artifact_entry,
    atomic_json,
    canonical_json,
    exact_keys,
    read_json_object,
    require_sha256,
    sha256_file,
    verify_local_inventory,
)

METRICS = ("ndcg_at_10", "precision_at_10", "top_1", "mrr")
KNOWN_CANONICAL_QUERIES_SHA256 = "d2653a885de437845ddc91f6c0667641ed8b08b1f505a376e0f7bdfa142f7214"
KNOWN_EVALUATION_SPLIT_SHA256 = "e68a4e5cbed7c356b0029eb936dbc5b8d8195b19a056297e70a0ab6cf99a6c9e"
KNOWN_QRELS_SHA256 = "a32140dca3495ceff00ca176d05af5ddd66b87ac99f0a705ca6c3da0ed05c6ff"
KNOWN_BASELINE_RUN_SHA256 = "35db1ef0a61066d9ffe4e5b28a30b5901607ad201f41e4b7b22cf6af20a6f214"
KNOWN_BASELINE_MANIFEST_SHA256 = "d3bdb5a4599b44c0ec846030f51477b26eb5db3c2c876b281159ea7bae8dd9f1"
KNOWN_JOBS_SHA256 = "53937f7bf076789c4cd7e3be34fb89875336108d57707b5a93182181e1087089"
SEARCH_QID = re.compile(r"ctx:(?P<context>[0-9]+):search:(?P<search>[0-9]+)")
EVIDENCE_PATHS = {
    "canonical_queries": "canonical-queries.jsonl",
    "evaluation_split": "split-manifest.json",
    "qrels": "qrels.txt",
    "baseline_run": "baseline.run",
    "baseline_run_manifest": "baseline-run.manifest.json",
    "candidate_run": "candidate.run",
    "candidate_run_manifest": "candidate-run.manifest.json",
    "evaluated_candidate_manifest": "evaluated-candidate-manifest.json",
    "evaluated_candidate_build_manifest": "evaluated-candidate-build-manifest.json",
}
ATTESTATION_KEYS = {
    "schema_version",
    "complete",
    "attestation_kind",
    "experiment",
    "promotion_allowed",
    "official_score_claimed",
    "evaluator_id",
    "query_count",
    "candidate_policy_sha256",
    "evaluated_candidate_manifest_sha256",
    "canonical_queries_sha256",
    "evaluation_split_sha256",
    "qrels_sha256",
    "baseline_run_sha256",
    "candidate_run_sha256",
    "metrics",
    "coverage",
    "artifacts",
}
METRIC_KEYS = {"baseline", "candidate", "delta"}
COVERAGE_KEYS = {"zero_result_contexts", "underfilled_top_10_contexts"}
COMPONENT_POLICY_FIELDS = (
    "engine",
    "jobs_sha256",
    "job_row_order_sha256",
    "schema_fields",
    "field_boosts",
    "lexical_policy_version",
    "lexical_policy_sha256",
    "tokenizers",
    "source_fields",
    "query_corrections",
    "filter_semantics",
    "updated_at_field",
    "temporal_filter_semantics",
)
BUILD_POLICY_FIELDS = (
    "dataset_sha256",
    "rows",
    "source_csv_fields",
    "salary_filter_excluded_rows",
)
SHARED_FIELDS = (
    "engine",
    "jobs_sha256",
    "job_row_order_sha256",
    "lexical_policy_version",
    "lexical_policy_sha256",
    "tokenizers",
    "source_fields",
    "query_corrections",
)
SOURCE_FILES = (
    "packages/search-core/src/work_retrieval_core/constraints.py",
    "packages/search-core/src/work_retrieval_core/adapters.py",
    "packages/search-core/src/work_retrieval_core/engine.py",
    "packages/search-core/src/work_retrieval_core/serialization.py",
    "packages/database/src/work_retrieval_database/repository.py",
    "scripts/tantivy_index_pipeline.py",
    "scripts/tantivy_graph_off_runner.py",
)


def candidate_policy_fingerprint(
    *, candidate_manifest_path: Path, candidate_build_manifest_path: Path
) -> tuple[str, dict[str, object]]:
    """Hash stable retrieval semantics, excluding nondeterministic Tantivy segment bytes."""

    component = read_json_object(candidate_manifest_path, "temporal-v3 component manifest")
    build = read_json_object(candidate_build_manifest_path, "temporal-v3 build manifest")
    if any(name not in component for name in COMPONENT_POLICY_FIELDS) or any(
        name not in build for name in BUILD_POLICY_FIELDS
    ):
        raise RuntimeError("temporal-v3 candidate policy fields are incomplete")
    if any(component.get(name) != build.get(name) for name in SHARED_FIELDS):
        raise RuntimeError("temporal-v3 component and build policies differ")
    if component.get("build_manifest_sha256") != sha256_file(candidate_build_manifest_path):
        raise RuntimeError("temporal-v3 build manifest bytes differ from the component")
    for name in ("jobs_sha256", "job_row_order_sha256", "lexical_policy_sha256"):
        require_sha256(component[name], f"temporal-v3 candidate {name}")
    require_sha256(build["dataset_sha256"], "temporal-v3 candidate dataset_sha256")
    rows = build["rows"]
    excluded = build["salary_filter_excluded_rows"]
    if (
        isinstance(rows, bool)
        or not isinstance(rows, int)
        or rows < 1
        or isinstance(excluded, bool)
        or not isinstance(excluded, int)
        or not 0 <= excluded <= rows
    ):
        raise RuntimeError("temporal-v3 candidate row accounting differs")
    identity = {
        "schema_version": 1,
        "component": {name: component[name] for name in COMPONENT_POLICY_FIELDS},
        "build": {name: build[name] for name in BUILD_POLICY_FIELDS},
        "source_files": {
            name: sha256_file(Path(__file__).parents[1] / name) for name in SOURCE_FILES
        },
    }
    return hashlib.sha256(canonical_json(identity)).hexdigest(), identity


def _evidence_paths(attestation_path: Path, artifacts: object) -> dict[str, Path]:
    verified = verify_local_inventory(root=attestation_path.parent, artifacts=artifacts)
    by_path = {cast(str, item["path"]): item for item in verified}
    if set(by_path) != set(EVIDENCE_PATHS.values()) or any(
        item["kind"] != "evidence" for item in by_path.values()
    ):
        raise RuntimeError("temporal-v3 promotion evidence inventory differs")
    paths = {name: attestation_path.parent / path for name, path in EVIDENCE_PATHS.items()}
    expected = {
        "canonical_queries": KNOWN_CANONICAL_QUERIES_SHA256,
        "evaluation_split": KNOWN_EVALUATION_SPLIT_SHA256,
        "qrels": KNOWN_QRELS_SHA256,
        "baseline_run": KNOWN_BASELINE_RUN_SHA256,
        "baseline_run_manifest": KNOWN_BASELINE_MANIFEST_SHA256,
    }
    for name, digest in expected.items():
        if by_path[EVIDENCE_PATHS[name]]["sha256"] != digest:
            raise RuntimeError(f"fixed-339 evidence differs: {name}")
    return paths


def _canonical_qids(path: Path) -> tuple[str, ...]:
    qids: list[str] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"canonical query line {line_number} is invalid") from error
        qid = value.get("qid") if isinstance(value, dict) else None
        if not isinstance(qid, str) or qid in seen:
            raise RuntimeError(f"canonical query line {line_number} has an invalid qid")
        seen.add(qid)
        qids.append(qid)
    if len(qids) != 339:
        raise RuntimeError("fixed-339 canonical query count differs")
    return tuple(qids)


def _read_run(path: Path) -> dict[str, list[str]]:
    runs: dict[str, list[str]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        fields = line.split()
        if len(fields) != 6:
            raise RuntimeError(f"run line {line_number} must have six fields")
        qid, q0, job_id, raw_rank, raw_score, _tag = fields
        try:
            rank, score = int(raw_rank), float(raw_score)
        except ValueError as error:
            raise RuntimeError(f"run line {line_number} rank or score is invalid") from error
        rows = runs.setdefault(qid, [])
        if (
            q0 != "Q0"
            or not qid.startswith("ctx:")
            or not qid.removeprefix("ctx:").isdecimal()
            or not job_id.isdecimal()
            or rank != len(rows) + 1
            or not math.isfinite(score)
            or job_id in rows
        ):
            raise RuntimeError(f"run line {line_number} violates the fixed-339 contract")
        rows.append(job_id)
    return runs


def _read_qrels(path: Path) -> dict[str, tuple[str, dict[str, float]]]:
    qrels: dict[str, tuple[str, dict[str, float]]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        fields = line.split()
        if len(fields) != 4:
            raise RuntimeError(f"qrels line {line_number} must have four fields")
        search_qid, iteration, job_id, raw_grade = fields
        match = SEARCH_QID.fullmatch(search_qid)
        try:
            grade = float(raw_grade)
        except ValueError as error:
            raise RuntimeError(f"qrels line {line_number} grade is invalid") from error
        if (
            match is None
            or iteration != "0"
            or not job_id.isdecimal()
            or not math.isfinite(grade)
            or grade <= 0
        ):
            raise RuntimeError(f"qrels line {line_number} violates the fixed-339 contract")
        context_qid = f"ctx:{match.group('context')}"
        existing_context, ratings = qrels.setdefault(search_qid, (context_qid, {}))
        if existing_context != context_qid or job_id in ratings:
            raise RuntimeError(f"qrels line {line_number} duplicates a judgment")
        ratings[job_id] = grade
    if not qrels:
        raise RuntimeError("fixed-339 qrels are empty")
    return qrels


def _score(
    runs: dict[str, list[str]], qrels: dict[str, tuple[str, dict[str, float]]]
) -> dict[str, float]:
    rows: list[tuple[float, float, float, float]] = []
    for search_qid in sorted(qrels):
        context_qid, ratings = qrels[search_qid]
        ranked = runs.get(context_qid, [])
        ideal = sorted(ratings.values(), reverse=True)[:10]
        idcg = sum(value / math.log2(rank + 1) for rank, value in enumerate(ideal, 1))
        hits = [
            (rank, ratings[job_id])
            for rank, job_id in enumerate(ranked[:10], 1)
            if job_id in ratings
        ]
        first = min((rank for rank, _grade in hits), default=None)
        rows.append(
            (
                sum(grade / math.log2(rank + 1) for rank, grade in hits) / idcg,
                len(hits) / 10.0,
                float(first == 1),
                0.0 if first is None else 1.0 / first,
            )
        )
    return {
        metric: math.fsum(row[index] for row in rows) / len(rows)
        for index, metric in enumerate(METRICS)
    }


def _coverage(runs: dict[str, list[str]], qids: tuple[str, ...]) -> dict[str, int]:
    return {
        "zero_result_contexts": sum(qid not in runs for qid in qids),
        "underfilled_top_10_contexts": sum(len(runs.get(qid, [])) < 10 for qid in qids),
    }


def _validate_candidate_run_manifest(
    path: Path,
    *,
    run_path: Path,
    canonical_qids: tuple[str, ...],
    evaluated_manifest_sha256: str,
) -> None:
    value = read_json_object(path, "temporal-v3 candidate run manifest")
    non_graph_inputs = value.get("non_graph_inputs")
    zero = value.get("zero_result_qids")
    if (
        value.get("schema_version") != 1
        or value.get("complete") is not True
        or value.get("variant") != "graph_off"
        or value.get("split_manifest_sha256") != KNOWN_EVALUATION_SPLIT_SHA256
        or value.get("canonical_qids") != list(canonical_qids)
        or not isinstance(zero, list)
        or any(not isinstance(qid, str) for qid in zero)
        or value.get("run_sha256") != sha256_file(run_path)
        or not isinstance(non_graph_inputs, dict)
        or non_graph_inputs.get("canonical_queries") != KNOWN_CANONICAL_QUERIES_SHA256
        or non_graph_inputs.get("jobs_csv") != KNOWN_JOBS_SHA256
        or non_graph_inputs.get("tantivy_component_manifest") != evaluated_manifest_sha256
    ):
        raise RuntimeError("temporal-v3 candidate run lineage differs")
    expected_sources = {
        f"source:{name}": sha256_file(Path(__file__).parents[1] / name)
        for name in (
            "packages/search-core/src/work_retrieval_core/constraints.py",
            "packages/search-core/src/work_retrieval_core/adapters.py",
            "packages/search-core/src/work_retrieval_core/engine.py",
            "packages/search-core/src/work_retrieval_core/serialization.py",
            "scripts/tantivy_index_pipeline.py",
            "scripts/tantivy_graph_off_runner.py",
        )
    }
    if any(non_graph_inputs.get(name) != digest for name, digest in expected_sources.items()):
        raise RuntimeError("temporal-v3 candidate run source lineage differs")
    runs = _read_run(run_path)
    if set(runs) != set(canonical_qids).difference(cast(list[str], zero)):
        raise RuntimeError("temporal-v3 candidate run coverage differs from its manifest")


def create_attestation(
    *,
    output: Path,
    canonical_queries_path: Path,
    evaluation_split_path: Path,
    qrels_path: Path,
    baseline_run_path: Path,
    baseline_run_manifest_path: Path,
    candidate_run_path: Path,
    candidate_run_manifest_path: Path,
    candidate_manifest_path: Path,
    candidate_build_manifest_path: Path,
) -> dict[str, object]:
    """Seal fixed evidence and create an attestation only when every gate passes."""

    if output.exists():
        raise RuntimeError("temporal-v3 evidence output already exists")
    fixed = {
        canonical_queries_path: KNOWN_CANONICAL_QUERIES_SHA256,
        evaluation_split_path: KNOWN_EVALUATION_SPLIT_SHA256,
        qrels_path: KNOWN_QRELS_SHA256,
        baseline_run_path: KNOWN_BASELINE_RUN_SHA256,
        baseline_run_manifest_path: KNOWN_BASELINE_MANIFEST_SHA256,
    }
    if any(sha256_file(path) != digest for path, digest in fixed.items()):
        raise RuntimeError("fixed-339 source evidence differs from the approved bytes")
    partial = output.with_name(f".{output.name}.partial")
    if partial.exists():
        raise RuntimeError("temporal-v3 partial evidence output already exists")
    partial.mkdir(parents=True)
    sources = {
        "canonical_queries": canonical_queries_path,
        "evaluation_split": evaluation_split_path,
        "qrels": qrels_path,
        "baseline_run": baseline_run_path,
        "baseline_run_manifest": baseline_run_manifest_path,
        "candidate_run": candidate_run_path,
        "candidate_run_manifest": candidate_run_manifest_path,
        "evaluated_candidate_manifest": candidate_manifest_path,
        "evaluated_candidate_build_manifest": candidate_build_manifest_path,
    }
    try:
        for name, source in sources.items():
            shutil.copyfile(source, partial / EVIDENCE_PATHS[name])
        copied = {name: partial / path for name, path in EVIDENCE_PATHS.items()}
        canonical_qids = _canonical_qids(copied["canonical_queries"])
        evaluated_manifest_sha256 = sha256_file(copied["evaluated_candidate_manifest"])
        _validate_candidate_run_manifest(
            copied["candidate_run_manifest"],
            run_path=copied["candidate_run"],
            canonical_qids=canonical_qids,
            evaluated_manifest_sha256=evaluated_manifest_sha256,
        )
        policy_sha256, _identity = candidate_policy_fingerprint(
            candidate_manifest_path=copied["evaluated_candidate_manifest"],
            candidate_build_manifest_path=copied["evaluated_candidate_build_manifest"],
        )
        qrels = _read_qrels(copied["qrels"])
        if set(canonical_qids) != {context for context, _ratings in qrels.values()}:
            raise RuntimeError("fixed-339 query and qrels coverage differs")
        runs = {
            "baseline": _read_run(copied["baseline_run"]),
            "candidate": _read_run(copied["candidate_run"]),
        }
        scores = {name: _score(run, qrels) for name, run in runs.items()}
        metrics = {
            metric: {
                "baseline": scores["baseline"][metric],
                "candidate": scores["candidate"][metric],
                "delta": scores["candidate"][metric] - scores["baseline"][metric],
            }
            for metric in METRICS
        }
        coverage = {name: _coverage(run, canonical_qids) for name, run in runs.items()}
        if metrics["ndcg_at_10"]["delta"] <= 0 or any(
            metrics[metric]["delta"] < 0 for metric in METRICS
        ):
            raise RuntimeError("temporal-v3 fixed-339 ranking gate did not pass")
        if any(
            coverage["candidate"][metric] > coverage["baseline"][metric] for metric in COVERAGE_KEYS
        ):
            raise RuntimeError("temporal-v3 fixed-339 coverage gate did not pass")
        artifacts = [
            artifact_entry(path, relative_to=partial, kind="evidence") for path in copied.values()
        ]
        attestation: dict[str, object] = {
            "schema_version": 1,
            "complete": True,
            "attestation_kind": "fixed-339-temporal-v3-promotion",
            "experiment": "fixed-339-temporal-v3-typed-constraint-ablation",
            "promotion_allowed": True,
            "official_score_claimed": False,
            "evaluator_id": "fixed-339-gt1-development-proxy-v1",
            "query_count": 339,
            "candidate_policy_sha256": policy_sha256,
            "evaluated_candidate_manifest_sha256": evaluated_manifest_sha256,
            "canonical_queries_sha256": KNOWN_CANONICAL_QUERIES_SHA256,
            "evaluation_split_sha256": KNOWN_EVALUATION_SPLIT_SHA256,
            "qrels_sha256": KNOWN_QRELS_SHA256,
            "baseline_run_sha256": KNOWN_BASELINE_RUN_SHA256,
            "candidate_run_sha256": sha256_file(copied["candidate_run"]),
            "metrics": metrics,
            "coverage": coverage,
            "artifacts": artifacts,
        }
        atomic_json(partial / "attestation.json", attestation)
        partial.replace(output)
        return {
            "passed": True,
            "attestation_path": str(output / "attestation.json"),
            "attestation_sha256": sha256_file(output / "attestation.json"),
            "candidate_policy_sha256": policy_sha256,
            "metrics": metrics,
            "coverage": coverage,
        }
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise


def verify_attestation(
    *,
    attestation_path: Path,
    approved_attestation_sha256: str,
    candidate_manifest_path: Path,
    candidate_build_manifest_path: Path,
) -> dict[str, object]:
    """Verify immutable, externally produced positive/non-regressing evidence."""

    require_sha256(approved_attestation_sha256, "approved attestation SHA-256")
    if sha256_file(attestation_path) != approved_attestation_sha256:
        raise RuntimeError("temporal-v3 attestation bytes differ from the approved SHA-256")
    candidate_policy_sha256, _identity = candidate_policy_fingerprint(
        candidate_manifest_path=candidate_manifest_path,
        candidate_build_manifest_path=candidate_build_manifest_path,
    )
    attestation = read_json_object(attestation_path, "temporal-v3 promotion attestation")
    exact_keys(attestation, ATTESTATION_KEYS, "temporal-v3 promotion attestation")
    paths = _evidence_paths(attestation_path, attestation["artifacts"])
    evaluated_manifest_sha256 = sha256_file(paths["evaluated_candidate_manifest"])
    evaluated_policy_sha256, _evaluated_identity = candidate_policy_fingerprint(
        candidate_manifest_path=paths["evaluated_candidate_manifest"],
        candidate_build_manifest_path=paths["evaluated_candidate_build_manifest"],
    )
    evaluator_id = attestation["evaluator_id"]
    if (
        attestation["schema_version"] != 1
        or attestation["complete"] is not True
        or attestation["attestation_kind"] != "fixed-339-temporal-v3-promotion"
        or attestation["experiment"] != "fixed-339-temporal-v3-typed-constraint-ablation"
        or attestation["promotion_allowed"] is not True
        or attestation["official_score_claimed"] is not False
        or not isinstance(evaluator_id, str)
        or not evaluator_id.strip()
        or attestation["query_count"] != 339
        or attestation["candidate_policy_sha256"] != candidate_policy_sha256
        or evaluated_policy_sha256 != candidate_policy_sha256
        or attestation["evaluated_candidate_manifest_sha256"] != evaluated_manifest_sha256
        or attestation["canonical_queries_sha256"] != KNOWN_CANONICAL_QUERIES_SHA256
        or attestation["evaluation_split_sha256"] != KNOWN_EVALUATION_SPLIT_SHA256
        or attestation["qrels_sha256"] != KNOWN_QRELS_SHA256
        or attestation["baseline_run_sha256"] != KNOWN_BASELINE_RUN_SHA256
        or attestation["candidate_run_sha256"] != sha256_file(paths["candidate_run"])
    ):
        raise RuntimeError("temporal-v3 promotion policy or lineage differs")
    for name in (
        "evaluated_candidate_manifest_sha256",
        "canonical_queries_sha256",
        "evaluation_split_sha256",
        "qrels_sha256",
        "baseline_run_sha256",
        "candidate_run_sha256",
    ):
        require_sha256(attestation[name], f"temporal-v3 promotion {name}")

    canonical_qids = _canonical_qids(paths["canonical_queries"])
    _validate_candidate_run_manifest(
        paths["candidate_run_manifest"],
        run_path=paths["candidate_run"],
        canonical_qids=canonical_qids,
        evaluated_manifest_sha256=evaluated_manifest_sha256,
    )
    qrels = _read_qrels(paths["qrels"])
    if set(canonical_qids) != {context for context, _ratings in qrels.values()}:
        raise RuntimeError("fixed-339 query and qrels coverage differs")
    baseline_run = _read_run(paths["baseline_run"])
    candidate_run = _read_run(paths["candidate_run"])
    recomputed = {
        "baseline": _score(baseline_run, qrels),
        "candidate": _score(candidate_run, qrels),
    }

    metrics = attestation["metrics"]
    if not isinstance(metrics, dict) or set(metrics) != set(METRICS):
        raise RuntimeError("temporal-v3 promotion metric set differs")
    parsed: dict[str, dict[str, float]] = {}
    for metric in METRICS:
        values = metrics[metric]
        if not isinstance(values, dict):
            raise RuntimeError(f"temporal-v3 promotion metric is malformed: {metric}")
        exact_keys(values, METRIC_KEYS, f"temporal-v3 promotion {metric}")
        numeric: dict[str, float] = {}
        for name in METRIC_KEYS:
            value = values[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise RuntimeError(f"temporal-v3 promotion metric is invalid: {metric}.{name}")
            numeric[name] = float(value)
        if not 0 <= numeric["baseline"] <= 1 or not 0 <= numeric["candidate"] <= 1:
            raise RuntimeError(f"temporal-v3 promotion score is outside [0, 1]: {metric}")
        expected_delta = numeric["candidate"] - numeric["baseline"]
        if not math.isclose(numeric["delta"], expected_delta, abs_tol=1e-12):
            raise RuntimeError(f"temporal-v3 promotion delta differs: {metric}")
        if numeric["delta"] < 0:
            raise RuntimeError(f"temporal-v3 promotion regressed: {metric}")
        if any(
            not math.isclose(numeric[name], recomputed[name][metric], abs_tol=1e-12)
            for name in ("baseline", "candidate")
        ):
            raise RuntimeError(f"temporal-v3 promotion metric differs from run bytes: {metric}")
        parsed[metric] = numeric
    if parsed["ndcg_at_10"]["delta"] <= 0:
        raise RuntimeError("temporal-v3 promotion requires a positive NDCG@10 delta")
    coverage = attestation["coverage"]
    recomputed_coverage = {
        "baseline": _coverage(baseline_run, canonical_qids),
        "candidate": _coverage(candidate_run, canonical_qids),
    }
    if not isinstance(coverage, dict) or set(coverage) != {"baseline", "candidate"}:
        raise RuntimeError("temporal-v3 promotion coverage is malformed")
    for variant in ("baseline", "candidate"):
        value = coverage[variant]
        if (
            not isinstance(value, dict)
            or set(value) != COVERAGE_KEYS
            or value != recomputed_coverage[variant]
        ):
            raise RuntimeError(f"temporal-v3 promotion coverage differs: {variant}")
    if any(
        recomputed_coverage["candidate"][metric] > recomputed_coverage["baseline"][metric]
        for metric in COVERAGE_KEYS
    ):
        raise RuntimeError("temporal-v3 promotion coverage regressed")
    return {
        "passed": True,
        "attestation_sha256": approved_attestation_sha256,
        "candidate_policy_sha256": candidate_policy_sha256,
        "query_count": 339,
        "metrics": cast(dict[str, object], parsed),
        "coverage": recomputed_coverage,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    fingerprint = commands.add_parser("fingerprint")
    create = commands.add_parser("create")
    verify = commands.add_parser("verify")
    for command in (fingerprint, verify):
        command.add_argument("--candidate-manifest", type=Path, required=True)
        command.add_argument("--candidate-build-manifest", type=Path, required=True)
    verify.add_argument("--attestation", type=Path, required=True)
    verify.add_argument("--approved-attestation-sha256", required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--canonical-queries", type=Path, required=True)
    create.add_argument("--evaluation-split", type=Path, required=True)
    create.add_argument("--qrels", type=Path, required=True)
    create.add_argument("--baseline-run", type=Path, required=True)
    create.add_argument("--baseline-run-manifest", type=Path, required=True)
    create.add_argument("--candidate-run", type=Path, required=True)
    create.add_argument("--candidate-run-manifest", type=Path, required=True)
    create.add_argument("--candidate-manifest", type=Path, required=True)
    create.add_argument("--candidate-build-manifest", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "fingerprint":
        digest, identity = candidate_policy_fingerprint(
            candidate_manifest_path=args.candidate_manifest,
            candidate_build_manifest_path=args.candidate_build_manifest,
        )
        print(
            json.dumps(
                {"candidate_policy_sha256": digest, "identity": identity},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return
    if args.command == "create":
        print(
            json.dumps(
                create_attestation(
                    output=args.output,
                    canonical_queries_path=args.canonical_queries,
                    evaluation_split_path=args.evaluation_split,
                    qrels_path=args.qrels,
                    baseline_run_path=args.baseline_run,
                    baseline_run_manifest_path=args.baseline_run_manifest,
                    candidate_run_path=args.candidate_run,
                    candidate_run_manifest_path=args.candidate_run_manifest,
                    candidate_manifest_path=args.candidate_manifest,
                    candidate_build_manifest_path=args.candidate_build_manifest,
                ),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return
    print(
        json.dumps(
            verify_attestation(
                attestation_path=args.attestation,
                approved_attestation_sha256=args.approved_attestation_sha256,
                candidate_manifest_path=args.candidate_manifest,
                candidate_build_manifest_path=args.candidate_build_manifest,
            ),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
