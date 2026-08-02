from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]

MODEL_ID = "Qwen/Qwen3-Reranker-8B"
MAX_DOCUMENTS = 50
MAX_REQUEST_BYTES = 6_291_456
DEFAULT_CONNECT_TIMEOUT_SECONDS = 1
DEFAULT_READ_TIMEOUT_SECONDS = 8


class JobDocumentLookup(Protocol):
    def job_documents_for_job_ids(self, job_ids: tuple[str, ...]) -> Mapping[str, str]: ...


class SageMakerRuntime(Protocol):
    def invoke_endpoint(self, **kwargs: object) -> Mapping[str, object]: ...


@runtime_checkable
class ReadableBody(Protocol):
    def read(self) -> bytes: ...


class SemanticReranker:
    def __init__(
        self,
        endpoint_name: str,
        documents: JobDocumentLookup,
        runtime: SageMakerRuntime,
    ) -> None:
        if not isinstance(endpoint_name, str) or not endpoint_name.strip():
            raise ValueError("SageMaker reranker endpoint must be non-empty")
        self._endpoint_name = endpoint_name
        self._documents = documents
        self._runtime = runtime

    @classmethod
    def from_aws(
        cls,
        *,
        endpoint_name: str,
        region_name: str,
        documents: JobDocumentLookup,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
    ) -> SemanticReranker:
        if (
            isinstance(connect_timeout_seconds, bool)
            or isinstance(read_timeout_seconds, bool)
            or connect_timeout_seconds <= 0
            or read_timeout_seconds <= 0
        ):
            raise ValueError("SageMaker reranker timeouts must be positive")
        config = Config(
            connect_timeout=connect_timeout_seconds,
            read_timeout=read_timeout_seconds,
            retries={"total_max_attempts": 1, "mode": "standard"},
        )
        runtime = boto3.client("sagemaker-runtime", region_name=region_name, config=config)
        return cls(endpoint_name, documents, runtime)

    def score(self, query: str, job_ids: tuple[str, ...]) -> Mapping[str, float]:
        normalized_query = query.strip() if isinstance(query, str) else ""
        if not normalized_query:
            raise ValueError("reranker query must be non-empty")
        if not isinstance(job_ids, tuple) or any(
            not isinstance(job_id, str) or not job_id or job_id != job_id.strip()
            for job_id in job_ids
        ):
            raise ValueError("reranker job IDs must be non-empty canonical strings")
        if len(set(job_ids)) != len(job_ids):
            raise ValueError("reranker job IDs must be unique")
        if len(job_ids) > MAX_DOCUMENTS:
            raise ValueError("reranker accepts at most 50 documents")
        if not job_ids:
            return {}

        values = self._documents.job_documents_for_job_ids(job_ids)
        if not isinstance(values, Mapping):
            raise RuntimeError("full-JD lookup returned a malformed mapping")
        missing = set(job_ids).difference(values)
        if missing:
            raise RuntimeError("full-JD lookup is missing requested jobs")
        unexpected = set(values).difference(job_ids)
        if unexpected:
            raise RuntimeError("full-JD lookup returned unexpected jobs")
        documents = [values[job_id] for job_id in job_ids]
        if any(not isinstance(document, str) or not document.strip() for document in documents):
            raise RuntimeError("full-JD documents must be non-empty strings")

        body = json.dumps(
            {"model": MODEL_ID, "query": normalized_query, "documents": documents},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        if len(body) > MAX_REQUEST_BYTES:
            raise ValueError("SageMaker reranker request body exceeds 6 MiB")
        response = self._runtime.invoke_endpoint(
            EndpointName=self._endpoint_name,
            Body=body,
            ContentType="application/json",
            Accept="application/json",
            CustomAttributes="route=/v1/rerank",
        )
        scores = _response_scores(response, len(job_ids))
        return {job_id: scores[index] for index, job_id in enumerate(job_ids)}

    def close(self) -> None:
        pass


def _response_scores(response: Mapping[str, object], expected_count: int) -> tuple[float, ...]:
    body = response.get("Body")
    if not isinstance(body, ReadableBody):
        raise RuntimeError("SageMaker reranker response body is malformed")
    try:
        payload = json.loads(body.read())
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as error:
        raise RuntimeError("SageMaker reranker response is invalid JSON") from error
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or len(results) != expected_count:
        raise RuntimeError("SageMaker reranker must return one result per document")

    scores: dict[int, float] = {}
    for result in results:
        index = result.get("index") if isinstance(result, dict) else None
        score = result.get("relevance_score") if isinstance(result, dict) else None
        if not isinstance(index, int) or isinstance(index, bool):
            raise RuntimeError("SageMaker reranker returned invalid indices")
        if (
            not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not math.isfinite(score)
            or not 0 <= score <= 1
        ):
            raise RuntimeError("SageMaker reranker returned an invalid relevance score")
        if index in scores or not 0 <= index < expected_count:
            raise RuntimeError("SageMaker reranker returned invalid indices")
        scores[index] = float(score)
    if set(scores) != set(range(expected_count)):
        raise RuntimeError("SageMaker reranker returned invalid indices")
    return tuple(scores[index] for index in range(expected_count))
