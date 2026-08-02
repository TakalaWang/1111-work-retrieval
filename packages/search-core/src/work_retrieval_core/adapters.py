from __future__ import annotations

import hashlib
import html
import json
import math
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import BoundedSemaphore
from time import monotonic
from typing import Any, Protocol, cast, runtime_checkable

import boto3  # type: ignore[import-untyped]
import numpy as np
import numpy.typing as npt
import tantivy
from botocore.config import Config  # type: ignore[import-untyped]

from work_retrieval_core.constraints import MANAGEMENT_REQUIRED_TOKEN, NO_EXPERIENCE_VALUES
from work_retrieval_core.engine import (
    CandidateEvidence,
    CandidateRequest,
    CompiledQuery,
    QueryRewrite,
)
from work_retrieval_core.manifest import (
    MODEL,
    MODEL_REVISION,
    SOURCE_EMBEDDING_DIMENSION,
    WHOLE_DIMENSION,
    WHOLE_DOCUMENT_FIELDS,
    WHOLE_DOCUMENT_POLICY_VERSION,
    WHOLE_DOCUMENT_TEMPLATE_SHA256,
    WHOLE_PROJECTION,
    RuntimeManifest,
)
from work_retrieval_core.serialization import (
    FULL_JOB_FIELDS,
    canonical_code,
)

QUERY_PROMPT = (
    "Instruct: Given a job search query, retrieve relevant job postings matching the user's "
    "intent\nQuery: "
)
FIELD_BOOSTS = {
    "title": 15.0,
    "duty": 8.0,
    "skills": 6.0,
    "industry": 1.0,
    "body": 0.5,
}
FILTER_FIELDS = ("location_filter", "duty_filter")
VISIBILITY_FIELD = "visibility_filter"
EDUCATION_FILTER_FIELD = "education_filter"
JOB_ATTRIBUTE_FILTER_FIELD = "job_attribute_filter"
WORK_SHIFT_FILTER_FIELD = "work_shift_filter"
EXPERIENCE_FILTER_FIELD = "experience_filter"
MANAGEMENT_FILTER_FIELD = "management_filter"
UPDATED_AT_FIELD = "updated_at_epoch_ms"
MONTHLY_SALARY_LOWER_FIELD = "monthly_salary_lower_filter"
MONTHLY_SALARY_RECALL_FIELD = "monthly_salary_recall_filter"
NUMERIC_FILTER_FIELDS = (MONTHLY_SALARY_LOWER_FIELD, MONTHLY_SALARY_RECALL_FIELD)
JOB_INDEX_FIELD = "job_index"
FILTER_SEMANTICS = (
    "visibility AND (location OR) AND (duty OR) AND optional education "
    "AND optional monthly salary/job attribute/work shift/no-experience/management, "
    "applied before Top-K"
)
LATIN = re.compile(r"[a-z0-9][a-z0-9+#.\-]*")
HTML_TAG = re.compile(r"<[^>]*>")
URL = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
EXACT_DENSE_CHUNK_ROWS = 24_576
EXACT_DENSE_MAX_ELIGIBLE_ROWS = 250_000
EXACT_DENSE_TIMEOUT_SECONDS = 2.0
LEXICAL_POLICY_VERSION = "2026-08-02-pretokenized-v3"
ENDPOINT_NAME = "qwen3-embedding-8b-20260801-031826"
ENDPOINT_CONFIG_NAME = ENDPOINT_NAME
ENDPOINT_MODEL_NAME = ENDPOINT_NAME
TEI_IMAGE_URI = (
    "246618743249.dkr.ecr.us-west-2.amazonaws.com/tei@"
    "sha256:45be982bc2eb434d1dccd7d05ca4e3ab63972f41d8030f1fe8bc809c2bcbf564"
)
TEI_ENVIRONMENT = {
    "AUTO_TRUNCATE": "true",
    "DTYPE": "float16",
    "HF_MODEL_ID": MODEL,
    "HF_MODEL_REVISION": MODEL_REVISION,
    "MAX_BATCH_TOKENS": "4096",
    "MAX_CLIENT_BATCH_SIZE": "32",
    "MAX_INPUT_LENGTH": "512",
}
TEXT_FIELDS = ("title", "duty", "skills", "industry", "body")
RAW_FILTER_FIELDS = (
    "location_filter",
    "duty_filter",
    "visibility_filter",
    EDUCATION_FILTER_FIELD,
    JOB_ATTRIBUTE_FILTER_FIELD,
    WORK_SHIFT_FILTER_FIELD,
    EXPERIENCE_FILTER_FIELD,
    MANAGEMENT_FILTER_FIELD,
)
TOKENIZERS = {**dict.fromkeys(TEXT_FIELDS, "default"), **dict.fromkeys(RAW_FILTER_FIELDS, "raw")}
SOURCE_FIELDS = {
    "title": ["title"],
    "duty": ["duty_minor", "duty_middle", "duty_major"],
    "skills": ["computer_skills", "work_skills", "professional_certifications"],
    "industry": ["industry_minor", "industry_middle", "industry_major"],
    "body": [
        field
        for _label, field in FULL_JOB_FIELDS
        if field
        not in {
            "title",
            "duty_minor",
            "duty_middle",
            "duty_major",
            "computer_skills",
            "work_skills",
            "professional_certifications",
            "industry_minor",
            "industry_middle",
            "industry_major",
        }
    ],
}


