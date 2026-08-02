from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from work_retrieval_core import CandidateRetriever, JobMetadata, RetrievalPorts, RuntimeManifest
from work_retrieval_core.adapters import (
    CorpusQueryCompiler,
    EligibleRows,
    FilterTaxonomy,
    SageMakerQueryEncoder,
    SkillGraphLayout,
    TantivyBm25Retriever,
    TantivyLayout,
    WholeEmbeddingLayout,
    WholeQwenExactRetriever,
    graph_conditioned_retriever,
    load_job_ids,
)
from work_retrieval_core.constraints import normalize_salary_bound, salary_period
from work_retrieval_core.reranker import SemanticReranker
from work_retrieval_database import DatabaseSettings, SqlAlchemyJobReader

from .runtime import RERANKER_V7_ENDPOINT_NAME

SOURCE_TIMEZONE = ZoneInfo("Asia/Taipei")


class ProductionJobMetadataLookup:
    def __init__(self, reader: SqlAlchemyJobReader, taxonomy: FilterTaxonomy) -> None:
        self._reader = reader
        self._taxonomy = taxonomy

    def get_many(self, job_ids: tuple[str, ...]) -> tuple[JobMetadata, ...]:
        records = self._reader.metadata_for_job_ids(job_ids)
        result: list[JobMetadata] = []
        for record in records:
            salary_min, salary_max = _salary_bounds(record.salary_min, record.salary_max)
            result.append(
                JobMetadata(
                    job_id=record.job_id,
                    source_modified_at=_source_timestamp(record.source_modified_at),
                    location_codes=self._taxonomy.location_codes_for_term(record.work_city),
                    duty_codes=self._taxonomy.duty_codes_for_terms(
                        (record.duty_major, record.duty_middle, record.duty_minor)
                    ),
                    job_attribute=record.job_attribute,
                    work_hours=record.work_hours,
                    experience_requirement=record.experience_requirement,
                    management_count=record.management_count,
                    education_requirement=record.education_requirement,
                    salary_period=salary_period(record.salary_text),
                    salary_min=salary_min,
                    salary_max=salary_max,
                )
            )
        return tuple(result)

    def job_details(self, job_id: str) -> dict[str, str | None] | None:
        return self._reader.job_details(job_id)

    def close(self) -> None:
        self._reader.close()


