from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from work_retrieval_core.reranker import CHAT_TEMPLATE, JOB_SEARCH_INSTRUCTION

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import deploy_sagemaker_reranker as deployer


def test_deployment_is_pinned_to_competition_us_west_2() -> None:
    assert deployer.AWS_PROFILE == "competition"
    assert deployer.AWS_ACCOUNT == "378849533305"
    assert deployer.AWS_REGION == "us-west-2"
    assert deployer.INSTANCE_TYPE == "ml.g6.16xlarge"
    assert deployer.EXECUTION_ROLE_NAME == "SageMakerQwen3RerankerRole"
    assert deployer.MODEL_ID == "Qwen/Qwen3-Reranker-8B"
    assert len(deployer.MODEL_REVISION) == 40
    assert deployer.ENDPOINT_NAME.endswith("-v8-business")
    assert deployer.MODEL_NAME.endswith("-v8-business")
    assert deployer.ENDPOINT_CONFIG_NAME.endswith("-v8-business-g6-16xl")
    assert deployer.IMAGE_URI.endswith("@" + deployer.IMAGE_DIGEST)


def test_only_actual_missing_resource_errors_are_treated_as_not_found() -> None:
    assert deployer._not_found(deployer.AwsError('Could not find endpoint "candidate"'))
    assert deployer._not_found(deployer.AwsError("NoSuchEntity: role"))
    assert not deployer._not_found(deployer.AwsError("ValidationException: invalid config"))


def test_vllm_environment_uses_the_verified_reranker_contract() -> None:
    environment = deployer.model_environment()

    assert environment["SM_VLLM_RUNNER"] == "pooling"
    overrides = environment["SM_VLLM_HF_OVERRIDES"]
    assert overrides.startswith("'") and overrides.endswith("'")
    assert json.loads(overrides[1:-1])["architectures"] == ["Qwen3ForSequenceClassification"]
    chat_template = environment["SM_VLLM_CHAT_TEMPLATE"]
    assert chat_template.startswith("'") and chat_template.endswith("'")
    assert '{{ "\\n" }}' in chat_template
    assert "Document" in environment["SM_VLLM_CHAT_TEMPLATE"]
    assert len(environment["SM_VLLM_CHAT_TEMPLATE"]) <= 1_024
    assert "{{ instruction" not in CHAT_TEMPLATE
    assert JOB_SEARCH_INSTRUCTION in CHAT_TEMPLATE
    assert "'" not in JOB_SEARCH_INSTRUCTION
    assert 'selectattr("role", "eq", "system")' not in CHAT_TEMPLATE
    assert "Shared skills never justify a different occupation" in JOB_SEARCH_INSTRUCTION
    assert "predicted appeal is not measured popularity" in JOB_SEARCH_INSTRUCTION
    assert "never outweighs relevance" in JOB_SEARCH_INSTRUCTION
    assert environment["WORK_RETRIEVAL_CHAT_TEMPLATE_SHA256"] == deployer.CHAT_TEMPLATE_SHA256
    assert environment["WORK_RETRIEVAL_RERANK_REQUEST_CONTRACT"] == "query_documents_only_v1"


def test_request_contract_does_not_accept_instruction() -> None:
    request = deployer.build_rerank_request("Python engineer", ["A document"])

    assert request == {
        "model": deployer.MODEL_ID,
        "query": "Python engineer",
        "documents": ["A document"],
    }

    with pytest.raises(TypeError):
        deployer.build_rerank_request(  # type: ignore[call-arg]
            "Python engineer", ["A document"], instruction="caller-controlled"
        )