def lexical_policy_sha256() -> str:
    policy = {
        "version": LEXICAL_POLICY_VERSION,
        "field_boosts": FIELD_BOOSTS,
        "source_fields": SOURCE_FIELDS,
        "tokenizers": TOKENIZERS,
    }
    return hashlib.sha256(
        json.dumps(policy, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


class SageMakerRuntime(Protocol):
    def invoke_endpoint(self, **kwargs: object) -> Mapping[str, object]: ...


class SageMakerControlPlane(Protocol):
    def describe_endpoint(self, **kwargs: object) -> Mapping[str, object]: ...

    def describe_endpoint_config(self, **kwargs: object) -> Mapping[str, object]: ...

    def describe_model(self, **kwargs: object) -> Mapping[str, object]: ...


@runtime_checkable
class ReadableBody(Protocol):
    def read(self) -> bytes: ...


class EligibleRows(Protocol):
    def eligible_indices(
        self, request: CandidateRequest, *, max_rows: int
    ) -> npt.NDArray[np.int64]: ...


class QueryEncoder(Protocol):
    def encode(self, query: str) -> npt.NDArray[np.float32]: ...


@dataclass(frozen=True, slots=True)
class FilterTaxonomy:
    location_code_to_terms: Mapping[str, tuple[str, ...]]
    duty_code_to_terms: Mapping[str, tuple[str, ...]]
    location_term_to_codes: Mapping[str, tuple[str, ...]]
    duty_term_to_codes: Mapping[str, tuple[str, ...]]

    @classmethod
    def from_path(cls, path: Path) -> FilterTaxonomy:
        raw = _json_object(path, "filter taxonomy")
        _exact_keys(
            raw,
            {"schema_version", "location_code_to_terms", "duty_code_to_terms"},
            "filter taxonomy",
        )
        if raw["schema_version"] != 1:
            raise RuntimeError("filter taxonomy schema_version must equal 1")
        locations = _code_term_mapping(raw["location_code_to_terms"], "location")
        duties = _code_term_mapping(raw["duty_code_to_terms"], "duty")
        return cls(locations, duties, _reverse(locations), _reverse(duties))

    def resolve_locations(self, codes: tuple[str, ...]) -> tuple[str, ...] | None:
        return _resolve_codes(codes, self.location_code_to_terms)

    def resolve_duties(self, codes: tuple[str, ...]) -> tuple[str, ...] | None:
        return _resolve_codes(codes, self.duty_code_to_terms)

    def location_codes_for_term(self, term: str | None) -> tuple[str, ...]:
        return self.location_term_to_codes.get(canonical_code(term), ())

    def duty_codes_for_terms(self, terms: tuple[str | None, ...]) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    code
                    for term in terms
                    for code in self.duty_term_to_codes.get(canonical_code(term), ())
                }
            )
        )


@dataclass(frozen=True, slots=True)
class CorpusQueryCompiler:
    corrections: Mapping[str, str]

    @classmethod
    def identity(cls) -> CorpusQueryCompiler:
        """Return the compiler only for an explicitly disabled manifest branch."""
        return cls({})

    @classmethod
    def from_promoted_paths(
        cls, artifact_path: Path, attestation_path: Path
    ) -> CorpusQueryCompiler:
        raw = _json_object(artifact_path, "query corrections")
        _exact_keys(
            raw,
            {
                "schema_version",
                "complete",
                "publication_allowed",
                "source_policy",
                "test_jd_used",
                "uses_ground_truth",
                "uses_behavior_logs",
                "train_cutoff_exclusive",
                "max_source_timestamp",
                "source_manifest_sha256",
                "evidence_sha256",
                "minimum_support",
                "corrections",
            },
            "query corrections",
        )
        if (
            raw["schema_version"] != 1
            or raw["complete"] is not True
            or raw["publication_allowed"] is not False
            or raw["source_policy"] != "train_jd_only"
            or raw["test_jd_used"] is not False
            or raw["uses_ground_truth"] is not False
            or raw["uses_behavior_logs"] is not False
        ):
            raise RuntimeError("query corrections are not train-JD corpus safe")
        cutoff = _aware_timestamp(raw["train_cutoff_exclusive"], "train cutoff")
        maximum = _aware_timestamp(raw["max_source_timestamp"], "max source timestamp")
        if maximum >= cutoff:
            raise RuntimeError("query corrections include post-cutoff source data")
        for name in ("source_manifest_sha256", "evidence_sha256"):
            _sha(raw[name], f"query corrections {name}")
        minimum_support = _integer(raw["minimum_support"], "query correction minimum support")
        if minimum_support < 1:
            raise RuntimeError("query correction minimum support must be positive")
        values = _object(raw["corrections"], "query corrections mapping")
        corrections: dict[str, str] = {}
        for source, target in values.items():
            normalized_source = canonical_code(source if isinstance(source, str) else None)
            normalized_target = canonical_code(target if isinstance(target, str) else None)
            if (
                not normalized_source
                or not normalized_target
                or normalized_source != source
                or normalized_target != target
                or normalized_source == normalized_target
            ):
                raise RuntimeError("query corrections contain a non-canonical rule")
            corrections[normalized_source] = normalized_target
        if not corrections:
            raise RuntimeError("enabled query corrections cannot be empty")
        candidate_sha256 = _sha(
            hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
            "query correction candidate",
        )
        attestation = _json_object(attestation_path, "query correction promotion attestation")
        _exact_keys(
            attestation,
            {
                "schema_version",
                "complete",
                "attestation_kind",
                "candidate_sha256",
                "promotion_report_sha256",
                "publication_allowed",
                "evaluator_kind",
                "significant",
                "primary_metric",
                "absolute_delta",
                "evaluation_split_sha256",
                "baseline_run_sha256",
                "candidate_run_sha256",
            },
            "query correction promotion attestation",
        )
        delta = attestation["absolute_delta"]
        if (
            attestation["schema_version"] != 1
            or attestation["complete"] is not True
            or attestation["attestation_kind"] != "fixed-input-query-correction-promotion"
            or attestation["candidate_sha256"] != candidate_sha256
            or attestation["publication_allowed"] is not True
            or attestation["evaluator_kind"] != "organizer"
            or attestation["significant"] is not True
            or attestation["primary_metric"] != "ndcg_at_10"
            or isinstance(delta, bool)
            or not isinstance(delta, (int, float))
            or not math.isfinite(delta)
            or delta <= 0
        ):
            raise RuntimeError("query correction promotion attestation did not pass")
        for name in (
            "promotion_report_sha256",
            "evaluation_split_sha256",
            "baseline_run_sha256",
            "candidate_run_sha256",
        ):
            _sha(attestation[name], f"query correction attestation {name}")
        return cls(corrections)

    def compile(self, text: str) -> CompiledQuery:
        normalized = canonical_code(text)
        corrected = self.corrections.get(normalized)
        if corrected is None and normalized:
            tokens = normalized.split()
            replaced = [self.corrections.get(token, token) for token in tokens]
            if replaced != tokens:
                corrected = " ".join(replaced)
        if corrected is None:
            return CompiledQuery((text,))
        return CompiledQuery(
            (text, corrected),
            (QueryRewrite(normalized, corrected, "train_jd_corpus_v1"),),
        )


