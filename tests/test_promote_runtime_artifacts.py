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
    vectors_path = "embeddings/qwen3-embedding-8b/whole/shards/00000.f16.npy"
    job_ids_path = "embeddings/qwen3-embedding-8b/whole/shards/00000-job-ids.i64.npy"
    model_path = "models/qwen3-embedding-8b/config.json"
    vectors = b"verified-vectors"
    job_ids = b"verified-job-ids"
    model = b'{"model_type":"qwen3"}\n'
    whole = {
        "complete": True,
        "model": "Qwen/Qwen3-Embedding-8B",
        "revision": "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af",
        "dtype": "float16",
        "normalized": True,
        "rows": 1_218_635,
        "dataset_sha256": HEX["e"],
        "jobs_sha256": HEX["a"],
        "job_row_order_sha256": HEX["b"],
        "document_policy_version": "2026-07-24-clean-v1",
        "document_template_sha256": HEX["c"],
        "document_fields": promotion.APPROVED_DOCUMENT_FIELDS,
        "query_instruction": promotion.APPROVED_QUERY_INSTRUCTION,
        "shards": [
            {
                "vectors_path": vectors_path,
                "job_ids_path": job_ids_path,
                "row_start": 0,
                "row_end": 1_218_635,
                "rows": 1_218_635,
                "dimension": 4096,
            }
        ],
        "files": [
            {"path": vectors_path, "sha256": digest(vectors), "size_bytes": len(vectors)},
            {"path": job_ids_path, "sha256": digest(job_ids), "size_bytes": len(job_ids)},
            {"path": model_path, "sha256": digest(model), "size_bytes": len(model)},
        ],
    }
    index_path = "indexes/tantivy-bm25-temporal-v1/index/meta.json"
    index_file = b'{"segments":[]}\n'
    temporal = {
        "complete": True,
        "engine": "tantivy v0.26.0, index_format v7",
        "jobs_sha256": HEX["a"],
        "job_row_order_sha256": HEX["b"],
        "index_sha256": HEX["d"],
        "updated_at_field": "updated_at_epoch_ms",
        "filter_semantics": "visibility AND (location OR) AND (duty OR), applied before Top-K",
        "temporal_filter_semantics": promotion.TEMPORAL_FILTER_SEMANTICS,
        "index_directory": "indexes/tantivy-bm25-temporal-v1/index",
        "index_files": [
            {"path": index_path, "sha256": digest(index_file), "size_bytes": len(index_file)}
        ],
        "schema_fields": promotion.APPROVED_TANTIVY_SCHEMA_FIELDS,
        "field_boosts": promotion.APPROVED_TANTIVY_FIELD_BOOSTS,
    }
    return {
        "embeddings/qwen3-embedding-8b/whole/manifest.json": encoded(whole),
        vectors_path: vectors,
        job_ids_path: job_ids,
        model_path: model,
        "indexes/tantivy-bm25-temporal-v1/manifest.json": encoded(temporal),
        index_path: index_file,
    }


