import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from oidc_fake import make_settings
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.auth.dependencies import (
    Principal,
    auth_settings,
    current_principal,
    database_engine,
)
from baluarte.auth.roles import Role
from baluarte.db.models import AuditEvent
from baluarte.logs import REDACTED
from baluarte.main import create_app

pytestmark = pytest.mark.db


def audit_logs(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [r.audit for r in caplog.records if r.name == "baluarte.audit"]  # type: ignore[attr-defined]


def test_details_are_redacted_before_hashing(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as db:
        audit.record(
            db,
            actor="user:1",
            action="gitlab.connection_tested",
            resource_type="gitlab_instance",
            details={"url": "https://root:hunter2-secret@gitlab.example.test", "status": 200},
        )
        db.commit()
        row = db.scalars(select(AuditEvent)).one()

    assert row.details == {"url": f"https://root:{REDACTED}@gitlab.example.test", "status": 200}
    assert row.details_digest == audit.details_digest(row.details)


def test_events_are_logged_only_after_commit(
    migrated_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="baluarte.audit")
    with Session(migrated_engine) as db:
        audit.record(db, actor="user:1", action="policy.created", resource_type="policy")
        db.flush()
        assert audit_logs(caplog) == []
        db.commit()
        row_id = db.scalars(select(AuditEvent.id)).one()

    [entry] = audit_logs(caplog)
    assert entry["id"] == row_id
    assert (entry["actor"], entry["action"]) == ("user:1", "policy.created")
    assert entry["details_digest"] == audit.details_digest({})


def test_rolled_back_events_are_not_logged(
    migrated_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="baluarte.audit")
    with Session(migrated_engine) as db:
        audit.record(db, actor="user:1", action="policy.created", resource_type="policy")
        db.flush()
        db.rollback()
        db.commit()

    assert audit_logs(caplog) == []
    with Session(migrated_engine) as db:
        assert db.scalars(select(AuditEvent)).all() == []


def principal(*roles: Role) -> Principal:
    return Principal(
        user_id=1,
        subject="user-1",
        email=None,
        display_name=None,
        roles=frozenset(roles),
        session_id=1,
        session_expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        csrf_token="csrf",
    )


def client_as(engine: Engine, *roles: Role) -> TestClient:
    app = create_app()
    app.dependency_overrides[database_engine] = lambda: engine
    app.dependency_overrides[current_principal] = lambda: principal(*roles)
    app.dependency_overrides[auth_settings] = lambda: make_settings()
    return TestClient(app)


@pytest.fixture
def events(migrated_engine: Engine) -> list[int]:
    with Session(migrated_engine) as db:
        for n, action in enumerate(["auth.login", "auth.logout", "auth.login", "policy.created"]):
            audit.record(
                db,
                actor=f"user:{n % 2}",
                action=action,
                resource_type="session",
                details={"n": n},
            )
            db.flush()
        db.commit()
        return list(db.scalars(select(AuditEvent.id).order_by(AuditEvent.id)))


@pytest.mark.parametrize(
    ("role", "status"), [(Role.ADMIN, 200), (Role.AUDITOR, 200), (Role.ANALYST, 403)]
)
def test_audit_events_permission(migrated_engine: Engine, role: Role, status: int) -> None:
    assert client_as(migrated_engine, role).get("/api/v1/audit-events").status_code == status


def test_audit_events_list_and_digest(migrated_engine: Engine, events: list[int]) -> None:
    body = client_as(migrated_engine, Role.AUDITOR).get("/api/v1/audit-events").json()

    assert [item["id"] for item in body["items"]] == list(reversed(events))
    newest = body["items"][0]
    assert newest["action"] == "policy.created"
    assert newest["details"] == {"n": 3}
    assert newest["details_digest"] == audit.details_digest({"n": 3})


def test_audit_events_filters_and_paging(migrated_engine: Engine, events: list[int]) -> None:
    client = client_as(migrated_engine, Role.AUDITOR)

    logins = client.get("/api/v1/audit-events", params={"action": "auth.login"}).json()
    by_actor = client.get("/api/v1/audit-events", params={"actor": "user:1"}).json()
    page = client.get("/api/v1/audit-events", params={"limit": 3}).json()
    rest = client.get(
        "/api/v1/audit-events", params={"limit": 3, "before_id": page["next_before_id"]}
    ).json()

    assert len(logins["items"]) == 2
    assert {item["actor"] for item in by_actor["items"]} == {"user:1"}
    assert [item["id"] for item in rest["items"]] == [events[0]]
    assert rest["next_before_id"] is None


def test_audit_events_time_window(migrated_engine: Engine, events: list[int]) -> None:
    client = client_as(migrated_engine, Role.AUDITOR)
    now = datetime.now(UTC)

    recent = client.get(
        "/api/v1/audit-events",
        params={
            "since": (now - timedelta(hours=1)).isoformat(),
            "until": (now + timedelta(hours=1)).isoformat(),
        },
    ).json()
    future = client.get(
        "/api/v1/audit-events", params={"since": (now + timedelta(hours=1)).isoformat()}
    ).json()

    assert len(recent["items"]) == 4
    assert future["items"] == []


@pytest.mark.parametrize(
    "params", [{"since": "2026-10-28T12:00:00"}, {"limit": 201}, {"before_id": -1}]
)
def test_audit_events_rejects_invalid_parameters(
    migrated_engine: Engine, params: dict[str, object]
) -> None:
    response = client_as(migrated_engine, Role.ADMIN).get("/api/v1/audit-events", params=params)

    assert response.status_code == 422
