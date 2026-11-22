"""How the shared rule types are judged on GitHub repositories (roadmap M13.3).

A rule type keeps one name and one schema across providers; each provider that can judge it
contributes its own evaluator, reading that provider's evidence. A rule type with no evaluator for
a provider, or a rule setting the provider has no equivalent for, is ``unsupported`` there, never
``pass`` (docs/GITHUB_INTEGRATION.md#rule-types).

- ``gitlab.project.visibility``: the repository's ``visibility``, judged as on GitLab.
- ``gitlab.branch.default_protected``: the default branch is protected by classic branch
  protection or by any ruleset rule that applies to it.
- ``gitlab.project.merge_request_approvals``: the larger of classic protection's
  ``required_approving_review_count`` and that of any ``pull_request`` ruleset rule. GitHub never
  lets a pull request's author approve it, so ``forbid_author_approval`` always holds;
  ``reset_approvals_on_push`` is classic ``dismiss_stale_reviews`` or the rule's
  ``dismiss_stale_reviews_on_push``. GitHub has no equivalent of ``forbid_committer_approval`` or
  ``forbid_override``, so a rule that sets either is ``unsupported``.
- ``gitlab.group.two_factor_required``: the organization's ``two_factor_requirement_enabled``.
  GitHub has no grace period: members without two-factor authentication are removed when the
  requirement is turned on, so ``max_grace_hours`` always holds.

The branch rules are ``unsupported`` for a repository whose plan does not offer branch
protection (a private repository on GitHub Free).

GitHub-only rule types (roadmap M13.3b):

- ``github.repository.security_features``: every listed feature is ``enabled`` for the
  repository. A feature GitHub did not report (``security_and_analysis`` needs admin rights) is
  ``unknown``; code scanning on a repository whose licence lacks GitHub Advanced Security is
  ``unsupported``.
- ``gitlab.labels.consistent`` is judged on GitHub as on GitLab, from the repositories' labels
  (GitHub organizations define none that repositories inherit).
"""

from dataclasses import replace
from typing import Any

from baluarte.integrations.github.branches import BRANCH_SOURCE
from baluarte.integrations.github.discovery import ORG_SOURCE
from baluarte.integrations.github.repositories import SECURITY_SOURCE
from baluarte.policy.evaluation.access import MERGE_REQUEST_APPROVALS, TWO_FACTOR_REQUIRED
from baluarte.policy.evaluation.default_branch import DEFAULT_BRANCH_PROTECTED
from baluarte.policy.evaluation.engine import ProjectFacts, ProjectObservation, RuleEvaluator
from baluarte.policy.evaluation.labels import LABELS_CONSISTENT
from baluarte.policy.evaluation.visibility import VISIBILITY
from baluarte.policy.schema import (
    GitHubSecurityFeaturesRule,
    MergeRequestApprovalsRule,
    Rule,
    TwoFactorRequiredRule,
)

PROVIDER = "github"
BRANCH_CAPABILITY = "branch_protection.read"
TWO_FACTOR_CAPABILITY = "org.two_factor"
# Approval flags GitHub has no equivalent for.
_NO_EQUIVALENT = {
    "forbid_committer_approval": "committers cannot approve",
    "forbid_override": "approval rules cannot be changed per pull request",
}


def _branch_observation(project: ProjectFacts) -> ProjectObservation | None:
    return project.github.get(BRANCH_SOURCE)


def _branch(project: ProjectFacts) -> dict[str, Any] | None:
    observation = _branch_observation(project)
    if project.default_branch is None or observation is None or observation.status != "ok":
        return None
    payload = observation.payload
    if payload is None or payload.get("branch") != project.default_branch:
        return None
    if not isinstance(payload.get("protected"), bool):
        return None
    return payload


def _plan_refused(_rule: Rule, project: ProjectFacts) -> str | None:
    observation = _branch_observation(project)
    if observation is not None and observation.status == "unsupported":
        return (
            f"GitHub does not offer branch protection for repository {project.path} on its "
            "plan (private repositories need GitHub Pro, Team or Enterprise)."
        )
    return None


def _branch_unknown(project: ProjectFacts) -> str:
    path = project.path
    observation = _branch_observation(project)
    if project.default_branch is None:
        return f"Repository {path} has no default branch (it is empty), so there is no branch."
    if observation is None:
        return f"The default-branch settings of repository {path} have not been read yet."
    if observation.status != "ok":
        detail = f": {observation.error}" if observation.error else ""
        return (
            f"The default-branch settings of repository {path} could not be read "
            f"({observation.status}){detail}."
        )
    read = (observation.payload or {}).get("branch")
    return (
        f"The settings of repository {path} were last read for branch {read}, but the default "
        f"branch is now {project.default_branch}; they are read again at the next collection."
    )


def _observed_at(project: ProjectFacts) -> Any:
    observation = _branch_observation(project)
    return observation.collected_at if observation is not None else project.last_seen_at


