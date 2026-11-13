"""Issue plans: the time rules on closed issues (docs/TIME_TRACKING.md, ADR-0011).

- The preview and plans need ``findings:read``.
- Plans are created and submitted with ``remediation:plan``, approved or rejected by someone
  other than their creator with ``remediation:approve``, and applied with ``remediation:plan``
  once approved. Applying needs ``BALUARTE_ISSUE_WRITES=true`` and the stored write token.

Every change needs the ``X-CSRF-Token`` header and is audited. Refusals are
``{detail: {code, message, reasons}}``.
"""

from collections import Counter
from datetime import datetime
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte.api.v1.gitlab import gitlab_transport
from baluarte.api.v1.labels import (
    ApprovePlanIn,
    CanApply,
    Person,
    PlanIn,
    RejectIn,
    _instance,
    _people,
    _person,
    _refusal,
    _settings,
)
from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.db.models import MAX_ISSUE_PLAN_STEPS, ClosedIssue, IssuePlan
from baluarte.integrations.gitlab.client import GitLabClient
from baluarte.policy.changes import Actor, review_required
from baluarte.policy.schema import GROUP_PATTERN
from baluarte.settings import ConfigurationError
from baluarte.timetracking import issue_plans as plans
from baluarte.timetracking import settings as time_settings
from baluarte.timetracking.issue_plans import NothingToPlan, PlanConflict, ScopeTooLarge

router = APIRouter(prefix="/issue-plans", tags=["issue plans"])

Reader = Annotated[Principal, Depends(require(Permission.FINDINGS_READ))]
Database = Annotated[Session, Depends(database)]
PlanState = Literal[
    "draft", "submitted", "approved", "rejected", "applying", "applied", "failed", "expired"
]
StepStatus = Literal["pending", "skipped", "stale", "applied", "failed", "not_run"]


class Rules(BaseModel):
    missing_time_label: str
    estimate_round_minutes: int
    issue_types: list[str]


class PreviewOut(BaseModel):
    rules: Rules
    closed_issues: int
    last_read_at: datetime | None
    add_label: int
    remove_label: int
    set_estimate: int
    commits_unknown: int
    writes_enabled: bool
    max_steps: int


class ProjectOut(BaseModel):
    gitlab_id: int
    path: str


class IssueOut(BaseModel):
    iid: int
    type: str | None
    title: str | None
    web_url: str | None


class StepOut(BaseModel):
    step: int
    action: Literal["add_label", "remove_label", "set_estimate"]
    project: ProjectOut
    issue: IssueOut
    label: str | None
    prior: dict[str, Any]
    intended: dict[str, Any]
    status: StepStatus
    request_status: int | None = None
    verified: bool | None = None
    message: str | None = None
    finished_at: str | None = None


class ManualOut(BaseModel):
    reason: str
    message: str
    project: ProjectOut
    issues: list[int]


class IssuePlanSummary(BaseModel):
    id: int
    state: PlanState
    scope: dict[str, list[str]]
    totals: dict[str, int]
    steps: int
    manual: int
    plan_digest: str
    created_by: Person
    created_at: datetime
    expires_at: datetime
    decided_by: Person | None
    decided_at: datetime | None
    finished_at: datetime | None


class IssuePlanOut(IssuePlanSummary):
    rules: Rules
    step_items: list[StepOut]
    manual_items: list[ManualOut]
    digest_valid: bool
    submitted_at: datetime | None
    decision_note: str | None
    review_required: bool
    self_approved: bool
    apply_started_at: datetime | None
    failed_step: int | None
    failure: str | None
    progress: dict[str, int]
    can_apply: CanApply


class IssuePlanPage(BaseModel):
    items: list[IssuePlanSummary]
    next_after_id: int | None


def _rules(rules: dict[str, Any]) -> Rules:
    return Rules(
        missing_time_label=rules.get("missing_time_label", ""),
        estimate_round_minutes=rules.get("estimate_round_minutes", 0),
        issue_types=list(rules.get("issue_types", [])),
    )


