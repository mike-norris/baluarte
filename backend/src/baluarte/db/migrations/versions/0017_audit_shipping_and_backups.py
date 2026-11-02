"""Audit shipping and backups.

``audit_shipment`` holds one cursor per audit shipping destination: the last audit event
delivered there and the outcome of the last attempt. A new destination gets a new row, so the
whole retained history ships to it.

``backup_run`` records each run of the ``baluarte-backup`` service, successful or not. Rows
cannot be updated; deleting is allowed so retention can expire them with other evidence. A
successful run names its file, size and SHA-256 and must be encrypted.

``app_setting`` accepts the ``audit_shipping`` key and ``app_secret`` its two credentials.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-02 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SETTING_KEYS = ("oidc", "gitlab", "governance", "operations")
SECRET_NAMES = ("oidc_client_secret", "gitlab_token")
NEW_SECRETS = ("audit_shipping_es_api_key", "audit_shipping_es_password")


def _allowed(table: str, column: str, values: Sequence[str]) -> None:
    name = op.f(f"ck_{table}_{column}_allowed")
    op.drop_constraint(name, table, type_="check")
    allowed = ", ".join(f"'{value}'" for value in values)
    op.create_check_constraint(name, table, f"{column} IN ({allowed})")


def upgrade() -> None:
    op.create_table(
        "audit_shipment",
        sa.Column("destination_fingerprint", sa.Text(), nullable=False),
        sa.Column("destination", sa.Text(), nullable=False),
        sa.Column("last_shipped_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("last_shipped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "destination_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_audit_shipment_destination_fingerprint_sha256"),
        ),
        sa.CheckConstraint(
            "last_shipped_id >= 0", name=op.f("ck_audit_shipment_last_shipped_id_nonnegative")
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name=op.f("ck_audit_shipment_consecutive_failures_nonnegative"),
        ),
        sa.PrimaryKeyConstraint("destination_fingerprint", name=op.f("pk_audit_shipment")),
    )
    op.create_table(
        "backup_run",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.Text(), nullable=True),
        sa.Column("encrypted", sa.Boolean(), nullable=False),
        sa.Column("recipient", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("host", sa.Text(), nullable=True),
        sa.Column("reported_by", sa.Text(), server_default=sa.text("CURRENT_USER"), nullable=False),
        sa.CheckConstraint(
            "status IN ('succeeded', 'failed')", name=op.f("ck_backup_run_status_allowed")
        ),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_backup_run_sha256_sha256")),
        sa.CheckConstraint(
            "finished_at >= started_at", name=op.f("ck_backup_run_finished_after_started")
        ),
        sa.CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0", name=op.f("ck_backup_run_size_nonnegative")
        ),
        sa.CheckConstraint(
            "status <> 'succeeded' OR (file_name IS NOT NULL AND size_bytes IS NOT NULL "
            "AND sha256 IS NOT NULL AND encrypted)",
            name=op.f("ck_backup_run_success_complete"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backup_run")),
    )
    op.create_index("ix_backup_run_finished", "backup_run", ["finished_at"], unique=False)
    op.execute(
        "CREATE TRIGGER backup_run_immutable BEFORE UPDATE ON backup_run "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_reject_modification()"
    )

    _allowed("app_setting", "key", (*SETTING_KEYS, "audit_shipping"))
    _allowed("app_secret", "name", (*SECRET_NAMES, *NEW_SECRETS))


def downgrade() -> None:
    op.execute(
        "DELETE FROM app_secret WHERE name IN ('audit_shipping_es_api_key', "
        "'audit_shipping_es_password')"
    )
    op.execute("DELETE FROM app_setting WHERE key = 'audit_shipping'")
    _allowed("app_secret", "name", SECRET_NAMES)
    _allowed("app_setting", "key", SETTING_KEYS)

    op.execute("DROP TRIGGER backup_run_immutable ON backup_run")
    op.drop_index("ix_backup_run_finished", table_name="backup_run")
    op.drop_table("backup_run")
    op.drop_table("audit_shipment")
