"""GitHub connections, teams and memberships (roadmap M13.2).

- ``gitlab_instance`` (the connection table, named for its first provider) gains ``auth_kind``:
  ``token`` for either provider or, for GitHub only, ``app`` with the App's ``app_id`` and
  ``installation_id``. A GitHub connection's URL is its organization's
  (``https://github.com/<org>``), so URLs stay unique per connection.
- ``provider_team``: a team in a connection's organization, keyed by its provider ID.
- ``provider_membership``: who belongs to the organization or a team, and their role.

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-22 01:00:00.000000
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _seen() -> list[sa.Column[Any]]:
    return [
        sa.Column("state", sa.Text(), server_default="active", nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.add_column(
        "gitlab_instance",
        sa.Column("auth_kind", sa.Text(), server_default="token", nullable=False),
    )
    op.add_column("gitlab_instance", sa.Column("app_id", sa.BigInteger(), nullable=True))
    op.add_column("gitlab_instance", sa.Column("installation_id", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        op.f("ck_gitlab_instance_auth_kind_allowed"),
        "gitlab_instance",
        "auth_kind IN ('token', 'app')",
    )
    op.create_check_constraint(
        op.f("ck_gitlab_instance_app_for_github"),
        "gitlab_instance",
        "auth_kind = 'token' OR provider = 'github'",
    )
    op.create_check_constraint(
        op.f("ck_gitlab_instance_app_identified"),
        "gitlab_instance",
        "(auth_kind = 'app') = (app_id IS NOT NULL AND installation_id IS NOT NULL)",
    )
    op.create_table(
        "provider_team",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("provider_id", sa.BigInteger(), nullable=False),
        sa.Column("parent_provider_id", sa.BigInteger(), nullable=True),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("privacy", sa.Text(), nullable=True),
        *_seen(),
        sa.CheckConstraint(
            "state IN ('active', 'missing')", name=op.f("ck_provider_team_state_allowed")
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f("fk_provider_team_instance_id_gitlab_instance"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_team")),
        sa.UniqueConstraint(
            "instance_id", "provider_id", name=op.f("uq_provider_team_instance_id_provider_id")
        ),
    )
    op.create_table(
        "provider_membership",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("subject_kind", sa.Text(), nullable=False),
        sa.Column("subject_provider_id", sa.BigInteger(), nullable=False),
        sa.Column("user_provider_id", sa.BigInteger(), nullable=False),
        sa.Column("login", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        *_seen(),
        sa.CheckConstraint(
            "state IN ('active', 'missing')", name=op.f("ck_provider_membership_state_allowed")
        ),
        sa.CheckConstraint(
            "subject_kind IN ('organization', 'team')",
            name=op.f("ck_provider_membership_subject_kind_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f("fk_provider_membership_instance_id_gitlab_instance"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_membership")),
        sa.UniqueConstraint(
            "instance_id",
            "subject_kind",
            "subject_provider_id",
            "user_provider_id",
            name=op.f("uq_provider_membership_member"),
        ),
    )
    op.create_index("ix_provider_membership_login", "provider_membership", ["instance_id", "login"])


def downgrade() -> None:
    op.drop_index("ix_provider_membership_login", table_name="provider_membership")
    op.drop_table("provider_membership")
    op.drop_table("provider_team")
    for name in ("app_identified", "app_for_github", "auth_kind_allowed"):
        op.drop_constraint(op.f(f"ck_gitlab_instance_{name}"), "gitlab_instance", type_="check")
    op.drop_column("gitlab_instance", "installation_id")
    op.drop_column("gitlab_instance", "app_id")
    op.drop_column("gitlab_instance", "auth_kind")
