from __future__ import annotations

import csv
import hashlib
import importlib
import io
import json
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import numpy as np
import pytest
from botocore.session import get_session

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import graph_ablation_runner as ablation
import llm_skill_extraction_pipeline as llm_extraction
import multiview_embedding_pipeline as embeddings
import pipeline_contract as contract
import query_correction_pipeline as query_corrections
import skill_graph_pipeline as graph
import tantivy_graph_off_runner as graph_off
import tantivy_index_pipeline as tantivy_pipeline
import whole_embedding_pipeline as whole_embeddings
from work_retrieval_core import CandidateEvidence, CandidateRequest
from work_retrieval_core.engine import MAX_AGE_DAYS
from work_retrieval_core.graph import GraphConditionedRetriever, SkillGraphIndex
from work_retrieval_core.serialization import FULL_JOB_FIELDS


def _write_json(path: Path, value: object) -> None:
    contract.atomic_json(path, value)


def _write_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    path.write_bytes(b"".join(contract.canonical_json(value) + b"\n" for value in values))


def _split_manifest(tmp_path: Path, qrels: bytes = b"qrels") -> tuple[Path, Path]:
    qrels_path = tmp_path / "qrels.txt"
    qrels_path.write_bytes(qrels)
    split_path = tmp_path / "split.json"
    _write_json(
        split_path,
        {
            "schema_version": 1,
            "split_id": "demo-2026-06-08",
            "train_cutoff_exclusive": "2026-06-08T00:00:00+08:00",
            "evaluation_start_inclusive": "2026-06-08T00:00:00+08:00",
            "evaluation_end_exclusive": "2026-06-09T00:00:00+08:00",
            "qrels_sha256": contract.sha256_file(qrels_path),
        },
    )
    return split_path, qrels_path


def _skill_evidence(tmp_path: Path) -> tuple[Path, Path, Path]:
    split_path, _ = _split_manifest(tmp_path)
    evidence_path = tmp_path / "evidence.jsonl"
    timestamps = (
        "2026-06-06T10:00:00+08:00",
        "2026-06-07T09:00:00+08:00",
        "2026-06-07T10:00:00+08:00",
    )
    records: list[dict[str, object]] = []
    for index, timestamp in enumerate(timestamps, start=1):
        source = "Pyhton 與 SQL 是此職缺必要技能。" if index < 3 else "SQL 是此職缺必要技能。"
        job_id = str(100 + index)
        source_sha256 = hashlib.sha256(source.encode()).hexdigest()
        skills = [
            {
                "canonical_name": "sql",
                "surface": "SQL",
                "category": "query language",
                "evidence_span": "SQL",
            }
        ]
        relations: list[dict[str, object]] = []
        if index < 3:
            skills.insert(
                0,
                {
                    "canonical_name": "python",
                    "surface": "Pyhton",
                    "category": ("programming language" if index == 1 else "programming languages"),
                    "evidence_span": "Pyhton",
                },
            )
            relations.append(
                {
                    "source": "python",
                    "type": "USED_WITH",
                    "target": "sql",
                    "evidence_span": "Pyhton 與 SQL",
                }
            )
        records.append(
            {
                "record_id": hashlib.sha256(f"{job_id}\0{source_sha256}".encode()).hexdigest(),
                "job_id": job_id,
                "duty": "data engineer",
                "source_modified_at": timestamp,
                "source_text": source,
                "source_text_sha256": source_sha256,
                "skills": skills,
                "relations": relations,
                "skill_rejection_count": 0,
                "relation_rejection_count": 0,
            }
        )
    _write_jsonl(evidence_path, records)
    manifest_path = tmp_path / "extraction-manifest.json"
    _write_json(
        manifest_path,
        {
            "schema_version": 1,
            "complete": True,
            "model_id": "pinned-llm@revision",
            "prompt_version": "skill-extraction-v1",
            "prompt_sha256": "1" * 64,
            "canonicalization_policy": "open_surface_per_jd_llm_canonicalization_v1",
            "oov_policy": "accept_open_surface_with_exact_train_jd_evidence",
            "source_policy": "train_jd_only",
            "test_jd_used": False,
            "uses_ground_truth": False,
            "uses_behavior_logs": False,
            "train_cutoff_exclusive": "2026-06-08T00:00:00+08:00",
            "max_source_timestamp": timestamps[-1],
            "source_jd_sha256": "a" * 64,
            "requests_sha256": "2" * 64,
            "responses_inventory_sha256": "3" * 64,
            "evidence_sha256": contract.sha256_file(evidence_path),
            "sampling_policy": "duty_stratified_sqrt_support_stable_hash_v1",
            "sample_limit": len(records),
            "eligible_train_records": len(records),
            "sampling_sha256": hashlib.sha256(
                contract.canonical_json([record["record_id"] for record in records])
            ).hexdigest(),
            "source_records": len(records),
            "processed_records": len(records),
            "input_tokens": 10,
            "output_tokens": 10,
            "skill_rejections": 0,
            "relation_rejections": 0,
        },
    )
    return evidence_path, manifest_path, split_path


def _graph_search_jobs_csv(tmp_path: Path) -> Path:
    path = tmp_path / "graph-search-jobs.csv"
    extra = (
        whole_embeddings.JOB_ID_FIELD,
        tantivy_pipeline.DEFAULT_LOCATION_CODE_FIELD,
        tantivy_pipeline.DEFAULT_DUTY_CODE_FIELD,
        tantivy_pipeline.DEFAULT_VISIBILITY_FIELD,
        tantivy_pipeline.DEFAULT_MODIFIED_AT_FIELD,
        tantivy_pipeline.SALARY_LOWER_SOURCE_FIELD,
        tantivy_pipeline.SALARY_UPPER_SOURCE_FIELD,
    )
    fields = list(dict.fromkeys([*(label for label, _field in FULL_JOB_FIELDS), *extra]))
    rows = (
        ("101", "Pyhton", "台北市", "100100", "1", "2026-06-06T10:00:00+08:00"),
        ("102", "Pyhton", "台北市", "100100", "1", "2026-06-07T10:00:00+08:00"),
        ("103", "SQL", "台北市", "100100", "1", "2026-06-07T11:00:00+08:00"),
        ("104", "SQL", "台北市", "100100", "1", "2026-06-07T12:00:00+08:00"),
        ("105", "SQL", "高雄市", "200200", "1", "2026-06-07T12:00:00+08:00"),
        ("106", "SQL", "台北市", "100100", "1", "2025-01-01T12:00:00+08:00"),
        ("107", "SQL", "台北市", "100100", "0", "2026-06-07T12:00:00+08:00"),
    )
    with path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        for job_id, skills, city, city_code, visible, timestamp in rows:
            row = dict.fromkeys(fields, "")
            row.update(
                {
                    whole_embeddings.JOB_ID_FIELD: job_id,
                    "職務名稱": f"{skills} 資料工程師",
                    "職務小類": "資料工程師",
                    "職務中類": "資訊軟體",
                    "職務大類": "資訊科技",
                    "電腦技能資料": skills,
                    "薪資": "月薪",
                    "薪資下限": "40000",
                    "薪資上限": "60000",
                    "學歷需求": "不拘",
                    "職缺屬性": "全職",
                    "工時": "日班",
                    "工作經驗需求": "不拘",
                    "管理人數": "需管理人數10人以下",
                    "工作城市": city,
                    "職務內容": f"使用 {skills} 建立資料平台",
                    tantivy_pipeline.DEFAULT_LOCATION_CODE_FIELD: city_code,
                    tantivy_pipeline.DEFAULT_DUTY_CODE_FIELD: "140200",
                    tantivy_pipeline.DEFAULT_VISIBILITY_FIELD: visible,
                    tantivy_pipeline.DEFAULT_MODIFIED_AT_FIELD: timestamp,
                }
            )
            writer.writerow(row)
    return path