def create_production_ports(
    manifest: RuntimeManifest,
    enable_multiview: bool,
    enable_graph: bool,
    environment: Mapping[str, str],
) -> RetrievalPorts:
    if enable_multiview:
        raise RuntimeError("multi-view serving has no promoted production adapter")
    enable_dense_shadow = _boolean(
        environment.get("SEARCH_ENABLE_DENSE_SHADOW", "false"),
        name="SEARCH_ENABLE_DENSE_SHADOW",
    )
    reranker_mode = _reranker_mode(environment.get("SEARCH_ENABLE_RERANKER", "off"))
    if reranker_mode == "active":
        raise RuntimeError("semantic reranker fixed339 promotion gate failed")
    runtime_root = Path(_required(environment, "SEARCH_RUNTIME_ROOT")).resolve()
    tantivy_layout = TantivyLayout.from_path(
        runtime_root / manifest.temporal_tantivy.manifest_path,
        manifest,
    )
    taxonomy = FilterTaxonomy.from_path(runtime_root / tantivy_layout.taxonomy_path)
    job_ids = load_job_ids(
        runtime_root / tantivy_layout.job_ids_path,
        expected_rows=manifest.whole_embedding.rows if enable_dense_shadow else None,
    )
    if tantivy_layout.query_corrections_path is None:
        if tantivy_layout.query_corrections_attestation_path is not None:
            raise RuntimeError("disabled query corrections cannot carry an attestation")
        compiler = CorpusQueryCompiler.identity()
    else:
        if tantivy_layout.query_corrections_attestation_path is None:
            raise RuntimeError("enabled query corrections require a promotion attestation")
        compiler = CorpusQueryCompiler.from_promoted_paths(
            runtime_root / tantivy_layout.query_corrections_path,
            runtime_root / tantivy_layout.query_corrections_attestation_path,
        )
    baseline = TantivyBm25Retriever(
        runtime_root / tantivy_layout.index_directory,
        job_ids,
        taxonomy,
    )
    lexical: CandidateRetriever = baseline
    eligible_rows: EligibleRows = baseline
    if enable_graph:
        if manifest.skill_graph is None:
            baseline.close()
            raise RuntimeError("SEARCH_ENABLE_GRAPH requires a promoted Graph manifest")
        try:
            graph_layout = SkillGraphLayout.from_path(
                runtime_root / manifest.skill_graph.manifest_path,
                manifest,
                runtime_root=runtime_root,
            )
            graph_retriever = graph_conditioned_retriever(
                runtime_root=runtime_root,
                layout=graph_layout,
                baseline=baseline,
                taxonomy=taxonomy,
            )
            lexical = graph_retriever
            eligible_rows = graph_retriever
        except Exception:
            baseline.close()
            raise
    dense: WholeQwenExactRetriever | None = None
    if enable_dense_shadow:
        try:
            whole_layout = WholeEmbeddingLayout.from_path(
                runtime_root / manifest.whole_embedding.manifest_path,
                manifest,
            )
            dense_job_ids = load_job_ids(
                runtime_root / whole_layout.job_ids_path,
                expected_rows=manifest.whole_embedding.rows,
            )
            if dense_job_ids != job_ids:
                raise RuntimeError("BM25 and whole-Qwen job row order differs")
            dense = WholeQwenExactRetriever(
                runtime_root=runtime_root,
                layout=whole_layout,
                job_ids=job_ids,
                eligible_rows=eligible_rows,
                encoder=SageMakerQueryEncoder.from_aws(
                    endpoint_name=_required(environment, "EMBEDDING_ENDPOINT_NAME"),
                    endpoint_config_name=_required(environment, "EMBEDDING_ENDPOINT_CONFIG_NAME"),
                    model_name=_required(environment, "EMBEDDING_MODEL_NAME"),
                    region_name=_required(environment, "AWS_REGION"),
                ),
            )
        except Exception:
            lexical.close()
            raise
    try:
        reader = SqlAlchemyJobReader.from_settings(DatabaseSettings.from_environment(environment))
        metadata = ProductionJobMetadataLookup(reader, taxonomy)
    except Exception:
        if dense is not None:
            dense.close()
        lexical.close()
        raise
    reranker: SemanticReranker | None = None
    if reranker_mode == "shadow":
        endpoint_name = _required(environment, "RERANKER_ENDPOINT_NAME")
        if endpoint_name != RERANKER_V7_ENDPOINT_NAME:
            metadata.close()
            if dense is not None:
                dense.close()
            lexical.close()
            raise RuntimeError("RERANKER_ENDPOINT_NAME must identify the v7 endpoint")
        reranker = SemanticReranker.from_aws(
            endpoint_name=endpoint_name,
            region_name=_required(environment, "AWS_REGION"),
            documents=reader,
        )
    return RetrievalPorts(
        lexical,
        dense,
        metadata,
        query_compiler=compiler,
        reranker=reranker,
    )


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"missing required production setting: {name}")
    return value.strip()


def _boolean(value: str, *, name: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise RuntimeError(f"{name} must be true or false")


def _reranker_mode(value: str) -> str:
    if value in {"off", "shadow", "active"}:
        return value
    raise RuntimeError("SEARCH_ENABLE_RERANKER must be off, shadow, or active")


def _source_timestamp(value: datetime) -> datetime:
    if value.tzinfo is not None or value.utcoffset() is not None:
        raise RuntimeError("source_modified_at must use the database's naive Taiwan contract")
    return value.replace(tzinfo=SOURCE_TIMEZONE).astimezone(UTC)


def _salary_bounds(
    lower: Decimal | None,
    upper: Decimal | None,
) -> tuple[int | None, int | None]:
    try:
        return normalize_salary_bound(lower), normalize_salary_bound(upper)
    except ValueError:
        return None, None
