from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import cast

import numpy as np
import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import materialize_runtime_components as materializer  # noqa: E402
import pipeline_contract as pipeline  # noqa: E402
import promote_runtime_artifacts as promotion  # noqa: E402
import tantivy_index_pipeline as tantivy_builder  # noqa: E402
from work_retrieval_core.adapters import TantivyLayout, WholeEmbeddingLayout  # noqa: E402
from work_retrieval_core.manifest import RuntimeManifest  # noqa: E402
from work_retrieval_core.serialization import FULL_JOB_FIELDS  # noqa: E402


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pipeline.canonical_json(value) + b"\n")


def full_jobs_csv(root: Path, job_ids: tuple[str, ...]) -> Path:
    path = root / "jobs.csv"
    extra = (
        "職缺編號",
        tantivy_builder.DEFAULT_LOCATION_CODE_FIELD,
        tantivy_builder.DEFAULT_DUTY_CODE_FIELD,
        tantivy_builder.DEFAULT_VISIBILITY_FIELD,
        tantivy_builder.DEFAULT_MODIFIED_AT_FIELD,
    )
    fields = list(dict.fromkeys([*(label for label, _field in FULL_JOB_FIELDS), *extra]))
    with path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        for job_id in job_ids:
            row = dict.fromkeys(fields, "")
            row.update(
                {
                    "職缺編號": job_id,
                    "職務名稱": "資料工程師",
                    "職務小類": "資料工程師",
                    "職務中類": "資訊軟體",
                    "職務大類": "資訊科技",
                    "電腦技能資料": "Python SQL",
                    "工作城市": "台北市",
                    "職務內容": "使用 Python 與 SQL 建立資料平台",
                    tantivy_builder.DEFAULT_LOCATION_CODE_FIELD: "100100",
                    tantivy_builder.DEFAULT_DUTY_CODE_FIELD: "140200",
                    tantivy_builder.DEFAULT_VISIBILITY_FIELD: "1",
                    tantivy_builder.DEFAULT_MODIFIED_AT_FIELD: "2026-06-06T10:00:00+08:00",
                }
            )
            writer.writerow(row)
    return path


def query_correction_pair(root: Path) -> tuple[Path, Path]:
    candidate = root / "candidate.json"
    write_json(
        candidate,
        {
            "schema_version": 1,
            "complete": True,
            "publication_allowed": False,
            "source_policy": "train_jd_only",
            "test_jd_used": False,
            "uses_ground_truth": False,
            "uses_behavior_logs": False,
            "train_cutoff_exclusive": "2026-06-08T00:00:00+08:00",
            "max_source_timestamp": "2026-06-07T23:59:59+08:00",
            "source_manifest_sha256": "1" * 64,
            "evidence_sha256": "2" * 64,
            "minimum_support": 2,
            "corrections": {"pyhton": "python"},
        },
    )
    attestation = root / "attestation.json"
    write_json(
        attestation,
        {
            "schema_version": 1,
            "complete": True,
            "attestation_kind": "fixed-input-query-correction-promotion",
            "candidate_sha256": pipeline.sha256_file(candidate),
            "promotion_report_sha256": "3" * 64,
            "publication_allowed": True,
            "evaluator_kind": "organizer",
            "significant": True,
            "primary_metric": "ndcg_at_10",
            "absolute_delta": 0.001,
            "evaluation_split_sha256": "4" * 64,
            "baseline_run_sha256": "5" * 64,
            "candidate_run_sha256": "6" * 64,
        },
    )
    return candidate, attestation


