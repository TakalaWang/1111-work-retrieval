#!/usr/bin/env python3
"""Build and verify the production full-JD fielded Tantivy index."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Protocol, cast
from zoneinfo import ZoneInfo

import boto3  # type: ignore[import-untyped]
import tantivy
from pipeline_contract import (
    artifact_entry,
    atomic_json,
    canonical_json,
    exact_keys,
    publish_s3_directory,
    read_json_object,
    require_sha256,
    sha256_file,
    verify_local_inventory,
    verify_s3_inventory,
    verify_s3_object,
)
from work_retrieval_core.adapters import (
    EDUCATION_FILTER_FIELD,
    EXPERIENCE_FILTER_FIELD,
    FIELD_BOOSTS,
    FILTER_SEMANTICS,
    JOB_ATTRIBUTE_FILTER_FIELD,
    JOB_INDEX_FIELD,
    LEXICAL_POLICY_VERSION,
    MANAGEMENT_FILTER_FIELD,
    MONTHLY_SALARY_LOWER_FIELD,
    MONTHLY_SALARY_RECALL_FIELD,
    NUMERIC_FILTER_FIELDS,
    RAW_FILTER_FIELDS,
    SOURCE_FIELDS,
    TEXT_FIELDS,
    TOKENIZERS,
    UPDATED_AT_FIELD,
    VISIBILITY_FIELD,
    WORK_SHIFT_FILTER_FIELD,
    CorpusQueryCompiler,
    lexical_policy_sha256,
    lexical_tokens,
)
from work_retrieval_core.constraints import (
    education_filter_values,
    job_attribute_filter_value,
    management_filter_value,
    monthly_salary_filter_values,
    no_experience_filter_value,
    normalize_salary_bound,
    salary_period,
    work_shift_filter_values,
)
from work_retrieval_core.manifest import TEMPORAL_FILTER_SEMANTICS
from work_retrieval_core.serialization import FULL_JOB_FIELDS, canonical_code, canonical_text

JOB_ID_FIELD = "職缺編號"
DEFAULT_LOCATION_CODE_FIELD = "工作城市編號"
DEFAULT_LOCATION_TERM_FIELD = "工作城市"
DEFAULT_DUTY_CODE_FIELD = "職務小類編號"
DEFAULT_DUTY_TERM_FIELD = "職務小類"
DEFAULT_VISIBILITY_FIELD = "是否公開"
DEFAULT_MODIFIED_AT_FIELD = "職缺最後修改時間"
SALARY_LOWER_SOURCE_FIELD = "薪資下限"
SALARY_UPPER_SOURCE_FIELD = "薪資上限"
DEFAULT_ARTIFACT_PREFIX = "indexes/tantivy-bm25-temporal-v3"
SOURCE_TIMEZONE = ZoneInfo("Asia/Taipei")
TAXONOMY_FIELDS = {"CodeNo", "CodeNameA", "CodeNameB", "CodeNameC"}
ENGINE = "tantivy v0.26.0, index_format v7"
TEMPORAL_SEMANTICS = TEMPORAL_FILTER_SEMANTICS
SCHEMA_FIELDS = [
    *TEXT_FIELDS,
    *RAW_FILTER_FIELDS,
    UPDATED_AT_FIELD,
    *NUMERIC_FILTER_FIELDS,
    JOB_INDEX_FIELD,
]
COMPONENT_KEYS = {
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
    "query_corrections",
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
BUILD_KEYS = {
    "schema_version",
    "complete",
    "builder",
    "engine",
    "dataset_sha256",
    "jobs_sha256",
    "job_row_order_sha256",
    "rows",
    "index_sha256",
    "index_tree",
    "taxonomy_sha256",
    "query_corrections",
    "lexical_policy_version",
    "lexical_policy_sha256",
    "tokenizers",
    "source_fields",
    "source_csv_fields",
    "salary_filter_excluded_rows",
}


class AwsIdentity(Protocol):
    def get_caller_identity(self) -> Mapping[str, object]: ...


def _timestamp(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError(f"{name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=SOURCE_TIMEZONE)
    return parsed


def _serialized(values: Mapping[str, str | None], fields: Sequence[str]) -> str:
    labels = {field: label for label, field in FULL_JOB_FIELDS}
    seen: set[str] = set()
    lines: list[str] = []
    for field in fields:
        value = canonical_text(values[field])
        identity = canonical_code(value)
        if identity and identity not in seen:
            seen.add(identity)
            lines.append(f"{labels[field]}: {value}")
    return "\n".join(lines)


def _taxonomy_add(mapping: dict[str, set[str]], code: str, term: str, name: str) -> None:
    if not code.isascii() or not code.isdecimal() or not term:
        raise RuntimeError(f"source CSV contains an invalid {name} filter value")
    mapping.setdefault(code, set()).add(term)


def _taxonomy_csv(path: Path, name: str) -> tuple[dict[str, set[str]], set[str]]:
    mapping: dict[str, set[str]] = {}
    primary_terms: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None or not TAXONOMY_FIELDS.issubset(reader.fieldnames):
            missing = sorted(TAXONOMY_FIELDS.difference(reader.fieldnames or ()))
            raise RuntimeError(f"{name} taxonomy CSV is missing fields: {missing}")
        for row in reader:
            code = canonical_text(row["CodeNo"])
            primary = canonical_code(row["CodeNameA"])
            if code in mapping or not primary:
                raise RuntimeError(
                    f"{name} taxonomy contains a duplicate code or empty primary term"
                )
            for field in ("CodeNameA", "CodeNameB", "CodeNameC"):
                term = canonical_code(row[field])
                if term:
                    _taxonomy_add(mapping, code, term, name)
            primary_terms.add(primary)
    if not mapping:
        raise RuntimeError(f"{name} taxonomy CSV contains no rows")
    return mapping, primary_terms


def _tree(index_directory: Path) -> list[dict[str, object]]:
    files = sorted(path for path in index_directory.rglob("*") if path.is_file())
    if not files:
        raise RuntimeError("Tantivy index tree is empty")
    return [
        {
            "path": path.relative_to(index_directory).as_posix(),
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
        for path in files
    ]


def _tree_sha256(tree: Sequence[Mapping[str, object]]) -> str:
    return hashlib.sha256(canonical_json(list(tree))).hexdigest()


def _schema() -> tantivy.Schema:
    builder = tantivy.SchemaBuilder()
    for field in TEXT_FIELDS:
        builder.add_text_field(field)
    for field in RAW_FILTER_FIELDS:
        builder.add_text_field(field, tokenizer_name="raw")
    builder.add_unsigned_field(UPDATED_AT_FIELD, indexed=True, fast=True)
    for field in NUMERIC_FILTER_FIELDS:
        builder.add_unsigned_field(field, indexed=True)
    builder.add_unsigned_field(JOB_INDEX_FIELD, fast=True)
    return builder.build()


def build_tantivy(
    *,
    jobs_csv: Path,
    output: Path,
    artifact_prefix: str,
    location_code_field: str | None,
    location_term_field: str,
    location_taxonomy_csv: Path | None = None,
    duty_code_field: str | None,
    duty_term_field: str,
    duty_taxonomy_csv: Path | None = None,
    visibility_field: str | None,
    modified_at_field: str,
    correction_candidate_path: Path | None,
    correction_attestation_path: Path | None,
) -> dict[str, object]:
    if output.exists():
        raise RuntimeError("Tantivy output already exists; builds never overwrite")
    clean_prefix = PurePosixPath(artifact_prefix)
    if clean_prefix.is_absolute() or ".." in clean_prefix.parts or len(clean_prefix.parts) < 2:
        raise ValueError("artifact prefix must be a safe runtime-relative path")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f".{output.name}.{os.getpid()}.partial")
    if partial.exists():
        raise RuntimeError(f"partial Tantivy output already exists: {partial}")
    partial.mkdir()
    index_directory = partial / "index"
    try:
        dataset_sha256 = sha256_file(jobs_csv)
        index_directory.mkdir()
        index = tantivy.Index(_schema(), path=str(index_directory), reuse=False)
        writer = index.writer()
        job_ids: list[str] = []
        seen: set[str] = set()
        order = hashlib.sha256()
        salary_filter_excluded_rows = 0
        if location_taxonomy_csv is None:
            if location_code_field is None:
                raise RuntimeError("location code field or taxonomy CSV is required")
            locations: dict[str, set[str]] = {}
            location_terms: set[str] | None = None
        else:
            locations, location_terms = _taxonomy_csv(location_taxonomy_csv, "location")
        if duty_taxonomy_csv is None:
            if duty_code_field is None:
                raise RuntimeError("duty code field or taxonomy CSV is required")
            duties: dict[str, set[str]] = {}
            duty_terms: set[str] | None = None
        else:
            duties, duty_terms = _taxonomy_csv(duty_taxonomy_csv, "duty")
        required = {
            JOB_ID_FIELD,
            location_term_field,
            duty_term_field,
            modified_at_field,
            SALARY_LOWER_SOURCE_FIELD,
            SALARY_UPPER_SOURCE_FIELD,
            *(label for label, _field in FULL_JOB_FIELDS),
        }
        if location_taxonomy_csv is None:
            assert location_code_field is not None
            required.add(location_code_field)
        if duty_taxonomy_csv is None:
            assert duty_code_field is not None
            required.add(duty_code_field)
        if visibility_field is not None:
            required.add(visibility_field)
        csv.field_size_limit(64 * 1024 * 1024)
        with jobs_csv.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames is None or not required.issubset(reader.fieldnames):
                missing = sorted(required.difference(reader.fieldnames or ()))
                raise RuntimeError(f"source CSV is missing Tantivy fields: {missing}")
            for row_index, row in enumerate(reader):
                job_id = canonical_text(row[JOB_ID_FIELD])
                if not job_id.isascii() or not job_id.isdecimal() or job_id in seen:
                    raise RuntimeError("source CSV contains invalid or duplicate job_id")
                seen.add(job_id)
                job_ids.append(job_id)
                order.update(job_id.encode() + b"\n")
                values = {field: row[label] for label, field in FULL_JOB_FIELDS}
                location_term = canonical_code(canonical_text(row[location_term_field]))
                duty_term = canonical_code(canonical_text(row[duty_term_field]))
                visibility = (
                    "1" if visibility_field is None else canonical_code(row[visibility_field])
                )
                if visibility not in {"0", "1"}:
                    raise RuntimeError("source CSV visibility must be exactly 0 or 1")
                if location_taxonomy_csv is None:
                    assert location_code_field is not None
                    _taxonomy_add(
                        locations,
                        canonical_text(row[location_code_field]),
                        location_term,
                        "location",
                    )
                elif location_term and location_term not in cast(set[str], location_terms):
                    raise RuntimeError("source CSV location is absent from its taxonomy")
                if duty_taxonomy_csv is None:
                    assert duty_code_field is not None
                    _taxonomy_add(
                        duties,
                        canonical_text(row[duty_code_field]),
                        duty_term,
                        "duty",
                    )
                elif duty_term and duty_term not in cast(set[str], duty_terms):
                    raise RuntimeError("source CSV duty is absent from its taxonomy")
                modified = _timestamp(row[modified_at_field], "job modified timestamp")
                epoch_ms = int(modified.timestamp() * 1000)
                if epoch_ms < 0:
                    raise RuntimeError("job modified timestamp precedes the Unix epoch")
                try:
                    salary_lower = normalize_salary_bound(row[SALARY_LOWER_SOURCE_FIELD])
                    salary_upper = normalize_salary_bound(row[SALARY_UPPER_SOURCE_FIELD])
                except ValueError:
                    salary_lower = None
                    salary_upper = None
                    salary_filter_excluded_rows += 1
                indexed_salary_lower, indexed_salary_recall = monthly_salary_filter_values(
                    salary_period(values["salary_text"]),
                    salary_lower,
                    salary_upper,
                )
                document = tantivy.Document()
                for field in TEXT_FIELDS:
                    text = _serialized(values, SOURCE_FIELDS[field])
                    document.add_text(field, " ".join(lexical_tokens(text)))
                if location_term:
                    document.add_text("location_filter", location_term)
                if duty_term:
                    document.add_text("duty_filter", duty_term)
                document.add_text(VISIBILITY_FIELD, visibility)
                for education in education_filter_values(values["education_requirement"]):
                    document.add_text(EDUCATION_FILTER_FIELD, education)
                if attribute := job_attribute_filter_value(values["job_attribute"]):
                    document.add_text(JOB_ATTRIBUTE_FILTER_FIELD, attribute)
                for shift in work_shift_filter_values(values["work_hours"]):
                    document.add_text(WORK_SHIFT_FILTER_FIELD, shift)
                if experience := no_experience_filter_value(values["experience_requirement"]):
                    document.add_text(EXPERIENCE_FILTER_FIELD, experience)
                if management := management_filter_value(values["management_count"]):
                    document.add_text(MANAGEMENT_FILTER_FIELD, management)
                document.add_unsigned(UPDATED_AT_FIELD, epoch_ms)
                if indexed_salary_lower is not None:
                    document.add_unsigned(MONTHLY_SALARY_LOWER_FIELD, indexed_salary_lower)
                if indexed_salary_recall is not None:
                    document.add_unsigned(MONTHLY_SALARY_RECALL_FIELD, indexed_salary_recall)
                document.add_unsigned(JOB_INDEX_FIELD, row_index)
                writer.add_document(document)
        if not job_ids:
            raise RuntimeError("source CSV contains no jobs")
        writer.commit()
        writer.wait_merging_threads()
        index.reload()
        del writer, index
        if sha256_file(jobs_csv) != dataset_sha256:
            raise RuntimeError("source CSV bytes changed during Tantivy build")
        job_ids_path = partial / "job-ids.json"
        job_ids_path.write_bytes(canonical_json(job_ids) + b"\n")
        taxonomy_path = partial / "filter-taxonomy.json"
        atomic_json(
            taxonomy_path,
            {
                "schema_version": 1,
                "location_code_to_terms": {
                    code: sorted(terms) for code, terms in sorted(locations.items())
                },
                "duty_code_to_terms": {
                    code: sorted(terms) for code, terms in sorted(duties.items())
                },
            },
        )
        prefix = clean_prefix.as_posix()
        if correction_candidate_path is None and correction_attestation_path is None:
            query_corrections: dict[str, object] = {"enabled": False}
        elif correction_candidate_path is None or correction_attestation_path is None:
            raise RuntimeError(
                "enabled query corrections require both candidate and promotion attestation"
            )
        else:
            CorpusQueryCompiler.from_promoted_paths(
                correction_candidate_path, correction_attestation_path
            )
            corrections_path = partial / "query-corrections.json"
            attestation_path = partial / "query-corrections.attestation.json"
            shutil.copyfile(correction_candidate_path, corrections_path)
            shutil.copyfile(correction_attestation_path, attestation_path)
            query_corrections = {
                "enabled": True,
                "artifact_path": f"{prefix}/{corrections_path.name}",
                "artifact_sha256": sha256_file(corrections_path),
                "promotion_attestation_path": f"{prefix}/{attestation_path.name}",
                "promotion_attestation_sha256": sha256_file(attestation_path),
            }
        tree = _tree(index_directory)
        tree_sha256 = _tree_sha256(tree)
        build_manifest = {
            "schema_version": 1,
            "complete": True,
            "builder": "tantivy_index_pipeline.py",
            "engine": ENGINE,
            "dataset_sha256": dataset_sha256,
            "jobs_sha256": sha256_file(job_ids_path),
            "job_row_order_sha256": order.hexdigest(),
            "rows": len(job_ids),
            "index_sha256": tree_sha256,
            "index_tree": tree,
            "taxonomy_sha256": sha256_file(taxonomy_path),
            "query_corrections": query_corrections,
            "lexical_policy_version": LEXICAL_POLICY_VERSION,
            "lexical_policy_sha256": lexical_policy_sha256(),
            "tokenizers": TOKENIZERS,
            "source_fields": SOURCE_FIELDS,
            "source_csv_fields": sorted(required),
            "salary_filter_excluded_rows": salary_filter_excluded_rows,
        }
        build_manifest_path = partial / "build-manifest.json"
        atomic_json(build_manifest_path, build_manifest)
        component = {
            "schema_version": 1,
            "complete": True,
            "engine": ENGINE,
            "jobs_sha256": sha256_file(job_ids_path),
            "job_row_order_sha256": order.hexdigest(),
            "index_sha256": tree_sha256,
            "index_directory": f"{prefix}/index",
            "index_files": [f"{prefix}/index/{item['path']}" for item in tree],
            "taxonomy_path": f"{prefix}/{taxonomy_path.name}",
            "job_ids_path": f"{prefix}/{job_ids_path.name}",
            "query_corrections": query_corrections,
            "build_manifest_path": f"{prefix}/{build_manifest_path.name}",
            "build_manifest_sha256": sha256_file(build_manifest_path),
            "schema_fields": SCHEMA_FIELDS,
            "field_boosts": FIELD_BOOSTS,
            "lexical_policy_version": LEXICAL_POLICY_VERSION,
            "lexical_policy_sha256": lexical_policy_sha256(),
            "tokenizers": TOKENIZERS,
            "source_fields": SOURCE_FIELDS,
            "filter_semantics": FILTER_SEMANTICS,
            "updated_at_field": UPDATED_AT_FIELD,
            "temporal_filter_semantics": TEMPORAL_SEMANTICS,
        }
        atomic_json(partial / "manifest.json", component)
        validate_tantivy(partial, jobs_csv=jobs_csv, artifact_prefix=prefix)
        partial.replace(output)
        return component
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise


def _local(output: Path, value: object, prefix: str) -> Path:
    if not isinstance(value, str) or not value.startswith(prefix.rstrip("/") + "/"):
        raise RuntimeError("Tantivy artifact path differs from its component prefix")
    suffix = value.removeprefix(prefix.rstrip("/") + "/")
    path = PurePosixPath(suffix)
    if not suffix or path.is_absolute() or ".." in path.parts:
        raise RuntimeError("Tantivy artifact path is unsafe")
    return output.joinpath(*path.parts)


def _inventory(
    output: Path, manifest: Mapping[str, object], prefix: str
) -> tuple[dict[str, object], ...]:
    values = [
        artifact_entry(
            _local(output, manifest["taxonomy_path"], prefix), relative_to=output, kind="index"
        ),
        artifact_entry(
            _local(output, manifest["job_ids_path"], prefix), relative_to=output, kind="index"
        ),
        artifact_entry(
            _local(output, manifest["build_manifest_path"], prefix),
            relative_to=output,
            kind="evidence",
        ),
    ]
    correction = manifest["query_corrections"]
    if not isinstance(correction, dict):
        raise RuntimeError("Tantivy query correction mode must be an object")
    if correction != {"enabled": False}:
        values.extend(
            (
                artifact_entry(
                    _local(output, correction["artifact_path"], prefix),
                    relative_to=output,
                    kind="index",
                ),
                artifact_entry(
                    _local(output, correction["promotion_attestation_path"], prefix),
                    relative_to=output,
                    kind="evidence",
                ),
            )
        )
    files = manifest["index_files"]
    if not isinstance(files, list):
        raise RuntimeError("Tantivy index files must be an array")
    values.extend(
        artifact_entry(_local(output, path, prefix), relative_to=output, kind="index")
        for path in files
    )
    return tuple(values)


def validate_tantivy(output: Path, *, jobs_csv: Path, artifact_prefix: str) -> dict[str, object]:
    manifest = read_json_object(output / "manifest.json", "Tantivy component")
    exact_keys(manifest, COMPONENT_KEYS, "Tantivy component")
    expected = {
        "schema_version": 1,
        "complete": True,
        "engine": ENGINE,
        "schema_fields": SCHEMA_FIELDS,
        "field_boosts": FIELD_BOOSTS,
        "lexical_policy_version": LEXICAL_POLICY_VERSION,
        "lexical_policy_sha256": lexical_policy_sha256(),
        "tokenizers": TOKENIZERS,
        "source_fields": SOURCE_FIELDS,
        "query_corrections": manifest["query_corrections"],
        "filter_semantics": FILTER_SEMANTICS,
        "updated_at_field": UPDATED_AT_FIELD,
        "temporal_filter_semantics": TEMPORAL_SEMANTICS,
    }
    if any(manifest[name] != value for name, value in expected.items()):
        raise RuntimeError("Tantivy component policy differs")
    for name in ("jobs_sha256", "job_row_order_sha256", "index_sha256", "build_manifest_sha256"):
        require_sha256(manifest[name], f"Tantivy {name}")
    prefix = PurePosixPath(artifact_prefix).as_posix()
    index_directory = _local(output, manifest["index_directory"], prefix)
    tree = _tree(index_directory)
    if _tree_sha256(tree) != manifest["index_sha256"]:
        raise RuntimeError("Tantivy index tree differs")
    expected_files = [f"{prefix}/index/{item['path']}" for item in tree]
    if manifest["index_files"] != expected_files:
        raise RuntimeError("Tantivy index file inventory differs")
    job_ids_path = _local(output, manifest["job_ids_path"], prefix)
    if sha256_file(job_ids_path) != manifest["jobs_sha256"]:
        raise RuntimeError("Tantivy job ID bytes differ")
    try:
        job_ids = json.loads(job_ids_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Tantivy job IDs cannot be read") from error
    if (
        not isinstance(job_ids, list)
        or not job_ids
        or any(
            not isinstance(value, str) or not value.isascii() or not value.isdecimal()
            for value in job_ids
        )
        or len(set(job_ids)) != len(job_ids)
    ):
        raise RuntimeError("Tantivy job IDs violate canonical order")
    order = hashlib.sha256()
    for job_id in job_ids:
        order.update(cast(str, job_id).encode() + b"\n")
    if order.hexdigest() != manifest["job_row_order_sha256"]:
        raise RuntimeError("Tantivy row-order SHA-256 differs")
    build_path = _local(output, manifest["build_manifest_path"], prefix)
    if sha256_file(build_path) != manifest["build_manifest_sha256"]:
        raise RuntimeError("Tantivy build-manifest bytes differ")
    build = read_json_object(build_path, "Tantivy build manifest")
    exact_keys(build, BUILD_KEYS, "Tantivy build manifest")
    build_expected = {
        "schema_version": 1,
        "complete": True,
        "builder": "tantivy_index_pipeline.py",
        "engine": ENGINE,
        "dataset_sha256": sha256_file(jobs_csv),
        "jobs_sha256": manifest["jobs_sha256"],
        "job_row_order_sha256": manifest["job_row_order_sha256"],
        "rows": len(job_ids),
        "index_sha256": manifest["index_sha256"],
        "index_tree": tree,
        "lexical_policy_version": LEXICAL_POLICY_VERSION,
        "lexical_policy_sha256": lexical_policy_sha256(),
        "tokenizers": TOKENIZERS,
        "source_fields": SOURCE_FIELDS,
        "salary_filter_excluded_rows": build["salary_filter_excluded_rows"],
    }
    if any(build[name] != value for name, value in build_expected.items()):
        raise RuntimeError("Tantivy build lineage differs")
    if (
        not isinstance(build["salary_filter_excluded_rows"], int)
        or isinstance(build["salary_filter_excluded_rows"], bool)
        or not 0 <= build["salary_filter_excluded_rows"] <= len(job_ids)
    ):
        raise RuntimeError("Tantivy salary filter exclusion count is invalid")
    require_sha256(build["taxonomy_sha256"], "Tantivy build taxonomy SHA-256")
    taxonomy_path = _local(output, manifest["taxonomy_path"], prefix)
    if sha256_file(taxonomy_path) != build["taxonomy_sha256"]:
        raise RuntimeError("Tantivy taxonomy bytes differ")
    taxonomy = read_json_object(taxonomy_path, "Tantivy filter taxonomy")
    exact_keys(
        taxonomy,
        {"schema_version", "location_code_to_terms", "duty_code_to_terms"},
        "Tantivy filter taxonomy",
    )
    if (
        taxonomy["schema_version"] != 1
        or not taxonomy["location_code_to_terms"]
        or not taxonomy["duty_code_to_terms"]
    ):
        raise RuntimeError("Tantivy filter taxonomy is empty")
    correction = manifest["query_corrections"]
    if not isinstance(correction, dict):
        raise RuntimeError("Tantivy query correction mode must be an object")
    if correction != {"enabled": False}:
        exact_keys(
            correction,
            {
                "enabled",
                "artifact_path",
                "artifact_sha256",
                "promotion_attestation_path",
                "promotion_attestation_sha256",
            },
            "enabled Tantivy query corrections",
        )
        if correction["enabled"] is not True:
            raise RuntimeError("query correction enabled flag must be boolean")
        corrections_path = _local(output, correction["artifact_path"], prefix)
        attestation_path = _local(output, correction["promotion_attestation_path"], prefix)
        if (
            sha256_file(corrections_path) != correction["artifact_sha256"]
            or sha256_file(attestation_path) != correction["promotion_attestation_sha256"]
        ):
            raise RuntimeError("enabled query correction bytes differ")
        CorpusQueryCompiler.from_promoted_paths(corrections_path, attestation_path)
    tantivy.Index.open(str(index_directory)).searcher()
    return {
        "passed": True,
        "rows": len(job_ids),
        "index_sha256": manifest["index_sha256"],
        "manifest_sha256": sha256_file(output / "manifest.json"),
        "artifacts": list(_inventory(output, manifest, prefix)),
    }


def _s3(
    output: Path,
    *,
    jobs_csv: Path,
    artifact_prefix: str,
    bucket: str,
    prefix: str,
    expected_owner: str,
    profile: str | None,
    region: str,
    publish: bool,
) -> dict[str, object]:
    validation = validate_tantivy(output, jobs_csv=jobs_csv, artifact_prefix=artifact_prefix)
    if region != "us-west-2":
        raise RuntimeError("Tantivy S3 publication is pinned to us-west-2")
    session = boto3.Session(profile_name=profile, region_name=region)
    identity = cast(AwsIdentity, session.client("sts")).get_caller_identity()
    if identity.get("Account") != expected_owner:
        raise RuntimeError("AWS caller identity differs from expected S3 owner")
    manifest_path = output / "manifest.json"
    manifest_sha256 = sha256_file(manifest_path)
    clean_prefix = prefix.strip("/")
    if not clean_prefix or clean_prefix.rsplit("/", 1)[-1] != manifest_sha256:
        raise RuntimeError("S3 prefix must end with the manifest SHA-256")
    s3 = session.client("s3")
    artifacts = validation["artifacts"]
    if publish:
        return publish_s3_directory(
            root=output,
            bucket=bucket,
            prefix=prefix,
            expected_owner=expected_owner,
            artifacts=artifacts,
            s3=s3,
        )
    parsed = verify_local_inventory(output, artifacts)
    verify_s3_inventory(
        bucket=bucket,
        prefix=prefix,
        expected_owner=expected_owner,
        artifacts=parsed,
        s3=s3,
    )
    verify_s3_object(
        bucket=bucket,
        key=f"{clean_prefix}/manifest.json",
        expected_owner=expected_owner,
        expected_sha256=manifest_sha256,
        expected_size=manifest_path.stat().st_size,
        s3=s3,
    )
    return {
        "passed": True,
        "manifest_sha256": manifest_sha256,
        "s3_prefix": f"s3://{bucket}/{clean_prefix}/",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--jobs-csv", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--artifact-prefix", default=DEFAULT_ARTIFACT_PREFIX)
    build.add_argument("--location-code-field", default=DEFAULT_LOCATION_CODE_FIELD)
    build.add_argument("--location-term-field", default=DEFAULT_LOCATION_TERM_FIELD)
    build.add_argument("--location-taxonomy-csv", type=Path)
    build.add_argument("--duty-code-field", default=DEFAULT_DUTY_CODE_FIELD)
    build.add_argument("--duty-term-field", default=DEFAULT_DUTY_TERM_FIELD)
    build.add_argument("--duty-taxonomy-csv", type=Path)
    build.add_argument("--visibility-field")
    build.add_argument("--modified-at-field", default=DEFAULT_MODIFIED_AT_FIELD)
    build.add_argument("--query-correction-candidate", type=Path)
    build.add_argument("--query-correction-attestation", type=Path)
    validate = commands.add_parser("validate")
    validate.add_argument("--output", type=Path, required=True)
    validate.add_argument("--jobs-csv", type=Path, required=True)
    validate.add_argument("--artifact-prefix", default=DEFAULT_ARTIFACT_PREFIX)
    for name in ("publish-s3", "verify-s3"):
        command = commands.add_parser(name)
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--jobs-csv", type=Path, required=True)
        command.add_argument("--artifact-prefix", default=DEFAULT_ARTIFACT_PREFIX)
        command.add_argument("--bucket", required=True)
        command.add_argument("--prefix", required=True)
        command.add_argument("--expected-owner", default="378849533305")
        command.add_argument("--profile")
        command.add_argument("--region", default="us-west-2")
    args = parser.parse_args()
    if args.command == "build":
        result = build_tantivy(
            jobs_csv=args.jobs_csv,
            output=args.output,
            artifact_prefix=args.artifact_prefix,
            location_code_field=args.location_code_field,
            location_term_field=args.location_term_field,
            location_taxonomy_csv=args.location_taxonomy_csv,
            duty_code_field=args.duty_code_field,
            duty_term_field=args.duty_term_field,
            duty_taxonomy_csv=args.duty_taxonomy_csv,
            visibility_field=args.visibility_field,
            modified_at_field=args.modified_at_field,
            correction_candidate_path=args.query_correction_candidate,
            correction_attestation_path=args.query_correction_attestation,
        )
    elif args.command == "validate":
        result = validate_tantivy(
            args.output, jobs_csv=args.jobs_csv, artifact_prefix=args.artifact_prefix
        )
    else:
        result = _s3(
            args.output,
            jobs_csv=args.jobs_csv,
            artifact_prefix=args.artifact_prefix,
            bucket=args.bucket,
            prefix=args.prefix,
            expected_owner=args.expected_owner,
            profile=args.profile,
            region=args.region,
            publish=args.command == "publish-s3",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
