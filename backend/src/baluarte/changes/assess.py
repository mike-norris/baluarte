"""Judging one change from its observation: was it reviewed, did its pipeline pass, what deployed.

Pure functions over the payloads ``baluarte.integrations.gitlab.changes`` records, shared by the
change rules (``baluarte.policy.evaluation.changes``), the stored outcome of each change and the
Changes API, so a change is judged the same way everywhere. A part GitLab did not answer
(``status`` other than ``ok`` or ``none``) makes the verdict ``unknown``, never a pass.

Payloads:

- ``merge_request``: iid, title, web_path, author, merged_by, created_at, merged_at,
  target_branch, sha, merge_commit_sha, squash_commit_sha, and three parts: ``approvals``
  (approved_by usernames and when, approvals_required and approvals_left where GitLab reports
  them, self_approved, committer_approved), ``pipeline`` (the newest pipeline for the merge
  request's last commit: id, pipeline_status, source, finished_at and ``tests``, the test report
  summary or null when there is none) and ``deployments`` (successful deployments of its commits
  to the production environments).
- ``direct_push``: a first-parent commit on the default branch that no merged merge request
  brought: sha, title, author_name, committed_at, branch and its ``pipeline``.
- ``deployment``: one successful deployment: id, iid, environment, status, ref, sha, finished_at.
"""

from collections.abc import Mapping, Sequence
from typing import Any, Literal

Verdict = Literal["pass", "fail", "unknown"]
Judged = tuple[Verdict, str]

# Part statuses: ``ok`` was read, ``none`` means GitLab has nothing (no pipeline ran). Any other
# status means it could not be read. Only ``failed`` may pass with time, so a change with a
# failed part is read again.
KNOWN = ("ok", "none")
TRANSIENT = ("failed",)

DEFAULT_MIN_APPROVALS = 1


def _part(payload: Mapping[str, Any], name: str) -> dict[str, Any]:
    part = payload.get(name)
    return part if isinstance(part, dict) else {"status": "failed", "error": "not recorded"}


def _why(part: Mapping[str, Any], what: str) -> str:
    error = part.get("error")
    status = part.get("status")
    if status == "not_supported":
        return f"{what} cannot be read with this token or GitLab edition"
    return f"{what} could not be read ({status}{': ' + error if error else ''})"


def label(payload: Mapping[str, Any]) -> str:
    """How a change is named in reasons: ``!12`` or a short SHA."""
    if payload.get("kind") == "merge_request":
        return f"!{payload.get('iid')}"
    sha = payload.get("sha")
    short = sha[:8] if isinstance(sha, str) else "?"
    if payload.get("kind") == "deployment":
        return f"deployment {payload.get('id')} ({short})"
    return f"commit {short}"


def approvers(payload: Mapping[str, Any]) -> list[str] | None:
    """Usernames that approved the merge request, or None when not known."""
    part = _part(payload, "approvals")
    if part.get("status") != "ok" or not isinstance(part.get("approved_by"), list):
        return None
    names = [entry.get("username") for entry in part["approved_by"] if isinstance(entry, dict)]
    if any(not isinstance(name, str) for name in names):
        return None
    return [name for name in names if isinstance(name, str)]


def self_approved(payload: Mapping[str, Any]) -> bool | None:
    """Whether the author approved their own merge request; None when that is not known."""
    names = approvers(payload)
    author = payload.get("author")
    if names is None or not isinstance(author, str):
        return None
    return author in names


def review(
    payload: Mapping[str, Any],
    min_approvals: int = DEFAULT_MIN_APPROVALS,
    forbid_self_approval: bool = True,
    allow_direct_push: bool = False,
) -> Judged:
    """At least ``min_approvals`` approvals by someone other than the author."""
    name = label(payload)
    if payload.get("kind") == "direct_push":
        if allow_direct_push:
            return "pass", f"{name} was pushed directly, which the policy allows"
        return "fail", f"{name} was pushed directly to the default branch, without review"
    if payload.get("kind") != "merge_request":
        return "unknown", f"{name} is not a merge request or a commit"
    part = _part(payload, "approvals")
    names = approvers(payload)
    if names is None:
        return "unknown", f"approvals of {name}: " + _why(part, "approvals")
    author = payload.get("author")
    if not isinstance(author, str):
        return "unknown", f"the author of {name} is not known, so independent approval is not"
    others = sorted({approver for approver in names if approver != author})
    if forbid_self_approval and author in names:
        return "fail", f"{name} was approved by its author {author}"
    if len(others) < min_approvals:
        count = len(others)
        return "fail", (
            f"{name} had {count} approval{'' if count == 1 else 's'} by someone other than its "
            f"author; {min_approvals} required"
        )
    return "pass", f"{name} was approved by {', '.join(others)}"


