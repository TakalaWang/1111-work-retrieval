from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import materialize_runtime_components as materializer  # noqa: E402
import promote_runtime_artifacts as promotion  # noqa: E402


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def tree_digest(path: Path) -> str:
    value = hashlib.sha256()
    for child in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        value.update(child.relative_to(path).as_posix().encode())
        value.update(b"\0")
        value.update(child.read_bytes())
    return value.hexdigest()


def source_fixture(root: Path) -> tuple[Path, Path, Path, Path, Path, str]:
    whole = root / "whole"
    ids = [["10", "20"], ["30"]]
    shards = []
    order = hashlib.sha256()
    for index, values in enumerate(ids):
        ids_path = whole / f"job-ids-{index:05d}.json"
        vector_path = whole / f"embeddings-{index:05d}.f16.npy"
        write_json(ids_path, values)
        vectors = (
            np.arange(len(values) * promotion.APPROVED_SOURCE_EMBEDDING_DIMENSION, dtype=np.float32)
            .reshape(len(values), promotion.APPROVED_SOURCE_EMBEDDING_DIMENSION)
            .__add__(1)
            .astype(np.float16)
        )
        np.save(vector_path, vectors, allow_pickle=False)
        for job_id in values:
            order.update(job_id.encode())
            order.update(b"\n")
        shards.append(
            {
                "index": index,
                "rows": len(values),
                "dimension": promotion.APPROVED_SOURCE_EMBEDDING_DIMENSION,
                "job_ids_sha256": digest("\n".join(values).encode()),
                "job_ids_file_sha256": digest(ids_path.read_bytes()),
                "embedding_sha256": digest(vector_path.read_bytes()),
            }
        )
    whole_manifest = {
        "complete": True,
        "model": promotion.APPROVED_MODEL,
        "revision": promotion.APPROVED_MODEL_REVISION,
        "dtype": "float16",
        "normalized": True,
        "document_policy_version": promotion.APPROVED_DOCUMENT_POLICY_VERSION,
        "document_template_sha256": promotion.APPROVED_DOCUMENT_TEMPLATE_SHA256,
        "document_fields": promotion.APPROVED_DOCUMENT_FIELDS,
        "dataset_sha256": "a" * 64,
        "jobs_sha256": "b" * 64,
        "job_row_order_sha256": order.hexdigest(),
        "rows": 3,
        "shards": shards,
    }
    write_json(whole / "manifest.json", whole_manifest)
    approved = digest((whole / "manifest.json").read_bytes())

    tantivy = root / "tantivy"
    (tantivy / "index").mkdir(parents=True)
    (tantivy / "index/meta.json").write_text('{"segments":[]}\n', encoding="utf-8")
    write_json(
        tantivy / "manifest.json",
        {
            "complete": True,
            "engine": promotion.APPROVED_TANTIVY_ENGINE,
            "jobs_sha256": "b" * 64,
            "job_row_order_sha256": order.hexdigest(),
            "updated_at_field": "updated_at_epoch_ms",
            "filter_semantics": promotion.TANTIVY_FILTER_SEMANTICS,
            "fields": promotion.APPROVED_TANTIVY_FIELD_BOOSTS,
            "document_policy_version": promotion.APPROVED_DOCUMENT_POLICY_VERSION,
            "lexical_policy_version": promotion.APPROVED_LEXICAL_POLICY_VERSION,
            "lexical_policy_sha256": promotion.APPROVED_LEXICAL_POLICY_SHA256,
            "tokenizers": promotion.APPROVED_TANTIVY_TOKENIZERS,
            "source_fields": promotion.APPROVED_TANTIVY_SOURCE_FIELDS,
            "index_sha256": tree_digest(tantivy / "index"),
        },
    )
    city = root / "cities.csv"
    city.write_text(
        "CodeNo,CodeNameA,CodeNameB,CodeNameC\n100100,台北市,台北地區,台灣\n",
        encoding="utf-8",
    )
    duty = root / "duties.csv"
    duty.write_text(
        "CodeNo,CodeNameA,CodeNameB,CodeNameC\n140200,軟體工程師,資訊軟體,資訊科技\n",
        encoding="utf-8",
    )
    corrections = root / "query-corrections.json"
    write_json(
        corrections,
        {
            "schema_version": 1,
            "source_policy": "train_jd_only",
            "train_cutoff_exclusive": promotion.APPROVED_GRAPH_TRAIN_CUTOFF,
            "max_source_timestamp": promotion.APPROVED_GRAPH_MAX_SOURCE_TIMESTAMP,
            "corrections": {"sofware": "software"},
        },
    )
    return whole, tantivy, city, duty, corrections, approved


