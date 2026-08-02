"""Create the authoritative job snapshot table."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_create_jobs"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("salary_text", sa.Text(), nullable=False),
        sa.Column("salary_min", sa.Numeric(12, 2), nullable=True),
        sa.Column("salary_max", sa.Numeric(12, 2), nullable=True),
        sa.Column("duty_major", sa.Text(), nullable=True),
        sa.Column("duty_middle", sa.Text(), nullable=True),
        sa.Column("duty_minor", sa.Text(), nullable=True),
        sa.Column("job_attribute", sa.Text(), nullable=True),
        sa.Column("work_hours", sa.Text(), nullable=True),
        sa.Column("work_hours_description", sa.Text(), nullable=True),
        sa.Column("work_city", sa.Text(), nullable=True),
        sa.Column("education_requirement", sa.Text(), nullable=True),
        sa.Column("major_requirement_1", sa.Text(), nullable=True),
        sa.Column("major_requirement_2", sa.Text(), nullable=True),
        sa.Column("major_requirement_3", sa.Text(), nullable=True),
        sa.Column("experience_requirement", sa.Text(), nullable=True),
        sa.Column("language_1", sa.Text(), nullable=True),
        sa.Column("language_1_listening", sa.Text(), nullable=True),
        sa.Column("language_1_speaking", sa.Text(), nullable=True),
        sa.Column("language_1_reading", sa.Text(), nullable=True),
        sa.Column("language_1_writing", sa.Text(), nullable=True),
        sa.Column("language_2", sa.Text(), nullable=True),
        sa.Column("language_2_listening", sa.Text(), nullable=True),
        sa.Column("language_2_speaking", sa.Text(), nullable=True),
        sa.Column("language_2_reading", sa.Text(), nullable=True),
        sa.Column("language_2_writing", sa.Text(), nullable=True),
        sa.Column("computer_skills", sa.Text(), nullable=True),
        sa.Column("professional_certifications", sa.Text(), nullable=True),
        sa.Column("work_skills", sa.Text(), nullable=True),
        sa.Column("additional_conditions", sa.Text(), nullable=True),
        sa.Column("management_count", sa.Text(), nullable=True),
        sa.Column("requires_travel", sa.Text(), nullable=True),
        sa.Column("vendor_id", sa.Text(), nullable=False),
        sa.Column("industry_major", sa.Text(), nullable=True),
        sa.Column("industry_middle", sa.Text(), nullable=True),
        sa.Column("industry_minor", sa.Text(), nullable=True),
        sa.Column("source_modified_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("source_row", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("job_id", name="pk_jobs"),
        sa.UniqueConstraint("source_row", name="uq_jobs_source_row"),
    )


def downgrade() -> None:
    op.drop_table("jobs")
