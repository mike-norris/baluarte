"""Release practices, where they apply, and what each project's releases show (RG.2).

Reading needs ``findings:read``; creating, changing, deleting, assigning and unassigning need
``policy:write`` and the CSRF header, and are audited (docs/RELEASE_GOVERNANCE.md#api).
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte.api.v1.templates import _connection
from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.db.models import GitLabInstance, ReleasePractice, ReleasePracticeAssignment
from baluarte.policy.assignments import Target, TargetType, find_target
from baluarte.policy.schema import NAME_PATTERN
from baluarte.releases import practices
from baluarte.releases.practices import (
    PRESET_LABELS,
    PRESETS,
    PracticeInvalid,
    PracticeSettings,
    Preset,
    ProjectPractice,
    Status,
)

router = APIRouter(tags=["release practices"])

Name = Annotated[str, Path(pattern=NAME_PATTERN, max_length=63)]
Reader = Annotated[Principal, Depends(require(Permission.FINDINGS_READ))]
Writer = Annotated[Principal, Depends(require(Permission.POLICY_WRITE))]
Description = Annotated[str, Field(max_length=2000)]


class PresetOut(BaseModel):
    preset: Preset
    label: str
    # The preset's defaults; null for custom.
    settings: PracticeSettings | None


class PracticeOut(BaseModel):
    name: str
    preset: Preset
    preset_label: str
    description: str | None
    settings: PracticeSettings
    assignments: int
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime


class PracticeList(BaseModel):
    presets: list[PresetOut]
    practices: list[PracticeOut]
    window_days: int


class PracticeIn(BaseModel):
    name: Annotated[str, Field(pattern=NAME_PATTERN, max_length=63)]
    preset: Preset
    description: Description | None = None
    # Parameters over the preset's defaults; a custom practice needs ``production_from``.
    settings: dict[str, Any] | None = None


class PracticeUpdate(BaseModel):
    description: Description | None = None
    settings: dict[str, Any] | None = None


class AssignmentIn(BaseModel):
    connection_id: Annotated[
        int | None, Field(gt=0, description="Omitted for the connection the request works on")
    ] = None
    name: Annotated[str, Field(pattern=NAME_PATTERN, max_length=63)]
    target_type: TargetType
    target_id: Annotated[
        int | None,
        Field(gt=0, description="Provider ID of the group or project; omitted for the connection"),
    ] = None

    @model_validator(mode="after")
    def _target(self) -> "AssignmentIn":
        if (self.target_type == "instance") != (self.target_id is None):
            raise ValueError("target_id is required for a group or project, and omitted otherwise")
        return self


class AssignmentOut(BaseModel):
    id: int
    connection_id: int
    name: str
    target: Target
    assigned_by: str
    created_at: datetime


class AssignmentList(BaseModel):
    assignments: list[AssignmentOut]


class ProjectList(BaseModel):
    window_days: int
    counts: dict[str, int]
    projects: list[ProjectPractice]


def _invalid(error: PracticeInvalid) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error))


def _out(db: Session, practice: ReleasePractice) -> PracticeOut:
    return PracticeOut(
        name=practice.name,
        preset=practice.preset,  # type: ignore[arg-type]
        preset_label=PRESET_LABELS[practice.preset],
        description=practice.description,
        settings=PracticeSettings.model_validate(practice.settings),
        assignments=practices.assignments_of(db, practice),
        created_by=practice.created_by,
        created_at=practice.created_at,
        updated_by=practice.updated_by,
        updated_at=practice.updated_at,
    )


def _found(db: Session, name: str) -> ReleasePractice:
    practice = practices.find(db, name)
    if practice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such release practice")
    return practice


@router.get(
    "/release-practices",
    response_model=PracticeList,
    summary="Release practices and the presets they start from",
)
def list_practices(_: Reader, db: Annotated[Session, Depends(database)]) -> PracticeList:
    rows = db.scalars(select(ReleasePractice).order_by(ReleasePractice.name)).all()
    return PracticeList(
        presets=[
            PresetOut(preset=preset, label=label, settings=PRESETS.get(preset))  # type: ignore[arg-type]
            for preset, label in PRESET_LABELS.items()
        ],
        practices=[_out(db, row) for row in rows],
        window_days=practices.WINDOW_DAYS,
    )


@router.post(
    "/release-practices",
    response_model=PracticeOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a release practice",
    description="From a preset, with parameters over its defaults, or custom. 422 when the "
    "parameters are invalid or the name is taken. Requires the `X-CSRF-Token` header. Audited.",
    responses={422: {"description": "Invalid practice"}},
)
def create_practice(
    body: PracticeIn, principal: Writer, db: Annotated[Session, Depends(database)]
) -> PracticeOut:
    try:
        practice = practices.create(
            db, body.name, body.preset, body.settings, body.description, principal.actor
        )
    except PracticeInvalid as error:
        raise _invalid(error) from None
    db.commit()
    return _out(db, practice)


@router.put(
    "/release-practices/{name}",
    response_model=PracticeOut,
    summary="Change a release practice's parameters and description",
    description="The preset stays. Requires the `X-CSRF-Token` header. Audited with the values "
    "before and after.",
    responses={404: {"description": "No such practice"}, 422: {"description": "Invalid"}},
)
def update_practice(
    name: Name,
    body: PracticeUpdate,
    principal: Writer,
    db: Annotated[Session, Depends(database)],
) -> PracticeOut:
    practice = _found(db, name)
    try:
        practices.update(db, practice, body.settings, body.description, principal.actor)
    except PracticeInvalid as error:
        raise _invalid(error) from None
    db.commit()
    return _out(db, practice)


@router.delete(
    "/release-practices/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a release practice that applies nowhere",
    description="409 while it is assigned. Requires the `X-CSRF-Token` header. Audited.",
    responses={404: {"description": "No such practice"}, 409: {"description": "Still assigned"}},
)
def delete_practice(
    name: Name, principal: Writer, db: Annotated[Session, Depends(database)]
) -> Response:
    practice = _found(db, name)
    try:
        practices.delete(db, practice, principal.actor)
    except PracticeInvalid as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _assignment_out(db: Session, row: ReleasePracticeAssignment) -> AssignmentOut:
    instance = db.get_one(GitLabInstance, row.instance_id)
    practice = db.get_one(ReleasePractice, row.practice_id)
    target = find_target(db, instance, row.target_type, row.target_id)  # type: ignore[arg-type]
    return AssignmentOut(
        id=row.id,
        connection_id=row.instance_id,
        name=practice.name,
        target=target
        or Target(
            target_type=row.target_type,  # type: ignore[arg-type]
            target_id=row.target_id,
            path="(not in the inventory)",
            state=None,
        ),
        assigned_by=row.assigned_by,
        created_at=row.created_at,
    )


@router.get(
    "/release-practice-assignments",
    response_model=AssignmentList,
    summary="Where release practices are assigned",
)
def list_assignments(_: Reader, db: Annotated[Session, Depends(database)]) -> AssignmentList:
    rows = db.scalars(
        select(ReleasePracticeAssignment).order_by(ReleasePracticeAssignment.id)
    ).all()
    return AssignmentList(assignments=[_assignment_out(db, row) for row in rows])


@router.post(
    "/release-practice-assignments",
    response_model=AssignmentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Assign a release practice to a connection, group or project",
    description="Replaces the target's practice. Requires the `X-CSRF-Token` header. Audited.",
    responses={
        404: {"description": "No such connection, practice or target"},
        409: {"description": "The target is not active in the inventory"},
    },
)
def create_assignment(
    body: AssignmentIn, principal: Writer, db: Annotated[Session, Depends(database)]
) -> AssignmentOut:
    instance = _connection(db, body.connection_id)
    practice = _found(db, body.name)
    target_id = instance.id if body.target_type == "instance" else body.target_id
    target = find_target(db, instance, body.target_type, int(target_id or 0))
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The target is not in the inventory")
    if target.state not in (None, "active"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"The target is {target.state}")
    assignment = practices.assign(db, instance, practice, target, principal.actor)
    db.commit()
    return _assignment_out(db, assignment)


@router.delete(
    "/release-practice-assignments/{assignment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a release practice assignment",
    description="Requires the `X-CSRF-Token` header. Audited.",
    responses={404: {"description": "No such assignment"}},
)
def remove_assignment(
    assignment_id: Annotated[int, Path(ge=1)],
    principal: Writer,
    db: Annotated[Session, Depends(database)],
) -> Response:
    row = db.scalars(
        select(ReleasePracticeAssignment).where(ReleasePracticeAssignment.id == assignment_id)
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such assignment")
    practices.unassign(db, row, principal.actor)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/release-practices/projects",
    response_model=ProjectList,
    summary="Each project's release practice and what its releases show",
    description="Every active project in monitoring scope on the connection: its practice, the "
    "connection, group or project that decided it, and its production deployments in the last "
    "`window_days` by what they deployed. `status` is `follows`, `differs` (a deployment of a "
    "ref the practice does not expect), `unknown` (a ref whose kind is not recorded), "
    "`no_releases` or `unassigned`.",
)
def list_projects(
    _: Reader,
    db: Annotated[Session, Depends(database)],
    connection_id: Annotated[int | None, Query(gt=0)] = None,
    status_filter: Annotated[Status | None, Query(alias="status")] = None,
    practice: Annotated[str | None, Query(max_length=63)] = None,
) -> ProjectList:
    instance = _connection(db, connection_id)
    rows = practices.overview(db, instance)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    return ProjectList(
        window_days=practices.WINDOW_DAYS,
        counts=counts,
        projects=[
            row
            for row in rows
            if (status_filter is None or row.status == status_filter)
            and (practice is None or row.practice == practice)
        ],
    )
