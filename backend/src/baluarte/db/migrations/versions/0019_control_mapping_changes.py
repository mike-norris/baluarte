"""Organization changes to the control mapping.

``control_mapping_change`` records every change an organization makes to the default mapping of
baseline rule types to framework controls (``baluarte.controls.catalogue``): adding or removing a
rule type, scoping a control out, recording that it is met outside GitLab, and withdrawing one of
those. Each carries a justification. Review follows the ``governance`` setting, like baseline
changes.

A trigger allows only ``proposed`` -> ``active`` | ``rejected`` (recording the decision) and
``active`` -> ``withdrawn`` (nothing else changes), and rejects ``DELETE``.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-03 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRANSITION_FUNCTION = """
CREATE FUNCTION baluarte_control_mapping_change_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'control_mapping_change rows cannot be deleted'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (NEW.id, NEW.framework, NEW.control_id, NEW.kind, NEW.rule_type, NEW.justification,
        NEW.evidence_ref, NEW.review_by, NEW.supersedes_id, NEW.review_required,
        NEW.self_approved, NEW.mapping_version, NEW.proposed_by, NEW.proposed_by_user_id,
        NEW.proposed_at)
       IS DISTINCT FROM
       (OLD.id, OLD.framework, OLD.control_id, OLD.kind, OLD.rule_type, OLD.justification,
        OLD.evidence_ref, OLD.review_by, OLD.supersedes_id, OLD.review_required,
        OLD.self_approved, OLD.mapping_version, OLD.proposed_by, OLD.proposed_by_user_id,
        OLD.proposed_at) THEN
        RAISE EXCEPTION 'what a control mapping change proposed cannot change'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.state = 'proposed' AND NEW.state IN ('active', 'rejected') THEN
        RETURN NEW;
    END IF;
    IF OLD.state = 'active' AND NEW.state = 'withdrawn'
       AND (NEW.decided_by, NEW.decided_by_user_id, NEW.decided_at, NEW.decision_note)
           IS NOT DISTINCT FROM
           (OLD.decided_by, OLD.decided_by_user_id, OLD.decided_at, OLD.decision_note) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'control mapping change cannot change from % to %', OLD.state, NEW.state
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$
"""


def upgrade() -> None:
    op.create_table(
        "control_mapping_change",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("framework", sa.Text(), nullable=False),
        sa.Column("control_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("rule_type", sa.Text(), nullable=True),
        sa.Column("justification", sa.Text(), nullable=False),
        sa.Column("evidence_ref", sa.Text(), nullable=True),
        sa.Column("review_by", sa.Date(), nullable=True),
        sa.Column("supersedes_id", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("review_required", sa.Boolean(), nullable=False),
        sa.Column("self_approved", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("mapping_version", sa.Text(), nullable=False),
        sa.Column("proposed_by", sa.Text(), nullable=False),
        sa.Column("proposed_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "proposed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('map', 'unmap', 'not_applicable', 'manual', 'withdraw')",
            name=op.f("ck_control_mapping_change_kind_allowed"),
        ),
        sa.CheckConstraint(
            "state IN ('proposed', 'active', 'rejected', 'withdrawn')",
            name=op.f("ck_control_mapping_change_state_allowed"),
        ),
        sa.CheckConstraint(
            "framework IN ('soc2', 'iso27001', 'nist_800_53_moderate')",
            name=op.f("ck_control_mapping_change_framework_allowed"),
        ),
        sa.CheckConstraint(
            "length(justification) BETWEEN 20 AND 2000",
            name=op.f("ck_control_mapping_change_justification_length"),
        ),
        sa.CheckConstraint(
            "evidence_ref IS NULL OR length(evidence_ref) BETWEEN 1 AND 500",
            name=op.f("ck_control_mapping_change_evidence_ref_length"),
        ),
        sa.CheckConstraint(
            "(rule_type IS NOT NULL) = (kind IN ('map', 'unmap'))",
            name=op.f("ck_control_mapping_change_rule_type_for_pairs"),
        ),
        sa.CheckConstraint(
            "(review_by IS NOT NULL) = (kind IN ('not_applicable', 'manual'))",
            name=op.f("ck_control_mapping_change_review_by_for_status"),
        ),
        sa.CheckConstraint(
            "kind <> 'manual' OR evidence_ref IS NOT NULL",
            name=op.f("ck_control_mapping_change_manual_has_evidence"),
        ),
        sa.CheckConstraint(
            "kind <> 'withdraw' OR supersedes_id IS NOT NULL",
            name=op.f("ck_control_mapping_change_withdraw_names_change"),
        ),
        sa.CheckConstraint(
            "supersedes_id IS NULL OR kind IN ('withdraw', 'not_applicable', 'manual')",
            name=op.f("ck_control_mapping_change_supersedes_allowed"),
        ),
        sa.CheckConstraint(
            "(state = 'proposed') = (decided_at IS NULL)",
            name=op.f("ck_control_mapping_change_decided_unless_proposed"),
        ),
        sa.CheckConstraint(
            "(decided_at IS NULL) = (decided_by IS NULL) "
            "AND (decided_at IS NULL) = (decided_by_user_id IS NULL)",
            name=op.f("ck_control_mapping_change_decision_complete"),
        ),
        sa.CheckConstraint(
            "NOT review_required OR decided_by_user_id IS NULL "
            "OR decided_by_user_id <> proposed_by_user_id",
            name=op.f("ck_control_mapping_change_decided_by_another_user"),
        ),
        sa.CheckConstraint(
            "review_required OR state IN ('active', 'withdrawn')",
            name=op.f("ck_control_mapping_change_unreviewed_is_applied"),
        ),
        sa.CheckConstraint(
            "self_approved = NOT review_required",
            name=op.f("ck_control_mapping_change_self_approved_when_unreviewed"),
        ),
        sa.CheckConstraint(
            "NOT self_approved OR decided_by_user_id = proposed_by_user_id",
            name=op.f("ck_control_mapping_change_self_approved_by_proposer"),
        ),
        sa.CheckConstraint(
            "state <> 'rejected' OR length(decision_note) > 0",
            name=op.f("ck_control_mapping_change_rejection_explained"),
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["control_mapping_change.id"],
            name=op.f("fk_control_mapping_change_supersedes_id_control_mapping_change"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_control_mapping_change")),
    )
    op.create_index(
        "ix_control_mapping_change_control",
        "control_mapping_change",
        ["framework", "control_id", "id"],
        unique=False,
    )
    op.create_index(
        "uq_control_mapping_change_open",
        "control_mapping_change",
        ["framework", "control_id", "kind", sa.text("coalesce(rule_type, '')")],
        unique=True,
        postgresql_where=sa.text(
            "state IN ('proposed', 'active') AND kind <> 'withdraw' AND supersedes_id IS NULL"
        ),
    )
    op.create_index(
        "uq_control_mapping_change_withdraw",
        "control_mapping_change",
        ["supersedes_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('proposed', 'active') AND supersedes_id IS NOT NULL"),
    )
    op.execute(TRANSITION_FUNCTION)
    op.execute(
        "CREATE TRIGGER control_mapping_change_transition "
        "BEFORE UPDATE OR DELETE ON control_mapping_change "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_control_mapping_change_transition()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER control_mapping_change_transition ON control_mapping_change")
    op.execute("DROP FUNCTION baluarte_control_mapping_change_transition()")
    op.drop_index("uq_control_mapping_change_withdraw", table_name="control_mapping_change")
    op.drop_index("uq_control_mapping_change_open", table_name="control_mapping_change")
    op.drop_index("ix_control_mapping_change_control", table_name="control_mapping_change")
    op.drop_table("control_mapping_change")
