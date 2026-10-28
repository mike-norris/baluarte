from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from gitlab_fake import TOKEN, URL, FakeGitLab
from gitlab_fake import settings as fake_settings
from oidc_fake import make_settings
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from baluarte.api.v1.gitlab import gitlab_transport
from baluarte.auth.dependencies import (
    Principal,
    auth_settings,
    current_principal,
    database_engine,
)
from baluarte.auth.roles import Role
from baluarte.db.models import AuditEvent, CollectionJob
from baluarte.integrations.gitlab.capabilities import detect, record_snapshot
from baluarte.integrations.gitlab.client import GitLabClient
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
