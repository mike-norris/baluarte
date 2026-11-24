"""Restore runs: each restore test the backup service reports, so the restore-test control is
a check (docs/OPERATIONS.md#restore-tests).

A run fetches one backup (off site when it can), checks its SHA-256 against backup_run,
decrypts it into a temporary PostgreSQL inside the backup container and checks what it
restored. Rows cannot be updated, like backup_run's. backup_run_id is not a foreign key:
retention removes old backup runs while their restore runs stay as evidence.

Revision ID: 0043
Revises: 0042
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "restore_run"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("backup_run_id", sa.BigInteger(), nullable=True),
        sa.Column("file_name", sa.Text(), nullable=True),
        sa.Column("sha256", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("schema_revision", sa.Text(), nullable=True),
        sa.Column("table_count", sa.Integer(), nullable=True),
        sa.Column("audit_events", sa.BigInteger(), nullable=True),
        sa.Column("digest_mismatches", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("host", sa.Text(), nullable=True),
        sa.Column("reported_by", sa.Text(), server_default=sa.text("CURRENT_USER"), nullable=False),
        sa.CheckConstraint(
            "status IN ('succeeded', 'failed')", name=op.f("ck_restore_run_status_allowed")
        ),
        sa.CheckConstraint(
            "source IS NULL OR source IN ('offsite', 'local')",
            name=op.f("ck_restore_run_source_allowed"),
        ),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_restore_run_sha256_sha256")),
        sa.CheckConstraint(
            "finished_at >= started_at", name=op.f("ck_restore_run_finished_after_started")
        ),
        sa.CheckConstraint(
            "coalesce(table_count, 0) >= 0 AND coalesce(audit_events, 0) >= 0 "
            "AND coalesce(digest_mismatches, 0) >= 0",
            name=op.f("ck_restore_run_counts_nonnegative"),
        ),
        sa.CheckConstraint(
            "status <> 'succeeded' OR (file_name IS NOT NULL AND sha256 IS NOT NULL "
            "AND source IS NOT NULL AND schema_revision IS NOT NULL AND table_count > 0 "
            "AND audit_events IS NOT NULL AND digest_mismatches = 0 AND error IS NULL)",
            name=op.f("ck_restore_run_success_complete"),
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR error IS NOT NULL", name=op.f("ck_restore_run_failure_explained")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_restore_run")),
    )
    op.create_index("ix_restore_run_finished", TABLE, ["finished_at"], unique=False)
    op.execute(
        f"CREATE TRIGGER {TABLE}_immutable BEFORE UPDATE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_reject_modification()"
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER {TABLE}_immutable ON {TABLE}")
    op.drop_index("ix_restore_run_finished", table_name=TABLE)
    op.drop_table(TABLE)
