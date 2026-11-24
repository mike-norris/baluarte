"""GitHub issues in the auditable issue register (roadmap M13.5).

GitHub is scripted (``test_github_changes.organization``); nothing here reaches github.com.
"""

from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from test_connections import environment as environment  # the fixture
from test_github import add, collect
from test_github_changes import organization
from test_issue_register import add_rule, client_as, register

from baluarte.auth.roles import Role
from baluarte.db.models import CollectionJob, CollectionRun, IssueEvidence, Observation
from baluarte.integrations.github.jobs import COLLECT_ISSUES, make_collect_issues
from baluarte.jobs.worker import Worker
from baluarte.register.mapping import items

pytestmark = pytest.mark.db

ISSUES = "GET /repos/acme/api/issues"


def gh_issue(number: int, labels: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "id": 5000 + number,
        "number": number,
        "title": f"Issue {number}",
        "state": "open",
        "labels": [{"name": name} for name in labels or []],
        "user": {"login": "ada"},
        "assignees": [{"login": "lin"}],
        "milestone": None,
        "html_url": f"https://github.com/acme/api/issues/{number}",
        "created_at": "2026-11-01T10:00:00Z",
        "updated_at": "2026-11-02T10:00:00Z",
        "closed_at": None,
        **extra,
    }


def a_pull_request(number: int) -> dict[str, Any]:
    return {**gh_issue(number), "pull_request": {"url": "…"}}


def run_issues(engine: Engine, fake: Any) -> None:
    assert Worker(engine, {COLLECT_ISSUES: make_collect_issues(fake.transport)}).run_once()


def test_issues_join_the_register_without_pull_requests(
    migrated_engine: Engine, environment: None
) -> None:
    fake = organization()
    fake.routes[ISSUES] = [
        gh_issue(1, ["security"]),
        a_pull_request(2),
        gh_issue(
            3, ["docs"], state="closed", closed_at="2026-11-03T10:00:00Z", type={"name": "Task"}
        ),
    ]
    connection_id = int(add(migrated_engine).json()["id"])
    # Without a register nothing is listed, and no issues run is recorded.
    collect(migrated_engine, fake, connection_id)
    assert not [r for r in fake.requests if r.url.path == "/repos/acme/api/issues"]
    add_rule(migrated_engine, labels=["security", "docs"])
    with Session(migrated_engine) as db:
        queued = db.scalars(
            select(CollectionJob).where(
                CollectionJob.kind == COLLECT_ISSUES, CollectionJob.state == "queued"
            )
        ).all()
    assert len(queued) == 1
    fake.routes["GET /repos/acme/api/issues/1"] = gh_issue(1, ["security"])
    fake.routes["GET /repos/acme/api/issues/3"] = gh_issue(3, ["docs"], state="closed")
    run_issues(migrated_engine, fake)

    listed = {issue["iid"]: issue for issue in register(migrated_engine)["items"]}
    assert set(listed) == {1, 3}
    assert listed[1]["state"] == "opened" and listed[3]["state"] == "closed"
    assert listed[3]["issue_type"] == "task" and listed[1]["issue_type"] == "issue"
    assert listed[1]["web_url"] == "https://github.com/acme/api/issues/1"
    first = [r for r in fake.requests if r.url.path == "/repos/acme/api/issues"][-1]
    assert first.url.params["state"] == "all" and "since" not in first.url.params

    # The next read lists only what changed since, with a little overlap.
    fake.requests.clear()
    run_again = client_as(migrated_engine, Role.AUDITOR).post("/api/v1/register/collect")
    assert run_again.status_code in (200, 202), run_again.text
    run_issues(migrated_engine, fake)
    later = [r for r in fake.requests if r.url.path == "/repos/acme/api/issues"][-1]
    assert later.url.params["since"] == "2026-11-02T09:55:00Z"
    assert {r.method for r in fake.requests} == {"GET"}
    with Session(migrated_engine) as db:
        runs = db.scalars(select(CollectionRun).where(CollectionRun.kind == "issues")).all()
    assert runs and all(run.status == "succeeded" for run in runs)