@dataclass(frozen=True, slots=True)
class EmbeddingShard:
    vectors_path: str
    row_start: int
    row_end: int


@dataclass(frozen=True, slots=True)
class WholeEmbeddingLayout:
    job_ids_path: str
    shards: tuple[EmbeddingShard, ...]

    @classmethod
    def from_path(cls, path: Path, manifest: RuntimeManifest) -> WholeEmbeddingLayout:
        raw = _json_object(path, "whole embedding component")
        expected_keys = {
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
            "source_manifest_path",
            "source_manifest_sha256",
            "source_inventory_path",
            "source_inventory_sha256",
            "job_ids_path",
            "shards",
        }
        _exact_keys(raw, expected_keys, "whole embedding component")
        whole = manifest.whole_embedding
        expected = {
            "schema_version": 1,
            "complete": True,
            "model": MODEL,
            "revision": MODEL_REVISION,
            "source_dimension": SOURCE_EMBEDDING_DIMENSION,
            "dimension": WHOLE_DIMENSION,
            "projection": WHOLE_PROJECTION,
            "dtype": "float16",
            "normalized": True,
            "rows": whole.rows,
            "dataset_sha256": whole.dataset_sha256,
            "jobs_sha256": whole.jobs_sha256,
            "job_row_order_sha256": whole.job_row_order_sha256,
            "document_policy_version": WHOLE_DOCUMENT_POLICY_VERSION,
            "document_template_sha256": WHOLE_DOCUMENT_TEMPLATE_SHA256,
            "document_fields": list(WHOLE_DOCUMENT_FIELDS),
            "query_prompt": QUERY_PROMPT,
        }
        _require_equal(raw, expected, "whole embedding component")
        source_manifest_path = _artifact_path(
            raw["source_manifest_path"], "whole embedding source manifest"
        )
        source_manifest_sha256 = _sha(
            raw["source_manifest_sha256"], "whole embedding source manifest"
        )
        source_inventory_path = _artifact_path(
            raw["source_inventory_path"], "whole embedding source inventory"
        )
        source_inventory_sha256 = _sha(
            raw["source_inventory_sha256"], "whole embedding source inventory"
        )
        whole_prefix = str(Path(whole.manifest_path).parent.as_posix()) + "/"
        for source_path, source_sha256, label in (
            (source_manifest_path, source_manifest_sha256, "source manifest"),
            (source_inventory_path, source_inventory_sha256, "source inventory"),
        ):
            if not source_path.startswith(whole_prefix):
                raise RuntimeError(f"whole embedding {label} escapes its component")
            source_artifact = manifest.artifact(source_path)
            if (
                source_artifact is None
                or source_artifact.kind != "evidence"
                or source_artifact.sha256 != source_sha256
            ):
                raise RuntimeError(f"whole embedding {label} is absent or differs")
        job_ids_path = _artifact_path(raw["job_ids_path"], "whole embedding job IDs")
        if not job_ids_path.startswith(whole_prefix):
            raise RuntimeError("whole embedding job IDs escape its component")
        _require_inventory_kind(manifest, job_ids_path, "embedding")
        raw_shards = raw["shards"]
        if not isinstance(raw_shards, list) or not raw_shards:
            raise RuntimeError("whole embedding shards must be a non-empty array")
        parsed: list[EmbeddingShard] = []
        expected_start = 0
        for position, value in enumerate(raw_shards):
            shard = _object(value, f"whole embedding shard {position}")
            _exact_keys(
                shard,
                {
                    "vectors_path",
                    "vectors_sha256",
                    "source_vectors_sha256",
                    "row_start",
                    "row_end",
                    "rows",
                    "dimension",
                },
                f"whole embedding shard {position}",
            )
            start = _integer(shard["row_start"], f"shard {position} row_start")
            end = _integer(shard["row_end"], f"shard {position} row_end")
            rows = _integer(shard["rows"], f"shard {position} rows")
            if (
                start != expected_start
                or end <= start
                or rows != end - start
                or shard["dimension"] != WHOLE_DIMENSION
            ):
                raise RuntimeError("whole embedding shards are not contiguous 1024d rows")
            vectors_path = _artifact_path(shard["vectors_path"], f"shard {position} vectors")
            if not vectors_path.startswith(whole_prefix):
                raise RuntimeError("whole embedding vector shard escapes its component")
            _require_inventory_kind(manifest, vectors_path, "embedding")
            vector_artifact = manifest.artifact(vectors_path)
            if vector_artifact is None or vector_artifact.sha256 != _sha(
                shard["vectors_sha256"], f"shard {position} vectors"
            ):
                raise RuntimeError("whole embedding derived shard SHA-256 differs")
            _sha(shard["source_vectors_sha256"], f"shard {position} source vectors")
            parsed.append(EmbeddingShard(vectors_path, start, end))
            expected_start = end
        if expected_start != whole.rows:
            raise RuntimeError("whole embedding shard rows differ from incumbent")
        return cls(job_ids_path, tuple(parsed))


