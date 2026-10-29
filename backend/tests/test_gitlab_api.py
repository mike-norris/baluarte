from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from gitlab_fake import TOKEN, URL, FakeGitLab, group, paged, project
from gitlab_fake import settings as fake_settings
from oidc_fake import make_settings
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from baluarte.api.v1.gitlab import gitlab_transport
from baluarte.auth.dependencies import (
    Principal,
    auth_settings,
    current_principal,
    database_engine,
)
from baluarte.auth.roles import Role
from baluarte.db.models import AuditEvent, CollectionJob, GitLabGroup, GitLabProject
from baluarte.integrations.gitlab.capabilities import detect, record_snapshot
from baluarte.integrations.gitlab.client import GitLabClient
from baluarte.integrations.gitlab.groups import discover_groups
from baluarte.integrations.gitlab.projects import discover_projects
from baluarte.integrations.gitlab.runs import finish_run, start_run
from baluarte.main import create_app

pytestmark = pytest.mark.db


@pytest.fixture
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("BALUARTE_GITLAB_TIMEOUT_SECONDS", raising=False)


def client_as(engine: Engine, fake: FakeGitLab, *roles: Role) -> TestClient:
    app = create_app()
    app.dependency_overrides[database_engine] = lambda: engine
    app.dependency_overrides[current_principal] = lambda: Principal(
        user_id=1,
        subject="user-1",
        email=None,
        display_name=None,
        roles=frozenset(roles),
        session_id=1,
        session_expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        csrf_token="csrf",
    )
    app.dependency_overrides[auth_settings] = lambda: make_settings()
    app.dependency_overrides[gitlab_transport] = lambda: fake.transport
    return TestClient(app)


def audit_rows(engine: Engine) -> list[AuditEvent]:
    with Session(engine) as db:
        return list(db.scalars(select(AuditEvent)))


@pytest.mark.parametrize("role", [Role.ANALYST, Role.AUDITOR])
def test_only_admins_run_diagnostics(migrated_engine: Engine, gitlab_env: None, role: Role) -> None:
    fake = FakeGitLab()

    response = client_as(migrated_engine, fake, role).post("/api/v1/gitlab/diagnostics")

    assert response.status_code == 403
    assert fake.requests == []


