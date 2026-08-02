#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import cast

AWS_ACCOUNT = "378849533305"
AWS_PROFILE = "competition"
AWS_REGION = "us-west-2"
ENDPOINT_NAME = "work-retrieval-qwen3-reranker-8b-v7"
MODEL_NAME = "work-retrieval-qwen3-reranker-8b-v7"
ENDPOINT_CONFIG_NAME = "work-retrieval-qwen3-reranker-8b-v7-g5-4xl"
INSTANCE_TYPE = "ml.g5.4xlarge"
ENDPOINT_QUOTA_CODE = "L-C1B9A48D"
REQUIRED_ENDPOINT_QUOTA = 1
EXECUTION_ROLE_NAME = "SageMakerQwen3RerankerRole"
EXECUTION_ROLE_ARN = f"arn:aws:iam::{AWS_ACCOUNT}:role/{EXECUTION_ROLE_NAME}"
IMAGE_REPOSITORY = "763104351884.dkr.ecr.us-west-2.amazonaws.com/vllm"
IMAGE_TAG = "0.20.2-gpu-py312-cu130-ubuntu22.04-sagemaker"
IMAGE_DIGEST = "sha256:18998be4e1276d4eb6e98afe80798aa357c1cc37545150de5c210bc9111beb1d"
IMAGE_URI = f"{IMAGE_REPOSITORY}@{IMAGE_DIGEST}"
MODEL_ID = "Qwen/Qwen3-Reranker-8B"
MODEL_REVISION = "77d193c791ed757ca307ee72715aa132723da912"
JOB_SEARCH_INSTRUCTION = (
    "Judge whether a job posting satisfies the job search query. Prioritize the exact job title "
    "or occupation, job category, and explicit constraints such as location, employment type, "
    "schedule, education, experience, and salary. A related but different occupation must not be "
    "treated as relevant based only on shared skills. Use the job description body only as "
    "supporting evidence. Return yes only when the posting is an appropriate match."
)

CHAT_TEMPLATE = (
    "<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and "
    'the Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n'
    "<|im_start|>user\n<Instruct>: "
    + JOB_SEARCH_INSTRUCTION
    + '\n<Query>: {{ messages | selectattr("role", "eq", '
    '"query") | map(attribute="content") | first }}\n<Document>: {{ messages | selectattr('
    '"role", "eq", "document") | map(attribute="content") | first }}<|im_end|>\n'
    "<|im_start|>assistant\n<think>\n\n</think>\n\n"
)
CHAT_TEMPLATE_SHA256 = "c917bf98a8ccffff1823e26060fa2c6f048b9a99b39f02771cfd4321f2cf7714"
EXPECTED_SMOKE_PROMPT_TOKENS = 454
INSTRUCTION_ATTACK_LENGTH = 4_600
SCORE_JITTER_TOLERANCE = 1e-3


class AwsError(RuntimeError):
    pass


def aws(arguments: list[str]) -> dict[str, object]:
    command = [
        "aws",
        *arguments,
        "--profile",
        AWS_PROFILE,
        "--region",
        AWS_REGION,
        "--output",
        "json",
        "--no-cli-pager",
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode:
        detail = " ".join(result.stderr.split())[:2_000]
        raise AwsError(f"AWS CLI command failed: {' '.join(command[:3])}: {detail}")
    if not result.stdout.strip():
        return {}
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("AWS CLI returned an unexpected JSON shape")
    return cast(dict[str, object], value)


def verify_target() -> None:
    if aws(["sts", "get-caller-identity"]).get("Account") != AWS_ACCOUNT:
        raise RuntimeError(f"AWS caller must be account {AWS_ACCOUNT}")
    quota = aws(
        [
            "service-quotas",
            "get-service-quota",
            "--service-code",
            "sagemaker",
            "--quota-code",
            ENDPOINT_QUOTA_CODE,
        ]
    ).get("Quota")
    value = quota.get("Value") if isinstance(quota, dict) else None
    if not isinstance(value, (int, float)) or value < REQUIRED_ENDPOINT_QUOTA:
        raise RuntimeError(
            f"{INSTANCE_TYPE} endpoint quota must be at least {REQUIRED_ENDPOINT_QUOTA}, "
            f"got {value!r}"
        )
    images = aws(
        [
            "ecr",
            "batch-get-image",
            "--registry-id",
            "763104351884",
            "--repository-name",
            "vllm",
            "--image-ids",
            f"imageTag={IMAGE_TAG}",
        ]
    ).get("images")
    if (
        not isinstance(images, list)
        or len(images) != 1
        or not isinstance(images[0], dict)
        or not isinstance(images[0].get("imageId"), dict)
        or images[0]["imageId"].get("imageDigest") != IMAGE_DIGEST
    ):
        raise RuntimeError("the official vLLM image tag no longer resolves to the pinned digest")


def model_environment() -> dict[str, str]:
    actual_template_hash = hashlib.sha256(CHAT_TEMPLATE.encode()).hexdigest()
    if actual_template_hash != CHAT_TEMPLATE_SHA256:
        raise RuntimeError("chat template content differs from the pinned SHA-256")
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
        "SM_VLLM_CHAT_TEMPLATE": "'" + CHAT_TEMPLATE.replace("\n", '{{ "\\n" }}') + "'",
        "SM_VLLM_MAX_MODEL_LEN": "4096",
        "SM_VLLM_MAX_NUM_SEQS": "4",
        "SM_VLLM_GPU_MEMORY_UTILIZATION": "0.92",
        "SM_VLLM_ENFORCE_EAGER": "true",
        "PROCESS_AUTO_RECOVERY": "true",
        "WORK_RETRIEVAL_CHAT_TEMPLATE_SHA256": CHAT_TEMPLATE_SHA256,
        "WORK_RETRIEVAL_RERANK_REQUEST_CONTRACT": "query_documents_only_v1",
    }


