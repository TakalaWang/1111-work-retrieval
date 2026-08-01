from __future__ import annotations

import html
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

import boto3  # type: ignore[import-untyped]
import numpy as np
import numpy.typing as npt
import tantivy

from work_retrieval_core.engine import CandidateEvidence, CandidateRequest
from work_retrieval_core.manifest import (
    MODEL,
    MODEL_REVISION,
    WHOLE_DIMENSION,
    RuntimeManifest,
)
from work_retrieval_core.serialization import (
    DOCUMENT_POLICY_VERSION,
    FULL_JOB_FIELDS,
    canonical_code,
    document_template_sha256,
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
UPDATED_AT_FIELD = "updated_at_epoch_ms"
JOB_INDEX_FIELD = "job_index"
LATIN = re.compile(r"[a-z0-9][a-z0-9+#.\-]*")
HTML_TAG = re.compile(r"<[^>]*>")
URL = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
EXACT_DENSE_CHUNK_ROWS = 24_576


class SageMakerRuntime(Protocol):
    def invoke_endpoint(self, **kwargs: object) -> Mapping[str, object]: ...


@runtime_checkable
class ReadableBody(Protocol):
    def read(self) -> bytes: ...


class EligibleRows(Protocol):
    def eligible_indices(self, request: CandidateRequest) -> npt.NDArray[np.int64]: ...


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
        _exact_keys(raw, expected_keys, "whole embedding component")
        whole = manifest.whole_embedding
        expected = {
            "schema_version": 1,
            "complete": True,
            "model": MODEL,
            "revision": MODEL_REVISION,
            "dimension": WHOLE_DIMENSION,
            "dtype": "float16",
            "normalized": True,
            "rows": whole.rows,
            "dataset_sha256": whole.dataset_sha256,
            "jobs_sha256": whole.jobs_sha256,
            "job_row_order_sha256": whole.job_row_order_sha256,
            "document_policy_version": DOCUMENT_POLICY_VERSION,
            "document_template_sha256": document_template_sha256(),
            "document_fields": [label for label, _ in FULL_JOB_FIELDS],
            "query_prompt": QUERY_PROMPT,
        }
        _require_equal(raw, expected, "whole embedding component")
        job_ids_path = _artifact_path(raw["job_ids_path"], "whole embedding job IDs")
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
                {"vectors_path", "row_start", "row_end", "rows", "dimension"},
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
                raise RuntimeError("whole embedding shards are not contiguous 4096d rows")
            vectors_path = _artifact_path(shard["vectors_path"], f"shard {position} vectors")
            _require_inventory_kind(manifest, vectors_path, "embedding")
            parsed.append(EmbeddingShard(vectors_path, start, end))
            expected_start = end
        if expected_start != whole.rows:
            raise RuntimeError("whole embedding shard rows differ from incumbent")
        return cls(job_ids_path, tuple(parsed))


@dataclass(frozen=True, slots=True)
class TantivyLayout:
    index_directory: str
    taxonomy_path: str

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
            "schema_fields",
            "field_boosts",
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
                "index_directory": "indexes/tantivy-bm25-temporal-v1/index",
                "schema_fields": [
                    "title",
                    "duty",
                    "skills",
                    "industry",
                    "body",
                    "location_filter",
                    "duty_filter",
                    "visibility_filter",
                    "updated_at_epoch_ms",
                    "job_index",
                ],
                "field_boosts": FIELD_BOOSTS,
                "filter_semantics": (
                    "visibility AND (location OR) AND (duty OR), applied before Top-K"
                ),
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
        prefix = directory + "/"
        for file in files:
            file_path = _artifact_path(file, "Tantivy index file")
            if not file_path.startswith(prefix):
                raise RuntimeError("Tantivy index file escapes its declared directory")
            _require_inventory_kind(manifest, file_path, "index")
        _require_inventory_kind(manifest, taxonomy_path, "index")
        return cls(directory, taxonomy_path)


class TantivyBm25Retriever:
    """Fielded full-JD BM25 whose visibility, taxonomy, and time filters precede Top-K."""

    def __init__(
        self,
        index_directory: Path,
        job_ids: tuple[str, ...],
        taxonomy: FilterTaxonomy,
    ) -> None:
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

    def eligible_indices(self, request: CandidateRequest) -> npt.NDArray[np.int64]:
        self._ensure_open()
        query = self._build_query(request, lexical=False)
        count_result = self._searcher.search(query, limit=1, count=True)
        count = cast(int, cast(Any, count_result).count)
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
            tokens = lexical_tokens(request.text)
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
    def from_aws(cls, *, endpoint_name: str, region_name: str) -> SageMakerQueryEncoder:
        runtime = boto3.client("sagemaker-runtime", region_name=region_name)
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
        if vector.shape != (1, WHOLE_DIMENSION) or not np.isfinite(vector).all():
            raise RuntimeError("SageMaker embedding response violates the 4096d contract")
        norm = float(np.linalg.norm(vector[0]))
        if not np.isfinite(norm) or norm == 0:
            raise RuntimeError("SageMaker embedding response has an invalid norm")
        return np.asarray(vector[0] / norm, dtype=np.float32)


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
    ) -> None:
        if chunk_rows < 1:
            raise ValueError("dense chunk_rows must be positive")
        self._job_ids = job_ids
        self._eligible_rows = eligible_rows
        self._encoder = encoder
        self._chunk_rows = chunk_rows
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
        query = self._encoder.encode(request.text)
        eligible = self._eligible_rows.eligible_indices(request)
        if not len(eligible):
            return ()
        best: list[tuple[float, int]] = []
        for layout, vectors in self._shards:
            start = int(np.searchsorted(eligible, layout.row_start, side="left"))
            end = int(np.searchsorted(eligible, layout.row_end, side="left"))
            selected = eligible[start:end]
            for offset in range(0, len(selected), self._chunk_rows):
                rows = selected[offset : offset + self._chunk_rows]
                local_rows = rows - layout.row_start
                matrix = np.asarray(vectors[local_rows], dtype=np.float32)
                scores = matrix @ query
                best.extend(
                    (float(score), int(row)) for score, row in zip(scores, rows, strict=True)
                )
                if len(best) > limit * 8:
                    best = sorted(best, key=lambda item: (-item[0], self._job_ids[item[1]]))[:limit]
        ranked = sorted(best, key=lambda item: (-item[0], self._job_ids[item[1]]))[:limit]
        return tuple(
            CandidateEvidence(self._job_ids[row], score, rank)
            for rank, (score, row) in enumerate(ranked, start=1)
        )

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


def load_job_ids(path: Path, *, expected_rows: int) -> tuple[str, ...]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("whole-Qwen job IDs cannot be read") from error
    if (
        not isinstance(raw, list)
        or len(raw) != expected_rows
        or any(
            not isinstance(value, str) or not value.isascii() or not value.isdecimal()
            for value in raw
        )
        or len(set(raw)) != len(raw)
    ):
        raise RuntimeError("whole-Qwen job IDs violate the serving contract")
    return tuple(raw)


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
