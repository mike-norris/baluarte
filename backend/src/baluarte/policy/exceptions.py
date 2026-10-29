"""Policy exceptions: time-limited waivers of one rule for one project (BAL-033).

An exception waives a *known* failure. While one is approved and unexpired, a ``fail`` for its
project, policy name and rule is stored as ``excepted`` with the exception's ID, keeping the
observed value and evidence, and the finding's state is ``excepted``. ``unknown`` and
``unsupported`` results are never excepted: an exception does not stand in for missing or
untrustworthy evidence.

Lifecycle (enforced again by a database trigger):

- ``requested`` -> ``approved`` or ``rejected``, by a user holding ``exception:approve`` who
  is not the requester;
- ``requested`` or ``approved`` -> ``revoked`` by such a user, or ``expired`` once
  ``expires_at`` has passed (database clock).

Expiry takes effect at ``expires_at`` whether or not the row has been marked ``expired``:
lookups compare with ``now()``, and every evaluation run first marks due exceptions expired
and reopens their findings. Revoking reopens them at once.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import ColumnElement, and_, case, func, select, update
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.db.models import Finding, PolicyException

ExceptionState = Literal["requested", "approved", "rejected", "revoked", "expired"]
SYSTEM_ACTOR = "system:policy-exceptions"
MAX_JUSTIFICATION = 4000
MAX_NOTE = 2000


class ExceptionConflict(Exception):
    """The requested transition is not allowed in the exception's current state."""


def in_force() -> ColumnElement[bool]:
    """SQL: the exception currently waives failures."""
    return and_(PolicyException.state == "approved", PolicyException.expires_at > func.now())


def effective_state() -> ColumnElement[str]:
    """SQL: the state, counting a requested or approved exception past its expiry as expired."""
    return case(
        (
            and_(
                PolicyException.state.in_(("requested", "approved")),
                PolicyException.expires_at <= func.now(),
            ),
            "expired",
        ),
        else_=PolicyException.state,
    )


def active_for_project(db: Session, project_id: int) -> dict[tuple[str, str], PolicyException]:
    """Exceptions in force for a project, by (policy name, rule ID)."""
    rows = db.scalars(
        select(PolicyException).where(PolicyException.project_id == project_id, in_force())
    )
    return {(row.policy_name, row.rule_id): row for row in rows}


def _reopen_findings(db: Session, exception_ids: Sequence[int]) -> int:
    """Findings waived by these exceptions are open again until an evaluation says otherwise."""
    if not exception_ids:
        return 0
    result = db.execute(
        update(Finding)
        .where(Finding.state == "excepted", Finding.exception_id.in_(exception_ids))
        .values(state="open", exception_id=None)
    )
    return int(getattr(result, "rowcount", 0) or 0)


def expire_due(db: Session) -> tuple[list[int], int]:
    """Mark exceptions past ``expires_at`` expired and reopen their findings. Caller commits.

    Returns the expired exceptions' IDs and how many findings were reopened.
    """
    expired = list(
        db.scalars(
            update(PolicyException)
            .where(
                PolicyException.state.in_(("requested", "approved")),
                PolicyException.expires_at <= func.now(),
            )
            .values(state="expired", ended_at=func.now())
            .returning(PolicyException.id)
        )
    )
    reopened = _reopen_findings(db, expired)
    for exception_id in expired:
        audit.record(
            db,
            actor=SYSTEM_ACTOR,
            action="policy_exception.expired",
            resource_type="policy_exception",
            resource_id=str(exception_id),
        )
    return expired, reopened


@dataclass(frozen=True)
class Requested:
    instance_id: int
    project_id: int
    policy_name: str
    rule_id: str
    justification: str
    expires_at: datetime


def _details(row: PolicyException, **extra: Any) -> dict[str, Any]:
    return {
        "project_id": row.project_id,
        "policy_name": row.policy_name,
        "rule_id": row.rule_id,
        "expires_at": row.expires_at.isoformat(),
        **extra,
    }


def request(db: Session, requested: Requested, user_id: int, actor: str) -> PolicyException:
    """Store a request. Raises ExceptionConflict when one is already requested or approved."""
    expire_due(db)
    existing = db.scalars(
        select(PolicyException.id).where(
            PolicyException.project_id == requested.project_id,
            PolicyException.policy_name == requested.policy_name,
            PolicyException.rule_id == requested.rule_id,
            PolicyException.state.in_(("requested", "approved")),
        )
    ).first()
    if existing is not None:
        raise ExceptionConflict(
            f"Exception {existing} is already requested or approved for this project and rule"
        )
    row = PolicyException(
        instance_id=requested.instance_id,
        project_id=requested.project_id,
        policy_name=requested.policy_name,
        rule_id=requested.rule_id,
        justification=requested.justification,
        expires_at=requested.expires_at,
        requested_by_user_id=user_id,
    )
    db.add(row)
    db.flush()
    db.refresh(row)
    audit.record(
        db,
        actor=actor,
        action="policy_exception.requested",
        resource_type="policy_exception",
        resource_id=str(row.id),
        details=_details(row),
    )
    return row


def _current(db: Session, row: PolicyException) -> str:
    """The state, counting a past ``expires_at`` as expired."""
    expired = db.execute(select(func.now() >= row.expires_at)).scalar_one()
    if row.state in ("requested", "approved") and expired:
        return "expired"
    return row.state


def decide(
    db: Session,
    row: PolicyException,
    decision: Literal["approved", "rejected"],
    user_id: int,
    actor: str,
    note: str | None,
) -> PolicyException:
    """Approve or reject a request. The decider must not be the requester."""
    state = _current(db, row)
    if state != "requested":
        raise ExceptionConflict(f"Only a requested exception can be decided; this one is {state}")
    if user_id == row.requested_by_user_id:
        raise PermissionError("An exception must be approved or rejected by someone else")
    now = db.execute(select(func.now())).scalar_one()
    row.state = decision
    row.decided_by_user_id = user_id
    row.decided_at = now
    row.decision_note = note
    db.flush()
    audit.record(
        db,
        actor=actor,
        action=f"policy_exception.{decision}",
        resource_type="policy_exception",
        resource_id=str(row.id),
        details=_details(row, requested_by_user_id=row.requested_by_user_id),
    )
    return row


def revoke(
    db: Session, row: PolicyException, user_id: int, actor: str, note: str
) -> PolicyException:
    """End a requested or approved exception now; its findings are open again."""
    state = _current(db, row)
    if state not in ("requested", "approved"):
        raise ExceptionConflict(
            f"Only a requested or approved exception can be revoked; this one is {state}"
        )
    now = db.execute(select(func.now())).scalar_one()
    row.state = "revoked"
    row.ended_by_user_id = user_id
    row.ended_at = now
    row.end_note = note
    db.flush()
    reopened = _reopen_findings(db, [row.id])
    audit.record(
        db,
        actor=actor,
        action="policy_exception.revoked",
        resource_type="policy_exception",
        resource_id=str(row.id),
        details=_details(row, previous_state=state, findings_reopened=reopened),
    )
    return row


def locked(db: Session, exception_id: int) -> PolicyException | None:
    """The exception, locked for a transition."""
    return db.get(PolicyException, exception_id, with_for_update=True, populate_existing=True)
