"""Changes to the baselines in force, and approving or rejecting proposed ones.

Reading needs ``findings:read`` (every role), like reading baselines: auditors must see who
proposed and who approved what is in force. Approving and rejecting need ``policy:write`` and
must be done by someone other than the proposer.
"""

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.db.models import (
    AppUser,
    GitLabGroup,
    GitLabInstance,
    GitLabProject,
    Policy,
    PolicyChange,
)
from baluarte.policy import changes
from baluarte.policy.changes import MAX_NOTE, Actor, Applied, ChangeConflict, NotAllowed
from baluarte.policy.evaluation.runner import request_evaluation
from baluarte.policy.schema import NAME_PATTERN

router = APIRouter(prefix="/policy-changes", tags=["policies"])

ChangeState = Literal["proposed", "approved", "rejected"]
Note = Annotated[str, Field(max_length=MAX_NOTE)]


class Person(BaseModel):
    # ``user:<id>`` as in the audit log, or a ``system:`` actor; "unknown" for changes made
    # before migration 0015 that the audit log does not name.
    actor: str
    user_id: int | None
    display_name: str | None


class Review(BaseModel):
    """How a version or assignment in force came to be: the change that applied it."""

    change_id: int
    # True when review was required at the time and someone other than the proposer approved.
    reviewed: bool
    # Applied while review was not required: the proposer approved it themselves.
    self_approved: bool
    # In force before review was recorded (migration 0015).
    migrated: bool
    proposed_by: Person
    decided_by: Person | None
    decided_at: datetime | None


class PolicyChangeOut(BaseModel):
    id: int
    kind: changes.ChangeKind
    state: ChangeState
    policy_name: str
    # The version concerned: for ``version``, the version it became once approved (else null);
    # for ``assign`` and ``unassign``, the version pinned.
    version: int | None
    yaml_digest: str | None
    # The latest version in force when a ``version`` change was proposed.
    base_version: int | None
    target_type: Literal["instance", "group", "project"] | None
    target_id: int | None
    target_path: str | None
    assignment_id: int | None
    review_required: bool
    self_approved: bool
    migrated: bool
    proposed_by: Person
    proposed_at: datetime
    decided_by: Person | None
    decided_at: datetime | None
    decision_note: str | None


class PolicyChangeDetail(PolicyChangeOut):
    # ``version`` changes: the proposed source, and the source of ``base_version`` to compare.
    source_yaml: str | None
    base_source_yaml: str | None


class PolicyChangePage(BaseModel):
    # Whether new changes currently need a second person (the ``governance`` setting).
    review_required: bool
    items: list[PolicyChangeOut]
    next_before_id: int | None


class ApproveIn(BaseModel):
    note: Note | None = None


class RejectIn(BaseModel):
    note: Annotated[str, Field(min_length=1, max_length=MAX_NOTE)]


def _user_id(actor: str | None) -> int | None:
    if actor and actor.startswith("user:") and actor[5:].isdigit():
        return int(actor[5:])
    return None


def _names(db: Session, rows: Iterable[PolicyChange]) -> dict[int, str | None]:
    ids: set[int] = set()
    for row in rows:
        for actor in (row.proposed_by, row.decided_by):
            user_id = _user_id(actor)
            if user_id is not None:
                ids.add(user_id)
    if not ids:
        return {}
    return dict(
        db.execute(select(AppUser.id, AppUser.display_name).where(AppUser.id.in_(ids))).all()
    )


def _person(actor: str | None, names: dict[int, str | None]) -> Person | None:
    return None if actor is None else _named(actor, names)


def _named(actor: str, names: dict[int, str | None]) -> Person:
    user_id = _user_id(actor)
    return Person(
        actor=actor, user_id=user_id, display_name=names.get(user_id) if user_id else None
    )


def _paths(db: Session, rows: Sequence[PolicyChange]) -> dict[tuple[str, int], str]:
    paths: dict[tuple[str, int], str] = {}
    for row in rows:
        if row.target_type is None or row.target_id is None or row.instance_id is None:
            continue
        key = (row.target_type, row.target_id)
        if key in paths:
            continue
        path: str | None
        if row.target_type == "instance":
            path = db.scalar(
                select(GitLabInstance.base_url).where(GitLabInstance.id == row.target_id)
            )
        elif row.target_type == "group":
            path = db.scalar(
                select(GitLabGroup.full_path).where(
                    GitLabGroup.instance_id == row.instance_id,
                    GitLabGroup.gitlab_id == row.target_id,
                )
            )
        else:
            path = db.scalar(
                select(GitLabProject.path_with_namespace).where(
                    GitLabProject.instance_id == row.instance_id,
                    GitLabProject.gitlab_id == row.target_id,
                )
            )
        if path is not None:
            paths[key] = path
    return paths


