"""Drift: each project's effective template settings against collected evidence (M14.2).

Each setting is judged by the evaluator of the rule type that already judges it
(``RULE_TYPE``), through a probe rule built from the template's value, so freshness, access,
capability and plan problems make it ``unknown`` or ``unsupported`` exactly as for baselines,
never ``aligned``. Templates name exact values, which the probes check from both sides:

- ``visibility: v``: aligned when a visibility rule allowing only ``v`` passes;
- a boolean: the probe requires the setting; aligned when it passes for ``true`` and fails for
  ``false``;
- ``merge_requests.min_approvals: n``: aligned when requiring ``n`` passes and requiring
  ``n + 1`` fails, so a project needing more approvals than the template drifts too;
- ``security_features``: aligned when every listed feature is enabled;
- ``labels_consistent: true``: aligned when no label drift reaches the project;
- ``deployments.forbid_pipeline_triggerer_approval``: a boolean, judged by
  ``gitlab.deployment.triggerer_cannot_approve``.

An approved, unexpired template exception for the project and setting turns ``drift`` into
``excepted``; it never hides ``unknown`` or ``unsupported``.
"""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte.db.models import GitLabInstance, GitLabProject, TemplateException
from baluarte.integrations.gitlab.settings import GitLabSettings
from baluarte.policy.evaluation.engine import EvaluationContext, evaluate_rule
from baluarte.policy.evaluation.model import CapabilityStatus, CollectionContext, PolicyRef
from baluarte.policy.evaluation.project import (
    PROVIDER_EVALUATORS,
    capability_statuses,
    collection_contexts,
    project_context,
)
from baluarte.policy.schema import (
    DefaultBranchProtectedRule,
    DeploymentTriggererApprovalRule,
    GitHubSecurityFeaturesRule,
    LabelsConsistentRule,
    MergeRequestApprovalsRule,
    ProjectVisibilityRule,
    Rule,
)
from baluarte.scope.exclusions import coverage
from baluarte.templates.exceptions import EXPIRING_WITHIN, open_for_project
from baluarte.templates.resolve import Effective, EffectiveSetting, Source, effective_settings

Status = Literal["aligned", "drift", "unknown", "unsupported", "excepted"]
STATUSES: tuple[Status, ...] = ("aligned", "drift", "excepted", "unknown", "unsupported")
MAX_APPROVALS = 20

# Probe results are not stored; they name the template so a reason can be traced.
_PROBE = PolicyRef(policy_id=0, name="configuration-template", version=1, yaml_digest="0" * 64)


class ExceptionRef(BaseModel):
    id: int
    state: str
    expires_at: datetime
    # Approved and ending within EXPIRING_WITHIN.
    expiring: bool


class SettingDrift(BaseModel):
    key: str
    expected: Any
    status: Status
    # What the evidence shows, or why it cannot be told.
    reason: str
    rule_type: str
    observed_at: datetime | None
    decided_by: Source
    exception: ExceptionRef | None


class ProjectRef(BaseModel):
    id: int
    gitlab_id: int
    path: str


class ProjectDrift(BaseModel):
    project: ProjectRef
    connection_id: int
    provider: str
    ancestry: Literal["complete", "incomplete"]
    evaluated_at: datetime
    settings: list[SettingDrift]
    counts: dict[str, int]
    # drift when any setting drifts; otherwise unknown when any is unknown; otherwise aligned.
    # None when no template applies.
    status: Literal["aligned", "drift", "unknown"] | None


def _visibility(value: str) -> Rule:
    return ProjectVisibilityRule.model_validate(
        {"id": "template-visibility", "type": "gitlab.project.visibility", "allowed": [value]}
    )


TRIGGERER_KEY = "deployments.forbid_pipeline_triggerer_approval"


def _triggerer() -> Rule:
    return DeploymentTriggererApprovalRule(
        id="template-deployments", type="gitlab.deployment.triggerer_cannot_approve"
    )


def _approvals(min_approvals: int, flag: str | None = None) -> Rule:
    fields: dict[str, Any] = {
        "id": "template-merge-requests",
        "type": "gitlab.project.merge_request_approvals",
        "min_approvals": min_approvals,
    }
    if flag is not None:
        fields[flag] = True
    if min_approvals > MAX_APPROVALS:
        # One above the schema's limit, only to tell "exactly the limit" from "more".
        return MergeRequestApprovalsRule.model_construct(**fields)
    return MergeRequestApprovalsRule.model_validate(fields)


