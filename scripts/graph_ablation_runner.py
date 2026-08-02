#!/usr/bin/env python3
"""Run a fixed-input graph-on/off evaluation through an external evaluator."""

from __future__ import annotations

import argparse
import json
import math
import shlex
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

from graph_candidate_runner import ALGORITHM as GRAPH_GENERATION_ALGORITHM
from graph_candidate_runner import PARAMETERS as GRAPH_GENERATION_PARAMETERS
from graph_candidate_runner import _manifest_qids, generate_graph_on, read_trec_run
from pipeline_contract import (
    atomic_json,
    exact_keys,
    read_json_object,
    require_sha256,
    sha256_file,
)
from skill_graph_pipeline import load_split_manifest, validate_graph
from tantivy_graph_off_runner import read_canonical_queries
from tantivy_index_pipeline import DEFAULT_ARTIFACT_PREFIX

RUN_MANIFEST_KEYS = {
    "schema_version",
    "complete",
    "variant",
    "split_manifest_sha256",
    "graph_manifest_sha256",
    "canonical_qids",
    "zero_result_qids",
    "non_graph_inputs",
    "run_sha256",
}
METRICS = ("ndcg_at_10", "precision_at_10", "top_1", "mrr")
CONTROL_NAMES = ("shuffled_graph", "placebo_edges", "path_mask")
GENERATION_KEYS = {
    "algorithm",
    "graph_off_run_sha256",
    "graph_off_manifest_sha256",
    "trace_path",
    "trace_sha256",
    "parameters",
    "statistics",
}
GENERATION_STATISTICS_KEYS = {
    "query_count",
    "baseline_rows",
    "graph_covered_seed_rows",
    "queries_with_any_anchors",
    "queries_with_query_exact_anchors",
    "queries_with_duty_filter_anchors",
    "queries_with_consensus_anchors",
    "queries_with_graph_candidates",
    "queries_with_novel_graph_candidates",
    "graph_candidate_rows",
    "novel_graph_candidate_rows",
    "bridge_terms",
    "bridge_retrieval_rows",
    "graph_paths",
}


def _non_graph_inputs(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise RuntimeError("run manifest non_graph_inputs must be a non-empty object")
    parsed: dict[str, str] = {}
    for name, digest in value.items():
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError("non-graph input name must be non-empty")
        parsed[name] = require_sha256(digest, f"non-graph input {name}")
    return parsed


def _run_manifest(
    path: Path,
    *,
    variant: str,
    run_path: Path,
    split_sha256: str,
    graph_sha256: str,
    graph_off_run: Path | None = None,
    graph_off_manifest: Path | None = None,
) -> tuple[dict[str, str], tuple[str, ...], tuple[str, ...]]:
    manifest = read_json_object(path, f"{variant} run manifest")
    expected_keys = RUN_MANIFEST_KEYS | ({"generation"} if variant == "graph_on" else set())
    exact_keys(manifest, expected_keys, f"{variant} run manifest")
    expected_graph: str | None = graph_sha256 if variant == "graph_on" else None
    if (
        manifest["schema_version"] != 1
        or manifest["complete"] is not True
        or manifest["variant"] != variant
        or manifest["split_manifest_sha256"] != split_sha256
        or manifest["graph_manifest_sha256"] != expected_graph
        or manifest["run_sha256"] != sha256_file(run_path)
    ):
        raise RuntimeError(f"{variant} run lineage differs")
    require_sha256(manifest["run_sha256"], f"{variant} run SHA-256")
    canonical_qids, zero_result_qids = _manifest_qids(manifest)
    if variant == "graph_on":
        generation = manifest["generation"]
        if not isinstance(generation, dict):
            raise RuntimeError("graph_on generation manifest must be an object")
        exact_keys(generation, GENERATION_KEYS, "graph_on generation manifest")
        statistics = generation["statistics"]
        if not isinstance(statistics, dict):
            raise RuntimeError("graph_on generation statistics must be an object")
        exact_keys(statistics, GENERATION_STATISTICS_KEYS, "graph_on generation statistics")
        trace_path = generation["trace_path"]
        if (
            graph_off_run is None
            or graph_off_manifest is None
            or generation["algorithm"] != GRAPH_GENERATION_ALGORITHM
            or generation["parameters"] != GRAPH_GENERATION_PARAMETERS
            or generation["graph_off_run_sha256"] != sha256_file(graph_off_run)
            or generation["graph_off_manifest_sha256"] != sha256_file(graph_off_manifest)
            or trace_path != "graph-traces.jsonl"
            or generation["trace_sha256"] != sha256_file(path.parent / str(trace_path))
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in statistics.values()
            )
            or statistics["query_count"] < 1
        ):
            raise RuntimeError("graph_on was not generated from the declared frozen inputs")
        for name in ("graph_off_run_sha256", "graph_off_manifest_sha256", "trace_sha256"):
            require_sha256(generation[name], f"graph_on generation {name}")
    return _non_graph_inputs(manifest["non_graph_inputs"]), canonical_qids, zero_result_qids


