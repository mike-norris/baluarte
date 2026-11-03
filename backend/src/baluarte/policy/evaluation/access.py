"""Access-control and merge request approval rules, judged from the per-project observations that
``baluarte.integrations.gitlab.access`` records after project discovery.

- ``gitlab.project.merge_request_approvals``: the project's own approval rules and settings.
- ``gitlab.project.privileged_members``: members with Maintainer or Owner access, and members
  whose account is no longer active.
- ``gitlab.group.two_factor_required``: two-factor enforcement by a group above the project, or
  by the instance.
- ``gitlab.project.access_tokens``: the lifetime and use of the project's access tokens.

Each rule's value is the latest observation's payload, so a result is backed by exactly one
observation. The value is unknown, never assumed, when that observation is missing or could not
be read, and when the part a rule checks was not reported or cannot be seen (for example, a group
above the project the token cannot see might require two-factor authentication).
"""

from collections.abc import Callable
from datetime import date
from typing import Any

from baluarte.integrations.gitlab.access import (
    ACCESS_TOKENS_SOURCE,
    APPROVALS_SOURCE,
    MEMBERS_SOURCE,
    TWO_FACTOR_SOURCE,
)
from baluarte.integrations.gitlab.branches import matches
from baluarte.policy.evaluation.engine import ProjectFacts, ProjectObservation, RuleEvaluator
from baluarte.policy.schema import (
    AccessTokensRule,
    MergeRequestApprovalsRule,
    PrivilegedMembersRule,
    Rule,
    TwoFactorRequiredRule,
)

APPROVALS_CAPABILITY = "merge_request_approvals.read"
TWO_FACTOR_CAPABILITY = "groups.two_factor"
# Rule types that apply to every merge request they cover. Code owner and security report rules
# apply only when certain files or findings change, so they never count toward the minimum.
COUNTED_RULE_TYPES = ("regular", "any_approver")
_UNCOUNTED_RULE_TYPES = ("code_owner", "report_approver")
# Each approval flag a rule can set: the GitLab setting, the value it requires, and its wording.
_APPROVAL_FLAGS: tuple[tuple[str, str, bool, str], ...] = (
    ("forbid_author_approval", "merge_requests_author_approval", False, "authors cannot approve"),
    (
        "forbid_committer_approval",
        "merge_requests_disable_committers_approval",
        True,
        "committers cannot approve",
    ),
    ("reset_approvals_on_push", "reset_approvals_on_push", True, "a new push resets approvals"),
    (
        "forbid_override",
        "disable_overriding_approvers_per_merge_request",
        True,
        "approval rules cannot be changed per merge request",
    ),
)


def _ok(observation: ProjectObservation | None) -> dict[str, Any] | None:
    if observation is None or observation.status != "ok":
        return None
    return observation.payload


def _not_read(
    what: str, field: Callable[[ProjectFacts], ProjectObservation | None]
) -> Callable[[ProjectFacts], str]:
    def message(project: ProjectFacts) -> str:
        observation = field(project)
        if observation is None:
            return f"The {what} of project {project.path} have not been read yet."
        detail = f": {observation.error}" if observation.error else ""
        return (
            f"The {what} of project {project.path} could not be read "
            f"({observation.status}){detail}."
        )

    return message


def _rule[T](rule: Rule, kind: type[T]) -> T:
    if not isinstance(rule, kind):
        raise TypeError(f"not a {kind.__name__}: {rule.type}")
    return rule


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


# Merge request approvals ---------------------------------------------------------------------


def required_approvals(observed: dict[str, Any]) -> int | None:
    """The approvals every merge request to the default branch needs, or None when which rules
    cover it is not known."""
    branch = observed["branch"]
    protected = observed.get("branch_protected")
    required = 0
    for rule in observed.get("rules", []):
        kind = rule.get("rule_type")
        if kind in _UNCOUNTED_RULE_TYPES:
            continue
        needed = rule.get("approvals_required")
        branches = rule.get("protected_branches")
        all_protected = rule.get("applies_to_all_protected_branches")
        if kind not in COUNTED_RULE_TYPES or not isinstance(needed, int):
            return None
        if all_protected is True:
            if not isinstance(protected, bool):
                return None
            covers = protected
        elif all_protected is not False or not isinstance(branches, list):
            return None
        else:
            # No branches listed: the rule applies to every branch.
            covers = not branches or any(matches(name, branch) for name in branches)
        if covers:
            required = max(required, needed)
    return required


