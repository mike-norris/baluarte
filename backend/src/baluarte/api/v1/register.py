"""The auditable issue register (roadmap M12.1, ADR-0015, docs/ISSUE_REGISTER.md).

Everyone who can read evidence lists the register, with why each issue is auditable and its
category. Admins, analysts and auditors (``register:write``) add and end selection rules, include
or exclude one issue with a reason, and ask for the issues to be read now. Every change is
audited. Everything is within the request's workspace.
"""

import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from baluarte import audit, connections
from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.db.models import (
    MAX_REGISTER_REASON,
    MIN_REGISTER_REASON,
    CapturedFile,
    GitLabProject,
    IssueEvidence,
    Observation,
    RegisterCursor,
    RegisterDecision,
    RegisterIssue,
    RegisterRule,
)
from baluarte.evidence.observations import verify
from baluarte.integrations.gitlab.issue_evidence import SOURCE as EVIDENCE_SOURCE
from baluarte.integrations.gitlab.jobs import COLLECT_ISSUES
from baluarte.jobs import queue
from baluarte.register import selection
from baluarte.workspaces import current_workspace_id

router = APIRouter(prefix="/register", tags=["issue register"])

Reader = Annotated[Principal, Depends(require(Permission.EVIDENCE_READ))]
Writer = Annotated[Principal, Depends(require(Permission.REGISTER_WRITE))]
Category = Literal["engineering", "security_finding", "exception", "remediation"]
IssueType = Literal["issue", "incident", "task", "test_case", "requirement", "ticket"]

MAX_CRITERIA = 50


class ReasonOut(BaseModel):
    kind: Literal["rule", "included", "excluded"]
    rule_id: int | None = None
    rule_name: str | None = None
    decision_id: int | None = None
    reason: str | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None


class RegisterIssueOut(BaseModel):
    project_id: int
    project: str
    connection_id: int
    iid: int
    issue_type: str | None
    title: str | None
    state: str | None
    labels: list[str]
    author: str | None
    assignees: list[str]
    milestone: str | None
    web_url: str | None
    confidential: bool
    opened_at: datetime | None
    updated_at: datetime | None
    closed_at: datetime | None
    read_at: datetime
    category: Category
    excluded: bool
    why: list[ReasonOut]


class PendingInclusion(BaseModel):
    """An issue included by hand that has not been read yet."""

    project_id: int
    project: str
    iid: int
    decision_id: int
    reason: str
    decided_by: str
    decided_at: datetime


class RegisterPage(BaseModel):
    items: list[RegisterIssueOut]
    total: int
    # Included issues not read yet (first page only); they appear once collected.
    pending: list[PendingInclusion]
    # Whether the workspace has a register at all (a rule or an open decision).
    active: bool
    # When the issues were last read, over all the workspace's projects.
    last_read_at: datetime | None


def _reason(reason: selection.Reason) -> ReasonOut:
    return ReasonOut(**reason.__dict__)


def _out(
    row: RegisterIssue,
    project: str,
    category: str,
    excluded: bool,
    rules: list[RegisterRule],
    decision: RegisterDecision | None,
) -> RegisterIssueOut:
    return RegisterIssueOut(
        project_id=row.project_id,
        project=project,
        connection_id=row.instance_id,
        iid=row.issue_iid,
        issue_type=row.issue_type,
        title=row.title,
        state=row.state,
        labels=list(row.labels),
        author=row.author,
        assignees=list(row.assignees),
        milestone=row.milestone,
        web_url=row.web_url,
        confidential=row.confidential,
        opened_at=row.opened_at,
        updated_at=row.source_updated_at,
        closed_at=row.closed_at,
        read_at=row.read_at,
        category=category,  # type: ignore[arg-type]
        excluded=excluded,
        why=[_reason(r) for r in selection.reasons(row, rules, decision)],
    )


def _pending(db: Session, workspace_id: int) -> list[PendingInclusion]:
    rows = db.execute(
        select(RegisterDecision, GitLabProject.path_with_namespace)
        .join(GitLabProject, GitLabProject.id == RegisterDecision.project_id)
        .outerjoin(
            RegisterIssue,
            (RegisterIssue.project_id == RegisterDecision.project_id)
            & (RegisterIssue.issue_iid == RegisterDecision.issue_iid),
        )
        .where(
            RegisterDecision.workspace_id == workspace_id,
            RegisterDecision.ended_at.is_(None),
            RegisterDecision.decision == "include",
            RegisterIssue.project_id.is_(None),
        )
        .order_by(RegisterDecision.id)
    ).all()
    return [
        PendingInclusion(
            project_id=decision.project_id,
            project=path,
            iid=decision.issue_iid,
            decision_id=decision.id,
            reason=decision.reason,
            decided_by=decision.decided_by,
            decided_at=decision.decided_at,
        )
        for decision, path in rows
    ]


