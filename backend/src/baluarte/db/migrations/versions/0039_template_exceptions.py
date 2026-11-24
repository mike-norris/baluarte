"""Template exceptions: time-limited, separately approved waivers of one template setting for one
project (roadmap M14.2, docs/CONFIGURATION_TEMPLATES.md#exceptions).

Mirrors ``policy_exception``: a trigger allows only requested -> approved | rejected,
requested | approved -> revoked, and requested | approved -> expired once ``expires_at`` has
passed, keeps what was requested unchanged and rejects ``DELETE``. CHECK constraints bound the
expiry and require the approver to be someone other than the requester, unless the decision was
made in development mode by the requester.

Revision ID: 0039
Revises: 0038
Create Date: 2026-11-24 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MAX_EXCEPTION_HOURS = 365 * 24

TRANSITION_FUNCTION = """
CREATE FUNCTION baluarte_template_exception_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'template_exception rows cannot be deleted'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (NEW.id, NEW.workspace_id, NEW.instance_id, NEW.project_id, NEW.setting,
        NEW.justification, NEW.requested_by_user_id, NEW.requested_at, NEW.expires_at)
       IS DISTINCT FROM
       (OLD.id, OLD.workspace_id, OLD.instance_id, OLD.project_id, OLD.setting,
        OLD.justification, OLD.requested_by_user_id, OLD.requested_at, OLD.expires_at) THEN
        RAISE EXCEPTION 'what a template exception requested cannot change'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.state = 'requested' AND NEW.state IN ('approved', 'rejected') THEN
        IF OLD.decided_at IS NOT NULL OR NEW.ended_at IS NOT NULL THEN
            RAISE EXCEPTION 'invalid template exception decision'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.state = 'approved' AND OLD.expires_at <= now() THEN
            RAISE EXCEPTION 'an expired template exception cannot be approved'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state IN ('requested', 'approved') AND NEW.state IN ('revoked', 'expired') THEN
        IF (NEW.decided_by_user_id, NEW.decided_at, NEW.decision_note, NEW.development_decision)
           IS DISTINCT FROM
           (OLD.decided_by_user_id, OLD.decided_at, OLD.decision_note, OLD.development_decision)
        THEN
            RAISE EXCEPTION 'a template exception decision cannot change'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.state = 'expired' AND OLD.expires_at > now() THEN
            RAISE EXCEPTION 'a template exception cannot expire before expires_at'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'template exception cannot change from % to %', OLD.state, NEW.state
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$
"""


def upgrade() -> None:
    op.create_table(
        "template_exception",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("setting", sa.Text(), nullable=False),
        sa.Column("justification", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default="requested", nullable=False),
        sa.Column("requested_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column(
            "development_decision", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("ended_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "state IN ('requested', 'approved', 'rejected', 'revoked', 'expired')",
            name=op.f("ck_template_exception_state_allowed"),
        ),
        sa.CheckConstraint(
            "length(justification) > 0", name=op.f("ck_template_exception_justified")
        ),
        sa.CheckConstraint(
            "expires_at > requested_at AND expires_at <= requested_at + "
            f"interval '{MAX_EXCEPTION_HOURS} hours'",
            name=op.f("ck_template_exception_expiry_bounded"),
        ),
        sa.CheckConstraint(
            "decided_by_user_id IS NULL OR decided_by_user_id <> requested_by_user_id "
            "OR development_decision",
            name=op.f("ck_template_exception_decided_by_another_user"),
        ),
        sa.CheckConstraint(
            "NOT development_decision OR decided_by_user_id = requested_by_user_id",
            name=op.f("ck_template_exception_development_decision_by_proposer"),
        ),
        sa.CheckConstraint(
            "(decided_at IS NULL) = (decided_by_user_id IS NULL)",
            name=op.f("ck_template_exception_decision_complete"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('approved', 'rejected') OR decided_at IS NOT NULL",
            name=op.f("ck_template_exception_decided_has_decision"),
        ),
        sa.CheckConstraint(
            "(state IN ('revoked', 'expired')) = (ended_at IS NOT NULL)",
            name=op.f("ck_template_exception_ended_has_time"),
        ),
        sa.CheckConstraint(
            "(state = 'revoked') = (ended_by_user_id IS NOT NULL)",
            name=op.f("ck_template_exception_revoked_has_user"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_template_exception_workspace_id_workspace"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f("fk_template_exception_instance_id_gitlab_instance"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["gitlab_project.id"],
            name=op.f("fk_template_exception_project_id_gitlab_project"),
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_template_exception_requested_by_user_id_app_user"),
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_template_exception_decided_by_user_id_app_user"),
        ),
        sa.ForeignKeyConstraint(
            ["ended_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_template_exception_ended_by_user_id_app_user"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_template_exception")),
    )
    op.create_index(
        op.f("ix_template_exception_workspace_id"), "template_exception", ["workspace_id"]
    )
    op.create_index(op.f("ix_template_exception_project_id"), "template_exception", ["project_id"])
    op.create_index(
        "uq_template_exception_active",
        "template_exception",
        ["project_id", "setting"],
        unique=True,
        postgresql_where=sa.text("state IN ('requested', 'approved')"),
    )
    op.execute(TRANSITION_FUNCTION)
    op.execute(
        "CREATE TRIGGER template_exception_transition BEFORE UPDATE OR DELETE "
        "ON template_exception FOR EACH ROW "
        "EXECUTE FUNCTION baluarte_template_exception_transition()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER template_exception_transition ON template_exception")
    op.drop_table("template_exception")
    op.execute("DROP FUNCTION baluarte_template_exception_transition()")
