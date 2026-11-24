"""Template drift, alignment and template exceptions (roadmap M14.2).

Reading drift needs ``findings:read``. Exceptions are requested with ``exception:request`` and
approved, rejected or revoked with ``exception:approve`` by someone other than the requester, as
for policy exceptions; every change needs the CSRF header and is audited
(docs/CONFIGURATION_TEMPLATES.md#drift).
"""

from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.db.models import (
    MAX_EXCEPTION_DAYS,
    AppUser,
    GitLabInstance,
    GitLabProject,
    TemplateException,
)
from baluarte.policy.evaluation.project import capability_statuses, collection_contexts
from baluarte.templates import exceptions
from baluarte.templates.drift import Alignment, ProjectDrift, alignment, project_drift
from baluarte.templates.exceptions import EXPIRING_WITHIN, ExceptionConflict, Requested
from baluarte.templates.resolve import effective_settings
from baluarte.templates.schema import SETTING_KEYS
from baluarte.workspaces import current_connection, settings_of

router = APIRouter(tags=["templates"])

Reader = Annotated[Principal, Depends(require(Permission.FINDINGS_READ))]
Note = Annotated[str, Field(min_length=1, max_length=exceptions.MAX_NOTE)]


def _project(db: Session, project_id: int) -> tuple[GitLabInstance, GitLabProject]:
    project = db.scalars(select(GitLabProject).where(GitLabProject.id == project_id)).first()
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such project")
    return db.get_one(GitLabInstance, project.instance_id), project


@router.get(
    "/templates/drift/{project_id}",
    response_model=ProjectDrift,
    summary="A project's drift from its effective template settings",
    description="Each effective setting judged against collected evidence: `aligned`, `drift`, "
    "`excepted` (drift waived by an approved exception), `unknown` (the evidence cannot tell) "
    "or `unsupported` (the provider or plan has no such setting), with the reason. "
    "`project_id` is Baluarte's project ID.",
    responses={404: {"description": "No such project in this workspace"}},
)
def read_project_drift(
    project_id: Annotated[int, Path(ge=1)], _: Reader, db: Annotated[Session, Depends(database)]
) -> ProjectDrift:
    instance, project = _project(db, project_id)
    collections = collection_contexts(db, instance.id, settings_of(instance))
    return project_drift(db, instance, project, collections, capability_statuses(db, instance.id))


@router.get(
    "/templates/alignment",
    response_model=Alignment,
    summary="Template alignment across the connection",
    description="For the connection the request works on: how many active, monitored, "
    "unarchived projects have templates and are aligned, drifting or cannot be told, with "
    "setting counts by template and by top-level group, the projects needing attention, and "
    "approved exceptions ending within 14 days. Computed on request.",
    responses={404: {"description": "No connection is configured"}},
)
def read_alignment(_: Reader, db: Annotated[Session, Depends(database)]) -> Alignment:
    instance = current_connection(db)
    if instance is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No connection is configured")
    return alignment(db, instance, settings_of(instance))


# --- exceptions ---


Setting = Literal[SETTING_KEYS]  # type: ignore[valid-type]


class ExceptionIn(BaseModel):
    project_id: Annotated[int, Field(gt=0, description="Baluarte's project ID")]
    setting: Setting
    justification: Annotated[str, Field(min_length=10, max_length=exceptions.MAX_JUSTIFICATION)]
    expires_at: Annotated[
        datetime,
        Field(description=f"With a time zone; at most {MAX_EXCEPTION_DAYS} days ahead."),
    ]


class DecisionIn(BaseModel):
    note: Note | None = None


class NoteIn(BaseModel):
    note: Note


class UserRef(BaseModel):
    user_id: int
    display_name: str | None


class ExceptionOut(BaseModel):
    id: int
    connection_id: int
    project_id: int
    project_path: str
    setting: str
    justification: str
    # The stored state, with a requested or approved exception past its end shown as expired.
    state: str
    expiring: bool
    requested_by: UserRef
    requested_at: datetime
    expires_at: datetime
    decided_by: UserRef | None
    decided_at: datetime | None
    decision_note: str | None
    development_decision: bool
    ended_by: UserRef | None
    ended_at: datetime | None
    end_note: str | None


class ExceptionList(BaseModel):
    exceptions: list[ExceptionOut]


def _outs(db: Session, rows: list[TemplateException]) -> list[ExceptionOut]:
    now: datetime = db.execute(select(func.now())).scalar_one()
    ids = {
        user
        for row in rows
        for user in (row.requested_by_user_id, row.decided_by_user_id, row.ended_by_user_id)
        if user is not None
    }
    names = (
        dict(db.execute(select(AppUser.id, AppUser.display_name).where(AppUser.id.in_(ids))).all())
        if ids
        else {}
    )
    paths = (
        dict(
            db.execute(
                select(GitLabProject.id, GitLabProject.path_with_namespace).where(
                    GitLabProject.id.in_({row.project_id for row in rows})
                )
            ).all()
        )
        if rows
        else {}
    )

    def user(user_id: int | None) -> UserRef | None:
        return UserRef(user_id=user_id, display_name=names.get(user_id)) if user_id else None

    found = []
    for row in rows:
        open_ = row.state in ("requested", "approved")
        state = "expired" if open_ and row.expires_at <= now else row.state
        requested_by = user(row.requested_by_user_id)
        if requested_by is None:  # always set; narrows the type
            raise AssertionError("an exception always has a requester")
        found.append(
            ExceptionOut(
                id=row.id,
                connection_id=row.instance_id,
                project_id=row.project_id,
                project_path=paths.get(row.project_id, "(not in the inventory)"),
                setting=row.setting,
                justification=row.justification,
                state=state,
                expiring=state == "approved" and row.expires_at - now <= EXPIRING_WITHIN,
                requested_by=requested_by,
                requested_at=row.requested_at,
                expires_at=row.expires_at,
                decided_by=user(row.decided_by_user_id),
                decided_at=row.decided_at,
                decision_note=row.decision_note,
                development_decision=row.development_decision,
                ended_by=user(row.ended_by_user_id),
                ended_at=row.ended_at,
                end_note=row.end_note,
            )
        )
    return found