def _graph_candidate_fixture(tmp_path: Path) -> dict[str, Path]:
    candidates = importlib.import_module("graph_candidate_runner")
    evidence, extraction_manifest, split_manifest = _skill_evidence(tmp_path)
    graph_output = tmp_path / "graph"
    graph.build_graph(
        evidence_path=evidence,
        extraction_manifest_path=extraction_manifest,
        split_manifest_path=split_manifest,
        output=graph_output,
        minimum_support=2,
    )
    jobs_csv = _graph_search_jobs_csv(tmp_path)
    tantivy_output = tmp_path / "tantivy"
    tantivy_pipeline.build_tantivy(
        jobs_csv=jobs_csv,
        output=tantivy_output,
        artifact_prefix=tantivy_pipeline.DEFAULT_ARTIFACT_PREFIX,
        location_code_field=tantivy_pipeline.DEFAULT_LOCATION_CODE_FIELD,
        location_term_field=tantivy_pipeline.DEFAULT_LOCATION_TERM_FIELD,
        duty_code_field=tantivy_pipeline.DEFAULT_DUTY_CODE_FIELD,
        duty_term_field=tantivy_pipeline.DEFAULT_DUTY_TERM_FIELD,
        visibility_field=tantivy_pipeline.DEFAULT_VISIBILITY_FIELD,
        modified_at_field=tantivy_pipeline.DEFAULT_MODIFIED_AT_FIELD,
        correction_candidate_path=None,
        correction_attestation_path=None,
    )
    queries = tmp_path / "queries.jsonl"
    _write_jsonl(
        queries,
        [
            {
                "qid": "q1",
                "query": "Pyhton",
                "as_of": "2026-06-08T23:59:59.999+08:00",
                "location_codes": ["100100"],
                "duty_codes": ["140200"],
            },
            {
                "qid": "q2",
                "query": "Python",
                "as_of": "2026-06-08T23:59:59.999+08:00",
                "location_codes": ["100100"],
                "duty_codes": ["140200"],
            },
        ],
    )
    baseline = tmp_path / "baseline"
    graph_off.generate_graph_off(
        split_manifest_path=split_manifest,
        queries_path=queries,
        tantivy_output=tantivy_output,
        jobs_csv=jobs_csv,
        artifact_prefix=tantivy_pipeline.DEFAULT_ARTIFACT_PREFIX,
        output=baseline,
    )
    generated = tmp_path / "generated"
    candidates.generate_graph_on(
        split_manifest_path=split_manifest,
        graph_output=graph_output,
        evidence_path=evidence,
        extraction_manifest_path=extraction_manifest,
        graph_off_run=baseline / "graph-off.run",
        graph_off_manifest=baseline / "graph-off.manifest.json",
        queries_path=queries,
        jobs_csv=jobs_csv,
        tantivy_output=tantivy_output,
        artifact_prefix=tantivy_pipeline.DEFAULT_ARTIFACT_PREFIX,
        output=generated,
    )
    return {
        "split": split_manifest,
        "evidence": evidence,
        "extraction_manifest": extraction_manifest,
        "qrels": tmp_path / "qrels.txt",
        "graph": graph_output,
        "jobs": jobs_csv,
        "tantivy": tantivy_output,
        "queries": queries,
        "baseline": baseline,
        "generated": generated,
    }


def test_offline_and_production_graph_ranking_have_golden_parity(tmp_path: Path) -> None:
    candidates = importlib.import_module("graph_candidate_runner")
    edge: dict[str, object] = {
        "job_id": "20",
        "skill": "javascript",
        "surface": "Node",
        "evidence_span": "Node",
    }
    relation: dict[str, object] = {
        "source": "javascript",
        "type": "USED_WITH",
        "target": "sql",
        "support": 2,
        "weight": 0.9,
    }
    bridge_rows = (
        CandidateEvidence("9", 8.0, 1),
        CandidateEvidence("20", 7.0, 2),
    )
    as_of = datetime(2026, 6, 8, tzinfo=UTC)
    query = graph_off.CanonicalQuery("q1", "Node.js engineer", as_of, (), ())
    baseline = [candidates.RunRow("q1", str(rank), rank, float(11 - rank)) for rank in range(1, 11)]
    indexes = candidates.GraphIndexes(
        skills_by_job={"20": [edge]},
        aliases={"javascript": [edge], "Node": [edge]},
        skills_by_duty={},
        relations_by_skill={"javascript": [relation], "sql": [relation]},
        evidence_by_relation={("javascript", "USED_WITH", "sql"): []},
    )

    class OfflineBridge:
        def duty_terms(self, codes: tuple[str, ...]) -> tuple[str, ...]:
            return ()

        def retrieve(
            self,
            canonical_query: graph_off.CanonicalQuery,
            bridge_term: str,
            *,
            limit: int,
        ) -> tuple[CandidateEvidence, ...]:
            assert bridge_term == "sql" and limit == 50
            return bridge_rows

    offline_ids = [
        job_id
        for job_id, _score in candidates._query_candidates(
            baseline, indexes, query, OfflineBridge()
        )[0][:10]
    ]
    job_skills = tmp_path / "job-skills.jsonl"
    duty_skills = tmp_path / "duty-skills.jsonl"
    relations = tmp_path / "skill-relations.jsonl"
    _write_jsonl(job_skills, [edge])
    _write_jsonl(duty_skills, [])
    _write_jsonl(relations, [relation])

    class ProductionBaseline:
        def retrieve(
            self, request: CandidateRequest, *, limit: int
        ) -> tuple[CandidateEvidence, ...]:
            if request.lexical_texts == ("sql",):
                return bridge_rows
            return tuple(
                CandidateEvidence(str(rank), float(11 - rank), rank) for rank in range(1, 11)
            )

        def close(self) -> None:
            pass

    production = GraphConditionedRetriever(
        ProductionBaseline(),
        SkillGraphIndex.from_paths(job_skills, duty_skills, relations),
        duty_terms=lambda codes: (),
    )
    production_ids = [
        row.job_id
        for row in production.retrieve(
            CandidateRequest(
                query.query,
                (),
                (),
                as_of,
                as_of - timedelta(days=MAX_AGE_DAYS),
                (query.query,),
            ),
            limit=10,
        )
    ]

    expected = ["1", "2", "3", "9", "20", "4", "5", "6", "7", "8"]
    assert offline_ids == production_ids == expected


def test_skill_graph_build_validate_and_trace(tmp_path: Path) -> None:
    evidence, extraction_manifest, split_manifest = _skill_evidence(tmp_path)
    output = tmp_path / "graph"

    manifest = graph.build_graph(
        evidence_path=evidence,
        extraction_manifest_path=extraction_manifest,
        split_manifest_path=split_manifest,
        output=output,
        minimum_support=2,
    )
    validation = graph.validate_graph(
        output,
        split_manifest,
        evidence_path=evidence,
        extraction_manifest_path=extraction_manifest,
    )
    trace = graph.trace_skill(output, split_manifest, evidence, extraction_manifest, "Python", 10)

    assert manifest["graph_kind"] == "llm-evidence-locked-typed-entity-graph"
    assert manifest["category_resolution_policy"] == "support_majority_then_lexical_v1"
    assert manifest["category_conflict_skills"] == 1
    assert manifest["category_mentions_in_conflict_sets"] == 2
    skills = {
        row["skill"]: row["category"]
        for row in graph._read_jsonl(output / graph.GRAPH_FILES["skills"], "skills")
    }
    assert skills["python"] == "programming language"
    assert validation["passed"] is True
    assert trace["jobs"] == [
        {"job_id": "101", "evidence_span": "Pyhton"},
        {"job_id": "102", "evidence_span": "Pyhton"},
    ]
    assert trace["relations"][0]["target"] == "sql"
    assert trace["relations"][0]["evidence"] == [
        {"job_id": "101", "evidence_span": "Pyhton 與 SQL"},
        {"job_id": "102", "evidence_span": "Pyhton 與 SQL"},
    ]
    assert trace["paths"] == [
        {
            "anchor_skill": "python",
            "edge": {
                "source": "python",
                "type": "USED_WITH",
                "target": "sql",
                "support": 2,
                "weight": pytest.approx(2 / (2 * 3) ** 0.5),
            },
            "related_skill": "sql",
            "related_jobs": [
                {"job_id": "101", "surface": "sql", "evidence_span": "SQL"},
                {"job_id": "102", "surface": "sql", "evidence_span": "SQL"},
                {"job_id": "103", "surface": "sql", "evidence_span": "SQL"},
            ],
            "relation_evidence": [
                {"job_id": "101", "evidence_span": "Pyhton 與 SQL"},
                {"job_id": "102", "evidence_span": "Pyhton 與 SQL"},
            ],
        }
    ]


def _refresh_graph_artifact(output: Path, filename: str) -> None:
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact = next(item for item in manifest["artifacts"] if item["path"] == filename)
    artifact_path = output / filename
    artifact["sha256"] = contract.sha256_file(artifact_path)
    artifact["size_bytes"] = artifact_path.stat().st_size
    manifest_path.unlink()
    _write_json(manifest_path, manifest)


