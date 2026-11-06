"""The manager space: manager scopes, the overview and trends (docs/MANAGER_SPACE.md).

Scopes say whose time per person a manager may see: the groups assigned to them, with their
subgroups and projects. Administrators assign and end them; every change is audited as
``manager.scope_*``. The overview and trends are read-only summaries for anyone with
``manager:read``; they show no per-person data.
"""

from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte import tags as tag_index
from baluarte.api.v1.controls import Person
from baluarte.api.v1.scope import _instance_id, _required_instance
from baluarte.api.v1.tags import tag_filters
from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.db.models import AppUser, ManagerScope, UserSession
from baluarte.manager import overview as manager_overview
from baluarte.manager import scopes
from baluarte.manager import trends as manager_trends
from baluarte.policy.changes import Actor

router = APIRouter(prefix="/manager", tags=["manager"])


class GroupRef(BaseModel):
    gitlab_id: int
    path: str


class ScopeOut(BaseModel):
    id: int
    user: Person
    group: GroupRef
    assigned_by: Person
    assigned_at: datetime
    ended_by: Person | None
    ended_at: datetime | None


class ScopesPage(BaseModel):
    items: list[ScopeOut]


class MyScopes(BaseModel):
    # True for an administrator, who sees time per person in every project.
    all: bool
    items: list[ScopeOut]


class AssignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: Annotated[int, Field(gt=0)]
    group_gitlab_ids: list[Annotated[int, Field(gt=0)]] = Field(
        min_length=1, max_length=scopes.MAX_GROUPS
    )


class UserOut(BaseModel):
    id: int
    display_name: str | None
    email: str | None
    # The roles of the user's latest session, as the identity provider last mapped them.
    roles: list[str]
    last_login_at: datetime | None


class UsersPage(BaseModel):
    items: list[UserOut]


def _names(db: Session, ids: set[int]) -> dict[int, str | None]:
    if not ids:
        return {}
    return dict(
        db.execute(select(AppUser.id, AppUser.display_name).where(AppUser.id.in_(ids))).all()
    )


def scope_out(db: Session, rows: list[ManagerScope]) -> list[ScopeOut]:
    names = _names(
        db,
        {
            user_id
            for row in rows
            for user_id in (row.user_id, row.assigned_by_user_id, row.ended_by_user_id)
            if user_id is not None
        },
    )

    def person(actor: str, user_id: int | None) -> Person:
        return Person(
            actor=actor, user_id=user_id, display_name=names.get(user_id) if user_id else None
        )

    return [
        ScopeOut(
            id=row.id,
            user=person(f"user:{row.user_id}", row.user_id),
            group=GroupRef(gitlab_id=row.group_gitlab_id, path=row.group_path),
            assigned_by=person(row.assigned_by, row.assigned_by_user_id),
            assigned_at=row.assigned_at,
            ended_by=person(row.ended_by, row.ended_by_user_id) if row.ended_by else None,
            ended_at=row.ended_at,
        )
        for row in rows
    ]


def _refused(error: scopes.ScopeRefused) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={"code": error.code, "message": str(error), "reasons": error.reasons},
    )


def unprocessable(code: str, message: str) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT, detail={"code": code, "message": message}
    )


@router.get(
    "/scopes",
    response_model=ScopesPage,
    summary="Manager scopes",
    description="Groups assigned to managers. `state=active` (the default) lists open "
    "assignments, `all` ended ones too. A scope covers the group's subgroups and projects.",
)
def list_scopes(
    _: Annotated[Principal, Depends(require(Permission.MANAGER_READ))],
    db: Annotated[Session, Depends(database)],
    state: Annotated[Literal["active", "all"], Query()] = "active",
) -> ScopesPage:
    instance_id = _instance_id(db)
    rows = (
        scopes.open_scopes(db, instance_id)
        if state == "active"
        else scopes.all_scopes(db, instance_id)
    )
    return ScopesPage(items=scope_out(db, rows))


@router.get(
    "/scopes/me",
    response_model=MyScopes,
    summary="My manager scope",
    description="The groups assigned to the signed-in user. `all` is true for an administrator.",
)
def my_scopes(
    principal: Annotated[Principal, Depends(require(Permission.MANAGER_READ))],
    db: Annotated[Session, Depends(database)],
) -> MyScopes:
    instance_id = _instance_id(db)
    found = scopes.people_scope(db, instance_id, principal.user_id, principal.roles)
    return MyScopes(
        all=found.all,
        items=scope_out(db, scopes.open_scopes(db, instance_id, principal.user_id)),
    )


