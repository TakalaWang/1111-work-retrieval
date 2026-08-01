from work_retrieval_database import Base


def test_base_starts_without_domain_tables() -> None:
    assert Base.metadata.tables == {}