def test_skill_graph_validation_rejects_nonfinite_weight(tmp_path: Path) -> None:
    evidence, extraction_manifest, split_manifest = _skill_evidence(tmp_path)
    output = tmp_path / "graph"
    graph.build_graph(
        evidence_path=evidence,
        extraction_manifest_path=extraction_manifest,
        split_manifest_path=split_manifest,
        output=output,
        minimum_support=2,
    )
    relations_path = output / graph.GRAPH_FILES["skill_relations"]
    relation = json.loads(relations_path.read_text(encoding="utf-8"))
    relation["weight"] = float("nan")
    relations_path.write_text(json.dumps(relation) + "\n", encoding="utf-8")
    _refresh_graph_artifact(output, graph.GRAPH_FILES["skill_relations"])

    with pytest.raises(RuntimeError, match="cannot be read"):
        graph.validate_graph(
            output,
            split_manifest,
            evidence_path=evidence,
            extraction_manifest_path=extraction_manifest,
        )


def test_skill_graph_validation_recomputes_relation_aggregates(tmp_path: Path) -> None:
    evidence, extraction_manifest, split_manifest = _skill_evidence(tmp_path)
    output = tmp_path / "graph"
    graph.build_graph(
        evidence_path=evidence,
        extraction_manifest_path=extraction_manifest,
        split_manifest_path=split_manifest,
        output=output,
        minimum_support=2,
    )
    relations_path = output / graph.GRAPH_FILES["skill_relations"]
    relation = json.loads(relations_path.read_text(encoding="utf-8"))
    relation["support"] = 3
    _write_jsonl(relations_path, [relation])
    _refresh_graph_artifact(output, graph.GRAPH_FILES["skill_relations"])

    with pytest.raises(RuntimeError, match="pinned LLM extraction evidence"):
        graph.validate_graph(
            output,
            split_manifest,
            evidence_path=evidence,
            extraction_manifest_path=extraction_manifest,
        )


def test_skill_graph_validation_rejects_forged_relation_evidence(tmp_path: Path) -> None:
    evidence, extraction_manifest, split_manifest = _skill_evidence(tmp_path)
    output = tmp_path / "graph"
    graph.build_graph(
        evidence_path=evidence,
        extraction_manifest_path=extraction_manifest,
        split_manifest_path=split_manifest,
        output=output,
        minimum_support=2,
    )
    for filename in ("skill-relations.jsonl", "relation-evidence.jsonl"):
        path = output / filename
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        for row in rows:
            row["type"] = "RELATED_TO"
            if filename == "relation-evidence.jsonl":
                row["evidence_span"] = "fabricated span not in source JD"
        _write_jsonl(path, rows)
        _refresh_graph_artifact(output, filename)

    with pytest.raises(RuntimeError, match="pinned LLM extraction evidence"):
        graph.validate_graph(
            output,
            split_manifest,
            evidence_path=evidence,
            extraction_manifest_path=extraction_manifest,
        )


