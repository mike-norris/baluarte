"""Stored evaluation results, findings and evaluation freshness (BAL-021).

Reading needs ``findings:read``. Queueing an evaluation needs ``policy:write`` and is audited.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import ColumnElement, Select, and_, func, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.controls.posture import Effective, effective
from baluarte.db.models import (
    Evaluation,
    EvaluationRun,
    Finding,
    GitLabInstance,
    GitLabProject,
    Policy,
    PolicyException,
)
from baluarte.evidence.as_of import FindingAt, RunRef, findings_at, run_at, run_ref
from baluarte.integrations.gitlab.settings import GitLabSettings, load_gitlab_settings
from baluarte.jobs import queue
from baluarte.policy.evaluation import freshness
from baluarte.policy.evaluation.freshness import EvaluationFreshness, EvaluationRunOut
from baluarte.policy.evaluation.runner import EVALUATE, MAX_ATTEMPTS
from baluarte.policy.exceptions import in_force
from baluarte.scope.exclusions import coverage
from baluarte.settings import ConfigurationError

router = APIRouter(tags=["evaluations"])

AsOf = Annotated[
    AwareDatetime | None,
    Query(
        description="Read as it stood at this moment (ISO 8601 with a time zone): the latest "
        "evaluation run that finished by then, and nothing recorded later."
    ),
]

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
    # The exception that waived this failure; set exactly when ``outcome`` is ``excepted``.
    exception_id: int | None
    evaluated_at: datetime
    # A change rule's result rests on many observations: every one, and their combined digest.
    observation_ids: list[int] | None = None
    evidence_digest: str | None = None


class PointInTime(BaseModel):
    # The moment asked for with ``as_of``; null for now.
    as_of: datetime | None = None
    # The evaluation run the response is read from: the latest successful one (that finished by
    # ``as_of``). Null when there is none, and then the results are empty.
    evaluation_run: RunRef | None = None


class EvaluationPage(PointInTime):
    items: list[EvaluationOut]
    next_before_id: int | None


class ProjectEvaluations(PointInTime):
    project: ProjectRef
    # The latest evaluation of each rule in the project's latest evaluation; empty when the
    # project has not been evaluated.
    items: list[EvaluationOut]


class FindingException(BaseModel):
    id: int
    expires_at: datetime
    in_force: bool


class ControlRef(BaseModel):
    framework: str
    id: str


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
    # While ``state`` is ``excepted``, the exception waiving the failure. A finding whose
    # exception expired or was revoked reads as ``open`` even before the next evaluation.
    exception: FindingException | None
    latest_evaluation: EvaluationOut
    # The framework controls the rule's type supports in the effective control mapping
    # (docs/CONTROL_MAPPING.md).
    controls: list[ControlRef] = []


class FindingPage(PointInTime):
    items: list[FindingOut]
    next_after_id: int | None


class FindingRead(FindingOut):
    as_of: datetime | None = None
    evaluation_run: RunRef | None = None


FindingRows = Select[Evaluation, int, int, str, str, Finding, PolicyException, bool]


class EvaluationQueued(BaseModel):
    job_id: int


class RunPage(PointInTime):
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
        exception_id=evaluation.exception_id,
        evaluated_at=evaluation.evaluated_at,
        observation_ids=evaluation.observation_ids,
        evidence_digest=evaluation.evidence_digest,
    )


def _finding_out(row: Any, mapping: Effective) -> FindingOut:
    finding: Finding = row[5]
    exception: PolicyException | None = row[6]
    waived: bool = bool(row[7])
    evaluation = _evaluation_out(row)
    state = finding.state
    if state == "excepted" and not waived:
        state = "open"
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
        state=state,  # type: ignore[arg-type]
        resolution=finding.resolution,
        latest_outcome=finding.latest_outcome,  # type: ignore[arg-type]
        first_seen_at=finding.first_seen_at,
        last_seen_at=finding.last_seen_at,
        resolved_at=finding.resolved_at,
        exception=(
            FindingException(id=exception.id, expires_at=exception.expires_at, in_force=waived)
            if exception is not None
            else None
        ),
        latest_evaluation=evaluation,
        controls=[
            ControlRef(framework=framework, id=control_id)
            for framework, control_id in mapping.controls_for(evaluation.rule_type)
        ],
    )


def _waived() -> ColumnElement[bool]:
    """SQL: the finding's exception is in force."""
    return and_(PolicyException.id.is_not(None), in_force())


