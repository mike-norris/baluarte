"""Policy assignments and the policies in force for a group or project (BAL-018).

Assigning and unassigning need ``policy:write`` and are audited. Reading needs
``findings:read``, like reading the policies themselves.
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.db.models import GitLabGroup, GitLabInstance, GitLabProject, Policy, PolicyAssignment
from baluarte.integrations.gitlab.instances import instance_for
from baluarte.integrations.gitlab.settings import load_gitlab_settings
from baluarte.policy.assignments import (
    Resolution,
    Target,
    TargetType,
    assign,
    find_target,
    resolve_group,
    resolve_project,
)
from baluarte.policy.schema import NAME_PATTERN
from baluarte.settings import ConfigurationError

router = APIRouter(tags=["policies"])


class AssignmentIn(BaseModel):
    policy_name: Annotated[str, Field(pattern=NAME_PATTERN, max_length=63)]
    version: Annotated[int, Field(ge=1, description="The exact version to pin.")]
    target_type: TargetType
    target_id: Annotated[
        int | None,
        Field(
            gt=0,
            description="GitLab ID of the group or project. Omitted for the instance.",
        ),
    ] = None

    @model_validator(mode="after")
    def _target(self) -> "AssignmentIn":
        if (self.target_type == "instance") != (self.target_id is None):
            raise ValueError("target_id is required for a group or project, and omitted otherwise")
        return self


class AssignmentOut(BaseModel):
    id: int
    policy_id: int
    policy_name: str
    version: int
    yaml_digest: str
    target_type: TargetType
    target_id: int
    # From the inventory; null when the target is no longer recorded there.
    target_path: str | None
    target_state: str | None
    created_at: datetime


class AssignmentPage(BaseModel):
    items: list[AssignmentOut]
    next_after_id: int | None


def _configured_url() -> str | None:
    try:
        settings = load_gitlab_settings()
    except ConfigurationError:
        return None
    return settings.url if settings else None


def _instance(db: Session) -> GitLabInstance | None:
    url = _configured_url()
    if url is None:
        return None
    return db.scalars(select(GitLabInstance).where(GitLabInstance.base_url == url)).first()


def _assignments() -> Any:
    group = and_(
        PolicyAssignment.target_type == "group",
        GitLabGroup.instance_id == PolicyAssignment.instance_id,
        GitLabGroup.gitlab_id == PolicyAssignment.target_id,
    )
    project = and_(
        PolicyAssignment.target_type == "project",
        GitLabProject.instance_id == PolicyAssignment.instance_id,
        GitLabProject.gitlab_id == PolicyAssignment.target_id,
    )
    return (
        select(
            PolicyAssignment,
            Policy,
            GitLabInstance.base_url,
            GitLabGroup.full_path,
            GitLabGroup.state,
            GitLabProject.path_with_namespace,
            GitLabProject.state,
        )
        .join(Policy, Policy.id == PolicyAssignment.policy_id)
        .join(GitLabInstance, GitLabInstance.id == PolicyAssignment.instance_id)
        .outerjoin(GitLabGroup, group)
        .outerjoin(GitLabProject, project)
    )


def _out(row: Any) -> AssignmentOut:
    assignment, policy, base_url, group_path, group_state, project_path, project_state = row
    path, state = {
        "instance": (base_url, None),
        "group": (group_path, group_state),
        "project": (project_path, project_state),
    }[assignment.target_type]
    return AssignmentOut(
        id=assignment.id,
        policy_id=policy.id,
        policy_name=policy.name,
        version=policy.version,
        yaml_digest=policy.yaml_digest,
        target_type=assignment.target_type,
        target_id=assignment.target_id,
        target_path=path,
        target_state=state,
        created_at=assignment.created_at,
    )


def _audit_details(out: AssignmentOut) -> dict[str, Any]:
    return {
        "policy_name": out.policy_name,
        "version": out.version,
        "yaml_digest": out.yaml_digest,
        "target_type": out.target_type,
        "target_id": out.target_id,
        "target_path": out.target_path,
    }


@router.get(
    "/policy-assignments",
    response_model=AssignmentPage,
    summary="Policy assignments",
    description="Assignments for the configured GitLab instance in ID order. Empty when GitLab "
    "is not configured.",
)
def list_assignments(
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
    policy_name: Annotated[str | None, Query(pattern=NAME_PATTERN, max_length=63)] = None,
    target_type: Annotated[TargetType | None, Query()] = None,
    target_id: Annotated[int | None, Query(gt=0)] = None,
    after_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> AssignmentPage:
    instance = _instance(db)
    if instance is None:
        return AssignmentPage(items=[], next_after_id=None)
    statement = (
        _assignments()
        .where(PolicyAssignment.instance_id == instance.id)
        .order_by(PolicyAssignment.id)
        .limit(limit + 1)
    )
    if policy_name is not None:
        statement = statement.where(PolicyAssignment.policy_name == policy_name)
    if target_type is not None:
        statement = statement.where(PolicyAssignment.target_type == target_type)
    if target_id is not None:
        statement = statement.where(PolicyAssignment.target_id == target_id)
    if after_id is not None:
        statement = statement.where(PolicyAssignment.id > after_id)
    rows = db.execute(statement).all()
    items = [_out(row) for row in rows[:limit]]
    return AssignmentPage(items=items, next_after_id=items[-1].id if len(rows) > limit else None)


def _read(db: Session, assignment_id: int) -> AssignmentOut:
    return _out(db.execute(_assignments().where(PolicyAssignment.id == assignment_id)).one())


@router.post(
    "/policy-assignments",
    response_model=AssignmentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Pin a policy version to the instance, a group or a project",
    description="A target holds one version of each policy: assigning another version of the "
    "same policy replaces the current one. 201 when assigned; 200 when this version is already "
    "assigned there. Requires the `X-CSRF-Token` header.",
    responses={
        200: {"model": AssignmentOut, "description": "Already assigned"},
        404: {"description": "No such policy version, or the target is not in the inventory"},
        409: {"description": "GitLab is not configured, or the target is missing or inaccessible"},
    },
)
def create_assignment(
    body: AssignmentIn,
    principal: Annotated[Principal, Depends(require(Permission.POLICY_WRITE))],
    db: Annotated[Session, Depends(database)],
) -> Any:
    url = _configured_url()
    if url is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "GitLab is not configured")
    policy = db.scalars(
        select(Policy).where(Policy.name == body.policy_name, Policy.version == body.version)
    ).first()
    if policy is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such policy version")
    instance = instance_for(db, url)
    target: Target | None = find_target(
        db, instance, body.target_type, body.target_id or instance.id
    )
    if target is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"No such {body.target_type} in the inventory"
        )
    if target.state not in (None, "active"):
        # It cannot be confirmed in GitLab, so what the policy would govern is unknown.
        raise HTTPException(status.HTTP_409_CONFLICT, f"The {body.target_type} is {target.state}")
    assigned = assign(db, instance.id, policy, target)
    if not assigned.created:
        db.commit()
        existing = _read(db, assigned.assignment.id)
        return JSONResponse(existing.model_dump(mode="json"), status_code=status.HTTP_200_OK)
    out = _read(db, assigned.assignment.id)
    details = _audit_details(out)
    if assigned.replaced is not None:
        details["replaced_assignment_id"] = assigned.replaced.id
        details["replaced_version"] = assigned.replaced_version
    audit.record(
        db,
        actor=principal.actor,
        action="policy.assigned",
        resource_type="policy_assignment",
        resource_id=str(out.id),
        details=details,
    )
    db.commit()
    return out


@router.delete(
    "/policy-assignments/{assignment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a policy assignment",
    description="Requires the `X-CSRF-Token` header.",
    responses={404: {"description": "No such assignment"}},
)
def delete_assignment(
    assignment_id: Annotated[int, Path(gt=0)],
    principal: Annotated[Principal, Depends(require(Permission.POLICY_WRITE))],
    db: Annotated[Session, Depends(database)],
) -> Response:
    instance = _instance(db)
    assignment = db.get(PolicyAssignment, assignment_id, with_for_update=True)
    if instance is None or assignment is None or assignment.instance_id != instance.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such assignment")
    out = _read(db, assignment_id)
    db.delete(assignment)
    audit.record(
        db,
        actor=principal.actor,
        action="policy.unassigned",
        resource_type="policy_assignment",
        resource_id=str(assignment_id),
        details=_audit_details(out),
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


_RESOLUTION_DOC = (
    "Nearest assignment wins per policy name: the project, its namespace group, that group's "
    "ancestors, then the instance. `considered` lists every assignment on the path with its "
    "outcome. When `ancestry` is `incomplete`, `gaps` names the groups that could not be "
    "followed and policies assigned above them may be missing."
)


@router.get(
    "/gitlab/projects/{gitlab_id}/effective-policies",
    response_model=Resolution,
    summary="Policies in force for a project, with how they were resolved",
    description=_RESOLUTION_DOC,
    responses={404: {"description": "The project is not in the inventory"}},
)
def project_policies(
    gitlab_id: Annotated[int, Path(gt=0)],
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
) -> Resolution:
    instance = _instance(db)
    project = (
        db.scalars(
            select(GitLabProject).where(
                GitLabProject.instance_id == instance.id, GitLabProject.gitlab_id == gitlab_id
            )
        ).first()
        if instance is not None
        else None
    )
    if instance is None or project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such project in the inventory")
    return resolve_project(db, instance, project)


@router.get(
    "/gitlab/groups/{gitlab_id}/effective-policies",
    response_model=Resolution,
    summary="Policies in force for a group, with how they were resolved",
    description=_RESOLUTION_DOC,
    responses={404: {"description": "The group is not in the inventory"}},
)
def group_policies(
    gitlab_id: Annotated[int, Path(gt=0)],
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
) -> Resolution:
    instance = _instance(db)
    group = (
        db.scalars(
            select(GitLabGroup).where(
                GitLabGroup.instance_id == instance.id, GitLabGroup.gitlab_id == gitlab_id
            )
        ).first()
        if instance is not None
        else None
    )
    if instance is None or group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such group in the inventory")
    return resolve_group(db, instance, group)
