"""The effective control mapping, and each control's status from the latest evaluation.

The effective mapping is the default mapping, less the default pairs the organization unmapped,
plus the rule types it mapped (active changes only). A control's status comes from the results
of its mapped rule types in the latest successful evaluation run, across every baseline in
force, the same snapshot as the posture dashboard (``GET /evaluations/summary``):

- ``not_applicable``: an active change scopes the control out; results are still counted.
- ``not_met``: a mapped rule failed for some project. A failure outranks ``manual``.
- ``manual``: an active change records that the control is met outside GitLab.
- ``review_overdue``: the not-applicable or manual change passed its ``review_by`` date, so it
  no longer counts until it is renewed.
- ``unknown``: some result was unknown or unsupported, and none failed.
- ``met``: every result passed or was excepted.
- ``no_results``: rule types are mapped, but no baseline in force has a rule of those types.
- ``not_covered``: no rule type is mapped.

The mapping is guidance for an organization and its auditor; a ``met`` control is supporting
evidence, not a certification.
"""

import hashlib
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte.controls.catalogue import CONTROLS, DEFAULT_MAPPING, MAPPING_VERSION, Control
from baluarte.db.models import (
    ControlMappingChange,
    Evaluation,
    EvaluationRun,
    GitLabProject,
    Policy,
)
from baluarte.scope.exclusions import coverage

ControlStatus = Literal[
    "met",
    "not_met",
    "unknown",
    "no_results",
    "not_covered",
    "not_applicable",
    "manual",
    "review_overdue",
]
Key = tuple[str, str]


@dataclass(frozen=True)
class MappedRule:
    rule_type: str
    source: Literal["default", "organization"]
    # The organization's change that mapped it; None for a default pair.
    change_id: int | None
    # The catalogue's rationale for a default pair, or the change's justification.
    text: str


@dataclass(frozen=True)
class Effective:
    """The mapping in force: rule types per control, and each control's status change."""

    rules: dict[Key, list[MappedRule]]
    # The active not_applicable or manual change of a control.
    status_changes: dict[Key, ControlMappingChange]
    # The active unmap changes of a control, so they can be withdrawn.
    unmapped: dict[Key, list[ControlMappingChange]]

    def controls_for(self, rule_type: str) -> list[Key]:
        """The controls ``rule_type`` supports, in catalogue order."""
        return [
            (item.framework, item.id)
            for item in CONTROLS
            if rule_type in self.rule_types(item.framework, item.id)
        ]

    def rule_types(self, framework: str, control_id: str) -> list[str]:
        return [rule.rule_type for rule in self.rules.get((framework, control_id), [])]


def effective(db: Session) -> Effective:
    active = list(
        db.scalars(
            select(ControlMappingChange)
            .where(ControlMappingChange.state == "active")
            .order_by(ControlMappingChange.id)
        )
    )
    unmapped = {(c.framework, c.control_id, c.rule_type) for c in active if c.kind == "unmap"}
    rules: dict[Key, list[MappedRule]] = defaultdict(list)
    for pair in DEFAULT_MAPPING:
        if (pair.framework, pair.control_id, pair.rule_type) not in unmapped:
            rules[(pair.framework, pair.control_id)].append(
                MappedRule(pair.rule_type, "default", None, pair.rationale)
            )
    status_changes: dict[Key, ControlMappingChange] = {}
    unmaps: dict[Key, list[ControlMappingChange]] = defaultdict(list)
    for change in active:
        key = (change.framework, change.control_id)
        if change.kind == "map" and change.rule_type is not None:
            rules[key].append(
                MappedRule(change.rule_type, "organization", change.id, change.justification)
            )
        elif change.kind == "unmap":
            unmaps[key].append(change)
        elif change.kind in ("not_applicable", "manual"):
            status_changes[key] = change
    return Effective(rules=dict(rules), status_changes=status_changes, unmapped=dict(unmaps))


@dataclass
class Counts:
    passed: int = 0
    fail: int = 0
    unknown: int = 0
    excepted: int = 0
    projects: set[int] = field(default_factory=set)

    def add(self, outcome: str, project_id: int) -> None:
        if outcome == "pass":
            self.passed += 1
        elif outcome in ("fail", "excepted"):
            setattr(self, outcome, getattr(self, outcome) + 1)
        else:
            self.unknown += 1
        self.projects.add(project_id)

    def merge(self, other: "Counts") -> None:
        self.passed += other.passed
        self.fail += other.fail
        self.unknown += other.unknown
        self.excepted += other.excepted
        self.projects |= other.projects

    @property
    def results(self) -> int:
        return self.passed + self.fail + self.unknown + self.excepted


@dataclass(frozen=True)
class RuleCounts:
    policy: str
    # The baseline version the run judged; a run judges each project under one version.
    version: int
    rule_id: str
    rule_type: str
    counts: Counts


