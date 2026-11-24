"""Evaluating the policies in force for one project against the inventory. Read-only.

The caller resolves the project's policies (``baluarte.policy.assignments.resolve_project``) and
reads collection freshness (``collection_contexts``) and capabilities (``capability_statuses``)
once, so a batch of projects shares one reading. Persisting the results is BAL-021.
"""

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Literal, get_args

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte.db.models import (
    ChangeRecord,
    GitLabGroup,
    GitLabInstance,
    GitLabProject,
    Observation,
    Policy,
)
from baluarte.evidence.observations import latest
from baluarte.integrations.github.branches import BRANCH_SOURCE as GITHUB_BRANCH_SOURCE
from baluarte.integrations.github.discovery import ORG_SOURCE as GITHUB_ORG_SOURCE
from baluarte.integrations.github.repositories import SECURITY_SOURCE as GITHUB_SECURITY_SOURCE
from baluarte.integrations.gitlab.access import (
    ACCESS_TOKENS_SOURCE,
    APPROVALS_SOURCE,
    MEMBERS_SOURCE,
    TWO_FACTOR_SOURCE,
)
from baluarte.integrations.gitlab.branches import PROTECTION_SOURCE
from baluarte.integrations.gitlab.changes import CHANGE_READ_SOURCE
from baluarte.integrations.gitlab.freshness import kind_freshness, report
from baluarte.integrations.gitlab.runs import RunKind
from baluarte.integrations.gitlab.security_policies import PROJECT_SOURCE as SECURITY_POLICY_SOURCE
from baluarte.integrations.gitlab.settings import GitLabSettings
from baluarte.policy.assignments import Resolution
from baluarte.policy.evaluation.access import ACCESS_EVALUATORS, ACCESS_SOURCES
from baluarte.policy.evaluation.changes import (
    CHANGE_EVALUATORS,
    CHANGE_RULE_TYPES,
    MAX_WINDOW_DAYS,
)
from baluarte.policy.evaluation.default_branch import DEFAULT_BRANCH_PROTECTED
from baluarte.policy.evaluation.engine import (
    BranchProtection,
    ChangeEntry,
    ChangeHistory,
    EvaluationContext,
    ProjectFacts,
    ProjectObservation,
    RuleEvaluator,
    SecurityPolicies,
    evaluate_rule,
)
from baluarte.policy.evaluation.github import EVALUATORS as GITHUB_EVALUATORS
from baluarte.policy.evaluation.github import not_on
from baluarte.policy.evaluation.labels import LABELS_CONSISTENT
from baluarte.policy.evaluation.labels import RULE_TYPE as LABELS_RULE_TYPE
from baluarte.policy.evaluation.labels import facts as label_facts
from baluarte.policy.evaluation.model import (
    CapabilityStatus,
    CollectionContext,
    PolicyRef,
    RuleResult,
    TargetRef,
)
from baluarte.policy.evaluation.security_policy import APPROVAL_POLICY, SCAN_EXECUTION_POLICY
from baluarte.policy.evaluation.visibility import VISIBILITY
from baluarte.policy.loader import parse_policy

# Rule types that can be evaluated; adding a rule type adds its evaluator here. Rules of other
# types are not evaluated (and produce no result) until their evaluator exists.
EVALUATORS: Mapping[str, RuleEvaluator] = {
    evaluator.rule_type: evaluator
    for evaluator in (
        VISIBILITY,
        DEFAULT_BRANCH_PROTECTED,
        SCAN_EXECUTION_POLICY,
        APPROVAL_POLICY,
        *ACCESS_EVALUATORS,
        *CHANGE_EVALUATORS,
        LABELS_CONSISTENT,
    )
}

# Each provider's evaluators (ADR-0014): a rule type a provider has no evaluator for is
# ``unsupported`` on that provider's projects, never left out and never passed.
_GITHUB = {evaluator.rule_type: evaluator for evaluator in GITHUB_EVALUATORS}
PROVIDER_EVALUATORS: Mapping[str, Mapping[str, RuleEvaluator]] = {
    "gitlab": {
        **EVALUATORS,
        **{
            rule_type: not_on(evaluator, "GitLab")
            for rule_type, evaluator in _GITHUB.items()
            if rule_type not in EVALUATORS
        },
    },
    "github": {
        **{rule_type: not_on(evaluator, "GitHub") for rule_type, evaluator in EVALUATORS.items()},
        **_GITHUB,
    },
}


def providers_of(rule_type: str) -> tuple[str, ...]:
    """The providers whose projects ``rule_type`` is evaluated on (not ``unsupported``)."""
    found = ["gitlab"] if rule_type in EVALUATORS else []
    if rule_type in _GITHUB:
        found.append("github")
    return tuple(found)


