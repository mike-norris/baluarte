"""Time rules on closed issues: reading closed issues, building, approving and applying plans."""

from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient
from gitlab_fake import TOKEN, WRITE_TOKEN, settings
from sqlalchemy import Engine, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_exceptions import client_as
from test_labels import audit_actions, engine, gitlab_env, instance_id, people, require_review

from baluarte.api.v1.gitlab import gitlab_transport
from baluarte.auth.roles import Role
from baluarte.db.models import (
    ClosedIssue,
    CollectionJob,
    GitLabProject,
    IssuePlan,
    TimeCursor,
)
from baluarte.evidence.observations import Provenance
from baluarte.integrations.gitlab import closed_issues
from baluarte.integrations.gitlab.client import GitLabClient
from baluarte.integrations.gitlab.jobs import COLLECT_TIME
from baluarte.integrations.gitlab.project_reads import targets
from baluarte.jobs.worker import Worker
from baluarte.timetracking.issue_plans import (
    APPLY_ISSUE_PLAN,
    make_apply_issue_plan,
    rounded_estimate,
)

pytestmark = pytest.mark.db

# Fixtures shared with test_labels.
__all__ = ["engine", "gitlab_env", "people"]

PLANS = "/api/v1/issue-plans"
WEB, API = 100, 101
LABEL = "missing-time"


def issue(
    iid: int,
    *,
    spent: int = 0,
    estimate: int = 0,
    mrs: int = 0,
    labels: list[str] | None = None,
    state: str = "closed",
    updated_at: str = "2026-10-01T00:00:00Z",
) -> dict[str, Any]:
    return {
        "iid": iid,
        "state": state,
        "issue_type": "issue",
        "title": f"Issue {iid}",
        "web_url": f"https://gitlab.example.test/acme/-/issues/{iid}",
        "labels": labels or [],
        "time_stats": {"time_estimate": estimate, "total_time_spent": spent},
        "merge_requests_count": mrs,
        "closed_at": "2026-09-30T00:00:00Z" if state == "closed" else None,
        "updated_at": updated_at,
    }