def source_fixture(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    corrections: bool = False,
) -> tuple[Path, Path, Path, dict[str, str]]:
    job_ids = ("10", "20", "30")
    jobs = full_jobs_csv(root, job_ids)
    whole = root / "whole"
    whole.mkdir()
    shards: list[dict[str, object]] = []
    order = hashlib.sha256()
    for index, values in enumerate((job_ids[:2], job_ids[2:])):
        ids_path = whole / f"job-ids-{index:05d}.json"
        vectors_path = whole / f"embeddings-{index:05d}.f16.npy"
        write_json(ids_path, list(values))
        vectors = np.zeros((len(values), promotion.APPROVED_SOURCE_EMBEDDING_DIMENSION))
        vectors[:, :2] = np.asarray([[3.0, 4.0], [4.0, 3.0]][: len(values)])
        np.save(vectors_path, vectors.astype(np.float16), allow_pickle=False)
        for job_id in values:
            order.update(job_id.encode() + b"\n")
        shards.append(
            {
                "index": index,
                "rows": len(values),
                "dimension": promotion.APPROVED_SOURCE_EMBEDDING_DIMENSION,
                "elapsed_s": 1.0,
                "documents_per_second": float(len(values)),
                "job_ids_sha256": digest("\n".join(values).encode()),
                "job_ids_file_sha256": pipeline.sha256_file(ids_path),
                "embedding_sha256": pipeline.sha256_file(vectors_path),
            }
        )
    whole_manifest = {
        "complete": True,
        "model": promotion.APPROVED_MODEL,
        "revision": promotion.APPROVED_MODEL_REVISION,
        "batch_size": 128,
        "max_length": 512,
        "shard_size": 2,
        "dtype": "float16",
        "normalized": True,
        "document_policy_version": promotion.APPROVED_DOCUMENT_POLICY_VERSION,
        "document_template_sha256": promotion.APPROVED_DOCUMENT_TEMPLATE_SHA256,
        "document_fields": promotion.APPROVED_DOCUMENT_FIELDS,
        "dataset_sha256": pipeline.sha256_file(jobs),
        "jobs_sha256": pipeline.sha256_file(jobs),
        "job_row_order_sha256": order.hexdigest(),
        "rows": len(job_ids),
        "shards": shards,
    }
    write_json(whole / "manifest.json", whole_manifest)
    inventory_files = []
    for path in sorted(whole.iterdir()):
        inventory_files.append(
            {
                "path": f"{materializer.SOURCE_CACHE_INVENTORY_PREFIX}{path.name}",
                "sha256": pipeline.sha256_file(path),
                "size": path.stat().st_size,
            }
        )
    inventory = root / "whole-inventory.json"
    write_json(inventory, {"schema_version": 3, "files": inventory_files})
    monkeypatch.setattr(
        promotion,
        "APPROVED_WHOLE_SOURCE_MANIFEST_SHA256",
        pipeline.sha256_file(whole / "manifest.json"),
    )
    monkeypatch.setattr(
        promotion,
        "APPROVED_WHOLE_SOURCE_INVENTORY_SHA256",
        pipeline.sha256_file(inventory),
    )
    monkeypatch.setattr(promotion, "APPROVED_WHOLE_SOURCE_FILE_COUNT", len(inventory_files))
    monkeypatch.setattr(
        promotion,
        "APPROVED_WHOLE_SOURCE_BYTES",
        sum(cast(int, item["size"]) for item in inventory_files),
    )
    monkeypatch.setattr(promotion, "APPROVED_WHOLE_SOURCE_ROWS", len(job_ids))
    monkeypatch.setattr(promotion, "APPROVED_WHOLE_SOURCE_SHARDS", len(shards))
    monkeypatch.setattr(promotion, "APPROVED_JOBS_DATASET_SHA256", pipeline.sha256_file(jobs))
    candidate: Path | None = None
    attestation: Path | None = None
    if corrections:
        candidate, attestation = query_correction_pair(root)
    tantivy = root / "tantivy"
    tantivy_builder.build_tantivy(
        jobs_csv=jobs,
        output=tantivy,
        artifact_prefix=tantivy_builder.DEFAULT_ARTIFACT_PREFIX,
        location_code_field=tantivy_builder.DEFAULT_LOCATION_CODE_FIELD,
        location_term_field=tantivy_builder.DEFAULT_LOCATION_TERM_FIELD,
        duty_code_field=tantivy_builder.DEFAULT_DUTY_CODE_FIELD,
        duty_term_field=tantivy_builder.DEFAULT_DUTY_TERM_FIELD,
        visibility_field=tantivy_builder.DEFAULT_VISIBILITY_FIELD,
        modified_at_field=tantivy_builder.DEFAULT_MODIFIED_AT_FIELD,
        correction_candidate_path=candidate,
        correction_attestation_path=attestation,
    )
    approvals = {
        "approved_tantivy_component_sha256": pipeline.sha256_file(tantivy / "manifest.json"),
        "approved_tantivy_build_sha256": pipeline.sha256_file(tantivy / "build-manifest.json"),
        "approved_tantivy_index_sha256": cast(
            str, json.loads((tantivy / "manifest.json").read_text())["index_sha256"]
        ),
    }
    return whole, inventory, tantivy, approvals