DEFAULT_BRANCH_PROTECTED_GITHUB = replace(
    DEFAULT_BRANCH_PROTECTED,
    source=BRANCH_SOURCE,
    capability=BRANCH_CAPABILITY,
    observe=_branch,
    value_unknown=_branch_unknown,
    unsupported=_plan_refused,
    observed_at=_observed_at,
)


def _review_settings(observed: dict[str, Any]) -> tuple[int, bool]:
    """(required approving reviews, whether a push dismisses stale approvals)."""
    required = 0
    dismiss = False
    classic = observed.get("classic")
    reviews = classic.get("reviews") if isinstance(classic, dict) else None
    if isinstance(reviews, dict):
        count = reviews.get("required_approving_review_count")
        required = max(required, count if isinstance(count, int) else 0)
        dismiss = dismiss or reviews.get("dismiss_stale_reviews") is True
    for rule in observed.get("ruleset_rules", []):
        if not isinstance(rule, dict) or rule.get("type") != "pull_request":
            continue
        parameters = rule.get("parameters") or {}
        count = parameters.get("required_approving_review_count")
        required = max(required, count if isinstance(count, int) else 0)
        dismiss = dismiss or parameters.get("dismiss_stale_reviews_on_push") is True
    return required, dismiss


def _approvals_rule(rule: Rule) -> MergeRequestApprovalsRule:
    if not isinstance(rule, MergeRequestApprovalsRule):
        raise TypeError(f"not an approvals rule: {rule.type}")
    return rule


def _approvals_unsupported(rule: Rule, project: ProjectFacts) -> str | None:
    refused = _plan_refused(rule, project)
    if refused is not None:
        return refused
    found = _approvals_rule(rule)
    missing = [wording for flag, wording in _NO_EQUIVALENT.items() if getattr(found, flag)]
    if missing:
        return (
            "GitHub has no setting equivalent to the policy's requirement that "
            f"{' and that '.join(missing)}."
        )
    return None


def _plural(count: int) -> str:
    return f"{count} approving review" + ("" if count == 1 else "s")


def _approvals_judge(rule: Rule, observed: Any) -> tuple[bool, str]:
    found = _approvals_rule(rule)
    branch = observed["branch"]
    required, dismiss = _review_settings(observed)
    problems = []
    if required < found.min_approvals:
        problems.append(
            f"pull requests to {branch} need {_plural(required)}, fewer than the "
            f"{found.min_approvals} required"
        )
    if found.reset_approvals_on_push and not dismiss:
        problems.append("a new push does not dismiss approving reviews")
    if problems:
        return False, "Review settings fall short: " + "; ".join(problems) + "."
    return True, (
        f"Pull requests to {branch} need {_plural(required)}, and the review settings meet the "
        "policy."
    )


MERGE_REQUEST_APPROVALS_GITHUB = replace(
    MERGE_REQUEST_APPROVALS,
    source=BRANCH_SOURCE,
    capability=BRANCH_CAPABILITY,
    observe=_branch,
    judge=_approvals_judge,
    value_unknown=_branch_unknown,
    undecided=lambda _rule, _observed: None,
    unsupported=_approvals_unsupported,
    observed_at=_observed_at,
)


def _organization(project: ProjectFacts) -> ProjectObservation | None:
    return project.github.get(ORG_SOURCE)


def _two_factor(project: ProjectFacts) -> dict[str, Any] | None:
    observation = _organization(project)
    if observation is None or observation.status != "ok" or observation.payload is None:
        return None
    if not isinstance(observation.payload.get("require_two_factor_authentication"), bool):
        return None
    return observation.payload


def _two_factor_judge(rule: Rule, observed: Any) -> tuple[bool, str]:
    if not isinstance(rule, TwoFactorRequiredRule):
        raise TypeError(f"not a two-factor rule: {rule.type}")
    organization = observed.get("full_path")
    if observed["require_two_factor_authentication"]:
        return True, (
            f"Organization {organization} requires two-factor authentication; GitHub removes "
            "members without it, so there is no grace period."
        )
    return False, f"Organization {organization} does not require two-factor authentication."


def _two_factor_unknown(project: ProjectFacts) -> str:
    observation = _organization(project)
    if observation is None:
        return f"The organization of repository {project.path} has not been read yet."
    if observation.status != "ok":
        return f"The organization of repository {project.path} could not be read."
    return (
        "GitHub did not report whether the organization requires two-factor authentication; it "
        "shows this only to organization owners."
    )


def _organization_at(project: ProjectFacts) -> Any:
    observation = _organization(project)
    return observation.collected_at if observation is not None else project.last_seen_at