def _writes_enabled() -> bool:
    try:
        return plans.writes_enabled()
    except ConfigurationError:
        return False


@router.get(
    "/preview",
    response_model=PreviewOut,
    summary="What a plan for the time rules would change now",
    description="Counts, from the closed issues as last collected, of the labels a plan would "
    "add or remove and the estimates it would set, in `group` (all monitored projects when "
    "omitted). Projects that do not define the label are found only when a plan is built. "
    "Nothing is read from GitLab.",
)
def preview(
    _: Reader,
    db: Database,
    group: Annotated[str | None, Query(pattern=GROUP_PATTERN, max_length=255)] = None,
) -> PreviewOut:
    rules = time_settings.load(db)
    instance = _instance(db)
    items = plans.candidates(db, instance.id, [group] if group else []) if instance else []
    counts: Counter[str] = Counter()
    for item in items:
        actions = plans.wanted(item, rules)
        counts.update(actions)
        if (
            not actions
            and item.issue_type in rules.rule_issue_types
            and item.spent_seconds == 0
            and rules.missing_time_label not in item.labels
            and item.commits == "unknown"
        ):
            counts["commits_unknown"] += 1
    last = (
        db.execute(
            select(func.max(ClosedIssue.read_at)).where(ClosedIssue.instance_id == instance.id)
        ).scalar_one()
        if instance
        else None
    )
    return PreviewOut(
        rules=Rules(
            missing_time_label=rules.missing_time_label,
            estimate_round_minutes=rules.estimate_round_minutes,
            issue_types=list(rules.rule_issue_types),
        ),
        closed_issues=len(items),
        last_read_at=last,
        add_label=counts["add_label"],
        remove_label=counts["remove_label"],
        set_estimate=counts["set_estimate"],
        commits_unknown=counts["commits_unknown"],
        writes_enabled=_writes_enabled(),
        max_steps=MAX_ISSUE_PLAN_STEPS,
    )


def _summary(row: IssuePlan, state: str, names: dict[int, str | None]) -> IssuePlanSummary:
    return IssuePlanSummary(
        id=row.id,
        state=state,  # type: ignore[arg-type]
        scope=row.plan["scope"],
        totals=row.plan.get("totals", {}),
        steps=row.steps,
        manual=row.manual,
        plan_digest=row.plan_digest,
        created_by=Person(
            actor=row.created_by,
            user_id=row.created_by_user_id,
            display_name=names.get(row.created_by_user_id),
        ),
        created_at=row.created_at,
        expires_at=row.expires_at,
        decided_by=_person(row.decided_by, row.decided_by_user_id, names),
        decided_at=row.decided_at,
        finished_at=row.finished_at,
    )


def _plan_out(db: Session, plan_id: int) -> IssuePlanOut:
    instance = _instance(db)
    found = db.execute(
        select(IssuePlan, plans.effective_state()).where(
            IssuePlan.id == plan_id, IssuePlan.instance_id == (instance.id if instance else 0)
        )
    ).first()
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such issue plan")
    row, state = found
    names = _people(db, {row.created_by_user_id, row.decided_by_user_id})
    progress = {entry["step"]: entry for entry in row.progress}
    steps = []
    for step in row.plan.get("steps", []):
        done = progress.get(step["step"], {})
        steps.append(
            StepOut(
                step=step["step"],
                action=step["action"],
                project=ProjectOut.model_validate(step["project"]),
                issue=IssueOut.model_validate(step["issue"]),
                label=step["label"],
                prior=step["prior"],
                intended=step["intended"],
                status=done.get("status", "pending"),
                request_status=done.get("request_status"),
                verified=done.get("verified"),
                message=done.get("message"),
                finished_at=done.get("finished_at"),
            )
        )
    reasons = plans.apply_refusals(db, row)
    summary = _summary(row, state, names)
    return IssuePlanOut(
        **summary.model_dump(),
        rules=_rules(row.plan.get("rules", {})),
        step_items=steps,
        manual_items=[ManualOut.model_validate(item) for item in row.plan.get("manual", [])],
        digest_valid=plans.digest_valid(row),
        submitted_at=row.submitted_at,
        decision_note=row.decision_note,
        review_required=row.review_required,
        self_approved=row.self_approved,
        apply_started_at=row.apply_started_at,
        failed_step=row.failed_step,
        failure=row.failure,
        progress=dict(Counter(step.status for step in steps)),
        can_apply=CanApply(allowed=not reasons, reasons=reasons),
    )


