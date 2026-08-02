from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
import tantivy
import work_retrieval_core.adapters as adapters
from work_retrieval_core import (
    Artifact,
    CandidateRequest,
    EducationConstraint,
    JobAttributeConstraint,
    ManagementConstraint,
    MonthlySalaryConstraint,
    NoExperienceConstraint,
    QueryConstraints,
    RuntimeManifest,
    WorkShiftConstraint,
)
from work_retrieval_core.adapters import (
    CorpusQueryCompiler,
    EmbeddingShard,
    FilterTaxonomy,
    SageMakerQueryEncoder,
    SkillGraphLayout,
    TantivyBm25Retriever,
    TantivyLayout,
    WholeEmbeddingLayout,
    WholeQwenExactRetriever,
    lexical_tokens,
    load_job_ids,
)
from work_retrieval_core.graph_policy import (
    GRAPH_SERVING_IMPLEMENTATION_SHA256,
    GRAPH_SERVING_POLICY_SHA256,
)
from work_retrieval_core.manifest import (
    WHOLE_DOCUMENT_FIELDS,
    WHOLE_DOCUMENT_POLICY_VERSION,
    WHOLE_DOCUMENT_TEMPLATE_SHA256,
    GraphPromotionEvidence,
    SkillGraph,
    TemporalTantivy,
    WholeEmbedding,
)
from work_retrieval_core.serialization import (
    FULL_JOB_FIELDS,
    serialize_full_job,
)

AS_OF = datetime(2026, 6, 8, tzinfo=UTC)
HEX = "a" * 64


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
    for field in adapters.RAW_FILTER_FIELDS:
        builder.add_text_field(field, tokenizer_name="raw")
    builder.add_unsigned_field("updated_at_epoch_ms", indexed=True, fast=True)
    for field in adapters.NUMERIC_FILTER_FIELDS:
        builder.add_unsigned_field(field, indexed=True)
    builder.add_unsigned_field("job_index", fast=True)
    schema = builder.build()
    index = tantivy.Index(schema, path=str(path), reuse=False)
    writer = index.writer()
    rows = (
        (
            "1",
            "平台職缺",
            "Kubernetes Kubernetes Kubernetes 維運服務",
            "台北市",
            "資訊系統",
            1,
            -10,
            "碩士",
            55_000,
            70_000,
        ),
        ("2", "平台職缺", "Kubernetes 維運服務", "台北市", "資訊系統", 1, -181, "不拘", 50_000, 0),
        ("3", "平台職缺", "Kubernetes 維運服務", "台北市", "資訊系統", 1, 1, "碩士", 40_000, 0),
        ("4", "平台職缺", "Kubernetes 維運服務", "台北市", "資訊系統", 0, -1, "不拘", 50_000, 0),
        ("5", "平台職缺", "Kubernetes 維運服務", "高雄市", "資訊系統", 1, -1, "不拘", 50_000, 0),
        (
            "6",
            "平台職缺",
            "Kubernetes Kubernetes 維運服務",
            "台北市",
            "資訊系統",
            1,
            -180,
            "大學",
            40_000,
            60_000,
        ),
        ("7", "平台職缺", "Kubernetes 維運服務", "台北市", "資訊系統", 1, 0, "不拘", 50_000, 0),
    )
    for row, (
        _job_id,
        title,
        body,
        city,
        duty,
        visible,
        days,
        education,
        salary_lower,
        salary_recall,
    ) in enumerate(rows):
        document = tantivy.Document()
        document.add_text("title", " ".join(lexical_tokens(title)))
        document.add_text("duty", " ".join(lexical_tokens(duty)))
        document.add_text("skills", "")
        document.add_text("industry", "")
        document.add_text("body", " ".join(lexical_tokens(body)))
        document.add_text("location_filter", city)
        document.add_text("duty_filter", duty)
        document.add_text("visibility_filter", str(visible))
        document.add_text(adapters.EDUCATION_FILTER_FIELD, education)
        attribute, shift, experience, management = {
            "1": ("全職", "日班", None, None),
            "6": ("兼職", "晚班", "無工作經驗", "required"),
            "7": ("工讀", "晚班", "不拘", "required"),
        }.get(_job_id, ("全職", "日班", None, None))
        document.add_text(adapters.JOB_ATTRIBUTE_FILTER_FIELD, attribute)
        document.add_text(adapters.WORK_SHIFT_FILTER_FIELD, shift)
        if experience is not None:
            document.add_text(adapters.EXPERIENCE_FILTER_FIELD, experience)
        if management is not None:
            document.add_text(adapters.MANAGEMENT_FILTER_FIELD, management)
        document.add_unsigned(
            "updated_at_epoch_ms", int((AS_OF + timedelta(days=days)).timestamp() * 1000)
        )
        document.add_unsigned(adapters.MONTHLY_SALARY_LOWER_FIELD, salary_lower)
        document.add_unsigned(adapters.MONTHLY_SALARY_RECALL_FIELD, salary_recall)
        document.add_unsigned("job_index", row)
        writer.add_document(document)
    writer.commit()
    writer.wait_merging_threads()
    index.reload()
    job_ids = tuple(row[0] for row in rows)
    return job_ids, TantivyBm25Retriever(path, job_ids, _taxonomy())


