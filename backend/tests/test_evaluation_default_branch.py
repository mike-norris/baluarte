"""The protected default branch evaluator, without a database."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from baluarte.policy.evaluation.default_branch import DEFAULT_BRANCH_PROTECTED
from baluarte.policy.evaluation.engine import (
    BranchProtection,
    EvaluationContext,
    ProjectFacts,
    evaluate_rule,
)
from baluarte.policy.evaluation.model import (
    CapabilityStatus,
    CollectionContext,
    PolicyRef,
    TargetRef,
)
from baluarte.policy.evaluation.project import EVALUATORS
from baluarte.policy.schema import DefaultBranchProtectedRule, ProjectVisibilityRule

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
SEEN = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
POLICY = PolicyRef(policy_id=7, name="corporate-default", version=3, yaml_digest="ab" * 32)
TARGET = TargetRef(target_type="project", gitlab_id=100, path="platform/api")
REQUIRED = DefaultBranchProtectedRule(
    id="protected-default-branch", type="gitlab.branch.default_protected", required=True
)
OPTIONAL = REQUIRED.model_copy(update={"required": False})
SUPPORTED: dict[str, CapabilityStatus] = {"protected_branches.read": "supported"}


def read(protected: bool, branch: str = "main", status: str = "ok") -> BranchProtection:
    payload = (
        {"branch": branch, "protected": protected, "rules": [{"name": "ma*"}] if protected else []}
        if status == "ok"
        else None
    )
    return BranchProtection(
        observation_id=1,
        status=status,
        collected_at=SEEN,
        payload=payload,
        error=None if status == "ok" else "GitLab returned 502 for /projects/100",
    )


PROJECT = ProjectFacts(
    gitlab_id=100,
    path="platform/api",
    visibility="private",
    state="active",
    last_seen_at=SEEN,
    last_error=None,
    default_branch="main",
    protection=read(True),
)


def context(
    project: ProjectFacts | None = PROJECT,
    capabilities: dict[str, CapabilityStatus] | None = None,
    status: Any = "fresh",
) -> EvaluationContext:
    return EvaluationContext(
        target=TARGET,
        project=project,
        collections={
            "projects": CollectionContext(kind="projects", status=status, last_success_at=SEEN)
        },
        ancestry="complete",
        evaluated_at=NOW,
        capabilities=SUPPORTED if capabilities is None else capabilities,
    )


def test_is_registered_for_its_rule_type() -> None:
    assert EVALUATORS["gitlab.branch.default_protected"] is DEFAULT_BRANCH_PROTECTED


@pytest.mark.parametrize(
    ("rule", "protected", "outcome", "reason"),
    [
        (REQUIRED, True, "pass", "Default branch main is protected by ma*."),
        (
            REQUIRED,
            False,
            "fail",
            "Default branch main is not protected; the policy requires protection.",
        ),
        (
            OPTIONAL,
            True,
            "pass",
            "The policy does not require protection; default branch main is protected by ma*.",
        ),
        (
            OPTIONAL,
            False,
            "pass",
            "The policy does not require protection; default branch main is not protected.",
        ),
    ],
)
def test_pass_or_fail_on_known_protection(
    rule: DefaultBranchProtectedRule, protected: bool, outcome: str, reason: str
) -> None:
    result = evaluate_rule(
        DEFAULT_BRANCH_PROTECTED,
        POLICY,
        rule,
        context(replace(PROJECT, protection=read(protected))),
    )

    assert (result.outcome, result.reason) == (outcome, reason)
    assert result.expected == {"required": rule.required}
    assert result.observed["protected"] is protected
    assert result.observed_at == SEEN
    assert result.capability.status == "supported"
    assert result.source == "GET /projects/:id/protected_branches"


@pytest.mark.parametrize(
    ("project", "fragment"),
    [
        (replace(PROJECT, default_branch=None), "has no default branch (an empty repository)"),
        (replace(PROJECT, protection=None), "has not been read yet"),
        (
            replace(PROJECT, protection=read(True, status="forbidden")),
            "could not be read (forbidden)",
        ),
        (replace(PROJECT, protection=read(True, status="failed")), "GitLab returned 502"),
        (
            replace(PROJECT, default_branch="trunk"),
            "last read for branch main, but the default branch is now trunk",
        ),
    ],
)
@pytest.mark.parametrize("rule", [REQUIRED, OPTIONAL])
def test_protection_that_is_not_known_is_never_compliant(
    project: ProjectFacts, fragment: str, rule: DefaultBranchProtectedRule
) -> None:
    result = evaluate_rule(DEFAULT_BRANCH_PROTECTED, POLICY, rule, context(project))

    assert (result.outcome, result.unknown_reasons) == ("unknown", ("value_unknown",))
    assert fragment in result.reason
    assert result.observed is None and result.observed_at is None


def test_a_payload_without_a_protected_flag_is_unknown() -> None:
    odd = BranchProtection(
        observation_id=1, status="ok", collected_at=SEEN, payload={"branch": "main"}, error=None
    )

    result = evaluate_rule(
        DEFAULT_BRANCH_PROTECTED, POLICY, REQUIRED, context(replace(PROJECT, protection=odd))
    )

    assert result.outcome == "unknown"


@pytest.mark.parametrize(
    ("capabilities", "outcome", "reasons"),
    [
        ({}, "unknown", ("capability_unknown",)),
        ({"protected_branches.read": "unknown"}, "unknown", ("capability_unknown",)),
        ({"protected_branches.read": "unsupported"}, "unsupported", ()),
    ],
)
def test_the_capability_decides_before_the_value(
    capabilities: dict[str, CapabilityStatus], outcome: str, reasons: tuple[str, ...]
) -> None:
    result = evaluate_rule(DEFAULT_BRANCH_PROTECTED, POLICY, REQUIRED, context(None, capabilities))
    assert result.outcome == "unknown"  # a target outside the inventory outranks everything

    result = evaluate_rule(
        DEFAULT_BRANCH_PROTECTED, POLICY, REQUIRED, context(PROJECT, capabilities)
    )

    assert (result.outcome, result.unknown_reasons) == (outcome, reasons)
    assert result.observed is None
    assert result.capability.name == "protected_branches.read"


def test_doubt_about_the_project_outranks_an_unsupported_capability() -> None:
    unsupported: dict[str, CapabilityStatus] = {"protected_branches.read": "unsupported"}

    result = evaluate_rule(
        DEFAULT_BRANCH_PROTECTED, POLICY, REQUIRED, context(PROJECT, unsupported, "stale")
    )

    assert (result.outcome, result.unknown_reasons) == ("unknown", ("collection_stale",))


def test_rejects_other_rule_types() -> None:
    visibility = ProjectVisibilityRule(
        id="visibility", type="gitlab.project.visibility", allowed=["private"]
    )

    with pytest.raises(TypeError):
        DEFAULT_BRANCH_PROTECTED.expected(visibility)
    with pytest.raises(ValueError, match="cannot evaluate"):
        evaluate_rule(DEFAULT_BRANCH_PROTECTED, POLICY, visibility, context())
