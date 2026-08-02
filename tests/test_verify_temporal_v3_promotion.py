from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import verify_temporal_v3_promotion as promotion
from pipeline_contract import artifact_entry, sha256_file


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, dict[str, object]]:
    shared = {
        "engine": "tantivy v0.26.0, index_format v7",
        "jobs_sha256": "a" * 64,
        "job_row_order_sha256": "b" * 64,
        "lexical_policy_version": "temporal-v3",
        "lexical_policy_sha256": "c" * 64,
        "tokenizers": {"title": "default"},
        "source_fields": {"title": ["title"]},
        "query_corrections": {"enabled": False},
    }
    build = tmp_path / "evaluated-candidate-build-manifest.json"
    _write_json(
        build,
        {
            **shared,
            "dataset_sha256": "d" * 64,
            "rows": 2,
            "source_csv_fields": ["title"],
            "salary_filter_excluded_rows": 0,
        },
    )
    component = tmp_path / "evaluated-candidate-manifest.json"
    _write_json(
        component,
        {
            **shared,
            "schema_fields": ["title", "updated_at_epoch_ms"],
            "field_boosts": {"title": 15.0},
            "filter_semantics": "typed filters before Top-K",
            "updated_at_field": "updated_at_epoch_ms",
            "temporal_filter_semantics": "180-day lower bound",
            "build_manifest_sha256": sha256_file(build),
        },
    )
    queries = tmp_path / "canonical-queries.jsonl"
    queries.write_text(
        "".join(
            json.dumps({"qid": f"ctx:{index}"}, separators=(",", ":")) + "\n"
            for index in range(1, 340)
        ),
        encoding="utf-8",
    )
    split = tmp_path / "split-manifest.json"
    _write_json(split, {"split": "fixed-339"})
    qrels = tmp_path / "qrels.txt"
    qrels.write_text(
        "".join(f"ctx:{index}:search:{index} 0 {1000 + index} 1\n" for index in range(1, 340)),
        encoding="utf-8",
    )
    baseline_run = tmp_path / "baseline.run"
    candidate_run = tmp_path / "candidate.run"
    baseline_run.write_text(
        "".join(
            f"ctx:{index} Q0 {2000 + index} 1 2 baseline\n"
            f"ctx:{index} Q0 {1000 + index} 2 1 baseline\n"
            for index in range(1, 340)
        ),
        encoding="utf-8",
    )
    candidate_run.write_text(
        "".join(
            f"ctx:{index} Q0 {1000 + index} 1 2 candidate\n"
            f"ctx:{index} Q0 {2000 + index} 2 1 candidate\n"
            for index in range(1, 340)
        ),
        encoding="utf-8",
    )
    baseline_manifest = tmp_path / "baseline-run.manifest.json"
    _write_json(baseline_manifest, {"fixed": "baseline"})

    monkeypatch.setattr(promotion, "KNOWN_CANONICAL_QUERIES_SHA256", sha256_file(queries))
    monkeypatch.setattr(promotion, "KNOWN_EVALUATION_SPLIT_SHA256", sha256_file(split))
    monkeypatch.setattr(promotion, "KNOWN_QRELS_SHA256", sha256_file(qrels))
    monkeypatch.setattr(promotion, "KNOWN_BASELINE_RUN_SHA256", sha256_file(baseline_run))
    monkeypatch.setattr(promotion, "KNOWN_BASELINE_MANIFEST_SHA256", sha256_file(baseline_manifest))
    candidate_manifest = tmp_path / "candidate-run.manifest.json"
    source_lineage = {
        f"source:{name}": sha256_file(Path(promotion.__file__).parents[1] / name)
        for name in (
            "packages/search-core/src/work_retrieval_core/constraints.py",
            "packages/search-core/src/work_retrieval_core/adapters.py",
            "packages/search-core/src/work_retrieval_core/engine.py",
            "packages/search-core/src/work_retrieval_core/serialization.py",
            "scripts/tantivy_index_pipeline.py",
            "scripts/tantivy_graph_off_runner.py",
        )
    }
    _write_json(
        candidate_manifest,
        {
            "schema_version": 1,
            "complete": True,
            "variant": "graph_off",
            "split_manifest_sha256": sha256_file(split),
            "canonical_qids": [f"ctx:{index}" for index in range(1, 340)],
            "zero_result_qids": [],
            "run_sha256": sha256_file(candidate_run),
            "non_graph_inputs": {
                "canonical_queries": sha256_file(queries),
                "jobs_csv": promotion.KNOWN_JOBS_SHA256,
                "tantivy_component_manifest": sha256_file(component),
                **source_lineage,
            },
        },
    )
    policy_sha256, _identity = promotion.candidate_policy_fingerprint(
        candidate_manifest_path=component,
        candidate_build_manifest_path=build,
    )
    artifacts = [
        artifact_entry(tmp_path / path, relative_to=tmp_path, kind="evidence")
        for path in promotion.EVIDENCE_PATHS.values()
    ]
    baseline_ndcg = 1.0 / math.log2(3)
    metrics = {
        "ndcg_at_10": {
            "baseline": baseline_ndcg,
            "candidate": 1.0,
            "delta": 1.0 - baseline_ndcg,
        },
        "precision_at_10": {"baseline": 0.1, "candidate": 0.1, "delta": 0.0},
        "top_1": {"baseline": 0.0, "candidate": 1.0, "delta": 1.0},
        "mrr": {"baseline": 0.5, "candidate": 1.0, "delta": 0.5},
    }
    attestation: dict[str, object] = {
        "schema_version": 1,
        "complete": True,
        "attestation_kind": "fixed-339-temporal-v3-promotion",
        "experiment": "fixed-339-temporal-v3-typed-constraint-ablation",
        "promotion_allowed": True,
        "official_score_claimed": False,
        "evaluator_id": "fixed-339-evaluator-v1",
        "query_count": 339,
        "candidate_policy_sha256": policy_sha256,
        "evaluated_candidate_manifest_sha256": sha256_file(component),
        "canonical_queries_sha256": sha256_file(queries),
        "evaluation_split_sha256": sha256_file(split),
        "qrels_sha256": sha256_file(qrels),
        "baseline_run_sha256": sha256_file(baseline_run),
        "candidate_run_sha256": sha256_file(candidate_run),
        "metrics": metrics,
        "coverage": {
            "baseline": {"zero_result_contexts": 0, "underfilled_top_10_contexts": 339},
            "candidate": {"zero_result_contexts": 0, "underfilled_top_10_contexts": 339},
        },
        "artifacts": artifacts,
    }
    attestation_path = tmp_path / "attestation.json"
    _write_json(attestation_path, attestation)
    return component, build, attestation_path, attestation


