"""Connections added on the Connections page (roadmap M11.3)."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from gitlab_fake import TOKEN, URL, FakeGitLab, paged, project
from oidc_fake import make_settings
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from test_setup import write_key

from baluarte import connections
from baluarte.api.v1.gitlab import gitlab_transport
from baluarte.auth.dependencies import Principal, auth_settings, current_principal, database_engine
from baluarte.auth.roles import Role
from baluarte.connections import ConnectionRefused, check_url, payload_for
from baluarte.db.models import AuditEvent, CollectionJob, GitLabInstance, StoredSecret
from baluarte.integrations.gitlab.instances import instance_for
from baluarte.integrations.gitlab.jobs import (
    DISCOVER_PROJECTS,
    SCHEDULED_KINDS,
    AddedConnectionsSchedule,
    ConfiguredSchedule,
    make_discover_projects,
)
from baluarte.jobs import queue
from baluarte.jobs.worker import Worker
from baluarte.main import create_app
from baluarte.policy.evaluation.runner import EVALUATE, request_evaluation

pytestmark = pytest.mark.db

SECOND = "https://second.example.test"
SECOND_TOKEN = "glpat-second-connection-token"
PUBLIC = "93.184.216.34"


def addresses(table: dict[str, list[str]]) -> Callable[[str], list[str]]:
    def lookup(host: str) -> list[str]:
        if host not in table:
            raise ConnectionRefused(f"{host} could not be resolved")
        return table[host]

    return lookup


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("BALUARTE_SECRET_KEY_PREVIOUS_FILE", raising=False)
    key = tmp_path / "secret-key"
    write_key(key)
    monkeypatch.setenv("BALUARTE_SECRET_KEY_FILE", str(key))
    monkeypatch.setattr(connections, "dns_lookup", addresses({"second.example.test": [PUBLIC]}))


def client_as(engine: Engine, *roles: Role, fake: FakeGitLab | None = None) -> TestClient:
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
    if fake is not None:
        app.dependency_overrides[gitlab_transport] = lambda: fake.transport
    return TestClient(app)


def add_second(engine: Engine, **extra: object) -> dict[str, object]:
    response = client_as(engine, Role.ADMIN).post(
        "/api/v1/connections",
        json={"name": "Second", "url": SECOND, "token": SECOND_TOKEN, **extra},
    )
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


# The address check


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "169.254.169.254",
        "fe80::1",
        "0.0.0.0",  # noqa: S104 - an address the check refuses
        "224.0.0.1",
        "::ffff:127.0.0.1",
    ],
)
def test_loopback_link_local_and_metadata_addresses_are_always_refused(address: str) -> None:
    with pytest.raises(ConnectionRefused, match="never allowed"):
        check_url("https://host.example.test", True, lambda _: [address])


@pytest.mark.parametrize("address", ["10.0.0.5", "172.16.1.1", "192.168.1.10", "fd00::5"])
def test_private_addresses_need_the_connection_to_allow_them(address: str) -> None:
    with pytest.raises(ConnectionRefused, match="private address"):
        check_url("https://host.example.test", False, lambda _: [address])
    check_url("https://host.example.test", True, lambda _: [address])


def test_one_refused_address_among_several_refuses_the_host() -> None:
    with pytest.raises(ConnectionRefused, match=r"169\.254\.169\.254"):
        check_url("https://host.example.test", True, lambda _: [PUBLIC, "169.254.169.254"])


def test_a_public_address_is_allowed() -> None:
    check_url("https://host.example.test", False, lambda _: [PUBLIC])


# The API


def test_an_admin_adds_a_connection_whose_token_is_sealed_and_never_returned(
    migrated_engine: Engine, environment: None
) -> None:
    body = add_second(migrated_engine, sync_interval_minutes=60)

    assert body["url"] == SECOND
    assert body["primary"] is False
    assert body["source"] == "connections_page"
    assert body["token"] == "configured"
    assert body["sync_interval_minutes"] == 60
    assert body["health"] == "never"
    with Session(migrated_engine) as db:
        secret = db.get_one(StoredSecret, f"connection_token:{body['id']}")
        assert SECOND_TOKEN.encode() not in bytes(secret.ciphertext)
        [event] = db.scalars(select(AuditEvent).where(AuditEvent.action == "connection.added"))
        assert event.details["url"] == SECOND
        assert SECOND_TOKEN not in str(event.details)
    listed = client_as(migrated_engine, Role.ANALYST).get("/api/v1/connections")
    assert listed.status_code == 200
    assert [c["url"] for c in listed.json()["connections"]] == [SECOND]
    assert SECOND_TOKEN not in listed.text


@pytest.mark.parametrize("role", [Role.ANALYST, Role.AUDITOR])
def test_only_admins_add_connections(
    migrated_engine: Engine, environment: None, role: Role
) -> None:
    response = client_as(migrated_engine, role).post(
        "/api/v1/connections", json={"url": SECOND, "token": SECOND_TOKEN}
    )

    assert response.status_code == 403


def test_a_private_host_is_refused_unless_allowed(
    migrated_engine: Engine, environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connections, "dns_lookup", addresses({"second.example.test": ["10.1.2.3"]}))
    client = client_as(migrated_engine, Role.ADMIN)

    refused = client.post("/api/v1/connections", json={"url": SECOND, "token": SECOND_TOKEN})
    allowed = client.post(
        "/api/v1/connections",
        json={"url": SECOND, "token": SECOND_TOKEN, "allow_private_network": True},
    )

    assert refused.status_code == 422
    assert "private address" in refused.json()["detail"]
    assert allowed.status_code == 201


@pytest.mark.parametrize(
    ("url", "expected"),
    [("http://second.example.test", 422), (URL, 409), ("https://unknown.example.test", 422)],
)
def test_bad_urls_and_the_primary_url_are_refused(
    migrated_engine: Engine, environment: None, url: str, expected: int
) -> None:
    response = client_as(migrated_engine, Role.ADMIN).post(
        "/api/v1/connections", json={"url": url, "token": SECOND_TOKEN}
    )

    assert response.status_code == expected


def test_a_url_is_connected_once(migrated_engine: Engine, environment: None) -> None:
    add_second(migrated_engine)

    again = client_as(migrated_engine, Role.ADMIN).post(
        "/api/v1/connections", json={"url": SECOND, "token": SECOND_TOKEN}
    )

    assert again.status_code == 409


def test_a_token_cannot_be_stored_without_the_secret_key(
    migrated_engine: Engine, environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BALUARTE_SECRET_KEY_FILE")

    response = client_as(migrated_engine, Role.ADMIN).post(
        "/api/v1/connections", json={"url": SECOND, "token": SECOND_TOKEN}
    )

    assert response.status_code == 409
    assert "BALUARTE_SECRET_KEY_FILE" in response.json()["detail"]


def test_changing_pausing_and_replacing_the_token_are_audited(
    migrated_engine: Engine, environment: None
) -> None:
    connection_id = add_second(migrated_engine)["id"]
    client = client_as(migrated_engine, Role.ADMIN)

    changed = client.patch(
        f"/api/v1/connections/{connection_id}",
        json={"name": "Renamed", "paused": True, "token": "glpat-replaced", "timeout_seconds": 30},
    )

    assert changed.status_code == 200
    body = changed.json()
    assert (body["name"], body["paused"], body["timeout_seconds"]) == ("Renamed", True, 30)
    with Session(migrated_engine) as db:
        [event] = db.scalars(select(AuditEvent).where(AuditEvent.action == "connection.changed"))
    assert event.details["token_replaced"] is True
    assert "glpat-replaced" not in str(event.details)
    bad = client.patch(f"/api/v1/connections/{connection_id}", json={"sync_interval_minutes": 5})
    assert bad.status_code == 422


def test_the_primary_connection_keeps_its_token_and_timing_on_the_settings_page(
    migrated_engine: Engine, environment: None
) -> None:
    with Session(migrated_engine) as db:
        primary = instance_for(db, URL)
        db.commit()
        primary_id = primary.id
    client = client_as(migrated_engine, Role.ADMIN)

    token = client.patch(f"/api/v1/connections/{primary_id}", json={"token": "glpat-x"})
    paused = client.patch(f"/api/v1/connections/{primary_id}", json={"paused": True})
    removed = client.delete(f"/api/v1/connections/{primary_id}")

    assert token.status_code == 409
    assert paused.status_code == 200
    assert paused.json()["primary"] is True
    assert paused.json()["source"] == "deployment"
    assert removed.status_code == 409


def test_removing_keeps_the_data_and_adding_the_url_again_restores_it(
    migrated_engine: Engine, environment: None
) -> None:
    connection_id = add_second(migrated_engine)["id"]
    client = client_as(migrated_engine, Role.ADMIN)

    assert client.delete(f"/api/v1/connections/{connection_id}").status_code == 204

    assert client.get("/api/v1/connections").json()["connections"] == []
    with Session(migrated_engine) as db:
        assert db.get(StoredSecret, f"connection_token:{connection_id}") is None
        assert db.get_one(GitLabInstance, connection_id).removed_at is not None
    assert client.delete(f"/api/v1/connections/{connection_id}").status_code == 404
    restored = add_second(migrated_engine)
    assert restored["id"] == connection_id


def test_diagnostics_use_the_connections_own_token(
    migrated_engine: Engine, environment: None
) -> None:
    connection_id = add_second(migrated_engine)["id"]
    fake = FakeGitLab()

    response = client_as(migrated_engine, Role.ADMIN, fake=fake).post(
        f"/api/v1/connections/{connection_id}/diagnostics"
    )

    assert response.status_code == 200
    assert response.json()["gitlab_url"] == SECOND
    assert {request.url.host for request in fake.requests} == {"second.example.test"}
    assert {request.headers["PRIVATE-TOKEN"] for request in fake.requests} == {SECOND_TOKEN}
    assert SECOND_TOKEN not in response.text


def test_collect_now_queues_the_connections_jobs(
    migrated_engine: Engine, environment: None
) -> None:
    connection_id = add_second(migrated_engine)["id"]

    response = client_as(migrated_engine, Role.ADMIN).post(
        f"/api/v1/connections/{connection_id}/collect"
    )

    assert response.status_code == 202
    with Session(migrated_engine) as db:
        jobs = list(db.scalars(select(CollectionJob).order_by(CollectionJob.id)))
    assert [job.kind for job in jobs] == list(SCHEDULED_KINDS)
    assert {job.payload["connection_id"] for job in jobs} == {connection_id}


# Jobs and the schedule


def db_jobs(engine: Engine) -> list[CollectionJob]:
    with Session(engine) as db:
        return list(db.scalars(select(CollectionJob)))


def test_a_job_collects_the_connection_it_names_with_that_token(
    migrated_engine: Engine, environment: None
) -> None:
    connection_id = add_second(migrated_engine)["id"]
    assert isinstance(connection_id, int)
    fake = FakeGitLab()
    fake.set("/projects", paged("/projects", [[project(42, "acme/web", 7)]], base=SECOND))
    with Session(migrated_engine) as db:
        queue.enqueue(db, DISCOVER_PROJECTS, "test", payload_for(connection_id))
        db.commit()

    assert Worker(
        migrated_engine, {DISCOVER_PROJECTS: make_discover_projects(fake.transport)}
    ).run_once()

    assert fake.requests
    assert {request.url.host for request in fake.requests} == {"second.example.test"}
    assert {request.headers["PRIVATE-TOKEN"] for request in fake.requests} == {SECOND_TOKEN}
    with Session(migrated_engine) as db:
        follow_ups = list(
            db.scalars(select(CollectionJob).where(CollectionJob.kind != DISCOVER_PROJECTS))
        )
    assert follow_ups, [(job.state, job.error) for job in db_jobs(migrated_engine)]
    assert {job.payload.get("connection_id") for job in follow_ups} == {connection_id}


def test_a_job_for_a_removed_connection_fails_without_calling_gitlab(
    migrated_engine: Engine, environment: None
) -> None:
    connection_id = add_second(migrated_engine)["id"]
    assert isinstance(connection_id, int)
    client_as(migrated_engine, Role.ADMIN).delete(f"/api/v1/connections/{connection_id}")
    fake = FakeGitLab()
    with Session(migrated_engine) as db:
        job_id = queue.enqueue(db, DISCOVER_PROJECTS, "test", payload_for(connection_id))
        db.commit()

    Worker(migrated_engine, {DISCOVER_PROJECTS: make_discover_projects(fake.transport)}).run_once()

    assert fake.requests == []
    with Session(migrated_engine) as db:
        job = db.get_one(CollectionJob, job_id)
    assert job.state == "failed"
    assert "removed" in (job.error or "")


def test_a_job_is_refused_when_the_host_now_resolves_to_a_refused_address(
    migrated_engine: Engine, environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection_id = add_second(migrated_engine)["id"]
    assert isinstance(connection_id, int)
    monkeypatch.setattr(
        connections, "dns_lookup", addresses({"second.example.test": ["169.254.169.254"]})
    )
    fake = FakeGitLab()
    with Session(migrated_engine) as db:
        queue.enqueue(db, DISCOVER_PROJECTS, "test", payload_for(connection_id))
        db.commit()

    Worker(migrated_engine, {DISCOVER_PROJECTS: make_discover_projects(fake.transport)}).run_once()

    assert fake.requests == []


def test_the_schedule_queues_each_added_connection_once_per_its_interval(
    migrated_engine: Engine, environment: None
) -> None:
    first = add_second(migrated_engine, sync_interval_minutes=60)["id"]
    paused = client_as(migrated_engine, Role.ADMIN).post(
        "/api/v1/connections",
        json={"url": "https://third.example.test", "token": "glpat-third"},
    )
    assert paused.status_code == 422  # not resolvable in this test
    schedule = AddedConnectionsSchedule(migrated_engine, check_every=0)

    schedule()
    schedule()

    with Session(migrated_engine) as db:
        jobs = list(db.scalars(select(CollectionJob)))
    assert sorted(job.kind for job in jobs) == sorted(SCHEDULED_KINDS)
    assert {job.payload["connection_id"] for job in jobs} == {first}
    assert all(f"connection-{first}:" in job.idempotency_key for job in jobs)


def test_a_paused_connection_is_not_scheduled(migrated_engine: Engine, environment: None) -> None:
    connection_id = add_second(migrated_engine)["id"]
    client_as(migrated_engine, Role.ADMIN).patch(
        f"/api/v1/connections/{connection_id}", json={"paused": True}
    )

    AddedConnectionsSchedule(migrated_engine, check_every=0)()

    with Session(migrated_engine) as db:
        assert list(db.scalars(select(CollectionJob))) == []


def test_a_paused_primary_connection_is_not_scheduled(
    migrated_engine: Engine, environment: None
) -> None:
    with Session(migrated_engine) as db:
        instance_for(db, URL).paused = True
        db.commit()
    schedule = ConfiguredSchedule(migrated_engine, check_every=0)

    schedule()

    assert schedule.interval_minutes is None
    assert db_jobs(migrated_engine) == []


def test_a_governance_change_queues_an_evaluation_of_every_connection(
    migrated_engine: Engine, environment: None
) -> None:
    connection_id = add_second(migrated_engine)["id"]
    with Session(migrated_engine) as db:
        primary_job = request_evaluation(db, "test")
        again = request_evaluation(db, "test")
        db.commit()

    assert primary_job is not None
    assert again is None
    evaluations = [job for job in db_jobs(migrated_engine) if job.kind == EVALUATE]
    assert sorted(str(job.payload.get("connection_id")) for job in evaluations) == sorted(
        ["None", str(connection_id)]
    )
