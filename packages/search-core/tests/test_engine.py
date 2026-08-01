from dataclasses import FrozenInstanceError
from datetime import date

import pytest
from work_retrieval_core import DEMO_SEARCH_DATE, SearchQuery


def test_search_query_is_immutable() -> None:
    query = SearchQuery("後端工程師", date(2026, 6, 8), ("100100",), ("140200",))

    with pytest.raises(FrozenInstanceError):
        query.text = "changed"  # type: ignore[misc]


def test_search_query_uses_the_demo_date_when_omitted() -> None:
    assert SearchQuery("後端工程師").search_date == DEMO_SEARCH_DATE
