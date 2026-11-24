"""Built-in TOTP, the second factor when the identity provider does not prove MFA
(docs/SECURITY.md#built-in-totp).

- ``user_totp``: one authenticator per user. The shared secret is sealed with the deployment's
  secret key (ADR-0008), bound to ``totp:<user id>``; ``last_step`` is the last accepted time
  step, so a code is never accepted twice.
- ``user_recovery_code``: one-time codes for a lost authenticator, stored as SHA-256 digests.
- ``user_session``: ``mfa_pending`` marks a session that signed in but has still to enter a
  code; it grants nothing until then. ``mfa_failures`` counts wrong codes. ``mfa_method`` gains
  ``totp`` and ``recovery``.

Revision ID: 0045
Revises: 0044
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SESSION = "user_session"
METHODS_BEFORE = "mfa_method IS NULL OR mfa_method IN ('provider')"
METHODS = "mfa_method IS NULL OR mfa_method IN ('provider', 'totp', 'recovery')"


def upgrade() -> None:
    op.create_table(
        "user_totp",
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("app_user.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("key_id", sa.Text(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("last_step", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("key_id ~ '^[0-9a-f]{16}$'", name="key_id_format"),
        sa.CheckConstraint("octet_length(nonce) = 12", name="nonce_length"),
        sa.CheckConstraint("octet_length(ciphertext) > 16", name="ciphertext_length"),
        sa.CheckConstraint("last_step IS NULL OR last_step >= 0", name="last_step_nonnegative"),
        sa.CheckConstraint(
            "last_step IS NULL OR confirmed_at IS NOT NULL", name="steps_after_confirmation"
        ),
    )
    op.create_table(
        "user_recovery_code",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("app_user.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="digest_sha256"),
        sa.UniqueConstraint("user_id", "digest"),
    )
    op.add_column(
        SESSION,
        sa.Column("mfa_pending", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        SESSION,
        sa.Column("mfa_failures", sa.SmallInteger(), server_default="0", nullable=False),
    )
    op.create_check_constraint(
        op.f(f"ck_{SESSION}_mfa_failures_nonnegative"), SESSION, "mfa_failures >= 0"
    )
    op.create_check_constraint(
        op.f(f"ck_{SESSION}_pending_without_method"),
        SESSION,
        "NOT mfa_pending OR mfa_method IS NULL",
    )
    op.drop_constraint(op.f(f"ck_{SESSION}_mfa_method_allowed"), SESSION, type_="check")
    op.create_check_constraint(op.f(f"ck_{SESSION}_mfa_method_allowed"), SESSION, METHODS)


def downgrade() -> None:
    # Pending sessions end; TOTP-completed ones keep going, recorded as without MFA.
    op.execute(
        "UPDATE user_session SET revoked_at = now() WHERE mfa_pending AND revoked_at IS NULL"
    )
    op.execute("UPDATE user_session SET mfa_method = NULL WHERE mfa_method IN ('totp', 'recovery')")
    op.drop_constraint(op.f(f"ck_{SESSION}_mfa_method_allowed"), SESSION, type_="check")
    op.create_check_constraint(op.f(f"ck_{SESSION}_mfa_method_allowed"), SESSION, METHODS_BEFORE)
    op.drop_constraint(op.f(f"ck_{SESSION}_pending_without_method"), SESSION, type_="check")
    op.drop_constraint(op.f(f"ck_{SESSION}_mfa_failures_nonnegative"), SESSION, type_="check")
    op.drop_column(SESSION, "mfa_failures")
    op.drop_column(SESSION, "mfa_pending")
    op.drop_table("user_recovery_code")
    op.drop_table("user_totp")