def _state_is(state: str) -> ColumnElement[bool]:
    """SQL: the finding's state as reported, with ended exceptions counted as open."""
    if state == "excepted":
        return and_(Finding.state == "excepted", _waived())
    if state == "open":
        return or_(Finding.state == "open", and_(Finding.state == "excepted", ~_waived()))
    return Finding.state == state


def _findings() -> FindingRows:
    return (
        select(
            Evaluation,
            Policy.version,
            GitLabProject.gitlab_id,
            GitLabProject.path_with_namespace,
            GitLabProject.state,
            Finding,
            PolicyException,
            func.coalesce(_waived(), False),
        )
        .select_from(Finding)
        .join(Evaluation, Evaluation.id == Finding.latest_evaluation_id)
        .join(Policy, Policy.id == Evaluation.policy_id)
        .join(GitLabProject, GitLabProject.id == Finding.project_id)
        .outerjoin(PolicyException, PolicyException.id == Finding.exception_id)
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


class OutcomeCounts(BaseModel):
    pass_: int = Field(default=0, alias="pass")
    fail: int = 0
    unknown: int = 0
    unsupported: int = 0
    excepted: int = 0

    # Responses serialise by alias, so the field reads ``pass`` like the outcome it counts.
    model_config = ConfigDict(populate_by_name=True)

    def add(self, outcome: str, count: int = 1) -> None:
        name = "pass_" if outcome == "pass" else outcome
        setattr(self, name, getattr(self, name) + count)

    def merge(self, other: "OutcomeCounts") -> None:
        for outcome in _PROJECT_ORDER:
            self.add(outcome, other.count(outcome))

    def count(self, outcome: str) -> int:
        return int(getattr(self, "pass_" if outcome == "pass" else outcome))


class RuleResults(BaseModel):
    rule_id: str
    rule_type: str
    severity: str
    # One result per evaluated project.
    results: OutcomeCounts


class VersionResults(BaseModel):
    policy_id: int
    version: int
    yaml_digest: str
    # Projects by their worst result for this version: fail, then unknown, then unsupported,
    # then excepted; pass only when every rule passed.
    projects: OutcomeCounts
    rules: list[RuleResults]


class BaselineResults(BaseModel):
    name: str
    projects: OutcomeCounts
    versions: list[VersionResults]


class EvaluationSummary(BaseModel):
    # The run the counts come from: the latest successful one (that finished by ``as_of``).
    # Null when none has succeeded.
    evaluation_run: EvaluationRunOut | None
    as_of: datetime | None = None
    freshness: EvaluationFreshness
    baselines: list[BaselineResults]


# Worst first: a project's result for a version is the first of these that any rule gave.
_PROJECT_ORDER = ("fail", "unknown", "unsupported", "excepted", "pass")


@router.get(
    "/evaluations/summary",
    response_model=EvaluationSummary,
    summary="Result counts per baseline, version and rule",
    description="From the latest successful evaluation run, which judges every project once per "
    "rule in force, so the counts are a consistent snapshot; results of a run that failed or is "
    "still running are not counted. Rule counts are results (one per project); `projects` "
    "counts each project once by its worst result. `unknown` and `unsupported` are never "
    "counted as passing. Read with `freshness`: old counts are not current compliance. Filter "
    "with `policy` (a baseline name). Projects out of monitoring scope are not counted, even "
    "when the run judged them before they were excluded.",
)
def evaluation_summary(
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
    policy: Annotated[str | None, Query(max_length=63)] = None,
    as_of: AsOf = None,
) -> EvaluationSummary:
    settings = _settings()
    instance_id = _instance_id(db, settings)
    report = freshness.report(db, instance_id, settings)
    if as_of is None:
        run = db.scalars(
            select(EvaluationRun)
            .where(
                EvaluationRun.instance_id == (instance_id or 0),
                EvaluationRun.status == "succeeded",
            )
            .order_by(EvaluationRun.started_at.desc(), EvaluationRun.id.desc())
            .limit(1)
        ).first()
    else:
        run = run_at(db, instance_id, as_of)
    if run is None:
        return EvaluationSummary(evaluation_run=None, freshness=report, baselines=[], as_of=as_of)
    statement = (
        select(
            Evaluation.policy_name,
            Evaluation.policy_id,
            Policy.version,
            Policy.yaml_digest,
            Evaluation.rule_id,
            Evaluation.rule_type,
            Evaluation.severity,
            Evaluation.project_id,
            Evaluation.outcome,
        )
        .join(Policy, Policy.id == Evaluation.policy_id)
        .join(GitLabProject, GitLabProject.id == Evaluation.project_id)
        .where(
            Evaluation.evaluation_run_id == run.id,
            coverage(db, run.instance_id, as_of).project_included(),
        )
    )
    if policy is not None:
        statement = statement.where(Evaluation.policy_name == policy)
    rules: dict[tuple[int, str], RuleResults] = {}
    versions: dict[int, tuple[str, int, str]] = {}
    worst: dict[tuple[int, int], str] = {}
    for (
        name,
        policy_id,
        version,
        digest,
        rule_id,
        rule_type,
        severity,
        project,
        outcome,
    ) in db.execute(statement):
        versions[policy_id] = (name, version, digest)
        entry = rules.setdefault(
            (policy_id, rule_id),
            RuleResults(
                rule_id=rule_id, rule_type=rule_type, severity=severity, results=OutcomeCounts()
            ),
        )
        entry.results.add(outcome)
        current = worst.get((policy_id, project))
        if current is None or _PROJECT_ORDER.index(outcome) < _PROJECT_ORDER.index(current):
            worst[(policy_id, project)] = outcome
    baselines: dict[str, BaselineResults] = {}
    for policy_id, (name, version, digest) in sorted(
        versions.items(), key=lambda item: (item[1][0], -item[1][1])
    ):
        projects = OutcomeCounts()
        for (counted_policy, _project), outcome in worst.items():
            if counted_policy == policy_id:
                projects.add(outcome)
        baseline = baselines.setdefault(
            name, BaselineResults(name=name, projects=OutcomeCounts(), versions=[])
        )
        # One run judges a project under one version of each baseline, so versions add up.
        baseline.projects.merge(projects)
        baseline.versions.append(
            VersionResults(
                policy_id=policy_id,
                version=version,
                yaml_digest=digest,
                projects=projects,
                rules=sorted(
                    (rule for (pid, _rule), rule in rules.items() if pid == policy_id),
                    key=lambda rule: rule.rule_id,
                ),
            )
        )
    return EvaluationSummary(
        evaluation_run=EvaluationRunOut.model_validate(run, from_attributes=True),
        freshness=report,
        baselines=list(baselines.values()),
        as_of=as_of,
    )


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
    as_of: AsOf = None,
) -> RunPage:
    instance_id = _instance_id(db, _settings())
    statement = select(EvaluationRun).where(EvaluationRun.instance_id == (instance_id or 0))
    if as_of is not None:
        # Runs that had finished by then; a run still going then is not shown as it ended.
        statement = statement.where(EvaluationRun.finished_at <= as_of)
    if before_id is not None:
        statement = statement.where(EvaluationRun.id < before_id)
    rows = list(db.scalars(statement.order_by(EvaluationRun.id.desc()).limit(limit + 1)))
    items = [EvaluationRunOut.model_validate(row, from_attributes=True) for row in rows[:limit]]
    return RunPage(
        items=items,
        next_before_id=items[-1].id if len(rows) > limit else None,
        as_of=as_of,
        evaluation_run=run_ref(run_at(db, instance_id, as_of)),
    )


