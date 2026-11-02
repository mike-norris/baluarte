"""``gitlab.security_policy.scan_execution`` and ``gitlab.security_policy.approval``.

Both judge the GitLab security policies that reach a project, as recorded by security policy
discovery (``baluarte.integrations.gitlab.security_policies``): the project's latest
observation lists, per policy type, the policies GitLab applies to it. Discovery asks GitLab for
the project's policies with ``includeUnscoped: false``, so GitLab has already applied each
policy's ``policy_scope``; a policy that names the project in ``excluding_projects`` is also
skipped here. Baluarte only reads the policies; it never changes them.

Each policy's YAML is parsed with the same restricted loader as baselines (no tags, anchors or
duplicate keys). The value judged is a summary of the enabled policies of the rule's type,
built from the observation alone, so the evidence check can rebuild it from the stored payload.

The value is unknown, never assumed, when:

- the project's policies have never been read, or the last read failed
  (``security_policies_unread``), or the type was not checked (also ``security_policies_unread``);
- this GitLab does not offer the policy type (``security_policy_type_unsupported``);
- the listing for the type was cut short (``security_policy_truncated``);
- a policy's YAML or enabled flag cannot be read (``security_policy_yaml_invalid``).

What counts:

- **Scan execution:** a policy runs a scan on the default branch when one of its ``pipeline``
  or ``schedule`` rules has ``branch_type`` ``default``, ``protected`` (GitLab protects the
  default branch unless told otherwise; the default-branch rule checks that separately) or
  ``all``, or lists the branch ``*``. A policy without ``skip_ci`` lets pipelines skip it, as
  GitLab does by default.
- **Approval:** only ``any_merge_request`` rules require approval on every merge request; a
  ``scan_finding`` or ``license_finding`` rule asks for approval only when something is found,
  so it does not satisfy the rule. The rule must cover protected branches: ``branch_type``
  ``protected``, ``default`` or ``all``, or an empty ``branches`` list (all protected branches).
"""

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from baluarte.integrations.gitlab.security_policies import PROJECT_SOURCE, READ_PROBE
from baluarte.policy.evaluation.engine import ProjectFacts, RuleEvaluator, SecurityPolicies
from baluarte.policy.evaluation.model import UnknownReason
from baluarte.policy.loader import PolicyInvalid, load_yaml
from baluarte.policy.schema import (
    Rule,
    SecurityPolicyApprovalRule,
    SecurityPolicyScanExecutionRule,
)

SCAN_EXECUTION = "gitlab.security_policy.scan_execution"
APPROVAL = "gitlab.security_policy.approval"
# The observation's ``types`` key for each rule type.
POLICY_TYPES: Mapping[str, str] = {SCAN_EXECUTION: "scan_execution", APPROVAL: "approval"}
_TYPE_NAMES: Mapping[str, str] = {SCAN_EXECUTION: "scan execution", APPROVAL: "approval"}

# GitLab caps a policy file at a few hundred KiB; a single policy is far smaller.
MAX_POLICY_YAML_BYTES = 256 * 1024
_SCAN_BRANCH_TYPES = frozenset({"default", "protected", "all"})
_APPROVAL_BRANCH_TYPES = frozenset({"protected", "default", "all"})


class _Invalid(Exception):
    """A policy whose YAML or enabled flag cannot be read."""


def _entry(payload: Mapping[str, Any] | None, rule_type: str) -> Mapping[str, Any] | None:
    types = payload.get("types") if isinstance(payload, Mapping) else None
    entry = types.get(POLICY_TYPES[rule_type]) if isinstance(types, Mapping) else None
    return entry if isinstance(entry, Mapping) else None


def _document(policy: Mapping[str, Any]) -> dict[str, Any]:
    source = policy.get("yaml")
    if not isinstance(source, str):
        raise _Invalid
    try:
        document = load_yaml(source, MAX_POLICY_YAML_BYTES)
    except PolicyInvalid as exc:
        raise _Invalid from exc
    if not isinstance(document, dict):
        raise _Invalid
    return document


def _enabled(policy: Mapping[str, Any], document: Mapping[str, Any]) -> bool:
    for value in (policy.get("enabled"), document.get("enabled")):
        if isinstance(value, bool):
            return value
    raise _Invalid


def _excluded(policy: Mapping[str, Any], path: str) -> bool:
    scope = policy.get("scope")
    excluding = scope.get("excluding_projects") if isinstance(scope, Mapping) else None
    return isinstance(excluding, list) and path in excluding


