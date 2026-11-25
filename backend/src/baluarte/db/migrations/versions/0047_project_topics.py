"""Project topics, collected so Baluarte tags can be synchronized to them (roadmap M14.5,
docs/CONFIGURATION_TEMPLATES.md#tags-and-topics).

``gitlab_project.topics`` holds the topics the last discovery read, sorted and without
duplicates; null until a discovery has read them.

Revision ID: 0047
Revises: 0046
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("gitlab_project", sa.Column("topics", postgresql.ARRAY(sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("gitlab_project", "topics")