@router.post(
    "",
    response_model=IssuePlanOut,
    status_code=status.HTTP_201_CREATED,
    summary="Build an issue plan from the closed issues as last collected",
    description="Plans the labels to add or remove and the estimates to set on the closed "
    "issues of the projects in `scope.groups` (every monitored project when empty). Reads, "
    "with the read-only token, whether each project with a label to add defines it; issues "
    "of a project that does not are listed under `manual`. 409 `nothing_to_plan` when every "
    f"issue follows the rules, `scope_too_large` above {MAX_ISSUE_PLAN_STEPS} steps. Requires "
    "the `X-CSRF-Token` header. Audited.",
)
def create_plan(
    body: PlanIn,
    principal: Annotated[Principal, Depends(require(Permission.REMEDIATION_PLAN))],
    db: Database,
    transport: Annotated[httpx.BaseTransport | None, Depends(gitlab_transport)],
) -> IssuePlanOut:
    settings = _settings()
    instance = _instance(db)
    if settings is None or instance is None:
        raise _refusal(
            status.HTTP_409_CONFLICT, "gitlab_not_configured", "GitLab is not configured"
        )
    rules = time_settings.load(db)
    items = plans.candidates(db, instance.id, body.scope.groups)
    projects = plans.label_projects(items, rules)
    db.commit()  # release the session's snapshot while GitLab is read
    with GitLabClient(settings, transport=transport) as client:
        available = plans.read_label_availability(client, projects, rules.missing_time_label)
    built = plans.build(items, rules, available)
    try:
        row = plans.create(
            db,
            instance,
            built,
            rules,
            body.scope.groups,
            Actor(principal.user_id, principal.actor),
            review_required(db),
        )
    except NothingToPlan as exc:
        raise _refusal(status.HTTP_409_CONFLICT, "nothing_to_plan", str(exc)) from None
    except ScopeTooLarge as exc:
        raise _refusal(status.HTTP_409_CONFLICT, "scope_too_large", str(exc)) from None
    db.commit()
    return _plan_out(db, row.id)


