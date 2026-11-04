"""Monitoring scope: take projects, groups or personal namespaces out of monitoring, and back.

Excluded projects stay in the inventory but are not evaluated, not read in detail and not
counted (``baluarte.scope``). Excluding follows the review setting; ending an exclusion needs a
justification but no second person.
"""

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte.api.v1.controls import ApproveIn, Person, RejectIn
from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.db.models import (
    MAX_JUSTIFICATION,
    MIN_JUSTIFICATION,
    AppUser,
    GitLabInstance,
    ScopeExclusion,
)
from baluarte.integrations.gitlab.settings import GitLabSettings, load_gitlab_settings
from baluarte.policy.changes import review_required
from baluarte.policy.evaluation.runner import request_evaluation
from baluarte.scope import exclusions
from baluarte.scope.exclusions import (
    MAX_TARGETS,
    Actor,
    ChangeConflict,
    ExclusionState,
    NotAllowed,
    ScopeConflict,
    Target,
    TargetType,
)
from baluarte.settings import ConfigurationError

router = APIRouter(prefix="/scope", tags=["scope"])

Justification = Annotated[str, Field(min_length=MIN_JUSTIFICATION, max_length=MAX_JUSTIFICATION)]


class ExclusionOut(BaseModel):
    id: int
    batch_id: str
    target_type: TargetType
    # GitLab ID of the project or group; null for personal namespaces.
    target_id: int | None
    # The project or group's path when the exclusion was proposed.
    target_path: str | None
    state: ExclusionState
    justification: str
    review_required: bool
    self_approved: bool
    proposed_by: Person
    proposed_at: datetime
    decided_by: Person | None
    decided_at: datetime | None
    decision_note: str | None
    ended_by: Person | None
    ended_at: datetime | None
    end_justification: str | None


class ExclusionsPage(BaseModel):
    items: list[ExclusionOut]
    review_required: bool


class TargetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["project", "group"]
    gitlab_id: Annotated[int, Field(gt=0)]


class ExcludeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targets: list[TargetIn] = Field(default_factory=list, max_length=MAX_TARGETS)
    personal_namespaces: bool = False
    justification: Justification


class BatchOut(BaseModel):
    batch_id: str
    items: list[ExclusionOut]


class EndIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1, max_length=MAX_TARGETS)
    justification: Justification


class EndOut(BaseModel):
    items: list[ExclusionOut]


def _instance_id(db: Session) -> int | None:
    settings: GitLabSettings | None
    try:
        settings = load_gitlab_settings()
    except ConfigurationError:
        settings = None
    if settings is None:
        return None
    return db.scalars(
        select(GitLabInstance.id).where(GitLabInstance.base_url == settings.url)
    ).first()


def _required_instance(db: Session) -> int:
    instance_id = _instance_id(db)
    if instance_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "not_configured",
                "message": "GitLab is not configured or not yet discovered",
                "reasons": [],
            },
        )
    return instance_id


def _names(db: Session, rows: Iterable[ScopeExclusion]) -> dict[int, str | None]:
    ids = {
        user_id
        for row in rows
        for user_id in (row.proposed_by_user_id, row.decided_by_user_id, row.ended_by_user_id)
        if user_id is not None
    }
    if not ids:
        return {}
    return dict(
        db.execute(select(AppUser.id, AppUser.display_name).where(AppUser.id.in_(ids))).all()
    )


def _person(actor: str, user_id: int | None, names: dict[int, str | None]) -> Person:
    return Person(
        actor=actor, user_id=user_id, display_name=names.get(user_id) if user_id else None
    )


def _optional_person(
    actor: str | None, user_id: int | None, names: dict[int, str | None]
) -> Person | None:
    return None if actor is None else _person(actor, user_id, names)


def exclusion_out(db: Session, rows: Sequence[ScopeExclusion]) -> list[ExclusionOut]:
    names = _names(db, rows)
    out: list[ExclusionOut] = []
    for row in rows:
        out.append(
            ExclusionOut(
                id=row.id,
                batch_id=row.batch_id,
                target_type=row.target_type,  # type: ignore[arg-type]
                target_id=row.target_id,
                target_path=row.target_path,
                state=row.state,  # type: ignore[arg-type]
                justification=row.justification,
                review_required=row.review_required,
                self_approved=row.self_approved,
                proposed_by=_person(row.proposed_by, row.proposed_by_user_id, names),
                proposed_at=row.proposed_at,
                decided_by=_optional_person(row.decided_by, row.decided_by_user_id, names),
                decided_at=row.decided_at,
                decision_note=row.decision_note,
                ended_by=_optional_person(row.ended_by, row.ended_by_user_id, names),
                ended_at=row.ended_at,
                end_justification=row.end_justification,
            )
        )
    return out


def _conflict(error: ChangeConflict) -> HTTPException:
    """409 with ``{code, message, reasons: [{code, message}]}``; ``code`` is the first reason."""
    if isinstance(error, ScopeConflict):
        reasons = [{"code": r.code, "message": r.message} for r in error.reasons]
    else:
        reasons = [{"code": "not_proposed", "message": str(error)}]
    detail = {"code": reasons[0]["code"], "message": str(error), "reasons": reasons}
    return HTTPException(status.HTTP_409_CONFLICT, detail=detail)


def _own_change(error: NotAllowed) -> HTTPException:
    return HTTPException(
        status.HTTP_403_FORBIDDEN, detail={"code": "own_change", "message": str(error)}
    )