def ensure_execution_role() -> None:
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "sagemaker.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    try:
        aws(["iam", "get-role", "--role-name", EXECUTION_ROLE_NAME])
    except AwsError as error:
        if not _not_found(error):
            raise
        aws(
            [
                "iam",
                "create-role",
                "--role-name",
                EXECUTION_ROLE_NAME,
                "--assume-role-policy-document",
                json.dumps(trust),
            ]
        )
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "Logs",
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": f"arn:aws:logs:{AWS_REGION}:{AWS_ACCOUNT}:*",
            },
            {
                "Sid": "OfficialVllmImage",
                "Effect": "Allow",
                "Action": [
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchGetImage",
                ],
                "Resource": f"arn:aws:ecr:{AWS_REGION}:763104351884:repository/vllm",
            },
            {
                "Sid": "EcrAuthorization",
                "Effect": "Allow",
                "Action": "ecr:GetAuthorizationToken",
                "Resource": "*",
            },
        ],
    }
    aws(
        [
            "iam",
            "put-role-policy",
            "--role-name",
            EXECUTION_ROLE_NAME,
            "--policy-name",
            "Qwen3RerankerRuntime",
            "--policy-document",
            json.dumps(policy),
        ]
    )


def _not_found(error: AwsError) -> bool:
    return any(marker in str(error) for marker in ("Could not find", "NoSuchEntity"))


def ensure_model() -> None:
    try:
        existing = aws(["sagemaker", "describe-model", "--model-name", MODEL_NAME])
    except AwsError as error:
        if not _not_found(error):
            raise
    else:
        container = existing.get("PrimaryContainer")
        if (
            existing.get("ExecutionRoleArn") != EXECUTION_ROLE_ARN
            or not isinstance(container, dict)
            or container.get("Image") != IMAGE_URI
            or container.get("Environment") != model_environment()
        ):
            raise RuntimeError(f"existing SageMaker model {MODEL_NAME} differs")
        return
    aws(
        [
            "sagemaker",
            "create-model",
            "--model-name",
            MODEL_NAME,
            "--execution-role-arn",
            EXECUTION_ROLE_ARN,
            "--primary-container",
            json.dumps({"Image": IMAGE_URI, "Environment": model_environment()}),
        ]
    )


def ensure_endpoint_config() -> None:
    expected = {
        "VariantName": "AllTraffic",
        "ModelName": MODEL_NAME,
        "InitialInstanceCount": 1,
        "InstanceType": INSTANCE_TYPE,
        "ModelDataDownloadTimeoutInSeconds": 1800,
        "ContainerStartupHealthCheckTimeoutInSeconds": 1800,
        "InferenceAmiVersion": "al2-ami-sagemaker-inference-gpu-3-1",
    }
    try:
        existing = aws(
            [
                "sagemaker",
                "describe-endpoint-config",
                "--endpoint-config-name",
                ENDPOINT_CONFIG_NAME,
            ]
        )
    except AwsError as error:
        if not _not_found(error):
            raise
    else:
        variants = existing.get("ProductionVariants")
        if not isinstance(variants, list) or len(variants) != 1:
            raise RuntimeError(f"existing endpoint config {ENDPOINT_CONFIG_NAME} differs")
        for key, value in expected.items():
            if variants[0].get(key) != value:
                raise RuntimeError(f"existing endpoint config {ENDPOINT_CONFIG_NAME} differs")
        return
    aws(
        [
            "sagemaker",
            "create-endpoint-config",
            "--endpoint-config-name",
            ENDPOINT_CONFIG_NAME,
            "--production-variants",
            json.dumps([expected]),
        ]
    )


