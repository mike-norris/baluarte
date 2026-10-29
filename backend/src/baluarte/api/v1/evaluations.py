"""Stored evaluation results, findings and evaluation freshness (BAL-021).

Reading needs ``findings:read``. Queueing an evaluation needs ``policy:write`` and is audited.
"""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.db.models import (
    Evaluation,
    EvaluationRun,
    Finding,
    GitLabInstance,
    GitLabProject,
    Policy,
)
from baluarte.integrations.gitlab.settings import GitLabSettings, load_gitlab_settings
from baluarte.jobs import queue
from baluarte.policy.evaluation import freshness
from baluarte.policy.evaluation.freshness import EvaluationFreshness, EvaluationRunOut
from baluarte.policy.evaluation.runner import EVALUATE, MAX_ATTEMPTS
from baluarte.settings import ConfigurationError

router = APIRouter(tags=["evaluations"])

Outcome = Literal["pass", "fail", "unknown", "unsupported", "excepted"]
FindingState = Literal["open", "resolved", "excepted"]
Severity = Literal["low", "medium", "high", "critical"]


class ProjectRef(BaseModel):
    gitlab_id: int
    path: str
    state: str


class PolicyVersionRef(BaseModel):
    policy_id: int
    name: str
    version: int


class CapabilityOut(BaseModel):
    name: str | None
    status: str


class CollectionOut(BaseModel):
    kind: str
    status: str
    last_success_at: datetime | None


class EvaluationOut(BaseModel):
    id: int
    evaluation_run_id: int | None
    observation_id: int | None
    project: ProjectRef
    policy: PolicyVersionRef
    rule_id: str
    rule_type: str
    severity: str
    outcome: Outcome
    reason: str
    unknown_reasons: list[str]
    expected: Any
    observed: Any
    source: str
    observed_at: datetime | None
    capability: CapabilityOut
    collection: CollectionOut
    ancestry: str
    evaluated_at: datetime


class EvaluationPage(BaseModel):
    items: list[EvaluationOut]
    next_before_id: int | None


class ProjectEvaluations(BaseModel):
    project: ProjectRef
    # The latest evaluation of each rule in the project's latest evaluation; empty when the
    # project has not been evaluated.
    items: list[EvaluationOut]


class FindingOut(BaseModel):
    id: int
    project: ProjectRef
    policy: PolicyVersionRef
    rule_id: str
    severity: str
    state: FindingState
    resolution: str | None
    latest_outcome: Outcome
    first_seen_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None
    latest_evaluation: EvaluationOut


class FindingPage(BaseModel):
    items: list[FindingOut]
    next_after_id: int | None


FindingRows = Select[Evaluation, int, int, str, str, Finding]


class EvaluationQueued(BaseModel):
    job_id: int


class RunPage(BaseModel):
    items: list[EvaluationRunOut]
    next_before_id: int | None


def _settings() -> GitLabSettings | None:
    try:
        return load_gitlab_settings()
    except ConfigurationError:
        return None


def _instance_id(db: Session, settings: GitLabSettings | None) -> int | None:
    if settings is None:
        return None
    return db.scalars(
        select(GitLabInstance.id).where(GitLabInstance.base_url == settings.url)
    ).first()


def _evaluations() -> Select[Evaluation, int, int, str, str]:
    return (
        select(
            Evaluation,
            Policy.version,
            GitLabProject.gitlab_id,
            GitLabProject.path_with_namespace,
            GitLabProject.state,
        )
        .join(Policy, Policy.id == Evaluation.policy_id)
        .join(GitLabProject, GitLabProject.id == Evaluation.project_id)
    )


def _evaluation_out(row: Any) -> EvaluationOut:
    evaluation: Evaluation = row[0]
    return EvaluationOut(
        id=evaluation.id,
        evaluation_run_id=evaluation.evaluation_run_id,
        observation_id=evaluation.observation_id,
        project=ProjectRef(gitlab_id=row[2], path=row[3], state=row[4]),
        policy=PolicyVersionRef(
            policy_id=evaluation.policy_id, name=evaluation.policy_name, version=row[1]
        ),
        rule_id=evaluation.rule_id,
        rule_type=evaluation.rule_type,
        severity=evaluation.severity,
        outcome=evaluation.outcome,  # type: ignore[arg-type]
        reason=evaluation.reason,
        unknown_reasons=list(evaluation.unknown_reasons),
        expected=evaluation.expected,
        observed=evaluation.observed,
        source=evaluation.source,
        observed_at=evaluation.observed_at,
        capability=CapabilityOut(
            name=evaluation.capability_name, status=evaluation.capability_status
        ),
        collection=CollectionOut(
            kind=evaluation.collection_kind,
            status=evaluation.collection_status,
            last_success_at=evaluation.collection_last_success_at,
        ),
        ancestry=evaluation.ancestry,
        evaluated_at=evaluation.evaluated_at,
    )


