"""Dry-run remediation plans: create, submit, approve, reject and read (BAL-031).

Plans only. No endpoint here, or anywhere in Baluarte, changes GitLab: executing an approved
plan needs a separate write credential and is BAL-032. Creating and submitting need
``remediation:plan``; approving and rejecting need ``remediation:approve`` and must be done by
someone other than the plan's creator; reading needs ``findings:read``. Every change is audited
and needs the ``X-CSRF-Token`` header.
"""

from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.db.models import MAX_PLAN_HOURS, MAX_PLAN_STEPS, GitLabInstance, RemediationPlan
from baluarte.integrations.gitlab import freshness
from baluarte.integrations.gitlab.settings import load_gitlab_settings
from baluarte.remediation import plans
from baluarte.remediation.plans import (
    NothingToPlan,
    PlanConflict,
    PlanStale,
    PlanState,
    Scope,
    ScopeTooLarge,
)
from baluarte.settings import ConfigurationError

router = APIRouter(tags=["remediation"])

Note = Annotated[str, Field(min_length=1, max_length=plans.MAX_NOTE)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ScopeIn(BaseModel):
    finding_ids: Annotated[list[Annotated[int, Field(gt=0)]], Field(max_length=MAX_PLAN_STEPS)] = []
    policy: Annotated[str | None, Field(max_length=63)] = None
    rule: Annotated[str | None, Field(max_length=63)] = None
    project_gitlab_id: Annotated[int | None, Field(gt=0)] = None

    @model_validator(mode="after")
    def _one_kind(self) -> "ScopeIn":
        filters = (self.policy, self.rule, self.project_gitlab_id)
        if self.finding_ids and any(value is not None for value in filters):
            raise ValueError("give either finding_ids or filters, not both")
        if not self.finding_ids and all(value is None for value in filters):
            raise ValueError("give finding_ids, or at least one of policy, rule, project_gitlab_id")
        return self


class PlanIn(BaseModel):
    scope: ScopeIn
    expires_in_hours: Annotated[int, Field(ge=1, le=MAX_PLAN_HOURS)] = 24


class ApproveIn(BaseModel):
    plan_digest: Annotated[
        Digest, Field(description="The digest of the plan the approver reviewed.")
    ]
    note: Note | None = None


class RejectIn(BaseModel):
    note: Note


class PlanSummary(BaseModel):
    id: int
    # ``expired`` once ``expires_at`` has passed, whether or not the row says so yet.
    state: PlanState
    steps: int
    skipped: int
    plan_digest: str
    created_by_user_id: int
    created_at: datetime
    expires_at: datetime
    submitted_at: datetime | None
    decided_by_user_id: int | None
    decided_at: datetime | None
    decision_note: str | None
    approved_digest: str | None


class PlanOut(PlanSummary):
    # Recomputed from the stored plan on every read.
    digest_valid: bool
    # Why approval would be refused now; empty when the plan still matches its evidence.
    stale_reasons: list[str]
    summary: list[str]
    plan: dict[str, Any]


class PlanPage(BaseModel):
    items: list[PlanSummary]
    next_after_id: int | None


def _instance(db: Session) -> GitLabInstance | None:
    try:
        settings = load_gitlab_settings()
    except ConfigurationError:
        return None
    if settings is None:
        return None
    return db.scalars(select(GitLabInstance).where(GitLabInstance.base_url == settings.url)).first()


def _projects_freshness(db: Session, instance_id: int) -> str | None:
    try:
        settings = load_gitlab_settings()
    except ConfigurationError:
        return None
    report = freshness.report(db, instance_id, settings)
    projects = report.collections.get("projects")
    return projects.status if projects is not None else None


def _summary(row: RemediationPlan, state: str) -> dict[str, Any]:
    return {
        "id": row.id,
        "state": state,
        "steps": row.steps,
        "skipped": row.skipped,
        "plan_digest": row.plan_digest,
        "created_by_user_id": row.created_by_user_id,
        "created_at": row.created_at,
        "expires_at": row.expires_at,
        "submitted_at": row.submitted_at,
        "decided_by_user_id": row.decided_by_user_id,
        "decided_at": row.decided_at,
        "decision_note": row.decision_note,
        "approved_digest": row.approved_digest,
    }


def _read(db: Session, plan_id: int) -> PlanOut:
    instance = _instance(db)
    found = (
        db.execute(
            select(RemediationPlan, plans.effective_state()).where(
                RemediationPlan.id == plan_id, RemediationPlan.instance_id == instance.id
            )
        ).first()
        if instance is not None
        else None
    )
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such remediation plan")
    row, state = found
    pending = state in ("draft", "submitted")
    return PlanOut(
        **_summary(row, state),
        digest_valid=plans.digest_valid(row),
        stale_reasons=plans.stale_reasons(db, row) if pending else [],
        summary=plans.summary(row.plan),
        plan=row.plan,
    )


@router.post(
    "/remediation/plans",
    response_model=PlanOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a dry-run remediation plan for open findings",
    description="Builds, from stored evidence, the GitLab changes that would fix open, failing "
    "findings: restricting project visibility and protecting default branches. Findings that "
    "are unknown, unsupported, excepted, resolved or not backed by the latest evidence are "
    "listed as skipped with a reason. Nothing is sent to GitLab, now or on approval. Requires "
    "the `X-CSRF-Token` header.",
    responses={
        409: {"description": "GitLab is not configured"},
        422: {
            "description": "Invalid scope, more than "
            f"{MAX_PLAN_STEPS} findings in scope, or nothing in scope can be remediated "
            "(the detail lists why for each finding)"
        },
    },
)
def create_plan(
    body: PlanIn,
    principal: Annotated[Principal, Depends(require(Permission.REMEDIATION_PLAN))],
    db: Annotated[Session, Depends(database)],
) -> PlanOut:
    instance = _instance(db)
    if instance is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "GitLab is not configured")
    current = _projects_freshness(db, instance.id)
    if current not in plans.CURRENT:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"The projects collection is {current or 'unknown'}; plans are built only on current "
            "evidence",
        )
    scope = Scope(
        finding_ids=tuple(body.scope.finding_ids),
        policy=body.scope.policy,
        rule=body.scope.rule,
        project_gitlab_id=body.scope.project_gitlab_id,
    )
    try:
        row = plans.create(
            db,
            instance,
            scope,
            timedelta(hours=body.expires_in_hours),
            principal.user_id,
            principal.actor,
        )
    except ScopeTooLarge as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except NothingToPlan as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"message": str(exc), "skipped": exc.skipped},
        ) from exc
    db.commit()
    return _read(db, row.id)