class IssueServer:
    """A GitLab with issues, their system notes and the labels each project can use.

    Reads and the two issue writes change its state. ``fail`` answers the next request to a
    (method, path) with a status; ``ignore_writes`` accepts writes without changing anything.
    """

    def __init__(self) -> None:
        self.issues: dict[int, dict[int, dict[str, Any]]] = {WEB: {}, API: {}}
        self.notes: dict[tuple[int, int], list[str]] = {}
        self.labels: dict[int, set[str]] = {WEB: {LABEL}, API: set()}
        self.requests: list[tuple[str, str, str | None]] = []
        self.failures: dict[tuple[str, str], int] = {}
        self.ignore_writes = False

    def add(self, project: int, *items: dict[str, Any]) -> None:
        for item in items:
            self.issues[project][item["iid"]] = item

    def fail(self, method: str, path: str, status: int) -> None:
        self.failures[(method, "/api/v4" + path)] = status

    def writes(self) -> list[tuple[str, str, str | None]]:
        return [r for r in self.requests if r[0] != "GET"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        token = request.headers.get("PRIVATE-TOKEN")
        self.requests.append((request.method, path, token))
        status = self.failures.pop((request.method, path), None)
        if status is not None:
            return httpx.Response(status, json={"message": "refused"})
        if path == "/api/v4/personal_access_tokens/self":
            return httpx.Response(
                200,
                json={
                    "id": 7,
                    "name": "writer",
                    "scopes": ["api"],
                    "active": True,
                    "revoked": False,
                    "expires_at": "2027-01-01",
                },
            )
        parts = path.removeprefix("/api/v4/projects/").split("/")
        project = int(parts[0])
        if project not in self.issues:
            return httpx.Response(404, json={"message": "404 Project Not Found"})
        found = self.issues[project]
        if parts[1:] == ["labels"]:
            name = request.url.params.get("search", "")
            return httpx.Response(
                200, json=[{"id": 1, "name": n} for n in sorted(self.labels[project]) if name in n]
            )
        if parts[1:] == ["issues"]:
            items = list(found.values())
            if request.url.params.get("state") == "closed":
                items = [i for i in items if i["state"] == "closed"]
            return httpx.Response(200, json=items)
        iid = int(parts[2])
        if iid not in found:
            return httpx.Response(404, json={"message": "404 Not found"})
        item = found[iid]
        if parts[3:] == ["notes"]:
            notes = [{"system": True, "body": b} for b in self.notes.get((project, iid), [])]
            return httpx.Response(200, json=notes)
        if request.method == "GET" and len(parts) == 3:
            return httpx.Response(200, json=item)
        form = parse_qs(request.content.decode())
        if request.method == "PUT" and len(parts) == 3:
            if not self.ignore_writes:
                for name in form.get("add_labels", []):
                    item["labels"] = sorted({*item["labels"], name})
                for name in form.get("remove_labels", []):
                    item["labels"] = [label for label in item["labels"] if label != name]
            return httpx.Response(200, json=item)
        if request.method == "POST" and parts[3:] == ["time_estimate"]:
            minutes = int(form["duration"][0].removesuffix("m"))
            if not self.ignore_writes:
                item["time_stats"]["time_estimate"] = minutes * 60
            return httpx.Response(200, json=item["time_stats"])
        return httpx.Response(404, json={"message": "404 Not found"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def server() -> IssueServer:
    found = IssueServer()
    found.add(
        WEB,
        issue(1, mrs=1),  # commits by merge request, no time: add the label
        issue(2),  # a commit mentions it, no time: add the label
        issue(3),  # no commits: nothing
        issue(4, spent=600, labels=[LABEL]),  # time logged since: remove, and estimate 30m
        issue(5, spent=5400, estimate=3600),  # has both: nothing
        issue(6, spent=1, mrs=1, state="opened"),  # open: never read as closed
    )
    found.notes[(WEB, 2)] = ["mentioned in commit abc123"]
    found.notes[(WEB, 3)] = ["changed the description"]
    found.add(API, issue(1, mrs=2), issue(2, spent=3700))  # the label is not defined in api
    return found


def read_closed(engine: Engine, server: IssueServer) -> closed_issues.ClosedTally:
    tally = closed_issues.ClosedTally()
    with GitLabClient(settings(), transport=server.transport) as client:
        for target in targets(engine, instance_id(engine)):
            with Session(engine) as db:
                known = closed_issues.stored_commits(db, target.row_id)
                cursor = closed_issues.cursor_of(db, target.row_id)
            read = closed_issues.read_project(client, target, cursor, known)
            with Session(engine) as db:
                closed_issues.write(db, instance_id(engine), read, Provenance(), tally)
                db.commit()
    return tally


@pytest.fixture
def collected(engine: Engine, server: IssueServer) -> Engine:
    with Session(engine) as db:
        for project in db.scalars(select(GitLabProject.id)):
            db.add(TimeCursor(project_id=project, history_since=datetime(2026, 1, 1, tzinfo=UTC)))
        db.commit()
    read_closed(engine, server)
    return engine


def stored(engine: Engine) -> dict[tuple[str, int], ClosedIssue]:
    with Session(engine) as db:
        rows = db.execute(
            select(ClosedIssue, GitLabProject.path_with_namespace).join(
                GitLabProject, GitLabProject.id == ClosedIssue.project_id
            )
        ).all()
        db.expunge_all()
        return {(path, row.issue_iid): row for row, path in rows}


def as_user(engine: Engine, user_id: int, server: IssueServer, *roles: Role) -> TestClient:
    client = client_as(engine, user_id, *roles)
    client.app.dependency_overrides[gitlab_transport] = lambda: server.transport  # type: ignore[attr-defined]
    return client


@pytest.fixture
def ana(collected: Engine, people: dict[str, int], server: IssueServer) -> TestClient:
    return as_user(collected, people["ana"], server, Role.ADMIN)


@pytest.fixture
def bea(collected: Engine, people: dict[str, int], server: IssueServer) -> TestClient:
    return as_user(collected, people["bea"], server, Role.ADMIN)


def writes_on(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    monkeypatch.setenv("BALUARTE_ISSUE_WRITES", "true")
    saved = client.put("/api/v1/settings/label-write-token", json={"token": WRITE_TOKEN})
    assert saved.status_code == 200, saved.text


def approved_plan(client: TestClient, **body: Any) -> dict[str, Any]:
    created = client.post(PLANS, json=body)
    assert created.status_code == 201, created.text
    submitted = client.post(f"{PLANS}/{created.json()['id']}/submit")
    assert submitted.json()["state"] == "approved", submitted.text
    return submitted.json()


def apply(engine: Engine, server: IssueServer, client: TestClient, plan_id: int) -> dict[str, Any]:
    started = client.post(f"{PLANS}/{plan_id}/apply")
    assert started.status_code == 202, started.text
    assert Worker(engine, {APPLY_ISSUE_PLAN: make_apply_issue_plan(server.transport)}).run_once()
    return client.get(f"{PLANS}/{plan_id}").json()


def steps(plan: dict[str, Any]) -> list[tuple[str, str, int, str]]:
    return [
        (s["action"], s["project"]["path"], s["issue"]["iid"], s["status"])
        for s in plan["step_items"]
    ]


# --- Rounding and reading ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spent", "minutes", "expected"),
    [(600, 30, 1800), (1, 30, 1800), (1800, 30, 1800), (1801, 30, 3600), (5400, 60, 7200)],
)
def test_time_spent_rounds_up_to_the_interval(spent: int, minutes: int, expected: int) -> None:
    assert rounded_estimate(spent, minutes) == expected


def test_closed_issues_are_read_with_get_and_commits_checked_only_without_time(
    collected: Engine, server: IssueServer
) -> None:
    assert all(method == "GET" for method, _, _ in server.requests)
    found = stored(collected)
    assert set(found) == {
        ("acme/team/web", 1),
        ("acme/team/web", 2),
        ("acme/team/web", 3),
        ("acme/team/web", 4),
        ("acme/team/web", 5),
        ("acme/api", 1),
        ("acme/api", 2),
    }
    commits = {key: row.commits for key, row in found.items()}
    assert commits[("acme/team/web", 1)] == "merge_request"
    assert commits[("acme/team/web", 2)] == "mention"
    assert commits[("acme/team/web", 3)] == "none"
    assert commits[("acme/team/web", 4)] == "not_checked"
    notes = [p for _, p, _ in server.requests if p.endswith("/notes")]
    # Only issues with no time and no merge request need their notes read.
    assert sorted(notes) == [
        "/api/v4/projects/100/issues/2/notes",
        "/api/v4/projects/100/issues/3/notes",
    ]


def test_later_reads_follow_updates_and_drop_reopened_issues(
    collected: Engine, server: IssueServer
) -> None:
    server.requests.clear()
    server.add(WEB, issue(3, state="opened", updated_at="2026-10-02T00:00:00Z"))
    server.add(WEB, issue(7, mrs=1, updated_at="2026-10-02T00:00:00Z"))
    read_closed(collected, server)
    found = stored(collected)
    assert ("acme/team/web", 3) not in found
    assert found[("acme/team/web", 7)].commits == "merge_request"
    # A later read lists issues updated since the last one, in any state.
    lists = [r for r in server.requests if r[1] == "/api/v4/projects/100/issues"]
    assert lists
    # Notes already read for an unchanged issue are not read again.
    assert not [p for _, p, _ in server.requests if p.endswith("/issues/2/notes")]


# --- Preview and building ----------------------------------------------------------------------


def test_the_preview_counts_what_a_plan_would_change(ana: TestClient) -> None:
    found = ana.get(f"{PLANS}/preview")
    assert found.status_code == 200, found.text
    body = found.json()
    assert (body["add_label"], body["remove_label"], body["set_estimate"]) == (3, 1, 2)
    assert body["rules"] == {
        "missing_time_label": LABEL,
        "estimate_round_minutes": 30,
        "issue_types": ["issue", "task"],
    }
    assert body["writes_enabled"] is False
    scoped = ana.get(f"{PLANS}/preview", params={"group": "acme/api"}).json()
    assert (scoped["add_label"], scoped["set_estimate"]) == (1, 1)


def test_a_plan_adds_the_label_only_where_it_is_defined(
    ana: TestClient, server: IssueServer
) -> None:
    created = ana.post(PLANS, json={})
    assert created.status_code == 201, created.text
    plan = created.json()
    assert plan["state"] == "draft" and plan["steps"] == 5
    assert steps(plan) == [
        ("set_estimate", "acme/api", 2, "pending"),
        ("add_label", "acme/team/web", 1, "pending"),
        ("add_label", "acme/team/web", 2, "pending"),
        ("remove_label", "acme/team/web", 4, "pending"),
        ("set_estimate", "acme/team/web", 4, "pending"),
    ]
    by_issue = {
        (s["project"]["path"], s["issue"]["iid"], s["action"]): s for s in plan["step_items"]
    }
    assert by_issue[("acme/team/web", 4, "set_estimate")]["intended"] == {"estimate_seconds": 1800}
    assert by_issue[("acme/api", 2, "set_estimate")]["intended"] == {"estimate_seconds": 5400}
    [manual] = plan["manual_items"]
    assert (manual["reason"], manual["project"]["path"], manual["issues"]) == (
        "label_undefined",
        "acme/api",
        [1],
    )
    # Building reads labels with the read token only.
    assert {(m, t) for m, _, t in server.requests} == {("GET", TOKEN)}


def test_a_plan_needs_something_to_change(ana: TestClient) -> None:
    refused = ana.post(PLANS, json={"scope": {"groups": ["other"]}})
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "nothing_to_plan"


# --- Review ------------------------------------------------------------------------------------


def test_approval_is_by_someone_else_and_bound_to_the_digest(
    collected: Engine, ana: TestClient, bea: TestClient
) -> None:
    require_review(collected)
    plan = ana.post(PLANS, json={}).json()
    assert ana.post(f"{PLANS}/{plan['id']}/submit").json()["state"] == "submitted"
    own = ana.post(f"{PLANS}/{plan['id']}/approve", json={"plan_digest": plan["plan_digest"]})
    assert own.status_code == 403
    wrong = bea.post(f"{PLANS}/{plan['id']}/approve", json={"plan_digest": "0" * 64})
    assert wrong.json()["detail"]["code"] == "digest_mismatch"
    approved = bea.post(f"{PLANS}/{plan['id']}/approve", json={"plan_digest": plan["plan_digest"]})
    assert approved.json()["state"] == "approved" and approved.json()["self_approved"] is False
    assert [a for a, _ in audit_actions(collected, "issues.plan")] == [
        "issues.plan_created",
        "issues.plan_submitted",
        "issues.plan_approved",
    ]


def test_the_database_keeps_a_plan_unchanged(collected: Engine, ana: TestClient) -> None:
    plan_id = ana.post(PLANS, json={}).json()["id"]
    with Session(collected) as db, pytest.raises(DBAPIError, match="cannot be rewritten"):
        row = db.get(IssuePlan, plan_id)
        assert row is not None
        row.steps = 1
        db.commit()


def test_applying_is_refused_while_writes_are_off(
    collected: Engine, ana: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = approved_plan(ana)
    assert plan["can_apply"] == {
        "allowed": False,
        "reasons": ["issue_writes_disabled", "write_token_missing"],
    }
    refused = ana.post(f"{PLANS}/{plan['id']}/apply")
    assert refused.status_code == 409
    assert refused.json()["detail"]["reasons"] == ["issue_writes_disabled", "write_token_missing"]
    monkeypatch.setenv("BALUARTE_LABEL_WRITES", "true")  # label writes do not allow issue writes
    saved = ana.put("/api/v1/settings/label-write-token", json={"token": WRITE_TOKEN})
    assert saved.status_code == 200
    assert ana.post(f"{PLANS}/{plan['id']}/apply").json()["detail"]["reasons"] == [
        "issue_writes_disabled"
    ]


# --- Applying ----------------------------------------------------------------------------------


def test_applying_writes_once_with_the_write_token_and_reads_back(
    monkeypatch: pytest.MonkeyPatch, collected: Engine, ana: TestClient, server: IssueServer
) -> None:
    writes_on(monkeypatch, ana)
    plan_id = approved_plan(ana)["id"]
    server.requests.clear()

    plan = apply(collected, server, ana, plan_id)

    assert plan["state"] == "applied", plan["failure"]
    assert {status for *_, status in steps(plan)} == {"applied"}
    assert sorted((m, p) for m, p, _ in server.writes()) == [
        ("POST", "/api/v4/projects/100/issues/4/time_estimate"),
        ("POST", "/api/v4/projects/101/issues/2/time_estimate"),
        ("PUT", "/api/v4/projects/100/issues/1"),
        ("PUT", "/api/v4/projects/100/issues/2"),
        ("PUT", "/api/v4/projects/100/issues/4"),
    ]
    assert {token for method, _, token in server.requests if method != "GET"} == {WRITE_TOKEN}
    assert {token for method, _, token in server.requests if method == "GET"} == {TOKEN}
    web = server.issues[WEB]
    assert web[1]["labels"] == [LABEL] and web[2]["labels"] == [LABEL]
    assert web[4]["labels"] == [] and web[4]["time_stats"]["time_estimate"] == 1800
    assert server.issues[API][2]["time_stats"]["time_estimate"] == 5400
    assert web[5]["time_stats"]["time_estimate"] == 3600  # an estimate is never changed
    writes = [details for _, details in audit_actions(collected, "issues.write")]
    assert len(writes) == 5 and all((w or {})["status"] == 200 for w in writes)
    assert [a for a, _ in audit_actions(collected, "issues.plan")][-1] == "issues.plan_applied"
    with Session(collected) as db:
        assert db.scalars(select(CollectionJob).where(CollectionJob.kind == COLLECT_TIME)).all()


def test_an_issue_that_changed_since_the_plan_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, collected: Engine, ana: TestClient, server: IssueServer
) -> None:
    writes_on(monkeypatch, ana)
    plan_id = approved_plan(ana)["id"]
    web = server.issues[WEB]
    web[1]["time_stats"]["total_time_spent"] = 900  # time logged: no label needed now
    web[2]["labels"] = [LABEL]  # already labelled by someone: skipped
    web[4]["time_stats"]["time_estimate"] = 7200  # estimated meanwhile: never changed
    server.labels[WEB] = set()  # nothing to stop the other steps

    plan = apply(collected, server, ana, plan_id)

    assert plan["state"] == "applied"
    result = {(a, p, i): s for a, p, i, s in steps(plan)}
    assert result[("add_label", "acme/team/web", 1)] == "stale"
    assert result[("add_label", "acme/team/web", 2)] == "skipped"
    assert result[("set_estimate", "acme/team/web", 4)] == "stale"
    assert result[("remove_label", "acme/team/web", 4)] == "applied"
    assert web[4]["time_stats"]["time_estimate"] == 7200


def test_a_label_no_longer_defined_is_not_added(
    monkeypatch: pytest.MonkeyPatch, collected: Engine, ana: TestClient, server: IssueServer
) -> None:
    writes_on(monkeypatch, ana)
    plan_id = approved_plan(ana)["id"]
    server.labels[WEB] = set()
    plan = apply(collected, server, ana, plan_id)
    result = {(a, i): s for a, p, i, s in steps(plan) if p == "acme/team/web"}
    assert result[("add_label", 1)] == result[("add_label", 2)] == "stale"
    assert not [p for m, p, _ in server.writes() if p == "/api/v4/projects/100/issues/1"]


def test_the_plan_stops_at_a_refused_write_without_retrying(
    monkeypatch: pytest.MonkeyPatch, collected: Engine, ana: TestClient, server: IssueServer
) -> None:
    writes_on(monkeypatch, ana)
    plan = approved_plan(ana)
    first = plan["step_items"][0]
    assert first["action"] == "set_estimate"
    server.fail(
        "POST",
        f"/projects/{first['project']['gitlab_id']}/issues/{first['issue']['iid']}/time_estimate",
        403,
    )
    done = apply(collected, server, ana, plan["id"])
    assert done["state"] == "failed" and done["failed_step"] == first["step"]
    assert [s["status"] for s in done["step_items"]] == ["failed"] + ["not_run"] * 4
    assert len(server.writes()) == 1


def test_the_plan_stops_when_a_write_does_not_read_back(
    monkeypatch: pytest.MonkeyPatch, collected: Engine, ana: TestClient, server: IssueServer
) -> None:
    writes_on(monkeypatch, ana)
    plan_id = approved_plan(ana)["id"]
    server.ignore_writes = True
    done = apply(collected, server, ana, plan_id)
    assert done["state"] == "failed"
    assert done["step_items"][0]["status"] == "failed"
    assert done["step_items"][0]["verified"] is False


def test_the_applier_stops_if_writes_are_turned_off_after_it_was_queued(
    monkeypatch: pytest.MonkeyPatch, collected: Engine, ana: TestClient, server: IssueServer
) -> None:
    writes_on(monkeypatch, ana)
    plan_id = approved_plan(ana)["id"]
    assert ana.post(f"{PLANS}/{plan_id}/apply").status_code == 202
    monkeypatch.setenv("BALUARTE_ISSUE_WRITES", "false")
    assert Worker(collected, {APPLY_ISSUE_PLAN: make_apply_issue_plan(server.transport)}).run_once()
    done = ana.get(f"{PLANS}/{plan_id}").json()
    assert done["state"] == "failed" and "BALUARTE_ISSUE_WRITES" in done["failure"]
    assert server.writes() == []
