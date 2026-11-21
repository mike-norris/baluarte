"""Workspaces: the isolation boundary (ADR-0013, roadmap M11.4).

Anyone signed in lists the workspaces they hold a role in, with each one's connections, to choose
where to work (the ``X-Baluarte-Workspace`` and ``X-Baluarte-Connection`` headers). Deployment
administrators (administrators in the default workspace) see every workspace and create and
change them. A workspace grants roles through its own identity provider groups; the default
workspace uses the sign-in settings' groups. Roles are resolved at sign-in, so a change applies
from each person's next sign-in.
"""

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.auth.dependencies import Principal, current_principal, database
from baluarte.auth.roles import Role
from baluarte.db.models import GitLabInstance, Workspace
from baluarte.workspaces import (
    DEFAULT_SLUG,
    configured_settings,
    default_workspace_id,
    group_roles_of,
)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
MAX_GROUPS = 50

Slug = Annotated[str, Path(pattern=SLUG.pattern)]


def deployment_admin(db: Session, principal: Principal) -> bool:
    """Whether the caller administers the deployment: an administrator in the default workspace.

    A principal built without workspaces (tests) counts by its own roles.
    """
    held = (
        principal.workspaces.get(default_workspace_id(db), frozenset())
        if principal.workspaces
        else principal.roles
    )
    return Role.ADMIN in held


def _require_deployment_admin(db: Session, principal: Principal) -> None:
    if not deployment_admin(db, principal):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only administrators of the default workspace manage workspaces",
        )


class ConnectionChoice(BaseModel):
    id: int
    name: str | None
    url: str
    primary: bool


class WorkspaceOut(BaseModel):
    slug: str
    name: str
    default: bool
    # The caller's roles there; empty for a workspace a deployment administrator sees but holds
    # no role in.
    roles: list[Role]
    # Groups per role; shown to deployment administrators only. None for the default workspace,
    # which uses the sign-in settings' groups.
    group_roles: dict[Role, list[str]] | None
    connections: list[ConnectionChoice]


class WorkspaceList(BaseModel):
    workspaces: list[WorkspaceOut]
    # Whether the caller may create and change workspaces.
    can_manage: bool


def _groups(value: dict[str, list[str]] | None) -> dict[Role, list[str]] | None:
    if value is None:
        return None
    return {role: sorted(groups) for role, groups in group_roles_of(value).items() if groups}


def _out(
    db: Session, row: Workspace, principal: Principal, admin: bool, default_id: int
) -> WorkspaceOut:
    primary = configured_settings()
    held = (
        principal.workspaces.get(row.id, frozenset())
        if principal.workspaces
        else (principal.roles if row.id == default_id else frozenset())
    )
    connections = db.scalars(
        select(GitLabInstance)
        .where(GitLabInstance.workspace_id == row.id, GitLabInstance.removed_at.is_(None))
        .order_by(GitLabInstance.id)
    )
    return WorkspaceOut(
        slug=row.slug,
        name=row.name,
        default=row.id == default_id,
        roles=sorted(held),
        group_roles=_groups(row.group_roles) if admin and row.id != default_id else None,
        connections=[
            ConnectionChoice(
                id=connection.id,
                name=connection.name,
                url=connection.base_url,
                primary=primary is not None and connection.base_url == primary.url,
            )
            for connection in connections
        ],
    )


@router.get(
    "",
    response_model=WorkspaceList,
    summary="List the workspaces you can work in, with their connections",
    description="Deployment administrators see every workspace and its groups.",
)
def list_workspaces(
    principal: Annotated[Principal, Depends(current_principal)],
    db: Annotated[Session, Depends(database)],
) -> WorkspaceList:
    admin = deployment_admin(db, principal)
    default_id = default_workspace_id(db)
    visible = set(principal.workspaces) if principal.workspaces else {default_id}
    rows = db.scalars(select(Workspace).order_by(Workspace.id))
    return WorkspaceList(
        workspaces=[
            _out(db, row, principal, admin, default_id)
            for row in rows
            if admin or row.id in visible
        ],
        can_manage=admin,
    )


def _checked_groups(value: dict[Role, list[str]]) -> dict[Role, list[str]]:
    for groups in value.values():
        if len(groups) > MAX_GROUPS:
            raise ValueError(f"At most {MAX_GROUPS} groups per role")
        if any(not group.strip() or len(group) > 200 for group in groups):
            raise ValueError("Group names are 1 to 200 characters")
    return {role: sorted({group.strip() for group in groups}) for role, groups in value.items()}


class WorkspaceCreate(BaseModel):
    slug: str = Field(pattern=SLUG.pattern)
    name: str = Field(min_length=1, max_length=200)
    group_roles: dict[Role, list[str]] = {}

    @field_validator("group_roles")
    @classmethod
    def _groups(cls, value: dict[Role, list[str]]) -> dict[Role, list[str]]:
        return _checked_groups(value)


class WorkspaceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    group_roles: dict[Role, list[str]] | None = None

    @field_validator("group_roles")
    @classmethod
    def _groups(cls, value: dict[Role, list[str]] | None) -> dict[Role, list[str]] | None:
        return None if value is None else _checked_groups(value)


def _stored(group_roles: dict[Role, list[str]]) -> dict[str, list[str]]:
    return {role.value: groups for role, groups in group_roles.items() if groups}


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=WorkspaceOut,
    summary="Create a workspace",
    description="Deployment administrators only. Its groups grant roles in it from each person's "
    "next sign-in. Requires the `X-CSRF-Token` header.",
    responses={
        403: {"description": "Not a deployment administrator"},
        409: {"description": "Slug taken"},
    },
)
def create_workspace(
    body: WorkspaceCreate,
    principal: Annotated[Principal, Depends(current_principal)],
    db: Annotated[Session, Depends(database)],
) -> WorkspaceOut:
    _require_deployment_admin(db, principal)
    row = Workspace(slug=body.slug, name=body.name.strip(), group_roles=_stored(body.group_roles))
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "A workspace with this slug exists") from exc
    audit.record(
        db,
        actor=principal.actor,
        action="workspace.created",
        resource_type="workspace",
        resource_id=row.slug,
        details={"name": row.name, "group_roles": row.group_roles},
    )
    db.commit()
    return _out(db, row, principal, True, default_workspace_id(db))


@router.patch(
    "/{slug}",
    response_model=WorkspaceOut,
    summary="Rename a workspace or change its groups",
    description="Deployment administrators only. The default workspace's groups are the sign-in "
    "settings'. Requires the `X-CSRF-Token` header.",
    responses={
        403: {"description": "Not a deployment administrator"},
        404: {"description": "No such workspace"},
    },
)
def change_workspace(
    slug: Slug,
    body: WorkspaceUpdate,
    principal: Annotated[Principal, Depends(current_principal)],
    db: Annotated[Session, Depends(database)],
) -> WorkspaceOut:
    _require_deployment_admin(db, principal)
    row = db.scalars(select(Workspace).where(Workspace.slug == slug)).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such workspace")
    changed: dict[str, object] = {}
    if body.name is not None:
        row.name = changed["name"] = body.name.strip()
    if body.group_roles is not None:
        if row.slug == DEFAULT_SLUG:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "The default workspace's groups are set in the sign-in settings",
            )
        row.group_roles = changed["group_roles"] = _stored(body.group_roles)
    if changed:
        audit.record(
            db,
            actor=principal.actor,
            action="workspace.changed",
            resource_type="workspace",
            resource_id=row.slug,
            details=changed,
        )
    db.commit()
    return _out(db, row, principal, True, default_workspace_id(db))
