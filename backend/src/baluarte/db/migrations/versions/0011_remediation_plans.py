"""Dry-run remediation plans and their approvals (BAL-031).

``remediation_plan`` holds each plan document, its SHA-256 digest and its approval. A trigger
allows only the documented transitions (draft -> submitted -> approved | rejected, and draft,
submitted or approved -> expired once ``expires_at`` has passed), keeps the plan and who made it
unchanged and rejects ``DELETE``. CHECK constraints bound the expiry and the number of steps,
require the approver to be someone other than the creator, and bind an approval to the plan's
digest. Nothing in this revision lets Baluarte change GitLab.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-30 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MAX_PLAN_HOURS = 72
MAX_PLAN_STEPS = 100

TRANSITION_FUNCTION = """
CREATE FUNCTION baluarte_remediation_plan_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'remediation_plan rows cannot be deleted'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (NEW.id, NEW.instance_id, NEW.created_by_user_id, NEW.created_at, NEW.expires_at,
        NEW.plan, NEW.plan_digest, NEW.steps, NEW.skipped)
       IS DISTINCT FROM
       (OLD.id, OLD.instance_id, OLD.created_by_user_id, OLD.created_at, OLD.expires_at,
        OLD.plan, OLD.plan_digest, OLD.steps, OLD.skipped) THEN
        RAISE EXCEPTION 'a remediation plan cannot change'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.state = 'draft' AND NEW.state = 'submitted' THEN
        IF OLD.expires_at <= now() OR NEW.decided_at IS NOT NULL THEN
            RAISE EXCEPTION 'invalid remediation plan submission'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state = 'submitted' AND NEW.state IN ('approved', 'rejected') THEN
        IF NEW.submitted_at IS DISTINCT FROM OLD.submitted_at OR OLD.decided_at IS NOT NULL THEN
            RAISE EXCEPTION 'invalid remediation plan decision'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.state = 'approved' AND OLD.expires_at <= now() THEN
            RAISE EXCEPTION 'an expired remediation plan cannot be approved'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state IN ('draft', 'submitted', 'approved') AND NEW.state = 'expired' THEN
        IF (NEW.submitted_at, NEW.decided_by_user_id, NEW.decided_at, NEW.decision_note,
            NEW.approved_digest)
           IS DISTINCT FROM
           (OLD.submitted_at, OLD.decided_by_user_id, OLD.decided_at, OLD.decision_note,
            OLD.approved_digest) THEN
            RAISE EXCEPTION 'a remediation plan decision cannot change'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF OLD.expires_at > now() THEN
            RAISE EXCEPTION 'a remediation plan cannot expire before expires_at'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'remediation plan cannot change from % to %', OLD.state, NEW.state
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$
"""


def upgrade() -> None:
    op.create_table(
        "remediation_plan",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), server_default="draft", nullable=False),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("plan_digest", sa.Text(), nullable=False),
        sa.Column("steps", sa.Integer(), nullable=False),
        sa.Column("skipped", sa.Integer(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("approved_digest", sa.Text(), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('draft', 'submitted', 'approved', 'rejected', 'expired')",
            name=op.f("ck_remediation_plan_state_allowed"),
        ),
        sa.CheckConstraint(
            "plan_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_remediation_plan_plan_digest_sha256")
        ),
        sa.CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + "
            f"interval '{MAX_PLAN_HOURS} hours'",
            name=op.f("ck_remediation_plan_expiry_bounded"),
        ),
        sa.CheckConstraint(
            f"steps BETWEEN 1 AND {MAX_PLAN_STEPS}", name=op.f("ck_remediation_plan_steps_bounded")
        ),
        sa.CheckConstraint("skipped >= 0", name=op.f("ck_remediation_plan_skipped_non_negative")),
        sa.CheckConstraint(
            "decided_by_user_id IS NULL OR decided_by_user_id <> created_by_user_id",
            name=op.f("ck_remediation_plan_decided_by_another_user"),
        ),
        sa.CheckConstraint(
            "(decided_at IS NULL) = (decided_by_user_id IS NULL)",
            name=op.f("ck_remediation_plan_decision_complete"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('approved', 'rejected') OR decided_at IS NOT NULL",
            name=op.f("ck_remediation_plan_decided_has_decision"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('submitted', 'approved', 'rejected') OR submitted_at IS NOT NULL",
            name=op.f("ck_remediation_plan_submitted_has_time"),
        ),
        sa.CheckConstraint(
            "state <> 'approved' OR approved_digest IS NOT NULL",
            name=op.f("ck_remediation_plan_approved_has_digest"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('draft', 'submitted', 'rejected') OR approved_digest IS NULL",
            name=op.f("ck_remediation_plan_unapproved_has_no_digest"),
        ),
        sa.CheckConstraint(
            "approved_digest IS NULL OR approved_digest = plan_digest",
            name=op.f("ck_remediation_plan_approval_binds_digest"),
        ),
        sa.CheckConstraint(
            "(state = 'expired') = (ended_at IS NOT NULL)",
            name=op.f("ck_remediation_plan_expired_has_time"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f("fk_remediation_plan_instance_id_gitlab_instance"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_remediation_plan_created_by_user_id_app_user"),
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_remediation_plan_decided_by_user_id_app_user"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_remediation_plan")),
    )
    op.create_index(
        "ix_remediation_plan_state",
        "remediation_plan",
        ["instance_id", "state", "id"],
        unique=False,
    )
    op.execute(TRANSITION_FUNCTION)
    op.execute(
        "CREATE TRIGGER remediation_plan_transition BEFORE UPDATE OR DELETE ON remediation_plan "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_remediation_plan_transition()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER remediation_plan_transition ON remediation_plan")
    op.execute("DROP FUNCTION baluarte_remediation_plan_transition()")
    op.drop_index("ix_remediation_plan_state", table_name="remediation_plan")
    op.drop_table("remediation_plan")
