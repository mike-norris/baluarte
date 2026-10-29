"""Policy exceptions: time-limited, separately approved waivers of one rule for one project.

``policy_exception`` holds each request and its decision. A trigger allows only the documented
transitions (requested -> approved | rejected, requested | approved -> revoked, and
requested | approved -> expired once ``expires_at`` has passed), keeps what was requested
unchanged and rejects ``DELETE``. CHECK constraints bound the expiry and require the approver
to be someone other than the requester.

``evaluation`` and ``finding`` gain ``exception_id``, set exactly when the outcome or state is
``excepted``. An ``excepted`` evaluation keeps its observed value and evidence, like a ``fail``.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-29 23:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MAX_EXCEPTION_HOURS = 365 * 24

TRANSITION_FUNCTION = """
CREATE FUNCTION baluarte_policy_exception_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'policy_exception rows cannot be deleted'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (NEW.id, NEW.instance_id, NEW.project_id, NEW.policy_name, NEW.rule_id,
        NEW.justification, NEW.requested_by_user_id, NEW.requested_at, NEW.expires_at)
       IS DISTINCT FROM
       (OLD.id, OLD.instance_id, OLD.project_id, OLD.policy_name, OLD.rule_id,
        OLD.justification, OLD.requested_by_user_id, OLD.requested_at, OLD.expires_at) THEN
        RAISE EXCEPTION 'what a policy exception requested cannot change'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.state = 'requested' AND NEW.state IN ('approved', 'rejected') THEN
        IF OLD.decided_at IS NOT NULL OR NEW.ended_at IS NOT NULL THEN
            RAISE EXCEPTION 'invalid policy exception decision'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.state = 'approved' AND OLD.expires_at <= now() THEN
            RAISE EXCEPTION 'an expired policy exception cannot be approved'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state IN ('requested', 'approved') AND NEW.state IN ('revoked', 'expired') THEN
        IF (NEW.decided_by_user_id, NEW.decided_at, NEW.decision_note)
           IS DISTINCT FROM (OLD.decided_by_user_id, OLD.decided_at, OLD.decision_note) THEN
            RAISE EXCEPTION 'a policy exception decision cannot change'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.state = 'expired' AND OLD.expires_at > now() THEN
            RAISE EXCEPTION 'a policy exception cannot expire before expires_at'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'policy exception cannot change from % to %', OLD.state, NEW.state
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$
"""


def upgrade() -> None:
    op.create_table(
        "policy_exception",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("policy_name", sa.Text(), nullable=False),
        sa.Column("rule_id", sa.Text(), nullable=False),
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
        sa.Column("ended_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "state IN ('requested', 'approved', 'rejected', 'revoked', 'expired')",
            name=op.f("ck_policy_exception_state_allowed"),
        ),
        sa.CheckConstraint("length(justification) > 0", name=op.f("ck_policy_exception_justified")),
        sa.CheckConstraint(
            "expires_at > requested_at AND expires_at <= requested_at + "
            f"interval '{MAX_EXCEPTION_HOURS} hours'",
            name=op.f("ck_policy_exception_expiry_bounded"),
        ),
        sa.CheckConstraint(
            "decided_by_user_id IS NULL OR decided_by_user_id <> requested_by_user_id",
            name=op.f("ck_policy_exception_decided_by_another_user"),
        ),
        sa.CheckConstraint(
            "(decided_at IS NULL) = (decided_by_user_id IS NULL)",
            name=op.f("ck_policy_exception_decision_complete"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('approved', 'rejected') OR decided_at IS NOT NULL",
            name=op.f("ck_policy_exception_decided_has_decision"),
        ),
        sa.CheckConstraint(
            "(state IN ('revoked', 'expired')) = (ended_at IS NOT NULL)",
            name=op.f("ck_policy_exception_ended_has_time"),
        ),
        sa.CheckConstraint(
            "(state = 'revoked') = (ended_by_user_id IS NOT NULL)",
            name=op.f("ck_policy_exception_revoked_has_user"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f("fk_policy_exception_instance_id_gitlab_instance"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["gitlab_project.id"],
            name=op.f("fk_policy_exception_project_id_gitlab_project"),
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_policy_exception_requested_by_user_id_app_user"),
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_policy_exception_decided_by_user_id_app_user"),
        ),
        sa.ForeignKeyConstraint(
            ["ended_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_policy_exception_ended_by_user_id_app_user"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_exception")),
    )
    op.create_index(
        op.f("ix_policy_exception_project_id"), "policy_exception", ["project_id"], unique=False
    )
    op.create_index(
        "uq_policy_exception_active",
        "policy_exception",
        ["project_id", "policy_name", "rule_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('requested', 'approved')"),
    )
    op.execute(TRANSITION_FUNCTION)
    op.execute(
        "CREATE TRIGGER policy_exception_transition BEFORE UPDATE OR DELETE ON policy_exception "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_policy_exception_transition()"
    )

    for table in ("evaluation", "finding"):
        op.add_column(table, sa.Column("exception_id", sa.BigInteger(), nullable=True))
        op.create_foreign_key(
            op.f(f"fk_{table}_exception_id_policy_exception"),
            table,
            "policy_exception",
            ["exception_id"],
            ["id"],
        )
    op.create_check_constraint(
        op.f("ck_evaluation_excepted_has_exception"),
        "evaluation",
        "(outcome = 'excepted') = (exception_id IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_finding_excepted_has_exception"),
        "finding",
        "(state = 'excepted') = (exception_id IS NOT NULL)",
    )
    _replace_evaluation_checks(("pass", "fail", "excepted"))


def _replace_evaluation_checks(decided: tuple[str, ...]) -> None:
    values = ", ".join(f"'{outcome}'" for outcome in decided)
    for name, condition in (
        ("observed_only_when_decided", f"outcome IN ({values}) OR observed IS NULL"),
        ("decided_has_evidence", f"outcome NOT IN ({values}) OR observation_id IS NOT NULL"),
    ):
        op.drop_constraint(op.f(f"ck_evaluation_{name}"), "evaluation", "check")
        op.create_check_constraint(op.f(f"ck_evaluation_{name}"), "evaluation", condition)


def downgrade() -> None:
    _replace_evaluation_checks(("pass", "fail"))
    op.drop_constraint(op.f("ck_finding_excepted_has_exception"), "finding", "check")
    op.drop_constraint(op.f("ck_evaluation_excepted_has_exception"), "evaluation", "check")
    for table in ("evaluation", "finding"):
        op.drop_constraint(op.f(f"fk_{table}_exception_id_policy_exception"), table, "foreignkey")
        op.drop_column(table, "exception_id")
    op.execute("DROP TRIGGER policy_exception_transition ON policy_exception")
    op.execute("DROP FUNCTION baluarte_policy_exception_transition()")
    op.drop_index("uq_policy_exception_active", table_name="policy_exception")
    op.drop_index(op.f("ix_policy_exception_project_id"), table_name="policy_exception")
    op.drop_table("policy_exception")