@router.get(
    "",
    response_model=IssuePlanPage,
    summary="Issue plans",
    description="Filter by `state` (with `expired` counting any open plan past `expires_at`). "
    "Newest first; follow `next_after_id`.",
)
def list_plans(
    _: Reader,
    db: Database,
    state: PlanState | None = None,
    after_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> IssuePlanPage:
    instance = _instance(db)
    statement = select(IssuePlan, plans.effective_state()).where(
        IssuePlan.instance_id == (instance.id if instance else 0)
    )
    if state is not None:
        statement = statement.where(plans.effective_state() == state)
    if after_id is not None:
        statement = statement.where(IssuePlan.id < after_id)
    rows = list(db.execute(statement.order_by(IssuePlan.id.desc()).limit(limit + 1)).all())
    names = _people(db, {u for r, _ in rows for u in (r.created_by_user_id, r.decided_by_user_id)})
    items = [_summary(row, row_state, names) for row, row_state in rows[:limit]]
    return IssuePlanPage(items=items, next_after_id=items[-1].id if len(rows) > limit else None)


@router.get(
    "/{plan_id}",
    response_model=IssuePlanOut,
    summary="One issue plan, with its steps, manual items and apply progress",
    responses={404: {"description": "No such issue plan"}},
)
def read_plan(plan_id: Annotated[int, Path(gt=0)], _: Reader, db: Database) -> IssuePlanOut:
    return _plan_out(db, plan_id)


def _transition(
    db: Session,
    plan_id: int,
    principal: Principal,
    action: Literal["submit", "approved", "rejected", "apply"],
    note: str | None = None,
    digest: str | None = None,
) -> IssuePlanOut:
    _plan_out(db, plan_id)  # 404 unless it belongs to the configured instance
    row = plans.locked(db, plan_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such issue plan")
    by = Actor(principal.user_id, principal.actor)
    try:
        if action == "submit":
            plans.submit(db, row, by)
        elif action == "apply":
            plans.start_apply(db, row, by)
        else:
            plans.decide(db, row, action, by, note, digest)
    except PlanConflict as exc:
        reasons = plans.apply_refusals(db, row) if action == "apply" else [exc.code]
        raise _refusal(status.HTTP_409_CONFLICT, exc.code, str(exc), reasons) from None
    except PermissionError as exc:
        raise _refusal(status.HTTP_403_FORBIDDEN, "own_change", str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    db.commit()
    return _plan_out(db, plan_id)


_TRANSITION_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {"description": "Missing permission, CSRF failure, or not allowed for this user"},
    404: {"description": "No such issue plan"},
    409: {"description": "Not allowed now; `reasons` lists every reason code"},
}


@router.post(
    "/{plan_id}/submit",
    response_model=IssuePlanOut,
    summary="Submit a draft plan for approval",
    description="Only the plan's creator. While review is not required the plan is approved at "
    "once and `self_approved`. Requires the `X-CSRF-Token` header.",
    responses=_TRANSITION_RESPONSES,
)
def submit_plan(
    plan_id: Annotated[int, Path(gt=0)],
    principal: Annotated[Principal, Depends(require(Permission.REMEDIATION_PLAN))],
    db: Database,
) -> IssuePlanOut:
    return _transition(db, plan_id, principal, "submit")


@router.post(
    "/{plan_id}/approve",
    response_model=IssuePlanOut,
    summary="Approve a submitted plan",
    description="Only someone other than its creator, before `expires_at`, naming the "
    "`plan_digest` they reviewed (409 `digest_mismatch` otherwise). Requires the "
    "`X-CSRF-Token` header.",
    responses=_TRANSITION_RESPONSES,
)
def approve_plan(
    plan_id: Annotated[int, Path(gt=0)],
    body: ApprovePlanIn,
    principal: Annotated[Principal, Depends(require(Permission.REMEDIATION_APPROVE))],
    db: Database,
) -> IssuePlanOut:
    return _transition(db, plan_id, principal, "approved", body.note, body.plan_digest)


@router.post(
    "/{plan_id}/reject",
    response_model=IssuePlanOut,
    summary="Reject a submitted plan",
    description="Only someone other than its creator; `note` says why. Requires the "
    "`X-CSRF-Token` header.",
    responses=_TRANSITION_RESPONSES,
)
def reject_plan(
    plan_id: Annotated[int, Path(gt=0)],
    body: RejectIn,
    principal: Annotated[Principal, Depends(require(Permission.REMEDIATION_APPROVE))],
    db: Database,
) -> IssuePlanOut:
    return _transition(db, plan_id, principal, "rejected", body.note)


@router.post(
    "/{plan_id}/apply",
    response_model=IssuePlanOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Apply an approved plan to GitLab",
    description="Queues the applier and returns the plan `applying`; follow its steps' "
    "progress. Refused (409, every reason listed) unless `BALUARTE_ISSUE_WRITES` is true, the "
    "write token is stored, and the plan is approved and unexpired. Each issue is read first: "
    "a change already in place is `skipped`, an issue that changed since the plan is `stale`. "
    "Each write is sent once and read back; the plan stops at the first refused write or "
    "mismatch. Requires the `X-CSRF-Token` header. Audited.",
    responses=_TRANSITION_RESPONSES,
)
def apply_plan(
    plan_id: Annotated[int, Path(gt=0)],
    principal: Annotated[Principal, Depends(require(Permission.REMEDIATION_PLAN))],
    db: Database,
) -> IssuePlanOut:
    return _transition(db, plan_id, principal, "apply")