@router.post(
    "/scopes",
    response_model=ScopesPage,
    status_code=status.HTTP_201_CREATED,
    summary="Assign groups to a manager",
    description="Groups already assigned to the user are kept as they are. 409 `unknown_user`, "
    "`unknown_group` (reasons list the IDs) or `not_configured`.",
)
def assign_scopes(
    principal: Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))],
    db: Annotated[Session, Depends(database)],
    body: Annotated[AssignIn, Body()],
) -> ScopesPage:
    instance_id = _required_instance(db)
    try:
        rows = scopes.assign(
            db,
            instance_id,
            body.user_id,
            body.group_gitlab_ids,
            Actor(principal.user_id, principal.actor),
        )
    except scopes.ScopeRefused as error:
        raise _refused(error) from error
    db.commit()
    return ScopesPage(items=scope_out(db, rows))


@router.post(
    "/scopes/{scope_id}/end",
    response_model=ScopeOut,
    summary="End a manager scope",
    description="409 `already_ended` when it has ended.",
)
def end_scope(
    principal: Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))],
    db: Annotated[Session, Depends(database)],
    scope_id: Annotated[int, Path(gt=0)],
) -> ScopeOut:
    try:
        row = scopes.end(db, scope_id, Actor(principal.user_id, principal.actor))
    except scopes.UnknownScope as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such scope") from error
    except scopes.ScopeRefused as error:
        raise _refused(error) from error
    db.commit()
    return scope_out(db, [row])[0]


@router.get(
    "/users",
    response_model=UsersPage,
    summary="Users who have signed in",
    description="To choose whom to assign a scope; roles are those of each user's latest session.",
)
def list_users(
    _: Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))],
    db: Annotated[Session, Depends(database)],
) -> UsersPage:
    latest = (
        select(UserSession.user_id, func.max(UserSession.id).label("session_id"))
        .group_by(UserSession.user_id)
        .subquery()
    )
    rows = db.execute(
        select(AppUser, UserSession.roles)
        .outerjoin(latest, latest.c.user_id == AppUser.id)
        .outerjoin(UserSession, UserSession.id == latest.c.session_id)
        .order_by(func.lower(func.coalesce(AppUser.display_name, AppUser.email, "")), AppUser.id)
    ).all()
    return UsersPage(
        items=[
            UserOut(
                id=user.id,
                display_name=user.display_name,
                email=user.email,
                roles=sorted(roles or []),
                last_login_at=user.last_login_at,
            )
            for user, roles in rows
        ]
    )


@router.get(
    "/overview",
    summary="Posture, consistency and effort per group or tag value",
    description="One row per top-level group, per child group of `group`, or per value of the "
    "tag key `by_tag`; `tag=key:value` limits every row to the projects with that tag. With "
    "`as_of`, findings, controls, changes and label drift are as they stood then; the parts "
    "without history are listed in `current_only`.",
)
def read_overview(
    _: Annotated[Principal, Depends(require(Permission.MANAGER_READ))],
    db: Annotated[Session, Depends(database)],
    group: Annotated[str | None, Query(max_length=1000)] = None,
    tag: Annotated[str | None, Query(max_length=130)] = None,
    by_tag: Annotated[str | None, Query(max_length=63)] = None,
    as_of: Annotated[datetime | None, Query()] = None,
) -> dict[str, Any]:
    filters = tag_filters([tag] if tag else None)
    if by_tag is not None and not tag_index.valid_name(by_tag):
        raise unprocessable("invalid_tag_key", "by_tag is a tag key")
    try:
        found = manager_overview.overview(
            db,
            _instance_id(db),
            group=group,
            tag=filters[0] if filters else None,
            by_tag=by_tag,
            as_of=as_of,
        )
    except manager_overview.OverviewRefused as error:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail={"code": error.code, "message": str(error)}
        ) from error
    run = found.pop("evaluation_run")
    return {
        "as_of": as_of,
        "evaluation_run": (
            {"id": run.id, "finished_at": run.finished_at} if run is not None else None
        ),
        **found,
    }


@router.get(
    "/trends",
    summary="Findings, rule pass rate, review rate and time over time",
    description="Weekly (`bucket=week`, up to 105) or monthly (`month`, up to 36) points from "
    "the bucket holding `from` to the one holding `to`; by default the last 12. 422 "
    "`too_many_buckets` or `invalid_range`.",
)
def read_trends(
    _: Annotated[Principal, Depends(require(Permission.MANAGER_READ))],
    db: Annotated[Session, Depends(database)],
    bucket: Annotated[Literal["week", "month"], Query()] = "week",
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: Annotated[date | None, Query()] = None,
    group: Annotated[str | None, Query(max_length=1000)] = None,
    tag: Annotated[str | None, Query(max_length=130)] = None,
) -> dict[str, Any]:
    filters = tag_filters([tag] if tag else None)
    try:
        return manager_trends.trends(
            db,
            _instance_id(db),
            bucket,
            start=from_,
            end=to,
            group=group,
            tag=filters[0] if filters else None,
        )
    except manager_trends.TrendsRefused as error:
        if error.code == "unknown_group":
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail={"code": error.code, "message": str(error)}
            ) from error
        raise unprocessable(error.code, str(error)) from error
