"""Read-only view of background jobs, so operators can see queued, retrying and failed work.

Payloads are not returned: they are inputs for handlers, not operator information.
"""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.db.models import CollectionJob

router = APIRouter(prefix="/jobs", tags=["jobs"])

JobState = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class Job(BaseModel):
    id: int
    kind: str
    state: JobState
    attempt: int
    max_attempts: int
    run_after: datetime
    lease_until: datetime | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class JobPage(BaseModel):
    items: list[Job]
    next_before_id: int | None


@router.get("", response_model=JobPage, summary="List background jobs, newest first")
def list_jobs(
    _: Annotated[Principal, Depends(require(Permission.JOBS_READ))],
    db: Annotated[Session, Depends(database)],
    state: JobState | None = None,
    kind: Annotated[str | None, Query(max_length=100)] = None,
    before_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> JobPage:
    statement = select(CollectionJob).order_by(CollectionJob.id.desc()).limit(limit + 1)
    if state is not None:
        statement = statement.where(CollectionJob.state == state)
    if kind is not None:
        statement = statement.where(CollectionJob.kind == kind)
    if before_id is not None:
        statement = statement.where(CollectionJob.id < before_id)
    rows = db.scalars(statement).all()
    items = [Job.model_validate(row, from_attributes=True) for row in rows[:limit]]
    return JobPage(items=items, next_before_id=items[-1].id if len(rows) > limit else None)
