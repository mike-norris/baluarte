"""Signed GitHub webhooks (roadmap M13.4b).

GitHub is scripted (``test_github_changes.organization``); nothing here reaches github.com.
"""

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select, update
from sqlalchemy.orm import Session
from test_connections import client_as
from test_connections import environment as environment  # the fixture
from test_github import add, collect
from test_github_changes import organization

from baluarte.auth.roles import Role
from baluarte.db.models import (
    AuditEvent,
    ChangeRecord,
    CollectionJob,
    Observation,
    WebhookDelivery,
)
from baluarte.integrations.github.jobs import COLLECT
from baluarte.integrations.github.repositories import LABELS_SOURCE
from baluarte.integrations.github.webhooks import WEBHOOK, make_handle, signature
from baluarte.jobs.worker import Worker

pytestmark = pytest.mark.db

CSRF = {"X-CSRF-Token": "csrf"}


def test_the_signature_matches_githubs_documented_example() -> None:
    assert signature("It's a Secret to Everybody", b"Hello, World!") == (
        "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
    )


def set_up(engine: Engine) -> tuple[int, str, Any]:
    """A collected GitHub connection with a webhook secret: (connection ID, secret, fake)."""
    fake = organization()
    connection_id = int(add(engine).json()["id"])
    collect(engine, fake, connection_id)
    made = client_as(engine, Role.ADMIN).post(
        f"/api/v1/connections/{connection_id}/webhook", headers=CSRF
    )
    assert made.status_code == 201, made.text
    return connection_id, made.json()["secret"], fake


def deliver(
    engine: Engine,
    connection_id: int,
    secret: str,
    event: str,
    payload: dict[str, Any],
    delivery: str,
    sign: str | None = None,
) -> Any:
    body = json.dumps(payload).encode()
    http = TestClient(client_as(engine, Role.ADMIN).app)
    return http.post(
        f"/api/v1/webhooks/github/{connection_id}",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": sign if sign is not None else signature(secret, body),
        },
    )


def merged(number: int = 6) -> dict[str, Any]:
    return {
        "action": "closed",
        "number": number,
        "pull_request": {"number": number, "merged": True},
        "repository": {"id": 1, "full_name": "acme/api"},
    }


def run_webhooks(engine: Engine, fake: Any) -> int:
    """Run every webhook job now (their delay has passed); how many ran."""
    with Session(engine) as db:
        db.execute(
            update(CollectionJob)
            .where(CollectionJob.kind == WEBHOOK)
            .values(run_after=CollectionJob.created_at)
        )
        db.commit()
    worker = Worker(engine, {WEBHOOK: make_handle(fake.transport)})
    ran = 0
    while worker.run_once():
        ran += 1
    return ran


def deliveries(engine: Engine) -> dict[str, str]:
    with Session(engine) as db:
        return {row.delivery_id: row.status for row in db.scalars(select(WebhookDelivery))}


def test_setting_up_shows_the_secret_once_and_audits_without_it(
    migrated_engine: Engine, environment: None
) -> None:
    connection_id, secret, _ = set_up(migrated_engine)
    http = client_as(migrated_engine, Role.ADMIN)
    shown = http.get(f"/api/v1/connections/{connection_id}/webhook").json()
    assert shown["configured"] is True
    assert shown["url"].endswith(f"/api/v1/webhooks/github/{connection_id}")
    assert "pull_request" in shown["events"]
    assert secret not in json.dumps(shown)
    with Session(migrated_engine) as db:
        events = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "connection.webhook_secret_set")
        ).all()
    assert len(events) == 1 and secret not in json.dumps(events[0].details)
    # A GitLab connection has no webhook here.
    gitlab = http.post(
        "/api/v1/connections",
        json={"url": "https://second.example.test", "token": "glpat-example"},
        headers=CSRF,
    )
    assert gitlab.status_code == 201, gitlab.text
    refused = http.post(f"/api/v1/connections/{gitlab.json()['id']}/webhook", headers=CSRF)
    assert refused.status_code == 409
    # Turning it off makes deliveries refused.
    assert (
        http.delete(f"/api/v1/connections/{connection_id}/webhook", headers=CSRF).status_code == 204
    )
    gone = deliver(migrated_engine, connection_id, secret, "ping", {"zen": "x"}, "d-off")
    assert gone.status_code == 404