def test_diagnostics_report_and_audit(migrated_engine: Engine, gitlab_env: None) -> None:
    response = client_as(migrated_engine, FakeGitLab(), Role.ADMIN).post(
        "/api/v1/gitlab/diagnostics"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == "17.5.1-ee"
    assert [check["id"] for check in body["checks"]] == [
        "connectivity",
        "token",
        "user",
        "group_visibility",
    ]
    assert TOKEN not in response.text
    [row] = audit_rows(migrated_engine)
    assert (row.actor, row.action, row.resource_id) == ("user:1", "gitlab.diagnostics_run", URL)
    assert row.details["status"] == "ok"
    assert row.details["checks"]["token"] == "ok"


def test_failed_connection_is_reported_not_raised(
    migrated_engine: Engine, gitlab_env: None
) -> None:
    fake = FakeGitLab()
    fake.set("/version", httpx.Response(401))

    body = client_as(migrated_engine, fake, Role.ADMIN).post("/api/v1/gitlab/diagnostics").json()

    assert body["status"] == "failed"
    assert body["checks"][0]["details"] == {"reason": "unauthorized"}
    assert audit_rows(migrated_engine)[0].details["status"] == "failed"


def test_not_configured(migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BALUARTE_GITLAB_URL", raising=False)

    body = (
        client_as(migrated_engine, FakeGitLab(), Role.ADMIN)
        .post("/api/v1/gitlab/diagnostics")
        .json()
    )

    assert body["status"] == "failed"
    assert body["checks"] == [
        {
            "id": "configuration",
            "status": "failed",
            "summary": "BALUARTE_GITLAB_URL is not set",
            "details": {},
        }
    ]


def test_misconfigured_reports_the_error(
    migrated_engine: Engine, gitlab_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BALUARTE_GITLAB_URL", "http://gitlab.example.test")

    body = (
        client_as(migrated_engine, FakeGitLab(), Role.ADMIN)
        .post("/api/v1/gitlab/diagnostics")
        .json()
    )

    assert body["status"] == "failed"
    assert "https URL" in body["checks"][0]["summary"]


def test_settings_include_gitlab_without_token(migrated_engine: Engine, gitlab_env: None) -> None:
    response = client_as(migrated_engine, FakeGitLab(), Role.ADMIN).get("/api/v1/settings")

    assert response.json()["gitlab"] == {"url": URL, "token": "configured", "timeout_seconds": 15.0}
    assert TOKEN not in response.text


def store_snapshot(engine: Engine, checked_at: datetime) -> None:
    client = GitLabClient(fake_settings(), transport=FakeGitLab().transport)
    with Session(engine) as db:
        record_snapshot(db, URL, detect(client, now=checked_at))
        db.commit()


@pytest.mark.parametrize("role", [Role.ADMIN, Role.ANALYST, Role.AUDITOR])
def test_instance_is_readable_by_every_role(
    migrated_engine: Engine, gitlab_env: None, role: Role
) -> None:
    store_snapshot(migrated_engine, datetime.now(UTC))

    body = client_as(migrated_engine, FakeGitLab(), role).get("/api/v1/gitlab/instance").json()

    assert body["configured"] is True
    assert (body["url"], body["version"], body["edition"]) == (URL, "17.5.1-ee", "ee")
    assert body["stale"] is False
    assert body["capabilities"]["status"] == "ok"
    assert body["capabilities"]["probes"]["license.read"]["status"] == "forbidden"


def test_old_snapshot_is_stale(migrated_engine: Engine, gitlab_env: None) -> None:
    store_snapshot(migrated_engine, datetime.now(UTC) - timedelta(hours=25))

    body = (
        client_as(migrated_engine, FakeGitLab(), Role.AUDITOR).get("/api/v1/gitlab/instance").json()
    )

    assert body["stale"] is True


def test_instance_before_detection(migrated_engine: Engine, gitlab_env: None) -> None:
    body = (
        client_as(migrated_engine, FakeGitLab(), Role.ANALYST).get("/api/v1/gitlab/instance").json()
    )

    assert body == {
        "configured": True,
        "url": URL,
        "version": None,
        "edition": None,
        "last_checked_at": None,
        "stale": True,
        "capabilities": None,
    }


def test_instance_when_not_configured(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BALUARTE_GITLAB_URL", raising=False)

    body = (
        client_as(migrated_engine, FakeGitLab(), Role.ANALYST).get("/api/v1/gitlab/instance").json()
    )

    assert (body["configured"], body["url"], body["capabilities"]) == (False, None, None)


@pytest.mark.parametrize("role", [Role.ANALYST, Role.AUDITOR])
def test_only_admins_queue_detection(migrated_engine: Engine, gitlab_env: None, role: Role) -> None:
    response = client_as(migrated_engine, FakeGitLab(), role).post(
        "/api/v1/gitlab/capabilities/refresh"
    )

    assert response.status_code == 403


def test_refresh_queues_one_job_per_minute_and_audits(
    migrated_engine: Engine, gitlab_env: None
) -> None:
    client = client_as(migrated_engine, FakeGitLab(), Role.ADMIN)

    first = client.post("/api/v1/gitlab/capabilities/refresh")
    second = client.post("/api/v1/gitlab/capabilities/refresh")

    assert first.status_code == 202
    assert first.json() == second.json()
    with Session(migrated_engine) as db:
        [job] = db.scalars(select(CollectionJob)).all()
        actions = list(db.scalars(select(AuditEvent.action)))
    assert job.kind == "gitlab.detect_capabilities"
    assert job.id == first.json()["job_id"]
    assert actions == ["gitlab.capabilities_refresh_requested"] * 2


def test_refresh_without_configuration(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BALUARTE_GITLAB_URL", raising=False)

    response = client_as(migrated_engine, FakeGitLab(), Role.ADMIN).post(
        "/api/v1/gitlab/capabilities/refresh"
    )

    assert response.status_code == 409


def seed_groups(engine: Engine) -> None:
    fake = FakeGitLab()
    fake.set(
        "/groups",
        paged(
            "/groups",
            [
                [
                    group(1, "platform"),
                    group(2, "platform/back_end", 1),
                    group(3, "platform/frontend", 1),
                    group(4, "security"),
                ]
            ],
        ),
    )
    discover_groups(GitLabClient(fake_settings(), transport=fake.transport), engine, URL)
    with Session(engine) as db:
        db.execute(update(GitLabGroup).where(GitLabGroup.gitlab_id == 4).values(state="missing"))
        db.commit()


def group_paths(response: Any) -> list[str]:
    return [item["full_path"] for item in response.json()["items"]]


@pytest.mark.parametrize("role", [Role.ADMIN, Role.ANALYST, Role.AUDITOR])
def test_groups_are_listed_for_every_role(
    migrated_engine: Engine, gitlab_env: None, role: Role
) -> None:
    seed_groups(migrated_engine)

    response = client_as(migrated_engine, FakeGitLab(), role).get("/api/v1/gitlab/groups")

    assert response.status_code == 200
    body = response.json()
    assert group_paths(response) == [
        "platform",
        "platform/back_end",
        "platform/frontend",
        "security",
    ]
    assert body["items"][1]["parent_gitlab_id"] == 1
    assert body["items"][3]["state"] == "missing"
    assert body["next_after_id"] is None


def test_group_filters_and_paging(migrated_engine: Engine, gitlab_env: None) -> None:
    seed_groups(migrated_engine)
    client = client_as(migrated_engine, FakeGitLab(), Role.ANALYST)

    missing = client.get("/api/v1/gitlab/groups", params={"state": "missing"})
    children = client.get("/api/v1/gitlab/groups", params={"parent_gitlab_id": 1})
    # "_" is matched literally, not as a single-character wildcard.
    underscore = client.get("/api/v1/gitlab/groups", params={"search": "K_E"})
    first = client.get("/api/v1/gitlab/groups", params={"limit": 3}).json()
    rest = client.get(
        "/api/v1/gitlab/groups", params={"limit": 3, "after_id": first["next_after_id"]}
    )

    assert group_paths(missing) == ["security"]
    assert group_paths(children) == ["platform/back_end", "platform/frontend"]
    assert group_paths(underscore) == ["platform/back_end"]
    assert len(first["items"]) == 3
    assert group_paths(rest) == ["security"]
    assert rest.json()["next_after_id"] is None


def test_groups_when_not_configured(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BALUARTE_GITLAB_URL", raising=False)

    body = (
        client_as(migrated_engine, FakeGitLab(), Role.ANALYST).get("/api/v1/gitlab/groups").json()
    )

    assert body == {"items": [], "next_after_id": None}


@pytest.mark.parametrize("params", [{"state": "deleted"}, {"limit": 501}, {"search": ""}])
def test_group_listing_rejects_invalid_parameters(
    migrated_engine: Engine, gitlab_env: None, params: dict[str, object]
) -> None:
    response = client_as(migrated_engine, FakeGitLab(), Role.ANALYST).get(
        "/api/v1/gitlab/groups", params=params
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("role", "status"), [(Role.ADMIN, 202), (Role.ANALYST, 403), (Role.AUDITOR, 403)]
)
def test_group_discovery_is_queued_by_admins(
    migrated_engine: Engine, gitlab_env: None, role: Role, status: int
) -> None:
    client = client_as(migrated_engine, FakeGitLab(), role)

    responses = [client.post("/api/v1/gitlab/groups/discover") for _ in range(2)]

    assert [response.status_code for response in responses] == [status, status]
    if status == 202:
        assert responses[0].json() == responses[1].json()
        with Session(migrated_engine) as db:
            [job] = db.scalars(select(CollectionJob)).all()
            actions = set(db.scalars(select(AuditEvent.action)))
        assert (job.kind, job.max_attempts) == ("gitlab.discover_groups", 5)
        assert actions == {"gitlab.group_discovery_requested"}


def seed_projects(engine: Engine) -> None:
    fake = FakeGitLab()
    fake.set(
        "/projects",
        paged(
            "/projects",
            [
                [
                    project(10, "platform/api", 1, visibility="internal"),
                    project(11, "platform/old_api", 1, archived=True),
                    project(12, "alice/scratch", 900, kind="user", visibility="public"),
                ]
            ],
        ),
    )
    discover_projects(GitLabClient(fake_settings(), transport=fake.transport), engine, URL)
    with Session(engine) as db:
        db.execute(
            update(GitLabProject).where(GitLabProject.gitlab_id == 12).values(state="inaccessible")
        )
        db.commit()


def project_paths(response: Any) -> list[str]:
    return [item["path_with_namespace"] for item in response.json()["items"]]


@pytest.mark.parametrize("role", [Role.ADMIN, Role.ANALYST, Role.AUDITOR])
def test_projects_are_listed_for_every_role(
    migrated_engine: Engine, gitlab_env: None, role: Role
) -> None:
    seed_projects(migrated_engine)

    response = client_as(migrated_engine, FakeGitLab(), role).get("/api/v1/gitlab/projects")

    assert response.status_code == 200
    assert project_paths(response) == ["platform/api", "platform/old_api", "alice/scratch"]
    first = response.json()["items"][0]
    assert (first["namespace_gitlab_id"], first["default_branch"]) == (1, "main")


def test_project_filters_and_paging(migrated_engine: Engine, gitlab_env: None) -> None:
    seed_projects(migrated_engine)
    client = client_as(migrated_engine, FakeGitLab(), Role.AUDITOR)

    def paths(**params: object) -> list[str]:
        return project_paths(client.get("/api/v1/gitlab/projects", params=params))

    assert paths(state="inaccessible") == ["alice/scratch"]
    assert paths(namespace_gitlab_id=1) == ["platform/api", "platform/old_api"]
    assert paths(visibility="public") == ["alice/scratch"]
    assert paths(archived="true") == ["platform/old_api"]
    assert paths(archived="false") == ["platform/api", "alice/scratch"]
    assert paths(search="D_A") == ["platform/old_api"]
    first = client.get("/api/v1/gitlab/projects", params={"limit": 2}).json()
    assert paths(limit=2, after_id=first["next_after_id"]) == ["alice/scratch"]


def test_projects_when_not_configured(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BALUARTE_GITLAB_URL", raising=False)

    response = client_as(migrated_engine, FakeGitLab(), Role.ANALYST).get("/api/v1/gitlab/projects")

    assert response.json() == {"items": [], "next_after_id": None}


@pytest.mark.parametrize(
    ("role", "status"), [(Role.ADMIN, 202), (Role.ANALYST, 403), (Role.AUDITOR, 403)]
)
def test_project_discovery_is_queued_by_admins(
    migrated_engine: Engine, gitlab_env: None, role: Role, status: int
) -> None:
    response = client_as(migrated_engine, FakeGitLab(), role).post(
        "/api/v1/gitlab/projects/discover"
    )

    assert response.status_code == status
    if status == 202:
        with Session(migrated_engine) as db:
            job = db.get_one(CollectionJob, response.json()["job_id"])
            actions = set(db.scalars(select(AuditEvent.action)))
        assert job.kind == "gitlab.discover_projects"
        assert actions == {"gitlab.project_discovery_requested"}


def seed_runs(engine: Engine) -> list[int]:
    ids = []
    for kind, status_ in [("groups", "succeeded"), ("projects", "failed"), ("groups", "partial")]:
        run_id = start_run(engine, URL, kind, None)  # type: ignore[arg-type]
        finish_run(engine, run_id, status_, error="boom" if status_ == "failed" else None)  # type: ignore[arg-type]
        ids.append(run_id)
    return ids


@pytest.mark.parametrize("role", [Role.ADMIN, Role.ANALYST, Role.AUDITOR])
def test_freshness_for_every_role(migrated_engine: Engine, gitlab_env: None, role: Role) -> None:
    seed_runs(migrated_engine)

    body = client_as(migrated_engine, FakeGitLab(), role).get("/api/v1/gitlab/freshness").json()

    assert body["status"] == "failed"
    assert body["collections"]["capabilities"]["status"] == "never"
    assert body["collections"]["projects"]["last_run"]["error"] == "boom"
    assert body["collections"]["groups"]["status"] == "partial"
    assert body["sync_interval_minutes"] == 360
    assert body["resources"]["groups"] == {
        "active": 0,
        "missing": 0,
        "inaccessible": 0,
        "with_errors": 0,
    }


def test_freshness_before_anything_ran(migrated_engine: Engine, gitlab_env: None) -> None:
    body = (
        client_as(migrated_engine, FakeGitLab(), Role.AUDITOR)
        .get("/api/v1/gitlab/freshness")
        .json()
    )

    assert body["status"] == "never"


def test_freshness_not_configured(migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BALUARTE_GITLAB_URL", raising=False)

    body = (
        client_as(migrated_engine, FakeGitLab(), Role.AUDITOR)
        .get("/api/v1/gitlab/freshness")
        .json()
    )

    assert body["status"] == "not_configured"


def test_run_history(migrated_engine: Engine, gitlab_env: None) -> None:
    ids = seed_runs(migrated_engine)
    client = client_as(migrated_engine, FakeGitLab(), Role.ANALYST)

    def run_ids(**params: object) -> list[int]:
        return [
            item["id"] for item in client.get("/api/v1/gitlab/runs", params=params).json()["items"]
        ]

    assert run_ids() == list(reversed(ids))
    assert run_ids(kind="groups") == [ids[2], ids[0]]
    assert run_ids(status="failed") == [ids[1]]
    page = client.get("/api/v1/gitlab/runs", params={"limit": 2}).json()
    assert run_ids(limit=2, before_id=page["next_before_id"]) == [ids[0]]


def test_has_error_filter(migrated_engine: Engine, gitlab_env: None) -> None:
    seed_groups(migrated_engine)
    seed_projects(migrated_engine)
    with Session(migrated_engine) as db:
        db.execute(
            update(GitLabGroup)
            .where(GitLabGroup.gitlab_id == 2)
            .values(last_error="could not confirm")
        )
        db.execute(
            update(GitLabProject).where(GitLabProject.gitlab_id == 10).values(last_error="x")
        )
        db.commit()
    client = client_as(migrated_engine, FakeGitLab(), Role.ANALYST)

    errored = client.get("/api/v1/gitlab/groups", params={"has_error": "true"}).json()["items"]
    clean = client.get("/api/v1/gitlab/groups", params={"has_error": "false"})
    projects = client.get("/api/v1/gitlab/projects", params={"has_error": "true"})

    assert [(g["full_path"], g["last_error"]) for g in errored] == [
        ("platform/back_end", "could not confirm")
    ]
    assert "platform/back_end" not in group_paths(clean)
    assert project_paths(projects) == ["platform/api"]