@router.get(
    "/findings",
    response_model=FindingPage,
    summary="Findings: rules a project failed, followed until they pass",
    description="A finding opens when a rule fails and resolves when it passes "
    "(`resolution = passed`), no longer applies (`no_longer_applies`) or its project is archived "
    "(`project_archived`: archived projects are not evaluated) or is taken out of monitoring "
    "scope (`project_excluded`). An `unknown` or "
    "`unsupported` result never opens or resolves one; `latest_outcome` shows it. A failure "
    "waived by an approved exception is `excepted` until the exception expires or is revoked, "
    "then `open` again. Filter by "
    "`state`, `outcome` (the latest outcome), `severity`, `project` (GitLab ID), `policy` "
    "(name) and `rule` (rule ID). `monitoring`: `included` (the default) leaves out findings "
    "of projects out of monitoring scope, `excluded` lists only those, `all` both. Ordered by "
    "ID; follow `next_after_id`.",
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
    monitoring: Annotated[Literal["included", "excluded", "all"], Query()] = "included",
    after_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    as_of: AsOf = None,
) -> FindingPage:
    if as_of is not None:
        return _findings_as_of(
            db,
            as_of,
            FindingFilters(state, outcome, severity, project, policy, rule, monitoring),
            after_id,
            limit,
        )
    statement = _scoped(_findings(), db)
    if monitoring != "all":
        scope = coverage(db, _instance_id(db, _settings()))
        statement = statement.where(
            scope.project_included() if monitoring == "included" else scope.project_excluded()
        )
    if state is not None:
        statement = statement.where(_state_is(state))
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
    mapping = effective(db)
    items = [_finding_out(row, mapping) for row in rows[:limit]]
    instance_id = _instance_id(db, _settings())
    return FindingPage(
        items=items,
        next_after_id=items[-1].id if len(rows) > limit else None,
        evaluation_run=run_ref(run_at(db, instance_id, None)),
    )


