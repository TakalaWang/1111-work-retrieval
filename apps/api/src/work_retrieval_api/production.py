from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from work_retrieval_core import JobMetadata, RetrievalPorts, RuntimeManifest
from work_retrieval_core.adapters import (
    CorpusQueryCompiler,
    FilterTaxonomy,
    SageMakerQueryEncoder,
    TantivyBm25Retriever,
    TantivyLayout,
    WholeEmbeddingLayout,
    WholeQwenExactRetriever,
    load_job_ids,
)
from work_retrieval_database import DatabaseSettings, SqlAlchemyJobReader

SOURCE_TIMEZONE = ZoneInfo("Asia/Taipei")


class ProductionJobMetadataLookup:
    def __init__(self, reader: SqlAlchemyJobReader, taxonomy: FilterTaxonomy) -> None:
        self._reader = reader
        self._taxonomy = taxonomy

    def get_many(self, job_ids: tuple[str, ...]) -> tuple[JobMetadata, ...]:
        records = self._reader.metadata_for_job_ids(job_ids)
        return tuple(
            JobMetadata(
                job_id=record.job_id,
                source_modified_at=_source_timestamp(record.source_modified_at),
                location_codes=self._taxonomy.location_codes_for_term(record.work_city),
                duty_codes=self._taxonomy.duty_codes_for_terms(
                    (record.duty_major, record.duty_middle, record.duty_minor)
                ),
            )
            for record in records
        )

    def job_details(self, job_id: str) -> dict[str, str | None] | None:
        return self._reader.job_details(job_id)

    def close(self) -> None:
        self._reader.close()


def create_production_ports(
    manifest: RuntimeManifest,
    enable_multiview: bool,
    environment: Mapping[str, str],
) -> RetrievalPorts:
    if enable_multiview:
        raise RuntimeError("multi-view serving has no promoted production adapter")
    enable_dense_shadow = _boolean(environment.get("SEARCH_ENABLE_DENSE_SHADOW", "false"))
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
    lexical = TantivyBm25Retriever(
        runtime_root / tantivy_layout.index_directory,
        job_ids,
        taxonomy,
    )
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
                eligible_rows=lexical,
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
        metadata = ProductionJobMetadataLookup(
            SqlAlchemyJobReader.from_settings(DatabaseSettings.from_environment(environment)),
            taxonomy,
        )
    except Exception:
        if dense is not None:
            dense.close()
        lexical.close()
        raise
    return RetrievalPorts(lexical, dense, metadata, query_compiler=compiler)


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"missing required production setting: {name}")
    return value.strip()


def _boolean(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise RuntimeError("SEARCH_ENABLE_DENSE_SHADOW must be true or false")


def _source_timestamp(value: datetime) -> datetime:
    if value.tzinfo is not None or value.utcoffset() is not None:
        raise RuntimeError("source_modified_at must use the database's naive Taiwan contract")
    return value.replace(tzinfo=SOURCE_TIMEZONE).astimezone(UTC)