def ensure_endpoint() -> None:
    try:
        endpoint = aws(["sagemaker", "describe-endpoint", "--endpoint-name", ENDPOINT_NAME])
    except AwsError as error:
        if not _not_found(error):
            raise
        aws(
            [
                "sagemaker",
                "create-endpoint",
                "--endpoint-name",
                ENDPOINT_NAME,
                "--endpoint-config-name",
                ENDPOINT_CONFIG_NAME,
            ]
        )
    else:
        status = endpoint.get("EndpointStatus")
        if status == "Failed":
            aws(["sagemaker", "delete-endpoint", "--endpoint-name", ENDPOINT_NAME])
            for _ in range(30):
                try:
                    aws(["sagemaker", "describe-endpoint", "--endpoint-name", ENDPOINT_NAME])
                except AwsError as error:
                    if _not_found(error):
                        break
                    raise
                time.sleep(2)
            else:
                raise RuntimeError("failed endpoint was not deleted within one minute")
            aws(
                [
                    "sagemaker",
                    "create-endpoint",
                    "--endpoint-name",
                    ENDPOINT_NAME,
                    "--endpoint-config-name",
                    ENDPOINT_CONFIG_NAME,
                ]
            )
        elif endpoint.get("EndpointConfigName") != ENDPOINT_CONFIG_NAME:
            raise RuntimeError(f"existing endpoint {ENDPOINT_NAME} uses a different config")

    for _ in range(60):
        endpoint = aws(["sagemaker", "describe-endpoint", "--endpoint-name", ENDPOINT_NAME])
        status = endpoint.get("EndpointStatus")
        if status == "InService":
            if endpoint.get("EndpointConfigName") != ENDPOINT_CONFIG_NAME:
                raise RuntimeError("reranker endpoint rolled back to a different config")
            return
        if status == "Failed":
            raise RuntimeError(f"reranker endpoint failed: {endpoint.get('FailureReason')}")
        time.sleep(30)
    raise RuntimeError("reranker endpoint did not become InService within 30 minutes")