@dataclass(frozen=True)
class ControlPosture:
    control: Control
    status: ControlStatus
    mapped: list[MappedRule]
    # Default pairs the organization removed, with the change that removed them.
    unmapped: list[ControlMappingChange]
    change: ControlMappingChange | None
    counts: Counts
    rules: list[RuleCounts]


@dataclass(frozen=True)
class Posture:
    # The evaluation run the counts come from; None when none has succeeded.
    evaluation_run_id: int | None
    controls: list[ControlPosture]


def latest_run(db: Session, instance_id: int | None) -> EvaluationRun | None:
    return db.scalars(
        select(EvaluationRun)
        .where(EvaluationRun.instance_id == (instance_id or 0), EvaluationRun.status == "succeeded")
        .order_by(EvaluationRun.started_at.desc(), EvaluationRun.id.desc())
        .limit(1)
    ).first()


def _rule_counts(db: Session, run: EvaluationRun | None) -> dict[str, list[RuleCounts]]:
    """Results of the run per rule type, then per (baseline, rule)."""
    if run is None:
        return {}
    found: dict[tuple[str, str, int, str], Counts] = defaultdict(Counts)
    rows = db.execute(
        select(
            Evaluation.policy_name,
            Policy.version,
            Evaluation.rule_id,
            Evaluation.rule_type,
            Evaluation.outcome,
            Evaluation.project_id,
        )
        .join(Policy, Policy.id == Evaluation.policy_id)
        .join(GitLabProject, GitLabProject.id == Evaluation.project_id)
        .where(
            Evaluation.evaluation_run_id == run.id,
            # Projects out of monitoring scope are not counted, even if the run judged them.
            coverage(db, run.instance_id).project_included(),
        )
    )
    for policy, version, rule_id, rule_type, outcome, project_id in rows:
        found[(rule_type, policy, version, rule_id)].add(outcome, project_id)
    by_type: dict[str, list[RuleCounts]] = defaultdict(list)
    for (rule_type, policy, version, rule_id), counts in sorted(found.items()):
        by_type[rule_type].append(RuleCounts(policy, version, rule_id, rule_type, counts))
    return by_type


def status_of(
    mapped: list[MappedRule], counts: Counts, change: ControlMappingChange | None, today: date
) -> ControlStatus:
    if change is not None and change.kind == "not_applicable":
        overdue = change.review_by is not None and change.review_by < today
        return "review_overdue" if overdue else "not_applicable"
    if counts.fail:
        return "not_met"
    if change is not None:
        overdue = change.review_by is not None and change.review_by < today
        return "review_overdue" if overdue else "manual"
    if not mapped:
        return "not_covered"
    if counts.results == 0:
        return "no_results"
    if counts.unknown:
        return "unknown"
    return "met"


def posture(
    db: Session,
    instance_id: int | None,
    framework: str | None = None,
    controls: Iterable[Control] | None = None,
) -> Posture:
    run = latest_run(db, instance_id)
    by_type = _rule_counts(db, run)
    mapping = effective(db)
    today: date = db.execute(select(func.current_date())).scalar_one()
    out: list[ControlPosture] = []
    for item in controls if controls is not None else CONTROLS:
        if framework is not None and item.framework != framework:
            continue
        key = (item.framework, item.id)
        mapped = mapping.rules.get(key, [])
        rules = [rule for m in mapped for rule in by_type.get(m.rule_type, [])]
        counts = Counts()
        for rule in rules:
            counts.merge(rule.counts)
        change = mapping.status_changes.get(key)
        out.append(
            ControlPosture(
                control=item,
                status=status_of(mapped, counts, change, today),
                mapped=mapped,
                unmapped=mapping.unmapped.get(key, []),
                change=change,
                counts=counts,
                rules=rules,
            )
        )
    return Posture(evaluation_run_id=run.id if run else None, controls=out)


def mapping_summary(db: Session) -> dict[str, Any]:
    """The mapping in force, for an evidence export's manifest.

    Every effective rule type and control pair, and every active change with a SHA-256 of its
    justification, so an auditor can tie the package to the changes the audit log records.
    """
    mapping = effective(db)
    active = db.scalars(
        select(ControlMappingChange)
        .where(ControlMappingChange.state == "active", ControlMappingChange.kind != "withdraw")
        .order_by(ControlMappingChange.id)
    )
    return {
        "mapping_version": MAPPING_VERSION,
        "effective": [
            {
                "framework": framework,
                "control_id": control_id,
                "rule_type": rule.rule_type,
                "source": rule.source,
                "change_id": rule.change_id,
            }
            for (framework, control_id), rules in sorted(mapping.rules.items())
            for rule in rules
        ],
        "changes": [
            {
                "id": change.id,
                "framework": change.framework,
                "control_id": change.control_id,
                "kind": change.kind,
                "rule_type": change.rule_type,
                "review_by": change.review_by.isoformat() if change.review_by else None,
                "self_approved": change.self_approved,
                "mapping_version": change.mapping_version,
                "justification_sha256": hashlib.sha256(
                    change.justification.encode("utf-8")
                ).hexdigest(),
            }
            for change in active
        ],
    }
