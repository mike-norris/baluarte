"""Change evidence: merge requests, direct pushes and deployments on each default branch.

Adds ``changes`` as a collection run kind, ``change_record`` (one row per change, pointing at the
observation that describes it now) and ``change_cursor`` (how far each project has been read).
``evaluation`` gains ``observation_ids`` and ``evidence_digest`` for results backed by many
observations, and ``app_setting`` allows ``change_evidence``.

A trigger lets an update of ``change_record`` change only the observation it points at and the
outcome and reasons derived from it, so the change itself is never rewritten; earlier versions
stay as observations. Retention may delete rows older than the evidence period.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-05 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUN_KINDS = ("capabilities", "groups", "projects", "security_policies")
SETTING_KEYS = ("oidc", "gitlab", "governance", "operations", "audit_shipping")

UPDATE_FUNCTION = """
CREATE FUNCTION baluarte_change_record_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.instance_id, NEW.project_id, NEW.kind, NEW.iid, NEW.deployment_id, NEW.sha,
        NEW.shas, NEW.changed_at, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.instance_id, OLD.project_id, OLD.kind, OLD.iid, OLD.deployment_id, OLD.sha,
        OLD.shas, OLD.changed_at, OLD.created_at) THEN
        RAISE EXCEPTION 'a recorded change cannot be rewritten; only its latest observation moves'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$
"""


def _allowed(table: str, column: str, values: Sequence[str], validate: bool = True) -> None:
    name = op.f(f"ck_{table}_{column}_allowed")
    op.drop_constraint(name, table, type_="check")
    allowed = ", ".join(f"'{value}'" for value in values)
    # NOT VALID on downgrade: rows already recorded stay, and only new rows are checked.
    op.execute(
        f"ALTER TABLE {table} ADD CONSTRAINT {name} "
        f"CHECK ({column} IN ({allowed})){'' if validate else ' NOT VALID'}"
    )


def upgrade() -> None:
    _allowed("collection_run", "kind", (*RUN_KINDS, "changes"))
    _allowed("app_setting", "key", (*SETTING_KEYS, "change_evidence"))
    op.create_table(
        "change_record",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("iid", sa.BigInteger(), nullable=True),
        sa.Column("deployment_id", sa.BigInteger(), nullable=True),
        sa.Column("sha", sa.Text(), nullable=False),
        sa.Column(
            "shas",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_id", sa.BigInteger(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column(
            "reasons",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("complete", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('merge_request', 'direct_push', 'deployment')",
            name=op.f("ck_change_record_kind_allowed"),
        ),
        sa.CheckConstraint(
            "outcome IN ('pass', 'fail', 'unknown')",
            name=op.f("ck_change_record_outcome_allowed"),
        ),
        sa.CheckConstraint(
            "(kind = 'merge_request') = (iid IS NOT NULL)",
            name=op.f("ck_change_record_iid_for_merge_requests"),
        ),
        sa.CheckConstraint(
            "(kind = 'deployment') = (deployment_id IS NOT NULL)",
            name=op.f("ck_change_record_deployment_id_for_deployments"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f("fk_change_record_instance_id_gitlab_instance"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["gitlab_project.id"],
            name=op.f("fk_change_record_project_id_gitlab_project"),
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["observation.id"],
            name=op.f("fk_change_record_observation_id_observation"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_change_record")),
    )
    op.create_index(
        "ix_change_record_project_changed", "change_record", ["project_id", "changed_at"]
    )
    op.create_index(
        "ix_change_record_instance_changed", "change_record", ["instance_id", "changed_at"]
    )
    op.create_index("ix_change_record_observation", "change_record", ["observation_id"])
    op.create_index(
        "uq_change_record_merge_request",
        "change_record",
        ["project_id", "iid"],
        unique=True,
        postgresql_where=sa.text("kind = 'merge_request'"),
    )
    op.create_index(
        "uq_change_record_direct_push",
        "change_record",
        ["project_id", "sha"],
        unique=True,
        postgresql_where=sa.text("kind = 'direct_push'"),
    )
    op.create_index(
        "uq_change_record_deployment",
        "change_record",
        ["project_id", "deployment_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'deployment'"),
    )
    op.execute(UPDATE_FUNCTION)
    op.execute(
        "CREATE TRIGGER change_record_update BEFORE UPDATE ON change_record "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_change_record_update()"
    )
    op.create_table(
        "change_cursor",
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("history_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_merged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_commit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_deployed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["gitlab_project.id"],
            name=op.f("fk_change_cursor_project_id_gitlab_project"),
        ),
        sa.PrimaryKeyConstraint("project_id", name=op.f("pk_change_cursor")),
    )
    op.add_column(
        "evaluation", sa.Column("observation_ids", postgresql.ARRAY(sa.BigInteger()), nullable=True)
    )
    op.add_column("evaluation", sa.Column("evidence_digest", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_evaluation_evidence_digest_sha256"),
        "evaluation",
        "evidence_digest ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        op.f("ck_evaluation_observation_ids_have_digest"),
        "evaluation",
        "(observation_ids IS NULL) = (evidence_digest IS NULL)",
    )
    op.create_index(
        "ix_evaluation_observation_ids",
        "evaluation",
        ["observation_ids"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_observation_project_source_ref",
        "observation",
        ["project_id", "source", "source_ref", "collected_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_observation_project_source_ref", table_name="observation")
    op.drop_index("ix_evaluation_observation_ids", table_name="evaluation")
    op.drop_constraint(
        op.f("ck_evaluation_observation_ids_have_digest"), "evaluation", type_="check"
    )
    op.drop_constraint(op.f("ck_evaluation_evidence_digest_sha256"), "evaluation", type_="check")
    op.drop_column("evaluation", "evidence_digest")
    op.drop_column("evaluation", "observation_ids")
    op.drop_table("change_cursor")
    op.execute("DROP TRIGGER change_record_update ON change_record")
    op.execute("DROP FUNCTION baluarte_change_record_update()")
    op.drop_table("change_record")
    op.execute("DELETE FROM app_setting WHERE key = 'change_evidence'")
    _allowed("app_setting", "key", SETTING_KEYS)
    _allowed("collection_run", "kind", RUN_KINDS, validate=False)
