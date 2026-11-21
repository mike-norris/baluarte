"""Workspace ownership of governance records (ADR-0013, roadmap M11.2).

Every table that holds a workspace's decisions gains ``workspace_id``: baselines and their
assignments and changes, the control mapping's changes, monitoring exclusions, finding exceptions,
remediation, label and issue plans, label taxonomies, tags, manager scopes, exports and
notifications. Rows that carry a connection (``instance_id``) take that connection's workspace;
the rest take the default workspace.

Unique constraints that were global, or per connection, become per workspace: a baseline's name
and version, the one proposed version of a baseline, a label taxonomy's version and its one
proposed version, and a tag's key and value.

No observation, evaluation or audit event is rewritten.

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-21 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables that carry a connection: their workspace is the connection's.
WITH_CONNECTION = (
    "policy_assignment",
    "policy_change",
    "policy_exception",
    "scope_exclusion",
    "remediation_plan",
    "label_taxonomy",
    "label_plan",
    "issue_plan",
    "manager_scope",
    "tag",
)
# Tables without one: they join the default workspace.
WITHOUT_CONNECTION = ("policy", "control_mapping_change", "export_job", "notification")
TABLES = WITH_CONNECTION + WITHOUT_CONNECTION

DEFAULT = "(SELECT id FROM workspace WHERE slug = 'default')"


def upgrade() -> None:
    # Tables that reject UPDATE outside their lifecycle have triggers; back-filling a new column
    # must not trip them, so they are disabled for this migration's statements only.
    for table in TABLES:
        op.add_column(table, sa.Column("workspace_id", sa.BigInteger(), nullable=True))
        op.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
        if table in WITH_CONNECTION:
            op.execute(
                f"UPDATE {table} t SET workspace_id = i.workspace_id"  # noqa: S608
                " FROM gitlab_instance i WHERE i.id = t.instance_id"
            )
        op.execute(f"UPDATE {table} SET workspace_id = {DEFAULT} WHERE workspace_id IS NULL")  # noqa: S608
        op.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")
        op.alter_column(table, "workspace_id", nullable=False)
        op.create_foreign_key(
            op.f(f"fk_{table}_workspace_id_workspace"), table, "workspace", ["workspace_id"], ["id"]
        )
        op.create_index(op.f(f"ix_{table}_workspace_id"), table, ["workspace_id"])

    op.drop_constraint("uq_policy_name_version", "policy", type_="unique")
    op.create_unique_constraint(
        op.f("uq_policy_workspace_id_name_version"), "policy", ["workspace_id", "name", "version"]
    )
    op.drop_index("uq_policy_change_proposed_version", table_name="policy_change")
    op.create_index(
        "uq_policy_change_proposed_version",
        "policy_change",
        ["workspace_id", "policy_name"],
        unique=True,
        postgresql_where=sa.text("state = 'proposed' AND kind = 'version'"),
    )
    op.drop_constraint("uq_label_taxonomy_version", "label_taxonomy", type_="unique")
    op.create_unique_constraint(
        "uq_label_taxonomy_version", "label_taxonomy", ["workspace_id", "version"]
    )
    op.drop_index("uq_label_taxonomy_proposed", table_name="label_taxonomy")
    op.create_index(
        "uq_label_taxonomy_proposed",
        "label_taxonomy",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("state = 'proposed'"),
    )
    op.drop_constraint("uq_tag_instance_id_key_value", "tag", type_="unique")
    op.create_unique_constraint(
        op.f("uq_tag_workspace_id_key_value"), "tag", ["workspace_id", "key", "value"]
    )


def downgrade() -> None:
    op.drop_constraint(op.f("uq_tag_workspace_id_key_value"), "tag", type_="unique")
    op.create_unique_constraint(
        "uq_tag_instance_id_key_value", "tag", ["instance_id", "key", "value"]
    )
    op.drop_index("uq_label_taxonomy_proposed", table_name="label_taxonomy")
    op.create_index(
        "uq_label_taxonomy_proposed",
        "label_taxonomy",
        ["instance_id"],
        unique=True,
        postgresql_where=sa.text("state = 'proposed'"),
    )
    op.drop_constraint("uq_label_taxonomy_version", "label_taxonomy", type_="unique")
    op.create_unique_constraint(
        "uq_label_taxonomy_version", "label_taxonomy", ["instance_id", "version"]
    )
    op.drop_index("uq_policy_change_proposed_version", table_name="policy_change")
    op.create_index(
        "uq_policy_change_proposed_version",
        "policy_change",
        ["policy_name"],
        unique=True,
        postgresql_where=sa.text("state = 'proposed' AND kind = 'version'"),
    )
    op.drop_constraint(op.f("uq_policy_workspace_id_name_version"), "policy", type_="unique")
    op.create_unique_constraint("uq_policy_name_version", "policy", ["name", "version"])
    for table in reversed(TABLES):
        op.drop_index(op.f(f"ix_{table}_workspace_id"), table_name=table)
        op.drop_constraint(op.f(f"fk_{table}_workspace_id_workspace"), table, type_="foreignkey")
        op.drop_column(table, "workspace_id")
