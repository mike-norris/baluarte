"""Policy exceptions: request, decide, revoke and read (BAL-033).

Requesting needs ``exception:request``; approving, rejecting and revoking need
``exception:approve``, and the approver or rejecter must not be the requester. Reading needs
``findings:read``. Every change is audited, needs the ``X-CSRF-Token`` header and queues a
policy evaluation so findings follow it.
"""

from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.db.models import (
    MAX_EXCEPTION_DAYS,
    AppUser,
    GitLabInstance,
    GitLabProject,
    PolicyException,
)
from baluarte.integrations.gitlab.settings import load_gitlab_settings
from baluarte.policy import exceptions
from baluarte.policy.evaluation.runner import request_evaluation
from baluarte.policy.exceptions import ExceptionConflict, ExceptionState, Requested
from baluarte.policy.loader import parse_policy
from baluarte.policy.schema import NAME_PATTERN
from baluarte.policy.store import latest_version
from baluarte.settings import ConfigurationError

router = APIRouter(tags=["exceptions"])

Note = Annotated[str, Field(min_length=1, max_length=exceptions.MAX_NOTE)]


class ExceptionIn(BaseModel):
    project_gitlab_id: Annotated[int, Field(gt=0)]
    policy_name: Annotated[str, Field(pattern=NAME_PATTERN, max_length=63)]
    rule_id: Annotated[str, Field(min_length=1, max_length=63)]
    justification: Annotated[str, Field(min_length=10, max_length=exceptions.MAX_JUSTIFICATION)]
    expires_at: Annotated[
        datetime,
        Field(
            description="When the waiver ends, with a time zone. At most "
            f"{MAX_EXCEPTION_DAYS} days ahead."
        ),
    ]


class DecisionIn(BaseModel):
    note: Note | None = None


class RejectIn(BaseModel):
    note: Note


class RevokeIn(BaseModel):
    note: Note


class UserRef(BaseModel):
    user_id: int
    display_name: str | None


class ProjectRef(BaseModel):
    gitlab_id: int
    path: str
    state: str


class ExceptionOut(BaseModel):
    id: int
    project: ProjectRef
    policy_name: str
    rule_id: str
    justification: str
    # ``expired`` once ``expires_at`` has passed, whether or not the row says so yet.
    state: ExceptionState
    in_force: bool
    requested_by: UserRef
    requested_at: datetime
    expires_at: datetime
    decided_by: UserRef | None
    decided_at: datetime | None
    decision_note: str | None
    ended_by: UserRef | None
    ended_at: datetime | None
    end_note: str | None


class ExceptionPage(BaseModel):
    items: list[ExceptionOut]
    next_after_id: int | None


def _instance(db: Session) -> GitLabInstance | None:
    try:
        settings = load_gitlab_settings()
    except ConfigurationError:
        return None
    if settings is None:
        return None
    return db.scalars(select(GitLabInstance).where(GitLabInstance.base_url == settings.url)).first()


def _rows() -> Select[PolicyException, str, GitLabProject]:
    return select(PolicyException, exceptions.effective_state(), GitLabProject).join(
        GitLabProject, GitLabProject.id == PolicyException.project_id
    )


def _outs(db: Session, rows: list[Any]) -> list[ExceptionOut]:
    user_ids = {
        user_id
        for row in rows
        for user_id in (
            row[0].requested_by_user_id,
            row[0].decided_by_user_id,
            row[0].ended_by_user_id,
        )
        if user_id is not None
    }
    names = dict(
        db.execute(select(AppUser.id, AppUser.display_name).where(AppUser.id.in_(user_ids))).all()
    )

    def named(user_id: int) -> UserRef:
        return UserRef(user_id=user_id, display_name=names.get(user_id))

    def user(user_id: int | None) -> UserRef | None:
        return None if user_id is None else named(user_id)

    items = []
    for row in rows:
        exception: PolicyException = row[0]
        project: GitLabProject = row[2]
        items.append(
            ExceptionOut(
                id=exception.id,
                project=ProjectRef(
                    gitlab_id=project.gitlab_id,
                    path=project.path_with_namespace,
                    state=project.state,
                ),
                policy_name=exception.policy_name,
                rule_id=exception.rule_id,
                justification=exception.justification,
                state=row[1],
                in_force=row[1] == "approved",
                requested_by=named(exception.requested_by_user_id),
                requested_at=exception.requested_at,
                expires_at=exception.expires_at,
                decided_by=user(exception.decided_by_user_id),
                decided_at=exception.decided_at,
                decision_note=exception.decision_note,
                ended_by=user(exception.ended_by_user_id),
                ended_at=exception.ended_at,
                end_note=exception.end_note,
            )
        )
    return items


