"""Projects record whether their namespace is a group or a user.

Policy resolution needs it to tell a personal project, whose ancestry ends at the instance, from
a project whose group is missing from the inventory. Rows discovered before this revision keep
a null kind, meaning unknown, until the next project discovery refreshes them; resolution treats
null like before, so an unknown kind is never reported as a complete ancestry.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-29 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("gitlab_project", sa.Column("namespace_kind", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_gitlab_project_namespace_kind_allowed"),
        "gitlab_project",
        "namespace_kind IN ('group', 'user')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_gitlab_project_namespace_kind_allowed"), "gitlab_project", type_="check"
    )
    op.drop_column("gitlab_project", "namespace_kind")