def _approvals(project: ProjectFacts) -> dict[str, Any] | None:
    payload = _ok(project.approvals)
    if project.default_branch is None or payload is None:
        return None
    if payload.get("branch") != project.default_branch:
        return None
    if not isinstance(payload.get("settings"), dict) or not isinstance(payload.get("rules"), list):
        return None
    return payload


def _approvals_expected(rule: Rule) -> dict[str, Any]:
    found = _rule(rule, MergeRequestApprovalsRule)
    return {
        "min_approvals": found.min_approvals,
        **{flag: getattr(found, flag) for flag, *_ in _APPROVAL_FLAGS},
    }


def _approvals_undecided(rule: Rule, observed: Any) -> str | None:
    found = _rule(rule, MergeRequestApprovalsRule)
    branch = observed["branch"]
    if found.min_approvals > 0 and required_approvals(observed) is None:
        return (
            f"Which approval rules cover default branch {branch} is not known: a rule's type, "
            "required approvals or branches were not reported, or whether the branch is "
            "protected could not be read."
        )
    missing = [
        setting
        for flag, setting, _, _ in _APPROVAL_FLAGS
        if getattr(found, flag) and not isinstance(observed["settings"].get(setting), bool)
    ]
    if missing:
        return f"GitLab did not report the approval setting {', '.join(missing)}."
    return None


def _approvals_judge(rule: Rule, observed: Any) -> tuple[bool, str]:
    found = _rule(rule, MergeRequestApprovalsRule)
    branch = observed["branch"]
    problems = []
    required = required_approvals(observed) if found.min_approvals > 0 else None
    if required is not None and required < found.min_approvals:
        problems.append(
            f"merge requests to {branch} need {_plural(required, 'approval', 'approvals')}, "
            f"fewer than the {found.min_approvals} required"
        )
    for flag, setting, wanted, wording in _APPROVAL_FLAGS:
        if getattr(found, flag) and observed["settings"][setting] is not wanted:
            problems.append(
                f"the policy requires that {wording} ({setting} is not {str(wanted).lower()})"
            )
    if problems:
        return False, "Approval settings fall short: " + "; ".join(problems) + "."
    if required is not None:
        return True, (
            f"Merge requests to {branch} need {_plural(required, 'approval', 'approvals')}, "
            "and the approval settings meet the policy."
        )
    return True, "The approval settings meet the policy."


def _approvals_unknown(project: ProjectFacts) -> str:
    if project.default_branch is None:
        return (
            f"Project {project.path} has no default branch (an empty repository), so no "
            "approval rule covers one."
        )
    payload = _ok(project.approvals)
    if payload is not None:
        return (
            f"Approvals of project {project.path} were last read for branch "
            f"{payload.get('branch')}, but the default branch is now {project.default_branch}; "
            "they are read again at the next project discovery."
        )
    return _not_read("merge request approval settings", lambda p: p.approvals)(project)


MERGE_REQUEST_APPROVALS = RuleEvaluator(
    rule_type="gitlab.project.merge_request_approvals",
    collection="projects",
    source=APPROVALS_SOURCE,
    capability=APPROVALS_CAPABILITY,
    expected=_approvals_expected,
    observe=_approvals,
    judge=_approvals_judge,
    value_unknown=_approvals_unknown,
    undecided=_approvals_undecided,
)


# Privileged members --------------------------------------------------------------------------


def _members(project: ProjectFacts) -> dict[str, Any] | None:
    payload = _ok(project.members)
    if payload is None or not isinstance(payload.get("privileged"), list):
        return None
    if not isinstance(payload.get("inactive"), list):
        return None
    return payload


def _members_expected(rule: Rule) -> dict[str, Any]:
    found = _rule(rule, PrivilegedMembersRule)
    return {
        "max_privileged": found.max_privileged,
        "forbid_inactive_accounts": found.forbid_inactive_accounts,
    }


def _inactive(observed: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry for entry in observed["inactive"] if isinstance(entry.get("state"), str)]


def _members_undecided(rule: Rule, observed: Any) -> str | None:
    found = _rule(rule, PrivilegedMembersRule)
    privileged = len(observed["privileged"])
    unknown_levels = observed.get("by_access_level", {}).get("unknown", 0)
    if privileged <= found.max_privileged and unknown_levels:
        return (
            f"GitLab did not report the access level of "
            f"{_plural(unknown_levels, 'member', 'members')}, so how many hold Maintainer or "
            "Owner access is not known."
        )
    if found.forbid_inactive_accounts and privileged <= found.max_privileged:
        unknown_states = observed.get("by_state", {}).get("unknown", 0)
        if unknown_states and not _inactive(observed):
            return (
                f"GitLab did not report the account state of "
                f"{_plural(unknown_states, 'member', 'members')}."
            )
    return None