def test_graph_on_is_generated_from_graph_off_and_frozen_graph(tmp_path: Path) -> None:
    candidates = importlib.import_module("graph_candidate_runner")
    fixture = _graph_candidate_fixture(tmp_path)
    output = fixture["generated"]
    manifest = json.loads((output / "graph-on.manifest.json").read_text(encoding="utf-8"))
    run_rows = (output / "graph-on.run").read_text(encoding="utf-8").splitlines()
    trace_rows = [
        json.loads(line)
        for line in (output / "graph-traces.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    output_job_ids = [line.split()[2] for line in run_rows]
    scores_by_qid: dict[str, list[float]] = defaultdict(list)
    for line in run_rows:
        fields = line.split()
        scores_by_qid[fields[0]].append(float(fields[4]))
    graph_job_ids = {
        json.loads(line)["job_id"]
        for line in (fixture["graph"] / graph.GRAPH_FILES["jobs"])
        .read_text(encoding="utf-8")
        .splitlines()
    }
    assert {"101", "102"}.issubset(output_job_ids)
    assert "104" in output_job_ids
    assert "104" not in graph_job_ids
    assert not {"105", "106", "107"}.intersection(output_job_ids)
    assert all(
        left > right for scores in scores_by_qid.values() for left, right in pairwise(scores)
    )
    assert manifest["generation"]["parameters"] == candidates.PARAMETERS
    assert manifest["generation"]["graph_off_run_sha256"] == contract.sha256_file(
        fixture["baseline"] / "graph-off.run"
    )
    assert manifest["generation"]["statistics"]["queries_with_query_exact_anchors"] == 2
    assert manifest["generation"]["statistics"]["queries_with_consensus_anchors"] == 1
    assert manifest["canonical_qids"] == ["q1", "q2"]
    assert manifest["zero_result_qids"] == []
    assert manifest["generation"]["statistics"]["novel_graph_candidate_rows"] >= 1
    job_104_trace = next(
        row for row in trace_rows if row["qid"] == "q2" and row["candidate_job_id"] == "104"
    )
    assert job_104_trace["baseline_rank"] is None
    python_path = next(path for path in job_104_trace["paths"] if path["anchor_skill"] == "python")
    assert python_path["anchor_source"] == "query_exact"
    assert python_path["anchor_seed_job_ids"] == []
    assert python_path["anchor_evidence"][0]["matched_alias"] == "python"
    assert job_104_trace["path_count_total"] == (
        job_104_trace["path_count_retained"] + job_104_trace["path_count_omitted"]
    )
    assert job_104_trace["graph_evidence_score"] == pytest.approx(
        job_104_trace["retained_graph_evidence_score"]
        + job_104_trace["omitted_graph_evidence_score"]
    )
    assert python_path["candidate_evidence"]["retriever"] == "temporal_v2_tantivy"
    assert python_path["candidate_evidence"]["bridge_query"] == "sql"
    assert "evidence_span" not in python_path["candidate_evidence"]


@pytest.mark.parametrize(
    "line, message",
    [
        ("bad/qid Q0 101 1 1 baseline\n", "qid"),
        ("q1 Q0 job-101 1 1 baseline\n", "job_id"),
        ("q1 Q0 0101 1 1 baseline\n", "job_id"),
    ],
)
def test_graph_candidate_generation_rejects_unsafe_ids(
    tmp_path: Path, line: str, message: str
) -> None:
    candidates = importlib.import_module("graph_candidate_runner")
    run = tmp_path / "run"
    run.write_text(line, encoding="utf-8")
    with pytest.raises(RuntimeError, match=message):
        candidates.read_trec_run(run)


def test_graph_candidate_generation_rejects_a_larger_off_universe(tmp_path: Path) -> None:
    candidates = importlib.import_module("graph_candidate_runner")
    run = tmp_path / "run"
    maximum = int(candidates.PARAMETERS["maximum_results"])
    run.write_text(
        "".join(
            f"q1 Q0 {job_id} {rank} {maximum + 2 - rank} baseline\n"
            for rank, job_id in enumerate(range(1, maximum + 2), start=1)
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="maximum_results"):
        candidates.read_trec_run(run)


def test_graph_candidate_protected_order_never_exceeds_novel_top_ten_cap() -> None:
    candidates = importlib.import_module("graph_candidate_runner")
    baseline_ranks = {"101": 1, "102": 2}
    ordered = ["201", "202", "203", "204", "205", "101", "102"]

    ranked = candidates._protected_order(ordered, baseline_ranks)

    assert sum(job_id not in baseline_ranks for job_id in ranked[:10]) == 5
    assert set(ranked) == set(ordered)


def test_graph_candidate_top_ten_replaces_at_most_two_when_baseline_is_full() -> None:
    candidates = importlib.import_module("graph_candidate_runner")
    baseline_ranks = {str(job_id): job_id for job_id in range(1, 11)}
    ordered = ["201", "202", "203", *baseline_ranks]

    ranked = candidates._protected_order(ordered, baseline_ranks)

    assert sum(job_id not in baseline_ranks for job_id in ranked[:10]) == 2
    assert set(ranked) == set(ordered)


def test_graph_ablation_wrapper_generates_before_evaluation() -> None:
    script = Path(__file__).parents[1] / "scripts" / "run_graph_ablation.sh"
    check = subprocess.run(["bash", "-n", str(script)], check=False, capture_output=True, text=True)

    assert check.returncode == 0, check.stderr
    source = script.read_text(encoding="utf-8")
    assert (
        source.index("tantivy_graph_off_runner.py")
        < source.index("graph_candidate_runner.py")
        < source.index("graph_ablation_runner.py")
    )
    assert source.count('--queries "$6"') == 3
    assert source.count('--jobs-csv "$7"') == 3
    assert source.count('--tantivy-output "$8"') == 3
    assert source.count('--evidence "$3"') == 2
    assert source.count('--extraction-manifest "$4"') == 2
    assert "evaluate_trec_runs.py" in source
    assert "GRAPH_EVALUATOR_KIND:-train_semantic_proxy" in source
    assert "OFF_RUN" not in source


def test_skill_graph_rejects_test_period_jd(tmp_path: Path) -> None:
    evidence, extraction_manifest, split_manifest = _skill_evidence(tmp_path)
    manifest = json.loads(extraction_manifest.read_text(encoding="utf-8"))
    manifest["max_source_timestamp"] = "2026-06-08T00:00:00+08:00"
    _write_json(extraction_manifest, manifest)

    with pytest.raises(RuntimeError, match="test-period JD"):
        graph.build_graph(
            evidence_path=evidence,
            extraction_manifest_path=extraction_manifest,
            split_manifest_path=split_manifest,
            output=tmp_path / "graph",
            minimum_support=1,
        )


class FakeEncoder:
    def __init__(self) -> None:
        self.closed = False

    def encode(self, texts: list[str]) -> np.ndarray:
        matrix = np.zeros((len(texts), embeddings.OUTPUT_DIMENSION), dtype=np.float32)
        for row, text in enumerate(texts):
            matrix[row, row % 2] = float(len(text))
        return embeddings._normalize_prefix(matrix)

    def close(self) -> None:
        self.closed = True


class FakeTokenizer:
    def encode(self, text: str) -> list[int]:
        return list(range(len(text)))


def test_multiview_records_are_built_from_full_jd_fields(tmp_path: Path) -> None:
    source = tmp_path / "jobs.csv"
    fields = [
        embeddings.JOB_ID_FIELD,
        embeddings.CONTENT_FIELD,
        *(field for values in embeddings.VIEW_FIELDS.values() for field in values),
    ]
    with source.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        writer.writerow(
            {
                embeddings.JOB_ID_FIELD: "101",
                "職務名稱": "資料工程師",
                "職務小類": "資料工程師",
                "電腦技能資料": "Python SQL",
                "工作經驗需求": "一年",
                embeddings.CONTENT_FIELD: "甲" * 400,
            }
        )

    output = tmp_path / "records"
    manifest = embeddings.build_records(
        jobs_csv=source,
        output=output,
        tokenizer=FakeTokenizer(),
        tokenizer_sha256="a" * 64,
    )
    records = [
        json.loads(line)
        for line in (output / "records.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert manifest["document_policy_version"] == embeddings.DOCUMENT_POLICY_VERSION
    assert {record["kind"] for record in records} == set(embeddings.VIEW_KINDS)
    assert [record["view_index"] for record in records if record["kind"] == "content"] == [0, 1]
    assert all(len(FakeTokenizer().encode(record["text"])) <= 384 for record in records)


def _multiview_input(tmp_path: Path) -> tuple[Path, Path]:
    records_path = tmp_path / "views.jsonl"
    records = [
        {
            "job_id": "101",
            "job_row": 0,
            "kind": kind,
            "view_index": 0,
            "text": f"101 {kind}",
        }
        for kind in embeddings.VIEW_KINDS
    ]
    _write_jsonl(records_path, records)
    jobs_path = tmp_path / "jobs.jsonl"
    _write_jsonl(jobs_path, [{"job_id": "101", "job_row": 0}])
    order_sha256 = hashlib.sha256(b"101\n").hexdigest()
    manifest_path = tmp_path / "views-manifest.json"
    _write_json(
        manifest_path,
        {
            "schema_version": 1,
            "complete": True,
            "model": embeddings.MODEL,
            "revision": embeddings.MODEL_REVISION,
            "tokenizer_sha256": "0" * 64,
            "dataset_sha256": "a" * 64,
            "jobs_path": jobs_path.name,
            "jobs_sha256": contract.sha256_file(jobs_path),
            "job_row_order_sha256": order_sha256,
            "document_policy_version": embeddings.DOCUMENT_POLICY_VERSION,
            "view_policy_version": embeddings.VIEW_POLICY_VERSION,
            "view_kinds": list(embeddings.VIEW_KINDS),
            "content_min_tokens": embeddings.MIN_CONTENT_TOKENS,
            "content_max_tokens": embeddings.MODEL_MAX_LENGTH,
            "records": len(records),
            "records_sha256": contract.sha256_file(records_path),
        },
    )
    return records_path, manifest_path


def _promotion(tmp_path: Path, candidate_sha256: str, delta: float) -> tuple[Path, str]:
    promotion_path = tmp_path / "promotion.json"
    _write_json(
        promotion_path,
        {
            "schema_version": 1,
            "complete": True,
            "experiment": "Qwen3 multi-view retrieval ablation",
            "candidate_dimension": embeddings.OUTPUT_DIMENSION,
            "baseline_dimension": embeddings.OUTPUT_DIMENSION,
            "primary_metric": "ndcg_at_10",
            "absolute_delta": delta,
            "evaluator_kind": "organizer",
            "significant": True,
            "candidate_manifest_sha256": candidate_sha256,
            "evaluation_split_sha256": "d" * 64,
            "baseline_run_sha256": "e" * 64,
            "candidate_run_sha256": "f" * 64,
        },
    )
    return promotion_path, contract.sha256_file(promotion_path)


def test_multiview_embedding_build_is_pending_then_approved(
    tmp_path: Path,
) -> None:
    records, input_manifest = _multiview_input(tmp_path)
    output = tmp_path / "embeddings"
    encoder = FakeEncoder()

    manifest = embeddings.build_embeddings(
        records_path=records,
        input_manifest_path=input_manifest,
        output=output,
        encoder=encoder,
        encoder_backend="test",
        shard_size=3,
        batch_size=2,
    )

    assert encoder.closed is True
    assert manifest["publication_allowed"] is False
    assert len(manifest["shards"]) == 2
    assert embeddings.verify_embeddings(output)["records"] == 4
    candidate_sha256 = contract.sha256_file(output / "manifest.json")
    promotion, promotion_sha256 = _promotion(tmp_path, candidate_sha256, 0.001)
    attestation = embeddings.approve_embeddings(
        output=output,
        promotion_report_path=promotion,
        promotion_report_sha256=promotion_sha256,
        attestation_path=tmp_path / "attestation.json",
    )
    assert attestation["publication_allowed"] is True

    (output / "embeddings-00000.f16.npy").write_bytes(b"corrupt")
    with pytest.raises(RuntimeError, match="bytes differ"):
        embeddings.verify_embeddings(output)


def test_multiview_embedding_rejects_non_positive_ablation(tmp_path: Path) -> None:
    records, input_manifest = _multiview_input(tmp_path)
    encoder = FakeEncoder()
    output = tmp_path / "embeddings"
    embeddings.build_embeddings(
        records_path=records,
        input_manifest_path=input_manifest,
        output=output,
        encoder=encoder,
        encoder_backend="test",
        shard_size=3,
        batch_size=2,
    )
    promotion, promotion_sha256 = _promotion(
        tmp_path, contract.sha256_file(output / "manifest.json"), 0.0
    )
    with pytest.raises(RuntimeError, match="did not pass"):
        embeddings.approve_embeddings(
            promotion_report_path=promotion,
            promotion_report_sha256=promotion_sha256,
            output=output,
            attestation_path=tmp_path / "attestation.json",
        )
    assert encoder.closed is True


@pytest.mark.parametrize("value", ["cuda:0", "cuda:0,cuda:0", "cpu,cuda:1"])
def test_dual_gpu_contract_rejects_ambiguous_devices(value: str) -> None:
    with pytest.raises(ValueError, match="at least two unique explicit devices"):
        embeddings.parse_devices(value)


class FakeS3:
    def __init__(self, values: dict[str, bytes]) -> None:
        self.values = values
        self.puts: list[str] = []

    def get_object(self, **kwargs: object) -> dict[str, object]:
        body = self.values[str(kwargs["Key"])]
        return {"ContentLength": len(body), "Body": io.BytesIO(body)}

    def put_object(self, **kwargs: object) -> dict[str, object]:
        key = str(kwargs["Key"])
        body = kwargs["Body"]
        assert hasattr(body, "read")
        self.values[key] = body.read()  # type: ignore[union-attr]
        self.puts.append(key)
        return {}


def test_s3_inventory_verifies_object_bytes() -> None:
    payload = b"immutable"
    artifact = {
        "path": "artifact.bin",
        "kind": "embedding",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }
    contract.verify_s3_inventory(
        bucket="bucket",
        prefix="immutable/revision",
        expected_owner="378849533305",
        artifacts=[artifact],
        s3=FakeS3({"immutable/revision/artifact.bin": payload}),
    )

    with pytest.raises(RuntimeError, match="SHA-256 differs"):
        contract.verify_s3_inventory(
            bucket="bucket",
            prefix="immutable/revision",
            expected_owner="378849533305",
            artifacts=[artifact],
            s3=FakeS3({"immutable/revision/artifact.bin": b"immutablf"}),
        )


def test_s3_publication_is_content_addressed_and_manifest_last(tmp_path: Path) -> None:
    artifact_path = tmp_path / "artifact.bin"
    artifact_path.write_bytes(b"immutable")
    artifact = contract.artifact_entry(artifact_path, relative_to=tmp_path, kind="embedding")
    _write_json(tmp_path / "manifest.json", {"artifacts": [artifact]})
    manifest_sha256 = contract.sha256_file(tmp_path / "manifest.json")
    prefix = f"experiments/multiview/{manifest_sha256}"
    s3 = FakeS3({})

    result = contract.publish_s3_directory(
        root=tmp_path,
        bucket="bucket",
        prefix=prefix,
        expected_owner="378849533305",
        artifacts=[artifact],
        s3=s3,
    )

    assert result["manifest_sha256"] == manifest_sha256
    assert s3.puts == [f"{prefix}/artifact.bin", f"{prefix}/manifest.json"]


def test_fixed_input_graph_ablation_uses_external_evaluator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _graph_candidate_fixture(tmp_path)
    split_manifest = fixture["split"]
    graph_output = fixture["graph"]
    qrels = fixture["qrels"]
    generated = fixture["generated"]
    run_paths = {"graph_off": fixture["baseline"] / "graph-off.run"}
    manifest_paths = {"graph_off": fixture["baseline"] / "graph-off.manifest.json"}
    run_paths["graph_on"] = generated / "graph-on.run"
    manifest_paths["graph_on"] = generated / "graph-on.manifest.json"

    evaluation = {
        "query_count": 1,
        "graph_off": {
            "ndcg_at_10": 0.10,
            "precision_at_10": 0.10,
            "top_1": 0.10,
            "mrr": 0.10,
        },
        "graph_on": {
            "ndcg_at_10": 0.11,
            "precision_at_10": 0.10,
            "top_1": 0.10,
            "mrr": 0.10,
        },
        "delta": {
            "ndcg_at_10": 0.01,
            "precision_at_10": 0.0,
            "top_1": 0.0,
            "mrr": 0.0,
        },
        "significant": True,
    }
    monkeypatch.setattr(
        ablation,
        "_evaluate",
        lambda *args, **kwargs: evaluation,
    )
    with pytest.raises(RuntimeError, match="query coverage"):
        ablation.run_ablation(
            split_manifest_path=split_manifest,
            graph_output=graph_output,
            evidence_path=fixture["evidence"],
            extraction_manifest_path=fixture["extraction_manifest"],
            qrels_path=qrels,
            queries_path=fixture["queries"],
            jobs_csv=fixture["jobs"],
            tantivy_output=fixture["tantivy"],
            artifact_prefix=tantivy_pipeline.DEFAULT_ARTIFACT_PREFIX,
            graph_off_run=run_paths["graph_off"],
            graph_off_manifest=manifest_paths["graph_off"],
            graph_on_run=run_paths["graph_on"],
            graph_on_manifest=manifest_paths["graph_on"],
            evaluator_command=["organizer-evaluator"],
            evaluator_id="organizer-v1",
            evaluator_kind="organizer",
            minimum_ndcg_delta=0.005,
            output=tmp_path / "wrong-query-count.json",
        )
    evaluation["query_count"] = 2
    report = ablation.run_ablation(
        split_manifest_path=split_manifest,
        graph_output=graph_output,
        evidence_path=fixture["evidence"],
        extraction_manifest_path=fixture["extraction_manifest"],
        qrels_path=qrels,
        queries_path=fixture["queries"],
        jobs_csv=fixture["jobs"],
        tantivy_output=fixture["tantivy"],
        artifact_prefix=tantivy_pipeline.DEFAULT_ARTIFACT_PREFIX,
        graph_off_run=run_paths["graph_off"],
        graph_off_manifest=manifest_paths["graph_off"],
        graph_on_run=run_paths["graph_on"],
        graph_on_manifest=manifest_paths["graph_on"],
        evaluator_command=["organizer-evaluator"],
        evaluator_id="organizer-v1",
        evaluator_kind="organizer",
        minimum_ndcg_delta=0.005,
        output=tmp_path / "ablation.json",
    )

    assert report["metric_gate_passed"] is True
    assert report["promotion_allowed"] is False
    assert report["publication_allowed"] is False
    assert report["official_score_claimed"] is False
    assert all(not control["enabled"] for control in report["controls"].values())  # type: ignore[union-attr]
    report_sha256 = contract.sha256_file(tmp_path / "ablation.json")
    candidate_manifest = contract.read_json_object(
        graph_output / "manifest.json", "Graph candidate"
    )
    monkeypatch.setattr(
        graph, "GRAPH_MAX_SOURCE_TIMESTAMP", candidate_manifest["max_source_timestamp"]
    )
    monkeypatch.setattr(graph, "GRAPH_SOURCE_JD_SHA256", candidate_manifest["source_jd_sha256"])
    attestation_path = tmp_path / "graph-organizer-attestation.json"
    _write_json(
        attestation_path,
        {
            "schema_version": 1,
            "complete": True,
            "attestation_kind": "fixed-input-graph-promotion",
            "candidate_manifest_sha256": report["candidate_manifest_sha256"],
            "ablation_report_sha256": report_sha256,
            "publication_allowed": True,
            "evaluator_id": "organizer-v1",
            "evaluator_kind": "organizer",
            "significant": True,
            "primary_metric": "ndcg_at_10",
            "baseline_value": report["graph_off"]["ndcg_at_10"],  # type: ignore[index]
            "candidate_value": report["graph_on"]["ndcg_at_10"],  # type: ignore[index]
            "absolute_delta": report["delta"]["ndcg_at_10"],  # type: ignore[index]
            "evaluation_split_sha256": report["split_manifest_sha256"],
            "baseline_run_sha256": report["graph_off_run_sha256"],
            "candidate_run_sha256": report["graph_on_run_sha256"],
            "serving_algorithm": report["serving_algorithm"],
            "serving_policy_sha256": report["serving_policy_sha256"],
            "serving_implementation_sha256": report["serving_implementation_sha256"],
            "evaluation_implementation_sha256": report["evaluation_implementation_sha256"],
        },
    )
    approved_output = tmp_path / "approved-graph"
    approval = graph.approve_graph(
        graph_output=graph_output,
        split_manifest_path=split_manifest,
        evidence_path=fixture["evidence"],
        extraction_manifest_path=fixture["extraction_manifest"],
        ablation_report_path=tmp_path / "ablation.json",
        ablation_report_sha256=report_sha256,
        organizer_attestation_path=attestation_path,
        organizer_attestation_sha256=contract.sha256_file(attestation_path),
        runtime_prefix="graphs/skill-graph/approved-v1",
        candidate_manifest_runtime_path=("evidence/skill-graph/approved-v1-candidate.json"),
        promotion_report_runtime_path="evidence/skill-graph/approved-v1.json",
        organizer_attestation_runtime_path=("evidence/skill-graph/approved-v1-organizer.json"),
        output=approved_output,
    )
    approved_manifest = contract.read_json_object(
        approved_output / "manifest.json", "approved Graph"
    )
    assert approval["manifest_sha256"] == contract.sha256_file(approved_output / "manifest.json")
    assert approved_manifest["publication_allowed"] is True
    assert approved_manifest["serving_policy_sha256"] == report["serving_policy_sha256"]
    assert {item["path"].rsplit("/", 1)[-1] for item in approved_manifest["files"]} == set(  # type: ignore[index,union-attr]
        graph.GRAPH_FILES.values()
    )
    original_run = run_paths["graph_on"].read_bytes()
    original_manifest = manifest_paths["graph_on"].read_bytes()
    tampered_lines = original_run.decode().splitlines()
    tampered_fields = tampered_lines[0].split()
    tampered_fields[4] = "999"
    tampered_lines[0] = " ".join(tampered_fields)
    run_paths["graph_on"].write_text("\n".join(tampered_lines) + "\n", encoding="utf-8")
    on_manifest = json.loads(original_manifest)
    on_manifest["run_sha256"] = contract.sha256_file(run_paths["graph_on"])
    _write_json(manifest_paths["graph_on"], on_manifest)
    with pytest.raises(RuntimeError, match="byte-identical"):
        ablation.run_ablation(
            split_manifest_path=split_manifest,
            graph_output=graph_output,
            evidence_path=fixture["evidence"],
            extraction_manifest_path=fixture["extraction_manifest"],
            qrels_path=qrels,
            queries_path=fixture["queries"],
            jobs_csv=fixture["jobs"],
            tantivy_output=fixture["tantivy"],
            artifact_prefix=tantivy_pipeline.DEFAULT_ARTIFACT_PREFIX,
            graph_off_run=run_paths["graph_off"],
            graph_off_manifest=manifest_paths["graph_off"],
            graph_on_run=run_paths["graph_on"],
            graph_on_manifest=manifest_paths["graph_on"],
            evaluator_command=["organizer-evaluator"],
            evaluator_id="organizer-v1",
            evaluator_kind="organizer",
            minimum_ndcg_delta=0.005,
            output=tmp_path / "tampered.json",
        )

    run_paths["graph_on"].write_bytes(original_run)
    manifest_paths["graph_on"].write_bytes(original_manifest)
    on_manifest = json.loads(original_manifest)
    on_manifest["non_graph_inputs"]["retrieval_config"] = "3" * 64
    _write_json(manifest_paths["graph_on"], on_manifest)
    with pytest.raises(RuntimeError, match="byte-identical"):
        ablation.run_ablation(
            split_manifest_path=split_manifest,
            graph_output=graph_output,
            evidence_path=fixture["evidence"],
            extraction_manifest_path=fixture["extraction_manifest"],
            qrels_path=qrels,
            queries_path=fixture["queries"],
            jobs_csv=fixture["jobs"],
            tantivy_output=fixture["tantivy"],
            artifact_prefix=tantivy_pipeline.DEFAULT_ARTIFACT_PREFIX,
            graph_off_run=run_paths["graph_off"],
            graph_off_manifest=manifest_paths["graph_off"],
            graph_on_run=run_paths["graph_on"],
            graph_on_manifest=manifest_paths["graph_on"],
            evaluator_command=["organizer-evaluator"],
            evaluator_id="organizer-v1",
            evaluator_kind="organizer",
            minimum_ndcg_delta=0.005,
            output=tmp_path / "mismatch.json",
        )


def test_graph_ablation_requires_an_external_evaluator(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="evaluator is required"):
        ablation._evaluate(
            [],
            qrels=tmp_path / "qrels",
            graph_off_run=tmp_path / "off",
            graph_on_run=tmp_path / "on",
            output=tmp_path / "output",
        )


def _full_jobs_csv(tmp_path: Path) -> Path:
    path = tmp_path / "full-jobs.csv"
    extra = (
        whole_embeddings.JOB_ID_FIELD,
        tantivy_pipeline.DEFAULT_LOCATION_CODE_FIELD,
        tantivy_pipeline.DEFAULT_DUTY_CODE_FIELD,
        tantivy_pipeline.DEFAULT_VISIBILITY_FIELD,
        tantivy_pipeline.DEFAULT_MODIFIED_AT_FIELD,
        tantivy_pipeline.SALARY_LOWER_SOURCE_FIELD,
        tantivy_pipeline.SALARY_UPPER_SOURCE_FIELD,
    )
    fields = list(dict.fromkeys([*(label for label, _field in FULL_JOB_FIELDS), *extra]))
    with path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        for job_id, timestamp in (
            ("101", "2026-06-06T10:00:00+08:00"),
            ("102", "2026-06-09T10:00:00+08:00"),
        ):
            row = dict.fromkeys(fields, "")
            row.update(
                {
                    whole_embeddings.JOB_ID_FIELD: job_id,
                    "職務名稱": "資料工程師",
                    "職務小類": "資料工程師",
                    "職務中類": "資訊軟體",
                    "職務大類": "資訊科技",
                    "電腦技能資料": "Python SQL",
                    "薪資": "月薪",
                    "薪資下限": "40000" if job_id == "101" else "0.1",
                    "薪資上限": "60000",
                    "學歷需求": "不拘",
                    "職缺屬性": "全職",
                    "工時": "日班",
                    "工作經驗需求": "不拘",
                    "管理人數": "需管理人數10人以下",
                    "工作城市": "台北市",
                    "職務內容": "使用 Python 與 SQL 建立資料平台",
                    tantivy_pipeline.DEFAULT_LOCATION_CODE_FIELD: "100100",
                    tantivy_pipeline.DEFAULT_DUTY_CODE_FIELD: "140200",
                    tantivy_pipeline.DEFAULT_VISIBILITY_FIELD: "1",
                    tantivy_pipeline.DEFAULT_MODIFIED_AT_FIELD: timestamp,
                }
            )
            writer.writerow(row)
    return path


class InterruptingEncoder(FakeEncoder):
    def __init__(self, fail_at: int | None = None) -> None:
        super().__init__()
        self.calls = 0
        self.fail_at = fail_at

    def encode(self, texts: list[str]) -> np.ndarray:
        self.calls += 1
        if self.calls == self.fail_at:
            raise RuntimeError("simulated interruption")
        return super().encode(texts)


def test_whole_embedding_rebuilds_34_fields_and_resumes_verified_shards(tmp_path: Path) -> None:
    source = _full_jobs_csv(tmp_path)
    output = tmp_path / "whole"
    interrupted = InterruptingEncoder(fail_at=2)

    with pytest.raises(RuntimeError, match="simulated interruption"):
        whole_embeddings.build_whole_embeddings(
            jobs_csv=source,
            output=output,
            tokenizer_sha256="a" * 64,
            encoder=interrupted,
            encoder_backend="test",
            encoder_identity="test-4096-prefix",
            artifact_prefix=whole_embeddings.DEFAULT_ARTIFACT_PREFIX,
            shard_size=1,
            batch_size=1,
        )

    partial = output.with_name(f".{output.name}.partial")
    assert (partial / "embeddings-00000.f16.npy.manifest.json").is_file()
    resumed = InterruptingEncoder()
    component = whole_embeddings.build_whole_embeddings(
        jobs_csv=source,
        output=output,
        tokenizer_sha256="a" * 64,
        encoder=resumed,
        encoder_backend="test",
        encoder_identity="test-4096-prefix",
        artifact_prefix=whole_embeddings.DEFAULT_ARTIFACT_PREFIX,
        shard_size=1,
        batch_size=1,
    )

    assert resumed.calls == 1
    assert component["source_dimension"] == 4096
    assert component["dimension"] == 1024
    assert component["projection"] == whole_embeddings.PROJECTION
    validation = whole_embeddings.validate_whole_embeddings(
        output,
        jobs_csv=source,
        artifact_prefix=whole_embeddings.DEFAULT_ARTIFACT_PREFIX,
    )
    assert validation["rows"] == 2
    build = json.loads((output / "build-manifest.json").read_text(encoding="utf-8"))
    assert len(build["document_fields"]) == len(FULL_JOB_FIELDS) == 34
    assert len(build["shards"]) == 2


class FakeBedrock:
    def __init__(self, *, callable: bool = True) -> None:
        self.callable = callable
        self.calls = 0
        self.requests: list[dict[str, object]] = []

    def converse(self, **kwargs: object) -> dict[str, object]:
        if not self.callable:
            raise AssertionError("completed response should have resumed without Bedrock")
        self.calls += 1
        self.requests.append(kwargs)
        return {
            "stopReason": "tool_use",
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": "tool-use-1",
                                "name": llm_extraction.TOOL_NAME,
                                "type": "tool_use",
                                "input": {
                                    "skills": [
                                        {
                                            "canonical_name": "python",
                                            "surface": "Python",
                                            "category": "programming",
                                            "evidence_span": "電腦技能資料: Python SQL",
                                        }
                                    ],
                                    "relations": [],
                                },
                            }
                        }
                    ],
                }
            },
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }


