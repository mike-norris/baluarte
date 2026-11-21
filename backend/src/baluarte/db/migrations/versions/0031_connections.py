"""Connections with their own settings and token (ADR-0013, roadmap M11.3).

``gitlab_instance`` rows gain the settings a connection collects with: ``timeout_seconds``,
``sync_interval_minutes``, ``allow_private_network`` (whether its URL may resolve to a private
address) and ``removed_at`` (a removed connection is no longer collected; its data stays).

Connections added on the Connections page keep their token in ``app_secret`` as
``connection_token:<id>``, sealed like every other secret. The connection set in the deployment or
on the Settings page keeps using its existing settings and ``gitlab_token``.

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-21 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
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
# The secret names a connection token may use.
PER_CONNECTION = "name ~ '^connection_token:[1-9][0-9]*$'"


def upgrade() -> None:
    op.add_column(
        "gitlab_instance",
        sa.Column("timeout_seconds", sa.Float(), server_default=sa.text("15"), nullable=False),
    )
    op.add_column(
        "gitlab_instance",
        sa.Column(
            "sync_interval_minutes", sa.Integer(), server_default=sa.text("360"), nullable=False
        ),
    )
    op.add_column(
        "gitlab_instance",
        sa.Column(
            "allow_private_network",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "gitlab_instance", sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_gitlab_instance_timeout_range"),
        "gitlab_instance",
        "timeout_seconds BETWEEN 1 AND 120",
    )
    op.create_check_constraint(
        op.f("ck_gitlab_instance_interval_range"),
        "gitlab_instance",
        "sync_interval_minutes = 0 OR sync_interval_minutes BETWEEN 15 AND 10080",
    )
    op.drop_constraint(op.f("ck_app_secret_name_allowed"), "app_secret", type_="check")
    op.create_check_constraint(
        op.f("ck_app_secret_name_allowed"),
        "app_secret",
        f"name IN ({NAMED}) OR {PER_CONNECTION}",
    )


def downgrade() -> None:
    op.execute("DELETE FROM app_secret WHERE name LIKE 'connection\\_token:%'")
    op.drop_constraint(op.f("ck_app_secret_name_allowed"), "app_secret", type_="check")
    op.create_check_constraint(
        op.f("ck_app_secret_name_allowed"), "app_secret", f"name IN ({NAMED})"
    )
    op.drop_constraint(op.f("ck_gitlab_instance_interval_range"), "gitlab_instance", type_="check")
    op.drop_constraint(op.f("ck_gitlab_instance_timeout_range"), "gitlab_instance", type_="check")
    op.drop_column("gitlab_instance", "removed_at")
    op.drop_column("gitlab_instance", "allow_private_network")
    op.drop_column("gitlab_instance", "sync_interval_minutes")
    op.drop_column("gitlab_instance", "timeout_seconds")
