"""Time tracking: collection from GitLab, summaries and access to time per person."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from gitlab_fake import (
    URL,
    FakeGitLab,
    issue_with_time,
    paged,
    settings,
    timelog,
    timelogs_graphql,
    work_items_graphql,
)
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from test_exceptions import user
from test_manager_space import (
    ADMIN,
    as_user,
    assign,
    audit_actions,
    build,
    configure_gitlab,
    instance_id,
    make_tag,
)

from baluarte.auth.roles import Role
from baluarte.compliance import retention
from baluarte.db.models import (
    AuditEvent,
    CollectionRun,
    IssueEstimate,
    Observation,
    TimeCursor,
    TimeEntry,
)
from baluarte.integrations.gitlab import timelogs
from baluarte.integrations.gitlab.client import GitLabClient
from baluarte.integrations.gitlab.jobs import COLLECT_TIME, make_collect_time
from baluarte.jobs import queue
from baluarte.jobs.worker import Worker
from baluarte.manager import scopes
from baluarte.timetracking import settings as time_settings
from baluarte.timetracking.settings import TimeTrackingSettings

pytestmark = pytest.mark.db

SETTINGS = TimeTrackingSettings()


@pytest.fixture(autouse=True)
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    configure_gitlab(monkeypatch, tmp_path)


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    return build(migrated_engine)


def ago(days: float) -> str:
    moment = datetime.now(UTC) - timedelta(days=days)
    return moment.isoformat().replace("+00:00", "Z")


def server(
    logs: dict[str, list[dict[str, Any]]],
    issues: dict[int, list[dict[str, Any]]] | None = None,
    contexts: dict[str, list[dict[str, Any]]] | None = None,
    *,
    iterations: bool = True,
    **options: Any,
) -> FakeGitLab:
    fake = FakeGitLab({})
    items = work_items_graphql(contexts or {}, iterations=iterations)
    fake.graphql(timelogs_graphql(logs, fallback=items, **options))
    for gitlab_id in (100, 101, 102, 200):
        path = f"/projects/{gitlab_id}/issues"
        fake.set(path, paged(path, [(issues or {}).get(gitlab_id, [])]))
    return fake


def collect(engine: Engine, fake: FakeGitLab, options: TimeTrackingSettings = SETTINGS) -> Any:
    with GitLabClient(settings(), transport=fake.transport) as client:
        return timelogs.collect(client, engine, instance_id(engine), options)


def entries(engine: Engine) -> dict[str, TimeEntry]:
    with Session(engine) as db:
        rows = list(db.scalars(select(TimeEntry)))
        db.expunge_all()
        return {row.timelog_id.rsplit("/", 1)[-1]: row for row in rows}


# --- Collection --------------------------------------------------------------------------------


def test_collection_reads_logs_and_estimates_with_get_and_never_stores_summaries(
    engine: Engine,
) -> None:
    fake = server(
        {
            "acme/team/web": [
                timelog(1, ago(3), 3600, issue=7, labels=["bug", "backend"], milestone="M1"),
                timelog(2, ago(2), 1800, "dev2", merge_request=4, labels=["docs"]),
                timelog(3, ago(1), 900, summary="secret customer detail"),
            ],
            "acme/api": [timelog(4, ago(400), 600)],
        },
        {100: [issue_with_time(7, 7200, 3600, labels=["bug"])]},
    )
    result = collect(engine, fake)
    # acme/team/sub/svc and other/site are unknown to this GitLab.
    assert result.timelogs == {"ok": 2, "not_found": 2}
    assert all(request.method == "GET" for request in fake.requests)
    found = entries(engine)
    # The look-back is 180 days: the 400-day-old log is not read.
    assert set(found) == {"1", "2", "3"}
    assert (found["1"].issue_iid, found["1"].labels, found["1"].milestone) == (
        7,
        ["backend", "bug"],
        "M1",
    )
    assert (found["2"].merge_request_iid, found["2"].username) == (4, "dev2")
    assert found["3"].summary_sha256 and len(found["3"].summary_sha256) == 64
    with Session(engine) as db:
        payloads = json.dumps([row.payload for row in db.scalars(select(Observation))])
        estimate = db.get(IssueEstimate, (found["1"].project_id, 7))
        cursor = db.get(TimeCursor, found["1"].project_id)
    assert "secret customer detail" not in payloads and "summary_sha256" in payloads
    assert estimate is not None and estimate.estimate_seconds == 7200
    assert cursor is not None and cursor.last_spent_at is not None


def test_collection_rereads_recent_time_and_marks_deleted_logs_removed(engine: Engine) -> None:
    logs = {"acme/team/web": [timelog(1, ago(60), 600), timelog(2, ago(5), 300)]}
    collect(engine, server(logs))
    # Log 2 was deleted in GitLab and log 3 added; log 1 is older than the re-read window.
    logs = {"acme/team/web": [timelog(3, ago(1), 120)]}
    fake = server(logs)
    result = collect(engine, fake)
    found = entries(engine)
    assert found["1"].removed_at is None
    assert found["2"].removed_at is not None
    assert found["3"].removed_at is None
    assert result.entries["removed"] == 1
    window = json.loads(fake.requests[0].url.params["variables"])
    start = datetime.fromisoformat(window["start"].replace("Z", "+00:00"))
    assert timedelta(days=29) < datetime.now(UTC) - start < timedelta(days=31)
    # A log that comes back is no longer removed.
    collect(engine, server({"acme/team/web": [timelog(2, ago(5), 300), timelog(3, ago(1), 120)]}))
    assert entries(engine)["2"].removed_at is None


def test_unreadable_time_is_unknown_not_zero(engine: Engine) -> None:
    fake = server({"acme/team/web": [timelog(1, ago(1), 60)]}, forbidden=["acme/team/web"])
    result = collect(engine, fake)
    assert result.timelogs["forbidden"] == 1
    assert entries(engine) == {}
    with Session(engine) as db:
        statuses = {
            (row.source, row.collection_status)
            for row in db.scalars(
                select(Observation).where(Observation.source == timelogs.TIMELOG_SOURCE)
            )
        }
    assert (timelogs.TIMELOG_SOURCE, "forbidden") in statuses
    unsupported = collect(engine, server({}, unsupported=True))
    assert unsupported.timelogs["unsupported"] == 4


def test_a_rejected_token_stops_time_collection(engine: Engine) -> None:
    fake = FakeGitLab({})
    fake.graphql(httpx.Response(401, json={"message": "401 Unauthorized"}))
    result = collect(engine, fake)
    assert result.rejected
    assert len(fake.requests) == 1
    assert result.timelogs == {"failed": 4}


def test_the_time_job_records_a_run_after_project_discovery(engine: Engine) -> None:
    fake = server({"acme/team/web": [timelog(1, ago(1), 60)]})
    with Session(engine) as db:
        queue.enqueue(db, COLLECT_TIME, "test", max_attempts=1)
        db.commit()
    worker = Worker(engine, {COLLECT_TIME: make_collect_time(fake.transport)})
    assert worker.run_once()
    with Session(engine) as db:
        run = db.scalars(select(CollectionRun).where(CollectionRun.kind == "time")).one()
    assert run.status in ("succeeded", "partial")
    assert run.counts["timelogs"]["ok"] == 1


# --- Summaries ---------------------------------------------------------------------------------


@pytest.fixture
def logged(engine: Engine) -> Engine:
    collect(
        engine,
        server(
            {
                "acme/team/web": [
                    timelog(1, ago(3), 3600, "dev1", issue=7, labels=["bug"], milestone="M1"),
                    timelog(2, ago(2), 1800, "dev2", issue=7, labels=["bug"], milestone="M1"),
                    timelog(3, ago(10), 600, "dev1", merge_request=4),
                ],
                "acme/api": [timelog(4, ago(1), 1200, "dev3", issue=1, labels=["bug", "ops"])],
                "other/site": [timelog(5, ago(2), 300, "dev4", issue=2)],
            },
            {100: [issue_with_time(7, 10800, 5400)], 101: [issue_with_time(1, 3600, 1200)]},
        ),
    )
    return engine


def rows(response: httpx.Response) -> dict[str, tuple[int, int | None]]:
    assert response.status_code == 200, response.text
    return {
        r["label"]: (r["spent_seconds"], r["estimate_seconds"]) for r in response.json()["rows"]
    }


def test_summaries_by_each_grouping(logged: Engine) -> None:
    client = as_user(logged, user(logged, "aud"), Role.AUDITOR)
    url = "/api/v1/time/summary"
    by_project = client.get(url, params={"by": "project"})
    assert rows(by_project) == {
        "acme/team/web": (6000, 10800),
        "acme/api": (1200, 3600),
        "other/site": (300, 0),
    }
    totals = by_project.json()["totals"]
    assert (totals["spent_seconds"], totals["estimate_seconds"], totals["issues"]) == (
        7500,
        14400,
        3,
    )
    assert "dev1" not in by_project.text
    assert rows(client.get(url, params={"by": "group"})) == {
        "acme": (7200, 14400),
        "other": (300, 0),
    }
    assert rows(client.get(url, params={"by": "group", "group": "acme"})) == {
        "acme/team": (6000, 10800),
        "acme": (1200, 3600),
    }
    assert rows(client.get(url, params={"by": "label"})) == {
        "bug": (6600, 14400),
        "ops": (1200, 3600),
        "(no label)": (900, 0),
    }
    assert rows(client.get(url, params={"by": "milestone"})) == {
        "M1": (5400, 10800),
        "(no milestone)": (2100, 3600),
    }
    weeks = client.get(url, params={"by": "week", "from": ago(30)[:10]}).json()["rows"]
    assert sum(r["spent_seconds"] for r in weeks) == 7500
    assign(logged, make_tag(logged, "team", "payments"), ("group", 11))
    assert rows(client.get(url, params={"by": "tag:team"})) == {
        "payments": (6000, 10800),
        "(none)": (1500, 3600),
    }
    tagged = client.get(url, params={"by": "project", "tag": "team:payments", "label": "bug"})
    assert rows(tagged) == {"acme/team/web": (5400, 10800)}
    recent = client.get(url, params={"by": "project", "from": ago(5)[:10]})
    assert rows(recent)["acme/team/web"] == (5400, 10800)
    bad = client.get(url, params={"by": "person"})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid_grouping"


def test_removed_entries_are_not_counted(logged: Engine) -> None:
    collect(logged, server({"acme/team/web": [timelog(1, ago(3), 3600, issue=7)]}))
    client = as_user(logged, user(logged, "aud"), Role.AUDITOR)
    found = rows(client.get("/api/v1/time/summary", params={"by": "project"}))
    # Logs 2 and 3 are in the re-read window and no longer returned.
    assert found["acme/team/web"] == (3600, 10800)


# --- Time per person ---------------------------------------------------------------------------


def people(client: Any, **params: str) -> httpx.Response:
    return client.get("/api/v1/time/people", params=params)


def test_time_per_person_access_matrix(logged: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    mia, admin_id = user(logged, "mia"), user(logged, "admin")
    with Session(logged) as db:
        scopes.assign(db, instance_id(logged), mia, [11], ADMIN)
        db.commit()
    manager = as_user(logged, mia, Role.MANAGER)
    in_scope = people(manager)
    assert in_scope.status_code == 200
    assert {r["username"]: r["spent_seconds"] for r in in_scope.json()["rows"]} == {
        "dev1": 4200,
        "dev2": 1800,
    }
    assert in_scope.json()["all_projects"] is False
    assert people(manager, group="acme/team").status_code == 200
    outside = people(manager, group="acme")
    assert outside.status_code == 403 and outside.json()["detail"]["code"] == "out_of_scope"
    assert people(manager, project="200").json()["detail"]["code"] == "out_of_scope"
    assert manager.get("/api/v1/time/people/dev4").json()["items"] == []
    detail = manager.get("/api/v1/time/people/dev1", params={"limit": "1"}).json()
    assert len(detail["items"]) == 1 and detail["next_after"] is not None
    assert "summary" not in detail["items"][0]
    following = manager.get(
        "/api/v1/time/people/dev1", params={"limit": "1", "after": str(detail["next_after"])}
    ).json()
    assert len(following["items"]) == 1 and following["next_after"] is None

    unscoped = as_user(logged, user(logged, "max"), Role.MANAGER)
    assert people(unscoped).json()["detail"]["code"] == "out_of_scope"
    assert people(as_user(logged, user(logged, "ana"), Role.ANALYST)).status_code == 403
    assert people(as_user(logged, user(logged, "aud"), Role.AUDITOR)).status_code == 403
    admin = as_user(logged, admin_id, Role.ADMIN)
    everyone = people(admin).json()
    assert everyone["all_projects"] and len(everyone["rows"]) == 4

    monkeypatch.setenv("BALUARTE_TIME_PER_PERSON", "false")
    for client in (manager, admin):
        off = people(client)
        assert off.status_code == 403 and off.json()["detail"]["code"] == "per_person_disabled"
        assert client.get("/api/v1/time/people/dev1").status_code == 403
    assert manager.get("/api/v1/time/summary").json()["per_person_enabled"] is False


def test_each_per_person_read_is_audited_with_filters_only(logged: Engine) -> None:
    admin = as_user(logged, user(logged, "admin"), Role.ADMIN)
    people(admin, group="acme", label="bug")
    admin.get("/api/v1/time/people/dev1")
    admin.get("/api/v1/time/summary")
    viewed = audit_actions(logged, "time.")
    assert [action for action, _ in viewed] == ["time.people_viewed", "time.people_viewed"]
    first, second = viewed[0][1] or {}, viewed[1][1] or {}
    assert first["group"] == "acme" and first["label"] == "bug" and first["username"] is None
    assert second["username"] == "dev1"
    text = json.dumps([first, second])
    assert "4200" not in text and "dev2" not in text
    with Session(logged) as db:
        actors = set(
            db.scalars(select(AuditEvent.actor).where(AuditEvent.action == "time.people_viewed"))
        )
    assert len(actors) == 1


def test_time_settings_api(logged: Engine) -> None:
    admin = as_user(logged, user(logged, "admin"), Role.ADMIN)
    assert admin.get("/api/v1/settings/time").json() == {
        "lookback_days": 180,
        "reread_days": 30,
        "retention_days": None,
        "missing_time_label": "missing-time",
        "estimate_round_minutes": 30,
        "rule_issue_types": ["issue", "task"],
        "per_person_enabled": True,
    }
    saved = admin.put(
        "/api/v1/settings/time",
        json={"lookback_days": 90, "reread_days": 14, "retention_days": 365},
    )
    assert saved.status_code == 200 and saved.json()["reread_days"] == 14
    assert admin.put("/api/v1/settings/time", json={"lookback_days": 0}).status_code == 422
    manager = as_user(logged, user(logged, "mia"), Role.MANAGER)
    assert manager.get("/api/v1/settings/time").json()["lookback_days"] == 90
    assert manager.put("/api/v1/settings/time", json={}).status_code == 403
    assert [a for a, _ in audit_actions(logged, "settings.time")] == [
        "settings.time_tracking_updated"
    ]


def test_the_first_collection_honours_the_lookback_setting(engine: Engine) -> None:
    fake = server({"acme/team/web": [timelog(1, ago(20), 60), timelog(2, ago(5), 60)]})
    collect(engine, fake, TimeTrackingSettings(lookback_days=10, reread_days=3))
    assert set(entries(engine)) == {"2"}
    assert URL in str(fake.requests[0].url)


def test_retention_expires_time_before_the_shorter_period(engine: Engine) -> None:
    collect(engine, server({"acme/team/web": [timelog(1, ago(60), 60), timelog(2, ago(5), 60)]}))
    with Session(engine) as db:
        time_settings.save(db, TimeTrackingSettings(retention_days=30), "user:admin")
        details = retention.expire(db)
        db.commit()
    assert details["deleted"]["time_entry"] == 1
    assert set(entries(engine)) == {"2"}


def test_exports_hold_tags_and_time_per_person_only_on_request(
    logged: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    mia = user(logged, "mia")
    with Session(logged) as db:
        scopes.assign(db, instance_id(logged), mia, [11], ADMIN)
        db.commit()
    assign(logged, make_tag(logged, "team", "payments"), ("group", 11))
    body: dict[str, Any] = {"format": "json", "scope": {"all": True}}
    auditor = as_user(logged, user(logged, "aud"), Role.AUDITOR)
    response = auditor.post("/api/v1/evidence/exports", json=body)
    assert response.status_code == 200, response.text
    plain = response.json()
    assert "time_entries" not in plain and "time_people" not in plain["manifest"]
    web = next(p for p in plain["inventory"] if p["gitlab_id"] == 100)
    assert web["tags"] == "team=payments"

    body["scope"]["include_people"] = True
    refused = auditor.post("/api/v1/evidence/exports", json=body)
    assert refused.status_code == 403
    manager = as_user(logged, mia, Role.MANAGER, Role.AUDITOR)
    document = manager.post("/api/v1/evidence/exports", json=body).json()
    assert {e["project_path"] for e in document["time_entries"]} == {"acme/team/web"}
    assert "summary" not in document["time_entries"][0]
    assert document["manifest"]["time_people"] == {
        "included": True,
        "projects": "manager_scope",
        "entries": 3,
    }
    exported = audit_actions(logged, "time.people_exported")
    assert len(exported) == 1 and (exported[0][1] or {})["projects"] == "manager_scope"
    admin = as_user(logged, user(logged, "admin"), Role.ADMIN)
    everyone = admin.post("/api/v1/evidence/exports", json=body).json()
    assert len(everyone["time_entries"]) == 5
    monkeypatch.setenv("BALUARTE_TIME_PER_PERSON", "false")
    off = admin.post("/api/v1/evidence/exports", json=body)
    assert off.status_code == 403 and off.json()["detail"]["code"] == "per_person_disabled"