def test_llm_extraction_is_train_only_evidence_locked_and_resumable(tmp_path: Path) -> None:
    source = _full_jobs_csv(tmp_path)
    split, _qrels = _split_manifest(tmp_path)
    prepared = tmp_path / "prepared"
    prepared_manifest = llm_extraction.prepare_requests(
        jobs_csv=source,
        split_manifest_path=split,
        output=prepared,
        duty_field="職務小類",
        modified_at_field=tantivy_pipeline.DEFAULT_MODIFIED_AT_FIELD,
        source_timezone=llm_extraction.DEFAULT_SOURCE_TIMEZONE,
        sample_limit=llm_extraction.DEFAULT_SAMPLE_LIMIT,
    )
    assert prepared_manifest["records"] == 1
    assert prepared_manifest["post_cutoff_skipped"] == 1

    bedrock = FakeBedrock()
    extraction_output = tmp_path / "extraction"
    extraction_manifest = llm_extraction.extract(
        prepared=prepared,
        split_manifest_path=split,
        output=extraction_output,
        model_id="bedrock-model@revision",
        bedrock=bedrock,
    )
    assert bedrock.calls == 1
    tool_config = bedrock.requests[0]["toolConfig"]
    assert tool_config == llm_extraction.TOOL_CONFIG
    assert tool_config["toolChoice"] == {"tool": {"name": llm_extraction.TOOL_NAME}}
    assert llm_extraction.BEDROCK_MAX_OUTPUT_TOKENS == 64_000
    assert bedrock.requests[0]["inferenceConfig"] == {
        "temperature": 0,
        "maxTokens": llm_extraction.BEDROCK_MAX_OUTPUT_TOKENS,
    }
    schema = tool_config["tools"][0]["toolSpec"]["inputSchema"]["json"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["skills", "relations"]
    assert extraction_manifest["canonicalization_policy"] == (
        "open_surface_per_jd_llm_canonicalization_v1"
    )
    assert extraction_manifest["test_jd_used"] is False
    resumed = llm_extraction.extract(
        prepared=prepared,
        split_manifest_path=split,
        output=extraction_output,
        model_id="bedrock-model@revision",
        bedrock=FakeBedrock(callable=False),
    )
    assert resumed == extraction_manifest

    graph_output = tmp_path / "llm-graph"
    graph_manifest = graph.build_graph(
        evidence_path=extraction_output / "evidence.jsonl",
        extraction_manifest_path=extraction_output / "manifest.json",
        split_manifest_path=split,
        output=graph_output,
        minimum_support=1,
    )
    assert graph_manifest["source_records"] == 1


def test_llm_prepare_localizes_naive_source_time_and_skips_empty_duty(tmp_path: Path) -> None:
    source = tmp_path / "llm-jobs.csv"
    fields = list(
        dict.fromkeys(
            [
                *(label for label, _field in FULL_JOB_FIELDS),
                llm_extraction.JOB_ID_FIELD,
                llm_extraction.DEFAULT_MODIFIED_AT_FIELD,
            ]
        )
    )
    with source.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        for job_id, duty, timestamp in (
            ("101", "資料工程師", "2026-06-07 10:00:00"),
            ("102", "NULL", "2026-06-07 11:00:00"),
            ("103", "資料工程師", "2026-06-08 00:00:00"),
        ):
            row = dict.fromkeys(fields, "")
            row.update(
                {
                    llm_extraction.JOB_ID_FIELD: job_id,
                    "職務名稱": "資料工程師",
                    "職務小類": duty,
                    "職務中類": "資訊軟體",
                    "職務大類": "資訊科技",
                    "電腦技能資料": "Python SQL",
                    "職務內容": "使用 Python 與 SQL 建立資料平台",
                    llm_extraction.DEFAULT_MODIFIED_AT_FIELD: timestamp,
                }
            )
            writer.writerow(row)
    split, _qrels = _split_manifest(tmp_path)

    manifest = llm_extraction.prepare_requests(
        jobs_csv=source,
        split_manifest_path=split,
        output=tmp_path / "prepared",
        duty_field=llm_extraction.DEFAULT_DUTY_FIELD,
        modified_at_field=llm_extraction.DEFAULT_MODIFIED_AT_FIELD,
        source_timezone="Asia/Taipei",
        sample_limit=654,
    )

    request = json.loads((tmp_path / "prepared" / "requests.jsonl").read_text(encoding="utf-8"))
    assert llm_extraction.DEFAULT_MODIFIED_AT_FIELD == "職缺最後修改時間"
    assert manifest["source_timezone"] == "Asia/Taipei"
    assert manifest["source_timestamp_policy"] == llm_extraction.SOURCE_TIMESTAMP_POLICY
    assert manifest["empty_duty_skipped"] == 1
    assert manifest["eligible_train_records"] == 1
    assert manifest["post_cutoff_skipped"] == 1
    assert request["source_modified_at"] == "2026-06-07T10:00:00+08:00"
    with pytest.raises(RuntimeError, match="timezone"):
        llm_extraction._timestamp("2026-06-08T00:00:00", "split timestamp")


@pytest.mark.parametrize(
    "content",
    [
        [{"text": '{"skills":[],"relations":[]}'}],
        [
            {
                "toolUse": {
                    "toolUseId": "one",
                    "name": "extract_job_skill_graph",
                    "type": "tool_use",
                    "input": {"skills": [], "relations": []},
                }
            },
            {
                "toolUse": {
                    "toolUseId": "two",
                    "name": "extract_job_skill_graph",
                    "type": "tool_use",
                    "input": {"skills": [], "relations": []},
                }
            },
        ],
        [
            {
                "toolUse": {
                    "toolUseId": "one",
                    "name": "extract_job_skill_graph",
                    "type": "tool_use",
                    "input": {"skills": [], "relations": []},
                },
                "text": "also text",
            }
        ],
        [
            {
                "toolUse": {
                    "toolUseId": "one",
                    "name": "wrong_tool",
                    "type": "tool_use",
                    "input": {"skills": [], "relations": []},
                }
            }
        ],
        [
            {
                "toolUse": {
                    "toolUseId": "one",
                    "name": "extract_job_skill_graph",
                    "type": "server_tool_use",
                    "input": {"skills": [], "relations": []},
                }
            }
        ],
    ],
)
def test_llm_extraction_rejects_any_non_exact_tool_use(content: list[dict[str, object]]) -> None:
    response = {
        "stopReason": "tool_use",
        "output": {"message": {"role": "assistant", "content": content}},
        "usage": {"inputTokens": 1, "outputTokens": 1},
    }

    with pytest.raises(RuntimeError, match="toolUse"):
        llm_extraction._tool_input(response)


def test_llm_tool_parser_locks_observed_runtime_shape_despite_service_model_drift() -> None:
    observed_runtime_response = {
        "stopReason": "tool_use",
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "observed-runtime-tool-use",
                            "name": llm_extraction.TOOL_NAME,
                            "type": "tool_use",
                            "input": {"skills": [], "relations": []},
                        }
                    }
                ],
            }
        },
        "usage": {"inputTokens": 1, "outputTokens": 1},
    }

    payload, input_tokens, output_tokens = llm_extraction._tool_input(observed_runtime_response)
    assert payload == {"skills": [], "relations": []}
    assert (input_tokens, output_tokens) == (1, 1)
    assert llm_extraction.TOOL_USE_TYPE == "tool_use"

    service = get_session().get_service_model("bedrock-runtime")
    tool_use = (
        service.operation_model("Converse")
        .output_shape.members["output"]
        .members["message"]
        .members["content"]
        .member.members["toolUse"]
    )
    assert tool_use.required_members == ["toolUseId", "name", "input"]
    assert tool_use.members["type"].metadata["enum"] == ["server_tool_use"]


