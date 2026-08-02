from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from work_retrieval_api.production import ProductionJobMetadataLookup, _salary_bounds
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
            "全職",
            "日班",
            "不拘",
            "需管理人數10人以下",
            "大學,碩士",
            "月薪‧40000‧60000",
            Decimal("40000.00"),
            Decimal("60000.00"),
        ),
    )
    lookup = ProductionJobMetadataLookup(reader, _taxonomy())

    result = lookup.get_many(("1",))

    assert result[0].source_modified_at == datetime(2026, 6, 8, tzinfo=UTC)
    assert result[0].location_codes == ("100100",)
    assert result[0].duty_codes == ("140200",)
    assert result[0].job_attribute == "全職"
    assert result[0].work_hours == "日班"
    assert result[0].experience_requirement == "不拘"
    assert result[0].management_count == "需管理人數10人以下"
    assert result[0].education_requirement == "大學,碩士"
    assert result[0].salary_period == "月薪"
    assert result[0].salary_min == 40_000
    assert result[0].salary_max == 60_000


def test_production_metadata_rejects_unexpected_aware_database_timestamp() -> None:
    reader = MagicMock(spec=SqlAlchemyJobReader)
    reader.metadata_for_job_ids.return_value = (
        JobMetadataRecord(
            "1",
            "台北市",
            "資訊系統",
            None,
            None,
            datetime.now(UTC),
            "全職",
            "日班",
            "不拘",
            "無\uff0c不接受管理職",
            "不拘",
            "月薪‧50000‧",
            Decimal("50000.00"),
            Decimal("0.00"),
        ),
    )
    lookup = ProductionJobMetadataLookup(reader, _taxonomy())

    with pytest.raises(RuntimeError, match="naive Taiwan"):
        lookup.get_many(("1",))


def test_production_metadata_quarantines_malformed_salary_bounds_as_a_pair() -> None:
    assert _salary_bounds(Decimal("40000.00"), Decimal("60000.50")) == (None, None)


def test_production_metadata_delegates_job_detail_lookup() -> None:
    reader = MagicMock(spec=SqlAlchemyJobReader)
    reader.job_details.return_value = {"職務名稱": "後端工程師"}
    lookup = ProductionJobMetadataLookup(reader, _taxonomy())

    assert lookup.job_details("1") == {"職務名稱": "後端工程師"}
    reader.job_details.assert_called_once_with("1")
