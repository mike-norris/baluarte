"""The auditable issue register: issues as read, selection rules and decisions (roadmap M12.1).

- ``register_issue``: each project issue as last read, any type and state, with GitLab's
  timestamps and the observation that recorded it.
- ``register_cursor``: how far each project's issues have been read.
- ``register_rule``: selection rules per workspace (labels, projects, issue types) with the
  category of what they select. Ended, never edited or deleted.
- ``register_decision``: explicit inclusion or exclusion of one issue, with a reason. Ended,
  never edited or deleted.
- ``collection_run.kind`` allows ``issues``, the register's collection.

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-21 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATEGORIES = "('engineering', 'security_finding', 'exception', 'remediation')"
RUN_KINDS = (
    "capabilities",
    "groups",
    "projects",
    "security_policies",
    "changes",
    "labels",
    "time",
)


def _run_kinds(kinds: Sequence[str], validate: bool) -> None:
    op.drop_constraint(op.f("ck_collection_run_kind_allowed"), "collection_run", type_="check")
    allowed = ", ".join(f"'{kind}'" for kind in kinds)
    # NOT VALID on downgrade: runs already recorded stay (observations reference them).
    op.execute(
        "ALTER TABLE collection_run ADD CONSTRAINT ck_collection_run_kind_allowed "
        f"CHECK (kind IN ({allowed})){'' if validate else ' NOT VALID'}"
    )


END_ONLY = """
CREATE FUNCTION baluarte_{table}_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION '{what} cannot be deleted' USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.ended_at IS NOT NULL THEN
        RAISE EXCEPTION 'an ended {one} cannot change' USING ERRCODE = 'restrict_violation';
    END IF;
    IF ({columns}) IS DISTINCT FROM ({old_columns}) THEN
        RAISE EXCEPTION 'a {one} can only be ended' USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$
"""

RULE_COLUMNS = (
    "id",
    "workspace_id",
    "name",
    "category",
    "labels",
    "project_ids",
    "issue_types",
    "created_by",
    "created_at",
)
DECISION_COLUMNS = (
    "id",
    "workspace_id",
    "project_id",
    "issue_iid",
    "decision",
    "category",
    "reason",
    "decided_by",
    "decided_at",
)


def _end_only(table: str, what: str, one: str, columns: Sequence[str]) -> None:
    op.execute(
        END_ONLY.format(
            table=table,
            what=what,
            one=one,
            columns=", ".join(f"NEW.{c}" for c in columns),
            old_columns=", ".join(f"OLD.{c}" for c in columns),
        )
    )
    op.execute(
        f"CREATE TRIGGER {table}_transition BEFORE UPDATE OR DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION baluarte_{table}_transition()"
    )


def _text_array(name: str) -> sa.Column[list[str]]:
    return sa.Column(
        name, postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False
    )


def upgrade() -> None:
    _run_kinds((*RUN_KINDS, "issues"), validate=True)
    op.create_table(
        "register_issue",
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("issue_iid", sa.BigInteger(), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("gitlab_id", sa.BigInteger()),
        sa.Column("issue_type", sa.Text()),
        sa.Column("title", sa.Text()),
        sa.Column("state", sa.Text()),
        _text_array("labels"),
        sa.Column("author", sa.Text()),
        _text_array("assignees"),
        sa.Column("milestone", sa.Text()),
        sa.Column("web_url", sa.Text()),
        sa.Column("confidential", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True)),
        sa.Column("source_updated_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("observation_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "read_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["gitlab_project.id"],
            name=op.f("fk_register_issue_project_id_gitlab_project"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f("fk_register_issue_instance_id_gitlab_instance"),
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["observation.id"],
            name=op.f("fk_register_issue_observation_id_observation"),
        ),
        sa.PrimaryKeyConstraint("project_id", "issue_iid", name=op.f("pk_register_issue")),
    )
    op.create_index("ix_register_issue_instance", "register_issue", ["instance_id"])
    op.create_index("ix_register_issue_observation", "register_issue", ["observation_id"])
    op.create_index(
        "ix_register_issue_labels", "register_issue", ["labels"], postgresql_using="gin"
    )

    op.create_table(
        "register_cursor",
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("read_through", sa.DateTime(timezone=True)),
        sa.Column(
            "read_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["gitlab_project.id"],
            name=op.f("fk_register_cursor_project_id_gitlab_project"),
        ),
        sa.PrimaryKeyConstraint("project_id", name=op.f("pk_register_cursor")),
    )

    op.create_table(
        "register_rule",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        _text_array("labels"),
        sa.Column(
            "project_ids",
            postgresql.ARRAY(sa.BigInteger()),
            server_default=sa.text("'{}'::bigint[]"),
            nullable=False,
        ),
        _text_array("issue_types"),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("ended_by", sa.Text()),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(f"category IN {CATEGORIES}", name=op.f("ck_register_rule_category")),
        sa.CheckConstraint(
            "cardinality(labels) + cardinality(project_ids) + cardinality(issue_types) > 0",
            name=op.f("ck_register_rule_has_criteria"),
        ),
        sa.CheckConstraint(
            "length(name) BETWEEN 1 AND 200", name=op.f("ck_register_rule_name_length")
        ),
        sa.CheckConstraint(
            "(ended_at IS NULL) = (ended_by IS NULL)",
            name=op.f("ck_register_rule_ended_together"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_register_rule_workspace_id_workspace"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_register_rule")),
    )
    op.create_index(
        "uq_register_rule_active_name",
        "register_rule",
        ["workspace_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    _end_only("register_rule", "selection rules", "selection rule", RULE_COLUMNS)

    op.create_table(
        "register_decision",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("issue_iid", sa.BigInteger(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("category", sa.Text()),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=False),
        sa.Column(
            "decided_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("ended_by", sa.Text()),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "decision IN ('include', 'exclude')", name=op.f("ck_register_decision_decision")
        ),
        sa.CheckConstraint(
            f"category IS NULL OR (decision = 'include' AND category IN {CATEGORIES})",
            name=op.f("ck_register_decision_category_for_inclusions"),
        ),
        sa.CheckConstraint(
            "length(reason) BETWEEN 20 AND 2000",
            name=op.f("ck_register_decision_reason_length"),
        ),
        sa.CheckConstraint(
            "(ended_at IS NULL) = (ended_by IS NULL)",
            name=op.f("ck_register_decision_ended_together"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_register_decision_workspace_id_workspace"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["gitlab_project.id"],
            name=op.f("fk_register_decision_project_id_gitlab_project"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_register_decision")),
    )
    op.create_index(
        "uq_register_decision_open",
        "register_decision",
        ["project_id", "issue_iid"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_index("ix_register_decision_workspace", "register_decision", ["workspace_id"])
    _end_only("register_decision", "register decisions", "register decision", DECISION_COLUMNS)


def downgrade() -> None:
    _run_kinds(RUN_KINDS, validate=False)
    for table in ("register_decision", "register_rule"):
        op.execute(f"DROP TRIGGER {table}_transition ON {table}")
        op.execute(f"DROP FUNCTION baluarte_{table}_transition()")
    op.drop_table("register_decision")
    op.drop_table("register_rule")
    op.drop_table("register_cursor")
    op.drop_table("register_issue")
