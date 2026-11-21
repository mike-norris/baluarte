"""Issue-to-control mappings and audit periods (roadmap M12.3).

- ``issue_control_mapping``: an auditable issue mapped to a framework control, with a rationale
  and the capture it rests on, reviewed like ``control_mapping_change``. A trigger allows only
  ``proposed`` -> ``active`` | ``rejected`` (recording the decision) and ``active`` ->
  ``withdrawn`` (recording who, when and why), changes nothing that was proposed, and rejects
  ``DELETE``.
- ``issue_control_evidence``: the captured items each mapping cites, each with its rationale.
  A trigger rejects ``UPDATE`` and ``DELETE``.
- ``audit_period``: named date ranges. A trigger allows only ending one.

Revision ID: 0035
Revises: 0034
Create Date: 2026-11-21 23:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROPOSED = (
    "id",
    "workspace_id",
    "project_id",
    "issue_iid",
    "framework",
    "control_id",
    "rationale",
    "observation_id",
    "supersedes_id",
    "review_required",
    "self_approved",
    "mapping_version",
    "proposed_by",
    "proposed_by_user_id",
    "proposed_at",
)
DECISION = (
    "decided_by",
    "decided_by_user_id",
    "decided_at",
    "decision_note",
    "development_decision",
)
WITHDRAWAL = ("withdrawn_by", "withdrawn_at", "withdraw_reason")


def _row(prefix: str, columns: Sequence[str]) -> str:
    return ", ".join(f"{prefix}.{column}" for column in columns)


MAPPING_TRANSITION = f"""
CREATE FUNCTION baluarte_issue_control_mapping_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'issue control mappings cannot be deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF ({_row("NEW", PROPOSED)}) IS DISTINCT FROM ({_row("OLD", PROPOSED)}) THEN
        RAISE EXCEPTION 'what an issue control mapping proposed cannot change'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.state = 'proposed' AND NEW.state IN ('active', 'rejected')
       AND ({_row("NEW", WITHDRAWAL)}) IS NOT DISTINCT FROM ({_row("OLD", WITHDRAWAL)}) THEN
        RETURN NEW;
    END IF;
    IF OLD.state = 'active' AND NEW.state = 'withdrawn'
       AND ({_row("NEW", DECISION)}) IS NOT DISTINCT FROM ({_row("OLD", DECISION)})
       AND OLD.withdrawn_at IS NULL THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'issue control mapping cannot change from % to %', OLD.state, NEW.state
        USING ERRCODE = 'restrict_violation';
END;
$$
"""

EVIDENCE_IMMUTABLE = """
CREATE FUNCTION baluarte_issue_control_evidence_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'cited evidence cannot be changed or deleted'
        USING ERRCODE = 'restrict_violation';
END;
$$
"""

PERIOD_COLUMNS = ("id", "workspace_id", "name", "starts_on", "ends_on", "created_by", "created_at")

PERIOD_TRANSITION = f"""
CREATE FUNCTION baluarte_audit_period_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'audit periods cannot be deleted' USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.ended_at IS NOT NULL THEN
        RAISE EXCEPTION 'an ended audit period cannot change' USING ERRCODE = 'restrict_violation';
    END IF;
    IF ({_row("NEW", PERIOD_COLUMNS)}) IS DISTINCT FROM ({_row("OLD", PERIOD_COLUMNS)}) THEN
        RAISE EXCEPTION 'an audit period can only be ended' USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$
