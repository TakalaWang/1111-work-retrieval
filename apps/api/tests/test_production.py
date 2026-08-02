from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from work_retrieval_api.production import ProductionJobMetadataLookup
from work_retrieval_core.adapters import FilterTaxonomy
from work_retrieval_database import JobMetadataRecord, SqlAlchemyJobReader


def _taxonomy() -> FilterTaxonomy:
    return FilterTaxonomy(
        {"100100": ("台北市",)},
        {"140200": ("資訊系統",)},
        {"台北市": ("100100",)},
        {"資訊系統": ("140200",)},
    )


def test_production_metadata_maps_taxonomy_and_naive_taiwan_timestamp() -> None:
    reader = MagicMock(spec=SqlAlchemyJobReader)
    reader.metadata_for_job_ids.return_value = (
        JobMetadataRecord(
            "1",
            "台北市",
            "資訊系統",
            None,
            None,
            datetime(2026, 6, 8, 8),
        ),
    )
    lookup = ProductionJobMetadataLookup(reader, _taxonomy())

    result = lookup.get_many(("1",))

    assert result[0].source_modified_at == datetime(2026, 6, 8, tzinfo=UTC)
    assert result[0].location_codes == ("100100",)
    assert result[0].duty_codes == ("140200",)


def test_production_metadata_rejects_unexpected_aware_database_timestamp() -> None:
    reader = MagicMock(spec=SqlAlchemyJobReader)
    reader.metadata_for_job_ids.return_value = (
        JobMetadataRecord("1", "台北市", "資訊系統", None, None, datetime.now(UTC)),
    )
    lookup = ProductionJobMetadataLookup(reader, _taxonomy())

    with pytest.raises(RuntimeError, match="naive Taiwan"):
        lookup.get_many(("1",))


def test_production_metadata_delegates_job_detail_lookup() -> None:
    reader = MagicMock(spec=SqlAlchemyJobReader)
    reader.job_details.return_value = {"職務名稱": "後端工程師"}
    lookup = ProductionJobMetadataLookup(reader, _taxonomy())

    assert lookup.job_details("1") == {"職務名稱": "後端工程師"}
    reader.job_details.assert_called_once_with("1")
