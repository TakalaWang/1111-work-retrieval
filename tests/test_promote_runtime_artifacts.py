from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import promote_runtime_artifacts as promotion  # noqa: E402
import validate_runtime_manifest_file as runtime_file_validation  # noqa: E402

SCHEMA = json.loads(
    (ROOT / "packages/contract/runtime-manifest.schema.json").read_text(encoding="utf-8")
)
HEX = {
    "a": "a" * 64,
    "b": "b" * 64,
    "c": "c" * 64,
    "d": "d" * 64,
    "e": "e" * 64,
    "f": "f" * 64,
}


def encoded(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def base_documents() -> dict[str, bytes]:
    whole_manifest_path = f"{promotion.WHOLE_RUNTIME_PREFIX}/manifest.json"
    vectors_path = f"{promotion.WHOLE_RUNTIME_PREFIX}/shards/00000.f16.npy"
    job_ids_path = f"{promotion.WHOLE_RUNTIME_PREFIX}/job-ids.json"
    vectors = b"verified-vectors"
    job_ids = b'["1"]\n'
    source_manifest = {
        "schema_version": 1,
        "complete": True,
        "model": promotion.APPROVED_MODEL,
        "revision": promotion.APPROVED_MODEL_REVISION,
        "rows": 1_218_635,
        "dataset_sha256": promotion.APPROVED_JOBS_DATASET_SHA256,
        "jobs_sha256": HEX["a"],
        "job_row_order_sha256": HEX["b"],
        "dimension": promotion.APPROVED_SOURCE_EMBEDDING_DIMENSION,
        "dtype": "float16",
        "normalized": True,
        "document_policy_version": promotion.APPROVED_DOCUMENT_POLICY_VERSION,
        "document_template_sha256": promotion.APPROVED_DOCUMENT_TEMPLATE_SHA256,
        "document_fields": promotion.APPROVED_DOCUMENT_FIELDS,
        "query_prompt": promotion.APPROVED_QUERY_PROMPT,
        "job_ids_path": "artifacts/experiments/qwen3-8b/full/job-ids.json",
        "shards": [
            {
                "index": 0,
                "vectors_path": "artifacts/experiments/qwen3-8b/full/shards/00000.f16.npy",
                "rows": 1_218_635,
                "dimension": promotion.APPROVED_SOURCE_EMBEDDING_DIMENSION,
                "sha256": HEX["c"],
            }
        ],
    }
    source_manifest_payload = encoded(source_manifest)
    source_inventory = {
        "schema_version": 3,
        "files": [
            {
                "path": "artifacts/experiments/qwen3-8b/full/manifest.json",
                "sha256": digest(source_manifest_payload),
                "size": len(source_manifest_payload),
            }
        ],
    }
    source_inventory_payload = encoded(source_inventory)
    whole = {
        "schema_version": 1,
        "complete": True,
        "model": "Qwen/Qwen3-Embedding-8B",
        "revision": "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af",
        "source_dimension": promotion.APPROVED_SOURCE_EMBEDDING_DIMENSION,
        "dimension": promotion.APPROVED_WHOLE_DIMENSION,
        "projection": promotion.APPROVED_WHOLE_PROJECTION,
        "dtype": "float16",
        "normalized": True,
        "rows": 1_218_635,
        "dataset_sha256": promotion.APPROVED_JOBS_DATASET_SHA256,
        "jobs_sha256": HEX["a"],
        "job_row_order_sha256": HEX["b"],
        "document_policy_version": promotion.APPROVED_DOCUMENT_POLICY_VERSION,
        "document_template_sha256": promotion.APPROVED_DOCUMENT_TEMPLATE_SHA256,
        "document_fields": promotion.APPROVED_DOCUMENT_FIELDS,
        "query_prompt": promotion.APPROVED_QUERY_PROMPT,
        "source_manifest_path": promotion.APPROVED_WHOLE_SOURCE_MANIFEST_PATH,
        "source_manifest_sha256": digest(source_manifest_payload),
        "source_inventory_path": promotion.APPROVED_WHOLE_SOURCE_INVENTORY_PATH,
        "source_inventory_sha256": digest(source_inventory_payload),
        "job_ids_path": job_ids_path,
        "shards": [
            {
                "vectors_path": vectors_path,
                "row_start": 0,
                "row_end": 1_218_635,
                "rows": 1_218_635,
                "dimension": promotion.APPROVED_WHOLE_DIMENSION,
                "vectors_sha256": digest(vectors),
                "source_vectors_sha256": HEX["c"],
            }
        ],
    }
    index_path = f"{promotion.TANTIVY_RUNTIME_PREFIX}/index/meta.json"
    taxonomy_path = f"{promotion.TANTIVY_RUNTIME_PREFIX}/filter-taxonomy.json"
    index_file = b'{"segments":[]}\n'
    taxonomy = encoded(
        {
            "schema_version": 1,
            "location_code_to_terms": {"100100": ["台北市"]},
            "duty_code_to_terms": {"140200": ["軟體工程師"]},
        }
    )
    index_tree = [
        {
            "path": "meta.json",
            "sha256": digest(index_file),
            "size_bytes": len(index_file),
        }
    ]
    index_sha256 = promotion._canonical_sha256(index_tree)
    query_corrections = {"enabled": False}
    tantivy_build = {
        "schema_version": 1,
        "complete": True,
        "builder": "tantivy_index_pipeline.py",
        "engine": promotion.APPROVED_TANTIVY_ENGINE,
        "dataset_sha256": promotion.APPROVED_JOBS_DATASET_SHA256,
        "jobs_sha256": HEX["a"],
        "job_row_order_sha256": HEX["b"],
        "rows": 1_218_635,
        "index_sha256": index_sha256,
        "index_tree": index_tree,
        "taxonomy_sha256": digest(taxonomy),
        "query_corrections": query_corrections,
        "lexical_policy_version": promotion.APPROVED_LEXICAL_POLICY_VERSION,
        "lexical_policy_sha256": promotion.APPROVED_LEXICAL_POLICY_SHA256,
        "tokenizers": promotion.APPROVED_TANTIVY_TOKENIZERS,
        "source_fields": promotion.APPROVED_TANTIVY_SOURCE_FIELDS,
        "source_csv_fields": {"title": "職務名稱"},
        "salary_filter_excluded_rows": 0,
    }
    tantivy_build_payload = encoded(tantivy_build)
    temporal = {
        "schema_version": 1,
        "complete": True,
        "engine": "tantivy v0.26.0, index_format v7",
        "jobs_sha256": HEX["a"],
        "job_row_order_sha256": HEX["b"],
        "index_sha256": index_sha256,
        "updated_at_field": "updated_at_epoch_ms",
        "filter_semantics": promotion.TANTIVY_FILTER_SEMANTICS,
        "temporal_filter_semantics": promotion.TEMPORAL_FILTER_SEMANTICS,
        "index_directory": f"{promotion.TANTIVY_RUNTIME_PREFIX}/index",
        "index_files": [index_path],
        "taxonomy_path": taxonomy_path,
        "job_ids_path": promotion.TANTIVY_JOB_IDS_RUNTIME_PATH,
        "query_corrections": query_corrections,
        "build_manifest_path": promotion.APPROVED_TANTIVY_BUILD_PROVENANCE_PATH,
        "build_manifest_sha256": digest(tantivy_build_payload),
        "schema_fields": promotion.APPROVED_TANTIVY_SCHEMA_FIELDS,
        "field_boosts": promotion.APPROVED_TANTIVY_FIELD_BOOSTS,
        "lexical_policy_version": promotion.APPROVED_LEXICAL_POLICY_VERSION,
        "lexical_policy_sha256": promotion.APPROVED_LEXICAL_POLICY_SHA256,
        "tokenizers": promotion.APPROVED_TANTIVY_TOKENIZERS,
        "source_fields": promotion.APPROVED_TANTIVY_SOURCE_FIELDS,
    }
    documents = {
        whole_manifest_path: encoded(whole),
        vectors_path: vectors,
        job_ids_path: job_ids,
        f"{promotion.TANTIVY_RUNTIME_PREFIX}/manifest.json": encoded(temporal),
        index_path: index_file,
        taxonomy_path: taxonomy,
        promotion.TANTIVY_JOB_IDS_RUNTIME_PATH: job_ids,
        promotion.APPROVED_WHOLE_SOURCE_MANIFEST_PATH: source_manifest_payload,
        promotion.APPROVED_WHOLE_SOURCE_INVENTORY_PATH: source_inventory_payload,
        promotion.APPROVED_TANTIVY_BUILD_PROVENANCE_PATH: tantivy_build_payload,
    }
    documents[promotion.MATERIALIZATION_REPORT_PATH] = encoded(
        {
            "schema_version": 1,
            "whole_source_manifest_sha256": digest(source_manifest_payload),
            "whole_source_inventory_sha256": digest(source_inventory_payload),
            "whole_runtime_manifest_sha256": digest(documents[whole_manifest_path]),
            "projection": promotion.APPROVED_WHOLE_PROJECTION,
            "tantivy_build_manifest_sha256": digest(tantivy_build_payload),
            "tantivy_runtime_manifest_sha256": digest(
                documents[f"{promotion.TANTIVY_RUNTIME_PREFIX}/manifest.json"]
            ),
            "tantivy_index_sha256": index_sha256,
            "dataset_sha256": promotion.APPROVED_JOBS_DATASET_SHA256,
            "jobs_sha256": HEX["a"],
            "job_row_order_sha256": HEX["b"],
            "rows": 1_218_635,
            "placement": "copy_sha256_verified",
            "query_corrections": query_corrections,
        }
    )
    return documents


@pytest.fixture(autouse=True)
def approved_lineage(monkeypatch: pytest.MonkeyPatch) -> None:
    documents = base_documents()
    source_manifest = documents[promotion.APPROVED_WHOLE_SOURCE_MANIFEST_PATH]
    source_inventory = json.loads(documents[promotion.APPROVED_WHOLE_SOURCE_INVENTORY_PATH])
    source_cache_files = source_inventory["files"]
    tantivy_build = documents[promotion.APPROVED_TANTIVY_BUILD_PROVENANCE_PATH]
    temporal = json.loads(documents[f"{promotion.TANTIVY_RUNTIME_PREFIX}/manifest.json"])
    monkeypatch.setattr(promotion, "APPROVED_WHOLE_SOURCE_MANIFEST_SHA256", digest(source_manifest))
    monkeypatch.setattr(
        promotion,
        "APPROVED_WHOLE_SOURCE_INVENTORY_SHA256",
        digest(documents[promotion.APPROVED_WHOLE_SOURCE_INVENTORY_PATH]),
    )
    monkeypatch.setattr(promotion, "APPROVED_WHOLE_SOURCE_FILE_COUNT", 1)
    monkeypatch.setattr(promotion, "APPROVED_WHOLE_SOURCE_SHARDS", 1)
    monkeypatch.setattr(
        promotion,
        "APPROVED_WHOLE_SOURCE_BYTES",
        sum(item["size"] for item in source_cache_files),
    )
    monkeypatch.setattr(promotion, "APPROVED_TANTIVY_BUILD_MANIFEST_SHA256", digest(tantivy_build))
    monkeypatch.setattr(promotion, "APPROVED_TANTIVY_INDEX_SHA256", temporal["index_sha256"])


def base_source(documents: dict[str, bytes]) -> dict[str, object]:
    whole_prefix = promotion.WHOLE_RUNTIME_PREFIX
    tantivy_prefix = promotion.TANTIVY_RUNTIME_PREFIX
    paths = {
        f"{whole_prefix}/manifest.json": (
            f"runtime/{whole_prefix}/manifest.json",
            "embedding",
        ),
        f"{whole_prefix}/shards/00000.f16.npy": (
            f"runtime/{whole_prefix}/shards/00000.f16.npy",
            "embedding",
        ),
        f"{whole_prefix}/job-ids.json": (
            f"runtime/{whole_prefix}/job-ids.json",
            "embedding",
        ),
        f"{tantivy_prefix}/manifest.json": (
            f"runtime/{tantivy_prefix}/manifest.json",
            "index",
        ),
        f"{tantivy_prefix}/index/meta.json": (
            f"runtime/{tantivy_prefix}/index/meta.json",
            "index",
        ),
        f"{tantivy_prefix}/filter-taxonomy.json": (
            f"runtime/{tantivy_prefix}/filter-taxonomy.json",
            "index",
        ),
        promotion.TANTIVY_JOB_IDS_RUNTIME_PATH: (
            f"runtime/{promotion.TANTIVY_JOB_IDS_RUNTIME_PATH}",
            "index",
        ),
        promotion.MATERIALIZATION_REPORT_PATH: (
            f"runtime/{promotion.MATERIALIZATION_REPORT_PATH}",
            "evidence",
        ),
        promotion.APPROVED_WHOLE_SOURCE_MANIFEST_PATH: (
            promotion.WHOLE_SOURCE_MANIFEST_SOURCE_PATH,
            "evidence",
        ),
        promotion.APPROVED_WHOLE_SOURCE_INVENTORY_PATH: (
            promotion.WHOLE_SOURCE_INVENTORY_SOURCE_PATH,
            "evidence",
        ),
        promotion.APPROVED_TANTIVY_BUILD_PROVENANCE_PATH: (
            promotion.TANTIVY_BUILD_PROVENANCE_SOURCE_PATH,
            "evidence",
        ),
    }
    files = [
        {
            "path": source_path,
            "sha256": digest(documents[destination_path]),
            "size": len(documents[destination_path]),
        }
        for destination_path, (source_path, _) in paths.items()
    ]
    files.append(
        {
            "path": "artifacts/production/query-history/answers.sqlite3",
            "sha256": HEX["f"],
            "size": 99,
        }
    )
    return {"schema_version": 3, "files": files}


def base_spec(source: dict[str, object], documents: dict[str, bytes]) -> dict[str, object]:
    whole_prefix = promotion.WHOLE_RUNTIME_PREFIX
    tantivy_prefix = promotion.TANTIVY_RUNTIME_PREFIX
    selections = [
        {
            "source_prefix": f"runtime/{whole_prefix}/",
            "destination_prefix": f"{whole_prefix}/",
            "kind": "embedding",
        },
        {
            "source_prefix": f"runtime/{tantivy_prefix}/",
            "destination_prefix": f"{tantivy_prefix}/",
            "kind": "index",
        },
        {
            "source_prefix": "runtime/evidence/provenance/",
            "destination_prefix": "evidence/provenance/",
            "kind": "evidence",
        },
        {
            "source_prefix": "provenance/qwen3-embedding-8b-clean-v1/",
            "destination_prefix": f"{whole_prefix}/",
            "kind": "evidence",
        },
        {
            "source_prefix": "provenance/tantivy-bm25-temporal-v3/",
            "destination_prefix": f"{tantivy_prefix}/",
            "kind": "evidence",
        },
    ]
    expected_items = []
    for source_file in source["files"]:
        source_path = source_file["path"]
        for rule in selections:
            if source_path.startswith(rule["source_prefix"]):
                expected_items.append(
                    {
                        "source_path": source_path,
                        "path": rule["destination_prefix"]
                        + source_path.removeprefix(rule["source_prefix"]),
                        "kind": rule["kind"],
                        "sha256": source_file["sha256"],
                        "size_bytes": source_file["size"],
                    }
                )
                break
    expected_items.sort(key=lambda item: item["path"])
    whole = json.loads(documents[f"{whole_prefix}/manifest.json"])
    temporal = json.loads(documents[f"{tantivy_prefix}/manifest.json"])
    return {
        "schema_version": 1,
        "source_manifest": {
            "key": "bundle/source-sha/manifest.json",
            "sha256": digest(encoded(source)),
        },
        "selected_inventory_sha256": digest(
            json.dumps(expected_items, separators=(",", ":"), sort_keys=True).encode()
        ),
        "selections": selections,
        "runtime": {
            "retrieval_policy": {
                "as_of": {
                    "production_mode": "request_time",
                    "demo_reference": "2026-06-08T23:59:59.999+08:00",
                },
                "eligibility": {
                    "updated_within_days": 180,
                    "future_jobs": "retained_with_zero_freshness",
                    "stale_jobs": "exclude",
                    "applied_before_retrieval": True,
                },
            },
            "incumbents": {
                "whole_embedding": {
                    "manifest_path": f"{whole_prefix}/manifest.json",
                    "manifest_sha256": digest(documents[f"{whole_prefix}/manifest.json"]),
                    "complete": True,
                    "model": whole["model"],
                    "revision": whole["revision"],
                    "source_dimension": whole["source_dimension"],
                    "dimension": whole["dimension"],
                    "projection": whole["projection"],
                    "dtype": whole["dtype"],
                    "normalized": whole["normalized"],
                    "rows": whole["rows"],
                    "dataset_sha256": whole["dataset_sha256"],
                    "jobs_sha256": whole["jobs_sha256"],
                    "job_row_order_sha256": whole["job_row_order_sha256"],
                    "document_policy_version": whole["document_policy_version"],
                    "document_template_sha256": whole["document_template_sha256"],
                },
                "temporal_tantivy": {
                    "manifest_path": f"{tantivy_prefix}/manifest.json",
                    "manifest_sha256": digest(documents[f"{tantivy_prefix}/manifest.json"]),
                    "complete": True,
                    "index_sha256": temporal["index_sha256"],
                    "engine": temporal["engine"],
                    "jobs_sha256": temporal["jobs_sha256"],
                    "job_row_order_sha256": temporal["job_row_order_sha256"],
                    "updated_at_field": temporal["updated_at_field"],
                    "hard_filters": True,
                    "temporal_filter_semantics": temporal["temporal_filter_semantics"],
                },
            },
            "challengers": {
                name: {"enabled": False}
                for name in (
                    "multiview_embedding",
                    "skill_graph",
                    "semantic_reranker",
                    "learning_to_rank",
                    "guardrails",
                )
            },
        },
    }


@pytest.fixture
def release_fixture() -> tuple[dict[str, object], dict[str, object], dict[str, bytes]]:
    documents = base_documents()
    source = base_source(documents)
    spec = base_spec(source, documents)
    return source, spec, documents


def validate_schema(manifest: dict[str, object]) -> None:
    Draft202012Validator(SCHEMA, format_checker=FormatChecker()).validate(manifest)


def reseal(manifest: dict[str, object]) -> None:
    artifacts = manifest["artifacts"]
    manifest["release"]["object_count"] = len(artifacts)
    manifest["release"]["size_bytes"] = sum(value["size_bytes"] for value in artifacts.values())
    manifest["release"]["artifact_inventory_sha256"] = digest(
        json.dumps(artifacts, separators=(",", ":"), sort_keys=True).encode()
    )


def enable_ltr(
    manifest: dict[str, object],
    documents: dict[str, bytes],
    *,
    delta: float,
    report: dict[str, object],
) -> None:
    component_path = "rankers/ltr/manifest.json"
    model_path = "rankers/ltr/model.bin"
    report_path = "evidence/ltr/report.json"
    model_payload = b"verified-ltr"
    report_payload = encoded(report)
    component_payload = encoded(
        {
            "complete": True,
            "publication_allowed": True,
            "promotion_report_sha256": digest(report_payload),
            "files": [
                {
                    "path": model_path,
                    "sha256": digest(model_payload),
                    "size_bytes": len(model_payload),
                }
            ],
        }
    )
    manifest["artifacts"].update(
        {
            component_path: {
                "kind": "ranker",
                "sha256": digest(component_payload),
                "size_bytes": len(component_payload),
            },
            report_path: {
                "kind": "evidence",
                "sha256": digest(report_payload),
                "size_bytes": len(report_payload),
            },
            model_path: {
                "kind": "ranker",
                "sha256": digest(model_payload),
                "size_bytes": len(model_payload),
            },
        }
    )
    manifest["challengers"]["learning_to_rank"] = {
        "enabled": True,
        "complete": True,
        "publication_allowed": True,
        "manifest_path": component_path,
        "manifest_sha256": digest(component_payload),
        "promotion_evidence": {
            "decision": "accepted",
            "report_path": report_path,
            "report_sha256": digest(report_payload),
            "evaluation_split_sha256": HEX["c"],
            "baseline_run_sha256": HEX["d"],
            "candidate_run_sha256": HEX["e"],
            "primary_metric": "ndcg_at_10",
            "absolute_delta": delta,
        },
    }
    documents[component_path] = component_payload
    documents[report_path] = report_payload
    documents[model_path] = model_payload
    reseal(manifest)


def test_runtime_schema_is_valid_json_schema() -> None:
    Draft202012Validator.check_schema(SCHEMA)


def test_contract_matches_core_temporal_and_challenger_semantics() -> None:
    future_policy = SCHEMA["properties"]["retrieval_policy"]["properties"]["eligibility"][
        "properties"
    ]["future_jobs"]
    required = set(SCHEMA["properties"]["challengers"]["required"])
    guardrails = SCHEMA["properties"]["challengers"]["properties"]["guardrails"]
    temporal_semantics = SCHEMA["$defs"]["temporalTantivy"]["properties"][
        "temporal_filter_semantics"
    ]

    assert future_policy == {"const": "retained_with_zero_freshness"}
    assert temporal_semantics == {"const": promotion.TEMPORAL_FILTER_SEMANTICS}
    assert guardrails == {"$ref": "#/$defs/disabled"}
    assert (
        promotion.CHALLENGERS
        == required
        == {
            "multiview_embedding",
            "skill_graph",
            "semantic_reranker",
            "learning_to_rank",
            "guardrails",
        }
    )


def test_v2_manifest_is_canonical_and_schema_valid(release_fixture: object) -> None:
    source, spec, documents = release_fixture
    manifest, selected = promotion.build_manifest(
        source,
        spec,
        documents,
        release_spec_sha256=HEX["e"],
    )

    validate_schema(manifest)
    assert manifest["schema_version"] == 2
    assert manifest["release"]["complete"] is True
    assert manifest["release"]["publication_allowed"] is True
    assert manifest["release"]["object_count"] == len(selected) == 11
    assert all("history" not in str(item).lower() for item in selected)
    assert promotion.canonical_bytes(manifest) == promotion.canonical_bytes(
        dict(reversed(list(manifest.items())))
    )


@pytest.mark.parametrize(
    "path",
    [
        "/models/a",
        "models/../secret",
        "models//file",
        "runtime/file",
        "",
        "models/./file",
        "evidence/report.csv",
    ],
)
def test_runtime_paths_are_strictly_relative(path: str) -> None:
    with pytest.raises(RuntimeError, match="unsafe runtime artifact path"):
        promotion.validate_relative_path(path, "evidence" if path.startswith("evidence/") else None)


def test_multiview_cannot_publish_incomplete(release_fixture: object) -> None:
    source, spec, documents = release_fixture
    manifest, _ = promotion.build_manifest(source, spec, documents, HEX["e"])
    manifest["challengers"]["multiview_embedding"] = {
        "enabled": True,
        "complete": False,
        "publication_allowed": True,
    }

    with pytest.raises(ValidationError):
        validate_schema(manifest)
    with pytest.raises(RuntimeError, match="multi-view"):
        promotion.validate_runtime_manifest(manifest, documents)


def test_schema_accepts_publishable_multiview_while_graph_stays_disabled(
    release_fixture: object,
) -> None:
    source, spec, documents = release_fixture
    manifest, _ = promotion.build_manifest(source, spec, documents, HEX["e"])
    multiview_data_path = "embeddings/qwen3-embedding-8b/multiview-1024/vectors.f16.npy"
    graph_data_path = "graphs/skill-graph/graph.jsonl"
    multiview_data = b"multiview"
    graph_data = b"graph"
    mrl_report = encoded(
        {
            "stable_result_sha256": HEX["c"],
            "selected_dimension": 1024,
            "reference_dimension": 4096,
        }
    )
    mrl_evidence = {
        "decision": "accepted",
        "report_path": "evidence/qwen-mrl/report.json",
        "report_sha256": digest(mrl_report),
        "stable_result_sha256": HEX["c"],
        "selected_dimension": 1024,
        "reference_dimension": 4096,
    }
    multiview_promotion = encoded(
        {
            "schema_version": 1,
            "complete": True,
            "publication_allowed": True,
            "evaluation_split_sha256": HEX["a"],
            "baseline_run_sha256": HEX["b"],
            "candidate_run_sha256": HEX["c"],
            "primary_metric": "ndcg_at_10",
            "baseline_value": 0.2,
            "candidate_value": 0.201,
            "absolute_delta": 0.001,
        }
    )
    graph_promotion = encoded(
        {
            "schema_version": 1,
            "complete": True,
            "publication_allowed": True,
            "evaluation_split_sha256": HEX["d"],
            "baseline_run_sha256": HEX["e"],
            "candidate_run_sha256": HEX["f"],
            "primary_metric": "ndcg_at_10",
            "baseline_value": 0.3,
            "candidate_value": 0.302,
            "absolute_delta": 0.002,
        }
    )
    multiview_document = encoded(
        {
            "complete": True,
            "publication_allowed": True,
            "model": "Qwen/Qwen3-Embedding-8B",
            "revision": "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af",
            "model_contract_sha256": HEX["e"],
            "tokenizer_sha256": HEX["f"],
            "view_policy_sha256": HEX["a"],
            "dataset_sha256": promotion.APPROVED_JOBS_DATASET_SHA256,
            "output_dimension": 1024,
            "dtype": "float16",
            "normalized": True,
            "mrl_report_sha256": digest(mrl_report),
            "mrl_evidence": {
                key: value for key, value in mrl_evidence.items() if key != "report_path"
            },
            "view_policy": {"included_kinds": ["occupation", "skill", "requirement", "content"]},
            "files": [
                {
                    "path": multiview_data_path,
                    "sha256": digest(multiview_data),
                    "size_bytes": len(multiview_data),
                }
            ],
        }
    )
    graph_document = encoded(
        {
            "complete": True,
            "publication_allowed": True,
            "schema_version": 1,
            "train_cutoff_exclusive": "2026-06-08T00:00:00+08:00",
            "max_source_timestamp": promotion.APPROVED_GRAPH_MAX_SOURCE_TIMESTAMP,
            "source_jd_sha256": promotion.APPROVED_GRAPH_SOURCE_JD_SHA256,
            "source_policy": "train_jd_only",
            "test_jd_used": False,
            "files": [
                {
                    "path": graph_data_path,
                    "sha256": digest(graph_data),
                    "size_bytes": len(graph_data),
                }
            ],
        }
    )
    manifest["artifacts"].update(
        {
            "embeddings/qwen3-embedding-8b/multiview-1024/manifest.json": {
                "kind": "embedding",
                "sha256": digest(multiview_document),
                "size_bytes": len(multiview_document),
            },
            "evidence/qwen-mrl/report.json": {
                "kind": "evidence",
                "sha256": digest(mrl_report),
                "size_bytes": len(mrl_report),
            },
            "evidence/qwen-multiview-promotion/report.json": {
                "kind": "evidence",
                "sha256": digest(multiview_promotion),
                "size_bytes": len(multiview_promotion),
            },
            multiview_data_path: {
                "kind": "embedding",
                "sha256": digest(multiview_data),
                "size_bytes": len(multiview_data),
            },
            "graphs/skill-graph/manifest.json": {
                "kind": "graph",
                "sha256": digest(graph_document),
                "size_bytes": len(graph_document),
            },
            "evidence/skill-graph/report.json": {
                "kind": "evidence",
                "sha256": digest(graph_promotion),
                "size_bytes": len(graph_promotion),
            },
            graph_data_path: {
                "kind": "graph",
                "sha256": digest(graph_data),
                "size_bytes": len(graph_data),
            },
        }
    )
    manifest["challengers"]["multiview_embedding"] = {
        "enabled": True,
        "complete": True,
        "publication_allowed": True,
        "manifest_path": "embeddings/qwen3-embedding-8b/multiview-1024/manifest.json",
        "manifest_sha256": digest(multiview_document),
        "model": "Qwen/Qwen3-Embedding-8B",
        "revision": "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af",
        "model_contract_sha256": HEX["e"],
        "tokenizer_sha256": HEX["f"],
        "view_policy_sha256": HEX["a"],
        "dataset_sha256": promotion.APPROVED_JOBS_DATASET_SHA256,
        "output_dimension": 1024,
        "dtype": "float16",
        "normalized": True,
        "view_kinds": ["occupation", "skill", "requirement", "content"],
        "mrl_evidence": mrl_evidence,
        "promotion_evidence": {
            "decision": "accepted",
            "report_path": "evidence/qwen-multiview-promotion/report.json",
            "report_sha256": digest(multiview_promotion),
            "evaluation_split_sha256": HEX["a"],
            "baseline_run_sha256": HEX["b"],
            "candidate_run_sha256": HEX["c"],
            "primary_metric": "ndcg_at_10",
            "absolute_delta": 0.001,
        },
    }
    manifest["challengers"]["skill_graph"] = {
        "enabled": True,
        "complete": True,
        "publication_allowed": True,
        "manifest_path": "graphs/skill-graph/manifest.json",
        "manifest_sha256": digest(graph_document),
        "schema_version": 1,
        "train_cutoff_exclusive": "2026-06-08T00:00:00+08:00",
        "max_source_timestamp": promotion.APPROVED_GRAPH_MAX_SOURCE_TIMESTAMP,
        "source_jd_sha256": promotion.APPROVED_GRAPH_SOURCE_JD_SHA256,
        "source_policy": "train_jd_only",
        "test_jd_used": False,
        "promotion_evidence": {
            "decision": "accepted",
            "report_path": "evidence/skill-graph/report.json",
            "report_sha256": digest(graph_promotion),
            "evaluation_split_sha256": HEX["d"],
            "baseline_run_sha256": HEX["e"],
            "candidate_run_sha256": HEX["f"],
            "primary_metric": "ndcg_at_10",
            "absolute_delta": 0.002,
        },
    }
    for path in (
        "graphs/skill-graph/manifest.json",
        "evidence/skill-graph/report.json",
        graph_data_path,
    ):
        manifest["artifacts"].pop(path)
    manifest["challengers"]["skill_graph"] = {"enabled": False}
    reseal(manifest)
    optional_documents = {
        **documents,
        "embeddings/qwen3-embedding-8b/multiview-1024/manifest.json": multiview_document,
        multiview_data_path: multiview_data,
        "evidence/qwen-mrl/report.json": mrl_report,
        "evidence/qwen-multiview-promotion/report.json": multiview_promotion,
        "graphs/skill-graph/manifest.json": graph_document,
        graph_data_path: graph_data,
        "evidence/skill-graph/report.json": graph_promotion,
    }

    validate_schema(manifest)
    promotion.validate_runtime_manifest(manifest, optional_documents)


def test_enabled_challenger_requires_positive_promotion_evidence(
    release_fixture: object,
) -> None:
    source, spec, documents = release_fixture
    manifest, _ = promotion.build_manifest(source, spec, documents, HEX["e"])
    manifest["challengers"]["learning_to_rank"] = {
        "enabled": True,
        "complete": True,
        "publication_allowed": True,
        "manifest_path": "rankers/ltr/manifest.json",
        "manifest_sha256": HEX["a"],
        "promotion_evidence": {
            "decision": "accepted",
            "report_path": "evidence/ltr/report.json",
            "report_sha256": HEX["b"],
            "evaluation_split_sha256": HEX["c"],
            "baseline_run_sha256": HEX["d"],
            "candidate_run_sha256": HEX["e"],
            "primary_metric": "ndcg_at_10",
            "absolute_delta": 0,
        },
    }

    with pytest.raises(ValidationError):
        validate_schema(manifest)


def test_multiview_requires_positive_ndcg_promotion_evidence() -> None:
    enabled = SCHEMA["$defs"]["multiviewEmbedding"]["oneOf"][1]

    assert "promotion_evidence" in enabled["required"]


def test_promotion_report_body_lineage_is_verified(release_fixture: object) -> None:
    source, spec, documents = release_fixture
    documents = dict(documents)
    manifest, _ = promotion.build_manifest(source, spec, documents, HEX["e"])
    enable_ltr(
        manifest,
        documents,
        delta=0.01,
        report={
            "schema_version": 1,
            "complete": True,
            "publication_allowed": True,
            "evaluation_split_sha256": HEX["f"],
            "baseline_run_sha256": HEX["d"],
            "candidate_run_sha256": HEX["e"],
            "primary_metric": "ndcg_at_10",
            "baseline_value": 0.2,
            "candidate_value": 0.21,
            "absolute_delta": 0.01,
        },
    )

    with pytest.raises(RuntimeError, match="learning_to_rank has no production adapter"):
        promotion.validate_runtime_manifest(manifest, documents)


def test_nonfinite_promotion_delta_is_rejected(release_fixture: object) -> None:
    source, spec, documents = release_fixture
    documents = dict(documents)
    manifest, _ = promotion.build_manifest(source, spec, documents, HEX["e"])
    enable_ltr(
        manifest,
        documents,
        delta=float("nan"),
        report={
            "schema_version": 1,
            "complete": True,
            "publication_allowed": True,
            "evaluation_split_sha256": HEX["c"],
            "baseline_run_sha256": HEX["d"],
            "candidate_run_sha256": HEX["e"],
            "primary_metric": "ndcg_at_10",
            "baseline_value": 0.2,
            "candidate_value": 0.21,
            "absolute_delta": float("nan"),
        },
    )

    with pytest.raises(RuntimeError, match="learning_to_rank has no production adapter"):
        promotion.validate_runtime_manifest(manifest, documents)


def test_graph_cutoff_must_precede_demo_as_of(release_fixture: object) -> None:
    source, spec, documents = release_fixture
    manifest, _ = promotion.build_manifest(source, spec, documents, HEX["e"])
    manifest["challengers"]["skill_graph"] = {
        "enabled": True,
        "complete": True,
        "publication_allowed": True,
        "manifest_path": "graphs/skill-graph/manifest.json",
        "manifest_sha256": HEX["a"],
        "schema_version": 1,
        "train_cutoff_exclusive": "2026-06-09T00:00:00+08:00",
        "max_source_timestamp": "2026-06-07T23:59:59.999+08:00",
        "source_jd_sha256": HEX["e"],
        "source_policy": "train_jd_only",
        "test_jd_used": False,
        "promotion_evidence": {
            "decision": "accepted",
            "report_path": "evidence/graph/report.json",
            "report_sha256": HEX["b"],
            "evaluation_split_sha256": HEX["c"],
            "baseline_run_sha256": HEX["d"],
            "candidate_run_sha256": HEX["e"],
            "primary_metric": "ndcg_at_10",
            "absolute_delta": 0.001,
        },
    }

    with pytest.raises(RuntimeError, match="skill_graph has no production adapter"):
        promotion.validate_runtime_manifest(manifest, documents)


def test_graph_source_is_pinned_to_approved_train_snapshot(release_fixture: object) -> None:
    source, spec, documents = release_fixture
    manifest, _ = promotion.build_manifest(source, spec, documents, HEX["e"])
    manifest["challengers"]["skill_graph"] = {
        "enabled": True,
        "complete": True,
        "publication_allowed": True,
        "manifest_path": "graphs/skill-graph/manifest.json",
        "manifest_sha256": HEX["a"],
        "schema_version": 1,
        "train_cutoff_exclusive": "2026-06-07T00:00:00+08:00",
        "max_source_timestamp": "2026-06-06T23:59:59.999+08:00",
        "source_jd_sha256": HEX["e"],
        "source_policy": "train_jd_only",
        "test_jd_used": False,
        "promotion_evidence": {
            "decision": "accepted",
            "report_path": "evidence/graph/report.json",
            "report_sha256": HEX["b"],
            "evaluation_split_sha256": HEX["c"],
            "baseline_run_sha256": HEX["d"],
            "candidate_run_sha256": HEX["e"],
            "primary_metric": "ndcg_at_10",
            "absolute_delta": 0.001,
        },
    }

    with pytest.raises(RuntimeError, match="skill_graph has no production adapter"):
        promotion.validate_runtime_manifest(manifest, documents)


def test_component_manifest_must_match_selected_object(release_fixture: object) -> None:
    source, spec, documents = release_fixture
    spec["runtime"]["incumbents"]["temporal_tantivy"]["manifest_sha256"] = HEX["f"]

    with pytest.raises(RuntimeError, match="component manifest checksum"):
        promotion.build_manifest(source, spec, documents, HEX["e"])


def test_runtime_validator_rejects_schema_extra_fields(release_fixture: object) -> None:
    source, spec, documents = release_fixture
    manifest, _ = promotion.build_manifest(source, spec, documents, HEX["e"])
    manifest["incumbents"]["whole_embedding"]["unverified"] = True

    with pytest.raises(RuntimeError, match="violates v2 schema"):
        promotion.validate_runtime_manifest(manifest, documents)


def test_eva_build_manifest_is_not_accepted_as_runtime_layout(release_fixture: object) -> None:
    source, spec, documents = release_fixture
    documents = dict(documents)
    manifest, _ = promotion.build_manifest(source, spec, documents, HEX["e"])
    whole_path = manifest["incumbents"]["whole_embedding"]["manifest_path"]
    eva_build_manifest = {
        "model": promotion.APPROVED_MODEL,
        "revision": promotion.APPROVED_MODEL_REVISION,
        "batch_size": 64,
        "max_length": 512,
        "shard_size": 10_000,
        "dtype": "float16",
        "normalized": True,
        "document_fields": promotion.APPROVED_DOCUMENT_FIELDS,
        "document_policy_version": promotion.APPROVED_DOCUMENT_POLICY_VERSION,
        "document_template_sha256": promotion.APPROVED_DOCUMENT_TEMPLATE_SHA256,
        "dataset_sha256": HEX["e"],
        "complete": True,
        "rows": 1_218_635,
        "job_row_order_sha256": HEX["b"],
        "shards": [{"index": 0, "rows": 10_000, "dimension": 4096}],
    }
    payload = encoded(eva_build_manifest)
    documents[whole_path] = payload
    manifest["artifacts"][whole_path]["sha256"] = digest(payload)
    manifest["artifacts"][whole_path]["size_bytes"] = len(payload)
    manifest["incumbents"]["whole_embedding"]["manifest_sha256"] = digest(payload)
    reseal(manifest)

    with pytest.raises(RuntimeError, match="whole embedding component manifest schema differs"):
        promotion.validate_runtime_manifest(manifest, documents)


def test_component_inventory_must_reach_every_runtime_object(release_fixture: object) -> None:
    source, spec, documents = release_fixture
    documents = dict(documents)
    manifest, _ = promotion.build_manifest(source, spec, documents, HEX["e"])
    whole_path = manifest["incumbents"]["whole_embedding"]["manifest_path"]
    whole = json.loads(documents[whole_path])
    whole["job_ids_path"] = f"{promotion.WHOLE_RUNTIME_PREFIX}/missing.json"
    payload = encoded(whole)
    documents[whole_path] = payload
    manifest["artifacts"][whole_path]["sha256"] = digest(payload)
    manifest["artifacts"][whole_path]["size_bytes"] = len(payload)
    manifest["incumbents"]["whole_embedding"]["manifest_sha256"] = digest(payload)
    reseal(manifest)

    with pytest.raises(RuntimeError, match=r"component inventory|unreachable"):
        promotion.validate_runtime_manifest(manifest, documents)


def test_forbidden_raw_log_cannot_enter_bundle(release_fixture: object) -> None:
    source, spec, documents = release_fixture
    manifest, _ = promotion.build_manifest(source, spec, documents, HEX["e"])
    manifest["artifacts"]["evidence/raw-search-logs/session.json"] = {
        "kind": "evidence",
        "sha256": HEX["a"],
        "size_bytes": 1,
    }
    reseal(manifest)

    with pytest.raises(RuntimeError, match=r"forbidden|unreachable"):
        promotion.validate_runtime_manifest(manifest, documents)


@pytest.mark.parametrize(
    "path",
    [
        "evidence/qrels.json",
        "evidence/query_history.json",
        "evidence/test_jd.csv",
        "evidence/raw_logs.ndjson",
        "evidence/aws_credentials.json",
        "models/qwen/safe.qrels.json",
        "models/qwen/data.query_history.json",
        "models/qwen/eval.test_jd.csv",
        "models/qwen/run.raw_logs.ndjson",
        "models/qwen/aws.credentials.json",
        "models/qwen/key.secrets.txt",
    ],
)
def test_forbidden_filename_suffixes_cannot_bypass_path_gate(
    release_fixture: object, path: str
) -> None:
    source, spec, documents = release_fixture
    manifest, _ = promotion.build_manifest(source, spec, documents, HEX["e"])
    manifest["artifacts"][path] = {
        "kind": "evidence",
        "sha256": HEX["a"],
        "size_bytes": 1,
    }
    reseal(manifest)

    with pytest.raises(RuntimeError, match="forbidden"):
        promotion.validate_runtime_manifest(manifest, documents)


def test_guardrail_artifacts_stay_disabled_until_core_can_parse_them(
    release_fixture: object,
) -> None:
    source, spec, documents = release_fixture
    manifest, _ = promotion.build_manifest(source, spec, documents, HEX["e"])
    manifest["challengers"]["guardrails"] = {
        "enabled": True,
        "complete": True,
        "publication_allowed": True,
        "manifest_path": "guardrails/calibration/manifest.json",
        "manifest_sha256": HEX["a"],
        "promotion_evidence": {
            "decision": "accepted",
            "report_path": "evidence/guardrail/report.json",
            "report_sha256": HEX["b"],
            "evaluation_split_sha256": HEX["c"],
            "baseline_run_sha256": HEX["d"],
            "candidate_run_sha256": HEX["e"],
            "primary_metric": "ndcg_at_10",
            "absolute_delta": 0.001,
        },
    }

    with pytest.raises(RuntimeError, match="guardrails has no production adapter"):
        promotion.validate_runtime_manifest(manifest, documents)


def test_selected_inventory_drift_fails_closed(release_fixture: object) -> None:
    source, spec, documents = release_fixture
    spec["selected_inventory_sha256"] = HEX["f"]

    with pytest.raises(RuntimeError, match="selected artifact inventory"):
        promotion.build_manifest(source, spec, documents, HEX["e"])


@pytest.mark.parametrize(
    ("path", "label"),
    [
        (
            promotion.WHOLE_SOURCE_MANIFEST_SOURCE_PATH,
            "approved sealed whole source manifest",
        ),
        (
            promotion.WHOLE_SOURCE_INVENTORY_SOURCE_PATH,
            "approved sealed whole source inventory",
        ),
    ],
)
def test_source_inventory_must_pin_sealed_whole_provenance(
    release_fixture: object, path: str, label: str
) -> None:
    source, spec, _ = release_fixture
    provenance = next(item for item in source["files"] if item["path"] == path)
    provenance["sha256"] = HEX["f"]

    with pytest.raises(RuntimeError, match=label):
        promotion.select_artifacts(source, spec)


def test_promotion_fails_closed_until_temporal_v2_lineage_is_configured(
    release_fixture: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, spec, _ = release_fixture
    monkeypatch.setattr(promotion, "APPROVED_TANTIVY_BUILD_MANIFEST_SHA256", None)

    with pytest.raises(RuntimeError, match="temporal-v3 Tantivy build lineage"):
        promotion.select_artifacts(source, spec)


def test_source_inventory_must_pin_tantivy_build_provenance(
    release_fixture: object,
) -> None:
    source, spec, _ = release_fixture
    provenance = next(
        item
        for item in source["files"]
        if item["path"] == promotion.TANTIVY_BUILD_PROVENANCE_SOURCE_PATH
    )
    provenance["sha256"] = HEX["f"]

    with pytest.raises(RuntimeError, match="approved Tantivy build manifest"):
        promotion.select_artifacts(source, spec)


def test_materialization_report_projection_lineage_is_verified(
    release_fixture: object,
) -> None:
    source, spec, documents = release_fixture
    documents = dict(documents)
    manifest, _ = promotion.build_manifest(source, spec, documents, HEX["e"])
    report = json.loads(documents[promotion.MATERIALIZATION_REPORT_PATH])
    report["projection"] = "unapproved_projection"
    payload = encoded(report)
    documents[promotion.MATERIALIZATION_REPORT_PATH] = payload
    manifest["artifacts"][promotion.MATERIALIZATION_REPORT_PATH]["sha256"] = digest(payload)
    manifest["artifacts"][promotion.MATERIALIZATION_REPORT_PATH]["size_bytes"] = len(payload)
    reseal(manifest)

    with pytest.raises(RuntimeError, match="materialization lineage differs"):
        promotion.validate_runtime_manifest(manifest, documents)


def test_destination_prefix_is_content_addressed() -> None:
    digest_value = "f" * 64
    assert promotion.destination_key(digest_value, "models/qwen/file") == (
        f"runtime/{digest_value}/models/qwen/file"
    )


def test_account_and_region_are_fixed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(promotion, "aws", lambda _: {"Account": "wrong"})
    with pytest.raises(RuntimeError, match=promotion.AWS_ACCOUNT):
        promotion.verify_account()

    monkeypatch.setattr(promotion, "aws", lambda _: {"Account": promotion.AWS_ACCOUNT})
    monkeypatch.setattr(
        promotion,
        "_run",
        lambda *args, **kwargs: type("Result", (), {"stdout": "us-east-1\n"})(),
    )
    with pytest.raises(RuntimeError, match=promotion.AWS_REGION):
        promotion.verify_account()


def test_copy_requires_native_checksums_and_atomic_create(monkeypatch: pytest.MonkeyPatch) -> None:
    item = {
        "source_path": "source/file",
        "path": "models/qwen/file",
        "kind": "model",
        "sha256": "a" * 64,
        "size_bytes": 7,
    }
    checksum = promotion.base64.b64encode(bytes.fromhex("a" * 64)).decode()
    heads = iter(
        [
            {"ContentLength": 7, "ChecksumSHA256": checksum, "ETag": '"etag"'},
            promotion.AwsError("missing", "Not Found"),
            {"ContentLength": 7, "ChecksumSHA256": checksum},
        ]
    )
    calls: list[list[str]] = []

    def head(*_: object) -> dict[str, object]:
        value = next(heads)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(promotion, "_head", head)
    monkeypatch.setattr(promotion, "aws", lambda arguments: calls.append(arguments) or {})

    promotion.copy_artifacts([item], "f" * 64, "source-root/")

    copy_call = calls[0]
    assert copy_call[:2] == ["s3api", "copy-object"]
    assert copy_call[copy_call.index("--copy-source-if-match") + 1] == '"etag"'
    assert copy_call[copy_call.index("--if-none-match") + 1] == "*"
    assert copy_call[copy_call.index("--checksum-algorithm") + 1] == "SHA256"


def test_publish_orders_manifest_after_verified_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(promotion, "copy_artifacts", lambda *_: order.append("objects"))
    monkeypatch.setattr(promotion, "audit_data_objects", lambda *_: order.append("data-audit"))
    monkeypatch.setattr(promotion, "put_manifest", lambda *_: order.append("manifest"))
    monkeypatch.setattr(promotion, "audit_destination", lambda *_: order.append("audit"))

    promotion.publish_release([], b"{}\n", "f" * 64, "source-root/")

    assert order == ["objects", "data-audit", "manifest", "audit"]


def test_stage_source_uploads_content_addressed_manifest_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    payload = b"artifact\n"
    (root / "artifact.bin").write_bytes(payload)
    source = {
        "schema_version": 3,
        "files": [
            {
                "path": "artifact.bin",
                "sha256": digest(payload),
                "size": len(payload),
            }
        ],
    }
    manifest = encoded(source)
    manifest_path = root / "manifest.json"
    manifest_path.write_bytes(manifest)
    manifest_sha = digest(manifest)
    spec = {
        "source_manifest": {
            "key": f"one111-search/materialized/{manifest_sha}/manifest.json",
            "sha256": manifest_sha,
        }
    }
    order: list[str] = []
    monkeypatch.setattr(
        promotion,
        "_put_source_file",
        lambda path, key, sha256, size: order.append(key),
    )
    inventories = iter(
        [
            {f"one111-search/materialized/{manifest_sha}/artifact.bin": len(payload)},
            {
                f"one111-search/materialized/{manifest_sha}/artifact.bin": len(payload),
                f"one111-search/materialized/{manifest_sha}/manifest.json": len(manifest),
            },
        ]
    )
    monkeypatch.setattr(promotion, "_list_source", lambda _prefix: next(inventories))

    promotion.stage_source(source, spec, root, manifest_path)

    prefix = f"one111-search/materialized/{manifest_sha}/"
    assert order == [f"{prefix}artifact.bin", f"{prefix}manifest.json"]


def test_source_put_uses_full_sha256_and_atomic_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "artifact.bin"
    path.write_bytes(b"artifact\n")
    sha256 = digest(path.read_bytes())
    checksum = promotion.base64.b64encode(bytes.fromhex(sha256)).decode()
    heads = iter(
        [
            promotion.AwsError("missing", "Not Found"),
            {"ContentLength": path.stat().st_size, "ChecksumSHA256": checksum},
        ]
    )
    calls: list[list[str]] = []

    def head(*_: object) -> dict[str, object]:
        value = next(heads)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(promotion, "_head", head)
    monkeypatch.setattr(promotion, "aws", lambda arguments: calls.append(arguments) or {})

    promotion._put_source_file(path, "one111-search/materialized/x/artifact.bin", sha256, 9)

    put = calls[0]
    assert put[:2] == ["s3api", "put-object"]
    assert put[put.index("--checksum-sha256") + 1] == checksum
    assert put[put.index("--if-none-match") + 1] == "*"


def test_data_only_audit_rejects_extra_destination_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_sha = "f" * 64
    item = {
        "path": "models/qwen/config.json",
        "sha256": HEX["a"],
        "size_bytes": 7,
    }
    monkeypatch.setattr(
        promotion,
        "_list_destination",
        lambda _: {
            promotion.destination_key(manifest_sha, item["path"]): 7,
            f"runtime/{manifest_sha}/unexpected.bin": 1,
        },
    )

    with pytest.raises(RuntimeError, match="data-only"):
        promotion.audit_data_objects([item], manifest_sha, b"{}\n")


def test_manifest_put_requires_exact_body_readback(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b'{"schema_version":2}\n'
    manifest_sha = digest(payload)
    checksum = promotion.base64.b64encode(hashlib.sha256(payload).digest()).decode()
    monkeypatch.setattr(promotion, "aws", lambda _: {})
    monkeypatch.setattr(
        promotion,
        "_head",
        lambda *_: {
            "ContentLength": len(payload),
            "ChecksumSHA256": checksum,
            "Metadata": {"sha256": manifest_sha},
        },
    )
    monkeypatch.setattr(promotion, "_read_destination_manifest", lambda _: b"wrong")

    with pytest.raises(RuntimeError, match="destination manifest differs"):
        promotion.put_manifest(payload, manifest_sha)


def test_destination_inventory_audit_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    pages = iter(
        [
            {
                "Contents": [{"Key": "runtime/x/a", "Size": 1}],
                "IsTruncated": True,
                "NextContinuationToken": "next",
            },
            {
                "Contents": [{"Key": "runtime/x/b", "Size": 2}],
                "IsTruncated": False,
            },
        ]
    )

    def page(arguments: list[str]) -> dict[str, object]:
        calls.append(arguments)
        return next(pages)

    monkeypatch.setattr(promotion, "aws", page)

    assert promotion._list_destination("runtime/x/") == {
        "runtime/x/a": 1,
        "runtime/x/b": 2,
    }
    assert "--continuation-token" not in calls[0]
    assert calls[1][calls[1].index("--continuation-token") + 1] == "next"


def test_offline_dry_run_never_calls_aws(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    release_fixture: object,
) -> None:
    source, spec, documents = release_fixture
    source_path = tmp_path / "source-manifest.json"
    spec_path = tmp_path / "release-spec.json"
    source_root = tmp_path / "source"
    source_path.write_bytes(encoded(source))
    spec_path.write_bytes(encoded(spec))
    selected = promotion.select_artifacts(source, spec)
    for item in selected:
        if item["path"] not in documents:
            continue
        path = source_root / item["source_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(documents[item["path"]])
    monkeypatch.setattr(
        promotion,
        "verify_account",
        lambda: pytest.fail("offline dry-run called AWS"),
    )

    promotion.main(
        [
            "--release-spec",
            str(spec_path),
            "--source-manifest-file",
            str(source_path),
            "--source-root",
            str(source_root),
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert result["executed"] is False
    assert result["schema_version"] == 2


def test_cli_requires_tantivy_approvals_as_a_pair() -> None:
    with pytest.raises(RuntimeError, match="must be supplied together"):
        promotion.main(
            [
                "--release-spec",
                "does-not-matter.json",
                "--approved-tantivy-build-sha256",
                HEX["a"],
            ]
        )


def test_aws_cli_retries_only_transient_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def run(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls < 3:
            return subprocess.CompletedProcess([], 1, "", "Connection was closed")
        return subprocess.CompletedProcess([], 0, "{}", "")

    monkeypatch.setattr(promotion.subprocess, "run", run)
    monkeypatch.setattr(promotion, "sleep", lambda _: None)

    assert promotion.aws(["sts", "get-caller-identity"]) == {}
    assert calls == 3


def test_deploy_downloads_and_validates_v2_manifest_body() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")

    assert "compute_profile:" in workflow
    assert "default: cpu-incumbent" in workflow
    assert "options:" in workflow
    assert "- cpu-incumbent" in workflow
    assert "- gpu-shadow" in workflow
    assert (
        '[[ "$COMPUTE_PROFILE" == "cpu-incumbent" || '
        '"$COMPUTE_PROFILE" == "gpu-shadow" ]]' in workflow
    )
    for legacy_input in (
        "cpu_desired_count",
        "gpu_min_capacity",
        "gpu_max_capacity",
        "gpu_desired_count",
    ):
        assert legacy_input not in workflow
    assert "s3api get-object" in workflow
    assert "validate_runtime_manifest_file.py" in workflow
    smoke_step = workflow.split("- name: Smoke test the public application", 1)[1]
    assert 'READY_JSON="$("${CURL[@]}" "$WEB_URL/readyz")"' in smoke_step
    assert '"artifact_manifest_sha256": os.environ["ARTIFACT_MANIFEST_SHA"]' in smoke_step
    assert "payload != expected" in smoke_step
    platform_step = workflow.split("- name: Deploy the application stack", 1)[1].split(
        "- name: Publish the static web application", 1
    )[0]
    assert "ALARM_EMAIL: ${{ inputs.alarm_email }}" in platform_step


def test_bootstrap_stages_source_and_deploys_promoted_runtime_sha() -> None:
    bootstrap = (ROOT / "scripts" / "bootstrap_competition_release.sh").read_text(encoding="utf-8")

    assert "TYPED_V3_ATTESTATION APPROVED_ATTESTATION_SHA256 DEPLOY" in bootstrap
    assert "verify_temporal_v3_promotion.py" in bootstrap
    assert bootstrap.index("verify_temporal_v3_promotion.py") < bootstrap.index(
        "import_jobs_to_aws.py"
    )
    assert "--stage-source" in bootstrap
    assert 'json.loads(sys.argv[1])["manifest_sha256"]' in bootstrap
    assert "runtime_sha=$(uv run python -c 'import hashlib" not in bootstrap
    assert "expected_commit=$(git rev-parse HEAD)" in bootstrap
    assert "headSha,displayTitle" in bootstrap
    assert "before_run_ids" in bootstrap

    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert (
        "run-name: Deploy ${{ inputs.artifact_manifest_sha }} ${{ inputs.deployment_id }}"
        in workflow
    )
    assert "deployment_id=$(uv run python -c 'import uuid; print(uuid.uuid4())')" in bootstrap
    assert "-f compute_profile=cpu-incumbent" in bootstrap
    for legacy_input in (
        "cpu_desired_count",
        "gpu_min_capacity",
        "gpu_max_capacity",
        "gpu_desired_count",
    ):
        assert legacy_input not in bootstrap


def test_downloaded_manifest_validator_checks_exact_body_and_v2_schema(
    tmp_path: Path, release_fixture: object
) -> None:
    source, spec, documents = release_fixture
    manifest, _ = promotion.build_manifest(source, spec, documents, HEX["e"])
    payload = promotion.canonical_bytes(manifest)
    path = tmp_path / "manifest.json"
    path.write_bytes(payload)

    runtime_file_validation.validate(path, digest(payload))
    with pytest.raises(RuntimeError, match="body SHA-256"):
        runtime_file_validation.validate(path, HEX["f"])

    manifest["schema_version"] = 1
    invalid_payload = promotion.canonical_bytes(manifest)
    path.write_bytes(invalid_payload)
    with pytest.raises(ValidationError):
        runtime_file_validation.validate(path, digest(invalid_payload))