class _Judge:
    def __init__(self, context: EvaluationContext, provider: str) -> None:
        self.context = context
        self.evaluators = PROVIDER_EVALUATORS[provider]

    def run(self, rule: Rule) -> tuple[str, str, datetime | None]:
        evaluator = self.evaluators.get(rule.type)
        if evaluator is None:
            return "unsupported", "No evaluator judges this setting on this provider.", None
        result = evaluate_rule(evaluator, _PROBE, rule, self.context)
        return result.outcome, result.reason, result.observed_at

    def exact(
        self, rule: Rule, expected: bool, wording: str
    ) -> tuple[Status, str, datetime | None]:
        """A boolean setting: the probe requires it; aligned when that matches ``expected``."""
        outcome, reason, at = self.run(rule)
        if outcome not in ("pass", "fail"):
            return outcome, reason, at  # type: ignore[return-value]
        holds = outcome == "pass"
        if holds == expected:
            return "aligned", reason, at
        if expected:
            return "drift", reason, at
        return "drift", f"{wording} is set, and the template says it should not be. {reason}", at

    def setting(self, key: str, value: Any) -> tuple[Status, str, datetime | None]:
        if key == "visibility":
            outcome, reason, at = self.run(_visibility(value))
            return _decided(outcome), reason, at
        if key == "default_branch_protected":
            rule: Rule = DefaultBranchProtectedRule(
                id="template-default-branch", type="gitlab.branch.default_protected", required=True
            )
            return self.exact(rule, value, "Default branch protection")
        if key == "merge_requests.min_approvals":
            return self.min_approvals(int(value))
        if key.startswith("merge_requests."):
            flag = key.removeprefix("merge_requests.")
            return self.exact(_approvals(0, flag), value, flag.replace("_", " ").capitalize())
        if key == TRIGGERER_KEY:
            return self.exact(_triggerer(), value, "Pipeline triggerer approval being forbidden")
        if key == "security_features":
            features: Rule = GitHubSecurityFeaturesRule(
                id="template-security-features",
                type="github.repository.security_features",
                features=list(value),
            )
            outcome, reason, at = self.run(features)
            return _decided(outcome), reason, at
        if key == "labels_consistent":
            labels: Rule = LabelsConsistentRule(
                id="template-labels", type="gitlab.labels.consistent"
            )
            outcome, reason, at = self.run(labels)
            return _decided(outcome), reason, at
        raise ValueError(f"unknown template setting {key}")

    def min_approvals(self, wanted: int) -> tuple[Status, str, datetime | None]:
        outcome, reason, at = self.run(_approvals(wanted))
        if outcome not in ("pass", "fail"):
            return outcome, reason, at  # type: ignore[return-value]
        if outcome == "fail":
            return "drift", reason, at
        more, more_reason, _ = self.run(_approvals(wanted + 1))
        if more == "fail":
            return "aligned", reason, at
        if more == "pass":
            return (
                "drift",
                f"Merge requests need more than the {wanted} approvals the template sets.",
                at,
            )
        return more, more_reason, at  # type: ignore[return-value]

    def current(self, key: str, value: Any) -> "Current":
        """The value the evidence shows for ``key`` (``value`` is the template's, used for the
        security features it names), or why it cannot be told."""
        if key == "visibility":
            outcome, reason, at = self.run(_visibility(str(value)))
            if outcome not in ("pass", "fail") or self.context.project is None:
                return Current(None, outcome, reason, at)
            return Current(self.context.project.visibility, outcome, reason, at)
        if key == "default_branch_protected":
            rule: Rule = DefaultBranchProtectedRule(
                id="template-default-branch", type="gitlab.branch.default_protected", required=True
            )
            return self._holds(rule)
        if key == "merge_requests.min_approvals":
            return self._approvals_required()
        if key.startswith("merge_requests."):
            return self._holds(_approvals(0, key.removeprefix("merge_requests.")))
        if key == TRIGGERER_KEY:
            return self._holds(_triggerer())
        if key == "security_features":
            enabled: list[str] = []
            last = Current(None, "unsupported", "", None)
            for feature in value:
                features: Rule = GitHubSecurityFeaturesRule(
                    id="template-security-features",
                    type="github.repository.security_features",
                    features=[feature],
                )
                found = self._holds(features)
                if found.value is None:
                    return found
                if found.value:
                    enabled.append(feature)
                last = found
            return Current(enabled, last.outcome, last.reason, last.observed_at)
        raise ValueError(f"{key} has no single current value")

    def _holds(self, rule: Rule) -> "Current":
        outcome, reason, at = self.run(rule)
        if outcome not in ("pass", "fail"):
            return Current(None, outcome, reason, at)
        return Current(outcome == "pass", outcome, reason, at)

    def _approvals_required(self) -> "Current":
        """The approvals merge requests need, found by probing: the largest ``n`` for which
        requiring ``n`` passes, up to one above the schema's limit."""
        outcome, reason, at = self.run(_approvals(1))
        if outcome not in ("pass", "fail"):
            return Current(None, outcome, reason, at)
        low, high = (0, 0) if outcome == "fail" else (1, MAX_APPROVALS + 1)
        while low < high:
            middle = (low + high + 1) // 2
            found, why, _ = self.run(_approvals(middle))
            if found == "pass":
                low = middle
            elif found == "fail":
                high = middle - 1
            else:
                return Current(None, found, why, at)
        return Current(low, "pass", reason, at)