def _read(db: Session, exception_id: int) -> ExceptionOut:
    instance = _instance(db)
    row = (
        db.execute(
            _rows().where(
                PolicyException.id == exception_id,
                PolicyException.instance_id == instance.id,
            )
        ).first()
        if instance is not None
        else None
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such exception")
    return _outs(db, [row])[0]


def _rule_exists(db: Session, policy_name: str, rule_id: str) -> bool:
    policy = latest_version(db, policy_name)
    if policy is None:
        return False
    document = parse_policy(policy.source_yaml).document
    return any(rule.id == rule_id for rule in document.spec.rules)


@router.post(
    "/exceptions",
    response_model=ExceptionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Request a time-limited exception to one rule for one project",
    description="Waives a known failure only: while approved and unexpired, a `fail` is stored "
    "as `excepted`; `unknown` and `unsupported` results are never waived. Someone other than "
    "the requester must approve it. Requires the `X-CSRF-Token` header.",
    responses={
        404: {"description": "No such project in the inventory, or no such policy rule"},
        409: {
            "description": "GitLab is not configured, or an exception is already requested or "
            "approved for this project and rule"
        },
        422: {"description": "Invalid body, or `expires_at` not in the allowed range"},
    },
)
def create_exception(
    body: ExceptionIn,
    principal: Annotated[Principal, Depends(require(Permission.EXCEPTION_REQUEST))],
    db: Annotated[Session, Depends(database)],
) -> ExceptionOut:
    instance = _instance(db)
    if instance is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "GitLab is not configured")
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
    project = db.scalars(
        select(GitLabProject).where(
            GitLabProject.instance_id == instance.id,
            GitLabProject.gitlab_id == body.project_gitlab_id,
        )
    ).first()
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such project in the inventory")
    if not _rule_exists(db, body.policy_name, body.rule_id):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "No such rule in the latest version of that policy"
        )
    requested = Requested(
        instance_id=instance.id,
        project_id=project.id,
        policy_name=body.policy_name,
        rule_id=body.rule_id,
        justification=body.justification,
        expires_at=body.expires_at,
    )
    try:
        row = exceptions.request(db, requested, principal.user_id, principal.actor)
    except ExceptionConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    return _read(db, row.id)


@router.get(
    "/exceptions",
    response_model=ExceptionPage,
    summary="Policy exceptions",
    description="Filter by `state` (with `expired` counting any past `expires_at`), `project` "
    "(GitLab ID), `policy` and `rule`. Ordered by ID; follow `next_after_id`.",
)
def list_exceptions(
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
    state: ExceptionState | None = None,
    project: Annotated[int | None, Query(gt=0)] = None,
    policy: Annotated[str | None, Query(max_length=63)] = None,
    rule: Annotated[str | None, Query(max_length=63)] = None,
    after_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ExceptionPage:
    instance = _instance(db)
    statement = _rows().where(PolicyException.instance_id == (instance.id if instance else 0))
    if state is not None:
        statement = statement.where(exceptions.effective_state() == state)
    if project is not None:
        statement = statement.where(GitLabProject.gitlab_id == project)
    if policy is not None:
        statement = statement.where(PolicyException.policy_name == policy)
    if rule is not None:
        statement = statement.where(PolicyException.rule_id == rule)
    if after_id is not None:
        statement = statement.where(PolicyException.id > after_id)
    rows = list(db.execute(statement.order_by(PolicyException.id).limit(limit + 1)).all())
    items = _outs(db, rows[:limit])
    return ExceptionPage(items=items, next_after_id=items[-1].id if len(rows) > limit else None)


@router.get(
    "/exceptions/{exception_id}",
    response_model=ExceptionOut,
    summary="One policy exception",
    responses={404: {"description": "No such exception"}},
)
def read_exception(
    exception_id: Annotated[int, Path(gt=0)],
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
) -> ExceptionOut:
    return _read(db, exception_id)


def _transition(
    db: Session,
    exception_id: int,
    principal: Principal,
    action: Literal["approved", "rejected", "revoked"],
    note: str | None,
) -> ExceptionOut:
    _read(db, exception_id)  # 404 unless it belongs to the configured instance
    row = exceptions.locked(db, exception_id)
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
    request_evaluation(db, "exception")
    db.commit()
    return _read(db, exception_id)


_TRANSITION_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {"description": "Missing permission, CSRF failure, or deciding one's own request"},
    404: {"description": "No such exception"},
    409: {"description": "Not allowed in the exception's current state"},
}


@router.post(
    "/exceptions/{exception_id}/approve",
    response_model=ExceptionOut,
    summary="Approve a requested exception",
    description="Only someone other than the requester, before `expires_at`. Queues a policy "
    "evaluation. Requires the `X-CSRF-Token` header.",
    responses=_TRANSITION_RESPONSES,
)
def approve_exception(
    exception_id: Annotated[int, Path(gt=0)],
    body: DecisionIn,
    principal: Annotated[Principal, Depends(require(Permission.EXCEPTION_APPROVE))],
    db: Annotated[Session, Depends(database)],
) -> ExceptionOut:
    return _transition(db, exception_id, principal, "approved", body.note)


@router.post(
    "/exceptions/{exception_id}/reject",
    response_model=ExceptionOut,
    summary="Reject a requested exception",
    description="Only someone other than the requester; `note` says why. Requires the "
    "`X-CSRF-Token` header.",
    responses=_TRANSITION_RESPONSES,
)
def reject_exception(
    exception_id: Annotated[int, Path(gt=0)],
    body: RejectIn,
    principal: Annotated[Principal, Depends(require(Permission.EXCEPTION_APPROVE))],
    db: Annotated[Session, Depends(database)],
) -> ExceptionOut:
    return _transition(db, exception_id, principal, "rejected", body.note)


@router.post(
    "/exceptions/{exception_id}/revoke",
    response_model=ExceptionOut,
    summary="Revoke a requested or approved exception",
    description="Ends it now; findings it waived are open again at once and the queued "
    "evaluation settles them. `note` says why. Requires the `X-CSRF-Token` header.",
    responses=_TRANSITION_RESPONSES,
)
def revoke_exception(
    exception_id: Annotated[int, Path(gt=0)],
    body: RevokeIn,
    principal: Annotated[Principal, Depends(require(Permission.EXCEPTION_APPROVE))],
    db: Annotated[Session, Depends(database)],
) -> ExceptionOut:
    return _transition(db, exception_id, principal, "revoked", body.note)
