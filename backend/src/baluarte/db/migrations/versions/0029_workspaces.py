"""Workspaces, and GitLab instances as connections in a workspace (ADR-0013, roadmap M11).

``workspace`` is the isolation boundary. One workspace, ``default``, is created, and every
existing ``gitlab_instance`` row joins it. Nothing else is rewritten: observations, evaluations,
audit events and their digests are untouched.

``gitlab_instance`` becomes a connection: it gains ``workspace_id``, a ``provider`` (``gitlab``
today; ``github`` follows in M13), an optional display ``name`` and ``paused``.

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-21 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "slug ~ '^[a-z0-9][a-z0-9-]{0,62}$'", name=op.f("ck_workspace_slug_format")
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 200", name=op.f("ck_workspace_name_length")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace")),
        sa.UniqueConstraint("slug", name=op.f("uq_workspace_slug")),
    )
    op.execute("INSERT INTO workspace (slug, name) VALUES ('default', 'Default')")

    op.add_column("gitlab_instance", sa.Column("workspace_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "gitlab_instance",
        sa.Column("provider", sa.Text(), server_default="gitlab", nullable=False),
    )
    op.add_column("gitlab_instance", sa.Column("name", sa.Text(), nullable=True))
    op.add_column(
        "gitlab_instance",
        sa.Column("paused", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.execute(
        "UPDATE gitlab_instance SET workspace_id = (SELECT id FROM workspace WHERE slug = 'default')"
    )
    op.alter_column("gitlab_instance", "workspace_id", nullable=False)
    op.create_foreign_key(
        op.f("fk_gitlab_instance_workspace_id_workspace"),
        "gitlab_instance",
        "workspace",
        ["workspace_id"],
        ["id"],
    )
    op.create_index(op.f("ix_gitlab_instance_workspace_id"), "gitlab_instance", ["workspace_id"])
    op.create_check_constraint(
        op.f("ck_gitlab_instance_provider_allowed"),
        "gitlab_instance",
        "provider IN ('gitlab', 'github')",
    )
    op.create_check_constraint(
        op.f("ck_gitlab_instance_name_length"),
        "gitlab_instance",
        "name IS NULL OR char_length(name) BETWEEN 1 AND 200",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_gitlab_instance_name_length"), "gitlab_instance", type_="check")
    op.drop_constraint(
        op.f("ck_gitlab_instance_provider_allowed"), "gitlab_instance", type_="check"
    )
    op.drop_index(op.f("ix_gitlab_instance_workspace_id"), table_name="gitlab_instance")
    op.drop_constraint(
        op.f("fk_gitlab_instance_workspace_id_workspace"), "gitlab_instance", type_="foreignkey"
    )
    op.drop_column("gitlab_instance", "paused")
    op.drop_column("gitlab_instance", "name")
    op.drop_column("gitlab_instance", "provider")
    op.drop_column("gitlab_instance", "workspace_id")
    op.drop_table("workspace")