@dataclass(frozen=True)
class Current:
    """A setting's value as the evidence shows it; None when it cannot be told (``outcome`` is
    then unknown or unsupported and ``reason`` says why)."""

    value: Any
    outcome: str
    reason: str
    observed_at: datetime | None


def judge_for(
    db: Session,
    instance: GitLabInstance,
    project: GitLabProject,
    collections: Mapping[str, CollectionContext],
    capabilities: Mapping[str, CapabilityStatus] | None,
    ancestry: Literal["complete", "incomplete"],
    *,
    labels: bool = False,
) -> "_Judge":
    """A judge for ``project``'s stored evidence, for callers outside this module."""
    context = project_context(
        db,
        project,
        ancestry,
        collections,
        capabilities,
        None,
        instance.provider,
        labels=labels,
        release=instance.provider == "gitlab",
    )
    return _Judge(context, instance.provider)


def _decided(outcome: str) -> Status:
    if outcome == "pass":
        return "aligned"
    if outcome == "fail":
        return "drift"
    return outcome  # type: ignore[return-value]


def _exception_ref(row: TemplateException, now: datetime) -> ExceptionRef:
    return ExceptionRef(
        id=row.id,
        state=row.state,
        expires_at=row.expires_at,
        expiring=row.state == "approved" and row.expires_at - now <= EXPIRING_WITHIN,
    )


def _project_status(counts: Mapping[str, int]) -> Literal["aligned", "drift", "unknown"] | None:
    if not counts:
        return None
    if counts.get("drift"):
        return "drift"
    if counts.get("unknown"):
        return "unknown"
    return "aligned"


def project_drift(
    db: Session,
    instance: GitLabInstance,
    project: GitLabProject,
    collections: Mapping[str, CollectionContext],
    capabilities: Mapping[str, CapabilityStatus] | None = None,
    effective: Effective | None = None,
) -> ProjectDrift:
    """Every effective setting of ``project`` judged against its evidence."""
    if effective is None:
        effective = effective_settings(db, instance, project)
    now: datetime = db.execute(select(func.now())).scalar_one()
    settings: list[SettingDrift] = []
    if effective.settings:
        context = project_context(
            db,
            project,
            effective.ancestry,
            collections,
            capabilities,
            now,
            instance.provider,
            labels=any(s.key == "labels_consistent" for s in effective.settings),
            release=instance.provider == "gitlab"
            and any(s.key == TRIGGERER_KEY for s in effective.settings),
        )
        judge = _Judge(context, instance.provider)
        waivers = open_for_project(db, project.id)
        for setting in effective.settings:
            settings.append(_setting(judge, setting, waivers.get(setting.key), now))
    counts: Counter[str] = Counter(s.status for s in settings)
    return ProjectDrift(
        project=ProjectRef(
            id=project.id, gitlab_id=project.gitlab_id, path=project.path_with_namespace
        ),
        connection_id=instance.id,
        provider=instance.provider,
        ancestry=effective.ancestry,
        evaluated_at=now,
        settings=settings,
        counts=dict(counts),
        status=_project_status(counts),
    )


def _setting(
    judge: _Judge, setting: EffectiveSetting, waiver: TemplateException | None, now: datetime
) -> SettingDrift:
    status, reason, observed_at = judge.setting(setting.key, setting.value)
    if status == "unsupported" and setting.support_note:
        reason = f"{reason} {setting.support_note}" if reason else setting.support_note
    in_force = waiver is not None and waiver.state == "approved"
    if status == "drift" and in_force and waiver is not None:
        status = "excepted"
        reason = f"Excepted until {waiver.expires_at:%Y-%m-%d}. {reason}"
    return SettingDrift(
        key=setting.key,
        expected=setting.value,
        status=status,
        reason=reason,
        rule_type=setting.rule_type,
        observed_at=observed_at,
        decided_by=setting.decided_by,
        exception=_exception_ref(waiver, now) if waiver is not None else None,
    )