def _request(
    *,
    location: str = "100100",
    duty: str = "140200",
    constraints: QueryConstraints | None = None,
) -> CandidateRequest:
    return CandidateRequest(
        "kubernetes",
        (location,),
        (duty,),
        AS_OF,
        AS_OF - timedelta(days=180),
        constraints=constraints or QueryConstraints(),
    )


def test_tantivy_searches_body_and_applies_all_filters_before_top_k(tmp_path: Path) -> None:
    job_ids, retriever = _index(tmp_path / "index")

    candidates = retriever.retrieve(_request(), limit=10)
    eligible = retriever.eligible_indices(_request())

    assert [candidate.job_id for candidate in candidates] == ["1", "3", "6", "7"]
    assert [candidate.rank for candidate in candidates] == [1, 2, 3, 4]
    assert [job_ids[index] for index in eligible] == ["1", "3", "6", "7"]
    assert retriever.retrieve(_request(location="100200"), limit=10)[0].job_id == "5"
    assert retriever.retrieve(_request(duty="140300"), limit=10) == ()


def test_tantivy_applies_education_and_salary_constraints_before_top_k(
    tmp_path: Path,
) -> None:
    job_ids, retriever = _index(tmp_path / "index")
    education = QueryConstraints(education=EducationConstraint("大學"))
    strict_salary = QueryConstraints(monthly_salary=MonthlySalaryConstraint(50_000, strict=True))
    combined = QueryConstraints(
        education=EducationConstraint("大學"),
        monthly_salary=MonthlySalaryConstraint(50_000, strict=True),
    )

    assert [
        job_ids[index] for index in retriever.eligible_indices(_request(constraints=education))
    ] == [
        "6",
        "7",
    ]
    assert [
        job_ids[index] for index in retriever.eligible_indices(_request(constraints=strict_salary))
    ] == ["1", "7"]
    assert retriever.retrieve(_request(constraints=combined), limit=1)[0].job_id == "7"


def test_tantivy_applies_typed_job_constraints_before_top_k(tmp_path: Path) -> None:
    job_ids, retriever = _index(tmp_path / "index")
    constraints = (
        QueryConstraints(job_attribute=JobAttributeConstraint("兼職")),
        QueryConstraints(work_shift=WorkShiftConstraint("晚班")),
        QueryConstraints(no_experience=NoExperienceConstraint()),
        QueryConstraints(management=ManagementConstraint()),
    )

    eligible = [
        [job_ids[index] for index in retriever.eligible_indices(_request(constraints=value))]
        for value in constraints
    ]

    assert eligible == [["6"], ["6", "7"], ["6", "7"], ["6", "7"]]


