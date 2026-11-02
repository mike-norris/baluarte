"""Attestations: an administrator's statement that a control Baluarte cannot observe is in place.

Each is current for the control's ``attestation_days``; a newer one replaces it in the posture.
Rows are immutable (migration 0016), so the history of who attested what, and when, is kept.
"""

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.compliance.catalogue import CONTROLS_BY_ID
from baluarte.compliance.exceptions import Actor
from baluarte.db.models import ComplianceAttestation


class AttestationInvalid(Exception):
    """The control does not take attestations (HTTP 422)."""


def record(db: Session, control_id: str, statement: str, by: Actor) -> ComplianceAttestation:
    """Record an attestation. The caller commits."""
    control = CONTROLS_BY_ID.get(control_id)
    if control is None or control.attestation_days is None:
        raise AttestationInvalid(f"{control_id} does not take attestations")
    row = ComplianceAttestation(
        control_id=control_id,
        statement=statement.strip(),
        attested_by=by.actor,
        attested_by_user_id=by.user_id,
        expires_at=func.now() + timedelta(days=control.attestation_days),
    )
    db.add(row)
    db.flush()
    db.refresh(row)
    audit.record(
        db,
        actor=by.actor,
        action="compliance.attested",
        resource_type="compliance_attestation",
        resource_id=str(row.id),
        details={
            "control_id": control_id,
            "statement": row.statement,
            "expires_at": row.expires_at.isoformat(),
        },
    )
    return row


def listing(db: Session, control_id: str | None = None) -> list[ComplianceAttestation]:
    statement = select(ComplianceAttestation).order_by(ComplianceAttestation.id.desc())
    if control_id is not None:
        statement = statement.where(ComplianceAttestation.control_id == control_id)
    return list(db.scalars(statement))


def latest(db: Session) -> dict[str, ComplianceAttestation]:
    """The newest attestation of each control."""
    rows = db.scalars(
        select(ComplianceAttestation).order_by(
            ComplianceAttestation.attested_at, ComplianceAttestation.id
        )
    )
    return {row.control_id: row for row in rows}