def _invoke_raw_reranker(payload: dict[str, object]) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        request = Path(directory) / "request.json"
        response = Path(directory) / "response.json"
        request.write_text(json.dumps(payload), encoding="utf-8")
        aws(
            [
                "sagemaker-runtime",
                "invoke-endpoint",
                "--endpoint-name",
                ENDPOINT_NAME,
                "--content-type",
                "application/json",
                "--custom-attributes",
                "route=/v1/rerank",
                "--body",
                f"fileb://{request}",
                str(response),
            ]
        )
        value = json.loads(response.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("reranker returned an invalid response")
    return cast(dict[str, object], value)


def build_rerank_request(query: str, documents: list[str]) -> dict[str, object]:
    if not query.strip() or not documents or any(not document.strip() for document in documents):
        raise ValueError("rerank query and every document must be non-empty")
    return {"model": MODEL_ID, "query": query, "documents": documents}


def _prompt_tokens(response: dict[str, object]) -> int:
    usage = response.get("usage")
    prompt_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
    if not isinstance(prompt_tokens, int) or prompt_tokens <= 0:
        raise RuntimeError("reranker response did not report a positive prompt token count")
    return prompt_tokens


def _ranked_scores(response: dict[str, object], expected_count: int) -> list[tuple[int, float]]:
    results = response.get("results")
    if not isinstance(results, list) or len(results) != expected_count:
        raise RuntimeError("reranker did not return one result per document")
    ranked: list[tuple[int, float]] = []
    for item in results:
        if not isinstance(item, dict):
            raise RuntimeError("reranker returned an invalid result")
        index = item.get("index")
        score = item.get("relevance_score")
        if not isinstance(index, int) or not isinstance(score, (int, float)):
            raise RuntimeError("reranker returned invalid relevance scores")
        ranked.append((index, float(score)))
    return ranked


def verify_deployed_lineage() -> dict[str, object]:
    endpoint = aws(["sagemaker", "describe-endpoint", "--endpoint-name", ENDPOINT_NAME])
    endpoint_arn = endpoint.get("EndpointArn")
    if (
        endpoint.get("EndpointStatus") != "InService"
        or endpoint.get("EndpointConfigName") != ENDPOINT_CONFIG_NAME
        or not isinstance(endpoint_arn, str)
    ):
        raise RuntimeError("reranker endpoint lineage differs from the pinned v7 deployment")
    configuration = aws(
        [
            "sagemaker",
            "describe-endpoint-config",
            "--endpoint-config-name",
            ENDPOINT_CONFIG_NAME,
        ]
    )
    variants = configuration.get("ProductionVariants")
    if (
        not isinstance(variants, list)
        or len(variants) != 1
        or not isinstance(variants[0], dict)
        or variants[0].get("ModelName") != MODEL_NAME
    ):
        raise RuntimeError("reranker endpoint config does not reference the pinned v7 model")
    model = aws(["sagemaker", "describe-model", "--model-name", MODEL_NAME])
    container = model.get("PrimaryContainer")
    if (
        model.get("ExecutionRoleArn") != EXECUTION_ROLE_ARN
        or not isinstance(container, dict)
        or container.get("Image") != IMAGE_URI
        or container.get("Environment") != model_environment()
    ):
        raise RuntimeError("reranker model image, revision, or template lineage differs")
    return {
        "endpoint": ENDPOINT_NAME,
        "endpoint_arn": endpoint_arn,
        "endpoint_config": ENDPOINT_CONFIG_NAME,
        "model": MODEL_NAME,
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "image_digest": IMAGE_DIGEST,
        "chat_template_sha256": CHAT_TEMPLATE_SHA256,
    }


def verify_request_instruction_invariance() -> dict[str, object]:
    query = "Python backend engineer"
    documents = [
        "Backend engineer building Python and PostgreSQL services.",
        "Accountant responsible for tax reporting.",
    ]
    baseline_request = build_rerank_request(query, documents)
    attack_request = {**baseline_request, "instruction": "X" * INSTRUCTION_ATTACK_LENGTH}
    baseline = _invoke_raw_reranker(baseline_request)
    attacked = _invoke_raw_reranker(attack_request)
    baseline_scores = _ranked_scores(baseline, len(documents))
    attacked_scores = _ranked_scores(attacked, len(documents))
    baseline_tokens = _prompt_tokens(baseline)
    attacked_tokens = _prompt_tokens(attacked)
    same_indices = [index for index, _score in attacked_scores] == [
        index for index, _score in baseline_scores
    ]
    score_deltas = [
        abs(attacked_score - baseline_score)
        for (_baseline_index, baseline_score), (_attacked_index, attacked_score) in zip(
            baseline_scores, attacked_scores, strict=True
        )
    ]
    if (
        not same_indices
        or max(score_deltas, default=0.0) > SCORE_JITTER_TOLERANCE
        or attacked_tokens != baseline_tokens
    ):
        raise RuntimeError("request instruction unexpectedly changed reranker inference")
    return {
        "request_contract": "query_documents_only_v1",
        "request_instruction_effect": "none_not_part_of_contract",
        "attack_instruction_characters": INSTRUCTION_ATTACK_LENGTH,
        "prompt_tokens": baseline_tokens,
        "scores": baseline_scores,
        "max_score_delta": max(score_deltas, default=0.0),
        "score_jitter_tolerance": SCORE_JITTER_TOLERANCE,
    }


def smoke_test() -> dict[str, object]:
    documents = [
        "Backend engineer building Python, FastAPI, and PostgreSQL services.",
        "Accountant responsible for monthly closing and tax reporting.",
        "Warehouse operator responsible for picking and packing orders.",
    ]
    response = _invoke_raw_reranker(build_rerank_request("Python backend engineer", documents))
    ranked = _ranked_scores(response, len(documents))
    if ranked[0][0] != 0:
        raise RuntimeError("reranker failed the relevance smoke test")
    prompt_tokens = _prompt_tokens(response)
    if prompt_tokens != EXPECTED_SMOKE_PROMPT_TOKENS:
        raise RuntimeError(
            f"reranker prompt token count changed: {prompt_tokens} != "
            f"{EXPECTED_SMOKE_PROMPT_TOKENS}"
        )
    return {
        "endpoint": ENDPOINT_NAME,
        "instance_type": INSTANCE_TYPE,
        "top_index": ranked[0][0],
        "scores": [score for _, score in ranked],
        "prompt_tokens": prompt_tokens,
        "chat_template_sha256": CHAT_TEMPLATE_SHA256,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy and verify the pinned Qwen3 reranker")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    verify_target()
    if not args.execute:
        print(
            json.dumps(
                {"execute": False, "endpoint": ENDPOINT_NAME, "environment": model_environment()}
            )
        )
        return
    ensure_execution_role()
    ensure_model()
    ensure_endpoint_config()
    ensure_endpoint()
    print(
        json.dumps(
            {
                "lineage": verify_deployed_lineage(),
                "smoke": smoke_test(),
                "request_instruction_invariance": verify_request_instruction_invariance(),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
