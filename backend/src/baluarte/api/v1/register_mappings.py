"""Issue-to-control mappings and audit periods (roadmap M12.3, docs/ISSUE_REGISTER.md).

Everyone who can read evidence sees the mappings, their cited evidence and gaps, and the
coverage of controls by auditable issues. ``register:write`` proposes mappings and manages audit
periods; ``policy:write`` approves, rejects and withdraws mappings, like control mapping
changes, and nobody decides their own proposal outside development mode. Everything is within
the request's workspace and audited.
"""

from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.controls.catalogue import control
from baluarte.db.models import (
    MAX_ITEM_RATIONALE,
    MAX_MAPPING_RATIONALE,
    MAX_PERIOD_NAME,
    MIN_ITEM_RATIONALE,
    MIN_MAPPING_RATIONALE,
    AppUser,
    AuditPeriod,
    GitLabProject,
    IssueControlMapping,
    RegisterIssue,
)
from baluarte.policy.changes import Actor, ChangeConflict, NotAllowed, review_required
from baluarte.register import mapping as issue_mapping
from baluarte.register.mapping import GAP_MESSAGES, MAX_ITEMS, MappingConflict, Period
from baluarte.settings import own_decision_refused
from baluarte.workspaces import current_workspace_id

router = APIRouter(prefix="/register", tags=["issue register"])

Reader = Annotated[Principal, Depends(require(Permission.EVIDENCE_READ))]
Writer = Annotated[Principal, Depends(require(Permission.REGISTER_WRITE))]
Decider = Annotated[Principal, Depends(require(Permission.POLICY_WRITE))]
FrameworkId = Literal["soc2", "iso27001", "nist_800_53_moderate"]
MappingState = Literal["proposed", "active", "rejected", "withdrawn"]


class Person(BaseModel):
    actor: str
    user_id: int | None
    display_name: str | None


class GapOut(BaseModel):
    code: str
    message: str


class CitedOut(BaseModel):
    key: str
    kind: str
    label: str
    occurred_at: datetime | None
    rationale: str
    # Against the requested audit period: before, in_period, after or undated; null without one.
    timing: str | None


class MappingOut(BaseModel):
    id: int
    project_id: int
    project: str
    iid: int
    issue_title: str | None
    framework: FrameworkId
    control_id: str
    control_title: str | None
    rationale: str
    state: MappingState
    observation_id: int
    supersedes_id: int | None
    mapping_version: str
    review_required: bool
    self_approved: bool
    development_decision: bool
    proposed_by: Person
    proposed_at: datetime
    decided_by: Person | None
    decided_at: datetime | None
    decision_note: str | None
    withdrawn_by: str | None
    withdrawn_at: datetime | None
    withdraw_reason: str | None
    evidence: list[CitedOut]
    gaps: list[GapOut]
    # Whether the caller may approve or reject it now.
    can_decide: bool


class MappingPage(BaseModel):
    review_required: bool
    items: list[MappingOut]
    next_before_id: int | None


class CitableOut(BaseModel):
    key: str
    kind: str
    label: str
    occurred_at: datetime | None


class IssueMappings(BaseModel):
    project_id: int
    iid: int
    review_required: bool
    # The capture a new mapping would rest on, and what it can cite; null when not captured.
    observation_id: int | None
    citable: list[CitableOut]
    items: list[MappingOut]


def _user_id(actor: str | None) -> int | None:
    if actor and actor.startswith("user:") and actor[5:].isdigit():
        return int(actor[5:])
    return None


