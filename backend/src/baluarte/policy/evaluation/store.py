"""Storing evaluation results and following findings (BAL-021).

Every result becomes an immutable ``evaluation`` row that names the observation it judged.
A ``pass`` or ``fail`` must be backed by the project's latest observation of the value's
source, with a payload that matches the value judged; otherwise it is stored as ``unknown``
(``evidence_missing`` or ``evidence_mismatch``), so no stored compliance claim lacks evidence.

Findings follow a rule for a project by policy name, across versions:

- ``fail`` opens a finding, or reopens a resolved one.
- ``pass`` resolves an open finding (``resolution = passed``).
- ``unknown`` and ``unsupported`` change no state: they never resolve a finding and never open
  one. ``latest_outcome`` shows that the last check could not decide.
- A rule that no longer applies to the project resolves its open finding
  (``resolution = no_longer_applies``), but only when the project's policy resolution was
  complete: a gap in the group chain could be hiding the policy, so it proves nothing.
- ``excepted`` findings are left to the exception workflow (BAL-033); only their latest
  evaluation is updated.
"""

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte.db.models import Evaluation, Finding, GitLabProject, Observation
from baluarte.integrations.gitlab.branches import PROTECTION_SOURCE
from baluarte.integrations.gitlab.projects import PROJECT_SOURCES
from baluarte.policy.assignments import Resolution
from baluarte.policy.evaluation.model import RuleResult, UnknownReason

# Sources of the project's own fields (listing and lookup), from discovery.
INVENTORY_SOURCES: tuple[str, ...] = tuple(PROJECT_SOURCES)


def _whole(payload: dict[str, Any]) -> Any:
    return payload


def _visibility(payload: dict[str, Any]) -> Any:
    return payload.get("visibility")


@dataclass(frozen=True)
class Evidence:
    """Which observations can back a result, and how to read the judged value from one."""

    sources: tuple[str, ...]
    # The value in an observation's payload, compared with the judged value. None when it is
    # not known, so a pass or fail cannot be backed.
    value: Callable[[dict[str, Any]], Any] | None


# Evidence by result ``source``. A rule type whose value lives elsewhere either adds an entry
# here or sets ``RuleResult.observation_source`` (its value is then the whole payload).
EVIDENCE: Mapping[str, Evidence] = {
    "gitlab_project.visibility": Evidence(INVENTORY_SOURCES, _visibility),
    # The default-branch protection rule judges the protection observation's payload itself.
    PROTECTION_SOURCE: Evidence((PROTECTION_SOURCE,), _whole),
}


def evidence_for(result: RuleResult) -> Evidence:
    found = EVIDENCE.get(result.source)
    if found is not None:
        return found
    if result.observation_source is not None:
        return Evidence((result.observation_source,), _whole)
    # An unknown source: link the project's own observation, but nothing says where its value
    # is, so a pass or fail is stored as unknown.
    return Evidence(INVENTORY_SOURCES, None)


@dataclass
class Tally:
    """Counts for an evaluation run."""

    outcomes: Counter[str] = field(default_factory=Counter)
    findings: Counter[str] = field(default_factory=Counter)

    def counts(self) -> dict[str, Any]:
        return {"outcomes": dict(self.outcomes), "findings": dict(self.findings)}


def _latest_observation(
    db: Session, instance_id: int, project_id: int, sources: Sequence[str]
) -> Observation | None:
    return db.scalars(
        select(Observation)
        .where(
            Observation.instance_id == instance_id,
            Observation.project_id == project_id,
            Observation.source.in_(sources),
        )
        .order_by(Observation.collected_at.desc(), Observation.id.desc())
        .limit(1)
    ).first()


def _unbacked(result: RuleResult, reason: UnknownReason, text: str) -> RuleResult:
    return result.model_copy(
        update={
            "outcome": "unknown",
            "observed": None,
            "observed_at": None,
            "unknown_reasons": (reason,),
            "reason": text,
        }
    )


def backed(result: RuleResult, evidence: Evidence, observation: Observation | None) -> RuleResult:
    """The result, or ``unknown`` when a pass or fail is not supported by the observation."""
    if result.outcome not in ("pass", "fail"):
        return result
    path = result.target.path
    if observation is None or observation.collection_status != "ok" or observation.payload is None:
        return _unbacked(
            result,
            "evidence_missing",
            f"No stored observation of project {path} backs this value; it will be evaluated "
            "again after the next project discovery records one.",
        )
    if evidence.value is None or evidence.value(observation.payload) != result.observed:
        return _unbacked(
            result,
            "evidence_mismatch",
            f"The latest stored observation of project {path} does not match the inventory, "
            "so the value cannot be backed by evidence.",
        )
    return result