def test_filter_taxonomy_resolves_known_codes_and_makes_unknown_codes_no_match() -> None:
    taxonomy = _taxonomy()
    assert taxonomy.resolve_locations(("100100",)) == ("台北市",)
    assert taxonomy.duty_codes_for_terms(("資訊系統", None)) == ("140200",)
    assert taxonomy.resolve_duties(("999999",)) is None


class FixedEligibleRows:
    def eligible_indices(self, request: CandidateRequest, *, max_rows: int) -> np.ndarray:
        del request
        values = np.asarray([0, 2], dtype=np.int64)
        if len(values) > max_rows:
            raise RuntimeError("eligible universe exceeds its bounded materialization limit")
        return values


class FixedEncoder:
    def encode(self, query: str) -> np.ndarray:
        assert query == "kubernetes"
        value = np.zeros(1024, dtype=np.float32)
        value[0] = 1
        return value


def test_whole_qwen_exact_scan_uses_only_hard_filtered_rows(tmp_path: Path) -> None:
    vectors = np.zeros((3, 1024), dtype=np.float16)
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
    assert result.shape == (1024,)
    assert body["inputs"][0].endswith("Query: 資料工程師")


def test_full_job_serializer_includes_description_and_is_deterministic() -> None:
    values = {field: None for _label, field in FULL_JOB_FIELDS}
    values["title"] = "資料工程師"
    values["description"] = "<p>建立 ETL pipeline</p>"
    values["work_hours_description"] = "彈性工時"
    values["language_1"] = "英文"
    values["requires_travel"] = "否"

    serialized = serialize_full_job(values)

    assert serialized == (
        "職務名稱: 資料工程師\n"
        "工時說明: 彈性工時\n"
        "語言能力一: 英文\n"
        "是否需外派: 否\n"
        "職務內容: 建立 ETL pipeline"
    )


def test_bm25_job_ids_do_not_require_a_whole_embedding_row_count(tmp_path: Path) -> None:
    path = tmp_path / "job-ids.json"
    path.write_text('["1","2"]', encoding="utf-8")

    assert load_job_ids(path) == ("1", "2")
    with pytest.raises(RuntimeError, match="serving contract"):
        load_job_ids(path, expected_rows=3)


def test_query_compiler_uses_only_pre_cutoff_corpus_rules(tmp_path: Path) -> None:
    path = tmp_path / "corrections.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "complete": True,
                "publication_allowed": False,
                "source_policy": "train_jd_only",
                "test_jd_used": False,
                "uses_ground_truth": False,
                "uses_behavior_logs": False,
                "train_cutoff_exclusive": "2026-06-08T00:00:00+08:00",
                "max_source_timestamp": "2026-06-07T23:59:59+08:00",
                "source_manifest_sha256": "1" * 64,
                "evidence_sha256": "2" * 64,
                "minimum_support": 3,
                "corrections": {"kuberntes": "kubernetes"},
            }
        ),
        encoding="utf-8",
    )
    candidate_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    attestation_path = tmp_path / "corrections-attestation.json"
    attestation_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "complete": True,
                "attestation_kind": "fixed-input-query-correction-promotion",
                "candidate_sha256": candidate_sha256,
                "promotion_report_sha256": "3" * 64,
                "publication_allowed": True,
                "evaluator_kind": "organizer",
                "significant": True,
                "primary_metric": "ndcg_at_10",
                "absolute_delta": 0.001,
                "evaluation_split_sha256": "4" * 64,
                "baseline_run_sha256": "5" * 64,
                "candidate_run_sha256": "6" * 64,
            }
        ),
        encoding="utf-8",
    )

    compiled = CorpusQueryCompiler.from_promoted_paths(path, attestation_path).compile("Kuberntes")

    assert compiled.lexical_texts == ("Kuberntes", "kubernetes")
    assert compiled.rewrites[0].policy == "train_jd_corpus_v1"
    assert CorpusQueryCompiler.identity().compile("Kuberntes").lexical_texts == ("Kuberntes",)

    invalid = json.loads(path.read_text(encoding="utf-8"))
    invalid["max_source_timestamp"] = invalid["train_cutoff_exclusive"]
    path.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(RuntimeError, match="post-cutoff"):
        CorpusQueryCompiler.from_promoted_paths(path, attestation_path)


