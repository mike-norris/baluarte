"""Release practices and where they apply (release governance RG.2, docs/RELEASE_GOVERNANCE.md).

- ``release_practice``: how projects are meant to reach production. ``preset`` is
  ``merge_request``, ``release_tag``, ``release_branch`` or ``custom``; ``settings`` holds its
  parameters (``baluarte.releases.practices``).
- ``release_practice_assignment``: one practice per connection, group or project; the nearest
  assignment on a project's path decides.

Revision ID: 0048
Revises: 0047
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRACTICE = "release_practice"
ASSIGNMENT = "release_practice_assignment"


def _now(name: str) -> sa.Column[object]:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        PRACTICE,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("preset", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        _now("created_at"),
        sa.Column("updated_by", sa.Text(), nullable=False),
        _now("updated_at"),
        sa.CheckConstraint(
            "preset IN ('merge_request', 'release_tag', 'release_branch', 'custom')",
            name=op.f(f"ck_{PRACTICE}_preset_allowed"),
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 63", name=op.f(f"ck_{PRACTICE}_name_length")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspace.id"], name=op.f(f"fk_{PRACTICE}_workspace_id_workspace")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{PRACTICE}")),
        sa.UniqueConstraint("workspace_id", "name", name=op.f(f"uq_{PRACTICE}_workspace_id_name")),
    )
    op.create_index(op.f(f"ix_{PRACTICE}_workspace_id"), PRACTICE, ["workspace_id"])
    op.create_table(
        ASSIGNMENT,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("practice_id", sa.BigInteger(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.BigInteger(), nullable=False),
        sa.Column("assigned_by", sa.Text(), nullable=False),
        _now("created_at"),
        sa.CheckConstraint(
            "target_type IN ('instance', 'group', 'project')",
            name=op.f(f"ck_{ASSIGNMENT}_target_type_allowed"),
        ),
        sa.CheckConstraint(
            "target_type <> 'instance' OR target_id = instance_id",
            name=op.f(f"ck_{ASSIGNMENT}_instance_target"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f(f"fk_{ASSIGNMENT}_workspace_id_workspace"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f(f"fk_{ASSIGNMENT}_instance_id_gitlab_instance"),
        ),
        sa.ForeignKeyConstraint(
            ["practice_id"],
            [f"{PRACTICE}.id"],
            name=op.f(f"fk_{ASSIGNMENT}_practice_id_{PRACTICE}"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{ASSIGNMENT}")),
        sa.UniqueConstraint(
            "instance_id",
            "target_type",
            "target_id",
            name=op.f(f"uq_{ASSIGNMENT}_instance_id_target_type_target_id"),
        ),
    )
    op.create_index(op.f(f"ix_{ASSIGNMENT}_workspace_id"), ASSIGNMENT, ["workspace_id"])
    op.create_index(op.f(f"ix_{ASSIGNMENT}_practice_id"), ASSIGNMENT, ["practice_id"])


def downgrade() -> None:
    op.drop_table(ASSIGNMENT)
    op.drop_table(PRACTICE)
