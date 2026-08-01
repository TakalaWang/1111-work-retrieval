from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import numpy as np
import tantivy
from work_retrieval_core import CandidateRequest
from work_retrieval_core.adapters import (
    EmbeddingShard,
    FilterTaxonomy,
    SageMakerQueryEncoder,
    TantivyBm25Retriever,
    WholeEmbeddingLayout,
    WholeQwenExactRetriever,
    lexical_tokens,
)
from work_retrieval_core.serialization import FULL_JOB_FIELDS, serialize_full_job

AS_OF = datetime(2026, 6, 8, tzinfo=UTC)


def _taxonomy() -> FilterTaxonomy:
    locations = {"100100": ("台北市",), "100200": ("高雄市",)}
    duties = {"140200": ("資訊系統",), "140300": ("財務會計",)}
    return FilterTaxonomy(
        locations,
        duties,
        {"台北市": ("100100",), "高雄市": ("100200",)},
        {"資訊系統": ("140200",), "財務會計": ("140300",)},
    )


def _index(path: Path) -> tuple[tuple[str, ...], TantivyBm25Retriever]:
    path.mkdir()
    builder = tantivy.SchemaBuilder()
    for field in ("title", "duty", "skills", "industry", "body"):
        builder.add_text_field(field)
    for field in ("location_filter", "duty_filter", "visibility_filter"):
        builder.add_text_field(field, tokenizer_name="raw")
    builder.add_unsigned_field("updated_at_epoch_ms", indexed=True, fast=True)
    builder.add_unsigned_field("job_index", fast=True)
    schema = builder.build()
    index = tantivy.Index(schema, path=str(path), reuse=False)
    writer = index.writer()
    rows = (
        ("1", "平台職缺", "Kubernetes 維運服務", "台北市", "資訊系統", 1, -10),
        ("2", "平台職缺", "Kubernetes 維運服務", "台北市", "資訊系統", 1, -181),
        ("3", "平台職缺", "Kubernetes 維運服務", "台北市", "資訊系統", 1, 1),
        ("4", "平台職缺", "Kubernetes 維運服務", "台北市", "資訊系統", 0, -1),
        ("5", "平台職缺", "Kubernetes 維運服務", "高雄市", "資訊系統", 1, -1),
    )
    for row, (_job_id, title, body, city, duty, visible, days) in enumerate(rows):
        document = tantivy.Document()
        document.add_text("title", " ".join(lexical_tokens(title)))
        document.add_text("duty", " ".join(lexical_tokens(duty)))
        document.add_text("skills", "")
        document.add_text("industry", "")
        document.add_text("body", " ".join(lexical_tokens(body)))
        document.add_text("location_filter", city)
        document.add_text("duty_filter", duty)
        document.add_text("visibility_filter", str(visible))
        document.add_unsigned(
            "updated_at_epoch_ms", int((AS_OF + timedelta(days=days)).timestamp() * 1000)
        )
        document.add_unsigned("job_index", row)
        writer.add_document(document)
    writer.commit()
    writer.wait_merging_threads()
    index.reload()
    job_ids = tuple(row[0] for row in rows)
    return job_ids, TantivyBm25Retriever(path, job_ids, _taxonomy())


def _request(*, location: str = "100100", duty: str = "140200") -> CandidateRequest:
    return CandidateRequest(
        "kubernetes",
        (location,),
        (duty,),
        AS_OF,
        AS_OF - timedelta(days=180),
    )


def test_tantivy_searches_body_and_applies_all_filters_before_top_k(tmp_path: Path) -> None:
    job_ids, retriever = _index(tmp_path / "index")

    candidates = retriever.retrieve(_request(), limit=10)
    eligible = retriever.eligible_indices(_request())

    assert [candidate.job_id for candidate in candidates] == ["1", "3"]
    assert [candidate.rank for candidate in candidates] == [1, 2]
    assert [job_ids[index] for index in eligible] == ["1", "3"]
    assert retriever.retrieve(_request(location="100200"), limit=10)[0].job_id == "5"
    assert retriever.retrieve(_request(duty="140300"), limit=10) == ()


def test_filter_taxonomy_resolves_known_codes_and_makes_unknown_codes_no_match() -> None:
    taxonomy = _taxonomy()
    assert taxonomy.resolve_locations(("100100",)) == ("台北市",)
    assert taxonomy.duty_codes_for_terms(("資訊系統", None)) == ("140200",)
    assert taxonomy.resolve_duties(("999999",)) is None


class FixedEligibleRows:
    def eligible_indices(self, request: CandidateRequest) -> np.ndarray:
        del request
        return np.asarray([0, 2], dtype=np.int64)


class FixedEncoder:
    def encode(self, query: str) -> np.ndarray:
        assert query == "kubernetes"
        value = np.zeros(4096, dtype=np.float32)
        value[0] = 1
        return value


def test_whole_qwen_exact_scan_uses_only_hard_filtered_rows(tmp_path: Path) -> None:
    vectors = np.zeros((3, 4096), dtype=np.float16)
    vectors[:, 0] = [0.2, 1.0, 0.8]
    vector_path = tmp_path / "vectors.npy"
    np.save(vector_path, vectors, allow_pickle=False)
    retriever = WholeQwenExactRetriever(
        runtime_root=tmp_path,
        layout=WholeEmbeddingLayout("job-ids.json", (EmbeddingShard("vectors.npy", 0, 3),)),
        job_ids=("1", "2", "3"),
        eligible_rows=FixedEligibleRows(),
        encoder=FixedEncoder(),
        chunk_rows=1,
    )

    candidates = retriever.retrieve(_request(), limit=2)

    assert [(item.job_id, item.rank) for item in candidates] == [("3", 1), ("1", 2)]
    assert "2" not in {item.job_id for item in candidates}


class FakeSageMaker:
    def __init__(self, vector: list[list[float]]) -> None:
        self.vector = vector
        self.kwargs: dict[str, object] = {}

    def invoke_endpoint(self, **kwargs: object) -> dict[str, object]:
        self.kwargs = kwargs
        return {"Body": BytesIO(json.dumps(self.vector).encode())}


def test_sagemaker_encoder_uses_pinned_query_prompt_and_normalizes() -> None:
    vector = [[0.0] * 4096]
    vector[0][0] = 2.0
    runtime = FakeSageMaker(vector)
    encoder = SageMakerQueryEncoder("verified-endpoint", runtime)

    result = encoder.encode("資料工程師")
    body = json.loads(runtime.kwargs["Body"])

    assert result[0] == 1.0
    assert body["inputs"][0].endswith("Query: 資料工程師")


def test_full_job_serializer_includes_description_and_is_deterministic() -> None:
    values = {field: None for _label, field in FULL_JOB_FIELDS}
    values["title"] = "資料工程師"
    values["description"] = "<p>建立 ETL pipeline</p>"

    serialized = serialize_full_job(values)

    assert serialized == "職務名稱: 資料工程師\n職務內容: 建立 ETL pipeline"