def test_graph_sampling_is_stratified_and_hard_capped() -> None:
    quotas = llm_extraction._sample_quotas({"rare": 1, "common": 100}, 5)

    assert quotas == {"rare": 1, "common": 4}
    with pytest.raises(ValueError, match="between 1 and 10000"):
        llm_extraction._sample_quotas({"common": 1}, 10_001)


def test_tantivy_builder_is_independent_with_corrections_explicitly_disabled(
    tmp_path: Path,
) -> None:
    source = _full_jobs_csv(tmp_path)
    output = tmp_path / "tantivy"

    component = tantivy_pipeline.build_tantivy(
        jobs_csv=source,
        output=output,
        artifact_prefix=tantivy_pipeline.DEFAULT_ARTIFACT_PREFIX,
        location_code_field=tantivy_pipeline.DEFAULT_LOCATION_CODE_FIELD,
        location_term_field=tantivy_pipeline.DEFAULT_LOCATION_TERM_FIELD,
        duty_code_field=tantivy_pipeline.DEFAULT_DUTY_CODE_FIELD,
        duty_term_field=tantivy_pipeline.DEFAULT_DUTY_TERM_FIELD,
        visibility_field=tantivy_pipeline.DEFAULT_VISIBILITY_FIELD,
        modified_at_field=tantivy_pipeline.DEFAULT_MODIFIED_AT_FIELD,
        correction_candidate_path=None,
        correction_attestation_path=None,
    )
    validation = tantivy_pipeline.validate_tantivy(
        output,
        jobs_csv=source,
        artifact_prefix=tantivy_pipeline.DEFAULT_ARTIFACT_PREFIX,
    )

    assert validation["rows"] == 2
    assert component["lexical_policy_sha256"] == tantivy_pipeline.lexical_policy_sha256()
    assert set(component["source_fields"]) == set(tantivy_pipeline.TEXT_FIELDS)
    assert component["schema_fields"] == tantivy_pipeline.SCHEMA_FIELDS
    assert component["filter_semantics"] == tantivy_pipeline.FILTER_SEMANTICS
    build = json.loads((output / "build-manifest.json").read_text())
    assert tantivy_pipeline.SALARY_LOWER_SOURCE_FIELD in build["source_csv_fields"]
    assert tantivy_pipeline.SALARY_UPPER_SOURCE_FIELD in build["source_csv_fields"]
    assert build["salary_filter_excluded_rows"] == 1
    assert component["build_manifest_path"].endswith("/build-manifest.json")
    assert component["query_corrections"] == {"enabled": False}
    assert not (output / "query-corrections.json").exists()


