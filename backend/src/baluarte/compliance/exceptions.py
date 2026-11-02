"""Compliance exceptions: recorded, time-limited departures from the operating profile.

An exception in force lets its control be unmet: a setting may be saved below the profile's
minimum, and the posture shows the control ``excepted``. While two-person review is required
(the ``governance`` setting) a new exception is ``proposed`` and someone other than the requester
must approve it; otherwise it is approved at once and recorded ``self_approved``. Exceptions
are never deleted; one ends by expiring or being revoked. The database enforces the lifecycle
again (migration 0016).
"""

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.compliance.catalogue import CONTROLS_BY_ID
from baluarte.compliance.operations import stored_review, today
from baluarte.db.models import MAX_COMPLIANCE_EXCEPTION_DAYS, ComplianceException


class ExceptionInvalid(Exception):
    """The request is invalid (HTTP 422)."""


class ExceptionConflict(Exception):
    """Not possible in the exception's current state (HTTP 409)."""


class NotAllowed(Exception):
    """The caller may not decide this exception: it is their own (HTTP 409)."""


class NotFound(Exception):
    """No such exception (HTTP 404)."""


@dataclass(frozen=True)
class Actor:
    user_id: int
    actor: str


def in_force(row: ComplianceException, on: date | None = None) -> bool:
    return row.state == "approved" and (on or today()) < row.expires_on


def controls_in_force(db: Session, on: date | None = None) -> dict[str, ComplianceException]:
    """The control IDs with an exception in force, each with the one that lasts longest."""
    rows = db.scalars(
        select(ComplianceException)
        .where(
            ComplianceException.state == "approved",
            ComplianceException.expires_on > (on or today()),
        )
        .order_by(ComplianceException.expires_on, ComplianceException.id)
    )
    return {row.control_id: row for row in rows}


def details(row: ComplianceException) -> dict[str, object]:
    return {
        "control_id": row.control_id,
        "expires_on": row.expires_on.isoformat(),
        "state": row.state,
        "review_required": row.review_required,
        "self_approved": row.self_approved,
    }


def _get(db: Session, exception_id: int) -> ComplianceException:
    row = db.get(ComplianceException, exception_id, with_for_update=True)
    if row is None:
        raise NotFound(f"Compliance exception {exception_id} not found")
    return row


def request(
    db: Session, control_id: str, justification: str, expires_on: date, by: Actor
) -> ComplianceException:
    """Record an exception, approved at once unless review is required. The caller commits."""
    if control_id not in CONTROLS_BY_ID:
        raise ExceptionInvalid(f"Unknown control: {control_id}")
    now = today()
    if not now < expires_on <= now + timedelta(days=MAX_COMPLIANCE_EXCEPTION_DAYS):
        raise ExceptionInvalid(
            f"expires_on must be after today and at most {MAX_COMPLIANCE_EXCEPTION_DAYS} days away"
        )
    if db.scalar(
        select(ComplianceException.id).where(
            ComplianceException.control_id == control_id, ComplianceException.state == "proposed"
        )
    ):
        raise ExceptionConflict(f"An exception for {control_id} is already waiting for a decision")
    required = bool(stored_review(db))
    row = ComplianceException(
        control_id=control_id,
        justification=justification.strip(),
        expires_on=expires_on,
        review_required=required,
        self_approved=not required,
        requested_by=by.actor,
        requested_by_user_id=by.user_id,
    )
    if required:
        row.state = "proposed"
    else:
        row.state = "approved"
        row.decided_by = by.actor
        row.decided_by_user_id = by.user_id
        row.decided_at = func.now()
    db.add(row)
    db.flush()
    db.refresh(row)
    audit.record(
        db,
        actor=by.actor,
        action="compliance.exception_requested",
        resource_type="compliance_exception",
        resource_id=str(row.id),
        details=details(row),
    )
    return row


def _decide(
    db: Session, exception_id: int, by: Actor, state: str, note: str | None
) -> ComplianceException:
    row = _get(db, exception_id)
    if row.state != "proposed":
        raise ExceptionConflict(f"Compliance exception {exception_id} is {row.state}")
    if row.requested_by_user_id == by.user_id:
        raise NotAllowed("Someone other than the requester must decide this exception")
    if state == "approved" and row.expires_on <= today():
        raise ExceptionConflict(f"Compliance exception {exception_id} has already expired")
    row.state = state
    row.decided_by = by.actor
    row.decided_by_user_id = by.user_id
    row.decided_at = func.now()
    row.decision_note = note.strip() if note and note.strip() else None
    db.flush()
    db.refresh(row)
    audit.record(
        db,
        actor=by.actor,
        action=f"compliance.exception_{state}",
        resource_type="compliance_exception",
        resource_id=str(row.id),
        details={**details(row), "note": row.decision_note},
    )
    return row


def approve(
    db: Session, exception_id: int, by: Actor, note: str | None = None
) -> ComplianceException:
    return _decide(db, exception_id, by, "approved", note)


def reject(db: Session, exception_id: int, by: Actor, note: str) -> ComplianceException:
    if not note.strip():
        raise ExceptionInvalid("A rejection needs a note")
    return _decide(db, exception_id, by, "rejected", note)


def revoke(db: Session, exception_id: int, by: Actor) -> ComplianceException:
    """End a proposed or approved exception now. Anyone who may manage settings may revoke."""
    row = _get(db, exception_id)
    if row.state not in ("proposed", "approved"):
        raise ExceptionConflict(f"Compliance exception {exception_id} is {row.state}")
    row.state = "revoked"
    row.revoked_by = by.actor
    row.revoked_by_user_id = by.user_id
    row.revoked_at = func.now()
    db.flush()
    db.refresh(row)
    audit.record(
        db,
        actor=by.actor,
        action="compliance.exception_revoked",
        resource_type="compliance_exception",
        resource_id=str(row.id),
        details=details(row),
    )
    return row


def listing(db: Session) -> list[ComplianceException]:
    return list(db.scalars(select(ComplianceException).order_by(ComplianceException.id.desc())))
