"""Evidence captured for auditable issues (roadmap M12.2).

- ``issue_evidence``: the latest capture of each auditable issue, the observation holding it,
  its status and the sources that could not be read.
- ``captured_file``: attachments captured as evidence, stored once by SHA-256. A trigger rejects
  ``UPDATE`` and ``DELETE``.

Revision ID: 0034
Revises: 0033
Create Date: 2026-11-21 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE = """
CREATE FUNCTION baluarte_captured_file_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'captured files cannot be changed or deleted'
        USING ERRCODE = 'restrict_violation';
END;
$$
"""


def upgrade() -> None:
    op.create_table(
        "issue_evidence",
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("issue_iid", sa.BigInteger(), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("observation_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "gaps",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("captured_for", sa.DateTime(timezone=True)),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('complete', 'partial', 'failed')",
            name=op.f("ck_issue_evidence_status_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["gitlab_project.id"],
            name=op.f("fk_issue_evidence_project_id_gitlab_project"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f("fk_issue_evidence_instance_id_gitlab_instance"),
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["observation.id"],
            name=op.f("fk_issue_evidence_observation_id_observation"),
        ),
        sa.PrimaryKeyConstraint("project_id", "issue_iid", name=op.f("pk_issue_evidence")),
    )
    op.create_index("ix_issue_evidence_instance", "issue_evidence", ["instance_id"])
    op.create_index("ix_issue_evidence_observation", "issue_evidence", ["observation_id"])

    op.create_table(
        "captured_file",
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.Text()),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_captured_file_sha256_sha256")
        ),
        sa.CheckConstraint(
            "size_bytes = octet_length(content)", name=op.f("ck_captured_file_size_matches")
        ),
        sa.PrimaryKeyConstraint("sha256", name=op.f("pk_captured_file")),
    )
    op.execute(IMMUTABLE)
    op.execute(
        "CREATE TRIGGER captured_file_immutable BEFORE UPDATE OR DELETE ON captured_file "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_captured_file_immutable()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER captured_file_immutable ON captured_file")
    op.execute("DROP FUNCTION baluarte_captured_file_immutable()")
    op.drop_table("captured_file")
    op.drop_table("issue_evidence")