@dataclass(frozen=True)
class FindingFilters:
    state: str | None = None
    outcome: str | None = None
    severity: str | None = None
    project: int | None = None
    policy: str | None = None
    rule: str | None = None
    monitoring: str = "all"


def _finding_at_out(
    finding: Finding, project: GitLabProject, at: FindingAt, version: int, mapping: Effective
) -> FindingOut:
    evaluation = _evaluation_out(
        (at.latest, version, project.gitlab_id, project.path_with_namespace, project.state)
    )
    exception = at.exception
    resolved = finding.resolved_at
    return FindingOut(
        id=finding.id,
        project=evaluation.project,
        policy=PolicyVersionRef(
            policy_id=at.latest.policy_id, name=finding.policy_name, version=version
        ),
        rule_id=finding.rule_id,
        severity=finding.severity,
        state=at.state,  # type: ignore[arg-type]
        resolution=at.resolution,
        latest_outcome=at.latest.outcome,  # type: ignore[arg-type]
        first_seen_at=finding.first_seen_at,
        last_seen_at=min(finding.last_seen_at, at.latest.evaluated_at),
        # When it was resolved is known only if that resolution still stands.
        resolved_at=resolved
        if at.state == "resolved" and resolved is not None and resolved <= at.latest.evaluated_at
        else None,
        exception=(
            FindingException(id=exception.id, expires_at=exception.expires_at, in_force=at.waived)
            if exception is not None
            else None
        ),
        latest_evaluation=evaluation,
        controls=[
            ControlRef(framework=framework, id=control_id)
            for framework, control_id in mapping.controls_for(evaluation.rule_type)
        ],
    )


_AS_OF_CHUNK = 200


def _findings_as_of(
    db: Session,
    as_of: datetime,
    filters: FindingFilters,
    after_id: int | None,
    limit: int,
    finding_id: int | None = None,
) -> FindingPage:
    """Findings as they stood at ``as_of`` (``baluarte.evidence.as_of``), filtered on that state."""
    instance_id = _instance_id(db, _settings())
    run = run_at(db, instance_id, as_of)
    if run is None:
        return FindingPage(items=[], next_after_id=None, as_of=as_of, evaluation_run=None)
    scope = coverage(db, instance_id, as_of)
    mapping = effective(db, as_of)
    statement = (
        select(Finding, GitLabProject)
        .join(GitLabProject, GitLabProject.id == Finding.project_id)
        .where(GitLabProject.instance_id == (instance_id or 0), Finding.first_seen_at <= as_of)
    )
    if finding_id is not None:
        statement = statement.where(Finding.id == finding_id)
    if filters.monitoring != "all":
        statement = statement.where(
            scope.project_included()
            if filters.monitoring == "included"
            else scope.project_excluded()
        )
    if filters.severity is not None:
        statement = statement.where(Finding.severity == filters.severity)
    if filters.project is not None:
        statement = statement.where(GitLabProject.gitlab_id == filters.project)
    if filters.policy is not None:
        statement = statement.where(Finding.policy_name == filters.policy)
    if filters.rule is not None:
        statement = statement.where(Finding.rule_id == filters.rule)
    items: list[FindingOut] = []
    more = False
    cursor = after_id or 0
    while not more:
        chunk = list(
            db.execute(
                statement.where(Finding.id > cursor).order_by(Finding.id).limit(_AS_OF_CHUNK)
            ).all()
        )
        if not chunk:
            break
        cursor = chunk[-1][0].id
        states = findings_at(db, chunk, as_of, run, scope)
        versions = dict(
            db.execute(
                select(Policy.id, Policy.version).where(
                    Policy.id.in_({at.latest.policy_id for at in states.values()})
                )
            ).all()
        )
        for finding, project in chunk:
            at = states.get(finding.id)
            if at is None:
                continue
            if filters.state is not None and at.state != filters.state:
                continue
            if filters.outcome is not None and at.latest.outcome != filters.outcome:
                continue
            if len(items) == limit:
                more = True
                break
            items.append(
                _finding_at_out(finding, project, at, versions[at.latest.policy_id], mapping)
            )
        if len(chunk) < _AS_OF_CHUNK:
            break
    return FindingPage(
        items=items,
        next_after_id=items[-1].id if more and items else None,
        as_of=as_of,
        evaluation_run=run_ref(run),
    )