def _score_map(value: object, name: str) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != set(METRICS):
        raise RuntimeError(f"evaluator {name} metrics differ")
    parsed: dict[str, float] = {}
    for metric in METRICS:
        score = value[metric]
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
            or not 0 <= score <= 1
        ):
            raise RuntimeError(f"evaluator metric is invalid: {metric}")
        parsed[metric] = float(score)
    return parsed


def _evaluation(path: Path) -> dict[str, object]:
    value = read_json_object(path, "evaluator output")
    exact_keys(
        value,
        {
            "schema_version",
            "complete",
            "query_count",
            "graph_off",
            "graph_on",
            "delta",
            "significant",
        },
        "evaluator output",
    )
    query_count = value["query_count"]
    if (
        value["schema_version"] != 1
        or value["complete"] is not True
        or isinstance(query_count, bool)
        or not isinstance(query_count, int)
        or query_count < 1
        or not isinstance(value["significant"], bool)
    ):
        raise RuntimeError("evaluator output contract differs")
    off = _score_map(value["graph_off"], "graph_off")
    on = _score_map(value["graph_on"], "graph_on")
    delta = value["delta"]
    if not isinstance(delta, dict) or set(delta) != set(METRICS):
        raise RuntimeError("evaluator delta metrics differ")
    calculated = {name: on[name] - off[name] for name in METRICS}
    if any(
        isinstance(delta[name], bool)
        or not isinstance(delta[name], (int, float))
        or not math.isfinite(delta[name])
        or not math.isclose(float(delta[name]), calculated[name], abs_tol=1e-12)
        for name in METRICS
    ):
        raise RuntimeError("evaluator delta does not equal graph_on minus graph_off")
    return {
        "query_count": query_count,
        "graph_off": off,
        "graph_on": on,
        "delta": calculated,
        "significant": value["significant"],
    }