def _items(document: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = document.get(key)
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _source(policy: Mapping[str, Any]) -> str | None:
    source = policy.get("source")
    path = source.get("path") if isinstance(source, Mapping) else None
    return path if isinstance(path, str) else None


def _scan_summary(policy: Mapping[str, Any], document: Mapping[str, Any]) -> dict[str, Any]:
    on_default = any(
        rule.get("type") in ("pipeline", "schedule")
        and (
            rule.get("branch_type") in _SCAN_BRANCH_TYPES
            or (isinstance(rule.get("branches"), list) and "*" in rule["branches"])
        )
        for rule in _items(document, "rules")
    )
    scans = sorted(
        {
            action["scan"]
            for action in _items(document, "actions")
            if isinstance(action.get("scan"), str)
        }
    )
    skip_ci = document.get("skip_ci")
    allowed = skip_ci.get("allowed") if isinstance(skip_ci, Mapping) else None
    return {
        "name": policy.get("name"),
        "source": _source(policy),
        "scans": scans,
        "runs_on_default_branch": on_default,
        "skip_ci_allowed": allowed if isinstance(allowed, bool) else True,
    }


def _covers_protected(rule: Mapping[str, Any]) -> bool:
    if rule.get("branch_type") in _APPROVAL_BRANCH_TYPES:
        return True
    return "branch_type" not in rule and rule.get("branches") == []


def _approval_summary(policy: Mapping[str, Any], document: Mapping[str, Any]) -> dict[str, Any]:
    approvals = [
        action["approvals_required"]
        for action in _items(document, "actions")
        if action.get("type") == "require_approval"
        and isinstance(action.get("approvals_required"), int)
        and not isinstance(action.get("approvals_required"), bool)
    ]
    settings = document.get("approval_settings")
    settings = settings if isinstance(settings, Mapping) else {}

    def flag(name: str) -> bool:
        return settings.get(name) is True

    return {
        "name": policy.get("name"),
        "source": _source(policy),
        "approvals_required": max(approvals, default=0),
        "every_merge_request": any(
            rule.get("type") == "any_merge_request" and _covers_protected(rule)
            for rule in _items(document, "rules")
        ),
        "prevent_approval_by_author": flag("prevent_approval_by_author"),
        "prevent_approval_by_commit_author": flag("prevent_approval_by_commit_author"),
    }


_SUMMARIES: Mapping[str, Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]] = {
    SCAN_EXECUTION: _scan_summary,
    APPROVAL: _approval_summary,
}


def _problem(
    status: str | None, payload: Mapping[str, Any] | None, rule_type: str
) -> UnknownReason | None:
    """Why the observation cannot be judged for this rule type, or None when it can."""
    if status == "unsupported":
        return "security_policy_type_unsupported"
    if status not in ("ok", "partial"):
        return "security_policies_unread"
    entry = _entry(payload, rule_type)
    if entry is None:
        return "security_policies_unread"
    if entry.get("status") == "unsupported":
        return "security_policy_type_unsupported"
    if entry.get("status") != "ok" or not isinstance(entry.get("policies"), list):
        return "security_policies_unread"
    if entry.get("truncated") is not False:
        return "security_policy_truncated"
    return None


