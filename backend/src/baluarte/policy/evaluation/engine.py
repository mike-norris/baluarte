"""Deciding one rule for one target, the same way for every rule type.

A rule type contributes a ``RuleEvaluator``: where its value comes from, how to read it from the
target, and how to judge it. Everything that makes a value untrustworthy is checked here, once,
before any evaluator judges a value:

1. Reasons that apply to the whole target or its collection (see ``UNKNOWN_PRECEDENCE``) make
   the result ``unknown``, whatever the value says.
2. Otherwise a capability the instance does not support makes it ``unsupported``.
3. Otherwise an undetected capability, or a value GitLab did not report, makes it ``unknown``.
4. Only then does the evaluator decide ``pass`` or ``fail``.

Evaluation is pure: it reads only the context it is given.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from baluarte.policy.evaluation.model import (
    UNKNOWN_PRECEDENCE,
    CapabilityContext,
    CapabilityStatus,
    CollectionContext,
    Outcome,
    PolicyRef,
    RuleResult,
    TargetRef,
    UnknownReason,
)
from baluarte.policy.schema import Rule

# Reasons that concern the target or its data as a whole; they outrank ``unsupported``.
_TARGET_REASONS: frozenset[UnknownReason] = frozenset(
    UNKNOWN_PRECEDENCE[: UNKNOWN_PRECEDENCE.index("ancestry_incomplete") + 1]
)
_COLLECTION_REASONS: dict[str, UnknownReason] = {
    "not_configured": "collection_not_configured",
    "failed": "collection_failed",
    "never": "collection_never",
    "stale": "collection_stale",
}


@dataclass(frozen=True)
class BranchProtection:
    """The latest default-branch protection observation of a project."""

    observation_id: int
    # The observation's collection status: "ok", or why it could not be read.
    status: str
    collected_at: datetime
    # The payload (branch, protected, rules) when ``status`` is "ok".
    payload: dict[str, Any] | None
    error: str | None


@dataclass(frozen=True)
class ProjectFacts:
    """A project as recorded in the inventory."""

    gitlab_id: int
    path: str
    visibility: str | None
    state: str
    last_seen_at: datetime
    last_error: str | None
    archived: bool = False
    default_branch: str | None = None
    # None when the project's branch protection has never been read.
    protection: BranchProtection | None = None


@dataclass(frozen=True)
class EvaluationContext:
    target: TargetRef
    # None when the target is not in the inventory.
    project: ProjectFacts | None
    # Freshness of each collection kind ("projects", "groups", "capabilities").
    collections: Mapping[str, CollectionContext]
    ancestry: Literal["complete", "incomplete"]
    evaluated_at: datetime
    # Capability probe name to status; a probe that is absent counts as unknown.
    capabilities: Mapping[str, CapabilityStatus] = field(default_factory=dict)


@dataclass(frozen=True)
class RuleEvaluator:
    rule_type: str
    # The collection kind the value comes from, and the column it is read from.
    collection: str
    source: str
    # The capability probe the rule depends on, or None.
    capability: str | None
    expected: Callable[[Rule], dict[str, Any]]
    # The current value, or None when GitLab did not report one.
    observe: Callable[[ProjectFacts], Any]
    # (passed, reason) for a known value.
    judge: Callable[[Rule, Any], tuple[bool, str]]
    # Why the value is None, for a target that is otherwise fine.
    value_unknown: Callable[[ProjectFacts], str]


def _collection(context: EvaluationContext, kind: str) -> CollectionContext:
    found = context.collections.get(kind)
    if found is not None:
        return found
    return CollectionContext(kind=kind, status="never", last_success_at=None)


def _when(moment: datetime | None) -> str:
    return "never" if moment is None else moment.strftime("%Y-%m-%d %H:%M UTC")


def _message(
    reason: UnknownReason,
    context: EvaluationContext,
    collection: CollectionContext,
    evaluator: RuleEvaluator,
) -> str:
    path = context.target.path
    if reason == "value_unknown" and context.project is not None:
        return evaluator.value_unknown(context.project)
    messages: dict[UnknownReason, str] = {
        "target_not_in_inventory": f"Project {path} is not in the inventory.",
        "target_missing": (
            f"Project {path} was not found by the last discovery: it was deleted, or the "
            "GitLab token can no longer see it."
        ),
        "target_inaccessible": f"GitLab denied access to project {path} at the last discovery.",
        "collection_not_configured": "GitLab is not configured, so nothing has been collected.",
        "collection_failed": (
            f"The latest {collection.kind} collection failed, so the inventory may be out of date."
        ),
        "collection_never": f"No {collection.kind} collection has completed yet.",
        "collection_stale": (
            f"The {collection.kind} inventory is stale: the last successful collection was "
            f"{_when(collection.last_success_at)}."
        ),
        "target_collection_error": f"The last collection could not confirm project {path}.",
        "ancestry_incomplete": (
            f"Some groups above project {path} could not be followed, so a nearer policy "
            "version may apply."
        ),
        "capability_unknown": (
            f"Whether this GitLab instance supports {evaluator.capability} has not been detected."
        ),
    }
    return messages[reason]


def unknown_reasons(
    context: EvaluationContext, evaluator: RuleEvaluator
) -> tuple[UnknownReason, ...]:
    """Every reason the rule cannot be decided, most fundamental first."""
    found: set[UnknownReason] = set()
    project = context.project
    if project is None:
        found.add("target_not_in_inventory")
    elif project.state == "missing":
        found.add("target_missing")
    elif project.state == "inaccessible":
        found.add("target_inaccessible")
    collection_reason = _COLLECTION_REASONS.get(_collection(context, evaluator.collection).status)
    if collection_reason is not None:
        found.add(collection_reason)
    if project is not None and project.last_error is not None:
        found.add("target_collection_error")
    if context.ancestry == "incomplete":
        found.add("ancestry_incomplete")
    if evaluator.capability is not None and _capability(context, evaluator) == "unknown":
        found.add("capability_unknown")
    if project is not None and evaluator.observe(project) is None:
        found.add("value_unknown")
    return tuple(reason for reason in UNKNOWN_PRECEDENCE if reason in found)


def _capability(context: EvaluationContext, evaluator: RuleEvaluator) -> CapabilityStatus:
    if evaluator.capability is None:
        return "not_required"
    return context.capabilities.get(evaluator.capability, "unknown")


def evaluate_rule(
    evaluator: RuleEvaluator, policy: PolicyRef, rule: Rule, context: EvaluationContext
) -> RuleResult:
    if rule.type != evaluator.rule_type:
        raise ValueError(f"{evaluator.rule_type} cannot evaluate a {rule.type} rule")
    collection = _collection(context, evaluator.collection)
    capability = CapabilityContext(
        name=evaluator.capability, status=_capability(context, evaluator)
    )
    reasons = unknown_reasons(context, evaluator)
    outcome: Outcome
    observed: Any = None
    observed_at: datetime | None = None
    if reasons and (reasons[0] in _TARGET_REASONS or capability.status != "unsupported"):
        outcome = "unknown"
        reason = _message(reasons[0], context, collection, evaluator)
    elif capability.status == "unsupported":
        outcome = "unsupported"
        reasons = ()
        reason = f"This GitLab instance does not support {evaluator.capability}."
    elif context.project is None:  # always reported above; narrows the type
        raise AssertionError("a target outside the inventory is always unknown")
    else:
        project = context.project
        observed = evaluator.observe(project)
        observed_at = project.last_seen_at
        passed, reason = evaluator.judge(rule, observed)
        outcome = "pass" if passed else "fail"
    return RuleResult(
        policy=policy,
        rule_id=rule.id,
        rule_type=rule.type,
        severity=rule.severity,
        target=context.target,
        outcome=outcome,
        expected=evaluator.expected(rule),
        observed=observed,
        source=evaluator.source,
        observed_at=observed_at,
        evaluated_at=context.evaluated_at,
        reason=reason,
        unknown_reasons=reasons,
        ancestry=context.ancestry,
        capability=capability,
        collection=collection,
    )
