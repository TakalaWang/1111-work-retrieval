from dataclasses import FrozenInstanceError

import pytest
from work_retrieval_core import SearchQuery


def test_search_query_is_immutable() -> None:
    query = SearchQuery("後端工程師", ("100100",), ("140200",))

    with pytest.raises(FrozenInstanceError):
        query.text = "changed"  # type: ignore[misc]