def test_exact_dense_scan_rejects_unbounded_or_expired_work(tmp_path: Path) -> None:
    vectors = np.zeros((3, 1024), dtype=np.float16)
    vectors[:, 0] = [0.2, 1.0, 0.8]
    np.save(tmp_path / "vectors.npy", vectors, allow_pickle=False)
    layout = WholeEmbeddingLayout("job-ids.json", (EmbeddingShard("vectors.npy", 0, 3),))
    with pytest.raises(RuntimeError, match="exceeds"):
        WholeQwenExactRetriever(
            runtime_root=tmp_path,
            layout=layout,
            job_ids=("1", "2", "3"),
            eligible_rows=FixedEligibleRows(),
            encoder=FixedEncoder(),
            max_eligible_rows=1,
        ).retrieve(_request(), limit=2)

    clock_values = iter((0.0, 3.0))
    with pytest.raises(RuntimeError, match="deadline"):
        WholeQwenExactRetriever(
            runtime_root=tmp_path,
            layout=layout,
            job_ids=("1", "2", "3"),
            eligible_rows=FixedEligibleRows(),
            encoder=FixedEncoder(),
            timeout_seconds=2.0,
            clock=lambda: next(clock_values),
        ).retrieve(_request(), limit=2)


class FakeControlPlane:
    def __init__(self, image: str = adapters.TEI_IMAGE_URI) -> None:
        self.image = image

    def describe_endpoint(self, **kwargs: object) -> dict[str, object]:
        assert kwargs == {"EndpointName": adapters.ENDPOINT_NAME}
        return {
            "EndpointStatus": "InService",
            "EndpointConfigName": adapters.ENDPOINT_CONFIG_NAME,
        }

    def describe_endpoint_config(self, **kwargs: object) -> dict[str, object]:
        assert kwargs == {"EndpointConfigName": adapters.ENDPOINT_CONFIG_NAME}
        return {"ProductionVariants": [{"ModelName": adapters.ENDPOINT_MODEL_NAME}]}

    def describe_model(self, **kwargs: object) -> dict[str, object]:
        assert kwargs == {"ModelName": adapters.ENDPOINT_MODEL_NAME}
        return {
            "PrimaryContainer": {
                "Image": self.image,
                "Environment": adapters.TEI_ENVIRONMENT,
            }
        }


def test_sagemaker_factory_reads_back_promoted_model_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = FakeControlPlane()
    runtime = FakeSageMaker([[0.0] * 4096])

    def client(service: str, *, region_name: str, config: object) -> object:
        assert config is not None
        assert region_name == "us-west-2"
        return control if service == "sagemaker" else runtime

    monkeypatch.setattr(adapters.boto3, "client", client)

    encoder = SageMakerQueryEncoder.from_aws(
        endpoint_name=adapters.ENDPOINT_NAME,
        endpoint_config_name=adapters.ENDPOINT_CONFIG_NAME,
        model_name=adapters.ENDPOINT_MODEL_NAME,
        region_name="us-west-2",
    )

    assert isinstance(encoder, SageMakerQueryEncoder)

    with pytest.raises(RuntimeError, match="promoted identity"):
        SageMakerQueryEncoder.from_aws(
            endpoint_name="mutable-latest",
            endpoint_config_name=adapters.ENDPOINT_CONFIG_NAME,
            model_name=adapters.ENDPOINT_MODEL_NAME,
            region_name="us-west-2",
        )

    control = FakeControlPlane("mutable-image:latest")
    with pytest.raises(RuntimeError, match="image or environment"):
        SageMakerQueryEncoder.from_aws(
            endpoint_name=adapters.ENDPOINT_NAME,
            endpoint_config_name=adapters.ENDPOINT_CONFIG_NAME,
            model_name=adapters.ENDPOINT_MODEL_NAME,
            region_name="us-west-2",
        )