def tests_of(part: Mapping[str, Any]) -> dict[str, Any] | None:
    tests = part.get("tests")
    return tests if isinstance(tests, dict) else None


def pipeline(payload: Mapping[str, Any], require_tests: bool = False) -> Judged:
    """The pipeline for the change's last commit succeeded (with a clean test report when
    ``require_tests``). A change with no pipeline fails: nothing shows it was tested."""
    name = label(payload)
    part = _part(payload, "pipeline")
    status = part.get("status")
    if status == "none":
        return "fail", f"no pipeline ran for {name}"
    if status != "ok":
        return "unknown", f"pipeline of {name}: " + _why(part, "the pipeline")
    result = part.get("pipeline_status")
    if not isinstance(result, str):
        return "unknown", f"GitLab did not report the pipeline status of {name}"
    if result != "success":
        return "fail", f"the pipeline of {name} ended {result}"
    if not require_tests:
        return "pass", f"the pipeline of {name} succeeded"
    tests_status = part.get("tests_status")
    if tests_status not in KNOWN:
        return "unknown", f"test report of {name}: " + _why(
            {"status": tests_status, "error": part.get("tests_error")}, "the test report"
        )
    tests = tests_of(part)
    if tests is None:
        return "fail", f"the pipeline of {name} has no test report"
    failed = (tests.get("failed") or 0) + (tests.get("error") or 0)
    if not isinstance(tests.get("total"), int):
        return "unknown", f"GitLab did not report the test totals of {name}"
    if failed:
        return "fail", f"the test report of {name} has {failed} failed or erroring tests"
    return "pass", f"the pipeline of {name} succeeded with {tests['total']} tests and none failed"


def combine(verdicts: Sequence[Judged]) -> tuple[Verdict, list[str]]:
    """Fail when any part fails, unknown when any is unknown, else pass; with the reasons that
    decided it."""
    for wanted in ("fail", "unknown"):
        reasons = [reason for verdict, reason in verdicts if verdict == wanted]
        if reasons:
            return wanted, reasons
    return "pass", [reason for _, reason in verdicts]


def deployed_change(
    deployment: Mapping[str, Any],
    changes: Mapping[str, Mapping[str, Any]],
    default_branch: str | None,
) -> tuple[Mapping[str, Any] | None, Judged]:
    """Whether a deployment shipped a reviewed change: the change whose commit it deployed (by
    any of its SHAs in ``changes``), and the verdict."""
    name = label(deployment)
    sha = deployment.get("sha")
    found = changes.get(sha) if isinstance(sha, str) else None
    if found is None:
        ref = deployment.get("ref")
        if isinstance(ref, str) and default_branch is not None and ref != default_branch:
            return None, ("fail", f"{name} deployed {ref}, not the default branch")
        return None, (
            "unknown",
            f"{name} deployed a commit that is not among the recorded changes; it may predate "
            "the collected history",
        )
    verdict, reason = review(found)
    if verdict == "pass":
        return found, ("pass", f"{name} deployed {label(found)}, which was reviewed")
    return found, (verdict, f"{name} deployed {label(found)}: {reason}")


def assess(payload: Mapping[str, Any]) -> tuple[Verdict, list[str]]:
    """A merge request or direct push with the default checks: reviewed by someone other than its
    author, and its pipeline succeeded."""
    return combine([review(payload), pipeline(payload)])


def incomplete(payload: Mapping[str, Any]) -> bool:
    """Whether a part failed in a way the next read may fix."""
    return any(
        _part(payload, name).get("status") in TRANSIENT
        for name in ("approvals", "pipeline", "deployments")
        if name in payload
    )


def shas(payload: Mapping[str, Any]) -> list[str]:
    """Every commit that identifies the change, without repeats."""
    keys = ("merge_commit_sha", "squash_commit_sha", "sha")
    found: list[str] = []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value and value not in found:
            found.append(value)
    return found


def landed_sha(payload: Mapping[str, Any]) -> str | None:
    """The commit on the default branch: the merge or squash commit, else the last commit (a
    fast-forward merge)."""
    found = shas(payload)
    return found[0] if found else None
