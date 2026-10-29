"""Evaluating the policies in force for one project against the inventory. Read-only.

The caller resolves the project's policies (``baluarte.policy.assignments.resolve_project``) and
reads collection freshness (``collection_contexts``) and capabilities (``capability_statuses``)
once, so a batch of projects shares one reading. Persisting the results is BAL-021.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import get_args

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte.db.models import GitLabInstance, GitLabProject, Policy
from baluarte.evidence.observations import latest
from baluarte.integrations.gitlab.branches import PROTECTION_SOURCE
from baluarte.integrations.gitlab.freshness import report
from baluarte.integrations.gitlab.runs import RunKind
from baluarte.integrations.gitlab.settings import GitLabSettings
from baluarte.policy.assignments import Resolution
from baluarte.policy.evaluation.default_branch import DEFAULT_BRANCH_PROTECTED
from baluarte.policy.evaluation.engine import (
    BranchProtection,
    EvaluationContext,
    ProjectFacts,
    RuleEvaluator,
    evaluate_rule,
)
from baluarte.policy.evaluation.model import (
    CapabilityStatus,
    CollectionContext,
    PolicyRef,
    RuleResult,
    TargetRef,
)
from baluarte.policy.evaluation.visibility import VISIBILITY
from baluarte.policy.loader import parse_policy

# Rule types that can be evaluated; adding a rule type adds its evaluator here. Rules of other
# types are not evaluated (and produce no result) until their evaluator exists.
EVALUATORS: Mapping[str, RuleEvaluator] = {
    evaluator.rule_type: evaluator for evaluator in (VISIBILITY, DEFAULT_BRANCH_PROTECTED)
}

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


def project_facts(
    project: GitLabProject, protection: BranchProtection | None = None
) -> ProjectFacts:
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
    )


def evaluate_project(
    db: Session,
    project: GitLabProject,
    resolution: Resolution,
    collections: Mapping[str, CollectionContext],
    capabilities: Mapping[str, CapabilityStatus] | None = None,
    evaluated_at: datetime | None = None,
) -> list[RuleResult]:
    """Results for every rule with an evaluator in the project's effective policies.

    Ordered by policy name, then by the rule's position in the policy. ``capabilities``
    defaults to the instance's latest snapshot and ``evaluated_at`` to the database clock.
    """
    target = resolution.target
    if target.target_type != "project" or target.target_id != project.gitlab_id:
        raise ValueError("the resolution is not for this project")
    if evaluated_at is None:
        evaluated_at = db.execute(select(func.now())).scalar_one()
    if capabilities is None:
        capabilities = capability_statuses(db, project.instance_id)
    context = EvaluationContext(
        target=TargetRef(
            target_type="project", gitlab_id=project.gitlab_id, path=project.path_with_namespace
        ),
        project=project_facts(project, branch_protection(db, project)),
        collections=collections,
        ancestry=resolution.ancestry,
        evaluated_at=evaluated_at,
        capabilities=capabilities,
    )
    results: list[RuleResult] = []
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
        for rule in parse_policy(policy.source_yaml).document.spec.rules:
            evaluator = EVALUATORS.get(rule.type)
            if evaluator is not None:
                results.append(evaluate_rule(evaluator, ref, rule, context))
    return results
