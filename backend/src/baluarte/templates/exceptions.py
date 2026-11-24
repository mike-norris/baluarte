"""Template exceptions: time-limited waivers of one template setting for one project (M14.2).

The same lifecycle as policy exceptions (``baluarte.policy.exceptions``), enforced again by a
database trigger:

- ``requested`` -> ``approved`` or ``rejected``, by a user holding ``exception:approve`` who is
  not the requester (in development mode, the requester may decide their own);
- ``requested`` or ``approved`` -> ``revoked`` by such a user, or ``expired`` once
  ``expires_at`` has passed (database clock).

An approved, unexpired exception turns ``drift`` for its project and setting into ``excepted``.
``unknown`` and ``unsupported`` are never excepted: an exception does not stand in for missing
evidence. Expiry takes effect at ``expires_at`` whether or not the row is marked yet.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import ColumnElement, and_, func, select, update
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.db.models import TemplateException
from baluarte.policy.exceptions import ExceptionConflict
from baluarte.settings import own_decision_refused
from baluarte.workspaces import workspace_of

SYSTEM_ACTOR = "system:template-exceptions"
MAX_JUSTIFICATION = 4000
MAX_NOTE = 2000
# An approved exception this close to its end is flagged as expiring.
EXPIRING_WITHIN = timedelta(days=14)

__all__ = ["ExceptionConflict"]


def in_force() -> ColumnElement[bool]:
    """SQL: the exception currently waives drift."""
    return and_(TemplateException.state == "approved", TemplateException.expires_at > func.now())


def open_for_project(db: Session, project_id: int) -> dict[str, TemplateException]:
    """Requested or approved, unexpired exceptions of a project, by setting."""
    rows = db.scalars(
        select(TemplateException).where(
            TemplateException.project_id == project_id,
            TemplateException.state.in_(("requested", "approved")),
            TemplateException.expires_at > func.now(),
        )
    )
    return {row.setting: row for row in rows}


def expire_due(db: Session) -> list[int]:
    """Mark exceptions past ``expires_at`` expired. The caller commits."""
    expired = list(
        db.scalars(
            update(TemplateException)
            .where(
                TemplateException.state.in_(("requested", "approved")),
                TemplateException.expires_at <= func.now(),
            )
            .values(state="expired", ended_at=func.now())
            .returning(TemplateException.id)
        )
    )
    for exception_id in expired:
        audit.record(
            db,
            actor=SYSTEM_ACTOR,
            action="template_exception.expired",
            resource_type="template_exception",
            resource_id=str(exception_id),
        )
    return expired


@dataclass(frozen=True)
class Requested:
    instance_id: int
    project_id: int
    setting: str
    justification: str
    expires_at: datetime


def _details(row: TemplateException, **extra: Any) -> dict[str, Any]:
    return {
        "project_id": row.project_id,
        "setting": row.setting,
        "expires_at": row.expires_at.isoformat(),
        **({"development_decision": True} if row.development_decision else {}),
        **extra,
    }


def request(db: Session, requested: Requested, user_id: int, actor: str) -> TemplateException:
    """Store a request. Raises ExceptionConflict when one is already requested or approved."""
    expire_due(db)
    existing = db.scalars(
        select(TemplateException.id).where(
            TemplateException.project_id == requested.project_id,
            TemplateException.setting == requested.setting,
            TemplateException.state.in_(("requested", "approved")),
        )
    ).first()
    if existing is not None:
        raise ExceptionConflict(
            f"Exception {existing} is already requested or approved for this project and setting"
        )
    row = TemplateException(
        workspace_id=workspace_of(db, requested.instance_id),
        instance_id=requested.instance_id,
        project_id=requested.project_id,
        setting=requested.setting,
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
        action="template_exception.requested",
        resource_type="template_exception",
        resource_id=str(row.id),
        details=_details(row),
    )
    return row


def current_state(db: Session, row: TemplateException) -> str:
    """The state, counting a past ``expires_at`` as expired."""
    expired = db.execute(select(func.now() >= row.expires_at)).scalar_one()
    if row.state in ("requested", "approved") and expired:
        return "expired"
    return row.state


def decide(
    db: Session,
    row: TemplateException,
    decision: Literal["approved", "rejected"],
    user_id: int,
    actor: str,
    note: str | None,
) -> TemplateException:
    """Approve or reject a request. The decider must not be the requester."""
    state = current_state(db, row)
    if state != "requested":
        raise ExceptionConflict(f"Only a requested exception can be decided; this one is {state}")
    if own_decision_refused(row.requested_by_user_id, user_id):
        raise PermissionError("An exception must be approved or rejected by someone else")
    now = db.execute(select(func.now())).scalar_one()
    row.state = decision
    row.decided_by_user_id = user_id
    row.development_decision = user_id == row.requested_by_user_id
    row.decided_at = now
    row.decision_note = note
    db.flush()
    audit.record(
        db,
        actor=actor,
        action=f"template_exception.{decision}",
        resource_type="template_exception",
        resource_id=str(row.id),
        details=_details(row, requested_by_user_id=row.requested_by_user_id),
    )
    return row


def revoke(
    db: Session, row: TemplateException, user_id: int, actor: str, note: str
) -> TemplateException:
    """End a requested or approved exception now."""
    state = current_state(db, row)
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
    audit.record(
        db,
        actor=actor,
        action="template_exception.revoked",
        resource_type="template_exception",
        resource_id=str(row.id),
        details=_details(row, previous_state=state),
    )
    return row


def locked(db: Session, exception_id: int) -> TemplateException | None:
    return db.get(TemplateException, exception_id, with_for_update=True, populate_existing=True)
