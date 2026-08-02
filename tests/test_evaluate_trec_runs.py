from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_builtin_evaluator_scores_graph_on_and_off(tmp_path: Path) -> None:
    qrels = tmp_path / "qrels.txt"
    graph_off = tmp_path / "off.run"
    graph_on = tmp_path / "on.run"
    output = tmp_path / "metrics.json"
    qrels.write_text("q1 0 j1 2\nq2 0 j2 1\n", encoding="utf-8")
    graph_off.write_text("q1 Q0 j9 1 1 off\nq2 Q0 j9 1 1 off\n", encoding="utf-8")
    graph_on.write_text("q1 Q0 j1 1 1 on\nq2 Q0 j2 1 1 on\n", encoding="utf-8")

    subprocess.run(
        [
            sys.executable,
            "scripts/evaluate_trec_runs.py",
            "--qrels",
            str(qrels),
            "--graph-off-run",
            str(graph_off),
            "--graph-on-run",
            str(graph_on),
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["query_count"] == 2
    assert report["graph_off"]["ndcg_at_10"] == 0.0
    assert report["graph_on"] == {
        "mrr": 1.0,
        "ndcg_at_10": 1.0,
        "precision_at_10": 0.1,
        "top_1": 1.0,
    }
    assert report["delta"]["ndcg_at_10"] == 1.0
    assert report["significant"] is True