def base_source(documents: dict[str, bytes]) -> dict[str, object]:
    paths = {
        "embeddings/qwen3-embedding-8b/whole/manifest.json": (
            "artifacts/experiments/qwen3-8b/full/manifest.json",
            "embedding",
        ),
        "embeddings/qwen3-embedding-8b/whole/shards/00000.f16.npy": (
            "artifacts/experiments/qwen3-8b/full/shards/00000.f16.npy",
            "embedding",
        ),
        "embeddings/qwen3-embedding-8b/whole/shards/00000-job-ids.i64.npy": (
            "artifacts/experiments/qwen3-8b/full/shards/00000-job-ids.i64.npy",
            "embedding",
        ),
        "models/qwen3-embedding-8b/config.json": (
            "cache/huggingface/qwen/config.json",
            "model",
        ),
        "indexes/tantivy-bm25-temporal-v1/manifest.json": (
            "artifacts/experiments/tantivy-bm25-temporal-v1/manifest.json",
            "index",
        ),
        "indexes/tantivy-bm25-temporal-v1/index/meta.json": (
            "artifacts/experiments/tantivy-bm25-temporal-v1/index/meta.json",
            "index",
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
    files.extend(
        [
            {
                "path": "artifacts/production/query-history/answers.sqlite3",
                "sha256": HEX["f"],
                "size": 99,
            },
        ]
    )
    return {"schema_version": 3, "files": files}


def base_spec(source: dict[str, object], documents: dict[str, bytes]) -> dict[str, object]:
    selections = [
        {
            "source_prefix": "artifacts/experiments/qwen3-8b/full/",
            "destination_prefix": "embeddings/qwen3-embedding-8b/whole/",
            "kind": "embedding",
        },
        {
            "source_prefix": "cache/huggingface/qwen/",
            "destination_prefix": "models/qwen3-embedding-8b/",
            "kind": "model",
        },
        {
            "source_prefix": "artifacts/experiments/tantivy-bm25-temporal-v1/",
            "destination_prefix": "indexes/tantivy-bm25-temporal-v1/",
            "kind": "index",
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
    whole = json.loads(documents["embeddings/qwen3-embedding-8b/whole/manifest.json"])
    temporal = json.loads(documents["indexes/tantivy-bm25-temporal-v1/manifest.json"])
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
                    "manifest_path": "embeddings/qwen3-embedding-8b/whole/manifest.json",
                    "manifest_sha256": digest(
                        documents["embeddings/qwen3-embedding-8b/whole/manifest.json"]
                    ),
                    "complete": True,
                    "model": whole["model"],
                    "revision": whole["revision"],
                    "dimension": 4096,
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
                    "manifest_path": "indexes/tantivy-bm25-temporal-v1/manifest.json",
                    "manifest_sha256": digest(
                        documents["indexes/tantivy-bm25-temporal-v1/manifest.json"]
                    ),
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
def release_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, object], dict[str, object], dict[str, bytes]]:
    documents = base_documents()
    source = base_source(documents)
    spec = base_spec(source, documents)
    monkeypatch.setattr(
        promotion,
        "APPROVED_WHOLE_MANIFEST_SHA256",
        spec["runtime"]["incumbents"]["whole_embedding"]["manifest_sha256"],
    )
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

    assert future_policy == {"const": "retained_with_zero_freshness"}
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
    assert manifest["release"]["object_count"] == len(selected) == 6
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


def test_schema_accepts_publishable_multiview_and_train_only_graph(
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
            "dataset_sha256": HEX["e"],
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
        "dataset_sha256": HEX["e"],
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

    with pytest.raises(RuntimeError, match=r"promotion report.*split"):
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

    with pytest.raises(RuntimeError, match="finite positive"):
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

    with pytest.raises(RuntimeError, match="train cutoff"):
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

    with pytest.raises(RuntimeError, match="approved Graph train snapshot"):
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


def test_component_inventory_must_reach_every_runtime_object(
    release_fixture: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, spec, documents = release_fixture
    documents = dict(documents)
    manifest, _ = promotion.build_manifest(source, spec, documents, HEX["e"])
    whole_path = manifest["incumbents"]["whole_embedding"]["manifest_path"]
    whole = json.loads(documents[whole_path])
    whole["files"] = []
    payload = encoded(whole)
    documents[whole_path] = payload
    manifest["artifacts"][whole_path]["sha256"] = digest(payload)
    manifest["artifacts"][whole_path]["size_bytes"] = len(payload)
    manifest["incumbents"]["whole_embedding"]["manifest_sha256"] = digest(payload)
    monkeypatch.setattr(promotion, "APPROVED_WHOLE_MANIFEST_SHA256", digest(payload))
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


def test_selected_inventory_drift_fails_closed(release_fixture: object) -> None:
    source, spec, documents = release_fixture
    spec["selected_inventory_sha256"] = HEX["f"]

    with pytest.raises(RuntimeError, match="selected artifact inventory"):
        promotion.build_manifest(source, spec, documents, HEX["e"])


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

    assert "s3api get-object" in workflow
    assert "validate_runtime_manifest_file.py" in workflow


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
