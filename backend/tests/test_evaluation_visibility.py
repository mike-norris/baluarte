"""The visibility evaluator and the shared rule engine, without a database."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from baluarte.policy.evaluation.engine import (
    EvaluationContext,
    ProjectFacts,
    RuleEvaluator,
    evaluate_rule,
)
from baluarte.policy.evaluation.model import (
    UNKNOWN_PRECEDENCE,
    CapabilityStatus,
    CollectionContext,
    PolicyRef,
    TargetRef,
)
from baluarte.policy.evaluation.visibility import VISIBILITY
from baluarte.policy.schema import DefaultBranchProtectedRule, ProjectVisibilityRule

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
SEEN = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
LAST_SUCCESS = datetime(2026, 10, 28, 6, 0, tzinfo=UTC)
POLICY = PolicyRef(policy_id=7, name="corporate-default", version=3, yaml_digest="ab" * 32)
TARGET = TargetRef(target_type="project", gitlab_id=100, path="platform/api")
PRIVATE_ONLY = ProjectVisibilityRule(
    id="project-visibility", type="gitlab.project.visibility", allowed=["private"]
)
NOT_PUBLIC = ProjectVisibilityRule(
    id="not-public",
    type="gitlab.project.visibility",
    allowed=["private", "internal"],
    severity="high",
)
PROJECT = ProjectFacts(
    gitlab_id=100,
    path="platform/api",
    visibility="private",
    state="active",
    last_seen_at=SEEN,
    last_error=None,
)


def collection(status: Any, kind: str = "projects") -> CollectionContext:
    return CollectionContext(
        kind=kind, status=status, last_success_at=None if status == "never" else LAST_SUCCESS
    )


def context(
    project: ProjectFacts | None = PROJECT,
    status: Any = "fresh",
    ancestry: Any = "complete",
    capabilities: dict[str, CapabilityStatus] | None = None,
) -> EvaluationContext:
    return EvaluationContext(
        target=TARGET,
        project=project,
        collections={"projects": collection(status)},
        ancestry=ancestry,
        evaluated_at=NOW,
        capabilities=capabilities or {},
    )


@pytest.mark.parametrize(
    ("rule", "visibility", "outcome"),
    [
        (PRIVATE_ONLY, "private", "pass"),
        (PRIVATE_ONLY, "internal", "fail"),
        (PRIVATE_ONLY, "public", "fail"),
        (NOT_PUBLIC, "private", "pass"),
        (NOT_PUBLIC, "internal", "pass"),
        (NOT_PUBLIC, "public", "fail"),
    ],
)
def test_pass_or_fail_on_known_visibility(
    rule: ProjectVisibilityRule, visibility: str, outcome: str
) -> None:
    result = evaluate_rule(
        VISIBILITY, POLICY, rule, context(replace(PROJECT, visibility=visibility))
    )

    assert result.outcome == outcome
    assert result.observed == visibility
    assert result.observed_at == SEEN
    assert result.expected == {"allowed": list(rule.allowed)}
    assert result.unknown_reasons == ()


def test_a_result_records_everything_an_auditor_needs() -> None:
    result = evaluate_rule(
        VISIBILITY, POLICY, NOT_PUBLIC, context(replace(PROJECT, visibility="public"))
    )

    assert result.model_dump() == {
        "policy": POLICY.model_dump(),
        "rule_id": "not-public",
        "rule_type": "gitlab.project.visibility",
        "severity": "high",
        "target": {"target_type": "project", "gitlab_id": 100, "path": "platform/api"},
        "outcome": "fail",
        "expected": {"allowed": ["private", "internal"]},
        "observed": "public",
        "source": "gitlab_project.visibility",
        "observed_at": SEEN,
        "evaluated_at": NOW,
        "reason": "Visibility is public; the policy allows only private, internal.",
        "unknown_reasons": (),
        "ancestry": "complete",
        "capability": {"name": None, "status": "not_required"},
        "collection": {"kind": "projects", "status": "fresh", "last_success_at": LAST_SUCCESS},
    }


def test_pass_reason_names_the_value_and_the_allowed_set() -> None:
    result = evaluate_rule(VISIBILITY, POLICY, NOT_PUBLIC, context())

    assert result.reason == "Visibility is private, which is allowed (private, internal)."


def test_archived_projects_are_evaluated_like_any_other() -> None:
    result = evaluate_rule(
        VISIBILITY,
        POLICY,
        PRIVATE_ONLY,
        context(replace(PROJECT, archived=True, visibility="public")),
    )

    assert result.outcome == "fail"


@pytest.mark.parametrize(
    ("ctx", "reason", "message"),
    [
        (
            context(project=None),
            "target_not_in_inventory",
            "Project platform/api is not in the inventory.",
        ),
        (
            context(replace(PROJECT, state="missing")),
            "target_missing",
            "Project platform/api was not found by the last discovery: it was deleted, or the "
            "GitLab token can no longer see it.",
        ),
        (
            context(replace(PROJECT, state="inaccessible")),
            "target_inaccessible",
            "GitLab denied access to project platform/api at the last discovery.",
        ),
        (
            context(status="not_configured"),
            "collection_not_configured",
            "GitLab is not configured, so nothing has been collected.",
        ),
        (
            context(status="failed"),
            "collection_failed",
            "The latest projects collection failed, so the inventory may be out of date.",
        ),
        (context(status="never"), "collection_never", "No projects collection has completed yet."),
        (
            context(status="stale"),
            "collection_stale",
            "The projects inventory is stale: the last successful collection was "
            "2026-10-28 06:00 UTC.",
        ),
        (
            context(replace(PROJECT, last_error="GitLab returned 500 for /projects/100")),
            "target_collection_error",
            "The last collection could not confirm project platform/api.",
        ),
        (
            context(ancestry="incomplete"),
            "ancestry_incomplete",
            "Some groups above project platform/api could not be followed, so a nearer policy "
            "version may apply.",
        ),
        (
            context(replace(PROJECT, visibility=None)),
            "value_unknown",
            "GitLab did not report a visibility for project platform/api.",
        ),
    ],
)
def test_each_unknown_reason(ctx: EvaluationContext, reason: str, message: str) -> None:
    # A value that would pass on its own is still unknown when it cannot be trusted.
    result = evaluate_rule(VISIBILITY, POLICY, NOT_PUBLIC, ctx)

    assert result.outcome == "unknown"
    assert result.unknown_reasons == (reason,)
    assert result.reason == message
    assert result.observed is None
    assert result.observed_at is None


def test_unknown_never_hides_a_failing_value_as_a_fail_or_pass() -> None:
    public = replace(PROJECT, visibility="public")

    for ctx in (context(public, status="stale"), context(public, ancestry="incomplete")):
        assert evaluate_rule(VISIBILITY, POLICY, PRIVATE_ONLY, ctx).outcome == "unknown"


@pytest.mark.parametrize(("status", "outcome"), [("fresh", "pass"), ("partial", "pass")])
def test_a_partial_collection_still_decides_confirmed_projects(status: str, outcome: str) -> None:
    assert evaluate_rule(VISIBILITY, POLICY, NOT_PUBLIC, context(status=status)).outcome == outcome


def test_a_partial_collection_leaves_unconfirmed_projects_unknown() -> None:
    unconfirmed = replace(PROJECT, last_error="could not confirm: GitLab returned 502")

    result = evaluate_rule(VISIBILITY, POLICY, NOT_PUBLIC, context(unconfirmed, status="partial"))

    assert result.unknown_reasons == ("target_collection_error",)


def test_a_missing_collection_kind_counts_as_never_collected() -> None:
    ctx = replace(context(), collections={})

    result = evaluate_rule(VISIBILITY, POLICY, NOT_PUBLIC, ctx)

    assert result.unknown_reasons == ("collection_never",)
    assert result.collection == CollectionContext(
        kind="projects", status="never", last_success_at=None
    )


def test_every_reason_is_kept_in_precedence_order() -> None:
    worst = replace(PROJECT, state="missing", visibility=None, last_error="GitLab returned 404")

    result = evaluate_rule(
        VISIBILITY, POLICY, NOT_PUBLIC, context(worst, status="stale", ancestry="incomplete")
    )

    assert result.unknown_reasons == (
        "target_missing",
        "collection_stale",
        "target_collection_error",
        "ancestry_incomplete",
        "value_unknown",
    )
    assert result.reason.startswith("Project platform/api was not found")


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("target_not_in_inventory", "collection_failed"),
        ("target_inaccessible", "collection_failed"),
        ("collection_failed", "ancestry_incomplete"),
        ("collection_stale", "target_collection_error"),
        ("target_collection_error", "ancestry_incomplete"),
        ("ancestry_incomplete", "value_unknown"),
    ],
)
def test_precedence(first: str, second: str) -> None:
    assert UNKNOWN_PRECEDENCE.index(first) < UNKNOWN_PRECEDENCE.index(second)  # type: ignore[arg-type]


def test_visibility_needs_no_capability_and_is_never_unsupported() -> None:
    unsupported: dict[str, CapabilityStatus] = {"protected_branches": "unsupported"}

    result = evaluate_rule(VISIBILITY, POLICY, NOT_PUBLIC, context(capabilities=unsupported))

    assert result.outcome == "pass"
    assert result.capability.model_dump() == {"name": None, "status": "not_required"}


def test_the_evaluator_rejects_another_rule_type() -> None:
    rule = DefaultBranchProtectedRule(
        id="branch", type="gitlab.branch.default_protected", required=True
    )

    with pytest.raises(ValueError, match="cannot evaluate"):
        evaluate_rule(VISIBILITY, POLICY, rule, context())


# A stand-in for a rule type that depends on a capability probe (as BAL-020's will).
NEEDS_PROBE = RuleEvaluator(
    rule_type="gitlab.project.visibility",
    collection="projects",
    source="gitlab_project.visibility",
    capability="example_probe",
    expected=VISIBILITY.expected,
    observe=VISIBILITY.observe,
    judge=VISIBILITY.judge,
    value_unknown=VISIBILITY.value_unknown,
)


@pytest.mark.parametrize(
    ("status", "outcome", "reasons"),
    [
        ("supported", "pass", ()),
        ("unknown", "unknown", ("capability_unknown",)),
        (None, "unknown", ("capability_unknown",)),
        ("unsupported", "unsupported", ()),
    ],
)
def test_capability_handling_for_rule_types_that_need_one(
    status: CapabilityStatus | None, outcome: str, reasons: tuple[str, ...]
) -> None:
    capabilities = {} if status is None else {"example_probe": status}

    result = evaluate_rule(NEEDS_PROBE, POLICY, NOT_PUBLIC, context(capabilities=capabilities))

    assert (result.outcome, result.unknown_reasons) == (outcome, reasons)
    assert result.capability.status == (status or "unknown")


def test_unsupported_outranks_a_value_gitlab_did_not_report() -> None:
    ctx = context(replace(PROJECT, visibility=None), capabilities={"example_probe": "unsupported"})

    result = evaluate_rule(NEEDS_PROBE, POLICY, NOT_PUBLIC, ctx)

    assert result.outcome == "unsupported"
    assert result.reason == "This GitLab instance does not support example_probe."
    assert result.observed is None


def test_target_and_collection_problems_outrank_unsupported() -> None:
    ctx = context(status="stale", capabilities={"example_probe": "unsupported"})

    result = evaluate_rule(NEEDS_PROBE, POLICY, NOT_PUBLIC, ctx)

    assert result.outcome == "unknown"
    assert result.unknown_reasons == ("collection_stale",)
