"""Monitoring scope: projects, groups or personal namespaces taken out of monitoring.

``scope_exclusion`` records each target an organization takes out of monitoring, with a
justification, and when it is brought back. Review follows the ``governance`` setting, like
baseline and control-mapping changes. Adds ``project_excluded`` as a finding resolution: an
excluded project is not evaluated, and its open or excepted findings are resolved with it.

A trigger allows only ``proposed`` -> ``active`` | ``rejected`` (recording the decision) and
``active`` -> ``ended`` (recording the end), and rejects ``DELETE``.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-04 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRANSITION_FUNCTION = """
CREATE FUNCTION baluarte_scope_exclusion_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'scope_exclusion rows cannot be deleted'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (NEW.id, NEW.instance_id, NEW.target_type, NEW.target_id, NEW.target_path, NEW.batch_id,
        NEW.justification, NEW.review_required, NEW.self_approved, NEW.proposed_by,
        NEW.proposed_by_user_id, NEW.proposed_at)
       IS DISTINCT FROM
       (OLD.id, OLD.instance_id, OLD.target_type, OLD.target_id, OLD.target_path, OLD.batch_id,
        OLD.justification, OLD.review_required, OLD.self_approved, OLD.proposed_by,
        OLD.proposed_by_user_id, OLD.proposed_at) THEN
        RAISE EXCEPTION 'what a scope exclusion proposed cannot change'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.state = 'proposed' AND NEW.state IN ('active', 'rejected')
       AND NEW.ended_at IS NULL THEN
        RETURN NEW;
    END IF;
    IF OLD.state = 'active' AND NEW.state = 'ended'
       AND (NEW.decided_by, NEW.decided_by_user_id, NEW.decided_at, NEW.decision_note)
           IS NOT DISTINCT FROM
           (OLD.decided_by, OLD.decided_by_user_id, OLD.decided_at, OLD.decision_note) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'scope exclusion cannot change from % to %', OLD.state, NEW.state
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$
"""


def _resolutions(*resolutions: str, validate: bool = True) -> None:
    op.drop_constraint(op.f("ck_finding_resolution_allowed"), "finding", type_="check")
    allowed = ", ".join(f"'{resolution}'" for resolution in resolutions)
    # NOT VALID on downgrade: findings already resolved as project_excluded stay, and only new
    # or changed rows are checked.
    op.execute(
        "ALTER TABLE finding ADD CONSTRAINT ck_finding_resolution_allowed "
        f"CHECK (resolution IN ({allowed})){'' if validate else ' NOT VALID'}"
    )


def upgrade() -> None:
    op.create_table(
        "scope_exclusion",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.BigInteger(), nullable=True),
        sa.Column("target_path", sa.Text(), nullable=True),
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("justification", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("review_required", sa.Boolean(), nullable=False),
        sa.Column("self_approved", sa.Boolean(), server_default=sa.text("false"), nullable=False),
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
        sa.Column("ended_by", sa.Text(), nullable=True),
        sa.Column("ended_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_justification", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "target_type IN ('project', 'group', 'personal_namespaces')",
            name=op.f("ck_scope_exclusion_target_type_allowed"),
        ),
        sa.CheckConstraint(
            "state IN ('proposed', 'active', 'rejected', 'ended')",
            name=op.f("ck_scope_exclusion_state_allowed"),
        ),
        sa.CheckConstraint(
            "(target_id IS NULL) = (target_type = 'personal_namespaces')",
            name=op.f("ck_scope_exclusion_target_id_unless_personal"),
        ),
        sa.CheckConstraint(
            "length(justification) BETWEEN 20 AND 2000",
            name=op.f("ck_scope_exclusion_justification_length"),
        ),
        sa.CheckConstraint(
            "end_justification IS NULL OR length(end_justification) BETWEEN 20 AND 2000",
            name=op.f("ck_scope_exclusion_end_justification_length"),
        ),
        sa.CheckConstraint(
            "(state = 'proposed') = (decided_at IS NULL)",
            name=op.f("ck_scope_exclusion_decided_unless_proposed"),
        ),
        sa.CheckConstraint(
            "(decided_at IS NULL) = (decided_by IS NULL) "
            "AND (decided_at IS NULL) = (decided_by_user_id IS NULL)",
            name=op.f("ck_scope_exclusion_decision_complete"),
        ),
        sa.CheckConstraint(
            "(state = 'ended') = (ended_at IS NOT NULL)",
            name=op.f("ck_scope_exclusion_ended_when_ended"),
        ),
        sa.CheckConstraint(
            "(ended_at IS NULL) = (ended_by IS NULL) "
            "AND (ended_at IS NULL) = (ended_by_user_id IS NULL) "
            "AND (ended_at IS NULL) = (end_justification IS NULL)",
            name=op.f("ck_scope_exclusion_end_complete"),
        ),
        sa.CheckConstraint(
            "NOT review_required OR decided_by_user_id IS NULL "
            "OR decided_by_user_id <> proposed_by_user_id",
            name=op.f("ck_scope_exclusion_decided_by_another_user"),
        ),
        sa.CheckConstraint(
            "review_required OR state IN ('active', 'ended')",
            name=op.f("ck_scope_exclusion_unreviewed_is_applied"),
        ),
        sa.CheckConstraint(
            "self_approved = NOT review_required",
            name=op.f("ck_scope_exclusion_self_approved_when_unreviewed"),
        ),
        sa.CheckConstraint(
            "NOT self_approved OR decided_by_user_id = proposed_by_user_id",
            name=op.f("ck_scope_exclusion_self_approved_by_proposer"),
        ),
        sa.CheckConstraint(
            "state <> 'rejected' OR length(decision_note) > 0",
            name=op.f("ck_scope_exclusion_rejection_explained"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f("fk_scope_exclusion_instance_id_gitlab_instance"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scope_exclusion")),
    )
    op.create_index("ix_scope_exclusion_batch", "scope_exclusion", ["batch_id"], unique=False)
    op.create_index(
        "ix_scope_exclusion_state", "scope_exclusion", ["instance_id", "state"], unique=False
    )
    op.create_index(
        "uq_scope_exclusion_open",
        "scope_exclusion",
        ["instance_id", "target_type", sa.text("coalesce(target_id, 0)")],
        unique=True,
        postgresql_where=sa.text("state IN ('proposed', 'active')"),
    )
    op.execute(TRANSITION_FUNCTION)
    op.execute(
        "CREATE TRIGGER scope_exclusion_transition "
        "BEFORE UPDATE OR DELETE ON scope_exclusion "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_scope_exclusion_transition()"
    )
    _resolutions("passed", "no_longer_applies", "project_archived", "project_excluded")


def downgrade() -> None:
    _resolutions("passed", "no_longer_applies", "project_archived", validate=False)
    op.execute("DROP TRIGGER scope_exclusion_transition ON scope_exclusion")
    op.execute("DROP FUNCTION baluarte_scope_exclusion_transition()")
    op.drop_index("uq_scope_exclusion_open", table_name="scope_exclusion")
    op.drop_index("ix_scope_exclusion_state", table_name="scope_exclusion")
    op.drop_index("ix_scope_exclusion_batch", table_name="scope_exclusion")
    op.drop_table("scope_exclusion")