# Only a probe that answered is "supported". Capability detection cannot prove a feature is
# missing (a 404 may mean a hidden sample project, a 403 a token limit), so every other probe
# status is "unknown"; "unsupported" is reserved for when detection can tell.
_PROBE_STATUSES: Mapping[str, CapabilityStatus] = {"supported": "supported"}


def collection_contexts(
    db: Session, instance_id: int | None, settings: GitLabSettings | None
) -> dict[str, CollectionContext]:
    """Freshness of every collection kind, read once for a batch of evaluations."""
    freshness = report(db, instance_id, settings)
    contexts: dict[str, CollectionContext] = {}
    for kind in get_args(RunKind):
        found = freshness.collections.get(kind)
        if found is None and settings is not None:
            # Kinds outside the inventory report, such as security policy discovery.
            found = kind_freshness(db, instance_id, kind, settings)
        if found is None:
            contexts[kind] = CollectionContext(
                kind=kind, status="not_configured", last_success_at=None
            )
        else:
            contexts[kind] = CollectionContext(
                kind=kind, status=found.status, last_success_at=found.last_success_at
            )
    return contexts


def capability_statuses(db: Session, instance_id: int | None) -> dict[str, CapabilityStatus]:
    """Each probe's status from the instance's latest capability snapshot.

    Empty (every capability unknown) when there is no snapshot or the last detection failed.
    """
    instance = db.get(GitLabInstance, instance_id) if instance_id is not None else None
    snapshot = instance.capability_snapshot if instance is not None else None
    if not isinstance(snapshot, dict) or snapshot.get("status") == "failed":
        return {}
    probes = snapshot.get("probes")
    if not isinstance(probes, dict):
        return {}
    return {
        name: _PROBE_STATUSES.get(str(probe.get("status")), "unknown")
        for name, probe in probes.items()
        if isinstance(probe, dict)
    }


def branch_protection(db: Session, project: GitLabProject) -> BranchProtection | None:
    """The project's latest default-branch protection observation, if it was ever read."""
    observation = latest(
        db, project.instance_id, "project_id", project.id, sources=(PROTECTION_SOURCE,)
    )
    if observation is None:
        return None
    return BranchProtection(
        observation_id=observation.id,
        status=observation.collection_status,
        collected_at=observation.collected_at,
        payload=observation.payload,
        error=observation.error,
    )


def security_policies(db: Session, project: GitLabProject) -> SecurityPolicies | None:
    """The project's latest GitLab security policy observation, if it was ever read."""
    observation = latest(
        db, project.instance_id, "project_id", project.id, sources=(SECURITY_POLICY_SOURCE,)
    )
    if observation is None:
        return None
    return SecurityPolicies(
        observation_id=observation.id,
        status=observation.collection_status,
        collected_at=observation.collected_at,
        payload=observation.payload,
        error=observation.error,
    )


def project_observation(
    db: Session, project: GitLabProject, source: str
) -> ProjectObservation | None:
    """The project's latest observation of ``source``, if it was ever read."""
    observation = latest(db, project.instance_id, "project_id", project.id, sources=(source,))
    if observation is None:
        return None
    return ProjectObservation(
        observation_id=observation.id,
        status=observation.collection_status,
        collected_at=observation.collected_at,
        payload=observation.payload,
        error=observation.error,
    )


def change_history(db: Session, project: GitLabProject, as_of: datetime) -> ChangeHistory:
    """The project's change-read observation and its changes of the longest window up to
    ``as_of``, each with the observation that describes it now."""
    loaded_from = as_of - timedelta(days=MAX_WINDOW_DAYS)
    rows = db.execute(
        select(
            ChangeRecord.id,
            ChangeRecord.kind,
            ChangeRecord.changed_at,
            Observation.id,
            Observation.payload_digest,
            Observation.payload,
        )
        .join(Observation, Observation.id == ChangeRecord.observation_id)
        .where(
            ChangeRecord.project_id == project.id,
            ChangeRecord.changed_at >= loaded_from,
            ChangeRecord.changed_at <= as_of,
        )
        .order_by(ChangeRecord.changed_at, ChangeRecord.id)
    ).all()
    return ChangeHistory(
        read=project_observation(db, project, CHANGE_READ_SOURCE),
        entries=tuple(ChangeEntry(*row) for row in rows),
        loaded_from=loaded_from,
        as_of=as_of,
    )


def project_facts(
    project: GitLabProject,
    protection: BranchProtection | None = None,
    policies: SecurityPolicies | None = None,
    access: Mapping[str, ProjectObservation | None] | None = None,
) -> ProjectFacts:
    """``access`` holds the access-control observations by source (``ACCESS_SOURCES``)."""
    access = access or {}
    return ProjectFacts(
        gitlab_id=project.gitlab_id,
        path=project.path_with_namespace,
        visibility=project.visibility,
        state=project.state,
        last_seen_at=project.last_seen_at,
        last_error=project.last_error,
        archived=project.archived,
        default_branch=project.default_branch,
        protection=protection,
        security_policies=policies,
        approvals=access.get(APPROVALS_SOURCE),
        members=access.get(MEMBERS_SOURCE),
        access_tokens=access.get(ACCESS_TOKENS_SOURCE),
        two_factor=access.get(TWO_FACTOR_SOURCE),
    )


