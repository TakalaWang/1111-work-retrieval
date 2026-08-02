#!/usr/bin/env python3
"""Evaluate two TREC runs with the competition headline metrics."""

from __future__ import annotations

import argparse
import json
import math
import random
from collections import defaultdict
from pathlib import Path

from pipeline_contract import atomic_json

METRICS = ("ndcg_at_10", "precision_at_10", "top_1", "mrr")


def _qrels(path: Path) -> dict[str, dict[str, int]]:
    result: defaultdict[str, dict[str, int]] = defaultdict(dict)
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        fields = line.split()
        if len(fields) != 4:
            raise RuntimeError(f"qrels line {line_number} must have four fields")
        qid, _iteration, job_id, raw_relevance = fields
        try:
            relevance = int(raw_relevance)
        except ValueError as error:
            raise RuntimeError(f"qrels line {line_number} has invalid relevance") from error
        if relevance < 0:
            raise RuntimeError(f"qrels line {line_number} has negative relevance")
        result[qid][job_id] = max(relevance, result[qid].get(job_id, 0))
    if not result:
        raise RuntimeError("qrels are empty")
    return dict(result)


def _run(path: Path) -> dict[str, tuple[str, ...]]:
    rows: defaultdict[str, list[tuple[int, str]]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        fields = line.split()
        if len(fields) != 6:
            raise RuntimeError(f"run line {line_number} must have six fields")
        qid, _q0, job_id, raw_rank, _score, _tag = fields
        try:
            rank = int(raw_rank)
        except ValueError as error:
            raise RuntimeError(f"run line {line_number} has invalid rank") from error
        if rank < 1 or (qid, job_id) in seen:
            raise RuntimeError(f"run line {line_number} has duplicate or invalid ranking")
        seen.add((qid, job_id))
        rows[qid].append((rank, job_id))
    result: dict[str, tuple[str, ...]] = {}
    for qid, ranked in rows.items():
        ranked.sort()
        if [rank for rank, _job_id in ranked] != list(range(1, len(ranked) + 1)):
            raise RuntimeError(f"run ranks are not contiguous for query {qid}")
        result[qid] = tuple(job_id for _rank, job_id in ranked)
    return result


def _query_metrics(relevance: dict[str, int], ranking: tuple[str, ...]) -> dict[str, float]:
    top = ranking[:10]
    gains = [relevance.get(job_id, 0) for job_id in top]
    dcg = sum(gain / math.log2(rank + 1) for rank, gain in enumerate(gains, 1))
    ideal = sorted(relevance.values(), reverse=True)[:10]
    idcg = sum(gain / math.log2(rank + 1) for rank, gain in enumerate(ideal, 1))
    first_relevant = next(
        (rank for rank, job_id in enumerate(ranking, 1) if relevance.get(job_id, 0) > 0),
        None,
    )
    return {
        "ndcg_at_10": dcg / idcg if idcg else 0.0,
        "precision_at_10": sum(gain > 0 for gain in gains) / 10,
        "top_1": float(bool(ranking and relevance.get(ranking[0], 0) > 0)),
        "mrr": 1.0 / first_relevant if first_relevant is not None else 0.0,
    }


def _mean(rows: list[dict[str, float]]) -> dict[str, float]:
    return {metric: sum(row[metric] for row in rows) / len(rows) for metric in METRICS}


def _significant(off: list[dict[str, float]], on: list[dict[str, float]]) -> bool:
    deltas = [
        after["ndcg_at_10"] - before["ndcg_at_10"] for before, after in zip(off, on, strict=True)
    ]
    generator = random.Random(20260802)
    samples = sorted(
        sum(generator.choice(deltas) for _ in deltas) / len(deltas) for _ in range(5_000)
    )
    return samples[int(len(samples) * 0.025)] > 0.0


def evaluate(qrels_path: Path, off_path: Path, on_path: Path) -> dict[str, object]:
    qrels = _qrels(qrels_path)
    off_run, on_run = _run(off_path), _run(on_path)
    unexpected = (set(off_run) | set(on_run)).difference(qrels)
    if unexpected:
        raise RuntimeError("runs contain query IDs absent from qrels")
    qids = sorted(qrels)
    off_rows = [_query_metrics(qrels[qid], off_run.get(qid, ())) for qid in qids]
    on_rows = [_query_metrics(qrels[qid], on_run.get(qid, ())) for qid in qids]
    off, on = _mean(off_rows), _mean(on_rows)
    return {
        "schema_version": 1,
        "complete": True,
        "query_count": len(qids),
        "graph_off": off,
        "graph_on": on,
        "delta": {metric: on[metric] - off[metric] for metric in METRICS},
        "significant": _significant(off_rows, on_rows),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qrels", type=Path, required=True)
    parser.add_argument("--graph-off-run", type=Path, required=True)
    parser.add_argument("--graph-on-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = evaluate(args.qrels, args.graph_off_run, args.graph_on_run)
    atomic_json(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
