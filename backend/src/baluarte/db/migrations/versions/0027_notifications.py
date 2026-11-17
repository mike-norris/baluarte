"""Notifications: messages to email and a chat webhook, each sent once (FR-024).

``notification`` holds one row per event that produced a message. ``dedup_key`` is unique, so an
event (an evaluation run's new findings, a day's review reminder, a week's digest) produces at
most one notification however many workers notice it. The message is stored as sent, with the
SHA-256 of its subject and text.

``notification_delivery`` holds one row per notification and channel: ``pending`` until sent,
retried with backoff, then ``sent`` or ``failed``. Deliveries go with their notification when
retention deletes it.

``app_setting`` accepts the ``notifications`` key and ``app_secret`` the SMTP password and the
webhook URL.

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-17 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SETTING_KEYS = (
    "oidc",
    "gitlab",
    "governance",
    "operations",
    "audit_shipping",
    "change_evidence",
    "label_write_token",
    "time_tracking",
)
SECRET_NAMES = (
    "oidc_client_secret",
    "gitlab_token",
    "audit_shipping_es_api_key",
    "audit_shipping_es_password",
    "label_write_token",
)
NEW_SECRETS = ("notifications_smtp_password", "notifications_webhook_url")
EVENT_TYPES = (
    "new_findings",
    "collection_unhealthy",
    "collection_recovered",
    "review_waiting",
    "weekly_digest",
)


def _allowed(table: str, column: str, values: Sequence[str]) -> None:
    name = op.f(f"ck_{table}_{column}_allowed")
    op.drop_constraint(name, table, type_="check")
    allowed = ", ".join(f"'{value}'" for value in values)
    op.create_check_constraint(name, table, f"{column} IN ({allowed})")


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        "notification",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("dedup_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("body_digest", sa.Text(), nullable=False),
        sa.CheckConstraint(
            f"event_type IN ({_in(EVENT_TYPES)})", name=op.f("ck_notification_event_type_allowed")
        ),
        sa.CheckConstraint(
            "body_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_notification_body_digest_sha256")
        ),
        sa.CheckConstraint(
            "length(dedup_key) BETWEEN 1 AND 200", name=op.f("ck_notification_dedup_key_length")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification")),
        sa.UniqueConstraint("dedup_key", name=op.f("uq_notification_dedup_key")),
    )
    op.create_index(
        "ix_notification_event_type", "notification", ["event_type", "id"], unique=False
    )
    op.create_index("ix_notification_created", "notification", ["created_at"], unique=False)
    op.create_table(
        "notification_delivery",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("notification_id", sa.BigInteger(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "channel IN ('email', 'webhook')", name=op.f("ck_notification_delivery_channel_allowed")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed')",
            name=op.f("ck_notification_delivery_status_allowed"),
        ),
        sa.CheckConstraint(
            "attempts >= 0", name=op.f("ck_notification_delivery_attempts_nonnegative")
        ),
        sa.CheckConstraint(
            "(status = 'pending') = (next_attempt_at IS NOT NULL)",
            name=op.f("ck_notification_delivery_pending_has_next_attempt"),
        ),
        sa.CheckConstraint(
            "(status = 'sent') = (sent_at IS NOT NULL)",
            name=op.f("ck_notification_delivery_sent_has_time"),
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notification.id"],
            name=op.f("fk_notification_delivery_notification_id_notification"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_delivery")),
        sa.UniqueConstraint(
            "notification_id",
            "channel",
            name=op.f("uq_notification_delivery_notification_id_channel"),
        ),
    )
    op.create_index(
        "ix_notification_delivery_due",
        "notification_delivery",
        ["next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )

    _allowed("app_setting", "key", (*SETTING_KEYS, "notifications"))
    _allowed("app_secret", "name", (*SECRET_NAMES, *NEW_SECRETS))


def downgrade() -> None:
    op.execute(
        "DELETE FROM app_secret WHERE name IN ('notifications_smtp_password', "
        "'notifications_webhook_url')"
    )
    op.execute("DELETE FROM app_setting WHERE key = 'notifications'")
    _allowed("app_secret", "name", SECRET_NAMES)
    _allowed("app_setting", "key", SETTING_KEYS)

    op.drop_index("ix_notification_delivery_due", table_name="notification_delivery")
    op.drop_table("notification_delivery")
    op.drop_index("ix_notification_created", table_name="notification")
    op.drop_index("ix_notification_event_type", table_name="notification")
    op.drop_table("notification")
