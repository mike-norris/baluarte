"""Timecards: a person's display name on time entries and the planning context of issues.

``time_entry`` gains ``user_name`` (the display name in GitLab). The new ``issue_context`` table
holds, for each issue with logged time, its work item type and title, parent, top-level
ancestor, milestone, iteration and estimate as last read.

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-13 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("time_entry", sa.Column("user_name", sa.Text(), nullable=True))
    op.create_table(
        "issue_context",
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("issue_iid", sa.BigInteger(), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("work_item_type", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("web_url", sa.Text(), nullable=True),
        sa.Column("parent_type", sa.Text(), nullable=True),
        sa.Column("parent_reference", sa.Text(), nullable=True),
        sa.Column("parent_title", sa.Text(), nullable=True),
        sa.Column("top_type", sa.Text(), nullable=True),
        sa.Column("top_reference", sa.Text(), nullable=True),
        sa.Column("top_title", sa.Text(), nullable=True),
        sa.Column("milestone", sa.Text(), nullable=True),
        sa.Column("iteration_status", sa.Text(), nullable=False),
        sa.Column("iteration", sa.Text(), nullable=True),
        sa.Column("estimate_seconds", sa.Integer(), nullable=True),
        sa.Column("observation_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "read_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["gitlab_project.id"],
            name=op.f("fk_issue_context_project_id_gitlab_project"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f("fk_issue_context_instance_id_gitlab_instance"),
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["observation.id"],
            name=op.f("fk_issue_context_observation_id_observation"),
        ),
        sa.PrimaryKeyConstraint("project_id", "issue_iid", name=op.f("pk_issue_context")),
    )
    op.create_index("ix_issue_context_instance", "issue_context", ["instance_id"])
    op.create_index("ix_issue_context_observation", "issue_context", ["observation_id"])


def downgrade() -> None:
    op.drop_index("ix_issue_context_observation", table_name="issue_context")
    op.drop_index("ix_issue_context_instance", table_name="issue_context")
    op.drop_table("issue_context")
    op.drop_column("time_entry", "user_name")
