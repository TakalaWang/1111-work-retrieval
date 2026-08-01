from __future__ import annotations

import csv
import hashlib
import io
import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import graph_ablation_runner as ablation
import llm_skill_extraction_pipeline as llm_extraction
import multiview_embedding_pipeline as embeddings
import pipeline_contract as contract
import skill_graph_pipeline as graph
import tantivy_index_pipeline as tantivy_pipeline
import whole_embedding_pipeline as whole_embeddings
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
    timestamps = ("2026-06-06T10:00:00+08:00", "2026-06-07T09:00:00+08:00")
    records: list[dict[str, object]] = []
    for index, timestamp in enumerate(timestamps, start=1):
        source = "Python 與 SQL 是此職缺必要技能。"
        records.append(
            {
                "record_id": f"record-{index}",
                "job_id": str(100 + index),
                "duty": "data engineer",
                "source_modified_at": timestamp,
                "source_text": source,
                "source_text_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "skills": [
                    {
                        "canonical_name": "python",
                        "surface": "Python",
                        "category": "programming language",
                        "evidence_span": "Python",
                    },
                    {
                        "canonical_name": "sql",
                        "surface": "SQL",
                        "category": "query language",
                        "evidence_span": "SQL",
                    },
                ],
                "relations": [
                    {
                        "source": "python",
                        "type": "USED_WITH",
                        "target": "sql",
                        "evidence_span": "Python 與 SQL",
                    }
                ],
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
            "source_records": 2,
            "processed_records": 2,
            "input_tokens": 10,
            "output_tokens": 10,
            "skill_rejections": 0,
            "relation_rejections": 0,
        },
    )
    return evidence_path, manifest_path, split_path


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
    validation = graph.validate_graph(output, split_manifest)
    trace = graph.trace_skill(output, split_manifest, "Python", 10)

    assert manifest["graph_kind"] == "llm-evidence-locked-typed-entity-graph"
    assert validation["passed"] is True
    assert trace["jobs"] == [
        {"job_id": "101", "evidence_span": "Python"},
        {"job_id": "102", "evidence_span": "Python"},
    ]
    assert trace["relations"][0]["target"] == "sql"
    assert trace["relations"][0]["evidence"] == [
        {"job_id": "101", "evidence_span": "Python 與 SQL"},
        {"job_id": "102", "evidence_span": "Python 與 SQL"},
    ]


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
    evidence, extraction_manifest, split_manifest = _skill_evidence(tmp_path)
    graph_output = tmp_path / "graph"
    graph.build_graph(
        evidence_path=evidence,
        extraction_manifest_path=extraction_manifest,
        split_manifest_path=split_manifest,
        output=graph_output,
        minimum_support=2,
    )
    _, qrels = _split_manifest(tmp_path)
    graph_manifest_sha256 = contract.sha256_file(graph_output / "manifest.json")
    split_sha256 = contract.sha256_file(split_manifest)
    non_graph_inputs = {"queries": "1" * 64, "retrieval_config": "2" * 64}
    run_paths: dict[str, Path] = {}
    manifest_paths: dict[str, Path] = {}
    for variant in ("graph_off", "graph_on"):
        run_path = tmp_path / f"{variant}.run"
        run_path.write_text(f"{variant}\n", encoding="utf-8")
        manifest_path = tmp_path / f"{variant}.json"
        _write_json(
            manifest_path,
            {
                "schema_version": 1,
                "complete": True,
                "variant": variant,
                "split_manifest_sha256": split_sha256,
                "graph_manifest_sha256": (graph_manifest_sha256 if variant == "graph_on" else None),
                "non_graph_inputs": non_graph_inputs,
                "run_sha256": contract.sha256_file(run_path),
            },
        )
        run_paths[variant] = run_path
        manifest_paths[variant] = manifest_path

    monkeypatch.setattr(
        ablation,
        "_evaluate",
        lambda *args, **kwargs: {
            "query_count": 2,
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
        },
    )
    report = ablation.run_ablation(
        split_manifest_path=split_manifest,
        graph_output=graph_output,
        qrels_path=qrels,
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

    assert report["promotion_allowed"] is True
    assert report["publication_allowed"] is True
    assert report["official_score_claimed"] is False
    assert all(not control["enabled"] for control in report["controls"].values())  # type: ignore[union-attr]
    on_manifest = json.loads(manifest_paths["graph_on"].read_text(encoding="utf-8"))
    on_manifest["non_graph_inputs"]["retrieval_config"] = "3" * 64
    _write_json(manifest_paths["graph_on"], on_manifest)
    with pytest.raises(RuntimeError, match="not byte-identical"):
        ablation.run_ablation(
            split_manifest_path=split_manifest,
            graph_output=graph_output,
            qrels_path=qrels,
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

    def converse(self, **kwargs: object) -> dict[str, object]:
        if not self.callable:
            raise AssertionError("completed response should have resumed without Bedrock")
        self.calls += 1
        return {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "text": json.dumps(
                                {
                                    "skills": [
                                        {
                                            "canonical_name": "python",
                                            "surface": "Python",
                                            "category": "programming",
                                            "evidence_span": "電腦技能資料: Python SQL",
                                        }
                                    ],
                                    "relations": [],
                                }
                            )
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


def test_tantivy_builder_indexes_full_jd_filters_and_train_only_corrections(
    tmp_path: Path,
) -> None:
    source = _full_jobs_csv(tmp_path)
    evidence, extraction_manifest, split = _skill_evidence(tmp_path)
    output = tmp_path / "tantivy"

    component = tantivy_pipeline.build_tantivy(
        jobs_csv=source,
        evidence_path=evidence,
        extraction_manifest_path=extraction_manifest,
        split_manifest_path=split,
        output=output,
        artifact_prefix=tantivy_pipeline.DEFAULT_ARTIFACT_PREFIX,
        location_code_field=tantivy_pipeline.DEFAULT_LOCATION_CODE_FIELD,
        location_term_field=tantivy_pipeline.DEFAULT_LOCATION_TERM_FIELD,
        duty_code_field=tantivy_pipeline.DEFAULT_DUTY_CODE_FIELD,
        duty_term_field=tantivy_pipeline.DEFAULT_DUTY_TERM_FIELD,
        visibility_field=tantivy_pipeline.DEFAULT_VISIBILITY_FIELD,
        modified_at_field=tantivy_pipeline.DEFAULT_MODIFIED_AT_FIELD,
        correction_minimum_support=2,
    )
    validation = tantivy_pipeline.validate_tantivy(
        output,
        jobs_csv=source,
        artifact_prefix=tantivy_pipeline.DEFAULT_ARTIFACT_PREFIX,
    )

    assert validation["rows"] == 2
    assert component["lexical_policy_sha256"] == tantivy_pipeline.lexical_policy_sha256()
    assert set(component["source_fields"]) == set(tantivy_pipeline.TEXT_FIELDS)
    assert component["build_manifest_path"].endswith("/build-manifest.json")
    corrections = json.loads((output / "query-corrections.json").read_text(encoding="utf-8"))
    assert corrections["source_policy"] == "train_jd_only"