def _names(db: Session, rows: Sequence[IssueControlMapping]) -> dict[int, str | None]:
    ids = {r.proposed_by_user_id for r in rows} | {
        r.decided_by_user_id for r in rows if r.decided_by_user_id is not None
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


def _period(db: Session, period_id: int | None) -> Period | None:
    if period_id is None:
        return None
    row = db.get(AuditPeriod, period_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such audit period")
    return Period(row.starts_on, row.ends_on)


def _out(
    db: Session,
    principal: Principal,
    rows: Sequence[IssueControlMapping],
    period: Period | None,
) -> list[MappingOut]:
    views = issue_mapping.assess(db, rows, period)
    names = _names(db, rows)
    paths = dict(
        db.execute(
            select(GitLabProject.id, GitLabProject.path_with_namespace).where(
                GitLabProject.id.in_({r.project_id for r in rows})
            )
        ).all()
    )
    titles: dict[tuple[int, int], str | None] = {}
    if rows:
        for project_id, iid, title in db.execute(
            select(RegisterIssue.project_id, RegisterIssue.issue_iid, RegisterIssue.title).where(
                RegisterIssue.project_id.in_({r.project_id for r in rows})
            )
        ):
            titles[(project_id, iid)] = title
    may_decide = Permission.POLICY_WRITE in principal.permissions
    result = []
    for view in views:
        row = view.mapping
        found = control(row.framework, row.control_id)
        result.append(
            MappingOut(
                id=row.id,
                project_id=row.project_id,
                project=paths.get(row.project_id, ""),
                iid=row.issue_iid,
                issue_title=titles.get((row.project_id, row.issue_iid)),
                framework=row.framework,  # type: ignore[arg-type]
                control_id=row.control_id,
                control_title=found.title if found else None,
                rationale=row.rationale,
                state=row.state,  # type: ignore[arg-type]
                observation_id=row.observation_id,
                supersedes_id=row.supersedes_id,
                mapping_version=row.mapping_version,
                review_required=row.review_required,
                self_approved=row.self_approved,
                development_decision=row.development_decision,
                proposed_by=_person(row.proposed_by, row.proposed_by_user_id, names),
                proposed_at=row.proposed_at,
                decided_by=_person(row.decided_by, row.decided_by_user_id, names)
                if row.decided_by
                else None,
                decided_at=row.decided_at,
                decision_note=row.decision_note,
                withdrawn_by=row.withdrawn_by,
                withdrawn_at=row.withdrawn_at,
                withdraw_reason=row.withdraw_reason,
                evidence=[
                    CitedOut(
                        key=item.item_key,
                        kind=item.item_kind,
                        label=item.item_label,
                        occurred_at=item.occurred_at,
                        rationale=item.rationale,
                        timing=view.timings.get(item.item_key),
                    )
                    for item in view.evidence
                ],
                gaps=[GapOut(code=gap, message=GAP_MESSAGES[gap]) for gap in view.gaps],
                can_decide=row.state == "proposed"
                and may_decide
                and not own_decision_refused(row.proposed_by_user_id, principal.user_id),
            )
        )
    return result


def _conflict(error: ChangeConflict) -> HTTPException:
    """409 with ``{code, message, reasons: [{code, message}]}``; ``code`` is the first reason."""
    if isinstance(error, MappingConflict):
        reasons = [{"code": r, "message": issue_mapping.MESSAGES[r]} for r in error.reasons]
    else:
        reasons = [{"code": "not_proposed", "message": str(error)}]
    detail = {"code": reasons[0]["code"], "message": str(error), "reasons": reasons}
    return HTTPException(status.HTTP_409_CONFLICT, detail=detail)


def _own(error: NotAllowed) -> HTTPException:
    return HTTPException(
        status.HTTP_403_FORBIDDEN, detail={"code": "own_change", "message": str(error)}
    )


@router.get(
    "/issues/{project_id}/{iid}/mappings",
    response_model=IssueMappings,
    summary="An issue's control mappings, and what a new one could cite",
)
def issue_mappings(
    project_id: Annotated[int, Path(ge=1)],
    iid: Annotated[int, Path(ge=1)],
    principal: Reader,
    db: Annotated[Session, Depends(database)],
    period_id: Annotated[int | None, Query(gt=0)] = None,
) -> IssueMappings:
    if db.get(GitLabProject, project_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such project")
    period = _period(db, period_id)
    rows = db.scalars(
        select(IssueControlMapping)
        .where(IssueControlMapping.project_id == project_id, IssueControlMapping.issue_iid == iid)
        .order_by(IssueControlMapping.id.desc())
    ).all()
    capture = issue_mapping.latest_capture(db, project_id, iid)
    citable = issue_mapping.items(capture.payload or {}) if capture is not None else {}
    return IssueMappings(
        project_id=project_id,
        iid=iid,
        review_required=review_required(db),
        observation_id=capture.id if capture is not None else None,
        citable=[
            CitableOut(key=i.key, kind=i.kind, label=i.label, occurred_at=i.at)
            for i in citable.values()
        ],
        items=_out(db, principal, rows, period),
    )


@router.get(
    "/mappings",
    response_model=MappingPage,
    summary="Issue-to-control mappings, newest first",
)
def list_mappings(
    principal: Reader,
    db: Annotated[Session, Depends(database)],
    state: MappingState | None = None,
    framework: FrameworkId | None = None,
    control_id: Annotated[str | None, Query(min_length=1, max_length=20)] = None,
    period_id: Annotated[int | None, Query(gt=0)] = None,
    before_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> MappingPage:
    period = _period(db, period_id)
    statement = select(IssueControlMapping).order_by(IssueControlMapping.id.desc()).limit(limit + 1)
    if state is not None:
        statement = statement.where(IssueControlMapping.state == state)
    if framework is not None:
        statement = statement.where(IssueControlMapping.framework == framework)
    if control_id is not None:
        statement = statement.where(IssueControlMapping.control_id == control_id)
    if before_id is not None:
        statement = statement.where(IssueControlMapping.id < before_id)
    rows = db.scalars(statement).all()
    items = _out(db, principal, rows[:limit], period)
    return MappingPage(
        review_required=review_required(db),
        items=items,
        next_before_id=items[-1].id if len(rows) > limit else None,
    )


class CitedIn(BaseModel):
    key: str = Field(min_length=1, max_length=200)
    rationale: str = Field(min_length=MIN_ITEM_RATIONALE, max_length=MAX_ITEM_RATIONALE)

    @field_validator("rationale")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if len(value) < MIN_ITEM_RATIONALE:
            raise ValueError(f"must be at least {MIN_ITEM_RATIONALE} characters")
        return value


class MappingCreate(BaseModel):
    project_id: int = Field(ge=1)
    iid: int = Field(ge=1)
    framework: FrameworkId
    control_id: str = Field(min_length=1, max_length=20)
    rationale: str = Field(min_length=MIN_MAPPING_RATIONALE, max_length=MAX_MAPPING_RATIONALE)
    items: list[CitedIn] = Field(min_length=1, max_length=MAX_ITEMS)
    supersedes_id: int | None = Field(default=None, gt=0)

    @field_validator("rationale")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if len(value) < MIN_MAPPING_RATIONALE:
            raise ValueError(f"must be at least {MIN_MAPPING_RATIONALE} characters")
        return value


@router.post(
    "/mappings",
    response_model=MappingOut,
    status_code=status.HTTP_201_CREATED,
    summary="Map an auditable issue to a control, citing its captured evidence",
    description="Rests on the issue's latest capture; every cited item needs its own rationale. "
    "`supersedes_id` revises a mapping in force, which is withdrawn when the revision takes "
    "effect. While review is required the mapping is `proposed` until someone else with "
    "`policy:write` approves it; otherwise it is `active` and `self_approved`. Requires the "
    "`X-CSRF-Token` header. Audited.",
    responses={409: {"description": "It cannot be made; `reasons` says why"}},
)
def create_mapping(
    body: MappingCreate, principal: Writer, db: Annotated[Session, Depends(database)]
) -> MappingOut:
    if db.get(GitLabProject, body.project_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such project")
    proposal = issue_mapping.Proposal(
        project_id=body.project_id,
        issue_iid=body.iid,
        framework=body.framework,
        control_id=body.control_id,
        rationale=body.rationale,
        items=tuple(issue_mapping.CitedItem(i.key, i.rationale) for i in body.items),
        supersedes_id=body.supersedes_id,
    )
    try:
        row = issue_mapping.propose(db, proposal, Actor(principal.user_id, principal.actor))
    except ChangeConflict as error:
        db.rollback()
        raise _conflict(error) from None
    db.commit()
    return _out(db, principal, [row], None)[0]


class DecisionIn(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class RejectionIn(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


class WithdrawalIn(BaseModel):
    reason: str = Field(min_length=MIN_MAPPING_RATIONALE, max_length=MAX_MAPPING_RATIONALE)


def _decided(
    db: Session, principal: Principal, mapping_id: int, decide: str, text: str | None
) -> MappingOut:
    by = Actor(principal.user_id, principal.actor)
    try:
        if decide == "approve":
            row = issue_mapping.approve(db, mapping_id, by, text)
        elif decide == "reject":
            row = issue_mapping.reject(db, mapping_id, by, text or "")
        else:
            row = issue_mapping.withdraw(db, mapping_id, by, text or "")
    except LookupError:
        db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such mapping") from None
    except NotAllowed as error:
        db.rollback()
        raise _own(error) from None
    except ChangeConflict as error:
        db.rollback()
        raise _conflict(error) from None
    db.commit()
    return _out(db, principal, [row], None)[0]


@router.post(
    "/mappings/{mapping_id}/approve",
    response_model=MappingOut,
    summary="Approve a proposed mapping",
    description="By someone other than its proposer outside development mode. Requires the "
    "`X-CSRF-Token` header. Audited.",
)
def approve_mapping(
    mapping_id: Annotated[int, Path(ge=1)],
    body: DecisionIn,
    principal: Decider,
    db: Annotated[Session, Depends(database)],
) -> MappingOut:
    return _decided(db, principal, mapping_id, "approve", body.note)


@router.post(
    "/mappings/{mapping_id}/reject",
    response_model=MappingOut,
    summary="Reject a proposed mapping, with a note",
    description="Requires the `X-CSRF-Token` header. Audited.",
)
def reject_mapping(
    mapping_id: Annotated[int, Path(ge=1)],
    body: RejectionIn,
    principal: Decider,
    db: Annotated[Session, Depends(database)],
) -> MappingOut:
    return _decided(db, principal, mapping_id, "reject", body.note)


@router.post(
    "/mappings/{mapping_id}/withdraw",
    response_model=MappingOut,
    summary="Withdraw a mapping in force, with a reason",
    description="Requires the `X-CSRF-Token` header. Audited.",
)
def withdraw_mapping(
    mapping_id: Annotated[int, Path(ge=1)],
    body: WithdrawalIn,
    principal: Decider,
    db: Annotated[Session, Depends(database)],
) -> MappingOut:
    return _decided(db, principal, mapping_id, "withdraw", body.reason.strip())


class CoverageRow(BaseModel):
    framework: FrameworkId
    control_id: str
    control_title: str | None
    issues: int
    proposed: int
    # Active mappings with at least one cited item dated inside the period; null without one.
    in_period: int | None
    gaps: dict[str, int]


class Coverage(BaseModel):
    period_id: int | None
    rows: list[CoverageRow]


@router.get(
    "/coverage",
    response_model=Coverage,
    summary="Controls with auditable issues mapped to them, and their gaps",
)
def coverage(
    principal: Reader,
    db: Annotated[Session, Depends(database)],
    period_id: Annotated[int | None, Query(gt=0)] = None,
    framework: FrameworkId | None = None,
) -> Coverage:
    del principal
    period = _period(db, period_id)
    statement = select(IssueControlMapping).where(
        IssueControlMapping.state.in_(issue_mapping.OPEN_STATES)
    )
    if framework is not None:
        statement = statement.where(IssueControlMapping.framework == framework)
    views = issue_mapping.assess(db, db.scalars(statement).all(), period)
    grouped: dict[tuple[str, str], list[issue_mapping.MappingView]] = defaultdict(list)
    for view in views:
        grouped[(view.mapping.framework, view.mapping.control_id)].append(view)
    rows = []
    for (fw, control_id), group in sorted(grouped.items()):
        found = control(fw, control_id)
        active = [v for v in group if v.mapping.state == "active"]
        gaps: dict[str, int] = defaultdict(int)
        for view in group:
            for gap in view.gaps:
                gaps[gap] += 1
        rows.append(
            CoverageRow(
                framework=fw,  # type: ignore[arg-type]
                control_id=control_id,
                control_title=found.title if found else None,
                issues=len({(v.mapping.project_id, v.mapping.issue_iid) for v in active}),
                proposed=len(group) - len(active),
                in_period=None
                if period is None
                else sum(1 for v in active if "in_period" in v.timings.values()),
                gaps=dict(gaps),
            )
        )
    return Coverage(period_id=period_id, rows=rows)


class PeriodOut(BaseModel):
    id: int
    name: str
    starts_on: date
    ends_on: date
    created_by: str
    created_at: datetime
    ended_by: str | None
    ended_at: datetime | None


class PeriodList(BaseModel):
    items: list[PeriodOut]


def _period_out(row: AuditPeriod) -> PeriodOut:
    return PeriodOut.model_validate(row, from_attributes=True)


@router.get("/periods", response_model=PeriodList, summary="Audit periods, latest first")
def list_periods(_: Reader, db: Annotated[Session, Depends(database)]) -> PeriodList:
    return PeriodList(items=[_period_out(row) for row in issue_mapping.periods(db)])


class PeriodCreate(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_PERIOD_NAME)
    starts_on: date
    ends_on: date

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


@router.post(
    "/periods",
    response_model=PeriodOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add an audit period",
    description="Periods may overlap. Requires the `X-CSRF-Token` header. Audited.",
)
def create_period(
    body: PeriodCreate, principal: Writer, db: Annotated[Session, Depends(database)]
) -> PeriodOut:
    if body.ends_on < body.starts_on:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "The period must end on or after its start"
        )
    row = AuditPeriod(
        workspace_id=current_workspace_id(db),
        name=body.name,
        starts_on=body.starts_on,
        ends_on=body.ends_on,
        created_by=principal.actor,
    )
    db.add(row)
    db.flush()
    audit.record(
        db,
        actor=principal.actor,
        action="register.period_created",
        resource_type="audit_period",
        resource_id=str(row.id),
        details={
            "name": row.name,
            "starts_on": row.starts_on.isoformat(),
            "ends_on": row.ends_on.isoformat(),
        },
    )
    db.commit()
    return _period_out(row)


@router.post(
    "/periods/{period_id}/end",
    response_model=PeriodOut,
    summary="End an audit period",
    description="It stays listed, ended. Requires the `X-CSRF-Token` header. Audited.",
)
def end_period(
    period_id: Annotated[int, Path(ge=1)],
    principal: Writer,
    db: Annotated[Session, Depends(database)],
) -> PeriodOut:
    row = db.get(AuditPeriod, period_id, with_for_update=True)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such audit period")
    if row.ended_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "The audit period is already ended")
    row.ended_by = principal.actor
    row.ended_at = datetime.now().astimezone()
    db.flush()
    audit.record(
        db,
        actor=principal.actor,
        action="register.period_ended",
        resource_type="audit_period",
        resource_id=str(row.id),
        details={"name": row.name},
    )
    db.commit()
    return _period_out(row)