def _versions(db: Session, rows: Sequence[PolicyChange]) -> dict[int, int]:
    ids = {row.policy_id for row in rows if row.policy_id is not None}
    if not ids:
        return {}
    return dict(db.execute(select(Policy.id, Policy.version).where(Policy.id.in_(ids))).all())


def change_out(db: Session, rows: Sequence[PolicyChange]) -> list[PolicyChangeOut]:
    names = _names(db, rows)
    paths = _paths(db, rows)
    versions = _versions(db, rows)
    out: list[PolicyChangeOut] = []
    for row in rows:
        out.append(
            PolicyChangeOut(
                id=row.id,
                kind=row.kind,  # type: ignore[arg-type]
                state=row.state,  # type: ignore[arg-type]
                policy_name=row.policy_name,
                version=versions.get(row.policy_id) if row.policy_id is not None else None,
                yaml_digest=row.yaml_digest,
                base_version=row.base_version,
                target_type=row.target_type,  # type: ignore[arg-type]
                target_id=row.target_id,
                target_path=(
                    paths.get((row.target_type, row.target_id))
                    if row.target_type is not None and row.target_id is not None
                    else None
                ),
                assignment_id=row.assignment_id,
                review_required=row.review_required,
                self_approved=row.self_approved,
                migrated=row.migrated,
                proposed_by=_named(row.proposed_by, names),
                proposed_at=row.proposed_at,
                decided_by=_person(row.decided_by, names),
                decided_at=row.decided_at,
                decision_note=row.decision_note,
            )
        )
    return out


def reviews(
    db: Session, kind: Literal["version", "assign"], ids: Iterable[int]
) -> dict[int, Review]:
    """The approved change behind each version (by policy ID) or assignment (by its ID)."""
    wanted = list(set(ids))
    if not wanted:
        return {}
    column = PolicyChange.policy_id if kind == "version" else PolicyChange.assignment_id
    rows = db.scalars(
        select(PolicyChange)
        .where(
            and_(PolicyChange.kind == kind, PolicyChange.state == "approved", column.in_(wanted))
        )
        .order_by(PolicyChange.id)
    ).all()
    names = _names(db, rows)
    found: dict[int, Review] = {}
    for row in rows:
        key = row.policy_id if kind == "version" else row.assignment_id
        if key is None:
            continue
        found[key] = Review(
            change_id=row.id,
            reviewed=row.review_required,
            self_approved=row.self_approved,
            migrated=row.migrated,
            proposed_by=_named(row.proposed_by, names),
            decided_by=_person(row.decided_by, names),
            decided_at=row.decided_at,
        )
    return found


