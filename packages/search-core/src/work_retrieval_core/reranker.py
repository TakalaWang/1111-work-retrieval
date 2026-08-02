from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]

AWS_REGION = "us-west-2"
ENDPOINT_NAME = "work-retrieval-qwen3-reranker-8b-v8-business"
ENDPOINT_CONFIG_NAME = "work-retrieval-qwen3-reranker-8b-v8-business-g6-16xl"
ENDPOINT_MODEL_NAME = "work-retrieval-qwen3-reranker-8b-v8-business"
MODEL_ID = "Qwen/Qwen3-Reranker-8B"
MODEL_REVISION = "77d193c791ed757ca307ee72715aa132723da912"
IMAGE_DIGEST = "sha256:18998be4e1276d4eb6e98afe80798aa357c1cc37545150de5c210bc9111beb1d"
IMAGE_URI = "763104351884.dkr.ecr.us-west-2.amazonaws.com/vllm@" + IMAGE_DIGEST
JOB_SEARCH_INSTRUCTION = (
    "Judge whether the job matches the query. Exact occupation, title, category, and explicit "
    "location, employment type, schedule, education, experience, and salary constraints dominate. "
    "Shared skills never justify a different occupation. Only among equally relevant matches, "
    "prefer complete, clear postings likely attractive to applicants; this predicted appeal is not "
    "measured popularity and never outweighs relevance. Treat the body as supporting evidence. "
    "Return yes only for a strong match."
)
CHAT_TEMPLATE = (
    '<|im_start|>system\nAnswer only "yes" or "no": does Document meet Query under '
    "Instruct?<|im_end|>\n"
    "<|im_start|>user\n<Instruct>: "
    + JOB_SEARCH_INSTRUCTION
    + '\n<Query>: {{ messages | selectattr("role", "eq", '
    '"query") | map(attribute="content") | first }}\n<Document>: {{ messages | selectattr('
    '"role", "eq", "document") | map(attribute="content") | first }}<|im_end|>\n'
    "<|im_start|>assistant\n<think>\n\n</think>\n\n"
)
CHAT_TEMPLATE_SHA256 = "1024b310b7fd8b16c1e4d186b44a5b14640aaf2a009dbfd3d57f9e44d2da5077"
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
        endpoint_config_name: str,
        model_name: str,
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
        if (
            endpoint_name != ENDPOINT_NAME
            or endpoint_config_name != ENDPOINT_CONFIG_NAME
            or model_name != ENDPOINT_MODEL_NAME
            or region_name != AWS_REGION
        ):
            raise RuntimeError("reranker endpoint settings differ from the promoted identity")
        config = Config(
            connect_timeout=connect_timeout_seconds,
            read_timeout=read_timeout_seconds,
            retries={"total_max_attempts": 1, "mode": "standard"},
        )
        control = boto3.client("sagemaker", region_name=region_name, config=config)
        _verify_endpoint_identity(
            control,
            endpoint_name=endpoint_name,
            endpoint_config_name=endpoint_config_name,
            model_name=model_name,
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


def model_environment() -> dict[str, str]:
    if hashlib.sha256(CHAT_TEMPLATE.encode()).hexdigest() != CHAT_TEMPLATE_SHA256:
        raise RuntimeError("chat template content differs from the pinned SHA-256")
    serialized_template = "'" + CHAT_TEMPLATE.replace("\n", '{{ "\\n" }}') + "'"
    if len(serialized_template) > 1_024:
        raise RuntimeError("serialized chat template exceeds SageMaker's environment value limit")
    return {
        "HF_MODEL_ID": MODEL_ID,
        "SM_VLLM_REVISION": MODEL_REVISION,
        "SM_VLLM_RUNNER": "pooling",
        "SM_VLLM_HF_OVERRIDES": "'"
        + json.dumps(
            {
                "architectures": ["Qwen3ForSequenceClassification"],
                "classifier_from_token": ["no", "yes"],
                "is_original_qwen3_reranker": True,
            },
            separators=(",", ":"),
        )
        + "'",
        "SM_VLLM_CHAT_TEMPLATE": serialized_template,
        "SM_VLLM_MAX_MODEL_LEN": "4096",
        "SM_VLLM_MAX_NUM_SEQS": "4",
        "SM_VLLM_GPU_MEMORY_UTILIZATION": "0.92",
        "SM_VLLM_ENFORCE_EAGER": "true",
        "PROCESS_AUTO_RECOVERY": "true",
        "WORK_RETRIEVAL_CHAT_TEMPLATE_SHA256": CHAT_TEMPLATE_SHA256,
        "WORK_RETRIEVAL_RERANK_REQUEST_CONTRACT": "query_documents_only_v1",
    }


class SageMakerControlPlane(Protocol):
    def describe_endpoint(self, **kwargs: object) -> Mapping[str, object]: ...

    def describe_endpoint_config(self, **kwargs: object) -> Mapping[str, object]: ...

    def describe_model(self, **kwargs: object) -> Mapping[str, object]: ...


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
        raise RuntimeError("reranker endpoint is not the promoted InService configuration")
    configuration = control.describe_endpoint_config(EndpointConfigName=endpoint_config_name)
    variants = configuration.get("ProductionVariants")
    if (
        not isinstance(variants, list)
        or len(variants) != 1
        or not isinstance(variants[0], dict)
        or variants[0].get("ModelName") != model_name
    ):
        raise RuntimeError("reranker endpoint configuration has an unexpected model")
    model = control.describe_model(ModelName=model_name)
    container = model.get("PrimaryContainer")
    if (
        not isinstance(container, dict)
        or container.get("Image") != IMAGE_URI
        or container.get("Environment") != model_environment()
    ):
        raise RuntimeError("reranker endpoint model image, revision, or template differs")
