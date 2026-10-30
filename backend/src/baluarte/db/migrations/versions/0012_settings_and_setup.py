"""Settings and secrets entered through the web UI, and first-run setup (BAL-034, ADR-0008).

``app_setting`` holds the identity provider and GitLab sections, ``app_secret`` the client secret
and GitLab token sealed with AES-256-GCM (never plaintext). ``setup_state`` is a single row with
the digest of the one-time setup code and when setup completed; ``setup_session`` holds digests
of the browser sessions that entered the code.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-30 14:00:00.000000
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created(name: str) -> sa.Column[datetime]:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "app_setting",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        _created("updated_at"),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.CheckConstraint("key IN ('oidc', 'gitlab')", name=op.f("ck_app_setting_key_allowed")),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_app_setting")),
    )
    op.create_table(
        "app_secret",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("key_id", sa.Text(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        _created("updated_at"),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "name IN ('oidc_client_secret', 'gitlab_token')",
            name=op.f("ck_app_secret_name_allowed"),
        ),
        sa.CheckConstraint("key_id ~ '^[0-9a-f]{16}$'", name=op.f("ck_app_secret_key_id_format")),
        sa.CheckConstraint("octet_length(nonce) = 12", name=op.f("ck_app_secret_nonce_length")),
        sa.CheckConstraint(
            "octet_length(ciphertext) > 16", name=op.f("ck_app_secret_ciphertext_length")
        ),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_app_secret")),
    )
    op.create_table(
        "setup_state",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("code_digest", sa.Text(), nullable=True),
        sa.Column("code_issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failures_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_by_user_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("id = 1", name=op.f("ck_setup_state_single_row")),
        sa.CheckConstraint(
            "code_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_setup_state_code_digest_sha256")
        ),
        sa.CheckConstraint(
            "failed_attempts >= 0", name=op.f("ck_setup_state_failed_attempts_nonnegative")
        ),
        sa.CheckConstraint(
            "(completed_at IS NULL) = (completed_by_user_id IS NULL)",
            name=op.f("ck_setup_state_completed_by_whom"),
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR code_digest IS NULL", name=op.f("ck_setup_state_code_cleared")
        ),
        sa.ForeignKeyConstraint(
            ["completed_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_setup_state_completed_by_user_id_app_user"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_setup_state")),
    )
    op.execute("INSERT INTO setup_state (id) VALUES (1)")
    op.create_table(
        "setup_session",
        sa.Column("token_digest", sa.Text(), nullable=False),
        _created("created_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "token_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_setup_session_token_digest_sha256")
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_setup_session_expires_after_created")
        ),
        sa.PrimaryKeyConstraint("token_digest", name=op.f("pk_setup_session")),
    )
    op.create_index(
        op.f("ix_setup_session_expires_at"), "setup_session", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_setup_session_expires_at"), table_name="setup_session")
    op.drop_table("setup_session")
    op.drop_table("setup_state")
    op.drop_table("app_secret")
    op.drop_table("app_setting")
