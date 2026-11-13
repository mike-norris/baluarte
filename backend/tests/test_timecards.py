"""Timecards: display names, issue context and the timecard with its downloads."""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from gitlab_fake import FakeGitLab, timelog, timelogs_graphql, work_item, work_items_graphql
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from test_exceptions import user
from test_manager_space import ADMIN, as_user, audit_actions, build, configure_gitlab, instance_id
from test_time_tracking import ago, collect, entries, server

from baluarte.auth.roles import Role
from baluarte.db.models import IssueContext, Observation
from baluarte.integrations.gitlab import timelogs
from baluarte.manager import scopes

pytestmark = pytest.mark.db

SPRINT = {
    "title": None,
    "startDate": "2026-11-02",
    "dueDate": "2026-11-15",
    "iterationCadence": {"title": "Sprints"},
}
PORTAL = ("Epic", "acme&1", "Customer portal")
SIGN_IN = ("Epic", "acme&3", "Sign-in")
LOGIN = ("Issue", "acme/team/web#7", "Login form")
LOGS = {
    "acme/team/web": [
        timelog(1, ago(3), 3600, "dev1", issue=7, milestone="M1", name="Dana One"),
        timelog(2, ago(2), 1800, "dev2", issue=8, name="Dev Two"),
        timelog(3, ago(10), 600, "dev1", merge_request=4, name="Dana One"),
        # Time taken back in GitLab with /spend -15m.
        timelog(4, ago(1), -900, "dev1", issue=7, name="Dana One"),
    ],
    "acme/api": [timelog(5, ago(1), 1200, "dev3", issue=1)],
}
CONTEXTS = {
    "acme/team/web": [
        work_item(
            7,
            "Login form",
            parent=SIGN_IN,
            ancestors=[PORTAL, SIGN_IN],
            milestone="M2",
            iteration=SPRINT,
            estimate=7200,
        ),
        work_item(8, "=HYPERLINK(evil)", parent=LOGIN, ancestors=[PORTAL, LOGIN], kind="Task"),
    ],
    "acme/api": [work_item(1, "<script>alert(1)</script>")],
}


@pytest.fixture(autouse=True)
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    configure_gitlab(monkeypatch, tmp_path)


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    return build(migrated_engine)


def gitlab(logs: dict[str, list[dict[str, Any]]] = LOGS, **options: Any) -> FakeGitLab:
    fake = server(logs, contexts=CONTEXTS, **options)
    fake.json("/users", body=[{"username": "dev3", "name": "Dee Three"}])
    return fake


@pytest.fixture
def carded(engine: Engine) -> Engine:
    collect(engine, gitlab())
    return engine


def context(engine: Engine, iid: int) -> IssueContext:
    with Session(engine) as db:
        found = db.scalars(select(IssueContext).where(IssueContext.issue_iid == iid)).one()
        db.expunge_all()
        return found


def work_item_queries(fake: FakeGitLab) -> list[dict[str, Any]]:
    return [
        json.loads(r.url.params["variables"])
        for r in fake.requests
        if "workItems(" in r.url.params.get("query", "")
    ]