@router.get(
    "/remediation/plans",
    response_model=PlanPage,
    summary="Remediation plans",
    description="Filter by `state` (with `expired` counting any past `expires_at`). Ordered by "
    "ID; follow `next_after_id`.",
)
def list_plans(
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
    state: PlanState | None = None,
    after_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> PlanPage:
    instance = _instance(db)
    statement = select(RemediationPlan, plans.effective_state()).where(
        RemediationPlan.instance_id == (instance.id if instance else 0)
    )
    if state is not None:
        statement = statement.where(plans.effective_state() == state)
    if after_id is not None:
        statement = statement.where(RemediationPlan.id > after_id)
    rows = list(db.execute(statement.order_by(RemediationPlan.id).limit(limit + 1)).all())
    items = [PlanSummary(**_summary(row, row_state)) for row, row_state in rows[:limit]]
    return PlanPage(items=items, next_after_id=items[-1].id if len(rows) > limit else None)


@router.get(
    "/remediation/plans/{plan_id}",
    response_model=PlanOut,
    summary="One remediation plan, with its steps and a diff-style summary",
    responses={404: {"description": "No such remediation plan"}},
)
def read_plan(
    plan_id: Annotated[int, Path(gt=0)],
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
) -> PlanOut:
    return _read(db, plan_id)


def _transition(
    db: Session,
    plan_id: int,
    principal: Principal,
    action: Literal["submitted", "approved", "rejected"],
    note: str | None = None,
    digest: str | None = None,
) -> PlanOut:
    _read(db, plan_id)  # 404 unless it belongs to the configured instance
    row = plans.locked(db, plan_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such remediation plan")
    try:
        if action == "submitted":
            plans.submit(db, row, principal.user_id, principal.actor)
        else:
            plans.decide(
                db,
                row,
                action,
                principal.user_id,
                principal.actor,
                note,
                digest,
                projects_freshness=_projects_freshness(db, row.instance_id),
            )
    except PlanConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except PlanStale as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"message": "The plan is stale and cannot be approved", "reasons": exc.reasons},
        ) from exc
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    db.commit()
    return _read(db, plan_id)


_TRANSITION_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {"description": "Missing permission, CSRF failure, or not allowed for this user"},
    404: {"description": "No such remediation plan"},
    409: {"description": "Not allowed in the plan's current state, or the plan is stale"},
}


@router.post(
    "/remediation/plans/{plan_id}/submit",
    response_model=PlanOut,
    summary="Submit a draft plan for approval",
    description="Only the plan's creator. Requires the `X-CSRF-Token` header.",
    responses=_TRANSITION_RESPONSES,
)
def submit_plan(
    plan_id: Annotated[int, Path(gt=0)],
    principal: Annotated[Principal, Depends(require(Permission.REMEDIATION_PLAN))],
    db: Annotated[Session, Depends(database)],
) -> PlanOut:
    return _transition(db, plan_id, principal, "submitted")


@router.post(
    "/remediation/plans/{plan_id}/approve",
    response_model=PlanOut,
    summary="Approve a submitted plan",
    description="Only someone other than its creator, before `expires_at`, naming the "
    "`plan_digest` they reviewed. Refused (409) when the plan is stale or the projects "
    "collection is not current. Approval changes nothing in GitLab: execution is not "
    "implemented. Requires the `X-CSRF-Token` header.",
    responses=_TRANSITION_RESPONSES,
)
def approve_plan(
    plan_id: Annotated[int, Path(gt=0)],
    body: ApproveIn,
    principal: Annotated[Principal, Depends(require(Permission.REMEDIATION_APPROVE))],
    db: Annotated[Session, Depends(database)],
) -> PlanOut:
    return _transition(db, plan_id, principal, "approved", body.note, body.plan_digest)


@router.post(
    "/remediation/plans/{plan_id}/reject",
    response_model=PlanOut,
    summary="Reject a submitted plan",
    description="Only someone other than its creator; `note` says why. Requires the "
    "`X-CSRF-Token` header.",
    responses=_TRANSITION_RESPONSES,
)
def reject_plan(
    plan_id: Annotated[int, Path(gt=0)],
    body: RejectIn,
    principal: Annotated[Principal, Depends(require(Permission.REMEDIATION_APPROVE))],
    db: Annotated[Session, Depends(database)],
) -> PlanOut:
    return _transition(db, plan_id, principal, "rejected", body.note)
