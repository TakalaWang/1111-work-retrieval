from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import deploy_sagemaker_reranker as deployer


def test_deployment_is_pinned_to_competition_us_west_2() -> None:
    assert deployer.AWS_PROFILE == "competition"
    assert deployer.AWS_ACCOUNT == "378849533305"
    assert deployer.AWS_REGION == "us-west-2"
    assert deployer.INSTANCE_TYPE == "ml.g5.2xlarge"
    assert deployer.EXECUTION_ROLE_NAME == "SageMakerQwen3RerankerRole"
    assert deployer.MODEL_ID == "Qwen/Qwen3-Reranker-8B"
    assert len(deployer.MODEL_REVISION) == 40


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


def test_quota_and_account_validation_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deployer, "aws", lambda _: {"Account": "wrong"})
    with pytest.raises(RuntimeError, match=deployer.AWS_ACCOUNT):
        deployer.verify_target()

    responses = iter([{"Account": deployer.AWS_ACCOUNT}, {"Quota": {"Value": 1.0}}])
    monkeypatch.setattr(deployer, "aws", lambda _: next(responses))
    with pytest.raises(RuntimeError, match="at least 2"):
        deployer.verify_target()
