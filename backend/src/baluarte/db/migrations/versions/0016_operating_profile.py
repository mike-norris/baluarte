"""Operating profile: compliance exceptions, attestations and audit expiry.

``compliance_exception`` records a time-limited departure from the operating profile. A trigger
allows only ``proposed`` -> ``approved`` | ``rejected`` | ``revoked`` and ``approved`` ->
``revoked``, keeps the request and the decision unchanged and rejects ``DELETE``; CHECK
constraints require the approver to be someone other than the requester whenever review was
required. ``compliance_attestation`` rows are immutable.

``audit_event`` keeps rejecting ``UPDATE`` and ``DELETE``, except a ``DELETE`` made by
``baluarte_expire_audit``: a SECURITY DEFINER function, owned by the schema owner, that deletes
events older than its cutoff (never younger than 30 days) and returns how many it deleted, the
first and last IDs and a SHA-256 over their digests in ID order. The trigger lets a delete
through only while that function runs as the table owner. EXECUTE is revoked from PUBLIC;
``baluarte-migrate`` grants it to the application's role, which has no DELETE on audit_event.

``app_setting`` accepts the ``operations`` key.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-02 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXCEPTION_TRANSITION = """
CREATE FUNCTION baluarte_compliance_exception_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'compliance_exception rows cannot be deleted'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (NEW.id, NEW.control_id, NEW.justification, NEW.expires_on, NEW.review_required,
        NEW.self_approved, NEW.requested_by, NEW.requested_by_user_id, NEW.requested_at)
       IS DISTINCT FROM
       (OLD.id, OLD.control_id, OLD.justification, OLD.expires_on, OLD.review_required,
        OLD.self_approved, OLD.requested_by, OLD.requested_by_user_id, OLD.requested_at) THEN
        RAISE EXCEPTION 'what a compliance exception requested cannot change'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.decided_at IS NOT NULL
       AND (NEW.decided_by, NEW.decided_by_user_id, NEW.decided_at, NEW.decision_note)
           IS DISTINCT FROM
           (OLD.decided_by, OLD.decided_by_user_id, OLD.decided_at, OLD.decision_note) THEN
        RAISE EXCEPTION 'a decision on a compliance exception cannot change'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (OLD.state = 'proposed' AND NEW.state IN ('approved', 'rejected', 'revoked'))
       OR (OLD.state = 'approved' AND NEW.state = 'revoked') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'compliance exception cannot change from % to %', OLD.state, NEW.state
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$
"""

AUDIT_IMMUTABLE = """
CREATE FUNCTION baluarte_audit_event_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE'
       AND current_setting('baluarte.audit_expiry', true) = 'on'
       AND current_user = (SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID)
    THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION '% on % is not allowed: rows are immutable evidence', TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END;
