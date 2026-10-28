"""Job lease integrity.

A job holds a lease exactly when it is running, never exceeds its attempt budget, and expired
leases are found through a partial index. Autogenerate does not detect CHECK constraints, so
this revision is written by hand.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-28 16:05:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        op.f("ck_collection_job_attempt_within_max"),
        "collection_job",
        "attempt <= max_attempts",
    )
    op.create_check_constraint(
        op.f("ck_collection_job_lease_iff_running"),
        "collection_job",
        "(state = 'running') = (lease_owner IS NOT NULL)"
        " AND (state = 'running') = (lease_until IS NOT NULL)",
    )
    op.create_index(
        op.f("ix_collection_job_lease"),
        "collection_job",
        ["lease_until"],
        unique=False,
        postgresql_where=sa.text("state = 'running'"),
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_collection_job_lease"), table_name="collection_job")
    op.drop_constraint(op.f("ck_collection_job_lease_iff_running"), "collection_job", type_="check")
    op.drop_constraint(
        op.f("ck_collection_job_attempt_within_max"), "collection_job", type_="check"
    )
