"""The access-control and merge request approval evaluators, without a database."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from baluarte.integrations.gitlab.access import (
    ACCESS_TOKENS_SOURCE,
    APPROVALS_SOURCE,
    MEMBERS_SOURCE,
    TWO_FACTOR_SOURCE,
)
from baluarte.policy.evaluation.access import (
    ACCESS_TOKENS,
    MERGE_REQUEST_APPROVALS,
    PRIVILEGED_MEMBERS,
    TWO_FACTOR_REQUIRED,
    required_approvals,
)
from baluarte.policy.evaluation.engine import (
    EvaluationContext,
    ProjectFacts,
    ProjectObservation,
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
from baluarte.policy.evaluation.project import EVALUATORS
from baluarte.policy.schema import (
    AccessTokensRule,
    MergeRequestApprovalsRule,
    PrivilegedMembersRule,
    Rule,
    TwoFactorRequiredRule,
)

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
SEEN = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
POLICY = PolicyRef(policy_id=7, name="acme-default", version=3, yaml_digest="ab" * 32)
TARGET = TargetRef(target_type="project", gitlab_id=100, path="acme/api")
SUPPORTED: dict[str, CapabilityStatus] = {
    "merge_request_approvals.read": "supported",
    "groups.two_factor": "supported",
}


def observation(payload: dict[str, Any] | None, status: str = "ok") -> ProjectObservation:
    return ProjectObservation(
        observation_id=1,
        status=status,
        collected_at=SEEN,
        payload=payload if status == "ok" else None,
        error=None if status == "ok" else "the token may not read /projects/100/x (403)",
    )


PROJECT = ProjectFacts(
    gitlab_id=100,
    path="acme/api",
    visibility="private",
    state="active",
    last_seen_at=SEEN,
    last_error=None,
    default_branch="main",
)


def evaluate(
    evaluator: RuleEvaluator,
    rule: Rule,
    project: ProjectFacts,
    capabilities: dict[str, CapabilityStatus] | None = None,
) -> RuleResult:
    context = EvaluationContext(
        target=TARGET,
        project=project,
        collections={
            "projects": CollectionContext(kind="projects", status="fresh", last_success_at=SEEN)
        },
        ancestry="complete",
        evaluated_at=NOW,
        capabilities=SUPPORTED if capabilities is None else capabilities,
    )
    return evaluate_rule(evaluator, POLICY, rule, context)


@pytest.mark.parametrize(
    ("rule_type", "evaluator", "source"),
    [
        ("gitlab.project.merge_request_approvals", MERGE_REQUEST_APPROVALS, APPROVALS_SOURCE),
        ("gitlab.project.privileged_members", PRIVILEGED_MEMBERS, MEMBERS_SOURCE),
        ("gitlab.group.two_factor_required", TWO_FACTOR_REQUIRED, TWO_FACTOR_SOURCE),
        ("gitlab.project.access_tokens", ACCESS_TOKENS, ACCESS_TOKENS_SOURCE),
    ],
)
def test_each_is_registered_for_its_rule_type(
    rule_type: str, evaluator: RuleEvaluator, source: str
) -> None:
    assert EVALUATORS[rule_type] is evaluator
    assert (evaluator.collection, evaluator.source) == ("projects", source)


# Merge request approvals

APPROVALS = MergeRequestApprovalsRule(
    id="approvals",
    type="gitlab.project.merge_request_approvals",
    min_approvals=2,
    forbid_author_approval=True,
    forbid_committer_approval=True,
    reset_approvals_on_push=True,
    forbid_override=True,
)
GOOD_SETTINGS = {
    "merge_requests_author_approval": False,
    "merge_requests_disable_committers_approval": True,
    "reset_approvals_on_push": True,
    "disable_overriding_approvers_per_merge_request": True,
    "require_reauthentication_to_approve": None,
}


def approval_rule(
    required: int,
    rule_type: str = "regular",
    branches: list[str] | None = None,
    all_protected: bool | None = False,
) -> dict[str, Any]:
    return {
        "name": f"{rule_type}-{required}",
        "rule_type": rule_type,
        "approvals_required": required,
        "applies_to_all_protected_branches": all_protected,
        "protected_branches": [] if branches is None else branches,
    }


def approvals(
    rules: list[dict[str, Any]],
    settings: dict[str, Any] | None = None,
    protected: bool | None = True,
    branch: str = "main",
) -> ProjectFacts:
    payload = {
        "branch": branch,
        "branch_protected": protected,
        "settings": GOOD_SETTINGS if settings is None else settings,
        "rules": rules,
    }
    return replace(PROJECT, approvals=observation(payload))


@pytest.mark.parametrize(
    ("rules", "protected", "required"),
    [
        ([], True, 0),
        # No branches listed: every branch.
        ([approval_rule(2)], True, 2),
        ([approval_rule(1), approval_rule(3, "any_approver")], True, 3),
        ([approval_rule(4, branches=["develop"])], True, 0),
        ([approval_rule(4, branches=["ma*"])], True, 4),
        ([approval_rule(2, all_protected=True)], True, 2),
        ([approval_rule(2, all_protected=True)], False, 0),
        ([approval_rule(2, all_protected=True)], None, None),
        # Code owner and security report rules only apply to some changes.
        ([approval_rule(5, "code_owner"), approval_rule(5, "report_approver")], True, 0),
        ([approval_rule(1, "something_new")], True, None),
        ([approval_rule(1, all_protected=None)], True, None),
    ],
)
def test_required_approvals_follow_the_rules_that_cover_the_branch(
    rules: list[dict[str, Any]], protected: bool | None, required: int | None
) -> None:
    payload = approvals(rules, protected=protected).approvals
    assert payload is not None and payload.payload is not None
    assert required_approvals(payload.payload) == required


def test_approvals_pass() -> None:
    result = evaluate(MERGE_REQUEST_APPROVALS, APPROVALS, approvals([approval_rule(2)]))

    assert result.outcome == "pass"
    assert result.reason == (
        "Merge requests to main need 2 approvals, and the approval settings meet the policy."
    )
    assert result.expected == {
        "min_approvals": 2,
        "forbid_author_approval": True,
        "forbid_committer_approval": True,
        "reset_approvals_on_push": True,
        "forbid_override": True,
    }
    assert result.observed["rules"][0]["approvals_required"] == 2


def test_approvals_fail_on_too_few_approvals_and_each_setting() -> None:
    weak = {
        "merge_requests_author_approval": True,
        "merge_requests_disable_committers_approval": False,
        "reset_approvals_on_push": False,
        "disable_overriding_approvers_per_merge_request": False,
    }
    result = evaluate(
        MERGE_REQUEST_APPROVALS, APPROVALS, approvals([approval_rule(1)], settings=weak)
    )

    assert result.outcome == "fail"
    assert result.reason.startswith(
        "Approval settings fall short: merge requests to main need 1 approval, fewer than the 2 "
        "required; the policy requires that authors cannot approve"
    )
    for setting in weak:
        assert setting in result.reason


def test_flags_that_are_not_set_assert_nothing() -> None:
    rule = MergeRequestApprovalsRule(
        id="approvals", type="gitlab.project.merge_request_approvals", min_approvals=0
    )
    result = evaluate(
        MERGE_REQUEST_APPROVALS,
        rule,
        approvals([approval_rule(2, all_protected=True)], settings={}, protected=None),
    )

    assert (result.outcome, result.reason) == ("pass", "The approval settings meet the policy.")


@pytest.mark.parametrize(
    ("project", "reason"),
    [
        (
            approvals([approval_rule(2, all_protected=True)], protected=None),
            "Which approval rules cover default branch main is not known",
        ),
        (
            approvals([approval_rule(2)], settings={}),
            "GitLab did not report the approval setting merge_requests_author_approval, "
            "merge_requests_disable_committers_approval, reset_approvals_on_push, "
            "disable_overriding_approvers_per_merge_request.",
        ),
        (
            replace(PROJECT, approvals=None),
            "The merge request approval settings of project acme/api have not been read yet.",
        ),
        (
            replace(PROJECT, approvals=observation(None, "not_found")),
            "could not be read (not_found): the token may not read",
        ),
        (
            approvals([approval_rule(2)], branch="master"),
            "were last read for branch master, but the default branch is now main",
        ),
        (
            replace(approvals([approval_rule(2)]), default_branch=None),
            "has no default branch (an empty repository)",
        ),
    ],
)
def test_approvals_are_unknown_when_they_cannot_be_judged(
    project: ProjectFacts, reason: str
) -> None:
    result = evaluate(MERGE_REQUEST_APPROVALS, APPROVALS, project)

    assert result.outcome == "unknown"
    assert result.unknown_reasons == ("value_unknown",)
    assert reason in result.reason
    assert result.observed is None and result.observed_at is None


def test_approvals_need_the_approvals_capability() -> None:
    project = approvals([approval_rule(2)])

    assert evaluate(MERGE_REQUEST_APPROVALS, APPROVALS, project, {}).unknown_reasons == (
        "capability_unknown",
    )
    unsupported = {"merge_request_approvals.read": "unsupported"}
    assert evaluate(MERGE_REQUEST_APPROVALS, APPROVALS, project, unsupported).outcome == (
        "unsupported"
    )


# Privileged members

MEMBERS_RULE = PrivilegedMembersRule(
    id="privileged", type="gitlab.project.privileged_members", max_privileged=2
)


def member(username: str, level: int | None, state: str | None = "active") -> dict[str, Any]:
    return {"username": username, "access_level": level, "state": state, "expires_at": None}


def members(
    privileged: int, inactive: list[dict[str, Any]] | None = None, **extra: Any
) -> ProjectFacts:
    payload = {
        "total": privileged + 3,
        "by_access_level": {"30": 3, "40": privileged},
        "by_state": {"active": privileged + 3},
        "privileged": [member(f"maintainer-{n}", 40) for n in range(privileged)],
        "inactive": inactive or [],
    } | extra
    return replace(PROJECT, members=observation(payload))


def test_members_pass_within_the_limit() -> None:
    result = evaluate(PRIVILEGED_MEMBERS, MEMBERS_RULE, members(2))

    assert result.outcome == "pass"
    assert result.reason == "2 members hold Maintainer or Owner access (at most 2 allowed)."
    assert result.capability.status == "not_required"


def test_members_fail_over_the_limit() -> None:
    result = evaluate(PRIVILEGED_MEMBERS, MEMBERS_RULE, members(3))

    assert result.outcome == "fail"
    assert result.reason == (
        "3 members hold Maintainer or Owner access; the policy allows at most 2."
    )


def test_inactive_accounts_fail_only_when_forbidden() -> None:
    project = members(1, [member("left", 30, "blocked"), member("gone", 20, "deactivated")])
    strict = MEMBERS_RULE.model_copy(update={"forbid_inactive_accounts": True})

    assert evaluate(PRIVILEGED_MEMBERS, MEMBERS_RULE, project).outcome == "pass"
    result = evaluate(PRIVILEGED_MEMBERS, strict, project)
    assert result.outcome == "fail"
    assert result.reason == (
        "2 members whose account is not active (blocked, deactivated) still hold access."
    )


@pytest.mark.parametrize(
    ("project", "rule", "reason"),
    [
        (
            members(1, by_access_level={"40": 1, "unknown": 2}),
            MEMBERS_RULE,
            "GitLab did not report the access level of 2 members",
        ),
        (
            members(1, [member("odd", 30, None)], by_state={"active": 3, "unknown": 1}),
            MEMBERS_RULE.model_copy(update={"forbid_inactive_accounts": True}),
            "GitLab did not report the account state of 1 member.",
        ),
        (
            replace(PROJECT, members=observation(None, "forbidden")),
            MEMBERS_RULE,
            "The members of project acme/api could not be read (forbidden)",
        ),
        (replace(PROJECT, members=None), MEMBERS_RULE, "have not been read yet"),
    ],
)
def test_members_are_unknown_when_they_cannot_be_judged(
    project: ProjectFacts, rule: PrivilegedMembersRule, reason: str
) -> None:
    result = evaluate(PRIVILEGED_MEMBERS, rule, project)

    assert result.outcome == "unknown"
    assert reason in result.reason


def test_too_many_known_members_fail_even_with_unknown_levels() -> None:
    project = members(3, by_access_level={"40": 3, "unknown": 1})

    assert evaluate(PRIVILEGED_MEMBERS, MEMBERS_RULE, project).outcome == "fail"


# Two-factor authentication

TWO_FACTOR = TwoFactorRequiredRule(id="two-factor", type="gitlab.group.two_factor_required")


def tier(path: str, required: bool | None, grace: int | None = 48) -> dict[str, Any]:
    return {
        "path": path,
        "require_two_factor_authentication": required,
        "two_factor_grace_period": grace,
        "observation_id": 9,
    }


def two_factor(
    groups: list[dict[str, Any]],
    instance: dict[str, Any] | None = None,
    ancestry: str = "complete",
) -> ProjectFacts:
    payload = {"groups": groups, "ancestry": ancestry, "instance": instance}
    return replace(PROJECT, two_factor=observation(payload))


INSTANCE_OFF = {"require_two_factor_authentication": False, "two_factor_grace_period": 48}


def test_a_group_that_requires_two_factor_passes() -> None:
    result = evaluate(
        TWO_FACTOR_REQUIRED, TWO_FACTOR, two_factor([tier("acme", False), tier("acme/web", True)])
    )

    assert result.outcome == "pass"
    assert result.reason == (
        "Two-factor authentication is required by group acme/web, with a grace period of 48 hours."
    )


def test_the_instance_setting_counts() -> None:
    instance = {"require_two_factor_authentication": True, "two_factor_grace_period": 0}
    result = evaluate(TWO_FACTOR_REQUIRED, TWO_FACTOR, two_factor([], instance))

    assert result.outcome == "pass"
    assert "required by the instance, with a grace period of 0 hours" in result.reason


def test_nothing_requiring_two_factor_fails_only_when_everything_is_seen() -> None:
    seen = two_factor([tier("acme", False)], INSTANCE_OFF)
    result = evaluate(TWO_FACTOR_REQUIRED, TWO_FACTOR, seen)
    assert result.outcome == "fail"
    assert result.reason == (
        "Neither the groups above the project nor the instance require two-factor authentication."
    )

    # Without an administrator token the instance setting cannot be seen.
    unseen = evaluate(TWO_FACTOR_REQUIRED, TWO_FACTOR, two_factor([tier("acme", False)]))
    assert unseen.outcome == "unknown"
    assert "the instance (its setting needs an administrator token)" in unseen.reason


@pytest.mark.parametrize(
    ("project", "reason"),
    [
        (
            two_factor([tier("acme/web", False)], INSTANCE_OFF, ancestry="incomplete"),
            "a group above the project that the token cannot see",
        ),
        (two_factor([tier("acme", None)], INSTANCE_OFF), "group acme (setting not reported)"),
        (replace(PROJECT, two_factor=None), "two-factor settings of project acme/api have not"),
    ],
)
def test_two_factor_is_unknown_when_a_setting_cannot_be_seen(
    project: ProjectFacts, reason: str
) -> None:
    result = evaluate(TWO_FACTOR_REQUIRED, TWO_FACTOR, project)

    assert result.outcome == "unknown"
    assert reason in result.reason


def test_grace_period_limit() -> None:
    strict = TWO_FACTOR.model_copy(update={"max_grace_hours": 24})
    groups = [tier("acme", True, 72), tier("acme/web", True, 8)]

    assert evaluate(TWO_FACTOR_REQUIRED, strict, two_factor(groups)).outcome == "pass"
    long = evaluate(TWO_FACTOR_REQUIRED, strict, two_factor([tier("acme", True, 72)], INSTANCE_OFF))
    assert long.outcome == "fail"
    assert long.reason == (
        "Two-factor authentication is required by group acme, with a grace period of 72 hours; "
        "the policy allows at most 24."
    )
    # The instance might set a shorter grace period; that cannot be seen without an admin token.
    hidden = evaluate(TWO_FACTOR_REQUIRED, strict, two_factor([tier("acme", True, 72)]))
    assert hidden.outcome == "unknown"
    unreported = evaluate(TWO_FACTOR_REQUIRED, strict, two_factor([tier("acme", True, None)]))
    assert unreported.reason == "GitLab did not report the two-factor grace period."


def test_two_factor_needs_the_group_field() -> None:
    project = two_factor([tier("acme", True)])

    assert evaluate(TWO_FACTOR_REQUIRED, TWO_FACTOR, project, {}).unknown_reasons == (
        "capability_unknown",
    )


# Project access tokens

TOKENS = AccessTokensRule(
    id="tokens", type="gitlab.project.access_tokens", max_lifetime_days=90, max_unused_days=30
)


def token(
    name: str,
    created: str | None = "2026-10-01",
    expires: str | None = "2026-12-01",
    used: str | None = "2026-11-01",
) -> dict[str, Any]:
    return {
        "id": 1,
        "name": name,
        "scopes": ["read_api"],
        "access_level": 30,
        "created_on": created,
        "expires_at": expires,
        "last_used_on": used,
    }


def tokens(*entries: dict[str, Any], as_of: str | None = "2026-11-03") -> ProjectFacts:
    payload: dict[str, Any] = {"tokens": list(entries)}
    if entries and as_of is not None:
        payload["as_of"] = as_of
    return replace(PROJECT, access_tokens=observation(payload))


def test_tokens_pass() -> None:
    result = evaluate(ACCESS_TOKENS, TOKENS, tokens(token("deploy")))

    assert result.outcome == "pass"
    assert result.reason == (
        "1 active access token is within at most 90 days of lifetime and 30 days unused."
    )
    assert evaluate(ACCESS_TOKENS, TOKENS, tokens()).reason == (
        "The project has no active access tokens."
    )


@pytest.mark.parametrize(
    ("entry", "problem"),
    [
        (token("forever", expires=None), "Token forever never expires"),
        (token("long", expires="2027-06-01"), "Token long expires 243 days after it was created"),
        (token("idle", used="2026-09-01"), "Token idle has not been used for 63 days"),
        # Never used: counted from creation.
        (token("unused", created="2026-09-15", used=None), "Token unused has not been used for 49"),
    ],
)
def test_tokens_fail(entry: dict[str, Any], problem: str) -> None:
    result = evaluate(ACCESS_TOKENS, TOKENS, tokens(entry))

    assert result.outcome == "fail"
    assert result.reason.startswith(problem)
    assert result.reason.endswith(
        "the policy allows at most 90 days of lifetime and 30 days unused."
    )


def test_unused_days_are_only_checked_when_set() -> None:
    lifetime_only = TOKENS.model_copy(update={"max_unused_days": None})

    result = evaluate(ACCESS_TOKENS, lifetime_only, tokens(token("idle", used="2026-01-01")))

    assert result.outcome == "pass"


@pytest.mark.parametrize(
    ("project", "reason"),
    [
        (tokens(token("odd", created=None)), "did not report when every access token was created"),
        (tokens(token("deploy"), as_of=None), "date the access tokens were read was not recorded"),
        (
            replace(PROJECT, access_tokens=observation(None, "forbidden")),
            "The access tokens of project acme/api could not be read (forbidden)",
        ),
    ],
)
def test_tokens_are_unknown_when_they_cannot_be_judged(project: ProjectFacts, reason: str) -> None:
    result = evaluate(ACCESS_TOKENS, TOKENS, project)

    assert result.outcome == "unknown"
    assert reason in result.reason