def _finding_out(finding: Finding, evaluation: EvaluationOut) -> FindingOut:
    return FindingOut(
        id=finding.id,
        project=evaluation.project,
        policy=PolicyVersionRef(
            policy_id=finding.policy_id,
            name=finding.policy_name,
            version=evaluation.policy.version,
        ),
        rule_id=finding.rule_id,
        severity=finding.severity,
        state=finding.state,  # type: ignore[arg-type]
        resolution=finding.resolution,
        latest_outcome=finding.latest_outcome,  # type: ignore[arg-type]
        first_seen_at=finding.first_seen_at,
        last_seen_at=finding.last_seen_at,
        resolved_at=finding.resolved_at,
        latest_evaluation=evaluation,
    )


def _findings() -> FindingRows:
    return (
        select(
            Evaluation,
            Policy.version,
            GitLabProject.gitlab_id,
            GitLabProject.path_with_namespace,
            GitLabProject.state,
            Finding,
        )
        .select_from(Finding)
        .join(Evaluation, Evaluation.id == Finding.latest_evaluation_id)
        .join(Policy, Policy.id == Evaluation.policy_id)
        .join(GitLabProject, GitLabProject.id == Finding.project_id)
    )


def _scoped(statement: FindingRows, db: Session) -> FindingRows:
    """Limit to the configured instance; nothing when GitLab is not configured."""
    instance_id = _instance_id(db, _settings())
    return statement.where(GitLabProject.instance_id == (instance_id or 0))


@router.post(
    "/evaluations/run",
    response_model=EvaluationQueued,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue an evaluation of every project",
    description="Runs in the worker. Requests within the same minute share one job. Evaluations "
    "are also queued after each project discovery and after a policy assignment changes. "
    "Requires the `X-CSRF-Token` header.",
    responses={409: {"description": "GitLab is not configured"}},
)
def run_evaluation(
    principal: Annotated[Principal, Depends(require(Permission.POLICY_WRITE))],
    db: Annotated[Session, Depends(database)],
) -> EvaluationQueued:
    settings = _settings()
    if settings is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "GitLab is not configured")
    minute = datetime.now(UTC).strftime("%Y%m%dT%H%M")
    job_id = queue.enqueue(db, EVALUATE, f"{EVALUATE}:{minute}", max_attempts=MAX_ATTEMPTS)
    audit.record(
        db,
        actor=principal.actor,
        action="policy.evaluation_requested",
        resource_type="gitlab_instance",
        resource_id=settings.url,
        details={"job_id": job_id},
    )
    db.commit()
    return EvaluationQueued(job_id=job_id)


@router.get(
    "/evaluations/freshness",
    response_model=EvaluationFreshness,
    summary="Whether stored evaluation results are current",
    description="`status` is `failed` when the latest finished evaluation run failed (whatever "
    "succeeded before), `never`, `stale` when the latest successful run started longer ago than "
    "`stale_after_minutes` (the collection threshold), `fresh`, or `not_configured`. "
    "`behind_collection` is true when a project discovery finished after the latest successful "
    "evaluation started.",
)
def read_freshness(
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
) -> EvaluationFreshness:
    settings = _settings()
    return freshness.report(db, _instance_id(db, settings), settings)


