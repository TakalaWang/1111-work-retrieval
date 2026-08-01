from sqlalchemy import DateTime, Text
from sqlalchemy.dialects.postgresql import JSONB
from work_retrieval_database import Base, Job


def test_job_model_matches_the_persisted_schema() -> None:
    table = Base.metadata.tables[Job.__tablename__]

    assert list(table.columns) == [
        table.c.job_id,
        table.c.details,
        table.c.created_at,
        table.c.updated_at,
    ]
    assert isinstance(table.c.job_id.type, Text)
    assert table.c.job_id.primary_key
    assert isinstance(table.c.details.type, JSONB)
    assert not table.c.details.nullable
    for name in ("created_at", "updated_at"):
        column = table.c[name]
        assert isinstance(column.type, DateTime)
        assert column.type.timezone
        assert not column.nullable
        assert column.server_default is not None