def _evaluate(
    command: list[str], *, qrels: Path, graph_off_run: Path, graph_on_run: Path, output: Path
) -> dict[str, object]:
    if not command:
        raise RuntimeError("an organizer or train-only semantic-proxy evaluator is required")
    result = subprocess.run(
        [
            *command,
            "--qrels",
            str(qrels),
            "--graph-off-run",
            str(graph_off_run),
            "--graph-on-run",
            str(graph_on_run),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        detail = " ".join(result.stderr.split())[:2_000]
        raise RuntimeError(f"external evaluator failed: {detail}")
    return _evaluation(output)


def run_ablation(
    *,
    split_manifest_path: Path,
    graph_output: Path,
    evidence_path: Path,
    extraction_manifest_path: Path,
    qrels_path: Path,
    queries_path: Path,
    jobs_csv: Path,
    tantivy_output: Path,
    artifact_prefix: str,
    graph_off_run: Path,
    graph_off_manifest: Path,
    graph_on_run: Path,
    graph_on_manifest: Path,
    evaluator_command: list[str],
    evaluator_id: str,
    evaluator_kind: str,
    minimum_ndcg_delta: float,
    output: Path,
) -> dict[str, object]:
    if output.exists():
        raise RuntimeError("ablation output already exists; evaluations never overwrite")
    if not evaluator_command:
        raise RuntimeError("an organizer or train-only semantic-proxy evaluator is required")
    if not qrels_path.is_file():
        raise RuntimeError("a SHA-pinned qrels file is required")
    if (
        evaluator_kind not in {"organizer", "train_semantic_proxy"}
        or not evaluator_id.strip()
        or not math.isfinite(minimum_ndcg_delta)
        or minimum_ndcg_delta < 0
    ):
        raise ValueError("evaluator identity and a non-negative NDCG threshold are required")
    split, _ = load_split_manifest(split_manifest_path)
    split_sha256 = sha256_file(split_manifest_path)
    if sha256_file(qrels_path) != split["qrels_sha256"]:
        raise RuntimeError("qrels bytes differ from the split manifest")
    validate_graph(
        graph_output,
        split_manifest_path,
        evidence_path=evidence_path,
        extraction_manifest_path=extraction_manifest_path,
    )
    graph_sha256 = sha256_file(graph_output / "manifest.json")
    off_inputs, off_canonical_qids, off_zero_result_qids = _run_manifest(
        graph_off_manifest,
        variant="graph_off",
        run_path=graph_off_run,
        split_sha256=split_sha256,
        graph_sha256=graph_sha256,
    )
    canonical_qids = tuple(
        record.qid for record in read_canonical_queries(queries_path, split_manifest_path)
    )
    graph_off_qids = tuple(read_trec_run(graph_off_run))
    expected_result_qids = tuple(qid for qid in canonical_qids if qid not in off_zero_result_qids)
    if (
        not canonical_qids
        or off_canonical_qids != canonical_qids
        or graph_off_qids != expected_result_qids
        or set(off_zero_result_qids) != set(canonical_qids).difference(graph_off_qids)
        or off_inputs.get("canonical_queries") != sha256_file(queries_path)
    ):
        raise RuntimeError("canonical and graph_off query coverage differ")
    with tempfile.TemporaryDirectory() as raw:
        temporary = Path(raw)
        regenerated = temporary / "regenerated"
        generate_graph_on(
            split_manifest_path=split_manifest_path,
            graph_output=graph_output,
            evidence_path=evidence_path,
            extraction_manifest_path=extraction_manifest_path,
            graph_off_run=graph_off_run,
            graph_off_manifest=graph_off_manifest,
            queries_path=queries_path,
            jobs_csv=jobs_csv,
            tantivy_output=tantivy_output,
            artifact_prefix=artifact_prefix,
            output=regenerated,
        )
        provided_trace = graph_on_manifest.parent / "graph-traces.jsonl"
        if any(
            sha256_file(provided) != sha256_file(expected)
            for provided, expected in (
                (graph_on_run, regenerated / "graph-on.run"),
                (graph_on_manifest, regenerated / "graph-on.manifest.json"),
                (provided_trace, regenerated / "graph-traces.jsonl"),
            )
        ):
            raise RuntimeError("graph_on artifacts are not byte-identical to fresh generation")
        on_inputs, on_canonical_qids, on_zero_result_qids = _run_manifest(
            graph_on_manifest,
            variant="graph_on",
            run_path=graph_on_run,
            split_sha256=split_sha256,
            graph_sha256=graph_sha256,
            graph_off_run=graph_off_run,
            graph_off_manifest=graph_off_manifest,
        )
        if (
            off_inputs != on_inputs
            or on_canonical_qids != canonical_qids
            or not set(on_zero_result_qids).issubset(off_zero_result_qids)
        ):
            raise RuntimeError("graph-on/off non-graph inputs are not byte-identical")
        expected_on_qids = tuple(qid for qid in canonical_qids if qid not in on_zero_result_qids)
        if tuple(read_trec_run(graph_on_run)) != expected_on_qids:
            raise RuntimeError("canonical graph_off and graph_on query coverage differ")
        evaluation = _evaluate(
            evaluator_command,
            qrels=qrels_path,
            graph_off_run=graph_off_run,
            graph_on_run=graph_on_run,
            output=temporary / "evaluation.json",
        )
    if evaluation["query_count"] != len(canonical_qids):
        raise RuntimeError("evaluator query coverage differs from canonical graph_on/off runs")
    off_metrics = evaluation["graph_off"]
    on_metrics = evaluation["graph_on"]
    deltas = evaluation["delta"]
    if (
        not isinstance(off_metrics, Mapping)
        or not isinstance(on_metrics, Mapping)
        or not isinstance(deltas, Mapping)
    ):
        raise RuntimeError("evaluator metrics are malformed")
    metric_gate_passed = (
        evaluator_kind == "organizer"
        and evaluation["significant"] is True
        and deltas["ndcg_at_10"] > 0
        and deltas["ndcg_at_10"] >= minimum_ndcg_delta
    )
    report: dict[str, object] = {
        "schema_version": 1,
        "complete": True,
        "experiment": "fixed-input-graph-on-off-ablation",
        "attestation_kind": "fixed-input-graph-promotion",
        "candidate_manifest_sha256": graph_sha256,
        "publication_allowed": False,
        "official_score_claimed": False,
        "evaluator": {"id": evaluator_id, "kind": evaluator_kind},
        "split_manifest_sha256": split_sha256,
        "qrels_sha256": split["qrels_sha256"],
        "graph_manifest_sha256": graph_sha256,
        "graph_off_run_sha256": sha256_file(graph_off_run),
        "graph_on_run_sha256": sha256_file(graph_on_run),
        "non_graph_inputs": off_inputs,
        "query_count": evaluation["query_count"],
        "graph_off": off_metrics,
        "graph_on": on_metrics,
        "delta": deltas,
        "minimum_ndcg_delta": minimum_ndcg_delta,
        "organizer_significant": evaluation["significant"],
        "metric_gate_passed": metric_gate_passed,
        "promotion_allowed": False,
        "promotion_gate": "requires_external_organizer_attestation",
        "controls": {
            name: {"enabled": False, "reason": "control run not provided"} for name in CONTROL_NAMES
        },
    }
    atomic_json(output, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split-manifest", type=Path, required=True)
    parser.add_argument("--graph-output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--extraction-manifest", type=Path, required=True)
    parser.add_argument("--qrels", type=Path, required=True)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--jobs-csv", type=Path, required=True)
    parser.add_argument("--tantivy-output", type=Path, required=True)
    parser.add_argument("--artifact-prefix", default=DEFAULT_ARTIFACT_PREFIX)
    parser.add_argument("--graph-off-run", type=Path, required=True)
    parser.add_argument("--graph-off-manifest", type=Path, required=True)
    parser.add_argument("--graph-on-run", type=Path, required=True)
    parser.add_argument("--graph-on-manifest", type=Path, required=True)
    parser.add_argument("--evaluator-command", required=True)
    parser.add_argument("--evaluator-id", required=True)
    parser.add_argument(
        "--evaluator-kind", choices=("organizer", "train_semantic_proxy"), required=True
    )
    parser.add_argument("--minimum-ndcg-delta", type=float, default=0.001)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run_ablation(
        split_manifest_path=args.split_manifest,
        graph_output=args.graph_output,
        evidence_path=args.evidence,
        extraction_manifest_path=args.extraction_manifest,
        qrels_path=args.qrels,
        queries_path=args.queries,
        jobs_csv=args.jobs_csv,
        tantivy_output=args.tantivy_output,
        artifact_prefix=args.artifact_prefix,
        graph_off_run=args.graph_off_run,
        graph_off_manifest=args.graph_off_manifest,
        graph_on_run=args.graph_on_run,
        graph_on_manifest=args.graph_on_manifest,
        evaluator_command=shlex.split(args.evaluator_command),
        evaluator_id=args.evaluator_id,
        evaluator_kind=args.evaluator_kind,
        minimum_ndcg_delta=args.minimum_ndcg_delta,
        output=args.output,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