def _members_judge(rule: Rule, observed: Any) -> tuple[bool, str]:
    found = _rule(rule, PrivilegedMembersRule)
    privileged = len(observed["privileged"])
    holders = _plural(privileged, "member holds", "members hold")
    if privileged > found.max_privileged:
        return False, (
            f"{holders} Maintainer or Owner access; the policy allows at most "
            f"{found.max_privileged}."
        )
    if found.forbid_inactive_accounts:
        inactive = _inactive(observed)
        if inactive:
            states = ", ".join(sorted({entry["state"] for entry in inactive}))
            return False, (
                f"{_plural(len(inactive), 'member', 'members')} whose account is not active "
                f"({states}) still {'holds' if len(inactive) == 1 else 'hold'} access."
            )
    return True, f"{holders} Maintainer or Owner access (at most {found.max_privileged} allowed)."


PRIVILEGED_MEMBERS = RuleEvaluator(
    rule_type="gitlab.project.privileged_members",
    collection="projects",
    source=MEMBERS_SOURCE,
    # Every edition lists members; a project that cannot be read is unknown on its own.
    capability=None,
    expected=_members_expected,
    observe=_members,
    judge=_members_judge,
    value_unknown=_not_read("members", lambda project: project.members),
    undecided=_members_undecided,
)


# Two-factor authentication -------------------------------------------------------------------


def _two_factor(project: ProjectFacts) -> dict[str, Any] | None:
    payload = _ok(project.two_factor)
    if payload is None or not isinstance(payload.get("groups"), list):
        return None
    return payload


def _enforcing(observed: dict[str, Any]) -> list[tuple[str, int | None]]:
    """Where two-factor authentication is required, with each grace period in hours."""
    found = [
        (f"group {group['path']}", group.get("two_factor_grace_period"))
        for group in observed["groups"]
        if group.get("require_two_factor_authentication") is True
    ]
    instance = observed.get("instance")
    if isinstance(instance, dict) and instance.get("require_two_factor_authentication") is True:
        found.append(("the instance", instance.get("two_factor_grace_period")))
    return found


def _hidden(observed: dict[str, Any]) -> list[str]:
    """What could also require two-factor authentication but cannot be seen."""
    hidden = []
    if observed.get("ancestry") != "complete":
        hidden.append("a group above the project that the token cannot see")
    unreported = [
        group["path"]
        for group in observed["groups"]
        if not isinstance(group.get("require_two_factor_authentication"), bool)
    ]
    if unreported:
        hidden.append(f"group {', '.join(unreported)} (setting not reported)")
    instance = observed.get("instance")
    if not isinstance(instance, dict) or not isinstance(
        instance.get("require_two_factor_authentication"), bool
    ):
        hidden.append("the instance (its setting needs an administrator token)")
    return hidden


def _two_factor_expected(rule: Rule) -> dict[str, Any]:
    return {"max_grace_hours": _rule(rule, TwoFactorRequiredRule).max_grace_hours}


def _shortest_grace(enforcing: list[tuple[str, int | None]]) -> int | None:
    graces = [grace for _, grace in enforcing if isinstance(grace, int)]
    return min(graces) if graces else None


def _two_factor_undecided(rule: Rule, observed: Any) -> str | None:
    found = _rule(rule, TwoFactorRequiredRule)
    enforcing = _enforcing(observed)
    hidden = _hidden(observed)
    if not enforcing:
        if hidden:
            return (
                "No group above the project that Baluarte can see requires two-factor "
                f"authentication, and it cannot see whether {'; or '.join(hidden)} does."
            )
        return None
    if found.max_grace_hours is None:
        return None
    grace = _shortest_grace(enforcing)
    if grace is None:
        return "GitLab did not report the two-factor grace period."
    if grace > found.max_grace_hours and hidden:
        # A setting that cannot be seen may enforce a shorter grace period.
        return (
            f"The shortest grace period Baluarte can see is {grace} hours, longer than the "
            f"{found.max_grace_hours} allowed, and it cannot see whether {'; or '.join(hidden)} "
            "sets a shorter one."
        )
    return None


