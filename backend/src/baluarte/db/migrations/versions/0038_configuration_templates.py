"""Configuration templates (roadmap M14.1, ADR-0016).

- ``configuration_template``: immutable template versions per workspace, like ``policy``.
- ``template_assignment``: one template version pinned to a connection, group or project, with
  optional overrides of its settings and the reason for them.

Revision ID: 0038
Revises: 0037
Create Date: 2026-11-24 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "configuration_template",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_yaml", sa.Text(), nullable=False),
        sa.Column("yaml_digest", sa.Text(), nullable=False),
        sa.Column("extends_id", sa.BigInteger(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_configuration_template_version_positive")),
        sa.CheckConstraint(
            "yaml_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_configuration_template_yaml_digest_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_configuration_template_workspace_id_workspace"),
        ),
        sa.ForeignKeyConstraint(
            ["extends_id"],
            ["configuration_template.id"],
            name=op.f("fk_configuration_template_extends_id_configuration_template"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_configuration_template")),
        sa.UniqueConstraint(
            "workspace_id",
            "name",
            "version",
            name=op.f("uq_configuration_template_workspace_id_name_version"),
        ),
    )
    op.create_index(
        op.f("ix_configuration_template_workspace_id"), "configuration_template", ["workspace_id"]
    )
    op.execute(
        "CREATE TRIGGER configuration_template_immutable BEFORE UPDATE OR DELETE "
        "ON configuration_template FOR EACH ROW EXECUTE FUNCTION baluarte_reject_modification()"
    )
    op.create_table(
        "template_assignment",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("template_id", sa.BigInteger(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.BigInteger(), nullable=False),
        sa.Column("overrides", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("override_reason", sa.Text(), nullable=True),
        sa.Column("assigned_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "target_type IN ('instance', 'group', 'project')",
            name=op.f("ck_template_assignment_target_type_allowed"),
        ),
        sa.CheckConstraint(
            "target_type <> 'instance' OR target_id = instance_id",
            name=op.f("ck_template_assignment_instance_target"),
        ),
        sa.CheckConstraint(
            "(overrides IS NULL) = (override_reason IS NULL) AND (override_reason IS NULL "
            "OR char_length(override_reason) BETWEEN 20 AND 2000)",
            name=op.f("ck_template_assignment_override_reason"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_template_assignment_workspace_id_workspace"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f("fk_template_assignment_instance_id_gitlab_instance"),
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["configuration_template.id"],
            name=op.f("fk_template_assignment_template_id_configuration_template"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_template_assignment")),
        sa.UniqueConstraint(
            "instance_id",
            "target_type",
            "target_id",
            name=op.f("uq_template_assignment_instance_id_target_type_target_id"),
        ),
    )
    op.create_index(
        op.f("ix_template_assignment_workspace_id"), "template_assignment", ["workspace_id"]
    )
    op.create_index(
        op.f("ix_template_assignment_template_id"), "template_assignment", ["template_id"]
    )


def downgrade() -> None:
    op.drop_table("template_assignment")
    op.execute("DROP TRIGGER configuration_template_immutable ON configuration_template")
    op.drop_table("configuration_template")