$$
"""

EXPIRE_AUDIT = """
CREATE FUNCTION baluarte_expire_audit(cutoff timestamptz)
RETURNS TABLE (deleted bigint, first_id bigint, last_id bigint, digest text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF cutoff IS NULL OR cutoff > now() - interval '30 days' THEN
        RAISE EXCEPTION 'audit events younger than 30 days cannot be expired'
            USING ERRCODE = 'restrict_violation';
    END IF;
    PERFORM set_config('baluarte.audit_expiry', 'on', true);
    WITH gone AS (
        DELETE FROM audit_event WHERE occurred_at < cutoff RETURNING id, details_digest
    )
    SELECT count(*), min(gone.id), max(gone.id),
           encode(sha256(convert_to(
               coalesce(string_agg(gone.details_digest, '' ORDER BY gone.id), ''), 'UTF8'
           )), 'hex')
      INTO deleted, first_id, last_id, digest
      FROM gone;
    PERFORM set_config('baluarte.audit_expiry', 'off', true);
    RETURN NEXT;
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
        "compliance_exception",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("control_id", sa.Text(), nullable=False),
        sa.Column("justification", sa.Text(), nullable=False),
        sa.Column("expires_on", sa.Date(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("review_required", sa.Boolean(), nullable=False),
        sa.Column("self_approved", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("requested_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("revoked_by", sa.Text(), nullable=True),
        sa.Column("revoked_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('proposed', 'approved', 'rejected', 'revoked')",
            name=op.f("ck_compliance_exception_state_allowed"),
        ),
        sa.CheckConstraint(
            "control_id ~ '^[a-z][a-z0-9-]{0,63}$'",
            name=op.f("ck_compliance_exception_control_id_format"),
        ),
        sa.CheckConstraint(
            "length(justification) BETWEEN 10 AND 2000",
            name=op.f("ck_compliance_exception_justification_length"),
        ),
        sa.CheckConstraint(
            "expires_on > (requested_at AT TIME ZONE 'UTC')::date AND expires_on <= "
            "(requested_at AT TIME ZONE 'UTC')::date + 365",
            name=op.f("ck_compliance_exception_expiry_bounded"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('approved', 'rejected') OR decided_at IS NOT NULL",
            name=op.f("ck_compliance_exception_decided_has_decision"),
        ),
        sa.CheckConstraint(
            "state <> 'proposed' OR decided_at IS NULL",
            name=op.f("ck_compliance_exception_proposed_undecided"),
        ),
        sa.CheckConstraint(
            "(decided_at IS NULL) = (decided_by IS NULL) "
            "AND (decided_at IS NULL) = (decided_by_user_id IS NULL)",
            name=op.f("ck_compliance_exception_decision_complete"),
        ),
        sa.CheckConstraint(
            "(state = 'revoked') = (revoked_at IS NOT NULL) "
            "AND (revoked_at IS NULL) = (revoked_by IS NULL) "
            "AND (revoked_at IS NULL) = (revoked_by_user_id IS NULL)",
            name=op.f("ck_compliance_exception_revocation_complete"),
        ),
        sa.CheckConstraint(
            "NOT review_required OR decided_by_user_id IS NULL "
            "OR decided_by_user_id <> requested_by_user_id",
            name=op.f("ck_compliance_exception_decided_by_another_user"),
        ),
        sa.CheckConstraint(
            "self_approved = NOT review_required",
            name=op.f("ck_compliance_exception_self_approved_unreviewed"),
        ),
        sa.CheckConstraint(
            "NOT self_approved OR (decided_by_user_id = requested_by_user_id "
            "AND state IN ('approved', 'revoked'))",
            name=op.f("ck_compliance_exception_self_approved_by_requester"),
        ),
        sa.CheckConstraint(
            "state <> 'rejected' OR length(decision_note) > 0",
            name=op.f("ck_compliance_exception_rejection_explained"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_compliance_exception")),
    )
    op.create_index(
        "ix_compliance_exception_control",
        "compliance_exception",
        ["control_id", "state"],
        unique=False,
    )
    op.create_index(
        "uq_compliance_exception_proposed",
        "compliance_exception",
        ["control_id"],
        unique=True,
        postgresql_where=sa.text("state = 'proposed'"),
    )
    op.execute(EXCEPTION_TRANSITION)
    op.execute(
        "CREATE TRIGGER compliance_exception_transition BEFORE UPDATE OR DELETE "
        "ON compliance_exception FOR EACH ROW "
        "EXECUTE FUNCTION baluarte_compliance_exception_transition()"
    )

    op.create_table(
        "compliance_attestation",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("control_id", sa.Text(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("attested_by", sa.Text(), nullable=False),
        sa.Column("attested_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "attested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "control_id ~ '^[a-z][a-z0-9-]{0,63}$'",
            name=op.f("ck_compliance_attestation_control_id_format"),
        ),
        sa.CheckConstraint(
            "length(statement) BETWEEN 10 AND 2000",
            name=op.f("ck_compliance_attestation_statement_length"),
        ),
        sa.CheckConstraint(
            "expires_at > attested_at",
            name=op.f("ck_compliance_attestation_expires_after_attested"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_compliance_attestation")),
    )
    op.create_index(
        "ix_compliance_attestation_control",
        "compliance_attestation",
        ["control_id", "attested_at"],
        unique=False,
    )
    op.execute(
        "CREATE TRIGGER compliance_attestation_immutable BEFORE UPDATE OR DELETE "
        "ON compliance_attestation FOR EACH ROW "
        "EXECUTE FUNCTION baluarte_reject_modification()"
    )

    op.execute("DROP TRIGGER audit_event_immutable ON audit_event")
    op.execute(AUDIT_IMMUTABLE)
    op.execute(
        "CREATE TRIGGER audit_event_immutable BEFORE UPDATE OR DELETE ON audit_event "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_audit_event_immutable()"
    )
    op.execute(EXPIRE_AUDIT)
    op.execute("REVOKE ALL ON FUNCTION baluarte_expire_audit(timestamptz) FROM PUBLIC")

    _setting_keys("oidc", "gitlab", "governance", "operations")


def downgrade() -> None:
    op.execute("DELETE FROM app_setting WHERE key = 'operations'")
    _setting_keys("oidc", "gitlab", "governance")

    op.execute("DROP FUNCTION baluarte_expire_audit(timestamptz)")
    op.execute("DROP TRIGGER audit_event_immutable ON audit_event")
    op.execute("DROP FUNCTION baluarte_audit_event_immutable()")
    op.execute(
        "CREATE TRIGGER audit_event_immutable BEFORE UPDATE OR DELETE ON audit_event "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_reject_modification()"
    )

    op.execute("DROP TRIGGER compliance_attestation_immutable ON compliance_attestation")
    op.drop_index("ix_compliance_attestation_control", table_name="compliance_attestation")
    op.drop_table("compliance_attestation")

    op.execute("DROP TRIGGER compliance_exception_transition ON compliance_exception")
    op.execute("DROP FUNCTION baluarte_compliance_exception_transition()")
    op.drop_index("uq_compliance_exception_proposed", table_name="compliance_exception")
    op.drop_index("ix_compliance_exception_control", table_name="compliance_exception")
    op.drop_table("compliance_exception")
