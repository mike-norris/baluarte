"""``gitlab.branch.default_protected``: the project's default branch must be protected.

The value comes from the project's latest default-branch protection observation, read during
project discovery (``baluarte.integrations.gitlab.branches``), so the rule shares the
``projects`` collection's freshness. It depends on the ``protected_branches.read`` capability.

The value is unknown, never assumed, when:

- the project has no default branch (an empty repository): there is no branch to check, and
  GitLab's default protection for the first push is set elsewhere and not read;
- its protection has never been read;
- the last read failed or was denied;
- the last read was for a branch that is no longer the default.

With ``required: false`` the rule asserts nothing: it passes whenever the protection is known,
recording what was observed, and is still ``unknown`` when it is not.
"""

from typing import Any

from baluarte.integrations.gitlab.branches import PROTECTION_SOURCE
from baluarte.policy.evaluation.engine import ProjectFacts, RuleEvaluator
from baluarte.policy.schema import DefaultBranchProtectedRule, Rule

CAPABILITY = "protected_branches.read"


def _rule(rule: Rule) -> DefaultBranchProtectedRule:
    if not isinstance(rule, DefaultBranchProtectedRule):
        raise TypeError(f"not a default-branch protection rule: {rule.type}")
    return rule


def _expected(rule: Rule) -> dict[str, Any]:
    return {"required": _rule(rule).required}


def _observe(project: ProjectFacts) -> dict[str, Any] | None:
    protection = project.protection
    if project.default_branch is None or protection is None or protection.status != "ok":
        return None
    payload = protection.payload
    if payload is None or payload.get("branch") != project.default_branch:
        return None
    if not isinstance(payload.get("protected"), bool):
        return None
    return payload


def _rules(observed: dict[str, Any]) -> str:
    names = [rule["name"] for rule in observed.get("rules", []) if isinstance(rule, dict)]
    return ", ".join(names)


def _judge(rule: Rule, observed: Any) -> tuple[bool, str]:
    branch = observed["branch"]
    protected = observed["protected"]
    state = f"protected by {_rules(observed)}" if protected else "not protected"
    if not _rule(rule).required:
        return True, f"The policy does not require protection; default branch {branch} is {state}."
    if protected:
        return True, f"Default branch {branch} is {state}."
    return False, f"Default branch {branch} is not protected; the policy requires protection."


def _value_unknown(project: ProjectFacts) -> str:
    path = project.path
    protection = project.protection
    if project.default_branch is None:
        return (
            f"Project {path} has no default branch (an empty repository), so there is no branch "
            "to check."
        )
    if protection is None:
        return f"The default-branch protection of project {path} has not been read yet."
    if protection.status != "ok":
        detail = f": {protection.error}" if protection.error else ""
        return (
            f"The default-branch protection of project {path} could not be read "
            f"({protection.status}){detail}."
        )
    read = (protection.payload or {}).get("branch")
    return (
        f"Branch protection of project {path} was last read for branch {read}, but the default "
        f"branch is now {project.default_branch}; it is read again at the next project discovery."
    )


DEFAULT_BRANCH_PROTECTED = RuleEvaluator(
    rule_type="gitlab.branch.default_protected",
    collection="projects",
    source=PROTECTION_SOURCE,
    capability=CAPABILITY,
    expected=_expected,
    observe=_observe,
    judge=_judge,
    value_unknown=_value_unknown,
)
