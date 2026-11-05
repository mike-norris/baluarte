"""Judging changes from their payloads, and the change rules' values over a window."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from baluarte.changes import assess
from baluarte.evidence import sampling
from baluarte.policy.evaluation.changes import evidence_entries, summarise
from baluarte.policy.evaluation.engine import ChangeEntry

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)


def approvals(*names: str, status: str = "ok") -> dict[str, Any]:
    if status != "ok":
        return {"status": status, "error": f"403 {status}"}
    return {
        "status": "ok",
        "approved_by": [
            {"username": name, "approved_at": "2026-10-30T10:00:00Z"} for name in names
        ],
        "approvals_required": None,
        "approvals_left": None,
        "self_approved": None,
        "committer_approved": None,
    }


def pipeline(result: str | None = "success", status: str = "ok", **extra: Any) -> dict[str, Any]:
    if status != "ok":
        return {"status": status, "error": "boom"} if status != "none" else {"status": "none"}
    return {
        "status": "ok",
        "id": 70,
        "pipeline_status": result,
        "source": "merge_request_event",
        "sha": "a" * 40,
        "finished_at": "2026-10-30T09:00:00Z",
        "tests": None,
        "tests_status": "none",
        "tests_error": None,
    } | extra


def merge_request(
    iid: int = 12,
    author: str | None = "dev",
    approved: dict[str, Any] | None = None,
    ran: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "kind": "merge_request",
        "iid": iid,
        "title": f"Change {iid}",
        "web_path": f"/acme/web/-/merge_requests/{iid}",
        "author": author,
        "merged_by": "lead",
        "created_at": "2026-10-29T09:00:00Z",
        "merged_at": "2026-10-30T11:00:00Z",
        "target_branch": "main",
        "sha": f"{iid:040d}",
        "merge_commit_sha": f"m{iid:039d}",
        "squash_commit_sha": None,
        "approvals": approved if approved is not None else approvals("reviewer"),
        "pipeline": ran if ran is not None else pipeline(),
        "deployments": {"status": "ok", "items": []},
    } | extra


def direct_push(sha: str = "d" * 40, ran: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "kind": "direct_push",
        "sha": sha,
        "title": "Hotfix",
        "author": None,
        "author_name": "Dev",
        "committed_at": "2026-10-30T11:00:00Z",
        "branch": "main",
        "pipeline": ran if ran is not None else pipeline(),
    }


def deployment(sha: str, ref: str = "main", gitlab_id: int = 300) -> dict[str, Any]:
    return {
        "kind": "deployment",
        "id": gitlab_id,
        "iid": 3,
        "environment": "production",
        "status": "success",
        "ref": ref,
        "sha": sha,
        "created_at": "2026-10-31T08:00:00Z",
        "finished_at": "2026-10-31T09:00:00Z",
    }


# Review


def test_a_merge_request_approved_by_someone_else_passes() -> None:
    assert assess.review(merge_request()) == ("pass", "!12 was approved by reviewer")


def test_self_approval_fails_unless_allowed() -> None:
    change = merge_request(approved=approvals("dev"))

    assert assess.review(change)[0] == "fail"
    assert "approved by its author dev" in assess.review(change)[1]
    # Allowed, the author's own approval still does not count towards the minimum.
    assert assess.review(change, forbid_self_approval=False)[0] == "fail"
    assert assess.review(merge_request(approved=approvals("dev", "reviewer")), 1, False)[0] == (
        "pass"
    )


def test_too_few_approvals_fail() -> None:
    verdict, reason = assess.review(merge_request(approved=approvals("a")), min_approvals=2)

    assert verdict == "fail"
    assert reason == "!12 had 1 approval by someone other than its author; 2 required"
    assert assess.review(merge_request(approved=approvals()), 1)[0] == "fail"


@pytest.mark.parametrize("status", ["forbidden", "not_found", "failed", "not_supported"])
def test_approvals_that_could_not_be_read_are_unknown_never_a_pass(status: str) -> None:
    verdict, reason = assess.review(merge_request(approved=approvals(status=status)))

    assert verdict == "unknown"
    assert "approvals" in reason


def test_an_unknown_author_makes_review_unknown() -> None:
    assert assess.review(merge_request(author=None))[0] == "unknown"


def test_a_direct_push_fails_review_unless_the_policy_allows_it() -> None:
    assert assess.review(direct_push()) == (
        "fail",
        "commit dddddddd was pushed directly to the default branch, without review",
    )
    assert assess.review(direct_push(), allow_direct_push=True)[0] == "pass"


# Pipeline


def test_pipeline_outcomes() -> None:
    assert assess.pipeline(merge_request())[0] == "pass"
    assert assess.pipeline(merge_request(ran=pipeline("failed"))) == (
        "fail",
        "the pipeline of !12 ended failed",
    )
    assert assess.pipeline(merge_request(ran=pipeline(status="none"))) == (
        "fail",
        "no pipeline ran for !12",
    )
    assert assess.pipeline(merge_request(ran=pipeline(status="forbidden")))[0] == "unknown"
    assert assess.pipeline(merge_request(ran=pipeline(None)))[0] == "unknown"


def test_required_tests() -> None:
    clean = pipeline(
        tests={"total": 10, "success": 10, "failed": 0, "skipped": 0, "error": 0},
        tests_status="ok",
    )
    broken = pipeline(
        tests={"total": 10, "success": 9, "failed": 1, "skipped": 0, "error": 0},
        tests_status="ok",
    )

    assert assess.pipeline(merge_request(ran=clean), require_tests=True)[0] == "pass"
    assert assess.pipeline(merge_request(ran=broken), require_tests=True)[0] == "fail"
    assert assess.pipeline(merge_request(), require_tests=True) == (
        "fail",
        "the pipeline of !12 has no test report",
    )
    unread = pipeline(tests_status="forbidden", tests_error="403")
    assert assess.pipeline(merge_request(ran=unread), require_tests=True)[0] == "unknown"


def test_fail_outranks_unknown_outranks_pass() -> None:
    assert assess.combine([("pass", "a"), ("unknown", "b"), ("fail", "c")]) == ("fail", ["c"])
    assert assess.combine([("pass", "a"), ("unknown", "b")]) == ("unknown", ["b"])
    assert assess.combine([("pass", "a"), ("pass", "b")]) == ("pass", ["a", "b"])


def test_the_stored_outcome_uses_the_default_checks() -> None:
    assert assess.assess(merge_request())[0] == "pass"
    assert assess.assess(direct_push())[0] == "fail"
    assert assess.assess(merge_request(ran=pipeline(status="failed")))[0] == "unknown"


def test_a_failed_part_marks_the_change_incomplete() -> None:
    assert not assess.incomplete(merge_request())
    assert assess.incomplete(merge_request(ran=pipeline(status="failed")))
    # Forbidden does not pass with time: not read again each collection.
    assert not assess.incomplete(merge_request(approved=approvals(status="forbidden")))


def test_shas_cover_merge_squash_and_fast_forward() -> None:
    merged = merge_request()
    squashed = merge_request(merge_commit_sha=None, squash_commit_sha="s" * 40)
    fast_forward = merge_request(merge_commit_sha=None)

    assert assess.shas(merged) == [merged["merge_commit_sha"], merged["sha"]]
    assert assess.landed_sha(squashed) == "s" * 40
    assert assess.landed_sha(fast_forward) == fast_forward["sha"]


# Deployments


def test_a_deployment_of_a_reviewed_change_passes() -> None:
    change = merge_request()
    shas = dict.fromkeys(assess.shas(change), change)

    found, (verdict, reason) = assess.deployed_change(
        deployment(change["merge_commit_sha"]), shas, "main"
    )

    assert found is change
    assert (verdict, reason) == (
        "pass",
        "deployment 300 (m0000000) deployed !12, which was reviewed",
    )


def test_a_deployment_of_an_unreviewed_change_or_another_branch_fails() -> None:
    push = direct_push()
    shas = {push["sha"]: push}

    assert assess.deployed_change(deployment(push["sha"]), shas, "main")[1][0] == "fail"
    assert assess.deployed_change(deployment("f" * 40, ref="feature"), {}, "main")[1] == (
        "fail",
        "deployment 300 (ffffffff) deployed feature, not the default branch",
    )
    # A commit of the default branch Baluarte has not recorded: may predate the history.
    assert assess.deployed_change(deployment("f" * 40), {}, "main")[1][0] == "unknown"


# Rule values over a window


def entry(change_id: int, payload: dict[str, Any], days_ago: float = 1) -> ChangeEntry:
    return ChangeEntry(
        change_id=change_id,
        kind=payload["kind"],
        changed_at=NOW - timedelta(days=days_ago),
        observation_id=change_id * 10,
        payload_digest="0" * 64,
        payload=payload,
    )


READ = {
    "default_branch": "main",
    "history_since": "2026-01-01T00:00:00Z",
    "listed": {"merge_requests": "ok", "commits": "ok", "deployments": "ok"},
}
REVIEWED = {
    "window_days": 90,
    "min_approvals": 1,
    "forbid_self_approval": True,
    "allow_direct_push": False,
}
WINDOW = (NOW - timedelta(days=90), NOW)


def test_the_reviewed_rule_counts_and_names_failing_changes() -> None:
    entries = [
        entry(1, merge_request(1)),
        entry(2, merge_request(2, approved=approvals("dev"))),
        entry(3, direct_push()),
        entry(4, merge_request(4), days_ago=120),
    ]

    value = summarise("gitlab.change.reviewed", REVIEWED, READ, entries, WINDOW)

    assert value is not None
    assert (value["changes"], value["passed"], value["failed"], value["unknown"]) == (3, 1, 2, 0)
    assert [item["id"] for item in value["failing"]] == [2, 3]
    assert value["failing"][0] == {
        "id": 2,
        "kind": "merge_request",
        "iid": 2,
        "sha": f"{2:040d}",
        "title": "Change 2",
    }
    assert value["gaps"] == []


def test_no_changes_in_the_window_is_a_count_of_zero() -> None:
    value = summarise("gitlab.change.reviewed", REVIEWED, READ, [], WINDOW)

    assert value is not None
    assert value["changes"] == 0


def test_a_window_before_the_history_starts_has_no_value() -> None:
    late = READ | {"history_since": "2026-10-01T00:00:00Z"}

    assert summarise("gitlab.change.reviewed", REVIEWED, late, [], WINDOW) is None


def test_a_listing_that_failed_is_a_gap() -> None:
    partial = READ | {"listed": {"merge_requests": "ok", "commits": "failed", "deployments": "ok"}}

    value = summarise(
        "gitlab.change.reviewed", REVIEWED, partial, [entry(1, merge_request())], WINDOW
    )

    assert value is not None
    assert value["gaps"] == ["commits could not be listed (failed)"]


def test_the_deployment_rule_rests_on_the_deployed_changes_too() -> None:
    change = merge_request()
    entries = [entry(1, change, days_ago=200), entry(2, deployment(change["sha"]))]

    value = summarise(
        "gitlab.change.deployed_after_review", {"window_days": 90}, READ, entries, WINDOW
    )
    used = evidence_entries("gitlab.change.deployed_after_review", READ, entries, WINDOW)

    assert value is not None
    assert (value["changes"], value["passed"]) == (1, 1)
    assert [e.change_id for e in used] == [1, 2]


# Sampling


def test_the_sample_is_reproducible_and_sorted() -> None:
    population = list(range(1, 501))

    first = sampling.draw(population, 25, seed=42)
    again = sampling.draw(reversed(population), 25, seed=42)
    other = sampling.draw(population, 25, seed=43)

    assert first == again == sorted(first)
    assert len(first) == 25
    assert first != other
    assert sampling.draw([3, 1, 2], 10, seed=1) == [1, 2, 3]
    assert sampling.draw([], 5, seed=1) == []
