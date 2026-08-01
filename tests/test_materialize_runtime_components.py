from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

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


def source_fixture(root: Path) -> tuple[Path, Path, Path, Path, str]:
    whole = root / "whole"
    ids = [["10", "20"], ["30"]]
    shards = []
    order = hashlib.sha256()
    for index, values in enumerate(ids):
        ids_path = whole / f"job-ids-{index:05d}.json"
        vector_path = whole / f"embeddings-{index:05d}.f16.npy"
        write_json(ids_path, values)
        vector_path.write_bytes(f"vectors-{index}".encode())
        for job_id in values:
            order.update(job_id.encode())
            order.update(b"\n")
        shards.append(
            {
                "index": index,
                "rows": len(values),
                "dimension": 4096,
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
    return whole, tantivy, city, duty, approved


def test_materializer_fails_closed_on_unapproved_eva_manifest(tmp_path: Path) -> None:
    whole, tantivy, city, duty, _ = source_fixture(tmp_path)

    with pytest.raises(RuntimeError, match="approved EVA artifact"):
        materializer.materialize(
            whole_build_root=whole,
            tantivy_build_root=tantivy,
            city_taxonomy_csv=city,
            duty_taxonomy_csv=duty,
            output_root=tmp_path / "output",
            source_manifest_key="source/hash/manifest.json",
            link_mode="copy",
        )


def test_materializer_emits_core_exact_components_and_release_spec(tmp_path: Path) -> None:
    whole, tantivy, city, duty, approved = source_fixture(tmp_path)
    output = tmp_path / "output"

    materializer.materialize(
        whole_build_root=whole,
        tantivy_build_root=tantivy,
        city_taxonomy_csv=city,
        duty_taxonomy_csv=duty,
        output_root=output,
        source_manifest_key="source/hash/manifest.json",
        link_mode="copy",
        approved_whole_build_sha256=approved,
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
        "dimension",
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
        "schema_fields",
        "field_boosts",
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
        item["path"] == promotion.APPROVED_WHOLE_BUILD_PROVENANCE_PATH
        and item["sha256"] == approved
        for item in source_manifest["files"]
    )
