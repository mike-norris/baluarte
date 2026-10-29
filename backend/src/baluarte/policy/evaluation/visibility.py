"""``gitlab.project.visibility``: the project's visibility must be one of ``allowed``.

Every supported GitLab version and edition reports a project's visibility, so the rule needs no
capability and is never ``unsupported``. A project whose visibility was not reported (stored as
NULL) is ``unknown``.
"""

from typing import Any

from baluarte.policy.evaluation.engine import ProjectFacts, RuleEvaluator
from baluarte.policy.schema import ProjectVisibilityRule, Rule


def _rule(rule: Rule) -> ProjectVisibilityRule:
    if not isinstance(rule, ProjectVisibilityRule):
        raise TypeError(f"not a visibility rule: {rule.type}")
    return rule


def _expected(rule: Rule) -> dict[str, Any]:
    return {"allowed": list(_rule(rule).allowed)}


def _observe(project: ProjectFacts) -> str | None:
    return project.visibility


def _judge(rule: Rule, observed: Any) -> tuple[bool, str]:
    allowed = _rule(rule).allowed
    listed = ", ".join(allowed)
    if observed in allowed:
        return True, f"Visibility is {observed}, which is allowed ({listed})."
    return False, f"Visibility is {observed}; the policy allows only {listed}."


def _value_unknown(project: ProjectFacts) -> str:
    return f"GitLab did not report a visibility for project {project.path}."


VISIBILITY = RuleEvaluator(
    rule_type="gitlab.project.visibility",
    collection="projects",
    source="gitlab_project.visibility",
    capability=None,
    expected=_expected,
    observe=_observe,
    judge=_judge,
    value_unknown=_value_unknown,
)
