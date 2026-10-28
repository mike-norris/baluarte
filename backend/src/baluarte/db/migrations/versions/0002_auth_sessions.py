"""Users, browser sessions and pending OIDC sign-ins.

Sessions and sign-in state store SHA-256 digests of their bearer values, never the values.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-28 12:38:52.000439
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_user",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_user")),
        sa.UniqueConstraint("issuer", "subject", name=op.f("uq_app_user_issuer_subject")),
    )
    op.create_table(
        "oidc_login",
        sa.Column("state_digest", sa.Text(), nullable=False),
        sa.Column("nonce", sa.Text(), nullable=False),
        sa.Column("code_verifier", sa.Text(), nullable=False),
        sa.Column("return_to", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_oidc_login_state_digest_sha256")
        ),
        sa.PrimaryKeyConstraint("state_digest", name=op.f("pk_oidc_login")),
    )
    op.create_index(op.f("ix_oidc_login_expires_at"), "oidc_login", ["expires_at"], unique=False)
    op.create_table(
        "user_session",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("token_digest", sa.Text(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("roles", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column(
            "created_at",
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
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "cardinality(roles) > 0 AND roles <@ ARRAY['admin', 'analyst', 'auditor']::text[]",
            name=op.f("ck_user_session_roles_allowed"),
        ),
        sa.CheckConstraint(
            "token_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_user_session_token_digest_sha256")
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_user_session_expires_after_created")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_user_session_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_session")),
        sa.UniqueConstraint("token_digest", name=op.f("uq_user_session_token_digest")),
    )
    op.create_index(op.f("ix_user_session_user_id"), "user_session", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_user_session_user_id"), table_name="user_session")
    op.drop_table("user_session")
    op.drop_index(op.f("ix_oidc_login_expires_at"), table_name="oidc_login")
    op.drop_table("oidc_login")
    op.drop_table("app_user")