def _row(
    instance_id: int,
    project: GitLabProject,
    result: RuleResult,
    observation: Observation | None,
    run_id: int | None,
) -> Evaluation:
    return Evaluation(
        instance_id=instance_id,
        project_id=project.id,
        evaluation_run_id=run_id,
        observation_id=observation.id if observation is not None else None,
        policy_id=result.policy.policy_id,
        policy_name=result.policy.name,
        rule_id=result.rule_id,
        rule_type=result.rule_type,
        severity=result.severity,
        outcome=result.outcome,
        reason=result.reason,
        unknown_reasons=list(result.unknown_reasons),
        expected=result.expected,
        observed=result.observed,
        source=result.source,
        observed_at=result.observed_at,
        capability_name=result.capability.name,
        capability_status=result.capability.status,
        collection_kind=result.collection.kind,
        collection_status=result.collection.status,
        collection_last_success_at=result.collection.last_success_at,
        ancestry=result.ancestry,
        evaluated_at=result.evaluated_at,
    )


def _follow(db: Session, project: GitLabProject, row: Evaluation, tally: Tally) -> None:
    finding = db.scalars(
        select(Finding)
        .where(
            Finding.project_id == project.id,
            Finding.policy_name == row.policy_name,
            Finding.rule_id == row.rule_id,
        )
        .with_for_update()
    ).first()
    if finding is None:
        if row.outcome == "fail":
            db.add(
                Finding(
                    project_id=project.id,
                    policy_id=row.policy_id,
                    policy_name=row.policy_name,
                    rule_id=row.rule_id,
                    latest_evaluation_id=row.id,
                    latest_outcome=row.outcome,
                    severity=row.severity,
                    state="open",
                    first_seen_at=row.evaluated_at,
                    last_seen_at=row.evaluated_at,
                )
            )
            tally.findings["opened"] += 1
        return
    finding.policy_id = row.policy_id
    finding.latest_evaluation_id = row.id
    finding.latest_outcome = row.outcome
    if finding.state == "excepted":
        return
    if row.outcome == "fail":
        finding.severity = row.severity
        finding.last_seen_at = row.evaluated_at
        if finding.state == "resolved":
            finding.state = "open"
            finding.resolved_at = None
            finding.resolution = None
            tally.findings["reopened"] += 1
    elif row.outcome == "pass" and finding.state == "open":
        finding.state = "resolved"
        finding.resolved_at = row.evaluated_at
        finding.resolution = "passed"
        tally.findings["resolved"] += 1


def _close_inapplicable(
    db: Session, project: GitLabProject, applied: set[tuple[str, str]], tally: Tally
) -> None:
    open_findings = db.scalars(
        select(Finding)
        .where(Finding.project_id == project.id, Finding.state == "open")
        .with_for_update()
    ).all()
    stale = [f for f in open_findings if (f.policy_name, f.rule_id) not in applied]
    if not stale:
        return
    now = db.execute(select(func.now())).scalar_one()
    for finding in stale:
        finding.state = "resolved"
        finding.resolved_at = now
        finding.resolution = "no_longer_applies"
        tally.findings["no_longer_applies"] += 1


def store_project(
    db: Session,
    instance_id: int,
    project: GitLabProject,
    resolution: Resolution,
    results: Sequence[RuleResult],
    run_id: int | None,
    tally: Tally,
) -> list[Evaluation]:
    """Store a project's results and update its findings. The caller commits."""
    observations: dict[tuple[str, ...], Observation | None] = {}
    rows: list[Evaluation] = []
    for result in results:
        evidence = evidence_for(result)
        if evidence.sources not in observations:
            observations[evidence.sources] = _latest_observation(
                db, instance_id, project.id, evidence.sources
            )
        observation = observations[evidence.sources]
        result = backed(result, evidence, observation)
        row = _row(instance_id, project, result, observation, run_id)
        db.add(row)
        db.flush()
        _follow(db, project, row, tally)
        tally.outcomes[row.outcome] += 1
        rows.append(row)
    if resolution.ancestry == "complete":
        _close_inapplicable(db, project, {(row.policy_name, row.rule_id) for row in rows}, tally)
    return rows