@router.get(
    "",
    response_model=PolicyChangePage,
    summary="Changes to the baselines in force, newest first",
    description="Every new version, assignment and unassignment, with who proposed and who "
    "approved or rejected it. `review_required` is the current setting.",
)
def list_changes(
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
    state: Annotated[ChangeState | None, Query()] = None,
    kind: Annotated[changes.ChangeKind | None, Query()] = None,
    policy_name: Annotated[str | None, Query(pattern=NAME_PATTERN, max_length=63)] = None,
    before_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> PolicyChangePage:
    statement = select(PolicyChange).order_by(PolicyChange.id.desc()).limit(limit + 1)
    if state is not None:
        statement = statement.where(PolicyChange.state == state)
    if kind is not None:
        statement = statement.where(PolicyChange.kind == kind)
    if policy_name is not None:
        statement = statement.where(PolicyChange.policy_name == policy_name)
    if before_id is not None:
        statement = statement.where(PolicyChange.id < before_id)
    rows = db.scalars(statement).all()
    items = change_out(db, rows[:limit])
    return PolicyChangePage(
        review_required=changes.review_required(db),
        items=items,
        next_before_id=items[-1].id if len(rows) > limit else None,
    )


def change_detail(db: Session, row: PolicyChange) -> PolicyChangeDetail:
    [out] = change_out(db, [row])
    base = None
    if row.kind == "version" and row.base_version is not None:
        base = db.scalar(
            select(Policy.source_yaml).where(
                Policy.name == row.policy_name, Policy.version == row.base_version
            )
        )
    return PolicyChangeDetail(
        **out.model_dump(), source_yaml=row.source_yaml, base_source_yaml=base
    )


@router.get(
    "/{change_id}",
    response_model=PolicyChangeDetail,
    summary="One change, with the proposed source and the version it replaces",
    responses={404: {"description": "No such change"}},
)
def read_change(
    change_id: Annotated[int, Path(gt=0)],
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
) -> PolicyChangeDetail:
    row = db.get(PolicyChange, change_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such change")
    return change_detail(db, row)


def _audit_applied(db: Session, row: PolicyChange, applied: Applied, actor: str) -> None:
    """The events a change records when it takes effect, as when no review is required."""
    if applied.policy is not None:
        audit.record(
            db,
            actor=actor,
            action="policy.version_created",
            resource_type="policy",
            resource_id=f"{applied.policy.name}@{applied.policy.version}",
            details={
                "name": applied.policy.name,
                "version": applied.policy.version,
                "yaml_digest": applied.policy.yaml_digest,
                "change_id": row.id,
                "proposed_by": row.proposed_by,
            },
        )
        return
    details = {
        "policy_name": row.policy_name,
        "policy_id": row.policy_id,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "change_id": row.id,
        "proposed_by": row.proposed_by,
    }
    if applied.removed is not None:
        audit.record(
            db,
            actor=actor,
            action="policy.unassigned",
            resource_type="policy_assignment",
            resource_id=str(applied.removed.id),
            details=details,
        )
    elif applied.assignment is not None:
        if applied.replaced is not None:
            details["replaced_assignment_id"] = applied.replaced.id
            details["replaced_version"] = applied.replaced_version
        audit.record(
            db,
            actor=actor,
            action="policy.assigned",
            resource_type="policy_assignment",
            resource_id=str(applied.assignment.id),
            details=details,
        )
    request_evaluation(db, "assignment")


@router.post(
    "/{change_id}/approve",
    response_model=PolicyChangeDetail,
    summary="Approve a proposed change, which puts it in force",
    description="Someone other than the proposer, with `policy:write`. An assignment or "
    "unassignment queues a policy evaluation. Requires the `X-CSRF-Token` header.",
    responses={
        403: {"description": "Missing permission, CSRF failure, or approving one's own change"},
        404: {"description": "No such change"},
        409: {"description": "Already decided, or it can no longer be applied"},
    },
)
def approve_change(
    change_id: Annotated[int, Path(gt=0)],
    body: ApproveIn,
    principal: Annotated[Principal, Depends(require(Permission.POLICY_WRITE))],
    db: Annotated[Session, Depends(database)],
) -> PolicyChangeDetail:
    by = Actor(principal.user_id, principal.actor)
    try:
        row, applied = changes.approve(db, change_id, by, body.note)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such change") from None
    except NotAllowed as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(error)) from None
    except ChangeConflict as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
    _audit_applied(db, row, applied, principal.actor)
    db.commit()
    return change_detail(db, row)


@router.post(
    "/{change_id}/reject",
    response_model=PolicyChangeDetail,
    summary="Reject a proposed change, with a note",
    description="Someone other than the proposer, with `policy:write`. Nothing changes in force. "
    "Requires the `X-CSRF-Token` header.",
    responses={
        403: {"description": "Missing permission, CSRF failure, or rejecting one's own change"},
        404: {"description": "No such change"},
        409: {"description": "Already decided"},
    },
)
def reject_change(
    change_id: Annotated[int, Path(gt=0)],
    body: RejectIn,
    principal: Annotated[Principal, Depends(require(Permission.POLICY_WRITE))],
    db: Annotated[Session, Depends(database)],
) -> PolicyChangeDetail:
    by = Actor(principal.user_id, principal.actor)
    try:
        row = changes.reject(db, change_id, by, body.note)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such change") from None
    except NotAllowed as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(error)) from None
    except ChangeConflict as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
    db.commit()
    return change_detail(db, row)
