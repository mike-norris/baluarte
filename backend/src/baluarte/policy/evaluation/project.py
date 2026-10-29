"""Evaluating the policies in force for one project against the inventory. Read-only.

The caller resolves the project's policies (``baluarte.policy.assignments.resolve_project``) and
reads collection freshness once (``collection_contexts``), so a batch of projects shares one
freshness reading. Persisting the results is BAL-021.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import get_args

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte.db.models import GitLabProject, Policy
from baluarte.integrations.gitlab.freshness import report
from baluarte.integrations.gitlab.runs import RunKind
from baluarte.integrations.gitlab.settings import GitLabSettings
from baluarte.policy.assignments import Resolution
from baluarte.policy.evaluation.engine import (
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
EVALUATORS: Mapping[str, RuleEvaluator] = {VISIBILITY.rule_type: VISIBILITY}


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


def project_facts(project: GitLabProject) -> ProjectFacts:
    return ProjectFacts(
        gitlab_id=project.gitlab_id,
        path=project.path_with_namespace,
        visibility=project.visibility,
        state=project.state,
        last_seen_at=project.last_seen_at,
        last_error=project.last_error,
        archived=project.archived,
        default_branch=project.default_branch,
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

    Ordered by policy name, then by the rule's position in the policy. ``evaluated_at``
    defaults to the database clock.
    """
    target = resolution.target
    if target.target_type != "project" or target.target_id != project.gitlab_id:
        raise ValueError("the resolution is not for this project")
    if evaluated_at is None:
        evaluated_at = db.execute(select(func.now())).scalar_one()
    context = EvaluationContext(
        target=TargetRef(
            target_type="project", gitlab_id=project.gitlab_id, path=project.path_with_namespace
        ),
        project=project_facts(project),
        collections=collections,
        ancestry=resolution.ancestry,
        evaluated_at=evaluated_at,
        capabilities=capabilities or {},
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
