from __future__ import annotations

import hashlib
import io
import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import multiview_embedding_pipeline as embeddings
import pipeline_contract as contract
import skill_graph_pipeline as graph


def _write_json(path: Path, value: object) -> None:
    contract.atomic_json(path, value)


def _write_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    path.write_bytes(b"".join(contract.canonical_json(value) + b"\n" for value in values))


def _skill_evidence(tmp_path: Path) -> tuple[Path, Path]:
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
            "source_policy": "train_jd_only",
            "test_jd_used": False,
            "uses_ground_truth": False,
            "uses_behavior_logs": False,
            "train_cutoff_exclusive": graph.TRAIN_CUTOFF.isoformat(),
            "max_source_timestamp": timestamps[-1],
            "source_jd_sha256": "a" * 64,
            "evidence_sha256": contract.sha256_file(evidence_path),
        },
    )
    return evidence_path, manifest_path


def test_skill_graph_build_validate_and_trace(tmp_path: Path) -> None:
    evidence, extraction_manifest = _skill_evidence(tmp_path)
    output = tmp_path / "graph"

    manifest = graph.build_graph(
        evidence_path=evidence,
        extraction_manifest_path=extraction_manifest,
        output=output,
        minimum_support=2,
    )
    validation = graph.validate_graph(output)
    trace = graph.trace_skill(output, "Python", 10)

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
    evidence, extraction_manifest = _skill_evidence(tmp_path)
    manifest = json.loads(extraction_manifest.read_text(encoding="utf-8"))
    manifest["max_source_timestamp"] = graph.TRAIN_CUTOFF.isoformat()
    _write_json(extraction_manifest, manifest)

    with pytest.raises(RuntimeError, match="test-period JD"):
        graph.build_graph(
            evidence_path=evidence,
            extraction_manifest_path=extraction_manifest,
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


def _multiview_input(tmp_path: Path) -> tuple[Path, Path, Path, str]:
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
    manifest_path = tmp_path / "views-manifest.json"
    _write_json(
        manifest_path,
        {
            "schema_version": 1,
            "complete": True,
            "dataset_sha256": "a" * 64,
            "jobs_sha256": "b" * 64,
            "job_row_order_sha256": "c" * 64,
            "document_policy_version": "full-jd-v2",
            "view_policy_version": embeddings.VIEW_POLICY_VERSION,
            "view_kinds": list(embeddings.VIEW_KINDS),
            "records": len(records),
            "records_sha256": contract.sha256_file(records_path),
        },
    )
    promotion_path = tmp_path / "promotion.json"
    _write_json(
        promotion_path,
        {
            "schema_version": 1,
            "complete": True,
            "experiment": "Qwen3 multi-view MRL ablation",
            "selected_dimension": embeddings.OUTPUT_DIMENSION,
            "reference_dimension": embeddings.MODEL_DIMENSION,
            "primary_metric": "ndcg_at_10",
            "absolute_delta": 0.001,
            "evaluation_split_sha256": "d" * 64,
            "baseline_run_sha256": "e" * 64,
            "candidate_run_sha256": "f" * 64,
        },
    )
    return (
        records_path,
        manifest_path,
        promotion_path,
        contract.sha256_file(promotion_path),
    )


def test_multiview_embedding_build_is_promotion_gated_and_verifiable(
    tmp_path: Path,
) -> None:
    records, input_manifest, promotion, promotion_sha256 = _multiview_input(tmp_path)
    output = tmp_path / "embeddings"
    encoder = FakeEncoder()

    manifest = embeddings.build_embeddings(
        records_path=records,
        input_manifest_path=input_manifest,
        promotion_report_path=promotion,
        promotion_report_sha256=promotion_sha256,
        output=output,
        encoder=encoder,
        encoder_backend="test",
        shard_size=3,
        batch_size=2,
    )

    assert encoder.closed is True
    assert manifest["publication_allowed"] is True
    assert len(manifest["shards"]) == 2
    assert embeddings.verify_embeddings(output)["records"] == 4

    (output / "embeddings-00000.f16.npy").write_bytes(b"corrupt")
    with pytest.raises(RuntimeError, match="bytes differ"):
        embeddings.verify_embeddings(output)


def test_multiview_embedding_rejects_non_positive_ablation(tmp_path: Path) -> None:
    records, input_manifest, promotion, _ = _multiview_input(tmp_path)
    report = json.loads(promotion.read_text(encoding="utf-8"))
    report["absolute_delta"] = 0.0
    _write_json(promotion, report)
    encoder = FakeEncoder()

    with pytest.raises(RuntimeError, match="did not pass"):
        embeddings.build_embeddings(
            records_path=records,
            input_manifest_path=input_manifest,
            promotion_report_path=promotion,
            promotion_report_sha256=contract.sha256_file(promotion),
            output=tmp_path / "embeddings",
            encoder=encoder,
            encoder_backend="test",
            shard_size=3,
            batch_size=2,
        )
    assert encoder.closed is True


@pytest.mark.parametrize("value", ["cuda:0", "cuda:0,cuda:0", "cpu,cuda:1"])
def test_dual_gpu_contract_rejects_ambiguous_devices(value: str) -> None:
    with pytest.raises(ValueError, match="at least two unique explicit devices"):
        embeddings.parse_devices(value)


class FakeS3:
    def __init__(self, values: dict[str, bytes]) -> None:
        self.values = values

    def get_object(self, **kwargs: object) -> dict[str, object]:
        body = self.values[str(kwargs["Key"])]
        return {"ContentLength": len(body), "Body": io.BytesIO(body)}


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
