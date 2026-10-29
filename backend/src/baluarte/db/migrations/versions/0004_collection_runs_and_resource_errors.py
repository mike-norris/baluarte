"""Collection runs and per-resource collection errors.

One collection_run row per job attempt records freshness, outcome counts and the safe error;
groups and projects gain the last error from confirming them (BAL-015).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-29 07:35:31.650485
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "collection_run",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("collection_job_id", sa.BigInteger(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), server_default="running", nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seen", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "counts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(status = 'running') = (finished_at IS NULL)",
            name=op.f("ck_collection_run_finished_unless_running"),
        ),
        sa.CheckConstraint(
            "kind IN ('capabilities', 'groups', 'projects')",
            name=op.f("ck_collection_run_kind_allowed"),
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'partial', 'failed')",
            name=op.f("ck_collection_run_status_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["collection_job_id"],
            ["collection_job.id"],
            name=op.f("fk_collection_run_collection_job_id_collection_job"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f("fk_collection_run_instance_id_gitlab_instance"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collection_run")),
    )
    op.create_index(
        "ix_collection_run_kind_started",
        "collection_run",
        ["instance_id", "kind", "started_at"],
        unique=False,
    )
    op.add_column("gitlab_group", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column(
        "gitlab_group", sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("gitlab_project", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column(
        "gitlab_project", sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("gitlab_project", "last_error_at")
    op.drop_column("gitlab_project", "last_error")
    op.drop_column("gitlab_group", "last_error_at")
    op.drop_column("gitlab_group", "last_error")
    op.drop_index("ix_collection_run_kind_started", table_name="collection_run")
    op.drop_table("collection_run")