@router.get(
    "/template-exceptions",
    response_model=ExceptionList,
    summary="Template exceptions",
    description="Newest first, at most 500. Filter by `state` (requested, approved, rejected, "
    "revoked, expired) or `project_id`.",
)
def list_exceptions(
    _: Reader,
    db: Annotated[Session, Depends(database)],
    state: Annotated[
        Literal["requested", "approved", "rejected", "revoked", "expired"] | None, Query()
    ] = None,
    project_id: Annotated[int | None, Query(gt=0)] = None,
) -> ExceptionList:
    statement = select(TemplateException).order_by(TemplateException.id.desc()).limit(500)
    if project_id is not None:
        statement = statement.where(TemplateException.project_id == project_id)
    rows = list(db.scalars(statement))
    found = _outs(db, rows)
    if state is not None:
        found = [row for row in found if row.state == state]
    return ExceptionList(exceptions=found)


@router.post(
    "/template-exceptions",
    response_model=ExceptionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Request an exception for one template setting of one project",
    description="Someone other than the requester must approve it. Requires the `X-CSRF-Token` "
    "header. Audited.",
    responses={
        404: {"description": "No such project, or no template sets that setting for it"},
        409: {"description": "An exception is already requested or approved for it"},
        422: {"description": "Invalid body, or `expires_at` out of range"},
    },
)
def request_exception(
    body: ExceptionIn,
    principal: Annotated[Principal, Depends(require(Permission.EXCEPTION_REQUEST))],
    db: Annotated[Session, Depends(database)],
) -> ExceptionOut:
    if body.expires_at.tzinfo is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "expires_at must include a time zone"
        )
    now: datetime = db.execute(select(func.now())).scalar_one()
    if not now < body.expires_at <= now + timedelta(days=MAX_EXCEPTION_DAYS):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"expires_at must be in the future and at most {MAX_EXCEPTION_DAYS} days ahead",
        )
    instance, project = _project(db, body.project_id)
    effective = effective_settings(db, instance, project)
    if body.setting not in {setting.key for setting in effective.settings}:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "No template sets that setting for this project"
        )
    requested = Requested(
        instance_id=instance.id,
        project_id=project.id,
        setting=body.setting,
        justification=body.justification,
        expires_at=body.expires_at,
    )
    try:
        row = exceptions.request(db, requested, principal.user_id, principal.actor)
    except ExceptionConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    return _outs(db, [row])[0]


def _transition(
    db: Session,
    exception_id: int,
    principal: Principal,
    action: Literal["approved", "rejected", "revoked"],
    note: str | None,
) -> ExceptionOut:
    visible = db.scalars(
        select(TemplateException.id).where(TemplateException.id == exception_id)
    ).first()
    row = exceptions.locked(db, exception_id) if visible is not None else None
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such exception")
    try:
        if action == "revoked":
            exceptions.revoke(db, row, principal.user_id, principal.actor, note or "")
        else:
            exceptions.decide(db, row, action, principal.user_id, principal.actor, note)
    except ExceptionConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    db.commit()
    return _outs(db, [row])[0]


_TRANSITIONS: dict[int | str, dict[str, str]] = {
    403: {"description": "Missing permission, CSRF failure, or deciding one's own request"},
    404: {"description": "No such exception"},
    409: {"description": "Not allowed in the exception's current state"},
}
Approver = Annotated[Principal, Depends(require(Permission.EXCEPTION_APPROVE))]
ExceptionId = Annotated[int, Path(gt=0)]


@router.post(
    "/template-exceptions/{exception_id}/approve",
    response_model=ExceptionOut,
    summary="Approve a requested template exception",
    description="Someone other than the requester, before `expires_at`. Requires the "
    "`X-CSRF-Token` header.",
    responses=_TRANSITIONS,
)
def approve_exception(
    exception_id: ExceptionId,
    body: DecisionIn,
    principal: Approver,
    db: Annotated[Session, Depends(database)],
) -> ExceptionOut:
    return _transition(db, exception_id, principal, "approved", body.note)


@router.post(
    "/template-exceptions/{exception_id}/reject",
    response_model=ExceptionOut,
    summary="Reject a requested template exception",
    description="With a note. Requires the `X-CSRF-Token` header.",
    responses=_TRANSITIONS,
)
def reject_exception(
    exception_id: ExceptionId,
    body: NoteIn,
    principal: Approver,
    db: Annotated[Session, Depends(database)],
) -> ExceptionOut:
    return _transition(db, exception_id, principal, "rejected", body.note)


@router.post(
    "/template-exceptions/{exception_id}/revoke",
    response_model=ExceptionOut,
    summary="Revoke a requested or approved template exception",
    description="With a note; its drift shows again at once. Requires the `X-CSRF-Token` header.",
    responses=_TRANSITIONS,
)
def revoke_exception(
    exception_id: ExceptionId,
    body: NoteIn,
    principal: Approver,
    db: Annotated[Session, Depends(database)],
) -> ExceptionOut:
    return _transition(db, exception_id, principal, "revoked", body.note)