def _active(db: Session, workspace_id: int) -> bool:
    rule = db.scalars(
        select(RegisterRule.id).where(
            RegisterRule.workspace_id == workspace_id, RegisterRule.ended_at.is_(None)
        )
    ).first()
    decision = db.scalars(
        select(RegisterDecision.id).where(
            RegisterDecision.workspace_id == workspace_id, RegisterDecision.ended_at.is_(None)
        )
    ).first()
    return rule is not None or decision is not None


@router.get(
    "/issues",
    response_model=RegisterPage,
    summary="List auditable issues",
    description="Issues a selection rule matches or someone included, with why and their "
    "category. `period_start` and `period_end` keep issues open at any time in the period. "
    "Excluded issues are listed only with `include_excluded`.",
)
def list_register(
    _: Reader,
    db: Annotated[Session, Depends(database)],
    connection_id: int | None = None,
    project_id: int | None = None,
    category: Category | None = None,
    rule_id: int | None = None,
    state: Literal["opened", "closed"] | None = None,
    issue_type: str | None = Query(default=None, max_length=50),
    label: str | None = Query(default=None, max_length=255),
    assignee: str | None = Query(default=None, max_length=255),
    q: str | None = Query(default=None, max_length=200),
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    include_excluded: bool = False,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> RegisterPage:
    workspace_id = current_workspace_id(db)
    rules = selection.active_rules(db, workspace_id)
    filters = selection.Filters(
        instance_id=connection_id,
        project_id=project_id,
        category=category,
        rule_id=rule_id,
        state=state,
        issue_type=issue_type,
        label=label,
        assignee=assignee,
        text=q,
        period_start=period_start,
        period_end=period_end,
        include_excluded=include_excluded,
    )
    statement = selection.register_query(workspace_id, rules, filters)
    total = selection.count(db, statement)
    rows = db.execute(
        statement.order_by(
            RegisterIssue.source_updated_at.desc().nulls_last(),
            RegisterIssue.project_id,
            RegisterIssue.issue_iid,
        )
        .offset(offset)
        .limit(limit)
    ).all()
    decisions = selection.open_decisions(
        db, workspace_id, [(row.project_id, row.issue_iid) for row, *_ in rows]
    )
    last_read = db.execute(
        select(RegisterCursor.read_at)
        .join(GitLabProject, GitLabProject.id == RegisterCursor.project_id)
        .order_by(RegisterCursor.read_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return RegisterPage(
        items=[
            _out(row, path, cat, excl, rules, decisions.get((row.project_id, row.issue_iid)))
            for row, path, cat, excl in rows
        ],
        total=total,
        pending=_pending(db, workspace_id) if offset == 0 else [],
        active=bool(rules) or _active(db, workspace_id),
        last_read_at=last_read,
    )


class DecisionOut(BaseModel):
    id: int
    project_id: int
    iid: int
    decision: Literal["include", "exclude"]
    category: Category | None
    reason: str
    decided_by: str
    decided_at: datetime
    ended_by: str | None
    ended_at: datetime | None


def _decision_out(row: RegisterDecision) -> DecisionOut:
    return DecisionOut(
        id=row.id,
        project_id=row.project_id,
        iid=row.issue_iid,
        decision=row.decision,  # type: ignore[arg-type]
        category=row.category,  # type: ignore[arg-type]
        reason=row.reason,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        ended_by=row.ended_by,
        ended_at=row.ended_at,
    )


class IssueDetail(BaseModel):
    project_id: int
    project: str
    iid: int
    # None when the issue has not been read yet.
    issue: RegisterIssueOut | None
    # Whether it is in the register now (selected or included, and not excluded).
    auditable: bool
    # Every decision ever made on it, newest first.
    decisions: list[DecisionOut]


def _project(db: Session, project_id: int) -> GitLabProject:
    project = db.get(GitLabProject, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such project")
    return project


@router.get(
    "/issues/{project_id}/{iid}",
    response_model=IssueDetail,
    summary="One issue: why it is or is not auditable, and every decision on it",
    responses={404: {"description": "No such project in this workspace"}},
)
def issue_detail(
    project_id: Annotated[int, Path(ge=1)],
    iid: Annotated[int, Path(ge=1)],
    _: Reader,
    db: Annotated[Session, Depends(database)],
) -> IssueDetail:
    workspace_id = current_workspace_id(db)
    project = _project(db, project_id)
    rules = selection.active_rules(db, workspace_id)
    decisions = list(
        db.scalars(
            select(RegisterDecision)
            .where(
                RegisterDecision.workspace_id == workspace_id,
                RegisterDecision.project_id == project_id,
                RegisterDecision.issue_iid == iid,
            )
            .order_by(RegisterDecision.id.desc())
        )
    )
    open_decision = next((d for d in decisions if d.ended_at is None), None)
    row = db.execute(
        selection.register_query(
            workspace_id, rules, selection.Filters(project_id=project_id, include_excluded=True)
        ).where(RegisterIssue.issue_iid == iid)
    ).first()
    issue: RegisterIssueOut | None = None
    if row is not None:
        found, path, cat, excl = row
        issue = _out(found, path, cat, excl, rules, open_decision)
    else:
        stored = db.get(RegisterIssue, (project_id, iid))
        if stored is not None:
            issue = _out(
                stored,
                project.path_with_namespace,
                selection.DEFAULT_CATEGORY,
                False,
                rules,
                open_decision,
            )
    auditable = row is not None and not row[3]
    return IssueDetail(
        project_id=project_id,
        project=project.path_with_namespace,
        iid=iid,
        issue=issue,
        auditable=auditable,
        decisions=[_decision_out(d) for d in decisions],
    )


class RuleOut(BaseModel):
    id: int
    name: str
    category: Category
    labels: list[str]
    project_ids: list[int]
    projects: list[str]
    issue_types: list[str]
    created_by: str
    created_at: datetime
    ended_by: str | None
    ended_at: datetime | None


def _rule_out(db: Session, row: RegisterRule) -> RuleOut:
    paths = dict(
        db.execute(
            select(GitLabProject.id, GitLabProject.path_with_namespace).where(
                GitLabProject.id.in_(list(row.project_ids))
            )
        ).all()
    )
    return RuleOut(
        id=row.id,
        name=row.name,
        category=row.category,  # type: ignore[arg-type]
        labels=list(row.labels),
        project_ids=list(row.project_ids),
        projects=[paths.get(pid, f"project {pid}") for pid in row.project_ids],
        issue_types=list(row.issue_types),
        created_by=row.created_by,
        created_at=row.created_at,
        ended_by=row.ended_by,
        ended_at=row.ended_at,
    )


class RuleList(BaseModel):
    rules: list[RuleOut]


@router.get("/rules", response_model=RuleList, summary="List selection rules")
def list_rules(
    _: Reader,
    db: Annotated[Session, Depends(database)],
    include_ended: bool = False,
) -> RuleList:
    statement = select(RegisterRule).where(RegisterRule.workspace_id == current_workspace_id(db))
    if not include_ended:
        statement = statement.where(RegisterRule.ended_at.is_(None))
    rows = db.scalars(statement.order_by(RegisterRule.id))
    return RuleList(rules=[_rule_out(db, row) for row in rows])


def _clean(values: list[str], what: str) -> list[str]:
    cleaned = sorted({value.strip() for value in values if value.strip()})
    if len(cleaned) > MAX_CRITERIA:
        raise ValueError(f"At most {MAX_CRITERIA} {what}")
    if any(len(value) > 255 for value in cleaned):
        raise ValueError(f"{what.capitalize()} are at most 255 characters")
    return cleaned


class RuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    category: Category
    labels: list[str] = []
    project_ids: list[int] = Field(default=[], max_length=MAX_CRITERIA)
    issue_types: list[IssueType] = []

    @field_validator("labels")
    @classmethod
    def _labels(cls, value: list[str]) -> list[str]:
        return _clean(value, "labels")


def _queue_collection(db: Session, reason: str) -> list[int]:
    """Queue the issue collection for every active connection of the request's workspace."""
    minute = datetime.now(UTC).strftime("%Y%m%dT%H%M")
    return [
        queue.enqueue(
            db,
            COLLECT_ISSUES,
            f"{COLLECT_ISSUES}:{reason}:connection-{row.id}:{minute}",
            connections.payload_for(row.id),
            max_attempts=3,
        )
        for row in connections.active(db)
        if not row.paused
    ]


@router.post(
    "/rules",
    status_code=status.HTTP_201_CREATED,
    response_model=RuleOut,
    summary="Add a selection rule",
    description="Issues meeting all of its non-empty criteria become auditable. Queues the "
    "workspace's issue collection. Requires the `X-CSRF-Token` header.",
    responses={
        409: {"description": "An active rule has this name"},
        422: {"description": "No criteria, or a project outside this workspace"},
    },
)
def create_rule(
    body: RuleCreate,
    principal: Writer,
    db: Annotated[Session, Depends(database)],
) -> RuleOut:
    project_ids = sorted(set(body.project_ids))
    if not (body.labels or project_ids or body.issue_types):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "A rule needs at least one label, project or issue type",
        )
    known = set(db.scalars(select(GitLabProject.id).where(GitLabProject.id.in_(project_ids))))
    if known != set(project_ids):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown project in this workspace"
        )
    row = RegisterRule(
        workspace_id=current_workspace_id(db),
        name=body.name.strip(),
        category=body.category,
        labels=body.labels,
        project_ids=project_ids,
        issue_types=sorted(set(body.issue_types)),
        created_by=principal.actor,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "An active rule has this name") from exc
    job_ids = _queue_collection(db, f"rule-{row.id}")
    audit.record(
        db,
        actor=principal.actor,
        action="register.rule_created",
        resource_type="register_rule",
        resource_id=str(row.id),
        details={
            "name": row.name,
            "category": row.category,
            "labels": row.labels,
            "project_ids": row.project_ids,
            "issue_types": row.issue_types,
            "job_ids": job_ids,
        },
    )
    db.commit()
    db.refresh(row)
    return _rule_out(db, row)


@router.post(
    "/rules/{rule_id}/end",
    response_model=RuleOut,
    summary="End a selection rule",
    description="The issues it selected leave the register unless something else selects "
    "them. The rule stays on record. Requires the `X-CSRF-Token` header.",
    responses={404: {"description": "No such active rule"}},
)
def end_rule(
    rule_id: Annotated[int, Path(ge=1)],
    principal: Writer,
    db: Annotated[Session, Depends(database)],
) -> RuleOut:
    row = db.get(RegisterRule, rule_id)
    if row is None or row.ended_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such active rule")
    row.ended_by = principal.actor
    row.ended_at = datetime.now(UTC)
    audit.record(
        db,
        actor=principal.actor,
        action="register.rule_ended",
        resource_type="register_rule",
        resource_id=str(row.id),
        details={"name": row.name},
    )
    db.commit()
    return _rule_out(db, row)


class DecisionCreate(BaseModel):
    project_id: int = Field(ge=1)
    iid: int = Field(ge=1)
    decision: Literal["include", "exclude"]
    category: Category | None = None
    reason: str = Field(min_length=MIN_REGISTER_REASON, max_length=MAX_REGISTER_REASON)

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        if len(value.strip()) < MIN_REGISTER_REASON:
            raise ValueError(f"Give a reason of at least {MIN_REGISTER_REASON} characters")
        return value.strip()


@router.post(
    "/decisions",
    status_code=status.HTTP_201_CREATED,
    response_model=DecisionOut,
    summary="Include or exclude one issue",
    description="Inclusion makes an issue auditable whatever the rules say; exclusion takes a "
    "rule-selected issue out, and it stays listed as excluded with the reason. One open "
    "decision per issue: end it to make another. Requires the `X-CSRF-Token` header.",
    responses={
        404: {"description": "No such project in this workspace"},
        409: {"description": "The issue already has an open decision"},
        422: {"description": "A category on an exclusion"},
    },
)
def create_decision(
    body: DecisionCreate,
    principal: Writer,
    db: Annotated[Session, Depends(database)],
) -> DecisionOut:
    if body.decision == "exclude" and body.category is not None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Only an inclusion names a category"
        )
    _project(db, body.project_id)
    row = RegisterDecision(
        workspace_id=current_workspace_id(db),
        project_id=body.project_id,
        issue_iid=body.iid,
        decision=body.decision,
        category=body.category,
        reason=body.reason,
        decided_by=principal.actor,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "The issue already has an open decision; end it first"
        ) from exc
    details: dict[str, Any] = {
        "project_id": row.project_id,
        "iid": row.issue_iid,
        "category": row.category,
        "reason": row.reason,
    }
    if body.decision == "include" and db.get(RegisterIssue, (body.project_id, body.iid)) is None:
        details["job_ids"] = _queue_collection(db, f"decision-{row.id}")
    audit.record(
        db,
        actor=principal.actor,
        action=f"register.issue_{body.decision}d",
        resource_type="register_decision",
        resource_id=str(row.id),
        details=details,
    )
    db.commit()
    db.refresh(row)
    return _decision_out(row)


@router.post(
    "/decisions/{decision_id}/end",
    response_model=DecisionOut,
    summary="End a decision on an issue",
    description="The issue goes back to what the rules say. The decision stays on record. "
    "Requires the `X-CSRF-Token` header.",
    responses={404: {"description": "No such open decision"}},
)
def end_decision(
    decision_id: Annotated[int, Path(ge=1)],
    principal: Writer,
    db: Annotated[Session, Depends(database)],
) -> DecisionOut:
    row = db.get(RegisterDecision, decision_id)
    if row is None or row.ended_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such open decision")
    row.ended_by = principal.actor
    row.ended_at = datetime.now(UTC)
    audit.record(
        db,
        actor=principal.actor,
        action="register.decision_ended",
        resource_type="register_decision",
        resource_id=str(row.id),
        details={"project_id": row.project_id, "iid": row.issue_iid, "decision": row.decision},
    )
    db.commit()
    return _decision_out(row)


class CollectionQueued(BaseModel):
    job_ids: list[int]


@router.post(
    "/collect",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CollectionQueued,
    summary="Read the workspace's issues now",
    description="Queues the issue collection for each of the workspace's connections. Requests "
    "within the same minute share the jobs. Requires the `X-CSRF-Token` header.",
)
def collect_issues(
    principal: Writer,
    db: Annotated[Session, Depends(database)],
) -> CollectionQueued:
    job_ids = _queue_collection(db, "requested")
    audit.record(
        db,
        actor=principal.actor,
        action="register.collect_requested",
        resource_type="register",
        resource_id=str(current_workspace_id(db)),
        details={"job_ids": job_ids},
    )
    db.commit()
    return CollectionQueued(job_ids=job_ids)


class EvidenceOut(BaseModel):
    project_id: int
    iid: int
    # False until the issue's evidence is first captured.
    captured: bool
    status: Literal["complete", "partial", "failed"] | None = None
    # Sources that could not be read, each with why.
    gaps: list[str] = []
    captured_at: datetime | None = None
    # GitLab's last change to the issue that the capture reflects.
    captured_for: datetime | None = None
    observation_id: int | None = None
    payload_digest: str | None = None
    # Whether the stored digest matches the stored capture.
    digest_verified: bool | None = None
    # The capture: issue, notes, history, links, merge_requests, releases, attachments, sources.
    capture: dict[str, Any] | None = None


@router.get(
    "/issues/{project_id}/{iid}/evidence",
    response_model=EvidenceOut,
    summary="The evidence captured for one issue",
    description="The latest capture: the issue and its description, comments, status history, "
    "linked issues, merge requests with approvals and pipelines, releases and attachments, "
    "with the sources that could not be read.",
    responses={404: {"description": "No such project in this workspace"}},
)
def issue_evidence(
    project_id: Annotated[int, Path(ge=1)],
    iid: Annotated[int, Path(ge=1)],
    _: Reader,
    db: Annotated[Session, Depends(database)],
) -> EvidenceOut:
    _project(db, project_id)
    row = db.get(IssueEvidence, (project_id, iid))
    if row is None:
        return EvidenceOut(project_id=project_id, iid=iid, captured=False)
    observation = db.get_one(Observation, row.observation_id)
    return EvidenceOut(
        project_id=project_id,
        iid=iid,
        captured=True,
        status=row.status,  # type: ignore[arg-type]
        gaps=list(row.gaps),
        captured_at=row.captured_at,
        captured_for=row.captured_for,
        observation_id=observation.id,
        payload_digest=observation.payload_digest,
        digest_verified=verify(observation),
        capture=observation.payload,
    )


SHA256 = Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")]
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@router.get(
    "/files/{sha256}",
    summary="Download a captured attachment",
    description="Served as a download, never rendered. Only files cited by an issue capture in "
    "this workspace are served.",
    response_class=Response,
    responses={
        200: {"content": {"application/octet-stream": {}}},
        404: {"description": "No such file in this workspace"},
    },
)
def captured_file(
    sha256: SHA256,
    _: Reader,
    db: Annotated[Session, Depends(database)],
) -> Response:
    cited = db.scalars(
        select(Observation)
        .where(
            Observation.source == EVIDENCE_SOURCE,
            Observation.payload.contains({"attachments": [{"sha256": sha256}]}),
        )
        .order_by(Observation.id.desc())
        .limit(1)
    ).first()
    stored = db.get(CapturedFile, sha256) if cited is not None else None
    if cited is None or stored is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such file")
    name = next(
        (
            str(a.get("filename") or "")
            for a in (cited.payload or {}).get("attachments") or []
            if isinstance(a, dict) and a.get("sha256") == sha256
        ),
        "",
    )
    safe = SAFE_NAME.sub("_", name).strip("._")[:120] or sha256
    return Response(
        content=stored.content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{safe}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
            "Digest": f"sha-256={sha256}",
        },
    )