def test_tantivy_graph_off_is_generated_from_canonical_queries_and_index(
    tmp_path: Path,
) -> None:
    source = _full_jobs_csv(tmp_path)
    index_output = tmp_path / "tantivy"
    tantivy_pipeline.build_tantivy(
        jobs_csv=source,
        output=index_output,
        artifact_prefix=tantivy_pipeline.DEFAULT_ARTIFACT_PREFIX,
        location_code_field=tantivy_pipeline.DEFAULT_LOCATION_CODE_FIELD,
        location_term_field=tantivy_pipeline.DEFAULT_LOCATION_TERM_FIELD,
        duty_code_field=tantivy_pipeline.DEFAULT_DUTY_CODE_FIELD,
        duty_term_field=tantivy_pipeline.DEFAULT_DUTY_TERM_FIELD,
        visibility_field=tantivy_pipeline.DEFAULT_VISIBILITY_FIELD,
        modified_at_field=tantivy_pipeline.DEFAULT_MODIFIED_AT_FIELD,
        correction_candidate_path=None,
        correction_attestation_path=None,
    )
    split, _qrels = _split_manifest(tmp_path)
    queries = tmp_path / "queries.jsonl"
    _write_jsonl(
        queries,
        [
            {
                "qid": "q1",
                "query": "資料工程師 Python",
                "as_of": "2026-06-08T23:59:59.999+08:00",
                "location_codes": ["100100"],
                "duty_codes": ["140200"],
            },
            {
                "qid": "q2",
                "query": "資料工程師 Python",
                "as_of": "2026-06-08T23:59:59.999+08:00",
                "location_codes": ["999999"],
                "duty_codes": ["140200"],
            },
        ],
    )

    output = tmp_path / "graph-off"
    manifest = graph_off.generate_graph_off(
        split_manifest_path=split,
        queries_path=queries,
        tantivy_output=index_output,
        jobs_csv=source,
        artifact_prefix=tantivy_pipeline.DEFAULT_ARTIFACT_PREFIX,
        output=output,
    )

    rows = (output / "graph-off.run").read_text(encoding="utf-8").splitlines()
    assert [row.split()[0] for row in rows] == ["q1", "q1"]
    assert {row.split()[2] for row in rows} == {"101", "102"}
    assert manifest["run_sha256"] == contract.sha256_file(output / "graph-off.run")
    assert manifest["canonical_qids"] == ["q1", "q2"]
    assert manifest["zero_result_qids"] == ["q2"]
    assert manifest["non_graph_inputs"]["canonical_queries"] == contract.sha256_file(queries)
    assert (
        manifest["non_graph_inputs"]["tantivy_index"]
        == json.loads((index_output / "manifest.json").read_text(encoding="utf-8"))["index_sha256"]
    )