def test_fixed_339_attestation_recomputes_positive_non_regressing_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    component, build, attestation_path, _attestation = _write_evidence(tmp_path, monkeypatch)

    result = promotion.verify_attestation(
        attestation_path=attestation_path,
        approved_attestation_sha256=sha256_file(attestation_path),
        candidate_manifest_path=component,
        candidate_build_manifest_path=build,
    )

    assert result["passed"] is True
    assert result["query_count"] == 339


def test_create_attestation_seals_exact_evidence_and_rejects_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    component, build, _attestation_path, _attestation = _write_evidence(tmp_path, monkeypatch)
    output = tmp_path / "sealed"

    result = promotion.create_attestation(
        output=output,
        canonical_queries_path=tmp_path / "canonical-queries.jsonl",
        evaluation_split_path=tmp_path / "split-manifest.json",
        qrels_path=tmp_path / "qrels.txt",
        baseline_run_path=tmp_path / "baseline.run",
        baseline_run_manifest_path=tmp_path / "baseline-run.manifest.json",
        candidate_run_path=tmp_path / "candidate.run",
        candidate_run_manifest_path=tmp_path / "candidate-run.manifest.json",
        candidate_manifest_path=component,
        candidate_build_manifest_path=build,
    )

    assert result["passed"] is True
    assert (output / "attestation.json").is_file()
    with pytest.raises(RuntimeError, match="already exists"):
        promotion.create_attestation(
            output=output,
            canonical_queries_path=tmp_path / "canonical-queries.jsonl",
            evaluation_split_path=tmp_path / "split-manifest.json",
            qrels_path=tmp_path / "qrels.txt",
            baseline_run_path=tmp_path / "baseline.run",
            baseline_run_manifest_path=tmp_path / "baseline-run.manifest.json",
            candidate_run_path=tmp_path / "candidate.run",
            candidate_run_manifest_path=tmp_path / "candidate-run.manifest.json",
            candidate_manifest_path=component,
            candidate_build_manifest_path=build,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (("query_count", 338), "policy or lineage"),
        (("candidate_policy_sha256", "f" * 64), "policy or lineage"),
        (("ndcg_at_10", -0.01), "delta differs|regressed"),
        (("ndcg_at_10", 0.0), "delta differs|run bytes|positive NDCG"),
        (("mrr", -0.01), "delta differs|regressed"),
    ],
)
def test_fixed_339_attestation_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: tuple[str, object],
    message: str,
) -> None:
    component, build, attestation_path, attestation = _write_evidence(tmp_path, monkeypatch)
    field, value = mutation
    if field in {"ndcg_at_10", "mrr"}:
        metrics = attestation["metrics"]
        assert isinstance(metrics, dict)
        metric = metrics[field]
        assert isinstance(metric, dict)
        metric["candidate"] = float(metric["baseline"]) + float(value)
        metric["delta"] = value
    else:
        attestation[field] = value
    _write_json(attestation_path, attestation)

    with pytest.raises(RuntimeError, match=message):
        promotion.verify_attestation(
            attestation_path=attestation_path,
            approved_attestation_sha256=sha256_file(attestation_path),
            candidate_manifest_path=component,
            candidate_build_manifest_path=build,
        )


def test_fixed_339_attestation_rejects_tampered_candidate_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    component, build, attestation_path, _attestation = _write_evidence(tmp_path, monkeypatch)
    with (tmp_path / "candidate.run").open("a", encoding="utf-8") as target:
        target.write("ctx:1 Q0 99999 3 0 tampered\n")

    with pytest.raises(RuntimeError, match="bytes differ from inventory"):
        promotion.verify_attestation(
            attestation_path=attestation_path,
            approved_attestation_sha256=sha256_file(attestation_path),
            candidate_manifest_path=component,
            candidate_build_manifest_path=build,
        )


def test_fixed_339_attestation_requires_external_approved_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    component, build, attestation_path, _attestation = _write_evidence(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="approved SHA-256"):
        promotion.verify_attestation(
            attestation_path=attestation_path,
            approved_attestation_sha256="f" * 64,
            candidate_manifest_path=component,
            candidate_build_manifest_path=build,
        )


def test_policy_fingerprint_ignores_nondeterministic_physical_index_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    component, build, _attestation_path, _attestation = _write_evidence(tmp_path, monkeypatch)
    first, _identity = promotion.candidate_policy_fingerprint(
        candidate_manifest_path=component,
        candidate_build_manifest_path=build,
    )
    value = json.loads(component.read_text(encoding="utf-8"))
    value["index_sha256"] = "e" * 64
    _write_json(component, value)

    second, _identity = promotion.candidate_policy_fingerprint(
        candidate_manifest_path=component,
        candidate_build_manifest_path=build,
    )

    assert second == first
