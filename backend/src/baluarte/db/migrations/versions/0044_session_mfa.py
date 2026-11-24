"""MFA at sign-in: each session records how a second factor was proven, so the identity-mfa
control can check every live session (docs/SECURITY.md#multi-factor-authentication).

``provider``: the identity provider's ID token carried an amr value that proves MFA. NULL: no
second factor was proven (allowed only while MFA is not required).

Revision ID: 0044
Revises: 0043
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "user_session"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("mfa_method", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f(f"ck_{TABLE}_mfa_method_allowed"),
        TABLE,
        "mfa_method IS NULL OR mfa_method IN ('provider')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f(f"ck_{TABLE}_mfa_method_allowed"), TABLE, type_="check")
    op.drop_column(TABLE, "mfa_method")