def test_deliveries_are_verified_recorded_once_and_refused_otherwise(
    migrated_engine: Engine, environment: None
) -> None:
    connection_id, secret, _ = set_up(migrated_engine)
    first = deliver(migrated_engine, connection_id, secret, "pull_request", merged(), "d-1")
    assert first.status_code == 202 and first.json() == {"status": "queued"}
    again = deliver(migrated_engine, connection_id, secret, "pull_request", merged(), "d-1")
    assert again.json() == {"status": "duplicate"}
    ping = deliver(migrated_engine, connection_id, secret, "ping", {"zen": "x"}, "d-2")
    assert ping.json() == {"status": "ignored"}
    forged = deliver(
        migrated_engine, connection_id, secret, "pull_request", merged(), "d-3", sign="sha256=00"
    )
    assert forged.status_code == 401
    unsigned = deliver(migrated_engine, connection_id, "wrong", "pull_request", merged(), "d-4")
    assert unsigned.status_code == 401
    bad_id = deliver(migrated_engine, connection_id, secret, "ping", {}, "not valid!")
    assert bad_id.status_code == 400
    nobody = deliver(migrated_engine, 999999, secret, "ping", {}, "d-5")
    assert nobody.status_code == 404
    http = TestClient(client_as(migrated_engine, Role.ADMIN).app)
    huge = http.post(
        f"/api/v1/webhooks/github/{connection_id}",
        content=b"{}",
        headers={
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": "d-6",
            "Content-Length": str(6 * 1024 * 1024),
        },
    )
    assert huge.status_code == 413
    # Only verified deliveries are recorded; the body itself never is.
    assert deliveries(migrated_engine) == {"d-1": "queued", "d-2": "ignored"}
    with Session(migrated_engine) as db:
        row = db.scalars(select(WebhookDelivery).where(WebhookDelivery.delivery_id == "d-1")).one()
        jobs = db.scalars(select(CollectionJob).where(CollectionJob.kind == WEBHOOK)).all()
    assert (row.event, row.action, row.repository_id) == ("pull_request", "closed", 1)
    assert len(jobs) == 1


def test_a_burst_of_change_events_is_read_once(migrated_engine: Engine, environment: None) -> None:
    connection_id, secret, fake = set_up(migrated_engine)
    with Session(migrated_engine) as db:
        db.execute(delete(ChangeRecord))
        db.commit()
    for n, event in enumerate(["pull_request", "pull_request_review", "push"]):
        payload = merged() if event == "pull_request" else {"repository": {"id": 1}}
        accepted = deliver(migrated_engine, connection_id, secret, event, payload, f"b-{n}")
        assert accepted.json()["status"] == "queued"
    fake.requests.clear()
    assert run_webhooks(migrated_engine, fake) == 3
    assert deliveries(migrated_engine) == {f"b-{n}": "processed" for n in range(3)}
    # One read of the repository's pull requests served all three deliveries.
    listings = [r for r in fake.requests if r.url.path == "/repos/acme/api/pulls"]
    assert len(listings) == 1
    assert {r.method for r in fake.requests} == {"GET"}
    with Session(migrated_engine) as db:
        kinds = {row.kind for row in db.scalars(select(ChangeRecord))}
        evaluations = db.scalars(
            select(CollectionJob).where(CollectionJob.kind == "policy.evaluate")
        ).all()
    assert kinds == {"merge_request", "direct_push", "deployment"}
    assert evaluations


def test_organization_events_queue_one_full_collection(
    migrated_engine: Engine, environment: None
) -> None:
    connection_id, secret, fake = set_up(migrated_engine)
    for n in range(2):
        deliver(
            migrated_engine,
            connection_id,
            secret,
            "member",
            {"action": "added", "member": {"login": "new"}},
            f"o-{n}",
        )
    run_webhooks(migrated_engine, fake)
    with Session(migrated_engine) as db:
        queued = db.scalars(
            select(CollectionJob).where(
                CollectionJob.kind == COLLECT, CollectionJob.state == "queued"
            )
        ).all()
    assert len(queued) == 1
    assert set(deliveries(migrated_engine).values()) == {"processed"}


def test_removing_the_connection_stops_its_webhook(
    migrated_engine: Engine, environment: None
) -> None:
    connection_id, secret, _ = set_up(migrated_engine)
    removed = client_as(migrated_engine, Role.ADMIN).delete(
        f"/api/v1/connections/{connection_id}", headers=CSRF
    )
    assert removed.status_code == 204
    assert deliver(migrated_engine, connection_id, secret, "ping", {}, "r-1").status_code == 404


def test_a_label_event_reads_that_repositorys_labels_again(
    migrated_engine: Engine, environment: None
) -> None:
    connection_id, secret, fake = set_up(migrated_engine)
    fake.routes["GET /repos/acme/api/labels"] = [
        {"id": 71, "name": "bug", "color": "d73a4a", "description": None}
    ]
    payload = {"action": "created", "label": {"name": "bug"}, "repository": {"id": 1}}
    deliver(migrated_engine, connection_id, secret, "label", payload, "l-1")
    fake.requests.clear()
    run_webhooks(migrated_engine, fake)
    assert [r.url.path for r in fake.requests] == ["/repos/acme/api/labels"]
    assert deliveries(migrated_engine) == {"l-1": "processed"}
    with Session(migrated_engine) as db:
        latest = db.scalars(
            select(Observation)
            .where(Observation.source == LABELS_SOURCE)
            .order_by(Observation.id.desc())
        ).first()
    assert latest is not None and latest.collection_job_id is not None
    assert latest.collection_run_id is None
    assert "bug" in json.dumps(latest.payload)


def test_a_failed_read_marks_the_delivery_failed(
    migrated_engine: Engine, environment: None
) -> None:
    connection_id, secret, fake = set_up(migrated_engine)
    fake.routes["GET /repos/acme/api/pulls"] = lambda _: httpx.Response(
        500, json={"message": "boom"}
    )
    deliver(migrated_engine, connection_id, secret, "push", {"repository": {"id": 1}}, "f-1")
    run_webhooks(migrated_engine, fake)
    with Session(migrated_engine) as db:
        row = db.scalars(select(WebhookDelivery)).one()
    assert row.status == "failed" and row.error