def evidence_routes(fake: Any) -> None:
    fake.routes[ISSUES] = [gh_issue(7, ["security"])]
    fake.routes["GET /repos/acme/api/issues/7"] = gh_issue(
        7,
        ["security"],
        state="closed",
        closed_at="2026-11-05T10:00:00Z",
        closed_by={"login": "lin"},
        body="Steps: see https://github.com/user-attachments/assets/0b1c-screenshot",
    )
    fake.routes["GET /repos/acme/api/issues/7/comments"] = [
        {
            "id": 71,
            "user": {"login": "lin"},
            "body": "Fixed in #8",
            "created_at": "2026-11-04T10:00:00Z",
        }
    ]
    fake.routes["GET /repos/acme/api/issues/7/timeline"] = [
        {"id": 81, "event": "labeled", "actor": {"login": "ada"}, "label": {"name": "security"},
         "created_at": "2026-11-01T11:00:00Z"},
        {"event": "commented", "id": 71, "body": "Fixed in #8"},
        {"event": "cross-referenced", "created_at": "2026-11-04T09:00:00Z",
         "source": {"type": "issue", "issue": {
             "number": 8, "title": "Fix the leak", "state": "closed", "pull_request": {},
             "html_url": "https://github.com/acme/api/pull/8",
             "repository": {"id": 1, "full_name": "acme/api"}}}},
        {"event": "cross-referenced", "created_at": "2026-11-04T09:30:00Z",
         "source": {"type": "issue", "issue": {
             "number": 9, "title": "Follow-up", "state": "open",
             "html_url": "https://github.com/acme/api/issues/9",
             "repository": {"id": 1, "full_name": "acme/api"}}}},
        {"id": 82, "event": "closed", "actor": {"login": "lin"}, "commit_id": "merge8",
         "created_at": "2026-11-05T10:00:00Z"},
    ]  # fmt: skip
    fake.routes["GET /repos/acme/api/pulls/8"] = {
        "number": 8,
        "title": "Fix the leak",
        "state": "closed",
        "html_url": "https://github.com/acme/api/pull/8",
        "user": {"login": "lin"},
        "merged_by": {"login": "lin"},
        "created_at": "2026-11-04T08:00:00Z",
        "merged_at": "2026-11-05T09:59:00Z",
        "merge_commit_sha": "merge8",
        "head": {"ref": "fix-leak", "sha": "head8"},
        "base": {"ref": "main"},
        "requested_reviewers": [],
    }
    fake.routes["GET /repos/acme/api/pulls/8/reviews"] = [
        {"user": {"login": "ada"}, "state": "APPROVED", "submitted_at": "2026-11-05T09:00:00Z"}
    ]
    fake.routes["GET /repos/acme/api/actions/runs"] = {
        "workflow_runs": [
            {"id": 900, "workflow_id": 1, "name": "ci", "status": "completed",
             "conclusion": "success", "head_sha": "head8", "head_branch": "fix-leak",
             "created_at": "2026-11-04T08:05:00Z"},
        ]
    }  # fmt: skip


def test_an_auditable_issue_is_captured_with_its_pull_requests(
    migrated_engine: Engine, environment: None
) -> None:
    fake = organization()
    evidence_routes(fake)
    connection_id = int(add(migrated_engine).json()["id"])
    collect(migrated_engine, fake, connection_id)
    add_rule(migrated_engine, labels=["security"])
    fake.requests.clear()
    run_issues(migrated_engine, fake)
    assert {r.method for r in fake.requests} == {"GET"}

    with Session(migrated_engine) as db:
        evidence = db.scalars(select(IssueEvidence)).one()
        capture = db.get(Observation, evidence.observation_id)
    assert capture is not None and capture.payload is not None
    payload = capture.payload
    assert payload["provider"] == "github"
    assert payload["issue"]["closed_by"] == "lin"
    assert [n["body"] for n in payload["notes"]] == ["Fixed in #8"]
    assert [(e["kind"], e["action"], e["state"]) for e in payload["history"]] == [
        ("label", "add", None),
        ("state", None, "closed"),
    ]
    assert [link["iid"] for link in payload["links"]] == [9]
    (pull,) = payload["merge_requests"]
    assert (pull["iid"], pull["relation"], pull["state"]) == (8, "closes", "merged")
    assert pull["approvals"]["approved_by"] == ["ada"]
    assert [p["status"] for p in pull["pipelines"]] == ["success"]
    assert payload["sources"]["releases"] == "not_applicable"
    # A file attached on GitHub is a gap, not left out silently.
    assert evidence.status == "partial"
    assert any(gap.startswith("attachments:") for gap in evidence.gaps)

    labels = {item.kind: item.label for item in items(payload).values()}
    assert labels["merge_request"] == "Pull request #8 (merged, approved by ada)"
    assert labels["pipeline"] == "Workflow run 900 of #8: success"


def test_a_refused_issue_read_is_recorded_and_keeps_what_was_stored(
    migrated_engine: Engine, environment: None
) -> None:
    fake = organization()
    fake.routes[ISSUES] = [gh_issue(1, ["security"])]
    connection_id = int(add(migrated_engine).json()["id"])
    collect(migrated_engine, fake, connection_id)
    add_rule(migrated_engine, labels=["security"])
    fake.routes["GET /repos/acme/api/issues/1"] = gh_issue(1, ["security"])
    run_issues(migrated_engine, fake)
    fake.routes[ISSUES] = lambda _: httpx.Response(403, json={"message": "Resource not accessible"})
    client_as(migrated_engine, Role.AUDITOR).post("/api/v1/register/collect")
    run_issues(migrated_engine, fake)
    with Session(migrated_engine) as db:
        latest = db.scalars(
            select(CollectionRun)
            .where(CollectionRun.kind == "issues")
            .order_by(CollectionRun.id.desc())
        ).first()
    assert latest is not None and latest.status == "failed"
    assert [issue["iid"] for issue in register(migrated_engine)["items"]] == [1]