def github_observations(
    db: Session, project: GitLabProject
) -> dict[str, ProjectObservation | None]:
    """A GitHub repository's default-branch settings, security features and organization, as
    last read."""
    organization = db.scalars(
        select(GitLabGroup.id).where(
            GitLabGroup.instance_id == project.instance_id,
            GitLabGroup.gitlab_id == project.namespace_gitlab_id,
        )
    ).first()
    org_observation = (
        latest(db, project.instance_id, "group_id", organization, sources=(GITHUB_ORG_SOURCE,))
        if organization is not None
        else None
    )
    return {
        GITHUB_BRANCH_SOURCE: project_observation(db, project, GITHUB_BRANCH_SOURCE),
        GITHUB_SECURITY_SOURCE: project_observation(db, project, GITHUB_SECURITY_SOURCE),
        GITHUB_ORG_SOURCE: (
            ProjectObservation(
                observation_id=org_observation.id,
                status=org_observation.collection_status,
                collected_at=org_observation.collected_at,
                payload=org_observation.payload,
                error=org_observation.error,
            )
            if org_observation is not None
            else None
        ),
    }


def project_context(
    db: Session,
    project: GitLabProject,
    ancestry: Literal["complete", "incomplete"],
    collections: Mapping[str, CollectionContext],
    capabilities: Mapping[str, CapabilityStatus] | None = None,
    evaluated_at: datetime | None = None,
    provider: str = "gitlab",
    *,
    changes: bool = False,
    labels: bool = False,
) -> EvaluationContext:
    """What rules about ``project`` are judged from. Changes and label drift are read only when
    asked for, because they are read only for projects a rule of that kind is in force for.

    ``capabilities`` defaults to the instance's latest snapshot and ``evaluated_at`` to the
    database clock.
    """
    if evaluated_at is None:
        evaluated_at = db.execute(select(func.now())).scalar_one()
    if capabilities is None:
        capabilities = capability_statuses(db, project.instance_id)
    facts = project_facts(
        project,
        branch_protection(db, project),
        security_policies(db, project),
        {source: project_observation(db, project, source) for source in ACCESS_SOURCES},
    )
    if provider == "github":
        facts = replace(facts, provider=provider, github=github_observations(db, project))
    if changes:
        facts = replace(facts, changes=change_history(db, project, evaluated_at))
    if labels:
        facts = replace(facts, labels=label_facts(db, project, evaluated_at))
    return EvaluationContext(
        target=TargetRef(
            target_type="project", gitlab_id=project.gitlab_id, path=project.path_with_namespace
        ),
        project=facts,
        collections=collections,
        ancestry=ancestry,
        evaluated_at=evaluated_at,
        capabilities=capabilities,
    )


def evaluate_project(
    db: Session,
    project: GitLabProject,
    resolution: Resolution,
    collections: Mapping[str, CollectionContext],
    capabilities: Mapping[str, CapabilityStatus] | None = None,
    evaluated_at: datetime | None = None,
    provider: str = "gitlab",
) -> list[RuleResult]:
    """Results for every rule with an evaluator in the project's effective policies.

    Ordered by policy name, then by the rule's position in the policy. ``capabilities``
    defaults to the instance's latest snapshot and ``evaluated_at`` to the database clock.
    """
    target = resolution.target
    if target.target_type != "project" or target.target_id != project.gitlab_id:
        raise ValueError("the resolution is not for this project")
    in_force = []
    for resolved in sorted(resolution.policies, key=lambda entry: entry.name):
        policy = db.get(Policy, resolved.policy_id)
        if policy is None:  # versions are never deleted while assigned
            raise LookupError(f"policy version {resolved.policy_id} no longer exists")
        ref = PolicyRef(
            policy_id=policy.id,
            name=policy.name,
            version=policy.version,
            yaml_digest=policy.yaml_digest,
        )
        in_force.append((ref, parse_policy(policy.source_yaml).document.spec.rules))
    types = {rule.type for _, rules in in_force for rule in rules}
    context = project_context(
        db,
        project,
        resolution.ancestry,
        collections,
        capabilities,
        evaluated_at,
        provider,
        changes=bool(types & set(CHANGE_RULE_TYPES)),
        labels=LABELS_RULE_TYPE in types,
    )
    evaluators = PROVIDER_EVALUATORS[provider]
    results: list[RuleResult] = []
    for ref, rules in in_force:
        for rule in rules:
            evaluator = evaluators.get(rule.type)
            if evaluator is not None:
                results.append(evaluate_rule(evaluator, ref, rule, context))
    return results