@dataclass(frozen=True, slots=True)
class TantivyLayout:
    index_directory: str
    taxonomy_path: str
    job_ids_path: str
    query_corrections_path: str | None
    query_corrections_attestation_path: str | None

    @classmethod
    def from_path(cls, path: Path, manifest: RuntimeManifest) -> TantivyLayout:
        raw = _json_object(path, "Tantivy component")
        expected_keys = {
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
        _exact_keys(raw, expected_keys, "Tantivy component")
        temporal = manifest.temporal_tantivy
        _require_equal(
            raw,
            {
                "schema_version": 1,
                "complete": True,
                "engine": "tantivy v0.26.0, index_format v7",
                "jobs_sha256": temporal.jobs_sha256,
                "job_row_order_sha256": temporal.job_row_order_sha256,
                "index_sha256": temporal.index_sha256,
                "index_directory": "indexes/tantivy-bm25-temporal-v3/index",
                "schema_fields": [
                    "title",
                    "duty",
                    "skills",
                    "industry",
                    "body",
                    "location_filter",
                    "duty_filter",
                    "visibility_filter",
                    "education_filter",
                    "job_attribute_filter",
                    "work_shift_filter",
                    "experience_filter",
                    "management_filter",
                    "updated_at_epoch_ms",
                    "monthly_salary_lower_filter",
                    "monthly_salary_recall_filter",
                    "job_index",
                ],
                "field_boosts": FIELD_BOOSTS,
                "lexical_policy_version": LEXICAL_POLICY_VERSION,
                "lexical_policy_sha256": lexical_policy_sha256(),
                "tokenizers": TOKENIZERS,
                "source_fields": SOURCE_FIELDS,
                "filter_semantics": FILTER_SEMANTICS,
                "updated_at_field": UPDATED_AT_FIELD,
                "temporal_filter_semantics": temporal.temporal_filter_semantics,
            },
            "Tantivy component",
        )
        files = raw["index_files"]
        if not isinstance(files, list) or not files:
            raise RuntimeError("Tantivy component must declare index_files")
        directory = _artifact_path(raw["index_directory"], "Tantivy index directory")
        taxonomy_path = _artifact_path(raw["taxonomy_path"], "Tantivy filter taxonomy")
        job_ids_path = _artifact_path(raw["job_ids_path"], "Tantivy job IDs")
        build_manifest_path = _artifact_path(raw["build_manifest_path"], "Tantivy build manifest")
        build_manifest_sha256 = _sha(raw["build_manifest_sha256"], "Tantivy build manifest")
        prefix = directory + "/"
        for file in files:
            file_path = _artifact_path(file, "Tantivy index file")
            if not file_path.startswith(prefix):
                raise RuntimeError("Tantivy index file escapes its declared directory")
            _require_inventory_kind(manifest, file_path, "index")
        _require_inventory_kind(manifest, taxonomy_path, "index")
        _require_inventory_kind(manifest, job_ids_path, "index")
        build_artifact = manifest.artifact(build_manifest_path)
        if (
            build_artifact is None
            or build_artifact.kind != "evidence"
            or build_artifact.sha256 != build_manifest_sha256
        ):
            raise RuntimeError("Tantivy build manifest is absent or differs")
        component_prefix = str(Path(temporal.manifest_path).parent.as_posix()) + "/"
        if any(
            not path.startswith(component_prefix)
            for path in (
                directory,
                taxonomy_path,
                job_ids_path,
                build_manifest_path,
            )
        ):
            raise RuntimeError("Tantivy component artifact escapes its component")
        correction = _object(raw["query_corrections"], "Tantivy query corrections")
        if correction == {"enabled": False}:
            return cls(directory, taxonomy_path, job_ids_path, None, None)
        _exact_keys(
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
        correction_path = _artifact_path(
            correction["artifact_path"], "Tantivy query correction artifact"
        )
        correction_sha256 = _sha(correction["artifact_sha256"], "Tantivy query correction artifact")
        attestation_path = _artifact_path(
            correction["promotion_attestation_path"],
            "Tantivy query correction attestation",
        )
        attestation_sha256 = _sha(
            correction["promotion_attestation_sha256"],
            "Tantivy query correction attestation",
        )
        for candidate in (correction_path, attestation_path):
            if not candidate.startswith(component_prefix):
                raise RuntimeError("query correction artifact escapes its Tantivy component")
        correction_artifact = manifest.artifact(correction_path)
        attestation_artifact = manifest.artifact(attestation_path)
        if (
            correction_artifact is None
            or correction_artifact.kind != "index"
            or correction_artifact.sha256 != correction_sha256
            or attestation_artifact is None
            or attestation_artifact.kind != "evidence"
            or attestation_artifact.sha256 != attestation_sha256
        ):
            raise RuntimeError("enabled query correction artifacts are absent or differ")
        return cls(directory, taxonomy_path, job_ids_path, correction_path, attestation_path)


class TantivyBm25Retriever:
    """Fielded full-JD BM25 whose visibility, taxonomy, and time filters precede Top-K."""

    def __init__(
        self,
        index_directory: Path,
        job_ids: tuple[str, ...],
        taxonomy: FilterTaxonomy,
    ) -> None:
        _validate_tantivy_schema(index_directory / "meta.json")
        self._index = tantivy.Index.open(str(index_directory))
        self._schema = self._index.schema
        self._searcher = self._index.searcher()
        self._job_ids = job_ids
        self._taxonomy = taxonomy
        self._closed = False

    def retrieve(self, request: CandidateRequest, *, limit: int) -> tuple[CandidateEvidence, ...]:
        self._ensure_open()
        query = self._build_query(request, lexical=True)
        hits = self._searcher.search(query, limit=limit, count=False).hits
        indices = self._row_indices([address for _, address in hits])
        ranked = sorted(
            (
                (self._job_id(index), float(score))
                for (score, _address), index in zip(hits, indices, strict=True)
            ),
            key=lambda item: (-item[1], item[0]),
        )
        return tuple(
            CandidateEvidence(job_id, score, rank)
            for rank, (job_id, score) in enumerate(ranked, start=1)
        )

    def eligible_indices(
        self, request: CandidateRequest, *, max_rows: int = EXACT_DENSE_MAX_ELIGIBLE_ROWS
    ) -> npt.NDArray[np.int64]:
        self._ensure_open()
        query = self._build_query(request, lexical=False)
        count_result = self._searcher.search(query, limit=1, count=True)
        count = cast(int, cast(Any, count_result).count)
        if count > max_rows:
            raise RuntimeError("eligible universe exceeds its bounded materialization limit")
        if count == 0:
            return np.empty(0, dtype=np.int64)
        hits = self._searcher.search(query, limit=count, count=False).hits
        values = np.asarray(self._row_indices([address for _, address in hits]), dtype=np.int64)
        values.sort()
        if len(values) != count or len(np.unique(values)) != count:
            raise RuntimeError("Tantivy eligibility query returned duplicate rows")
        return values

    def close(self) -> None:
        self._closed = True

    def _build_query(self, request: CandidateRequest, *, lexical: bool) -> tantivy.Query:
        locations = self._taxonomy.resolve_locations(request.location_codes)
        duties = self._taxonomy.resolve_duties(request.duty_codes)
        if locations is None or duties is None:
            return tantivy.Query.empty_query()
        clauses: list[tuple[tantivy.Occur, tantivy.Query]] = []
        if lexical:
            tokens = list(
                dict.fromkeys(
                    token
                    for text in request.lexical_texts or (request.text,)
                    for token in lexical_tokens(text)
                )
            )
            han_bigrams = [
                token
                for token in tokens
                if len(token) >= 2 and all(_is_han(char) for char in token)
            ]
            non_han = [token for token in tokens if not all(_is_han(char) for char in token)]
            terms = han_bigrams + non_han or tokens
            if not terms:
                return tantivy.Query.empty_query()
            term_queries: list[tuple[tantivy.Occur, tantivy.Query]] = []
            for term in terms:
                term_queries.extend(
                    (
                        tantivy.Occur.Should,
                        tantivy.Query.boost_query(
                            tantivy.Query.term_query(self._schema, field, term),
                            boost,
                        ),
                    )
                    for field, boost in FIELD_BOOSTS.items()
                )
            clauses.append((tantivy.Occur.Must, tantivy.Query.boolean_query(term_queries)))
        else:
            clauses.append((tantivy.Occur.Must, tantivy.Query.all_query()))
        clauses.append(
            (
                tantivy.Occur.Must,
                _constant(tantivy.Query.term_query(self._schema, VISIBILITY_FIELD, "1")),
            )
        )
        for field, values in zip(
            FILTER_FIELDS,
            (locations, duties),
            strict=True,
        ):
            if values:
                clauses.append(
                    (
                        tantivy.Occur.Must,
                        _constant(tantivy.Query.term_set_query(self._schema, field, list(values))),
                    )
                )
        education = request.constraints.education
        if education is not None:
            clauses.append(
                (
                    tantivy.Occur.Must,
                    _constant(
                        tantivy.Query.term_set_query(
                            self._schema,
                            EDUCATION_FILTER_FIELD,
                            [education.degree, "不拘"],
                        )
                    ),
                )
            )
        salary = request.constraints.monthly_salary
        if salary is not None:
            salary_field = (
                MONTHLY_SALARY_LOWER_FIELD if salary.strict else MONTHLY_SALARY_RECALL_FIELD
            )
            clauses.append(
                (
                    tantivy.Occur.Must,
                    _constant(
                        tantivy.Query.range_query(
                            self._schema,
                            salary_field,
                            tantivy.FieldType.Unsigned,
                            salary.minimum,
                            None,
                            True,
                            True,
                            False,
                        )
                    ),
                )
            )
        job_attribute = request.constraints.job_attribute
        if job_attribute is not None:
            clauses.append(
                (
                    tantivy.Occur.Must,
                    _constant(
                        tantivy.Query.term_query(
                            self._schema,
                            JOB_ATTRIBUTE_FILTER_FIELD,
                            job_attribute.value,
                        )
                    ),
                )
            )
        work_shift = request.constraints.work_shift
        if work_shift is not None:
            clauses.append(
                (
                    tantivy.Occur.Must,
                    _constant(
                        tantivy.Query.term_query(
                            self._schema,
                            WORK_SHIFT_FILTER_FIELD,
                            work_shift.value,
                        )
                    ),
                )
            )
        if request.constraints.no_experience is not None:
            clauses.append(
                (
                    tantivy.Occur.Must,
                    _constant(
                        tantivy.Query.term_set_query(
                            self._schema,
                            EXPERIENCE_FILTER_FIELD,
                            list(NO_EXPERIENCE_VALUES),
                        )
                    ),
                )
            )
        if request.constraints.management is not None:
            clauses.append(
                (
                    tantivy.Occur.Must,
                    _constant(
                        tantivy.Query.term_query(
                            self._schema,
                            MANAGEMENT_FILTER_FIELD,
                            MANAGEMENT_REQUIRED_TOKEN,
                        )
                    ),
                )
            )
        minimum_epoch_ms = int(request.minimum_updated_at.timestamp() * 1000)
        clauses.append(
            (
                tantivy.Occur.Must,
                _constant(
                    tantivy.Query.range_query(
                        self._schema,
                        UPDATED_AT_FIELD,
                        tantivy.FieldType.Unsigned,
                        minimum_epoch_ms,
                        None,
                        True,
                        True,
                        False,
                    )
                ),
            )
        )
        return tantivy.Query.boolean_query(clauses)

    def _row_indices(self, addresses: list[tantivy.DocAddress]) -> list[int]:
        values = self._searcher.fast_field_values(JOB_INDEX_FIELD, addresses)
        if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
            raise RuntimeError("Tantivy job_index fast field is malformed")
        return [cast(int, value) for value in values]

    def _job_id(self, row: int) -> str:
        if not 0 <= row < len(self._job_ids):
            raise RuntimeError("Tantivy row is outside whole-Qwen job order")
        return self._job_ids[row]

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Tantivy retriever is closed")


class SageMakerQueryEncoder:
    def __init__(self, endpoint_name: str, runtime: SageMakerRuntime) -> None:
        if not endpoint_name.strip():
            raise ValueError("SageMaker embedding endpoint must be non-empty")
        self._endpoint_name = endpoint_name
        self._runtime = runtime

    @classmethod
    def from_aws(
        cls,
        *,
        endpoint_name: str,
        endpoint_config_name: str,
        model_name: str,
        region_name: str,
    ) -> SageMakerQueryEncoder:
        if (
            endpoint_name != ENDPOINT_NAME
            or endpoint_config_name != ENDPOINT_CONFIG_NAME
            or model_name != ENDPOINT_MODEL_NAME
            or region_name != "us-west-2"
        ):
            raise RuntimeError("embedding endpoint settings differ from the promoted identity")
        client_config = Config(
            connect_timeout=1,
            read_timeout=2,
            retries={"max_attempts": 2, "mode": "standard"},
        )
        control = boto3.client("sagemaker", region_name=region_name, config=client_config)
        _verify_endpoint_identity(
            control,
            endpoint_name=endpoint_name,
            endpoint_config_name=endpoint_config_name,
            model_name=model_name,
        )
        runtime = boto3.client(
            "sagemaker-runtime",
            region_name=region_name,
            config=client_config,
        )
        return cls(endpoint_name, runtime)

    def encode(self, query: str) -> npt.NDArray[np.float32]:
        response = self._runtime.invoke_endpoint(
            EndpointName=self._endpoint_name,
            Body=json.dumps({"inputs": [QUERY_PROMPT + query]}, ensure_ascii=False).encode(),
            ContentType="application/json",
            Accept="application/json",
        )
        body = response.get("Body")
        if not isinstance(body, ReadableBody):
            raise RuntimeError("SageMaker embedding response body is malformed")
        try:
            vector = np.asarray(json.loads(body.read()), dtype=np.float32)
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise RuntimeError("SageMaker embedding response is invalid JSON") from error
        if vector.shape != (1, SOURCE_EMBEDDING_DIMENSION) or not np.isfinite(vector).all():
            raise RuntimeError("SageMaker embedding response violates the 4096d contract")
        prefix = vector[0, :WHOLE_DIMENSION]
        norm = float(np.linalg.norm(prefix))
        if not np.isfinite(norm) or norm == 0:
            raise RuntimeError("SageMaker embedding response has an invalid norm")
        return np.asarray(prefix / norm, dtype=np.float32)


class WholeQwenExactRetriever:
    """Correctness-reference dense scan; an ANN may replace it only after promotion evidence."""

    def __init__(
        self,
        *,
        runtime_root: Path,
        layout: WholeEmbeddingLayout,
        job_ids: tuple[str, ...],
        eligible_rows: EligibleRows,
        encoder: QueryEncoder,
        chunk_rows: int = EXACT_DENSE_CHUNK_ROWS,
        max_eligible_rows: int = EXACT_DENSE_MAX_ELIGIBLE_ROWS,
        timeout_seconds: float = EXACT_DENSE_TIMEOUT_SECONDS,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if chunk_rows < 1 or max_eligible_rows < 1 or timeout_seconds <= 0:
            raise ValueError("dense scan bounds must be positive")
        if not layout.shards or layout.shards[-1].row_end != len(job_ids):
            raise ValueError("dense shard coverage must equal the immutable job order")
        self._job_ids = job_ids
        self._eligible_rows = eligible_rows
        self._encoder = encoder
        self._chunk_rows = chunk_rows
        self._max_eligible_rows = max_eligible_rows
        self._timeout_seconds = timeout_seconds
        self._clock = clock
        self._inflight = BoundedSemaphore(1)
        self._shards: tuple[tuple[EmbeddingShard, npt.NDArray[np.float16]], ...] = tuple(
            (shard, self._open_shard(runtime_root / shard.vectors_path, shard))
            for shard in layout.shards
        )
        self._closed = False

    def retrieve(self, request: CandidateRequest, *, limit: int) -> tuple[CandidateEvidence, ...]:
        if self._closed:
            raise RuntimeError("whole-Qwen retriever is closed")
        if limit < 1:
            raise ValueError("dense limit must be positive")
        if not self._inflight.acquire(blocking=False):
            raise RuntimeError("exact dense scanner is busy")
        try:
            deadline = self._clock() + self._timeout_seconds
            query = self._encoder.encode(request.text)
            if (
                query.shape != (WHOLE_DIMENSION,)
                or query.dtype != np.float32
                or not np.isfinite(query).all()
            ):
                raise RuntimeError("dense query embedding violates the 1024d contract")
            if self._clock() >= deadline:
                raise RuntimeError("exact dense scan exceeded its deadline")
            eligible = self._eligible_rows.eligible_indices(
                request,
                max_rows=self._max_eligible_rows,
            )
            if len(eligible) > self._max_eligible_rows:
                raise RuntimeError("exact dense eligible universe exceeds its production bound")
            if (
                eligible.ndim != 1
                or eligible.dtype != np.int64
                or (len(eligible) and (eligible[0] < 0 or eligible[-1] >= len(self._job_ids)))
                or (len(eligible) > 1 and np.any(eligible[1:] <= eligible[:-1]))
            ):
                raise RuntimeError("exact dense eligible rows violate the immutable row order")
            if self._clock() >= deadline:
                raise RuntimeError("exact dense scan exceeded its deadline")
            if not len(eligible):
                return ()
            best: list[tuple[float, int]] = []
            for layout, vectors in self._shards:
                start = int(np.searchsorted(eligible, layout.row_start, side="left"))
                end = int(np.searchsorted(eligible, layout.row_end, side="left"))
                selected = eligible[start:end]
                for offset in range(0, len(selected), self._chunk_rows):
                    if self._clock() >= deadline:
                        raise RuntimeError("exact dense scan exceeded its deadline")
                    rows = selected[offset : offset + self._chunk_rows]
                    local_rows = rows - layout.row_start
                    matrix = np.asarray(vectors[local_rows], dtype=np.float32)
                    scores = matrix @ query
                    local_limit = min(limit, len(scores))
                    indices = np.argpartition(scores, -local_limit)[-local_limit:]
                    best.extend((float(scores[index]), int(rows[index])) for index in indices)
                    best = sorted(
                        best,
                        key=lambda item: (-item[0], self._job_ids[item[1]]),
                    )[:limit]
            if self._clock() >= deadline:
                raise RuntimeError("exact dense scan exceeded its deadline")
            return tuple(
                CandidateEvidence(self._job_ids[row], score, rank)
                for rank, (score, row) in enumerate(best, start=1)
            )
        finally:
            self._inflight.release()

    def close(self) -> None:
        self._closed = True
        self._shards = ()

    @staticmethod
    def _open_shard(path: Path, shard: EmbeddingShard) -> npt.NDArray[np.float16]:
        try:
            values = np.load(path, mmap_mode="r", allow_pickle=False)
        except (OSError, ValueError) as error:
            raise RuntimeError("whole-Qwen vector shard cannot be opened") from error
        expected = (shard.row_end - shard.row_start, WHOLE_DIMENSION)
        if values.shape != expected or values.dtype != np.float16:
            raise RuntimeError("whole-Qwen vector shard violates its immutable shape")
        return cast(npt.NDArray[np.float16], values)


def load_job_ids(path: Path, *, expected_rows: int | None = None) -> tuple[str, ...]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("retrieval job IDs cannot be read") from error
    if (
        not isinstance(raw, list)
        or not raw
        or (expected_rows is not None and len(raw) != expected_rows)
        or any(
            not isinstance(value, str) or not value.isascii() or not value.isdecimal()
            for value in raw
        )
        or len(set(raw)) != len(raw)
    ):
        raise RuntimeError("retrieval job IDs violate the serving contract")
    return tuple(raw)


def _verify_endpoint_identity(
    control: SageMakerControlPlane,
    *,
    endpoint_name: str,
    endpoint_config_name: str,
    model_name: str,
) -> None:
    endpoint = control.describe_endpoint(EndpointName=endpoint_name)
    if (
        endpoint.get("EndpointStatus") != "InService"
        or endpoint.get("EndpointConfigName") != endpoint_config_name
    ):
        raise RuntimeError("embedding endpoint is not the promoted InService configuration")
    configuration = control.describe_endpoint_config(EndpointConfigName=endpoint_config_name)
    variants = configuration.get("ProductionVariants")
    if (
        not isinstance(variants, list)
        or len(variants) != 1
        or not isinstance(variants[0], dict)
        or variants[0].get("ModelName") != model_name
    ):
        raise RuntimeError("embedding endpoint configuration has an unexpected model")
    model = control.describe_model(ModelName=model_name)
    container = model.get("PrimaryContainer")
    if (
        not isinstance(container, dict)
        or container.get("Image") != TEI_IMAGE_URI
        or container.get("Environment") != TEI_ENVIRONMENT
    ):
        raise RuntimeError("embedding endpoint model image or environment differs")


def _validate_tantivy_schema(path: Path) -> None:
    meta = _json_object(path, "Tantivy meta")
    expected_schema: list[dict[str, object]] = []
    for name in TEXT_FIELDS + RAW_FILTER_FIELDS:
        expected_schema.append(
            {
                "name": name,
                "type": "text",
                "options": {
                    "indexing": {
                        "record": "position",
                        "fieldnorms": True,
                        "tokenizer": TOKENIZERS[name],
                    },
                    "stored": False,
                    "fast": False,
                },
            }
        )
    expected_schema.append(
        {
            "name": UPDATED_AT_FIELD,
            "type": "u64",
            "options": {
                "indexed": True,
                "fieldnorms": False,
                "fast": True,
                "stored": False,
            },
        }
    )
    for name in NUMERIC_FILTER_FIELDS:
        expected_schema.append(
            {
                "name": name,
                "type": "u64",
                "options": {
                    "indexed": True,
                    "fieldnorms": False,
                    "fast": False,
                    "stored": False,
                },
            }
        )
    expected_schema.append(
        {
            "name": JOB_INDEX_FIELD,
            "type": "u64",
            "options": {
                "indexed": False,
                "fieldnorms": False,
                "fast": True,
                "stored": False,
            },
        }
    )
    if meta.get("schema") != expected_schema:
        raise RuntimeError("Tantivy meta schema or tokenizers differ from the promoted policy")


def _aware_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError(f"{field} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"{field} must include a timezone")
    return parsed


def _constant(query: tantivy.Query) -> tantivy.Query:
    return tantivy.Query.const_score_query(query, 0.0)


def lexical_tokens(text: str | None) -> list[str]:
    value = text or ""
    if value.strip().casefold() == "null":
        return []
    value = html.unescape(value)
    value = HTML_TAG.sub(" ", value)
    value = ZERO_WIDTH.sub("", value)
    value = URL.sub(" ", value)
    value = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    tokens: list[str] = []
    han_run: list[str] = []

    def flush_han() -> None:
        if not han_run:
            return
        word = "".join(han_run)
        tokens.extend(word)
        tokens.extend(word[index : index + 2] for index in range(len(word) - 1))
        if len(word) <= 8:
            tokens.append(word)
        han_run.clear()

    for character in value:
        if _is_han(character):
            han_run.append(character)
        else:
            flush_han()
    flush_han()
    for match in LATIN.finditer(value):
        token = match.group()
        tokens.append(token)
        tokens.extend(part for part in re.split(r"[+#.\-]+", token) if part != token and part)
    return list(dict.fromkeys(tokens))


def _is_han(character: str) -> bool:
    code = ord(character)
    return 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF


def _json_object(path: Path, field: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{field} cannot be read as UTF-8 JSON") from error
    return dict(_object(value, field))


def _object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{field} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    if set(value) != expected:
        raise RuntimeError(f"{field} has missing or unknown keys")


def _require_equal(actual: Mapping[str, Any], expected: Mapping[str, object], field: str) -> None:
    if mismatches := [name for name, value in expected.items() if actual.get(name) != value]:
        raise RuntimeError(f"{field} differs in: {', '.join(mismatches)}")


def _artifact_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise RuntimeError(f"{field} must be a relative artifact path")
    path = Path(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeError(f"{field} is not a safe artifact path")
    return path.as_posix()


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"{field} must be a non-negative integer")
    return value


def _sha(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RuntimeError(f"{field} must be a lowercase SHA-256")
    return value


def _require_inventory_kind(manifest: RuntimeManifest, path: str, kind: str) -> None:
    artifact = manifest.artifact(path)
    if artifact is None or artifact.kind != kind:
        raise RuntimeError("component file is absent from immutable runtime inventory")


def _code_term_mapping(value: object, field: str) -> dict[str, tuple[str, ...]]:
    raw = _object(value, f"{field} taxonomy")
    parsed: dict[str, tuple[str, ...]] = {}
    for code, values in raw.items():
        if not isinstance(code, str) or not code.isascii() or not code.isdecimal():
            raise RuntimeError(f"{field} taxonomy has an invalid code")
        if not isinstance(values, list) or not values:
            raise RuntimeError(f"{field} taxonomy code has no terms")
        terms = tuple(canonical_code(value) for value in values if isinstance(value, str))
        if (
            len(terms) != len(values)
            or any(not term for term in terms)
            or len(set(terms)) != len(terms)
        ):
            raise RuntimeError(f"{field} taxonomy code has invalid terms")
        parsed[code] = terms
    if not parsed:
        raise RuntimeError(f"{field} taxonomy is empty")
    return parsed


def _reverse(mapping: Mapping[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    reverse: dict[str, list[str]] = {}
    for code, terms in mapping.items():
        for term in terms:
            reverse.setdefault(term, []).append(code)
    return {term: tuple(sorted(codes)) for term, codes in reverse.items()}


def _resolve_codes(
    codes: tuple[str, ...],
    mapping: Mapping[str, tuple[str, ...]],
) -> tuple[str, ...] | None:
    if not codes:
        return ()
    missing = [code for code in codes if code not in mapping]
    if missing:
        return None
    return tuple(sorted({term for code in codes for term in mapping[code]}))