def _finding(db: Session, finding_id: int) -> Any:
    row = db.execute(_scoped(_findings(), db).where(Finding.id == finding_id)).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such finding")
    return row


@router.get(
    "/findings/{finding_id}",
    response_model=FindingRead,
    summary="One finding with its latest evaluation",
    responses={404: {"description": "No such finding"}},
)
def read_finding(
    finding_id: Annotated[int, Path(gt=0)],
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
    as_of: AsOf = None,
) -> FindingRead:
    if as_of is None:
        out = _finding_out(_finding(db, finding_id), effective(db))
        run = run_at(db, _instance_id(db, _settings()), None)
        return FindingRead(**dict(out), evaluation_run=run_ref(run))
    page = _findings_as_of(db, as_of, FindingFilters(), None, 1, finding_id=finding_id)
    if not page.items:
        if page.evaluation_run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No evaluation run had finished by then")
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such finding at that moment")
    return FindingRead(**dict(page.items[0]), as_of=as_of, evaluation_run=page.evaluation_run)


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
    as_of: AsOf = None,
) -> EvaluationPage:
    finding: Finding = _finding(db, finding_id)[5]
    statement = _evaluations().where(
        Evaluation.project_id == finding.project_id,
        Evaluation.policy_name == finding.policy_name,
        Evaluation.rule_id == finding.rule_id,
    )
    if as_of is not None:
        statement = statement.where(Evaluation.evaluated_at <= as_of)
    if before_id is not None:
        statement = statement.where(Evaluation.id < before_id)
    rows = db.execute(statement.order_by(Evaluation.id.desc()).limit(limit + 1)).all()
    items = [_evaluation_out(row) for row in rows[:limit]]
    return EvaluationPage(
        items=items,
        next_before_id=items[-1].id if len(rows) > limit else None,
        as_of=as_of,
        evaluation_run=run_ref(run_at(db, _instance_id(db, _settings()), as_of)),
    )


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
    as_of: AsOf = None,
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
    if as_of is not None:
        # The project's results in the run in force then.
        run = run_at(db, instance_id, as_of)
        if run is None:
            return ProjectEvaluations(project=ref, items=[], as_of=as_of, evaluation_run=None)
        rows = db.execute(
            _evaluations()
            .where(Evaluation.project_id == project.id, Evaluation.evaluation_run_id == run.id)
            .ext(distinct_on(Evaluation.policy_name, Evaluation.rule_id))
            .order_by(Evaluation.policy_name, Evaluation.rule_id, Evaluation.id.desc())
        ).all()
        return ProjectEvaluations(
            project=ref,
            items=[_evaluation_out(row) for row in rows],
            as_of=as_of,
            evaluation_run=run_ref(run),
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
    current = run_ref(run_at(db, instance_id, None))
    if newest is None or (latest_success is not None and newest < latest_success):
        return ProjectEvaluations(project=ref, items=[], evaluation_run=current)
    rows = db.execute(
        _evaluations()
        .where(Evaluation.project_id == project.id, Evaluation.evaluated_at == newest)
        .ext(distinct_on(Evaluation.policy_name, Evaluation.rule_id))
        .order_by(Evaluation.policy_name, Evaluation.rule_id, Evaluation.id.desc())
    ).all()
    return ProjectEvaluations(
        project=ref, items=[_evaluation_out(row) for row in rows], evaluation_run=current
    )
