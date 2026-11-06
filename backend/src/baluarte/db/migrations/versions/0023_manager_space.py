"""Manager space: the manager role, manager scopes, Baluarte tags and time tracking.

Adds ``manager`` to the roles a session may hold, ``time`` as a collection run kind and
``time_tracking`` as a setting. New tables:

- ``manager_scope``: the GitLab groups an administrator assigned to a manager;
- ``tag`` and ``tag_assignment``: key=value tags kept in Baluarte and the groups and projects
  they are assigned to;
- ``time_entry``, ``issue_estimate`` and ``time_cursor``: time logged in GitLab, issue estimates
  and how far each project's time has been read.

Triggers keep manager scopes and tag assignments append-only apart from ending them, let a tag
change only its description and colour, and reject ``DELETE`` of any of them.

``change_record`` gains ``review_outcome`` (the review check alone, for review rates), and
``evaluation`` an index for trends by run, outcome and severity.

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-07 10:00:00.000000
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUN_KINDS = ("capabilities", "groups", "projects", "security_policies", "changes", "labels")
SETTING_KEYS = (
    "oidc",
    "gitlab",
    "governance",
    "operations",
    "audit_shipping",
    "change_evidence",
    "label_write_token",
)
ROLES = ("admin", "analyst", "auditor")
NAME_FORMAT = "^[a-z0-9][a-z0-9._-]{0,62}$"

# Shared by manager scopes and tag assignments: only an open row may be ended, once.
END_ONLY_FUNCTION = """
CREATE FUNCTION baluarte_{table}_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION '{what} cannot be deleted; end them instead'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.ended_at IS NOT NULL OR NEW.ended_at IS NULL THEN
        RAISE EXCEPTION 'only an open {what_one} can be ended'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF ({columns}) IS DISTINCT FROM ({old_columns}) THEN
        RAISE EXCEPTION 'a {what_one} cannot be rewritten'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$
"""

TAG_FUNCTION = """
CREATE FUNCTION baluarte_tag_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'tags cannot be deleted' USING ERRCODE = 'restrict_violation';
    END IF;
    IF (NEW.id, NEW.instance_id, NEW.key, NEW.value, NEW.created_by, NEW.created_by_user_id,
        NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.instance_id, OLD.key, OLD.value, OLD.created_by, OLD.created_by_user_id,
        OLD.created_at) THEN
        RAISE EXCEPTION 'only a tag''s description and colour can change'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$
