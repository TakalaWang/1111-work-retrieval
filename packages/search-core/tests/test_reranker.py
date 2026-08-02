from __future__ import annotations

import json
from io import BytesIO

import pytest
from work_retrieval_core import reranker as reranker_module


class FakeDocuments:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.calls: list[tuple[str, ...]] = []

    def job_documents_for_job_ids(self, job_ids: tuple[str, ...]) -> dict[str, str]:
        self.calls.append(job_ids)
        return {job_id: self.values[job_id] for job_id in job_ids if job_id in self.values}


class FakeRuntime:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def invoke_endpoint(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        if isinstance(self.response, bytes):
            return {"Body": BytesIO(self.response)}
        return {"Body": BytesIO(json.dumps(self.response).encode())}


def test_reranker_sends_only_the_v7_contract_and_uses_full_jd_lookup() -> None:
    documents = FakeDocuments(
        {
            "b": "職務名稱: Backend Engineer\n職務內容: Build Python APIs",
            "a": "職務名稱: Accountant\n職務內容: Prepare tax reports",
            "c": "職務名稱: Platform Engineer\n職務內容: Run Kubernetes",
        }
    )
    runtime = FakeRuntime(
        {
            "results": [
                {"index": 1, "relevance_score": 0.2},
                {"index": 2, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.9},
            ]
        }
    )
    reranker = reranker_module.SemanticReranker("reranker-v7", documents, runtime)

    result = reranker.score("  Python backend engineer  ", ("b", "a", "c"))

    assert result == {"b": 0.9, "a": 0.2, "c": 0.9}
    assert documents.calls == [("b", "a", "c")]
    assert len(runtime.calls) == 1
    call = runtime.calls[0]
    assert call["EndpointName"] == "reranker-v7"
    assert call["ContentType"] == call["Accept"] == "application/json"
    assert call["CustomAttributes"] == "route=/v1/rerank"
    assert json.loads(call["Body"]) == {
        "model": "Qwen/Qwen3-Reranker-8B",
        "query": "Python backend engineer",
        "documents": [documents.values[job_id] for job_id in ("b", "a", "c")],
    }
    reranker.close()


@pytest.mark.parametrize(
    ("query", "job_ids", "message"),
    [
        (" ", ("1",), "query"),
        ("engineer", ("",), "job ID"),
        ("engineer", (" 1",), "job ID"),
        ("engineer", ("1", "1"), "unique"),
    ],
)
def test_reranker_rejects_invalid_inputs(
    query: str,
    job_ids: tuple[str, ...],
    message: str,
) -> None:
    reranker = reranker_module.SemanticReranker(
        "reranker-v7",
        FakeDocuments({"1": "full job"}),
        FakeRuntime({"results": [{"index": 0, "relevance_score": 1.0}]}),
    )

    with pytest.raises(ValueError, match=message):
        reranker.score(query, job_ids)


def test_reranker_returns_empty_without_lookup_or_endpoint_call() -> None:
    documents = FakeDocuments({})
    runtime = FakeRuntime({})
    reranker = reranker_module.SemanticReranker("reranker-v7", documents, runtime)

    assert reranker.score("engineer", ()) == {}
    assert documents.calls == []
    assert runtime.calls == []


@pytest.mark.parametrize(
    ("values", "message"),
    [({}, "missing"), ({"1": " "}, "non-empty")],
)
def test_reranker_requires_every_full_job_document(values: dict[str, str], message: str) -> None:
    reranker = reranker_module.SemanticReranker(
        "reranker-v7",
        FakeDocuments(values),
        FakeRuntime({}),
    )

    with pytest.raises(RuntimeError, match=message):
        reranker.score("engineer", ("1",))


def test_reranker_rejects_requests_over_sagemaker_body_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reranker_module, "MAX_REQUEST_BYTES", 100)
    runtime = FakeRuntime({})
    reranker = reranker_module.SemanticReranker(
        "reranker-v7",
        FakeDocuments({"1": "x" * 100}),
        runtime,
    )

    with pytest.raises(ValueError, match="request body"):
        reranker.score("engineer", ("1",))
    assert runtime.calls == []


def test_reranker_rejects_more_than_the_bounded_top_50_pool() -> None:
    job_ids = tuple(str(index) for index in range(51))
    reranker = reranker_module.SemanticReranker(
        "reranker-v7",
        FakeDocuments(dict.fromkeys(job_ids, "full job")),
        FakeRuntime({}),
    )

    with pytest.raises(ValueError, match="50"):
        reranker.score("engineer", job_ids)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({"results": [{"index": 0, "relevance_score": 0.8}]}, "one result"),
        (
            {
                "results": [
                    {"index": 0, "relevance_score": 0.8},
                    {"index": 0, "relevance_score": 0.7},
                ]
            },
            "indices",
        ),
        (
            {
                "results": [
                    {"index": 0, "relevance_score": 0.8},
                    {"index": 2, "relevance_score": 0.7},
                ]
            },
            "indices",
        ),
        (
            {
                "results": [
                    {"index": 0, "relevance_score": 0.8},
                    {"index": 1, "relevance_score": float("nan")},
                ]
            },
            "score",
        ),
        (
            {
                "results": [
                    {"index": 0, "relevance_score": 0.8},
                    {"index": 1, "relevance_score": True},
                ]
            },
            "score",
        ),
    ],
)
def test_reranker_rejects_malformed_result_indices_and_scores(
    response: object, message: str
) -> None:
    reranker = reranker_module.SemanticReranker(
        "reranker-v7",
        FakeDocuments({"1": "first", "2": "second"}),
        FakeRuntime(response),
    )

    with pytest.raises(RuntimeError, match=message):
        reranker.score("engineer", ("1", "2"))


def test_from_aws_builds_runtime_with_explicit_botocore_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    runtime = FakeRuntime({"results": [{"index": 0, "relevance_score": 0.8}]})

    def client(service_name: str, **kwargs: object) -> FakeRuntime:
        captured["service_name"] = service_name
        captured.update(kwargs)
        return runtime

    monkeypatch.setattr(reranker_module.boto3, "client", client)
    reranker = reranker_module.SemanticReranker.from_aws(
        endpoint_name="reranker-v7",
        region_name="us-west-2",
        documents=FakeDocuments({"1": "full job"}),
        connect_timeout_seconds=1,
        read_timeout_seconds=30,
    )

    assert reranker.score("engineer", ("1",)) == {"1": 0.8}
    assert captured["service_name"] == "sagemaker-runtime"
    assert captured["region_name"] == "us-west-2"
    config = captured["config"]
    assert config.connect_timeout == 1
    assert config.read_timeout == 30


def test_constructor_rejects_blank_endpoint() -> None:
    with pytest.raises(ValueError, match="endpoint"):
        reranker_module.SemanticReranker(" ", FakeDocuments({}), FakeRuntime({}))