def approved_sources(
    whole: Path, tantivy: Path, city: Path, duty: Path, corrections: Path
) -> dict[str, str]:
    return {
        "approved_whole_build_sha256": digest((whole / "manifest.json").read_bytes()),
        "approved_tantivy_build_sha256": digest((tantivy / "manifest.json").read_bytes()),
        "approved_tantivy_index_sha256": tree_digest(tantivy / "index"),
        "approved_city_taxonomy_sha256": digest(city.read_bytes()),
        "approved_duty_taxonomy_sha256": digest(duty.read_bytes()),
        "approved_query_corrections_sha256": digest(corrections.read_bytes()),
    }


def test_materializer_fails_closed_on_unapproved_eva_manifest(tmp_path: Path) -> None:
    whole, tantivy, city, duty, corrections, _ = source_fixture(tmp_path)
    approvals = approved_sources(whole, tantivy, city, duty, corrections)
    approvals["approved_whole_build_sha256"] = "0" * 64

    with pytest.raises(RuntimeError, match="approved EVA artifact"):
        materializer.materialize(
            whole_build_root=whole,
            tantivy_build_root=tantivy,
            city_taxonomy_csv=city,
            duty_taxonomy_csv=duty,
            query_corrections_json=corrections,
            output_root=tmp_path / "output",
            source_manifest_key="source/hash/manifest.json",
            **approvals,
        )


def test_materializer_emits_core_exact_components_and_release_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    whole, tantivy, city, duty, corrections, approved = source_fixture(tmp_path)
    output = tmp_path / "output"
    approvals = approved_sources(whole, tantivy, city, duty, corrections)

    materializer.materialize(
        whole_build_root=whole,
        tantivy_build_root=tantivy,
        city_taxonomy_csv=city,
        duty_taxonomy_csv=duty,
        query_corrections_json=corrections,
        output_root=output,
        source_manifest_key="source/hash/manifest.json",
        **approvals,
    )

    whole_layout = json.loads(
        (output / materializer.WHOLE_DESTINATION / "manifest.json").read_text()
    )
    tantivy_layout = json.loads(
        (output / materializer.TANTIVY_DESTINATION / "manifest.json").read_text()
    )
    taxonomy = json.loads(
        (output / materializer.TANTIVY_DESTINATION / "filter-taxonomy.json").read_text()
    )
    source_manifest = json.loads((output / "manifest.json").read_text())
    release_spec = json.loads((output / "runtime-release-spec.json").read_text())

    assert set(whole_layout) == {
        "schema_version",
        "complete",
        "model",
        "revision",
        "source_dimension",
        "dimension",
        "projection",
        "dtype",
        "normalized",
        "rows",
        "dataset_sha256",
        "jobs_sha256",
        "job_row_order_sha256",
        "document_policy_version",
        "document_template_sha256",
        "document_fields",
        "query_prompt",
        "build_manifest_path",
        "build_manifest_sha256",
        "job_ids_path",
        "shards",
    }
    assert set(tantivy_layout) == {
        "schema_version",
        "complete",
        "engine",
        "jobs_sha256",
        "job_row_order_sha256",
        "index_sha256",
        "index_directory",
        "index_files",
        "taxonomy_path",
        "job_ids_path",
        "query_corrections_path",
        "build_manifest_path",
        "build_manifest_sha256",
        "schema_fields",
        "field_boosts",
        "lexical_policy_version",
        "lexical_policy_sha256",
        "tokenizers",
        "source_fields",
        "filter_semantics",
        "updated_at_field",
        "temporal_filter_semantics",
    }
    assert json.loads((output / materializer.WHOLE_DESTINATION / "job-ids.json").read_text()) == [
        "10",
        "20",
        "30",
    ]
    assert taxonomy["location_code_to_terms"]["100100"] == ["台北市", "台北地區", "台灣"]
    assert release_spec["source_manifest"]["sha256"] == digest(
        (output / "manifest.json").read_bytes()
    )
    assert any(
        item["path"] == promotion.WHOLE_BUILD_PROVENANCE_SOURCE_PATH and item["sha256"] == approved
        for item in source_manifest["files"]
    )
    assert not os.path.samefile(
        whole / "embeddings-00000.f16.npy",
        output / materializer.WHOLE_DESTINATION / "shards/00000.f16.npy",
    )
    projected = np.load(
        output / materializer.WHOLE_DESTINATION / "shards/00000.f16.npy",
        allow_pickle=False,
    )
    assert projected.shape == (2, promotion.APPROVED_WHOLE_DIMENSION)
    assert np.allclose(np.linalg.norm(projected.astype(np.float32), axis=1), 1.0, atol=2e-3)
    monkeypatch.setattr(
        promotion,
        "APPROVED_WHOLE_BUILD_MANIFEST_SHA256",
        approvals["approved_whole_build_sha256"],
    )
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
    monkeypatch.setattr(
        promotion,
        "APPROVED_CITY_TAXONOMY_SHA256",
        approvals["approved_city_taxonomy_sha256"],
    )
    monkeypatch.setattr(
        promotion,
        "APPROVED_DUTY_TAXONOMY_SHA256",
        approvals["approved_duty_taxonomy_sha256"],
    )
    selected = promotion.select_artifacts(source_manifest, release_spec)
    documents = promotion.load_component_documents(release_spec, selected, output)
    runtime, _ = promotion.build_manifest(
        source_manifest,
        release_spec,
        documents,
        digest((output / "runtime-release-spec.json").read_bytes()),
    )
    assert runtime["incumbents"]["whole_embedding"]["dimension"] == 1024