def card(client: Any, **params: Any) -> dict[str, Any]:
    response = client.get("/api/v1/time/timecard", params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def by_log(body: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    found: dict[str, list[dict[str, Any]]] = {}
    for row in body["rows"]:
        found.setdefault(row["timelog_id"].rsplit("/", 1)[-1], []).append(row)
    return found


# --- Collection --------------------------------------------------------------------------------


def test_collection_reads_display_names_and_issue_context(engine: Engine) -> None:
    fake = gitlab()
    result = collect(engine, fake)
    assert all(request.method == "GET" for request in fake.requests)
    assert result.contexts == {"ok": 2} and result.names == {"ok": 1}
    login = context(engine, 7)
    assert (login.work_item_type, login.title, login.milestone) == ("Issue", "Login form", "M2")
    assert (login.parent_type, login.parent_reference, login.parent_title) == SIGN_IN
    assert (login.top_type, login.top_reference, login.top_title) == PORTAL
    assert (login.iteration_status, login.iteration) == ("ok", "Sprints: 2026-11-02 to 2026-11-15")
    assert login.estimate_seconds == 7200
    task = context(engine, 8)
    assert (task.work_item_type, task.parent_reference, task.top_title) == (
        "Task",
        "acme/team/web#7",
        "Customer portal",
    )
    found = entries(engine)
    assert {key: row.user_name for key, row in found.items()} == {
        "1": "Dana One",
        "2": "Dev Two",
        "3": "Dana One",
        "4": "Dana One",
        "5": "Dee Three",
    }
    with Session(engine) as db:
        sources = set(db.scalars(select(Observation.source)))
    assert timelogs.CONTEXT_SOURCE in sources
    # Read again at once, the context is recent enough: no work item query is sent.
    again = gitlab()
    collect(engine, again)
    assert work_item_queries(again) == []


def test_an_updated_issue_has_its_context_read_again(carded: Engine) -> None:
    fake = gitlab()
    fake.json(
        "/projects/100/issues",
        body=[{"iid": 7, "time_stats": {"time_estimate": 7200, "total_time_spent": 2700}}],
    )
    collect(carded, fake)
    assert [q["iids"] for q in work_item_queries(fake) if q["path"] == "acme/team/web"] == [["7"]]


def test_without_iterations_the_context_is_read_and_iterations_are_unsupported(
    engine: Engine,
) -> None:
    result = collect(engine, gitlab(iterations=False))
    assert result.contexts == {"ok": 2}
    login = context(engine, 7)
    assert (login.iteration_status, login.iteration) == ("unsupported", None)
    assert login.top_title == "Customer portal"


def test_without_work_items_the_context_stays_unknown(engine: Engine) -> None:
    fake = FakeGitLab({})
    fake.graphql(timelogs_graphql(LOGS, fallback=work_items_graphql({}, available=False)))
    for gitlab_id in (100, 101, 102, 200):
        fake.json(f"/projects/{gitlab_id}/issues", body=[])
    result = collect(engine, fake)
    assert result.contexts == {"unsupported": 2}
    with Session(engine) as db:
        assert list(db.scalars(select(IssueContext))) == []
        statuses = set(
            db.scalars(
                select(Observation.collection_status).where(
                    Observation.source == timelogs.CONTEXT_SOURCE
                )
            )
        )
    assert statuses == {"unsupported"}


# --- The timecard ------------------------------------------------------------------------------


def test_timecard_rows_carry_the_developer_and_the_issue_context(carded: Engine) -> None:
    admin = as_user(carded, user(carded, "admin"), Role.ADMIN)
    body = card(admin)
    rows = by_log(body)
    first = rows["1"][0]
    assert first["developer"] == "Dana One" and first["username"] == "dev1"
    assert first["minutes"] == 60 and first["kind"] == "logged"
    assert first["work_item"] == "#7" and first["title"] == "Login form"
    assert first["top"] == {"title": "Customer portal", "reference": "acme&1"}
    assert first["parent"] == {"title": "Sign-in", "reference": "acme&3"}
    # The issue's milestone now, not the one recorded with the log.
    assert first["milestone"] == "M2"
    assert first["iteration"] == "Sprints: 2026-11-02 to 2026-11-15"
    assert first["estimate_minutes"] == 120 and first["context_read"] is True
    assert rows["4"][0]["minutes"] == -15
    merge_request = rows["3"][0]
    assert merge_request["work_item"] == "!4"
    assert merge_request["top"] is None and merge_request["estimate_minutes"] is None
    # No estimate in GitLab is shown as none, not as zero minutes.
    assert rows["2"][0]["estimate_minutes"] is None
    assert rows["5"][0]["developer"] == "Dee Three"
    assert body["totals"] == {
        "minutes": 105,
        "logged_minutes": 105,
        "removed_minutes": 0,
        "rows": 5,
        "people": 3,
    }
    assert body["per_person"] == {"Dana One": 55, "Dee Three": 20, "Dev Two": 30}
    assert body["sort"] == ["date", "developer"]
    days = [row["spent_on"] for row in body["rows"]]
    assert days == sorted(days)


def test_a_log_deleted_in_gitlab_is_balanced_by_a_negative_row(carded: Engine) -> None:
    logs = {**LOGS, "acme/team/web": [log for log in LOGS["acme/team/web"] if log["id"][-1] != "2"]}
    collect(carded, gitlab(logs))
    admin = as_user(carded, user(carded, "admin"), Role.ADMIN)
    body = card(admin)
    logged, removed = by_log(body)["2"]
    assert (logged["kind"], logged["minutes"]) == ("logged", 30)
    assert (removed["kind"], removed["minutes"]) == ("removed", -30)
    assert removed["spent_on"] == logged["spent_on"]
    assert removed["removed_on"] == datetime.now(UTC).date().isoformat()
    assert body["totals"]["minutes"] == 75
    assert (body["totals"]["logged_minutes"], body["totals"]["removed_minutes"]) == (105, -30)
    left_out = card(admin, removed="false")
    assert "2" not in by_log(left_out) and left_out["totals"]["minutes"] == 75


def test_timecard_filters_and_sorting(carded: Engine) -> None:
    admin = as_user(carded, user(carded, "admin"), Role.ADMIN)
    assert set(by_log(card(admin, person="dev1"))) == {"1", "3", "4"}
    assert set(by_log(card(admin, person=["dev2", "dev3"]))) == {"2", "5"}
    assert set(by_log(card(admin, top="Customer portal"))) == {"1", "2", "4"}
    assert set(by_log(card(admin, top="(none)"))) == {"3", "5"}
    assert set(by_log(card(admin, parent="Login form"))) == {"2"}
    assert set(by_log(card(admin, iteration="Sprints: 2026-11-02 to 2026-11-15"))) == {"1", "4"}
    assert set(by_log(card(admin, milestone="M2"))) == {"1", "4"}
    assert set(by_log(card(admin, project="101"))) == {"5"}
    minutes = [row["minutes"] for row in card(admin, sort="-minutes")["rows"]]
    assert minutes == sorted(minutes, reverse=True)
    names = [row["developer"] for row in card(admin, sort="developer,-minutes")["rows"]]
    assert names == ["Dana One", "Dana One", "Dana One", "Dee Three", "Dev Two"]
    bad = admin.get("/api/v1/time/timecard", params={"sort": "salary"})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid_sort"


def test_timecard_downloads_as_csv_and_html(carded: Engine) -> None:
    admin = as_user(carded, user(carded, "admin"), Role.ADMIN)
    response = admin.get("/api/v1/time/timecard", params={"format": "csv", "sort": "-minutes"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    disposition = response.headers["content-disposition"]
    today = date.today().isoformat()
    assert (
        disposition.startswith('attachment; filename="timecard-') and f"{today}.csv" in disposition
    )
    lines = response.text.strip().split("\r\n")
    assert lines[0].startswith("Developer,Username,Date,Minutes,Entry,Removed on,Project")
    assert len(lines) == 6
    # Text a spreadsheet would run as a formula is defused; negative minutes stay numbers.
    assert ",'=HYPERLINK(evil)," in response.text
    assert any(",-15,Logged," in line for line in lines)
    page = admin.get("/api/v1/time/timecard", params={"format": "html", "person": "dev3"})
    assert page.headers["content-type"].startswith("text/html")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    assert "<script>alert" not in page.text
    assert 'id="card"' in page.text and "Dee Three" in page.text and "Dana One" not in page.text
    actions = audit_actions(carded, "time.timecard")
    assert [a for a, _ in actions] == ["time.timecard_exported", "time.timecard_exported"]
    assert [(d or {})["format"] for _, d in actions] == ["csv", "html"]
    assert (actions[1][1] or {})["people"] == ["dev3"]
    assert "Dee Three" not in json.dumps([d for _, d in actions])


def test_timecard_access_follows_time_per_person(
    carded: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    mia = user(carded, "mia")
    with Session(carded) as db:
        scopes.assign(db, instance_id(carded), mia, [11], ADMIN)
        db.commit()
    manager = as_user(carded, mia, Role.MANAGER)
    body = card(manager)
    assert {row["project"]["path"] for row in body["rows"]} == {"acme/team/web"}
    assert body["all_projects"] is False
    outside = manager.get("/api/v1/time/timecard", params={"project": "101"})
    assert outside.status_code == 403 and outside.json()["detail"]["code"] == "out_of_scope"
    for role in (Role.ANALYST, Role.AUDITOR):
        other = as_user(carded, user(carded, role.value), role)
        assert other.get("/api/v1/time/timecard").status_code == 403
    assert [a for a, _ in audit_actions(carded, "time.timecard")] == ["time.timecard_viewed"]
    monkeypatch.setenv("BALUARTE_TIME_PER_PERSON", "false")
    off = manager.get("/api/v1/time/timecard", params={"format": "csv"})
    assert off.status_code == 403 and off.json()["detail"]["code"] == "per_person_disabled"


def test_a_timecard_too_large_is_refused(carded: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    from baluarte.timetracking import timecard

    monkeypatch.setattr(timecard, "MAX_ROWS", 3)
    admin = as_user(carded, user(carded, "admin"), Role.ADMIN)
    refused = admin.get("/api/v1/time/timecard")
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == "too_many_rows"
    assert admin.get("/api/v1/time/timecard", params={"person": "dev2"}).status_code == 200


def test_unread_context_falls_back_to_the_logged_milestone(engine: Engine) -> None:
    fake = server(LOGS)
    fake.json("/users", status=404, body={"message": "404 Not Found"})
    collect(engine, fake)
    admin = as_user(engine, user(engine, "admin"), Role.ADMIN)
    first = by_log(card(admin))["1"][0]
    assert first["milestone"] == "M1" and first["context_read"] is False
    assert first["top"] is None


def test_timecard_reply_is_not_cached(carded: Engine) -> None:
    admin = as_user(carded, user(carded, "admin"), Role.ADMIN)
    response: httpx.Response = admin.get("/api/v1/time/timecard", params={"format": "csv"})
    assert response.headers["cache-control"] == "no-store"
