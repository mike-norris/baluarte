"""Off-site backup copies: each backup run records whether its file was copied to the off-site
S3-compatible target, where, and why not (docs/OPERATIONS.md#off-site-copies).

Revision ID: 0042
Revises: 0041
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "backup_run"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("offsite_status", sa.Text(), nullable=True))
    op.add_column(TABLE, sa.Column("offsite_location", sa.Text(), nullable=True))
    op.add_column(TABLE, sa.Column("offsite_error", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f(f"ck_{TABLE}_offsite_status_allowed"),
        TABLE,
        "offsite_status IS NULL OR offsite_status IN ('copied', 'failed')",
    )
    op.create_check_constraint(
        op.f(f"ck_{TABLE}_offsite_complete"),
        TABLE,
        "offsite_status IS NULL OR (offsite_location IS NOT NULL AND "
        "(offsite_status = 'copied') = (offsite_error IS NULL))",
    )


def downgrade() -> None:
    op.drop_constraint(op.f(f"ck_{TABLE}_offsite_complete"), TABLE, type_="check")
    op.drop_constraint(op.f(f"ck_{TABLE}_offsite_status_allowed"), TABLE, type_="check")
    for column in ("offsite_error", "offsite_location", "offsite_status"):
        op.drop_column(TABLE, column)
