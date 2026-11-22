"""Rule coverage: which rule types the baselines in force use, and which controls that leaves
without automated evidence (docs/POLICY_ENGINE.md, docs/COMPLIANCE.md). Read-only.

A baseline is in force where it is applied: the versions pinned by ``policy_assignment`` are the
ones evaluation judges (any target, any ``spec.scope``). ``policy`` and ``policy_assignment``
hold only approved changes, so a proposed version or assignment never counts, and a baseline
that is saved but not applied anywhere does not either.

A rule type's ``supported`` comes from the instance's latest capability snapshot, for the probe
its evaluator depends on: ``true`` when it needs none or the probe answered, ``false`` only when
detection could tell GitLab lacks it (``unsupported``), and ``null`` otherwise, because a refusal
or a 404 does not prove the feature is missing. On a provider that does not judge the rule type
(``providers_of``), ``supported`` is ``false``.

A control is listed when every rule type the mapping in force (``baluarte.controls.posture``)
maps to it is out of force: Baluarte collects no automated evidence for it. Controls with no
mapped rule type at all are not listed here; the control pages show them as not covered.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte.controls.catalogue import CONTROLS
from baluarte.controls.posture import effective
from baluarte.db.models import GitLabInstance, Policy, PolicyAssignment
from baluarte.labels import taxonomy
from baluarte.policy.evaluation.project import PROVIDER_EVALUATORS, providers_of
from baluarte.policy.loader import parse_policy
from baluarte.policy.schema import RULE_TYPES


@dataclass(frozen=True)
class BaselineRef:
    name: str
    version: int


@dataclass(frozen=True)
class RuleTypeCoverage:
    rule_type: str
    in_force: bool
    # The applied baseline versions with a rule of this type, by name then version.
    baselines: list[BaselineRef]
    supported: bool | None
    # The capability probe the rule type's evaluator depends on, or None.
    capability: str | None


@dataclass(frozen=True)
class UncoveredControl:
    framework: str
    control_id: str
    title: str
    area: str
    # The mapped rule types, none of which is in force.
    rule_types: list[str]
    # An active organization decision that the control is met another way or does not apply.
    status_change: Literal["not_applicable", "manual"] | None


@dataclass(frozen=True)
class BaselineRules:
    """A baseline's latest version and the rule types it uses, applied or not."""

    name: str
    latest_version: int
    applied: bool
    rule_types: list[str]


@dataclass(frozen=True)
class Coverage:
    rule_types: list[RuleTypeCoverage]
    controls: list[UncoveredControl]
    baselines: list[BaselineRules]
    # The label taxonomy in force, which the label consistency rule follows; None without one.
    label_taxonomy_version: int | None
    # When capabilities were last detected; None when never.
    capabilities_checked_at: datetime | None


def _rule_types(source: str) -> list[str]:
    return [rule.type for rule in parse_policy(source).document.spec.rules]


def _probes(instance: GitLabInstance | None) -> tuple[dict[str, str] | None, datetime | None]:
    """Each probe's raw status, or None when capabilities were never detected or detection
    failed; and when they were checked."""
    snapshot = instance.capability_snapshot if instance is not None else None
    if not isinstance(snapshot, dict):
        return None, None
    checked_at: datetime | None = None
    raw = snapshot.get("checked_at")
    if isinstance(raw, str):
        try:
            checked_at = datetime.fromisoformat(raw)
        except ValueError:
            checked_at = None
    probes = snapshot.get("probes")
    if snapshot.get("status") == "failed" or not isinstance(probes, dict):
        return None, checked_at
    return {
        name: str(probe.get("status")) for name, probe in probes.items() if isinstance(probe, dict)
    }, checked_at


def _supported(capability: str | None, probes: dict[str, str] | None) -> bool | None:
    if capability is None:
        return True
    status = probes.get(capability) if probes is not None else None
    if status == "supported":
        return True
    if status == "unsupported":
        return False
    return None


def coverage(db: Session, instance: GitLabInstance | None) -> Coverage:
    """Rule types and controls against the baselines in force on ``instance``."""
    used: dict[str, set[BaselineRef]] = defaultdict(set)
    applied_names: set[str] = set()
    if instance is not None:
        rows = db.execute(
            select(Policy.name, Policy.version, Policy.source_yaml)
            .join(PolicyAssignment, PolicyAssignment.policy_id == Policy.id)
            .where(PolicyAssignment.instance_id == instance.id)
            .distinct()
        ).all()
        for name, version, source in rows:
            applied_names.add(name)
            for rule_type in _rule_types(source):
                used[rule_type].add(BaselineRef(name, version))

    probes, checked_at = _probes(instance)
    provider = instance.provider if instance is not None else "gitlab"
    evaluators = PROVIDER_EVALUATORS.get(provider, PROVIDER_EVALUATORS["gitlab"])
    rule_types: list[RuleTypeCoverage] = []
    for rule_type in RULE_TYPES:
        evaluator = evaluators.get(rule_type)
        capability = evaluator.capability if evaluator is not None else None
        supported: bool | None = _supported(capability, probes)
        if provider not in providers_of(rule_type):
            supported = False
        refs = sorted(used.get(rule_type, set()), key=lambda ref: (ref.name, ref.version))
        rule_types.append(
            RuleTypeCoverage(
                rule_type=rule_type,
                in_force=bool(refs),
                baselines=refs,
                supported=supported,
                capability=capability,
            )
        )

    mapping = effective(db)
    controls: list[UncoveredControl] = []
    for item in CONTROLS:
        key = (item.framework, item.id)
        mapped = list(dict.fromkeys(mapping.rule_types(*key)))
        if not mapped or any(rule_type in used for rule_type in mapped):
            continue
        change = mapping.status_changes.get(key)
        status_change: Literal["not_applicable", "manual"] | None = None
        if change is not None:
            status_change = "not_applicable" if change.kind == "not_applicable" else "manual"
        controls.append(
            UncoveredControl(
                framework=item.framework,
                control_id=item.id,
                title=item.title,
                area=item.area,
                rule_types=mapped,
                status_change=status_change,
            )
        )

    latest = (
        select(Policy.name, func.max(Policy.version).label("version"))
        .group_by(Policy.name)
        .subquery()
    )
    baselines = [
        BaselineRules(
            name=name,
            latest_version=version,
            applied=name in applied_names,
            rule_types=list(dict.fromkeys(_rule_types(source))),
        )
        for name, version, source in db.execute(
            select(Policy.name, Policy.version, Policy.source_yaml)
            .join(latest, (latest.c.name == Policy.name) & (latest.c.version == Policy.version))
            .order_by(Policy.name)
        ).all()
    ]

    labels = taxonomy.in_force(db, instance.id if instance is not None else None)
    return Coverage(
        rule_types=rule_types,
        controls=controls,
        baselines=baselines,
        label_taxonomy_version=labels.version if labels is not None else None,
        capabilities_checked_at=checked_at,
    )