def test_whole_layout_requires_sealed_source_lineage(tmp_path: Path) -> None:
    prefix = "embeddings/qwen3-embedding-8b-clean-v1-mrl1024"
    component_path = f"{prefix}/manifest.json"
    source_manifest_path = f"{prefix}/source-manifest.json"
    source_inventory_path = f"{prefix}/source-inventory.json"
    job_ids_path = f"{prefix}/job-ids.json"
    vectors_path = f"{prefix}/vectors.npy"
    manifest = RuntimeManifest(
        (
            (component_path, Artifact("embedding", "b" * 64, 1)),
            (source_manifest_path, Artifact("evidence", "c" * 64, 1)),
            (source_inventory_path, Artifact("evidence", "f" * 64, 1)),
            (job_ids_path, Artifact("embedding", "d" * 64, 1)),
            (vectors_path, Artifact("embedding", "e" * 64, 1)),
        ),
        WholeEmbedding(component_path, "b" * 64, 3, 1024, HEX, HEX, HEX),
        TemporalTantivy("indexes/x/manifest.json", HEX, HEX, HEX, HEX, "before Top-K"),
        None,
        None,
    )
    component = {
        "schema_version": 1,
        "complete": True,
        "model": "Qwen/Qwen3-Embedding-8B",
        "revision": "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af",
        "source_dimension": 4096,
        "dimension": 1024,
        "projection": "mrl_prefix_then_l2_normalize",
        "dtype": "float16",
        "normalized": True,
        "rows": 3,
        "dataset_sha256": HEX,
        "jobs_sha256": HEX,
        "job_row_order_sha256": HEX,
        "document_policy_version": WHOLE_DOCUMENT_POLICY_VERSION,
        "document_template_sha256": WHOLE_DOCUMENT_TEMPLATE_SHA256,
        "document_fields": list(WHOLE_DOCUMENT_FIELDS),
        "query_prompt": adapters.QUERY_PROMPT,
        "source_manifest_path": source_manifest_path,
        "source_manifest_sha256": "c" * 64,
        "source_inventory_path": source_inventory_path,
        "source_inventory_sha256": "f" * 64,
        "job_ids_path": job_ids_path,
        "shards": [
            {
                "vectors_path": vectors_path,
                "row_start": 0,
                "row_end": 3,
                "rows": 3,
                "dimension": 1024,
                "vectors_sha256": "e" * 64,
                "source_vectors_sha256": "1" * 64,
            }
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(component), encoding="utf-8")

    assert WholeEmbeddingLayout.from_path(path, manifest).job_ids_path == job_ids_path

    component["source_manifest_sha256"] = "0" * 64
    path.write_text(json.dumps(component), encoding="utf-8")
    with pytest.raises(RuntimeError, match="source manifest"):
        WholeEmbeddingLayout.from_path(path, manifest)


def test_tantivy_layout_pins_lexical_policy_and_build_lineage(tmp_path: Path) -> None:
    component_path = "indexes/tantivy-bm25-temporal-v3/manifest.json"
    index_file = "indexes/tantivy-bm25-temporal-v3/index/meta.json"
    taxonomy_path = "indexes/tantivy-bm25-temporal-v3/taxonomy.json"
    job_ids_path = "indexes/tantivy-bm25-temporal-v3/job-ids.json"
    build_path = "indexes/tantivy-bm25-temporal-v3/build-manifest.json"
    artifacts = (
        (component_path, Artifact("index", "b" * 64, 1)),
        (index_file, Artifact("index", "c" * 64, 1)),
        (taxonomy_path, Artifact("index", "d" * 64, 1)),
        (job_ids_path, Artifact("index", "e" * 64, 1)),
        (build_path, Artifact("evidence", "1" * 64, 1)),
    )
    semantics = (
        "updated_at >= as_of - 180 days before Top-K; future snapshots retained with freshness 0"
    )
    manifest = RuntimeManifest(
        artifacts,
        WholeEmbedding("embeddings/x/manifest.json", HEX, 3, 1024, HEX, HEX, HEX),
        TemporalTantivy(component_path, "b" * 64, HEX, HEX, HEX, semantics),
        None,
        None,
    )
    component = {
        "schema_version": 1,
        "complete": True,
        "engine": "tantivy v0.26.0, index_format v7",
        "jobs_sha256": HEX,
        "job_row_order_sha256": HEX,
        "index_sha256": HEX,
        "index_directory": "indexes/tantivy-bm25-temporal-v3/index",
        "index_files": [index_file],
        "taxonomy_path": taxonomy_path,
        "job_ids_path": job_ids_path,
        "query_corrections": {"enabled": False},
        "build_manifest_path": build_path,
        "build_manifest_sha256": "1" * 64,
        "schema_fields": [
            *adapters.TEXT_FIELDS,
            *adapters.RAW_FILTER_FIELDS,
            adapters.UPDATED_AT_FIELD,
            *adapters.NUMERIC_FILTER_FIELDS,
            adapters.JOB_INDEX_FIELD,
        ],
        "field_boosts": adapters.FIELD_BOOSTS,
        "lexical_policy_version": adapters.LEXICAL_POLICY_VERSION,
        "lexical_policy_sha256": adapters.lexical_policy_sha256(),
        "tokenizers": adapters.TOKENIZERS,
        "source_fields": adapters.SOURCE_FIELDS,
        "filter_semantics": adapters.FILTER_SEMANTICS,
        "updated_at_field": adapters.UPDATED_AT_FIELD,
        "temporal_filter_semantics": semantics,
    }
    path = tmp_path / "tantivy-manifest.json"
    path.write_text(json.dumps(component), encoding="utf-8")

    layout = TantivyLayout.from_path(path, manifest)
    assert layout.query_corrections_path is None
    assert layout.query_corrections_attestation_path is None

    component["source_fields"] = {"body": ["description"]}
    path.write_text(json.dumps(component), encoding="utf-8")
    with pytest.raises(RuntimeError, match="source_fields"):
        TantivyLayout.from_path(path, manifest)


def test_skill_graph_layout_requires_the_sealed_six_file_contract(tmp_path: Path) -> None:
    prefix = "graphs/skill-graph"
    component_path = f"{prefix}/manifest.json"
    names = (
        "jobs.jsonl",
        "skills.jsonl",
        "job-skills.jsonl",
        "duty-skills.jsonl",
        "skill-relations.jsonl",
        "relation-evidence.jsonl",
    )
    files = [
        {"path": f"{prefix}/{name}", "sha256": str(index) * 64, "size_bytes": index}
        for index, name in enumerate(names, start=1)
    ]
    report_path = "evidence/skill-graph/report.json"
    candidate_path = "evidence/skill-graph/candidate-manifest.json"
    attestation_path = "evidence/skill-graph/organizer-attestation.json"
    candidate_file = tmp_path / candidate_path
    candidate_file.parent.mkdir(parents=True)
    candidate_file.write_text(
        json.dumps(
            {
                "artifacts": [
                    {
                        "path": entry["path"].rsplit("/", 1)[-1],
                        "kind": "graph",
                        "sha256": entry["sha256"],
                        "size_bytes": entry["size_bytes"],
                    }
                    for entry in files
                ]
            }
        ),
        encoding="utf-8",
    )
    candidate_sha256 = hashlib.sha256(candidate_file.read_bytes()).hexdigest()
    promotion = {
        "schema_version": 1,
        "complete": True,
        "publication_allowed": True,
        "evaluation_split_sha256": "d" * 64,
        "baseline_run_sha256": "e" * 64,
        "candidate_run_sha256": "f" * 64,
        "primary_metric": "ndcg_at_10",
        "baseline_value": 0.3,
        "candidate_value": 0.302,
        "absolute_delta": 0.002,
    }
    report_file = tmp_path / report_path
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(json.dumps(promotion), encoding="utf-8")
    report_sha256 = hashlib.sha256(report_file.read_bytes()).hexdigest()
    attestation = {
        "schema_version": 1,
        "complete": True,
        "attestation_kind": "fixed-input-graph-promotion",
        "candidate_manifest_sha256": candidate_sha256,
        "ablation_report_sha256": "c" * 64,
        "publication_allowed": True,
        "evaluator_id": "organizer-v1",
        "evaluator_kind": "organizer",
        "significant": True,
        "primary_metric": "ndcg_at_10",
        "baseline_value": 0.3,
        "candidate_value": 0.302,
        "absolute_delta": 0.002,
        "evaluation_split_sha256": "d" * 64,
        "baseline_run_sha256": "e" * 64,
        "candidate_run_sha256": "f" * 64,
        "serving_algorithm": "graph-conditioned-temporal-bridge-retrieval-protected-rrf-v3",
        "serving_policy_sha256": GRAPH_SERVING_POLICY_SHA256,
        "serving_implementation_sha256": GRAPH_SERVING_IMPLEMENTATION_SHA256,
        "evaluation_implementation_sha256": "9" * 64,
    }
    attestation_file = tmp_path / attestation_path
    attestation_file.write_text(json.dumps(attestation), encoding="utf-8")
    attestation_sha256 = hashlib.sha256(attestation_file.read_bytes()).hexdigest()
    manifest = RuntimeManifest(
        tuple(
            [
                (component_path, Artifact("graph", "a" * 64, 1)),
                (
                    candidate_path,
                    Artifact("evidence", candidate_sha256, candidate_file.stat().st_size),
                ),
                (report_path, Artifact("evidence", report_sha256, report_file.stat().st_size)),
                (
                    attestation_path,
                    Artifact("evidence", attestation_sha256, attestation_file.stat().st_size),
                ),
            ]
            + [
                (entry["path"], Artifact("graph", entry["sha256"], entry["size_bytes"]))
                for entry in files
            ]
        ),
        WholeEmbedding("embeddings/x/manifest.json", HEX, 3, 1024, HEX, HEX, HEX),
        TemporalTantivy("indexes/x/manifest.json", HEX, HEX, HEX, HEX, "before Top-K"),
        None,
        SkillGraph(
            component_path,
            "a" * 64,
            candidate_path,
            candidate_sha256,
            "c" * 64,
            "9" * 64,
            attestation_path,
            attestation_sha256,
            GraphPromotionEvidence(
                report_path,
                report_sha256,
                "d" * 64,
                "e" * 64,
                "f" * 64,
                0.002,
            ),
        ),
    )
    component = {
        "complete": True,
        "publication_allowed": True,
        "schema_version": 1,
        "train_cutoff_exclusive": "2026-06-08T00:00:00+08:00",
        "max_source_timestamp": "2026-06-07T23:51:07.143000+08:00",
        "source_jd_sha256": "53937f7bf076789c4cd7e3be34fb89875336108d57707b5a93182181e1087089",
        "source_policy": "train_jd_only",
        "test_jd_used": False,
        "candidate_manifest_path": candidate_path,
        "candidate_manifest_sha256": candidate_sha256,
        "source_ablation_report_sha256": "c" * 64,
        "serving_algorithm": "graph-conditioned-temporal-bridge-retrieval-protected-rrf-v3",
        "serving_policy_sha256": GRAPH_SERVING_POLICY_SHA256,
        "serving_implementation_sha256": GRAPH_SERVING_IMPLEMENTATION_SHA256,
        "evaluation_implementation_sha256": "9" * 64,
        "promotion_report_path": report_path,
        "promotion_report_sha256": report_sha256,
        "organizer_attestation_path": attestation_path,
        "organizer_attestation_sha256": attestation_sha256,
        "files": files,
    }
    path = tmp_path / component_path
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(component), encoding="utf-8")

    layout = SkillGraphLayout.from_path(path, manifest, runtime_root=tmp_path)

    assert layout.job_skills_path == f"{prefix}/job-skills.jsonl"
    component["files"] = files[:-1]
    path.write_text(json.dumps(component), encoding="utf-8")
    with pytest.raises(RuntimeError, match="files differ"):
        SkillGraphLayout.from_path(path, manifest, runtime_root=tmp_path)