def materialize_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    corrections: bool = False,
) -> tuple[Path, Path, Path, dict[str, str]]:
    whole, inventory, tantivy, approvals = source_fixture(
        tmp_path, monkeypatch, corrections=corrections
    )
    output = tmp_path / "runtime-source"
    materializer.materialize(
        whole_build_root=whole,
        whole_source_inventory=inventory,
        tantivy_build_root=tantivy,
        output_root=output,
        **approvals,
    )
    return output, whole, tantivy, approvals


def promote_locally(
    output: Path,
    approvals: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, object], dict[str, bytes]]:
    monkeypatch.setattr(
        promotion,
        "APPROVED_TANTIVY_BUILD_MANIFEST_SHA256",
        approvals["approved_tantivy_build_sha256"],
    )
    monkeypatch.setattr(
        promotion,
        "APPROVED_TANTIVY_INDEX_SHA256",
        approvals["approved_tantivy_index_sha256"],
    )
    source = json.loads((output / "manifest.json").read_text())
    spec = json.loads((output / "runtime-release-spec.json").read_text())
    selected = promotion.select_artifacts(source, spec)
    documents = promotion.load_component_documents(spec, selected, output)
    runtime, _ = promotion.build_manifest(
        source,
        spec,
        documents,
        pipeline.sha256_file(output / "runtime-release-spec.json"),
    )
    return runtime, documents


def test_materializer_reuses_sealed_whole_and_round_trips_current_tantivy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, whole_source, _tantivy, approvals = materialize_fixture(tmp_path, monkeypatch)
    whole_path = output / materializer.WHOLE_DESTINATION / "manifest.json"
    temporal_path = output / materializer.TANTIVY_DESTINATION / "manifest.json"
    whole = json.loads(whole_path.read_text())
    temporal = json.loads(temporal_path.read_text())
    source_sha = pipeline.sha256_file(output / "manifest.json")
    release_spec = json.loads((output / "runtime-release-spec.json").read_text())

    assert release_spec["source_manifest"] == {
        "key": f"one111-search/materialized/{source_sha}/manifest.json",
        "sha256": source_sha,
    }
    assert whole["document_policy_version"] == "2026-07-24-clean-v1"
    assert len(whole["document_fields"]) == 15
    assert whole["source_manifest_sha256"] == promotion.APPROVED_WHOLE_SOURCE_MANIFEST_SHA256
    assert whole["source_inventory_sha256"] == promotion.APPROVED_WHOLE_SOURCE_INVENTORY_SHA256
    assert temporal["query_corrections"] == {"enabled": False}
    assert temporal["index_directory"] == "indexes/tantivy-bm25-temporal-v2/index"
    source_vectors = np.load(whole_source / "embeddings-00000.f16.npy", allow_pickle=False)
    derived_path = output / "runtime" / cast(str, whole["shards"][0]["vectors_path"])
    derived = np.load(derived_path, allow_pickle=False)
    assert source_vectors.shape == (2, 4096)
    assert derived.shape == (2, 1024)
    assert np.allclose(np.linalg.norm(derived.astype(np.float32), axis=1), 1.0, atol=2e-3)
    assert whole["shards"][0]["vectors_sha256"] == pipeline.sha256_file(derived_path)
    assert whole["shards"][0]["source_vectors_sha256"] == pipeline.sha256_file(
        whole_source / "embeddings-00000.f16.npy"
    )

    runtime, _documents = promote_locally(output, approvals, monkeypatch)
    runtime_path = output / "runtime-manifest.json"
    write_json(runtime_path, runtime)
    parsed = RuntimeManifest.from_path(runtime_path)
    whole_layout = WholeEmbeddingLayout.from_path(
        output / "runtime" / parsed.whole_embedding.manifest_path, parsed
    )
    tantivy_layout = TantivyLayout.from_path(
        output / "runtime" / parsed.temporal_tantivy.manifest_path, parsed
    )
    assert len(whole_layout.shards) == 2
    assert tantivy_layout.query_corrections_path is None