def _two_factor_judge(rule: Rule, observed: Any) -> tuple[bool, str]:
    found = _rule(rule, TwoFactorRequiredRule)
    enforcing = _enforcing(observed)
    if not enforcing:
        return False, (
            "Neither the groups above the project nor the instance require two-factor "
            "authentication."
        )
    where = ", ".join(name for name, _ in enforcing)
    grace = _shortest_grace(enforcing)
    if found.max_grace_hours is not None and grace is not None and grace > found.max_grace_hours:
        return False, (
            f"Two-factor authentication is required by {where}, with a grace period of {grace} "
            f"hours; the policy allows at most {found.max_grace_hours}."
        )
    suffix = f", with a grace period of {grace} hours" if grace is not None else ""
    return True, f"Two-factor authentication is required by {where}{suffix}."


TWO_FACTOR_REQUIRED = RuleEvaluator(
    rule_type="gitlab.group.two_factor_required",
    collection="projects",
    source=TWO_FACTOR_SOURCE,
    capability=TWO_FACTOR_CAPABILITY,
    expected=_two_factor_expected,
    observe=_two_factor,
    judge=_two_factor_judge,
    value_unknown=_not_read("two-factor settings", lambda project: project.two_factor),
    undecided=_two_factor_undecided,
)


# Project access tokens -----------------------------------------------------------------------


def _tokens(project: ProjectFacts) -> dict[str, Any] | None:
    payload = _ok(project.access_tokens)
    if payload is None or not isinstance(payload.get("tokens"), list):
        return None
    return payload


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _tokens_expected(rule: Rule) -> dict[str, Any]:
    found = _rule(rule, AccessTokensRule)
    return {"max_lifetime_days": found.max_lifetime_days, "max_unused_days": found.max_unused_days}


def _tokens_undecided(rule: Rule, observed: Any) -> str | None:
    found = _rule(rule, AccessTokensRule)
    tokens = observed["tokens"]
    if any(_date(token.get("created_on")) is None for token in tokens):
        return "GitLab did not report when every access token was created."
    if found.max_unused_days is not None and tokens and _date(observed.get("as_of")) is None:
        return "The date the access tokens were read was not recorded."
    return None


def _token_problem(token: dict[str, Any], rule: AccessTokensRule, as_of: date | None) -> str | None:
    name = token.get("name") or f"#{token.get('id')}"
    created = _date(token["created_on"])
    expires = _date(token.get("expires_at"))
    if created is None:  # excluded by ``undecided``
        return None
    if expires is None:
        return f"token {name} never expires"
    lifetime = (expires - created).days
    if lifetime > rule.max_lifetime_days:
        return f"token {name} expires {lifetime} days after it was created"
    if rule.max_unused_days is not None and as_of is not None:
        used = _date(token.get("last_used_on")) or created
        idle = (as_of - used).days
        if idle > rule.max_unused_days:
            return f"token {name} has not been used for {idle} days"
    return None


def _tokens_judge(rule: Rule, observed: Any) -> tuple[bool, str]:
    found = _rule(rule, AccessTokensRule)
    tokens = observed["tokens"]
    as_of = _date(observed.get("as_of"))
    problems = [
        problem for token in tokens if (problem := _token_problem(token, found, as_of)) is not None
    ]
    limits = f"at most {found.max_lifetime_days} days of lifetime"
    if found.max_unused_days is not None:
        limits += f" and {found.max_unused_days} days unused"
    if problems:
        text = "; ".join(problems)
        return False, f"{text[0].upper()}{text[1:]}; the policy allows {limits}."
    if not tokens:
        return True, "The project has no active access tokens."
    count = _plural(len(tokens), "active access token is", "active access tokens are")
    return True, f"{count} within {limits}."


ACCESS_TOKENS = RuleEvaluator(
    rule_type="gitlab.project.access_tokens",
    collection="projects",
    source=ACCESS_TOKENS_SOURCE,
    # Every edition has project access tokens; reading them needs the Maintainer role, so a
    # project the token cannot read is unknown on its own.
    capability=None,
    expected=_tokens_expected,
    observe=_tokens,
    judge=_tokens_judge,
    value_unknown=_not_read("access tokens", lambda project: project.access_tokens),
    undecided=_tokens_undecided,
)

ACCESS_EVALUATORS = (
    MERGE_REQUEST_APPROVALS,
    PRIVILEGED_MEMBERS,
    TWO_FACTOR_REQUIRED,
    ACCESS_TOKENS,
)
# Each rule's observation source; the value judged is always that observation's payload.
ACCESS_SOURCES = (APPROVALS_SOURCE, MEMBERS_SOURCE, TWO_FACTOR_SOURCE, ACCESS_TOKENS_SOURCE)