"""


def upgrade() -> None:
    op.create_table(
        "issue_control_mapping",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("issue_iid", sa.BigInteger(), nullable=False),
        sa.Column("framework", sa.Text(), nullable=False),
        sa.Column("control_id", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("observation_id", sa.BigInteger(), nullable=False),
        sa.Column("supersedes_id", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("review_required", sa.Boolean(), nullable=False),
        sa.Column("self_approved", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "development_decision", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("mapping_version", sa.Text(), nullable=False),
        sa.Column("proposed_by", sa.Text(), nullable=False),
        sa.Column("proposed_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "proposed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("withdrawn_by", sa.Text(), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdraw_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "state IN ('proposed', 'active', 'rejected', 'withdrawn')",
            name=op.f("ck_issue_control_mapping_state_allowed"),
        ),
        sa.CheckConstraint(
            "framework IN ('soc2', 'iso27001', 'nist_800_53_moderate')",
            name=op.f("ck_issue_control_mapping_framework_allowed"),
        ),
        sa.CheckConstraint(
            "length(rationale) BETWEEN 20 AND 2000",
            name=op.f("ck_issue_control_mapping_rationale_length"),
        ),
        sa.CheckConstraint(
            "(state = 'withdrawn') = (withdrawn_at IS NOT NULL)",
            name=op.f("ck_issue_control_mapping_withdrawn_when_ended"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_issue_control_mapping_workspace_id_workspace"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["gitlab_project.id"],
            name=op.f("fk_issue_control_mapping_project_id_gitlab_project"),
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["observation.id"],
            name=op.f("fk_issue_control_mapping_observation_id_observation"),
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["issue_control_mapping.id"],
            name=op.f("fk_issue_control_mapping_supersedes_id_issue_control_mapping"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_issue_control_mapping")),
    )
    op.create_index(
        "uq_issue_control_mapping_open",
        "issue_control_mapping",
        ["workspace_id", "project_id", "issue_iid", "framework", "control_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('proposed', 'active') AND supersedes_id IS NULL"),
    )
    op.create_index(
        "uq_issue_control_mapping_revision",
        "issue_control_mapping",
        ["supersedes_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('proposed', 'active') AND supersedes_id IS NOT NULL"),
    )
    op.create_index(
        "ix_issue_control_mapping_issue", "issue_control_mapping", ["project_id", "issue_iid"]
    )
    op.create_index(
        "ix_issue_control_mapping_control", "issue_control_mapping", ["framework", "control_id"]
    )
    op.create_index(
        "ix_issue_control_mapping_workspace", "issue_control_mapping", ["workspace_id", "state"]
    )
    op.execute(MAPPING_TRANSITION)
    op.execute(
        "CREATE TRIGGER issue_control_mapping_transition BEFORE UPDATE OR DELETE "
        "ON issue_control_mapping FOR EACH ROW "
        "EXECUTE FUNCTION baluarte_issue_control_mapping_transition()"
    )

    op.create_table(
        "issue_control_evidence",
        sa.Column("mapping_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("item_key", sa.Text(), nullable=False),
        sa.Column("item_kind", sa.Text(), nullable=False),
        sa.Column("item_label", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "item_kind IN ('description', 'note', 'event', 'link', 'merge_request', 'pipeline', "
            "'release', 'attachment')",
            name=op.f("ck_issue_control_evidence_item_kind_allowed"),
        ),
        sa.CheckConstraint(
            "length(rationale) BETWEEN 10 AND 1000",
            name=op.f("ck_issue_control_evidence_rationale_length"),
        ),
        sa.ForeignKeyConstraint(
            ["mapping_id"],
            ["issue_control_mapping.id"],
            name=op.f("fk_issue_control_evidence_mapping_id_issue_control_mapping"),
        ),
        sa.PrimaryKeyConstraint("mapping_id", "position", name=op.f("pk_issue_control_evidence")),
        sa.UniqueConstraint("mapping_id", "item_key", name="uq_issue_control_evidence_item"),
    )
    op.execute(EVIDENCE_IMMUTABLE)
    op.execute(
        "CREATE TRIGGER issue_control_evidence_immutable BEFORE UPDATE OR DELETE "
        "ON issue_control_evidence FOR EACH ROW "
        "EXECUTE FUNCTION baluarte_issue_control_evidence_immutable()"
    )

    op.create_table(
        "audit_period",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("ended_by", sa.Text(), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("ends_on >= starts_on", name=op.f("ck_audit_period_ends_after_start")),
        sa.CheckConstraint(
            "length(name) BETWEEN 1 AND 120", name=op.f("ck_audit_period_name_length")
        ),
        sa.CheckConstraint(
            "(ended_at IS NULL) = (ended_by IS NULL)", name=op.f("ck_audit_period_ended_together")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspace.id"], name=op.f("fk_audit_period_workspace_id_workspace")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_period")),
    )
    op.create_index("ix_audit_period_workspace", "audit_period", ["workspace_id"])
    op.execute(PERIOD_TRANSITION)
    op.execute(
        "CREATE TRIGGER audit_period_transition BEFORE UPDATE OR DELETE ON audit_period "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_audit_period_transition()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER audit_period_transition ON audit_period")
    op.execute("DROP FUNCTION baluarte_audit_period_transition()")
    op.drop_table("audit_period")
    op.execute("DROP TRIGGER issue_control_evidence_immutable ON issue_control_evidence")
    op.execute("DROP FUNCTION baluarte_issue_control_evidence_immutable()")
    op.drop_table("issue_control_evidence")
    op.execute("DROP TRIGGER issue_control_mapping_transition ON issue_control_mapping")
    op.execute("DROP FUNCTION baluarte_issue_control_mapping_transition()")
    op.drop_table("issue_control_mapping")