def summarize(
    rule_type: str, path: str, payload: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """The enabled policies of the rule's type that reach ``path``, from an observation payload.

    None when the payload does not say (see ``_problem``) or a policy cannot be read. The same
    function rebuilds the value from stored evidence, so it reads nothing but the payload.
    """
    entry = _entry(payload, rule_type)
    if entry is None or _problem("ok", payload, rule_type) is not None:
        return None
    policies: list[dict[str, Any]] = []
    disabled = 0
    for policy in entry["policies"]:
        if not isinstance(policy, Mapping):
            return None
        try:
            document = _document(policy)
            enabled = _enabled(policy, document)
        except _Invalid:
            return None
        if _excluded(policy, path):
            continue
        if not enabled:
            disabled += 1
            continue
        policies.append(_SUMMARIES[rule_type](policy, document))
    policies.sort(key=lambda summary: (summary["source"] or "", summary["name"] or ""))
    return {"policies": policies, "disabled": disabled}


def _observe(rule_type: str) -> Callable[[ProjectFacts], dict[str, Any] | None]:
    def observe(project: ProjectFacts) -> dict[str, Any] | None:
        read = project.security_policies
        if read is None or _problem(read.status, read.payload, rule_type) is not None:
            return None
        return summarize(rule_type, project.path, read.payload)

    return observe


def _unknown_reason(rule_type: str) -> Callable[[ProjectFacts], UnknownReason]:
    def reason(project: ProjectFacts) -> UnknownReason:
        read = project.security_policies
        if read is None:
            return "security_policies_unread"
        return _problem(read.status, read.payload, rule_type) or "security_policy_yaml_invalid"

    return reason


def _value_unknown(rule_type: str) -> Callable[[ProjectFacts], str]:
    name = _TYPE_NAMES[rule_type]

    def explain(project: ProjectFacts) -> str:
        path = project.path
        read: SecurityPolicies | None = project.security_policies
        if read is None:
            return f"The GitLab security policies of project {path} have not been read yet."
        reason = _unknown_reason(rule_type)(project)
        if reason == "security_policy_type_unsupported":
            return f"This GitLab instance does not offer {name} policies."
        if reason == "security_policy_truncated":
            return (
                f"GitLab listed more {name} policies for project {path} than one page holds, "
                "so the listing is incomplete."
            )
        if reason == "security_policy_yaml_invalid":
            return (
                f"A {name} policy reaching project {path} could not be read safely (its YAML "
                "or enabled flag is not valid)."
            )
        if read.status not in ("ok", "partial"):
            detail = f": {read.error}" if read.error else ""
            return (
                f"The GitLab security policies of project {path} could not be read "
                f"({read.status}){detail}."
            )
        return f"The {name} policies of project {path} were not checked at the last discovery."

    return explain


def _observed_at(project: ProjectFacts) -> datetime | None:
    read = project.security_policies
    return read.collected_at if read is not None else None


def _scan_rule(rule: Rule) -> SecurityPolicyScanExecutionRule:
    if not isinstance(rule, SecurityPolicyScanExecutionRule):
        raise TypeError(f"not a scan execution policy rule: {rule.type}")
    return rule


def _approval_rule(rule: Rule) -> SecurityPolicyApprovalRule:
    if not isinstance(rule, SecurityPolicyApprovalRule):
        raise TypeError(f"not an approval policy rule: {rule.type}")
    return rule


def _names(policies: list[dict[str, Any]]) -> str:
    return ", ".join(str(policy["name"]) for policy in policies)


def _judge_scans(rule: Rule, observed: Any) -> tuple[bool, str]:
    scan_rule = _scan_rule(rule)
    policies: list[dict[str, Any]] = observed["policies"]
    if not policies:
        return False, "No enabled scan execution policy reaches this project."
    counted = [
        policy
        for policy in policies
        if policy["runs_on_default_branch"]
        and (scan_rule.allow_skip_ci or not policy["skip_ci_allowed"])
    ]
    running = {scan for policy in counted for scan in policy["scans"]}
    missing = [scan for scan in scan_rule.scans if scan not in running]
    if not missing:
        return True, f"Every required scan runs on the default branch ({_names(counted)})."
    notes = []
    if not scan_rule.allow_skip_ci:
        skippable = [policy for policy in policies if policy["skip_ci_allowed"]]
        if skippable:
            notes.append(f"not counted because [skip ci] can skip them: {_names(skippable)}")
    off_default = [policy for policy in policies if not policy["runs_on_default_branch"]]
    if off_default:
        notes.append(f"not run on the default branch: {_names(off_default)}")
    suffix = f" ({'; '.join(notes)})" if notes else ""
    return False, f"Missing scans: {', '.join(missing)}{suffix}."


def _judge_approval(rule: Rule, observed: Any) -> tuple[bool, str]:
    approval_rule = _approval_rule(rule)
    policies: list[dict[str, Any]] = observed["policies"]
    if not policies:
        return False, "No enabled approval policy reaches this project."
    for policy in policies:
        if (
            policy["every_merge_request"]
            and policy["approvals_required"] >= approval_rule.min_approvals
            and (not approval_rule.forbid_author_approval or policy["prevent_approval_by_author"])
            and (
                not approval_rule.forbid_committer_approval
                or policy["prevent_approval_by_commit_author"]
            )
        ):
            return True, (
                f"Approval policy {policy['name']} requires {policy['approvals_required']} "
                "approval(s) on every merge request to protected branches."
            )
    gaps = []
    if not any(policy["every_merge_request"] for policy in policies):
        gaps.append("none requires approval on every merge request to protected branches")
    else:
        gaps.append(f"none that does requires at least {approval_rule.min_approvals} approval(s)")
        if approval_rule.forbid_author_approval:
            gaps.append("or prevents approval by the author")
        if approval_rule.forbid_committer_approval:
            gaps.append("or prevents approval by committers")
    return (
        False,
        f"Approval policies reach this project ({_names(policies)}), but {' '.join(gaps)}.",
    )


def _expected_scans(rule: Rule) -> dict[str, Any]:
    scan_rule = _scan_rule(rule)
    return {"scans": list(scan_rule.scans), "allow_skip_ci": scan_rule.allow_skip_ci}


def _expected_approval(rule: Rule) -> dict[str, Any]:
    approval_rule = _approval_rule(rule)
    return {
        "min_approvals": approval_rule.min_approvals,
        "forbid_author_approval": approval_rule.forbid_author_approval,
        "forbid_committer_approval": approval_rule.forbid_committer_approval,
    }


def _evaluator(
    rule_type: str,
    expected: Callable[[Rule], dict[str, Any]],
    judge: Callable[[Rule, Any], tuple[bool, str]],
) -> RuleEvaluator:
    return RuleEvaluator(
        rule_type=rule_type,
        collection="security_policies",
        source=PROJECT_SOURCE,
        capability=READ_PROBE,
        expected=expected,
        observe=_observe(rule_type),
        judge=judge,
        value_unknown=_value_unknown(rule_type),
        unknown_reason=_unknown_reason(rule_type),
        observed_at=_observed_at,
    )


SCAN_EXECUTION_POLICY = _evaluator(SCAN_EXECUTION, _expected_scans, _judge_scans)
APPROVAL_POLICY = _evaluator(APPROVAL, _expected_approval, _judge_approval)
