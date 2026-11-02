"""Two-person review of baseline changes.

``policy_change`` records every change to the baselines in force: a new policy version, an
assignment and an unassignment. While the ``governance`` setting requires review, a change is
``proposed`` and does nothing until someone other than the proposer approves it; otherwise it is
applied at once and recorded ``approved`` and ``self_approved``. ``policy`` and
``policy_assignment`` keep holding only what is in force, so evaluation is unchanged.

A trigger allows only ``proposed`` -> ``approved`` | ``rejected``, keeps what was proposed
unchanged (an approval may only fill in its result: the new version's ``policy_id`` or the new
assignment's ``assignment_id``) and rejects ``DELETE``. CHECK constraints require the approver
to be someone other than the proposer whenever review was required.

Every existing version and assignment is recorded as an approved, ``migrated`` change made
before review existed; its proposer is taken from the audit trail when it is there.
``app_setting`` accepts the ``governance`` key.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-01 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATION_ACTOR = "system:migration-0015"

TRANSITION_FUNCTION = """
CREATE FUNCTION baluarte_policy_change_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'policy_change rows cannot be deleted'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (NEW.id, NEW.kind, NEW.policy_name, NEW.source_yaml, NEW.yaml_digest, NEW.base_version,
        NEW.instance_id, NEW.target_type, NEW.target_id, NEW.review_required, NEW.self_approved,
        NEW.migrated, NEW.proposed_by, NEW.proposed_by_user_id, NEW.proposed_at)
       IS DISTINCT FROM
       (OLD.id, OLD.kind, OLD.policy_name, OLD.source_yaml, OLD.yaml_digest, OLD.base_version,
        OLD.instance_id, OLD.target_type, OLD.target_id, OLD.review_required, OLD.self_approved,
        OLD.migrated, OLD.proposed_by, OLD.proposed_by_user_id, OLD.proposed_at) THEN
        RAISE EXCEPTION 'what a policy change proposed cannot change'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.state = 'proposed' AND NEW.state IN ('approved', 'rejected') THEN
        IF NEW.policy_id IS DISTINCT FROM OLD.policy_id
           AND NOT (NEW.state = 'approved' AND OLD.kind = 'version' AND OLD.policy_id IS NULL) THEN
            RAISE EXCEPTION 'only an approved version records the version it became'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.assignment_id IS DISTINCT FROM OLD.assignment_id
           AND NOT (NEW.state = 'approved' AND OLD.kind = 'assign'
                    AND OLD.assignment_id IS NULL) THEN
            RAISE EXCEPTION 'only an approved assignment records the assignment it made'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'policy change cannot change from % to %', OLD.state, NEW.state
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$
"""


def _setting_keys(*keys: str) -> None:
    op.drop_constraint(op.f("ck_app_setting_key_allowed"), "app_setting", type_="check")
    allowed = ", ".join(f"'{key}'" for key in keys)
    op.create_check_constraint(
        op.f("ck_app_setting_key_allowed"), "app_setting", f"key IN ({allowed})"
    )


def upgrade() -> None:
    op.create_table(
        "policy_change",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("policy_name", sa.Text(), nullable=False),
        sa.Column("source_yaml", sa.Text(), nullable=True),
        sa.Column("yaml_digest", sa.Text(), nullable=True),
        sa.Column("base_version", sa.Integer(), nullable=True),
        sa.Column("policy_id", sa.BigInteger(), nullable=True),
        sa.Column("instance_id", sa.BigInteger(), nullable=True),
        sa.Column("target_type", sa.Text(), nullable=True),
        sa.Column("target_id", sa.BigInteger(), nullable=True),
        sa.Column("assignment_id", sa.BigInteger(), nullable=True),
        sa.Column("review_required", sa.Boolean(), nullable=False),
        sa.Column("self_approved", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("migrated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("proposed_by", sa.Text(), nullable=False),
        sa.Column("proposed_by_user_id", sa.BigInteger(), nullable=True),
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
            "kind IN ('version', 'assign', 'unassign')", name=op.f("ck_policy_change_kind_allowed")
        ),
        sa.CheckConstraint(
            "state IN ('proposed', 'approved', 'rejected')",
            name=op.f("ck_policy_change_state_allowed"),
        ),
        sa.CheckConstraint(
            "kind <> 'version' OR (source_yaml IS NOT NULL AND yaml_digest IS NOT NULL "
            "AND instance_id IS NULL AND target_type IS NULL AND target_id IS NULL "
            "AND assignment_id IS NULL)",
            name=op.f("ck_policy_change_version_shape"),
        ),
        sa.CheckConstraint(
            "kind = 'version' OR (source_yaml IS NULL AND yaml_digest IS NULL "
            "AND instance_id IS NOT NULL AND target_type IS NOT NULL AND target_id IS NOT NULL "
            "AND policy_id IS NOT NULL)",
            name=op.f("ck_policy_change_assignment_shape"),
        ),
        sa.CheckConstraint(
            "kind <> 'unassign' OR assignment_id IS NOT NULL",
            name=op.f("ck_policy_change_unassign_names"),
        ),
        sa.CheckConstraint(
            "state <> 'approved' OR kind <> 'version' OR policy_id IS NOT NULL",
            name=op.f("ck_policy_change_approved_version_has_result"),
        ),
        sa.CheckConstraint(
            "state <> 'approved' OR kind <> 'assign' OR assignment_id IS NOT NULL",
            name=op.f("ck_policy_change_approved_assignment_has_result"),
        ),
        sa.CheckConstraint(
            "target_type IS NULL OR target_type IN ('instance', 'group', 'project')",
            name=op.f("ck_policy_change_target_type_allowed"),
        ),
        sa.CheckConstraint(
            "yaml_digest IS NULL OR yaml_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_policy_change_yaml_digest_sha256"),
        ),
        sa.CheckConstraint(
            "(state = 'proposed') = (decided_at IS NULL)",
            name=op.f("ck_policy_change_decided_unless_proposed"),
        ),
        sa.CheckConstraint(
            "(decided_at IS NULL) = (decided_by IS NULL)",
            name=op.f("ck_policy_change_decision_complete"),
        ),
        sa.CheckConstraint(
            "migrated OR (proposed_by_user_id IS NOT NULL "
            "AND (decided_at IS NULL) = (decided_by_user_id IS NULL))",
            name=op.f("ck_policy_change_users_recorded"),
        ),
        sa.CheckConstraint(
            "NOT review_required OR decided_by_user_id IS NULL "
            "OR decided_by_user_id <> proposed_by_user_id",
            name=op.f("ck_policy_change_decided_by_another_user"),
        ),
        sa.CheckConstraint(
            "review_required OR state = 'approved'",
            name=op.f("ck_policy_change_unreviewed_is_applied"),
        ),
        sa.CheckConstraint(
            "self_approved = (NOT review_required AND NOT migrated)",
            name=op.f("ck_policy_change_self_approved_when_unreviewed"),
        ),
        sa.CheckConstraint(
            "NOT self_approved OR decided_by_user_id = proposed_by_user_id",
            name=op.f("ck_policy_change_self_approved_by_proposer"),
        ),
        sa.CheckConstraint(
            "NOT migrated OR NOT review_required", name=op.f("ck_policy_change_migrated_unreviewed")
        ),
        sa.CheckConstraint(
            "state <> 'rejected' OR length(decision_note) > 0",
            name=op.f("ck_policy_change_rejection_explained"),
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"], ["policy.id"], name=op.f("fk_policy_change_policy_id_policy")
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f("fk_policy_change_instance_id_gitlab_instance"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_change")),
    )
    op.create_index("ix_policy_change_policy_id", "policy_change", ["policy_id"], unique=False)
    op.create_index(
        "ix_policy_change_assignment_id", "policy_change", ["assignment_id"], unique=False
    )
    op.create_index(
        "uq_policy_change_proposed_version",
        "policy_change",
        ["policy_name"],
        unique=True,
        postgresql_where=sa.text("state = 'proposed' AND kind = 'version'"),
    )
    op.create_index(
        "uq_policy_change_proposed_target",
        "policy_change",
        ["instance_id", "target_type", "target_id", "policy_name"],
        unique=True,
        postgresql_where=sa.text("state = 'proposed' AND kind <> 'version'"),
    )

    # What is already in force was applied before review existed.
    op.execute(
        sa.text(
            """
        INSERT INTO policy_change (kind, state, policy_name, source_yaml, yaml_digest,
            base_version, policy_id, review_required, migrated, proposed_by, proposed_at,
            decided_by, decided_at, decision_note)
        SELECT 'version', 'approved', p.name, p.source_yaml, p.yaml_digest,
            NULLIF(p.version - 1, 0), p.id, false, true,
            COALESCE((SELECT a.actor FROM audit_event a
                      WHERE a.action = 'policy.version_created'
                        AND a.resource_id = p.name || '@' || p.version
                      ORDER BY a.id LIMIT 1), 'unknown'),
            p.created_at, :actor, now(), 'In force before baseline review existed'
        FROM policy p ORDER BY p.id
            """
        ).bindparams(actor=MIGRATION_ACTOR)
    )
    op.execute(
        sa.text(
            """
        INSERT INTO policy_change (kind, state, policy_name, policy_id, instance_id, target_type,
            target_id, assignment_id, review_required, migrated, proposed_by, proposed_at,
            decided_by, decided_at, decision_note)
        SELECT 'assign', 'approved', pa.policy_name, pa.policy_id, pa.instance_id,
            pa.target_type, pa.target_id, pa.id, false, true,
            COALESCE((SELECT a.actor FROM audit_event a
                      WHERE a.action = 'policy.assigned' AND a.resource_id = pa.id::text
                      ORDER BY a.id LIMIT 1), 'unknown'),
            pa.created_at, :actor, now(), 'In force before baseline review existed'
        FROM policy_assignment pa ORDER BY pa.id
            """
        ).bindparams(actor=MIGRATION_ACTOR)
    )

    op.execute(TRANSITION_FUNCTION)
    op.execute(
        "CREATE TRIGGER policy_change_transition BEFORE UPDATE OR DELETE ON policy_change "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_policy_change_transition()"
    )
    _setting_keys("oidc", "gitlab", "governance")


def downgrade() -> None:
    op.execute("DELETE FROM app_setting WHERE key = 'governance'")
    _setting_keys("oidc", "gitlab")
    op.execute("DROP TRIGGER policy_change_transition ON policy_change")
    op.execute("DROP FUNCTION baluarte_policy_change_transition()")
    op.drop_index("uq_policy_change_proposed_target", table_name="policy_change")
    op.drop_index("uq_policy_change_proposed_version", table_name="policy_change")
    op.drop_index("ix_policy_change_assignment_id", table_name="policy_change")
    op.drop_index("ix_policy_change_policy_id", table_name="policy_change")
    op.drop_table("policy_change")
