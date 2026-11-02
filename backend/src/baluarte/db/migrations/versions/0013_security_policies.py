"""Discovered GitLab security policies.

Adds ``security_policies`` as a collection run kind and ``gitlab_security_policy``, the
catalogue of security policies built from each discovery run. The evidence itself is stored as
observations of each group and project, so no new evidence table is needed.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-31 10:00:00.000000
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _kinds(*kinds: str, validate: bool = True) -> None:
    op.drop_constraint(op.f("ck_collection_run_kind_allowed"), "collection_run", type_="check")
    allowed = ", ".join(f"'{kind}'" for kind in kinds)
    # NOT VALID on downgrade: runs already recorded stay (observations reference them), and
    # only new rows are checked.
    op.execute(
        "ALTER TABLE collection_run ADD CONSTRAINT ck_collection_run_kind_allowed "
        f"CHECK (kind IN ({allowed})){'' if validate else ' NOT VALID'}"
    )


def _created(name: str) -> sa.Column[datetime]:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    _kinds("capabilities", "groups", "projects", "security_policies")
    op.create_table(
        "gitlab_security_policy",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("policy_type", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("origin_kind", sa.Text(), nullable=False),
        sa.Column("origin_path", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("yaml", sa.Text(), nullable=True),
        sa.Column("yaml_digest", sa.Text(), nullable=True),
        sa.Column("gitlab_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "scope",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "linked_from",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("state", sa.Text(), server_default="active", nullable=False),
        _created("first_seen_at"),
        _created("last_seen_at"),
        sa.Column("last_collection_run_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "policy_type IN ('scan_execution', 'approval', 'pipeline_execution', "
            "'vulnerability_management')",
            name=op.f("ck_gitlab_security_policy_policy_type_allowed"),
        ),
        sa.CheckConstraint(
            "origin_kind IN ('policy_project', 'linked_source')",
            name=op.f("ck_gitlab_security_policy_origin_kind_allowed"),
        ),
        sa.CheckConstraint(
            "state IN ('active', 'missing')", name=op.f("ck_gitlab_security_policy_state_allowed")
        ),
        sa.CheckConstraint(
            "yaml_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_gitlab_security_policy_yaml_digest_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f("fk_gitlab_security_policy_instance_id_gitlab_instance"),
        ),
        sa.ForeignKeyConstraint(
            ["last_collection_run_id"],
            ["collection_run.id"],
            name=op.f("fk_gitlab_security_policy_last_collection_run_id_collection_run"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gitlab_security_policy")),
        sa.UniqueConstraint(
            "instance_id",
            "policy_type",
            "origin_path",
            "name",
            name="uq_gitlab_security_policy_key",
        ),
    )


def downgrade() -> None:
    op.drop_table("gitlab_security_policy")
    _kinds("capabilities", "groups", "projects", validate=False)