TWO_FACTOR_REQUIRED_GITHUB = replace(
    TWO_FACTOR_REQUIRED,
    collection="groups",
    source=ORG_SOURCE,
    capability=TWO_FACTOR_CAPABILITY,
    observe=_two_factor,
    judge=_two_factor_judge,
    value_unknown=_two_factor_unknown,
    unknown_reason=lambda _project: "value_unknown",
    undecided=lambda _rule, _observed: None,
    observed_at=_organization_at,
)

# The repository's visibility, as listed or looked up (``REPOSITORY_SOURCES``).
VISIBILITY_SOURCE = "github_repository.visibility"
VISIBILITY_GITHUB = replace(
    VISIBILITY,
    source=VISIBILITY_SOURCE,
    value_unknown=lambda project: f"GitHub did not report a visibility for {project.path}.",
)

SECURITY_FEATURES_TYPE = "github.repository.security_features"
_FEATURE_NAMES = {
    "secret_scanning": "secret scanning",
    "secret_scanning_push_protection": "secret scanning push protection",
    "dependabot_security_updates": "Dependabot security updates",
    "code_scanning": "code scanning",
}


def _security_rule(rule: Rule) -> GitHubSecurityFeaturesRule:
    if not isinstance(rule, GitHubSecurityFeaturesRule):
        raise TypeError(f"not a security features rule: {rule.type}")
    return rule


def _security_observation(project: ProjectFacts) -> ProjectObservation | None:
    return project.github.get(SECURITY_SOURCE)


def _security(project: ProjectFacts) -> dict[str, Any] | None:
    observation = _security_observation(project)
    if observation is None or observation.status != "ok" or observation.payload is None:
        return None
    if not isinstance(observation.payload.get("features"), dict):
        return None
    return observation.payload


def _feature_status(observed: dict[str, Any], feature: str) -> Any:
    return observed["features"].get(feature)


def _security_undecided(rule: Rule, observed: Any) -> str | None:
    unreported = [
        _FEATURE_NAMES[feature]
        for feature in _security_rule(rule).features
        if _feature_status(observed, feature) not in ("enabled", "disabled", "unsupported")
    ]
    if not unreported:
        return None
    return (
        f"GitHub did not report {', '.join(unreported)} for the repository; it reports security "
        "settings only to a token with admin rights on it."
    )


def _security_unsupported(rule: Rule, project: ProjectFacts) -> str | None:
    observed = _security(project)
    if observed is None:
        return None
    if any(
        _feature_status(observed, feature) == "unsupported"
        for feature in _security_rule(rule).features
    ):
        return (
            f"GitHub does not offer code scanning for repository {project.path}: GitHub Advanced "
            "Security is not enabled for it."
        )
    return None


def _security_judge(rule: Rule, observed: Any) -> tuple[bool, str]:
    features = _security_rule(rule).features
    off = [_FEATURE_NAMES[f] for f in features if _feature_status(observed, f) != "enabled"]
    if off:
        return False, f"Not enabled: {', '.join(off)}."
    return True, f"Enabled: {', '.join(_FEATURE_NAMES[f] for f in features)}."


def _security_unknown(project: ProjectFacts) -> str:
    observation = _security_observation(project)
    if observation is None:
        return f"The security features of repository {project.path} have not been read yet."
    detail = f": {observation.error}" if observation.error else ""
    return (
        f"The security features of repository {project.path} could not be read "
        f"({observation.status}){detail}."
    )


def _security_at(project: ProjectFacts) -> Any:
    observation = _security_observation(project)
    return observation.collected_at if observation is not None else project.last_seen_at


SECURITY_FEATURES = RuleEvaluator(
    rule_type=SECURITY_FEATURES_TYPE,
    collection="projects",
    source=SECURITY_SOURCE,
    capability=None,
    expected=lambda rule: {"features": list(_security_rule(rule).features)},
    observe=_security,
    judge=_security_judge,
    value_unknown=_security_unknown,
    observed_at=_security_at,
    undecided=_security_undecided,
    unsupported=_security_unsupported,
)

EVALUATORS: tuple[RuleEvaluator, ...] = (
    VISIBILITY_GITHUB,
    DEFAULT_BRANCH_PROTECTED_GITHUB,
    MERGE_REQUEST_APPROVALS_GITHUB,
    TWO_FACTOR_REQUIRED_GITHUB,
    LABELS_CONSISTENT,
    SECURITY_FEATURES,
)


def not_on(evaluator: RuleEvaluator, provider: str) -> RuleEvaluator:
    """``evaluator``'s rule type on a provider that cannot judge it: always ``unsupported``."""
    targets = "repositories" if provider == "GitHub" else "projects"
    message = f"Baluarte does not evaluate {evaluator.rule_type} on {provider} {targets}."
    return replace(
        evaluator,
        collection="projects",
        capability=None,
        observe=lambda _project: None,
        observe_rule=None,
        undecided=lambda _rule, _observed: None,
        unsupported=lambda _rule, _project: message,
    )
