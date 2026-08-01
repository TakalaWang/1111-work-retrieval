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
        "shards": [{"dimension": 4096}],
    }
    temporal = {
        "complete": True,
        "engine": "tantivy v0.26.0, index_format v7",
        "jobs_sha256": HEX["a"],
        "job_row_order_sha256": HEX["b"],
        "index_sha256": HEX["d"],
        "updated_at_field": "updated_at_epoch_ms",
        "filter_semantics": "visibility AND (location OR) AND (duty OR), applied before Top-K",
        "temporal_filter_semantics": (
            "updated_at <= as_of AND updated_at >= as_of - 180 days before Top-K"
        ),
    }
    return {
        "embeddings/qwen3-embedding-8b/whole/manifest.json": encoded(whole),
        "indexes/tantivy-bm25-temporal-v1/manifest.json": encoded(temporal),
    }


def base_source(documents: dict[str, bytes]) -> dict[str, object]:
    paths = {
        "embeddings/qwen3-embedding-8b/whole/manifest.json": (
            "artifacts/experiments/qwen3-8b/full/manifest.json",
            "embedding",
        ),
        "indexes/tantivy-bm25-temporal-v1/manifest.json": (
            "artifacts/experiments/tantivy-bm25-temporal-v1/manifest.json",
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
    model = b"{}\n"
    files.extend(
        [
            {
                "path": "cache/huggingface/qwen/config.json",
                "sha256": digest(model),
                "size": len(model),
            },
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
    expected_items = [
        {
            "source_path": "artifacts/experiments/qwen3-8b/full/manifest.json",
            "path": "embeddings/qwen3-embedding-8b/whole/manifest.json",
            "kind": "embedding",
            "sha256": digest(documents["embeddings/qwen3-embedding-8b/whole/manifest.json"]),
            "size_bytes": len(documents["embeddings/qwen3-embedding-8b/whole/manifest.json"]),
        },
        {
            "source_path": "artifacts/experiments/tantivy-bm25-temporal-v1/manifest.json",
            "path": "indexes/tantivy-bm25-temporal-v1/manifest.json",
            "kind": "index",
            "sha256": digest(documents["indexes/tantivy-bm25-temporal-v1/manifest.json"]),
            "size_bytes": len(documents["indexes/tantivy-bm25-temporal-v1/manifest.json"]),
        },
        {
            "source_path": "cache/huggingface/qwen/config.json",
            "path": "models/qwen3-embedding-8b/config.json",
            "kind": "model",
            "sha256": digest(b"{}\n"),
            "size_bytes": 3,
        },
    ]
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
                    "future_jobs": "exclude",
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
                    "query_neighbor_history",
                    "behavior_prior",
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


def test_runtime_schema_is_valid_json_schema() -> None:
    Draft202012Validator.check_schema(SCHEMA)


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
    assert manifest["release"]["object_count"] == len(selected) == 3
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
    mrl_evidence = {
        "decision": "accepted",
        "report_path": "evidence/qwen-mrl/report.json",
        "report_sha256": HEX["b"],
        "stable_result_sha256": HEX["c"],
        "selected_dimension": 1024,
        "reference_dimension": 4096,
    }
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
            "mrl_report_sha256": HEX["b"],
            "mrl_evidence": {
                key: value for key, value in mrl_evidence.items() if key != "report_path"
            },
            "view_policy": {"included_kinds": ["occupation", "skill", "requirement", "content"]},
        }
    )
    graph_document = encoded(
        {
            "complete": True,
            "publication_allowed": True,
            "schema_version": 1,
            "train_cutoff_exclusive": "2026-06-08T00:00:00+08:00",
            "max_source_timestamp": "2026-06-07T23:59:59.999+08:00",
            "source_jd_sha256": HEX["e"],
            "source_policy": "train_jd_only",
            "test_jd_used": False,
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
                "sha256": HEX["b"],
                "size_bytes": 11,
            },
            "graphs/skill-graph/manifest.json": {
                "kind": "graph",
                "sha256": digest(graph_document),
                "size_bytes": len(graph_document),
            },
            "evidence/skill-graph/report.json": {
                "kind": "evidence",
                "sha256": HEX["d"],
                "size_bytes": 13,
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
    }
    manifest["challengers"]["skill_graph"] = {
        "enabled": True,
        "complete": True,
        "publication_allowed": True,
        "manifest_path": "graphs/skill-graph/manifest.json",
        "manifest_sha256": digest(graph_document),
        "schema_version": 1,
        "train_cutoff_exclusive": "2026-06-08T00:00:00+08:00",
        "max_source_timestamp": "2026-06-07T23:59:59.999+08:00",
        "source_jd_sha256": HEX["e"],
        "source_policy": "train_jd_only",
        "test_jd_used": False,
        "promotion_evidence": {
            "decision": "accepted",
            "report_path": "evidence/skill-graph/report.json",
            "report_sha256": HEX["d"],
            "evaluation_split_sha256": HEX["e"],
            "baseline_run_sha256": HEX["f"],
            "candidate_run_sha256": HEX["a"],
            "primary_metric": "ndcg_at_10",
            "absolute_delta": 0.001,
        },
    }
    reseal(manifest)
    optional_documents = {
        **documents,
        "embeddings/qwen3-embedding-8b/multiview-1024/manifest.json": multiview_document,
        "graphs/skill-graph/manifest.json": graph_document,
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
    monkeypatch.setattr(promotion, "put_manifest", lambda *_: order.append("manifest"))
    monkeypatch.setattr(promotion, "audit_destination", lambda *_: order.append("audit"))

    promotion.publish_release([], b"{}\n", "f" * 64, "source-root/")

    assert order == ["objects", "manifest", "audit"]


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
    source_by_destination = {
        "embeddings/qwen3-embedding-8b/whole/manifest.json": (
            "artifacts/experiments/qwen3-8b/full/manifest.json"
        ),
        "indexes/tantivy-bm25-temporal-v1/manifest.json": (
            "artifacts/experiments/tantivy-bm25-temporal-v1/manifest.json"
        ),
    }
    for destination, source_name in source_by_destination.items():
        path = source_root / source_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(documents[destination])
    model = source_root / "cache/huggingface/qwen/config.json"
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_bytes(b"{}\n")
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
