from sqlalchemy import DateTime, Numeric, Text, UniqueConstraint
from work_retrieval_database import Job

SOURCE_COLUMNS = (
    "job_id",
    "title",
    "description",
    "salary_text",
    "salary_min",
    "salary_max",
    "duty_major",
    "duty_middle",
    "duty_minor",
    "job_attribute",
    "work_hours",
    "work_hours_description",
    "work_city",
    "education_requirement",
    "major_requirement_1",
    "major_requirement_2",
    "major_requirement_3",
    "experience_requirement",
    "language_1",
    "language_1_listening",
    "language_1_speaking",
    "language_1_reading",
    "language_1_writing",
    "language_2",
    "language_2_listening",
    "language_2_speaking",
    "language_2_reading",
    "language_2_writing",
    "computer_skills",
    "professional_certifications",
    "work_skills",
    "additional_conditions",
    "management_count",
    "requires_travel",
    "vendor_id",
    "industry_major",
    "industry_middle",
    "industry_minor",
    "source_modified_at",
)


def test_job_maps_the_complete_source_record() -> None:
    table = Job.__table__

    assert table.name == "jobs"
    assert tuple(table.columns.keys()) == (*SOURCE_COLUMNS, "source_row")
    assert table.c.job_id.primary_key
    assert table.c.source_row.nullable is False
    assert table.c.source_row.unique


def test_job_uses_lossless_source_types_and_nullability() -> None:
    table = Job.__table__

    assert isinstance(table.c.salary_min.type, Numeric)
    assert (table.c.salary_min.type.precision, table.c.salary_min.type.scale) == (12, 2)
    assert isinstance(table.c.salary_max.type, Numeric)
    assert (table.c.salary_max.type.precision, table.c.salary_max.type.scale) == (12, 2)
    assert isinstance(table.c.source_modified_at.type, DateTime)
    assert table.c.source_modified_at.type.timezone is False

    required = {"job_id", "title", "salary_text", "vendor_id", "source_modified_at"}
    assert {column.name for column in table.columns if not column.nullable} == {
        *required,
        "source_row",
    }
    text_columns = set(SOURCE_COLUMNS) - {
        "salary_min",
        "salary_max",
        "source_modified_at",
    }
    assert all(isinstance(table.c[name].type, Text) for name in text_columns)


def test_job_defines_no_speculative_indexes_or_constraints() -> None:
    table = Job.__table__

    unique_constraints = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert unique_constraints == {("source_row",)}
    assert table.indexes == set()