@pytest.mark.parametrize(
    "patch, message",
    [
        ({"search_date": "2026-06-08"}, "canonical query"),
        ({"as_of": "2026-06-08T23:59:59"}, "timezone"),
        ({"as_of": "2026-06-09T00:00:00+08:00"}, "evaluation window"),
        ({"location_codes": ["100100", "100100"]}, "location_codes"),
    ],
)
def test_canonical_graph_off_queries_fail_closed(
    tmp_path: Path, patch: dict[str, object], message: str
) -> None:
    split, _qrels = _split_manifest(tmp_path)
    value: dict[str, object] = {
        "qid": "q1",
        "query": "資料工程師",
        "as_of": "2026-06-08T23:59:59.999+08:00",
        "location_codes": ["100100"],
        "duty_codes": ["140200"],
    }
    value.update(patch)
    path = tmp_path / "queries.jsonl"
    _write_jsonl(path, [value])

    with pytest.raises(RuntimeError, match=message):
        graph_off.read_canonical_queries(path, split)


def test_query_corrections_require_positive_promotion_before_tantivy_enablement(
    tmp_path: Path,
) -> None:
    source = _full_jobs_csv(tmp_path)
    evidence, extraction_manifest, split = _skill_evidence(tmp_path)
    candidate_path = tmp_path / "query-corrections.json"
    candidate = query_corrections.build_candidate(
        evidence_path=evidence,
        extraction_manifest_path=extraction_manifest,
        split_manifest_path=split,
        minimum_support=2,
        output=candidate_path,
    )
    assert candidate["corrections"] == {"pyhton": "python"}
    candidate_sha256 = contract.sha256_file(candidate_path)
    report_path = tmp_path / "query-correction-promotion.json"
    _write_json(
        report_path,
        {
            "schema_version": 1,
            "complete": True,
            "experiment": "query correction fixed-input ablation",
            "candidate_sha256": candidate_sha256,
            "primary_metric": "ndcg_at_10",
            "absolute_delta": 0.001,
            "evaluator_kind": "organizer",
            "significant": True,
            "evaluation_split_sha256": contract.sha256_file(split),
            "baseline_run_sha256": "6" * 64,
            "candidate_run_sha256": "7" * 64,
        },
    )
    attestation_path = tmp_path / "query-corrections.attestation.json"
    rejected = json.loads(report_path.read_text(encoding="utf-8"))
    rejected["absolute_delta"] = 0.0
    _write_json(report_path, rejected)
    with pytest.raises(RuntimeError, match="did not pass"):
        query_corrections.approve_candidate(
            candidate_path=candidate_path,
            split_manifest_path=split,
            promotion_report_path=report_path,
            promotion_report_sha256=contract.sha256_file(report_path),
            attestation_path=attestation_path,
        )
    rejected["absolute_delta"] = 0.001
    _write_json(report_path, rejected)
    query_corrections.approve_candidate(
        candidate_path=candidate_path,
        split_manifest_path=split,
        promotion_report_path=report_path,
        promotion_report_sha256=contract.sha256_file(report_path),
        attestation_path=attestation_path,
    )
    output = tmp_path / "tantivy-enabled"
    component = tantivy_pipeline.build_tantivy(
        jobs_csv=source,
        output=output,
        artifact_prefix=tantivy_pipeline.DEFAULT_ARTIFACT_PREFIX,
        location_code_field=tantivy_pipeline.DEFAULT_LOCATION_CODE_FIELD,
        location_term_field=tantivy_pipeline.DEFAULT_LOCATION_TERM_FIELD,
        duty_code_field=tantivy_pipeline.DEFAULT_DUTY_CODE_FIELD,
        duty_term_field=tantivy_pipeline.DEFAULT_DUTY_TERM_FIELD,
        visibility_field=tantivy_pipeline.DEFAULT_VISIBILITY_FIELD,
        modified_at_field=tantivy_pipeline.DEFAULT_MODIFIED_AT_FIELD,
        correction_candidate_path=candidate_path,
        correction_attestation_path=attestation_path,
    )

    correction = component["query_corrections"]
    assert isinstance(correction, dict)
    assert correction["enabled"] is True
    assert (output / "query-corrections.attestation.json").is_file()