# --- alignment across a connection ---


class Tally(BaseModel):
    aligned: int = 0
    drift: int = 0
    excepted: int = 0
    unknown: int = 0
    unsupported: int = 0

    def add(self, status: str) -> None:
        setattr(self, status, getattr(self, status) + 1)


class GroupTally(BaseModel):
    name: str
    projects: int
    projects_drifting: int
    settings: Tally


class DriftingProject(BaseModel):
    project: ProjectRef
    status: Literal["drift", "unknown"]
    counts: dict[str, int]


class Alignment(BaseModel):
    connection_id: int
    provider: str
    evaluated_at: datetime
    # Active, monitored, not archived projects.
    projects: int
    projects_with_templates: int
    projects_aligned: int
    projects_drifting: int
    projects_unknown: int
    settings: Tally
    by_template: list[GroupTally]
    by_group: list[GroupTally]
    # Projects with drift first, then those that cannot be told; at most MAX_LISTED.
    attention: list[DriftingProject]
    attention_truncated: bool
    # Approved exceptions ending within EXPIRING_WITHIN.
    expiring_exceptions: int


MAX_LISTED = 200


def alignment(db: Session, instance: GitLabInstance, settings: GitLabSettings | None) -> Alignment:
    """Drift for every active, monitored, unarchived project of a connection."""
    collections = collection_contexts(db, instance.id, settings)
    capabilities = capability_statuses(db, instance.id)
    scope = coverage(db, instance.id)
    now: datetime = db.execute(select(func.now())).scalar_one()
    projects = db.scalars(
        select(GitLabProject)
        .where(
            GitLabProject.instance_id == instance.id,
            GitLabProject.state == "active",
            GitLabProject.archived.is_(False),
        )
        .order_by(GitLabProject.path_with_namespace)
    ).all()
    total = Tally()
    by_template: dict[str, tuple[set[int], set[int], Tally]] = {}
    by_group: dict[str, tuple[set[int], set[int], Tally]] = {}
    attention: list[DriftingProject] = []
    counted = with_templates = aligned = drifting = unknown = 0
    for project in projects:
        if scope.of(project) is not None:
            continue
        counted += 1
        effective = effective_settings(db, instance, project)
        if not effective.settings:
            continue
        with_templates += 1
        drift = project_drift(db, instance, project, collections, capabilities, effective)
        group = project.path_with_namespace.split("/", 1)[0]
        for setting in drift.settings:
            total.add(setting.status)
            template = f"{setting.decided_by.template} v{setting.decided_by.version}"
            for table, name in ((by_template, template), (by_group, group)):
                seen, drifted, tally = table.setdefault(name, (set(), set(), Tally()))
                seen.add(project.id)
                if setting.status == "drift":
                    drifted.add(project.id)
                tally.add(setting.status)
        if drift.status == "aligned":
            aligned += 1
        elif drift.status in ("drift", "unknown"):
            if drift.status == "drift":
                drifting += 1
            else:
                unknown += 1
            attention.append(
                DriftingProject(project=drift.project, status=drift.status, counts=drift.counts)
            )
    attention.sort(key=lambda entry: (entry.status != "drift", entry.project.path))
    expiring = db.scalar(
        select(func.count())
        .select_from(TemplateException)
        .where(
            TemplateException.instance_id == instance.id,
            TemplateException.state == "approved",
            TemplateException.expires_at > now,
            TemplateException.expires_at <= now + EXPIRING_WITHIN,
        )
    )

    def tallies(table: dict[str, tuple[set[int], set[int], Tally]]) -> list[GroupTally]:
        return [
            GroupTally(name=name, projects=len(seen), projects_drifting=len(drifted), settings=t)
            for name, (seen, drifted, t) in sorted(table.items())
        ]

    return Alignment(
        connection_id=instance.id,
        provider=instance.provider,
        evaluated_at=now,
        projects=counted,
        projects_with_templates=with_templates,
        projects_aligned=aligned,
        projects_drifting=drifting,
        projects_unknown=unknown,
        settings=total,
        by_template=tallies(by_template),
        by_group=tallies(by_group),
        attention=attention[:MAX_LISTED],
        attention_truncated=len(attention) > MAX_LISTED,
        expiring_exceptions=int(expiring or 0),
    )
