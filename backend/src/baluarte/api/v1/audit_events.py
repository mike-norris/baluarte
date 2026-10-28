"""Read access to the audit trail, with each event's digest for independent verification."""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime, BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.db.models import AuditEvent

router = APIRouter(prefix="/audit-events", tags=["audit"])


class AuditEventOut(BaseModel):
    id: int
    occurred_at: datetime
    actor: str
    action: str
    resource_type: str
    resource_id: str | None
    details: dict[str, Any]
    details_digest: str


class AuditEventPage(BaseModel):
    items: list[AuditEventOut]
    next_before_id: int | None


@router.get("", response_model=AuditEventPage, summary="List audit events, newest first")
def list_audit_events(
    _: Annotated[Principal, Depends(require(Permission.AUDIT_LOG_READ))],
    db: Annotated[Session, Depends(database)],
    action: Annotated[str | None, Query(max_length=100)] = None,
    actor: Annotated[str | None, Query(max_length=300)] = None,
    since: Annotated[AwareDatetime | None, Query(description="Inclusive, with time zone")] = None,
    until: Annotated[AwareDatetime | None, Query(description="Exclusive, with time zone")] = None,
    before_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AuditEventPage:
    statement = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit + 1)
    if action is not None:
        statement = statement.where(AuditEvent.action == action)
    if actor is not None:
        statement = statement.where(AuditEvent.actor == actor)
    if since is not None:
        statement = statement.where(AuditEvent.occurred_at >= since)
    if until is not None:
        statement = statement.where(AuditEvent.occurred_at < until)
    if before_id is not None:
        statement = statement.where(AuditEvent.id < before_id)
    rows = db.scalars(statement).all()
    items = [AuditEventOut.model_validate(row, from_attributes=True) for row in rows[:limit]]
    return AuditEventPage(items=items, next_before_id=items[-1].id if len(rows) > limit else None)