def test_materializer_rejects_taxonomy_outside_pinned_lineage(tmp_path: Path) -> None:
    whole, tantivy, city, duty, corrections, _ = source_fixture(tmp_path)
    approvals = approved_sources(whole, tantivy, city, duty, corrections)
    replacement = tmp_path / "replacement-city.csv"
    replacement.write_text(
        "CodeNo,CodeNameA,CodeNameB,CodeNameC\n999999,偽造城市,偽造地區,偽造資料\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="approved city taxonomy"):
        materializer.materialize(
            whole_build_root=whole,
            tantivy_build_root=tantivy,
            city_taxonomy_csv=replacement,
            duty_taxonomy_csv=duty,
            query_corrections_json=corrections,
            output_root=tmp_path / "output",
            source_manifest_key="source/hash/manifest.json",
            **approvals,
        )


def test_materializer_rejects_tantivy_build_outside_pinned_lineage(tmp_path: Path) -> None:
    whole, tantivy, city, duty, corrections, _ = source_fixture(tmp_path)
    approvals = approved_sources(whole, tantivy, city, duty, corrections)
    manifest_path = tantivy / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["unexpected"] = True
    write_json(manifest_path, manifest)

    with pytest.raises(RuntimeError, match="approved Tantivy build manifest"):
        materializer.materialize(
            whole_build_root=whole,
            tantivy_build_root=tantivy,
            city_taxonomy_csv=city,
            duty_taxonomy_csv=duty,
            query_corrections_json=corrections,
            output_root=tmp_path / "output",
            source_manifest_key="source/hash/manifest.json",
            **approvals,
        )


def test_materializer_rejects_post_cutoff_query_corrections(tmp_path: Path) -> None:
    whole, tantivy, city, duty, corrections, _ = source_fixture(tmp_path)
    value = json.loads(corrections.read_text())
    value["max_source_timestamp"] = value["train_cutoff_exclusive"]
    write_json(corrections, value)

    with pytest.raises(RuntimeError, match="post-cutoff"):
        materializer.materialize(
            whole_build_root=whole,
            tantivy_build_root=tantivy,
            city_taxonomy_csv=city,
            duty_taxonomy_csv=duty,
            query_corrections_json=corrections,
            output_root=tmp_path / "output",
            source_manifest_key="source/hash/manifest.json",
            **approved_sources(whole, tantivy, city, duty, corrections),
        )


def test_materializer_rehashes_each_destination_after_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    whole, tantivy, city, duty, corrections, _ = source_fixture(tmp_path)
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
            tantivy_build_root=tantivy,
            city_taxonomy_csv=city,
            duty_taxonomy_csv=duty,
            query_corrections_json=corrections,
            output_root=tmp_path / "output",
            source_manifest_key="source/hash/manifest.json",
            **approved_sources(whole, tantivy, city, duty, corrections),
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
