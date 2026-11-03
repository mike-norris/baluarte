"""Group two-factor authentication settings.

``gitlab_group`` records ``require_two_factor_authentication`` and ``two_factor_grace_period``
as group discovery reads them, for the two-factor rule. Both are null until a group is
discovered again, and when GitLab does not report them.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-03 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "gitlab_group", sa.Column("require_two_factor_authentication", sa.Boolean(), nullable=True)
    )
    op.add_column("gitlab_group", sa.Column("two_factor_grace_period", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("gitlab_group", "two_factor_grace_period")
    op.drop_column("gitlab_group", "require_two_factor_authentication")
