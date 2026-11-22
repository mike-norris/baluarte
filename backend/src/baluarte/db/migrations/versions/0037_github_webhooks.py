"""Signed GitHub webhooks (roadmap M13.4b).

- ``app_secret`` may hold ``connection_webhook_secret:<id>``: a connection's webhook secret,
  sealed like its token.
- ``webhook_delivery``: each delivery a connection accepted, once per GitHub delivery ID. It keeps
  the event, its action, the repository it concerns and the SHA-256 of the body, never the body.

Revision ID: 0037
Revises: 0036
Create Date: 2026-11-21 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SECRET_NAMES = (
    "oidc_client_secret",
    "gitlab_token",
    "audit_shipping_es_api_key",
    "audit_shipping_es_password",
    "label_write_token",
    "notifications_smtp_password",
    "notifications_webhook_url",
    "remediation_write_token",
)
NAMED = ", ".join(f"'{name}'" for name in SECRET_NAMES)
PER_CONNECTION = "name ~ '^connection_token:[1-9][0-9]*$'"
PER_CONNECTION_WEBHOOK = "name ~ '^connection_webhook_secret:[1-9][0-9]*$'"
STATUSES = ("queued", "processed", "ignored", "failed")


def upgrade() -> None:
    op.drop_constraint(op.f("ck_app_secret_name_allowed"), "app_secret", type_="check")
    op.create_check_constraint(
        op.f("ck_app_secret_name_allowed"),
        "app_secret",
        f"name IN ({NAMED}) OR {PER_CONNECTION} OR {PER_CONNECTION_WEBHOOK}",
    )
    op.create_table(
        "webhook_delivery",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("delivery_id", sa.Text(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=True),
        sa.Column("repository_id", sa.BigInteger(), nullable=True),
        sa.Column("body_sha256", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f("fk_webhook_delivery_instance_id_gitlab_instance"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_delivery")),
        sa.UniqueConstraint(
            "instance_id", "delivery_id", name=op.f("uq_webhook_delivery_instance_id_delivery_id")
        ),
        sa.CheckConstraint(
            "status IN ({})".format(", ".join(f"'{s}'" for s in STATUSES)),
            name=op.f("ck_webhook_delivery_status_allowed"),
        ),
        sa.CheckConstraint(
            "char_length(delivery_id) BETWEEN 1 AND 100",
            name=op.f("ck_webhook_delivery_delivery_id_length"),
        ),
        sa.CheckConstraint(
            "body_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_webhook_delivery_body_sha256_sha256")
        ),
    )
    op.create_index(
        op.f("ix_webhook_delivery_pending"),
        "webhook_delivery",
        ["instance_id", "repository_id", "received_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_webhook_delivery_pending"), table_name="webhook_delivery")
    op.drop_table("webhook_delivery")
    op.execute("DELETE FROM app_secret WHERE name LIKE 'connection\\_webhook\\_secret:%'")
    op.drop_constraint(op.f("ck_app_secret_name_allowed"), "app_secret", type_="check")
    op.create_check_constraint(
        op.f("ck_app_secret_name_allowed"),
        "app_secret",
        f"name IN ({NAMED}) OR {PER_CONNECTION}",
    )