@router.get(
    "/evaluations/runs",
    response_model=RunPage,
    summary="Evaluation run history, newest first",
)
def list_runs(
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
    before_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> RunPage:
    instance_id = _instance_id(db, _settings())
    statement = select(EvaluationRun).where(EvaluationRun.instance_id == (instance_id or 0))
    if before_id is not None:
        statement = statement.where(EvaluationRun.id < before_id)
    rows = list(db.scalars(statement.order_by(EvaluationRun.id.desc()).limit(limit + 1)))
    items = [EvaluationRunOut.model_validate(row, from_attributes=True) for row in rows[:limit]]
    return RunPage(items=items, next_before_id=items[-1].id if len(rows) > limit else None)


@router.get(
    "/findings",
    response_model=FindingPage,
    summary="Findings: rules a project failed, followed until they pass",
    description="A finding opens when a rule fails and resolves when it passes "
    "(`resolution = passed`) or no longer applies (`no_longer_applies`). An `unknown` or "
    "`unsupported` result never opens or resolves one; `latest_outcome` shows it. Filter by "
    "`state`, `outcome` (the latest outcome), `severity`, `project` (GitLab ID), `policy` "
    "(name) and `rule` (rule ID). Ordered by ID; follow `next_after_id`.",
)
def list_findings(
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
    state: FindingState | None = None,
    outcome: Outcome | None = None,
    severity: Severity | None = None,
    project: Annotated[int | None, Query(gt=0)] = None,
    policy: Annotated[str | None, Query(max_length=63)] = None,
    rule: Annotated[str | None, Query(max_length=63)] = None,
    after_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> FindingPage:
    statement = _scoped(_findings(), db)
    if state is not None:
        statement = statement.where(Finding.state == state)
    if outcome is not None:
        statement = statement.where(Finding.latest_outcome == outcome)
    if severity is not None:
        statement = statement.where(Finding.severity == severity)
    if project is not None:
        statement = statement.where(GitLabProject.gitlab_id == project)
    if policy is not None:
        statement = statement.where(Finding.policy_name == policy)
    if rule is not None:
        statement = statement.where(Finding.rule_id == rule)
    if after_id is not None:
        statement = statement.where(Finding.id > after_id)
    rows = db.execute(statement.order_by(Finding.id).limit(limit + 1)).all()
    items = [_finding_out(row[5], _evaluation_out(row)) for row in rows[:limit]]
    return FindingPage(items=items, next_after_id=items[-1].id if len(rows) > limit else None)


def _finding(db: Session, finding_id: int) -> Any:
    row = db.execute(_scoped(_findings(), db).where(Finding.id == finding_id)).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such finding")
    return row


@router.get(
    "/findings/{finding_id}",
    response_model=FindingOut,
    summary="One finding with its latest evaluation",
    responses={404: {"description": "No such finding"}},
)
def read_finding(
    finding_id: Annotated[int, Path(gt=0)],
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
) -> FindingOut:
    row = _finding(db, finding_id)
    return _finding_out(row[5], _evaluation_out(row))


@router.get(
    "/findings/{finding_id}/evaluations",
    response_model=EvaluationPage,
    summary="Every evaluation of a finding's rule for its project, newest first",
    description="Across policy versions, including `unknown` results. Follow `next_before_id`.",
    responses={404: {"description": "No such finding"}},
)
def finding_history(
    finding_id: Annotated[int, Path(gt=0)],
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
    before_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> EvaluationPage:
    finding: Finding = _finding(db, finding_id)[5]
    statement = _evaluations().where(
        Evaluation.project_id == finding.project_id,
        Evaluation.policy_name == finding.policy_name,
        Evaluation.rule_id == finding.rule_id,
    )
    if before_id is not None:
        statement = statement.where(Evaluation.id < before_id)
    rows = db.execute(statement.order_by(Evaluation.id.desc()).limit(limit + 1)).all()
    items = [_evaluation_out(row) for row in rows[:limit]]
    return EvaluationPage(items=items, next_before_id=items[-1].id if len(rows) > limit else None)


@router.get(
    "/gitlab/projects/{gitlab_id}/evaluations",
    response_model=ProjectEvaluations,
    summary="The latest evaluation of each rule in force for a project",
    description="Rules from the project's most recent evaluation only, and nothing when the "
    "latest successful evaluation run gave the project no result (no rule in force, or the "
    "project is out of scope), so a rule that no longer applies is not shown as current. Read "
    "`outcome` with the evaluation freshness: an old `pass` is not current compliance.",
    responses={404: {"description": "No such project in the inventory"}},
)
def project_evaluations(
    gitlab_id: Annotated[int, Path(gt=0)],
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
) -> ProjectEvaluations:
    instance_id = _instance_id(db, _settings())
    project = db.scalars(
        select(GitLabProject).where(
            GitLabProject.instance_id == (instance_id or 0), GitLabProject.gitlab_id == gitlab_id
        )
    ).first()
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such project in the inventory")
    ref = ProjectRef(
        gitlab_id=project.gitlab_id, path=project.path_with_namespace, state=project.state
    )
    newest = db.execute(
        select(func.max(Evaluation.evaluated_at)).where(Evaluation.project_id == project.id)
    ).scalar_one()
    # A successful run evaluates every project in scope, so a project with no result in the
    # latest one had no rule in force (or was out of scope): its older results are not current.
    latest_success = db.execute(
        select(func.max(EvaluationRun.started_at)).where(
            EvaluationRun.instance_id == project.instance_id,
            EvaluationRun.status == "succeeded",
        )
    ).scalar_one()
    if newest is None or (latest_success is not None and newest < latest_success):
        return ProjectEvaluations(project=ref, items=[])
    rows = db.execute(
        _evaluations()
        .where(Evaluation.project_id == project.id, Evaluation.evaluated_at == newest)
        .ext(distinct_on(Evaluation.policy_name, Evaluation.rule_id))
        .order_by(Evaluation.policy_name, Evaluation.rule_id, Evaluation.id.desc())
    ).all()
    return ProjectEvaluations(project=ref, items=[_evaluation_out(row) for row in rows])