def test_materializer_round_trips_attested_query_corrections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, _whole, _tantivy, approvals = materialize_fixture(
        tmp_path, monkeypatch, corrections=True
    )

    runtime, _documents = promote_locally(output, approvals, monkeypatch)
    temporal = json.loads((output / materializer.TANTIVY_DESTINATION / "manifest.json").read_text())
    correction = temporal["query_corrections"]
    assert correction["enabled"] is True
    assert runtime["artifacts"][correction["artifact_path"]]["kind"] == "index"
    assert runtime["artifacts"][correction["promotion_attestation_path"]]["kind"] == "evidence"


def test_materializer_rejects_query_correction_attestation_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    whole, inventory, tantivy, approvals = source_fixture(tmp_path, monkeypatch, corrections=True)
    attestation = tantivy / "query-corrections.attestation.json"
    value = json.loads(attestation.read_text())
    value["absolute_delta"] = -0.001
    write_json(attestation, value)

    with pytest.raises(RuntimeError, match="query correction bytes differ"):
        materializer.materialize(
            whole_build_root=whole,
            whole_source_inventory=inventory,
            tantivy_build_root=tantivy,
            output_root=tmp_path / "output",
            **approvals,
        )


def test_materializer_rejects_unapproved_or_incomplete_source_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    whole, inventory, tantivy, approvals = source_fixture(tmp_path, monkeypatch)
    (whole / "parallel-extra.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="approved inventory"):
        materializer.materialize(
            whole_build_root=whole,
            whole_source_inventory=inventory,
            tantivy_build_root=tantivy,
            output_root=tmp_path / "output",
            **approvals,
        )


def test_materializer_rejects_tantivy_v1_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    whole, inventory, tantivy, approvals = source_fixture(tmp_path, monkeypatch)
    component_path = tantivy / "manifest.json"
    component = json.loads(component_path.read_text())
    component["index_directory"] = "indexes/tantivy-bm25-temporal-v1/index"
    write_json(component_path, component)
    approvals["approved_tantivy_component_sha256"] = pipeline.sha256_file(component_path)

    with pytest.raises(RuntimeError, match="component prefix"):
        materializer.materialize(
            whole_build_root=whole,
            whole_source_inventory=inventory,
            tantivy_build_root=tantivy,
            output_root=tmp_path / "output",
            **approvals,
        )


def test_materializer_rehashes_each_destination_after_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    whole, inventory, tantivy, approvals = source_fixture(tmp_path, monkeypatch)
    original = shutil.copyfile

    def corrupt_copy(
        source: object, destination: object, *args: object, **kwargs: object
    ) -> object:
        result = original(source, destination, *args, **kwargs)
        if Path(os.fspath(source)).name == "meta.json":
            Path(os.fspath(destination)).write_bytes(b"corrupted-after-copy")
        return result

    monkeypatch.setattr(materializer.shutil, "copyfile", corrupt_copy)
    with pytest.raises(RuntimeError, match="copied runtime artifact checksum differs"):
        materializer.materialize(
            whole_build_root=whole,
            whole_source_inventory=inventory,
            tantivy_build_root=tantivy,
            output_root=tmp_path / "output",
            **approvals,
        )
    assert not (tmp_path / "output").exists()


def test_publish_is_atomic_and_never_replaces_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "new").write_text("new", encoding="utf-8")
    (destination / "owner").write_text("existing", encoding="utf-8")

    with pytest.raises(RuntimeError, match="already exists"):
        materializer._publish_exclusive(source, destination)

    assert (destination / "owner").read_text(encoding="utf-8") == "existing"
    assert (source / "new").read_text(encoding="utf-8") == "new"
