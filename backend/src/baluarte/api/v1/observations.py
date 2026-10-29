"""Observation-time evidence: what was read from GitLab, when, and whether its digest holds."""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte.api.v1.gitlab import _configured_url
from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.db.models import GitLabGroup, GitLabProject, Observation
from baluarte.evidence.observations import TargetColumn, verify
from baluarte.integrations.gitlab.capabilities import CAPABILITY_SOURCE
from baluarte.integrations.gitlab.instances import instance_id_for

router = APIRouter(prefix="/gitlab", tags=["evidence"])

_DOC = (
    "Newest first. A new observation is stored only when the status, payload or error changes, "
    "so the newest one is the current evidence. `digest_valid` recomputes the SHA-256 of the "
    "payload's canonical JSON and compares it with the stored digest."
)


class ObservationOut(BaseModel):
    id: int
    source: str
    source_ref: str | None
    collected_at: datetime
    collection_status: str
    payload: dict[str, Any] | None
    payload_digest: str | None
    digest_valid: bool
    error: str | None
    collection_run_id: int | None
    collection_job_id: int | None


class ObservationPage(BaseModel):
    items: list[ObservationOut]
    next_before_id: int | None


def _out(row: Observation) -> ObservationOut:
    return ObservationOut(
        id=row.id,
        source=row.source,
        source_ref=row.source_ref,
        collected_at=row.collected_at,
        collection_status=row.collection_status,
        payload=row.payload,
        payload_digest=row.payload_digest,
        digest_valid=verify(row),
        error=row.error,
        collection_run_id=row.collection_run_id,
        collection_job_id=row.collection_job_id,
    )


def _page(
    db: Session,
    instance_id: int,
    column: TargetColumn | None,
    target_id: int | None,
    before_id: int | None,
    limit: int,
) -> ObservationPage:
    statement = select(Observation).where(Observation.instance_id == instance_id)
    if column is None:
        statement = statement.where(
            Observation.project_id.is_(None),
            Observation.group_id.is_(None),
            Observation.source == CAPABILITY_SOURCE,
        )
    else:
        statement = statement.where(getattr(Observation, column) == target_id)
    if before_id is not None:
        statement = statement.where(Observation.id < before_id)
    rows = db.scalars(statement.order_by(Observation.id.desc()).limit(limit + 1)).all()
    items = [_out(row) for row in rows[:limit]]
    return ObservationPage(items=items, next_before_id=items[-1].id if len(rows) > limit else None)


def _instance_id(db: Session) -> int | None:
    url = _configured_url()
    return instance_id_for(db, url) if url else None


Before = Annotated[int | None, Query(gt=0)]
Limit = Annotated[int, Query(ge=1, le=200)]


@router.get(
    "/projects/{gitlab_id}/observations",
    response_model=ObservationPage,
    summary="Evidence observed for a project",
    description=_DOC,
    responses={404: {"description": "The project is not in the inventory"}},
)
def project_observations(
    gitlab_id: Annotated[int, Path(gt=0)],
    _: Annotated[Principal, Depends(require(Permission.EVIDENCE_READ))],
    db: Annotated[Session, Depends(database)],
    before_id: Before = None,
    limit: Limit = 50,
) -> ObservationPage:
    instance_id = _instance_id(db)
    project_id = (
        db.scalars(
            select(GitLabProject.id).where(
                GitLabProject.instance_id == instance_id, GitLabProject.gitlab_id == gitlab_id
            )
        ).first()
        if instance_id is not None
        else None
    )
    if instance_id is None or project_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such project in the inventory")
    return _page(db, instance_id, "project_id", project_id, before_id, limit)


@router.get(
    "/groups/{gitlab_id}/observations",
    response_model=ObservationPage,
    summary="Evidence observed for a group",
    description=_DOC,
    responses={404: {"description": "The group is not in the inventory"}},
)
def group_observations(
    gitlab_id: Annotated[int, Path(gt=0)],
    _: Annotated[Principal, Depends(require(Permission.EVIDENCE_READ))],
    db: Annotated[Session, Depends(database)],
    before_id: Before = None,
    limit: Limit = 50,
) -> ObservationPage:
    instance_id = _instance_id(db)
    group_id = (
        db.scalars(
            select(GitLabGroup.id).where(
                GitLabGroup.instance_id == instance_id, GitLabGroup.gitlab_id == gitlab_id
            )
        ).first()
        if instance_id is not None
        else None
    )
    if instance_id is None or group_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such group in the inventory")
    return _page(db, instance_id, "group_id", group_id, before_id, limit)


@router.get(
    "/instance/observations",
    response_model=ObservationPage,
    summary="Evidence observed for the GitLab instance (capability snapshots)",
    description=_DOC + " Empty when GitLab is not configured or never contacted.",
)
def instance_observations(
    _: Annotated[Principal, Depends(require(Permission.EVIDENCE_READ))],
    db: Annotated[Session, Depends(database)],
    before_id: Before = None,
    limit: Limit = 50,
) -> ObservationPage:
    instance_id = _instance_id(db)
    if instance_id is None:
        return ObservationPage(items=[], next_before_id=None)
    return _page(db, instance_id, None, None, before_id, limit)
