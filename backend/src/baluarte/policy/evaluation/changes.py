"""Change rules, judged over a window of a project's recorded changes (docs/CHANGE_EVIDENCE.md).

- ``gitlab.change.reviewed``: every merge request in the window had ``min_approvals`` approvals
  by people other than its author (and its author did not approve it, when forbidden); a direct
  push fails unless ``allow_direct_push``.
- ``gitlab.change.pipeline_passed``: the pipeline for every change's last commit succeeded, with
  a clean test report when ``require_tests``. A change with no pipeline fails.
- ``gitlab.change.deployed_after_review``: every successful production deployment in the window
  deployed a commit of a merge request reviewed by someone other than its author.

The window is the ``window_days`` up to the evaluation. Each change is judged from its own
observation (``baluarte.changes.assess``). A change that fails makes the rule fail; otherwise a
change that cannot be judged, or a part of the project's changes that could not be listed (a
direct push or deployment Baluarte cannot see might be in the window), makes it unknown. No
changes in the window passes, with a count of 0. The value is unknown, never assumed, when the
project's changes were never read or could not be listed, or its history starts after the
window does.

The observed value counts the changes (``changes``, ``passed``, ``failed``, ``unknown``) and names
the failing ones (at most ``MAX_LISTED``, with ``failing_total``). The result is backed by every
change observation in the window (``baluarte.policy.evaluation.store``).
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta
from typing import Any

from baluarte.changes import assess
from baluarte.integrations.gitlab.changes import CHANGE_READ_SOURCE, by_sha, iso, parse_time
from baluarte.policy.evaluation.engine import (
    ChangeEntry,
    ChangeHistory,
    ProjectFacts,
    RuleEvaluator,
)
from baluarte.policy.schema import (
    ChangeDeployedAfterReviewRule,
    ChangePipelinePassedRule,
    ChangeReviewedRule,
    Rule,
)

MAX_LISTED = 50
# The longest window a rule may ask for; changes this far back are loaded.
MAX_WINDOW_DAYS = 365
CHANGE_RULE_TYPES = (
    "gitlab.change.reviewed",
    "gitlab.change.pipeline_passed",
    "gitlab.change.deployed_after_review",
)


def _expected_reviewed(rule: Rule) -> dict[str, Any]:
    found = rule if isinstance(rule, ChangeReviewedRule) else None
    if found is None:
        raise TypeError(f"not a ChangeReviewedRule: {rule.type}")
    return {
        "window_days": found.window_days,
        "min_approvals": found.min_approvals,
        "forbid_self_approval": found.forbid_self_approval,
        "allow_direct_push": found.allow_direct_push,
    }


def _expected_pipeline(rule: Rule) -> dict[str, Any]:
    if not isinstance(rule, ChangePipelinePassedRule):
        raise TypeError(f"not a ChangePipelinePassedRule: {rule.type}")
    return {"window_days": rule.window_days, "require_tests": rule.require_tests}


def _expected_deployed(rule: Rule) -> dict[str, Any]:
    if not isinstance(rule, ChangeDeployedAfterReviewRule):
        raise TypeError(f"not a ChangeDeployedAfterReviewRule: {rule.type}")
    return {"window_days": rule.window_days}


EXPECTED: Mapping[str, Callable[[Rule], dict[str, Any]]] = {
    "gitlab.change.reviewed": _expected_reviewed,
    "gitlab.change.pipeline_passed": _expected_pipeline,
    "gitlab.change.deployed_after_review": _expected_deployed,
}
# The kinds of change each rule judges, and the listing that must be complete to see them all.
KINDS: Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "gitlab.change.reviewed": (("merge_request", "direct_push"), ("merge_requests", "commits")),
    "gitlab.change.pipeline_passed": (
        ("merge_request", "direct_push"),
        ("merge_requests", "commits"),
    ),
    "gitlab.change.deployed_after_review": (("deployment",), ("deployments",)),
}


def _ok_read(history: ChangeHistory | None) -> dict[str, Any] | None:
    if history is None or history.read is None or history.read.payload is None:
        return None
    if history.read.status not in ("ok", "partial"):
        return None
    return history.read.payload


def _judge(
    rule_type: str, expected: Mapping[str, Any], default_branch: str | None
) -> Callable[[Mapping[str, Any], Mapping[str, Mapping[str, Any]]], assess.Judged]:
    def judge(payload: Mapping[str, Any], shas: Mapping[str, Mapping[str, Any]]) -> assess.Judged:
        if rule_type == "gitlab.change.reviewed":
            return assess.review(
                payload,
                expected["min_approvals"],
                expected["forbid_self_approval"],
                expected["allow_direct_push"],
            )
        if rule_type == "gitlab.change.pipeline_passed":
            return assess.pipeline(payload, expected["require_tests"])
        return assess.deployed_change(payload, shas, default_branch)[1]

    return judge


def summarise(
    rule_type: str,
    expected: Mapping[str, Any],
    read: Mapping[str, Any],
    entries: Sequence[ChangeEntry],
    window: tuple[Any, Any],
) -> dict[str, Any] | None:
    """The observed value of a change rule over ``entries`` (every change loaded, oldest first)
    in ``window``; None when the window starts before the project's history does.

    The store rebuilds it from the stored observations, so it must depend only on its arguments.
    """
    start, end = window
    since = parse_time(read.get("history_since"))
    if since is None or start < since:
        return None
    kinds, listings = KINDS[rule_type]
    default_branch = read.get("default_branch")
    payloads = [entry.payload for entry in entries if isinstance(entry.payload, dict)]
    shas = by_sha(reversed(payloads))
    judge = _judge(rule_type, expected, default_branch)
    counts = {"pass": 0, "fail": 0, "unknown": 0}
    failing: list[dict[str, Any]] = []
    failing_reasons: list[str] = []
    unknown_reasons: list[str] = []
    total = 0
    for entry in entries:
        if entry.kind not in kinds or not (start <= entry.changed_at <= end):
            continue
        total += 1
        payload = entry.payload or {}
        verdict, reason = judge(payload, shas)
        counts[verdict] += 1
        if verdict == "fail":
            if len(failing) < MAX_LISTED:
                failing.append(
                    {
                        "id": entry.change_id,
                        "kind": entry.kind,
                        "iid": payload.get("iid") if entry.kind == "merge_request" else None,
                        "sha": payload.get("sha"),
                        "title": payload.get("title"),
                    }
                )
                failing_reasons.append(reason)
        elif verdict == "unknown" and len(unknown_reasons) < MAX_LISTED:
            unknown_reasons.append(reason)
    found = read.get("listed")
    listed: Mapping[str, Any] = found if isinstance(found, dict) else {}
    gaps = [
        f"{name.replace('_', ' ')} could not be listed ({listed.get(name)})"
        for name in listings
        if listed.get(name) != "ok"
    ]
    return {
        "window_days": expected["window_days"],
        "from": iso(start),
        "to": iso(end),
        "changes": total,
        "passed": counts["pass"],
        "failed": counts["fail"],
        "unknown": counts["unknown"],
        "failing": failing,
        "failing_total": counts["fail"],
        "gaps": gaps,
        # The first reasons of failing changes, else of unknown ones, to explain the outcome
        # without reading every change.
        "reasons": (failing_reasons or unknown_reasons)[:5],
    }


def evidence_entries(
    rule_type: str, read: Mapping[str, Any], entries: Sequence[ChangeEntry], window: tuple[Any, Any]
) -> list[ChangeEntry]:
    """The changes a result rests on: every change of the rule's kinds in the window, and for
    deployments the changes they deployed."""
    start, end = window
    kinds, _ = KINDS[rule_type]
    used = [e for e in entries if e.kind in kinds and start <= e.changed_at <= end]
    if rule_type == "gitlab.change.deployed_after_review":
        payloads = {id(e.payload): e for e in entries if isinstance(e.payload, dict)}
        shas = by_sha(reversed([e.payload for e in entries if isinstance(e.payload, dict)]))
        for deployment in list(used):
            sha = (deployment.payload or {}).get("sha")
            found = shas.get(sha) if isinstance(sha, str) else None
            if found is not None and payloads[id(found)] not in used:
                used.append(payloads[id(found)])
    return sorted(used, key=lambda entry: entry.observation_id)


def window(history: ChangeHistory, window_days: int) -> tuple[Any, Any]:
    return history.as_of - timedelta(days=window_days), history.as_of


def _observe(rule_type: str) -> Callable[[Rule, ProjectFacts], Any]:
    def observe(rule: Rule, project: ProjectFacts) -> Any:
        history = project.changes
        read = _ok_read(history)
        if history is None or read is None:
            return None
        expected = EXPECTED[rule_type](rule)
        return summarise(
            rule_type, expected, read, history.entries, window(history, expected["window_days"])
        )

    return observe


def _undecided(rule: Rule, observed: Any) -> str | None:
    if observed["failed"]:
        return None
    if observed["unknown"]:
        first = observed["reasons"][0] if observed["reasons"] else ""
        return (
            f"{observed['unknown']} of {observed['changes']} changes in the last "
            f"{observed['window_days']} days could not be judged; for example: {first}."
        )
    if observed["gaps"]:
        return (
            "Some changes may not have been seen: "
            + "; ".join(observed["gaps"])
            + ". They are read again at the next change collection."
        )
    return None


def _decide(what: str) -> Callable[[Rule, Any], tuple[bool, str]]:
    def judge(rule: Rule, observed: Any) -> tuple[bool, str]:
        days = observed["window_days"]
        if observed["failed"]:
            examples = "; ".join(observed["reasons"][:3])
            return False, (
                f"{observed['failed']} of {observed['changes']} {what} in the last {days} days "
                f"fall short: {examples}."
            )
        if not observed["changes"]:
            return True, f"No {what} in the last {days} days."
        return True, f"All {observed['changes']} {what} in the last {days} days meet the policy."

    return judge


def _unknown(project: ProjectFacts) -> str:
    history = project.changes
    if history is None or history.read is None:
        return f"The changes of project {project.path} have not been read yet."
    read = history.read
    if read.status not in ("ok", "partial") or read.payload is None:
        detail = f": {read.error}" if read.error else ""
        return f"The changes of project {project.path} could not be read ({read.status}){detail}."
    return (
        f"The change history of project {project.path} starts at "
        f"{read.payload.get('history_since')}, after the start of the rule's window; the window "
        "is judged once the history covers it."
    )


def _evaluator(rule_type: str, what: str) -> RuleEvaluator:
    return RuleEvaluator(
        rule_type=rule_type,
        collection="changes",
        source=CHANGE_READ_SOURCE,
        # Every part is read for every change and recorded with what GitLab answered, so a part
        # the token may not read makes those changes unknown on its own.
        capability=None,
        expected=EXPECTED[rule_type],
        observe=lambda project: None,
        observe_rule=_observe(rule_type),
        judge=_decide(what),
        value_unknown=_unknown,
        observed_at=lambda project: (
            project.changes.read.collected_at
            if project.changes is not None and project.changes.read is not None
            else None
        ),
        undecided=_undecided,
    )


CHANGE_REVIEWED = _evaluator("gitlab.change.reviewed", "changes")
CHANGE_PIPELINE_PASSED = _evaluator("gitlab.change.pipeline_passed", "changes")
CHANGE_DEPLOYED_AFTER_REVIEW = _evaluator(
    "gitlab.change.deployed_after_review", "production deployments"
)
CHANGE_EVALUATORS = (CHANGE_REVIEWED, CHANGE_PIPELINE_PASSED, CHANGE_DEPLOYED_AFTER_REVIEW)