"""

SCOPE_COLUMNS = (
    "id",
    "instance_id",
    "user_id",
    "group_gitlab_id",
    "group_path",
    "assigned_by",
    "assigned_by_user_id",
    "assigned_at",
)
ASSIGNMENT_COLUMNS = (
    "id",
    "tag_id",
    "target_type",
    "target_gitlab_id",
    "target_path",
    "assigned_by",
    "assigned_by_user_id",
    "assigned_at",
)


def _end_only(table: str, what: str, what_one: str, columns: Sequence[str]) -> None:
    op.execute(
        END_ONLY_FUNCTION.format(
            table=table,
            what=what,
            what_one=what_one,
            columns=", ".join(f"NEW.{c}" for c in columns),
            old_columns=", ".join(f"OLD.{c}" for c in columns),
        )
    )
    op.execute(
        f"CREATE TRIGGER {table}_transition BEFORE UPDATE OR DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION baluarte_{table}_transition()"
    )


def _allowed(table: str, column: str, values: Sequence[str], validate: bool = True) -> None:
    name = op.f(f"ck_{table}_{column}_allowed")
    op.drop_constraint(name, table, type_="check")
    allowed = ", ".join(f"'{value}'" for value in values)
    # NOT VALID on downgrade: rows already recorded stay, and only new rows are checked.
    op.execute(
        f"ALTER TABLE {table} ADD CONSTRAINT {name} "
        f"CHECK ({column} IN ({allowed})){'' if validate else ' NOT VALID'}"
    )


def _roles(values: Sequence[str], validate: bool = True) -> None:
    name = op.f("ck_user_session_roles_allowed")
    op.drop_constraint(name, "user_session", type_="check")
    allowed = ", ".join(f"'{value}'" for value in values)
    op.execute(
        f"ALTER TABLE user_session ADD CONSTRAINT {name} "
        f"CHECK (cardinality(roles) > 0 AND roles <@ ARRAY[{allowed}]::text[])"
        f"{'' if validate else ' NOT VALID'}"
    )


def _ended_columns() -> list[sa.Column[Any]]:
    return [
        sa.Column("ended_by", sa.Text(), nullable=True),
        sa.Column("ended_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    ]


def _now(name: str) -> sa.Column[Any]:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    _roles((*ROLES, "manager"))
    _allowed("collection_run", "kind", (*RUN_KINDS, "time"))
    _allowed("app_setting", "key", (*SETTING_KEYS, "time_tracking"))

    op.create_table(
        "manager_scope",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("group_gitlab_id", sa.BigInteger(), nullable=False),
        sa.Column("group_path", sa.Text(), nullable=False),
        sa.Column("assigned_by", sa.Text(), nullable=False),
        sa.Column("assigned_by_user_id", sa.BigInteger(), nullable=True),
        _now("assigned_at"),
        *_ended_columns(),
        sa.CheckConstraint(
            "(ended_at IS NULL) = (ended_by IS NULL)", name=op.f("ck_manager_scope_ended_by_whom")
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f("fk_manager_scope_instance_id_gitlab_instance"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name=op.f("fk_manager_scope_user_id_app_user")
        ),
        sa.ForeignKeyConstraint(
            ["assigned_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_manager_scope_assigned_by_user_id_app_user"),
        ),
        sa.ForeignKeyConstraint(
            ["ended_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_manager_scope_ended_by_user_id_app_user"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_manager_scope")),
    )
    op.create_index(
        "uq_manager_scope_open",
        "manager_scope",
        ["user_id", "group_gitlab_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_index("ix_manager_scope_user", "manager_scope", ["user_id"])
    _end_only("manager_scope", "manager scopes", "manager scope", SCOPE_COLUMNS)

    op.create_table(
        "tag",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("color", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.CheckConstraint(f"key ~ '{NAME_FORMAT}'", name=op.f("ck_tag_key_format")),
        sa.CheckConstraint(f"value ~ '{NAME_FORMAT}'", name=op.f("ck_tag_value_format")),
        sa.CheckConstraint(
            "color IS NULL OR color ~ '^#[0-9A-F]{6}$'", name=op.f("ck_tag_color_format")
        ),
        sa.CheckConstraint(
            "description IS NULL OR char_length(description) <= 500",
            name=op.f("ck_tag_description_length"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"], ["gitlab_instance.id"], name=op.f("fk_tag_instance_id_gitlab_instance")
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_tag_created_by_user_id_app_user"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tag")),
        sa.UniqueConstraint(
            "instance_id", "key", "value", name=op.f("uq_tag_instance_id_key_value")
        ),
    )
    op.execute(TAG_FUNCTION)
    op.execute(
        "CREATE TRIGGER tag_transition BEFORE UPDATE OR DELETE ON tag "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_tag_transition()"
    )

    op.create_table(
        "tag_assignment",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("tag_id", sa.BigInteger(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_gitlab_id", sa.BigInteger(), nullable=False),
        sa.Column("target_path", sa.Text(), nullable=False),
        sa.Column("assigned_by", sa.Text(), nullable=False),
        sa.Column("assigned_by_user_id", sa.BigInteger(), nullable=True),
        _now("assigned_at"),
        *_ended_columns(),
        sa.CheckConstraint(
            "target_type IN ('group', 'project')",
            name=op.f("ck_tag_assignment_target_type_allowed"),
        ),
        sa.CheckConstraint(
            "(ended_at IS NULL) = (ended_by IS NULL)",
            name=op.f("ck_tag_assignment_ended_by_whom"),
        ),
        sa.ForeignKeyConstraint(["tag_id"], ["tag.id"], name=op.f("fk_tag_assignment_tag_id_tag")),
        sa.ForeignKeyConstraint(
            ["assigned_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_tag_assignment_assigned_by_user_id_app_user"),
        ),
        sa.ForeignKeyConstraint(
            ["ended_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_tag_assignment_ended_by_user_id_app_user"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tag_assignment")),
    )
    op.create_index(
        "uq_tag_assignment_open",
        "tag_assignment",
        ["tag_id", "target_type", "target_gitlab_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_index(
        "ix_tag_assignment_target", "tag_assignment", ["target_type", "target_gitlab_id"]
    )
    _end_only("tag_assignment", "tag assignments", "tag assignment", ASSIGNMENT_COLUMNS)

    op.create_table(
        "time_entry",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("timelog_id", sa.Text(), nullable=False),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("spent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("seconds", sa.Integer(), nullable=False),
        sa.Column("issue_iid", sa.BigInteger(), nullable=True),
        sa.Column("merge_request_iid", sa.BigInteger(), nullable=True),
        sa.Column(
            "labels",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("milestone", sa.Text(), nullable=True),
        sa.Column("summary_sha256", sa.Text(), nullable=True),
        sa.Column("observation_id", sa.BigInteger(), nullable=False),
        _now("created_at"),
        _now("updated_at"),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "summary_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_time_entry_summary_sha256_sha256")
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f("fk_time_entry_instance_id_gitlab_instance"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["gitlab_project.id"],
            name=op.f("fk_time_entry_project_id_gitlab_project"),
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["observation.id"],
            name=op.f("fk_time_entry_observation_id_observation"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_time_entry")),
        sa.UniqueConstraint(
            "instance_id", "timelog_id", name=op.f("uq_time_entry_instance_id_timelog_id")
        ),
    )
    op.create_index("ix_time_entry_instance_spent", "time_entry", ["instance_id", "spent_at"])
    op.create_index("ix_time_entry_project_spent", "time_entry", ["project_id", "spent_at"])
    op.create_index("ix_time_entry_user_spent", "time_entry", ["username", "spent_at"])
    op.create_index("ix_time_entry_observation", "time_entry", ["observation_id"])

    op.create_table(
        "issue_estimate",
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("issue_iid", sa.BigInteger(), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column("estimate_seconds", sa.Integer(), nullable=False),
        sa.Column("spent_seconds", sa.Integer(), nullable=False),
        sa.Column(
            "labels",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("milestone", sa.Text(), nullable=True),
        sa.Column("issue_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observation_id", sa.BigInteger(), nullable=False),
        _now("read_at"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["gitlab_project.id"],
            name=op.f("fk_issue_estimate_project_id_gitlab_project"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f("fk_issue_estimate_instance_id_gitlab_instance"),
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["observation.id"],
            name=op.f("fk_issue_estimate_observation_id_observation"),
        ),
        sa.PrimaryKeyConstraint("project_id", "issue_iid", name=op.f("pk_issue_estimate")),
    )
    op.create_index("ix_issue_estimate_instance", "issue_estimate", ["instance_id"])
    op.create_index("ix_issue_estimate_observation", "issue_estimate", ["observation_id"])

    op.create_table(
        "time_cursor",
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("history_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_spent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("issues_updated_after", sa.DateTime(timezone=True), nullable=True),
        _now("updated_at"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["gitlab_project.id"],
            name=op.f("fk_time_cursor_project_id_gitlab_project"),
        ),
        sa.PrimaryKeyConstraint("project_id", name=op.f("pk_time_cursor")),
    )

    op.add_column("change_record", sa.Column("review_outcome", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_change_record_review_outcome_allowed"),
        "change_record",
        "review_outcome IN ('pass', 'fail', 'unknown')",
    )
    op.create_index(
        "ix_evaluation_run_outcome",
        "evaluation",
        ["evaluation_run_id", "outcome", "severity", "project_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_evaluation_run_outcome", table_name="evaluation")
    op.drop_constraint(
        op.f("ck_change_record_review_outcome_allowed"), "change_record", type_="check"
    )
    op.drop_column("change_record", "review_outcome")
    op.drop_table("time_cursor")
    op.drop_table("issue_estimate")
    op.drop_table("time_entry")
    for table in ("tag_assignment", "tag", "manager_scope"):
        op.execute(f"DROP TRIGGER {table}_transition ON {table}")
        op.execute(f"DROP FUNCTION baluarte_{table}_transition()")
        op.drop_table(table)
    op.execute("DELETE FROM app_setting WHERE key = 'time_tracking'")
    _allowed("app_setting", "key", SETTING_KEYS)
    _allowed("collection_run", "kind", RUN_KINDS, validate=False)
    _roles(ROLES, validate=False)