def test_smoke_uses_only_the_pinned_template_instruction(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def invoke(payload: dict[str, object]) -> dict[str, object]:
        captured.update(payload)
        return {
            "results": [
                {"index": 0, "relevance_score": 0.9},
                {"index": 1, "relevance_score": 0.2},
                {"index": 2, "relevance_score": 0.1},
            ],
            "usage": {
                "prompt_tokens": 500,
                "total_tokens": 500,
            },
        }

    monkeypatch.setattr(deployer, "_invoke_raw_reranker", invoke)

    evidence = deployer.smoke_test()

    assert set(captured) == {"model", "query", "documents"}
    assert "instruction" not in captured
    assert evidence["prompt_tokens"] == 500


def test_black_box_regression_proves_request_instruction_has_no_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def invoke(payload: dict[str, object]) -> dict[str, object]:
        calls.append(payload)
        jitter = 0.0004 if len(calls) == 2 else 0.0
        return {
            "results": [
                {"index": 0, "relevance_score": 0.9 + jitter},
                {"index": 1, "relevance_score": 0.2 - jitter},
            ],
            "usage": {"prompt_tokens": 211, "total_tokens": 211},
        }

    monkeypatch.setattr(deployer, "_invoke_raw_reranker", invoke)

    evidence = deployer.verify_request_instruction_invariance()

    assert len(calls) == 2
    assert "instruction" not in calls[0]
    assert len(str(calls[1]["instruction"])) == deployer.INSTRUCTION_ATTACK_LENGTH
    assert evidence["request_instruction_effect"] == "none_not_part_of_contract"
    assert evidence["prompt_tokens"] == 211
    assert evidence["max_score_delta"] == pytest.approx(0.0004)
    assert evidence["score_jitter_tolerance"] == 0.001


def test_black_box_regression_fails_when_instruction_changes_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def invoke(_payload: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {
            "results": [
                {"index": 0, "relevance_score": 0.9},
                {"index": 1, "relevance_score": 0.2},
            ],
            "usage": {"prompt_tokens": 210 + calls, "total_tokens": 210 + calls},
        }

    monkeypatch.setattr(deployer, "_invoke_raw_reranker", invoke)

    with pytest.raises(RuntimeError, match="unexpectedly changed"):
        deployer.verify_request_instruction_invariance()


def test_existing_endpoint_with_other_config_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def aws(arguments: list[str]) -> dict[str, object]:
        calls.append(arguments)
        if arguments[1] == "describe-endpoint":
            return {"EndpointStatus": "InService", "EndpointConfigName": "old-config"}
        return {}

    monkeypatch.setattr(deployer, "aws", aws)
    monkeypatch.setattr(deployer.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="different config"):
        deployer.ensure_endpoint()
    assert all(call[1] != "update-endpoint" for call in calls)


def test_deployed_lineage_verifies_template_hash_and_image_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [
            {
                "EndpointStatus": "InService",
                "EndpointConfigName": deployer.ENDPOINT_CONFIG_NAME,
                "EndpointArn": (
                    "arn:aws:sagemaker:us-west-2:378849533305:endpoint/" + deployer.ENDPOINT_NAME
                ),
            },
            {
                "ProductionVariants": [
                    {"VariantName": "AllTraffic", "ModelName": deployer.MODEL_NAME}
                ]
            },
            {
                "ExecutionRoleArn": deployer.EXECUTION_ROLE_ARN,
                "PrimaryContainer": {
                    "Image": deployer.IMAGE_URI,
                    "Environment": deployer.model_environment(),
                },
            },
        ]
    )
    monkeypatch.setattr(deployer, "aws", lambda _: next(responses))

    evidence = deployer.verify_deployed_lineage()

    assert evidence["chat_template_sha256"] == deployer.CHAT_TEMPLATE_SHA256
    assert evidence["image_digest"] == deployer.IMAGE_DIGEST


def test_quota_and_account_validation_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deployer, "aws", lambda _: {"Account": "wrong"})
    with pytest.raises(RuntimeError, match=deployer.AWS_ACCOUNT):
        deployer.verify_target()

    responses = iter([{"Account": deployer.AWS_ACCOUNT}, {"Quota": {"Value": 0.0}}])
    monkeypatch.setattr(deployer, "aws", lambda _: next(responses))
    with pytest.raises(RuntimeError, match="at least 1"):
        deployer.verify_target()