@router.get(
    "/exclusions",
    response_model=ExclusionsPage,
    summary="Exclusions from monitoring scope",
    description="Every exclusion of the configured instance, newest first; filter with `state` "
    "(`proposed`, `active`, `rejected` or `ended`). A project is out of monitoring while an "
    "`active` exclusion names it, one of its groups (a group covers its subgroups), or, for a "
    "personal-namespace project, `personal_namespaces`. `review_required` is whether new "
    "exclusions wait for a second person.",
)
def list_exclusions(
    _: Annotated[Principal, Depends(require(Permission.INVENTORY_READ))],
    db: Annotated[Session, Depends(database)],
    state: Annotated[ExclusionState | None, Query()] = None,
) -> ExclusionsPage:
    instance_id = _instance_id(db)
    required = review_required(db)
    if instance_id is None:
        return ExclusionsPage(items=[], review_required=required)
    statement = (
        select(ScopeExclusion)
        .where(ScopeExclusion.instance_id == instance_id)
        .order_by(ScopeExclusion.id.desc())
    )
    if state is not None:
        statement = statement.where(ScopeExclusion.state == state)
    rows = list(db.scalars(statement))
    return ExclusionsPage(items=exclusion_out(db, rows), review_required=required)


@router.post(
    "/exclusions",
    response_model=BatchOut,
    status_code=status.HTTP_201_CREATED,
    summary="Take projects, groups or personal namespaces out of monitoring",
    description="One batch of up to 500 targets with one justification: projects and groups "
    "by GitLab ID, and `personal_namespaces` for every project in a personal namespace, "
    "including ones discovered later. A group covers its subgroups and their projects. "
    "Excluded projects stay in the inventory but are not evaluated, not read in detail and "
    "not counted; their open findings resolve as `project_excluded`. While review is required "
    "the batch is `proposed` until someone else approves it; otherwise it is `active` and "
    "`self_approved`. Requires the `X-CSRF-Token` header. Audited.",
    responses={409: {"description": "The batch conflicts; `reasons` lists every reason code"}},
)
def propose_exclusions(
    body: ExcludeIn,
    principal: Annotated[Principal, Depends(require(Permission.POLICY_WRITE))],
    db: Annotated[Session, Depends(database)],
) -> BatchOut:
    instance_id = _required_instance(db)
    try:
        rows = exclusions.propose(
            db,
            instance_id,
            [Target(target.type, target.gitlab_id) for target in body.targets],
            body.personal_namespaces,
            body.justification,
            Actor(principal.user_id, principal.actor),
        )
    except ChangeConflict as error:
        raise _conflict(error) from None
    if rows[0].state == "active":
        request_evaluation(db, "scope")
    db.commit()
    return BatchOut(batch_id=rows[0].batch_id, items=exclusion_out(db, rows))


BatchId = Annotated[str, Path(min_length=1, max_length=64)]


@router.post(
    "/exclusions/batches/{batch_id}/approve",
    response_model=BatchOut,
    summary="Approve a proposed batch of exclusions",
    description="Puts every exclusion of the batch in force. Not by its proposer. Requires the "
    "`X-CSRF-Token` header. Audited.",
    responses={
        403: {"description": "`own_change`: the proposer cannot decide their own batch"},
        404: {"description": "No such batch"},
        409: {"description": "The batch is already decided"},
    },
)
def approve_batch(
    batch_id: BatchId,
    body: ApproveIn,
    principal: Annotated[Principal, Depends(require(Permission.POLICY_WRITE))],
    db: Annotated[Session, Depends(database)],
) -> BatchOut:
    try:
        rows = exclusions.approve(
            db, batch_id, Actor(principal.user_id, principal.actor), body.note
        )
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such batch") from None
    except NotAllowed as error:
        raise _own_change(error) from None
    except ChangeConflict as error:
        raise _conflict(error) from None
    request_evaluation(db, "scope")
    db.commit()
    return BatchOut(batch_id=batch_id, items=exclusion_out(db, rows))


@router.post(
    "/exclusions/batches/{batch_id}/reject",
    response_model=BatchOut,
    summary="Reject a proposed batch of exclusions",
    description="`note` says why. Not by its proposer. Requires the `X-CSRF-Token` header. "
    "Audited.",
    responses={
        403: {"description": "`own_change`: the proposer cannot decide their own batch"},
        404: {"description": "No such batch"},
        409: {"description": "The batch is already decided"},
    },
)
def reject_batch(
    batch_id: BatchId,
    body: RejectIn,
    principal: Annotated[Principal, Depends(require(Permission.POLICY_WRITE))],
    db: Annotated[Session, Depends(database)],
) -> BatchOut:
    try:
        rows = exclusions.reject(db, batch_id, Actor(principal.user_id, principal.actor), body.note)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such batch") from None
    except NotAllowed as error:
        raise _own_change(error) from None
    except ChangeConflict as error:
        raise _conflict(error) from None
    db.commit()
    return BatchOut(batch_id=batch_id, items=exclusion_out(db, rows))


@router.post(
    "/exclusions/end",
    response_model=EndOut,
    summary="Bring excluded targets back into monitoring",
    description="Ends active exclusions at once, with a justification. Including projects again "
    "widens monitoring, so no second person is needed. The next evaluation judges the projects "
    "again and reopens findings that still fail. Requires the `X-CSRF-Token` header. Audited.",
    responses={409: {"description": "An exclusion is unknown or not in force"}},
)
def end_exclusions(
    body: EndIn,
    principal: Annotated[Principal, Depends(require(Permission.POLICY_WRITE))],
    db: Annotated[Session, Depends(database)],
) -> EndOut:
    instance_id = _required_instance(db)
    try:
        rows = exclusions.end(
            db,
            instance_id,
            body.ids,
            body.justification,
            Actor(principal.user_id, principal.actor),
        )
    except ChangeConflict as error:
        raise _conflict(error) from None
    request_evaluation(db, "scope")
    db.commit()
    return EndOut(items=exclusion_out(db, rows))
