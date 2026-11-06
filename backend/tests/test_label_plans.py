"""Label plans: building from a fresh read, review, and applying with the separate write token."""

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient
from gitlab_fake import TOKEN, WRITE_TOKEN, LabelServer
from sqlalchemy import Engine, delete, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_labels import (
    ACME,
    TAXONOMY,
    as_user,
    audit_actions,
    engine,
    gitlab_env,
    people,
    put_in_force,
    require_review,
    server,
)

from baluarte.auth.roles import Role
from baluarte.db.models import AuditEvent, CollectionJob, LabelPlan
from baluarte.integrations.gitlab.jobs import COLLECT_LABELS
from baluarte.jobs.worker import Worker
from baluarte.labels.plans import APPLY_LABEL_PLAN, make_apply_label_plan

pytestmark = pytest.mark.db

# Fixtures shared with test_labels.
__all__ = ["engine", "gitlab_env", "people", "server"]

PLANS = "/api/v1/labels/plans"


@pytest.fixture
def ready(engine: Engine, people: dict[str, int]) -> Engine:
    put_in_force(engine, people["ana"])
    return engine


@pytest.fixture
def ana(ready: Engine, people: dict[str, int], server: LabelServer) -> TestClient:
    return as_user(ready, people["ana"], server, Role.ADMIN)


@pytest.fixture
def bea(ready: Engine, people: dict[str, int], server: LabelServer) -> TestClient:
    return as_user(ready, people["bea"], server, Role.ADMIN)


def writes_on(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    monkeypatch.setenv("BALUARTE_LABEL_WRITES", "true")
    stored = client.put("/api/v1/settings/label-write-token", json={"token": WRITE_TOKEN})
    assert stored.status_code == 200, stored.text


def approved_plan(client: TestClient) -> dict[str, Any]:
    created = client.post(PLANS, json={})
    assert created.status_code == 201, created.text
    submitted = client.post(f"{PLANS}/{created.json()['id']}/submit")
    assert submitted.json()["state"] == "approved"
    return submitted.json()


def apply(engine: Engine, server: LabelServer, client: TestClient, plan_id: int) -> dict[str, Any]:
    started = client.post(f"{PLANS}/{plan_id}/apply")
    assert started.status_code == 202, started.text
    assert started.json()["state"] == "applying"
    handler = make_apply_label_plan(server.transport)
    assert Worker(engine, {APPLY_LABEL_PLAN: handler}).run_once()
    return client.get(f"{PLANS}/{plan_id}").json()


def statuses(plan: dict[str, Any]) -> list[str]:
    return [step["status"] for step in plan["steps"]]


# --- Building ----------------------------------------------------------------------------------


def test_a_plan_creates_renames_and_updates_on_the_root_and_lists_the_rest(
    ana: TestClient, server: LabelServer
) -> None:
    created = ana.post(PLANS, json={})

    assert created.status_code == 201
    plan = created.json()
    assert plan["state"] == "draft"
    assert [(s["action"], s["owner"]["path"], s["label"]) for s in plan["steps"]] == [
        ("create", "acme", "bug"),
        ("rename", "acme", "priority::high"),
        ("update", "acme", "docs"),
    ]
    rename = plan["steps"][1]
    assert rename["prior"] == {
        "name": "P1",
        "color": "#d9534f",
        "description": "Must be fixed this sprint",
    }
    assert rename["intended"]["name"] == "priority::high"
    assert len({step["idempotency_key"] for step in plan["steps"]}) == 3
    assert [(m["owner"]["path"], m["kind"], m["label"]) for m in plan["manual"]] == [
        ("acme/team", "misplaced", "bug")
    ]
    assert plan["digest_valid"] and plan["taxonomy"]["name"] == "acme-labels"
    assert plan["can_apply"] == {
        "allowed": False,
        "reasons": ["label_writes_disabled", "write_token_missing", "not_approved"],
    }
    # Built from a fresh read with the read-only token; nothing was written.
    assert {(method, token) for method, _, token in server.requests} == {("GET", TOKEN)}
    assert "DELETE" not in created.text


def test_a_label_is_renamed_only_while_the_canonical_name_is_absent(
    ana: TestClient, server: LabelServer
) -> None:
    server.define(ACME, "priority::high", "#D9534F", description="Must be fixed this sprint")

    plan = ana.post(PLANS, json={}).json()

    assert "rename" not in {step["action"] for step in plan["steps"]}
    blocked = [m for m in plan["manual"] if m["kind"] == "misnamed"]
    assert len(blocked) == 1 and "already defined" in blocked[0]["reason"]


def test_a_plan_needs_drift_in_its_scope(ana: TestClient) -> None:
    refused = ana.post(PLANS, json={"scope": {"groups": ["other"]}})

    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "nothing_to_plan"


# --- Review ------------------------------------------------------------------------------------


def test_approval_is_by_someone_else_and_bound_to_the_reviewed_digest(
    ready: Engine, ana: TestClient, bea: TestClient
) -> None:
    require_review(ready)
    plan = ana.post(PLANS, json={}).json()
    assert ana.post(f"{PLANS}/{plan['id']}/submit").json()["state"] == "submitted"

    own = ana.post(f"{PLANS}/{plan['id']}/approve", json={"plan_digest": plan["plan_digest"]})
    assert own.status_code == 403 and own.json()["detail"]["code"] == "own_change"
    wrong = bea.post(f"{PLANS}/{plan['id']}/approve", json={"plan_digest": "0" * 64})
    assert wrong.status_code == 409 and wrong.json()["detail"]["code"] == "digest_mismatch"
    approved = bea.post(f"{PLANS}/{plan['id']}/approve", json={"plan_digest": plan["plan_digest"]})
    assert approved.json()["state"] == "approved"
    assert approved.json()["self_approved"] is False
    assert approved.json()["decided_by"]["display_name"] == "Bea"
    assert [a for a, _ in audit_actions(ready, "labels.plan")] == [
        "labels.plan_created",
        "labels.plan_submitted",
        "labels.plan_approved",
    ]


def test_the_database_keeps_an_approved_plan_as_approved(ready: Engine, ana: TestClient) -> None:
    plan_id = approved_plan(ana)["id"]

    for statement in (
        update(LabelPlan).where(LabelPlan.id == plan_id).values(plan_digest="0" * 64),
        update(LabelPlan).where(LabelPlan.id == plan_id).values(state="draft"),
        delete(LabelPlan).where(LabelPlan.id == plan_id),
    ):
        with Session(ready) as db, pytest.raises(DBAPIError):
            db.execute(statement)
            db.commit()


# --- Applying ----------------------------------------------------------------------------------


def test_applying_is_refused_while_writes_are_off_or_no_token_is_stored(
    monkeypatch: pytest.MonkeyPatch, ana: TestClient
) -> None:
    plan_id = approved_plan(ana)["id"]

    off = ana.post(f"{PLANS}/{plan_id}/apply")
    assert off.status_code == 409
    assert off.json()["detail"]["code"] == "label_writes_disabled"
    assert off.json()["detail"]["reasons"] == ["label_writes_disabled", "write_token_missing"]

    monkeypatch.setenv("BALUARTE_LABEL_WRITES", "true")
    missing = ana.post(f"{PLANS}/{plan_id}/apply")
    assert missing.status_code == 409
    assert missing.json()["detail"]["code"] == "write_token_missing"


def test_applying_is_refused_once_the_taxonomy_changes(
    monkeypatch: pytest.MonkeyPatch, ready: Engine, ana: TestClient, people: dict[str, int]
) -> None:
    writes_on(monkeypatch, ana)
    plan_id = approved_plan(ana)["id"]
    put_in_force(ready, people["ana"], TAXONOMY.replace("acme-labels", "acme-labels-2"))

    refused = ana.post(f"{PLANS}/{plan_id}/apply")

    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "taxonomy_changed"


def test_applying_writes_once_with_the_write_token_and_reads_back_with_the_read_token(
    monkeypatch: pytest.MonkeyPatch, ready: Engine, ana: TestClient, server: LabelServer
) -> None:
    writes_on(monkeypatch, ana)
    plan_id = approved_plan(ana)["id"]
    server.requests.clear()

    plan = apply(ready, server, ana, plan_id)

    assert plan["state"] == "applied" and plan["finished_at"] is not None
    assert statuses(plan) == ["applied", "applied", "applied"]
    assert all(step["verified"] for step in plan["steps"])
    assert [(m, p) for m, p, _ in server.writes()] == [
        ("POST", "/api/v4/groups/10/labels"),
        ("PUT", "/api/v4/groups/10/labels/1001"),
        ("PUT", "/api/v4/groups/10/labels/1002"),
    ]
    assert {token for method, _, token in server.requests if method != "GET"} == {WRITE_TOKEN}
    assert {token for method, _, token in server.requests if method == "GET"} == {TOKEN}
    names = {(label["name"], label["color"]) for label in server.labels[ACME]}
    assert names == {("bug", "#FF0000"), ("priority::high", "#D9534F"), ("docs", "#0000FF")}
    writes = [details for action, details in audit_actions(ready, "labels.write")]
    assert [(w or {})["status"] for w in writes] == [201, 200, 200]
    assert [a for a, _ in audit_actions(ready, "labels.plan")][-2:] == [
        "labels.plan_applying",
        "labels.plan_applied",
    ]
    with Session(ready) as db:
        assert db.scalars(select(CollectionJob).where(CollectionJob.kind == COLLECT_LABELS)).all()


def test_a_step_already_as_intended_is_skipped(
    monkeypatch: pytest.MonkeyPatch, ready: Engine, ana: TestClient, server: LabelServer
) -> None:
    writes_on(monkeypatch, ana)
    plan_id = approved_plan(ana)["id"]
    server.define(ACME, "bug", "#ff0000")

    plan = apply(ready, server, ana, plan_id)

    assert statuses(plan) == ["skipped", "applied", "applied"]
    assert [m for m, _, _ in server.writes()] == ["PUT", "PUT"]


@pytest.mark.parametrize("status", [400, 403, 500])
def test_the_plan_stops_at_a_refused_write_without_retrying(
    monkeypatch: pytest.MonkeyPatch,
    ready: Engine,
    ana: TestClient,
    server: LabelServer,
    status: int,
) -> None:
    writes_on(monkeypatch, ana)
    plan_id = approved_plan(ana)["id"]
    server.fail("POST", "/groups/10/labels", status)

    plan = apply(ready, server, ana, plan_id)

    assert plan["state"] == "failed" and plan["failed_step"] == 1
    assert statuses(plan) == ["failed", "not_run", "not_run"]
    assert plan["steps"][0]["request_status"] == status
    assert [m for m, _, _ in server.writes()] == ["POST"]


def test_the_plan_stops_when_a_write_does_not_read_back(
    monkeypatch: pytest.MonkeyPatch, ready: Engine, ana: TestClient, server: LabelServer
) -> None:
    writes_on(monkeypatch, ana)
    plan_id = approved_plan(ana)["id"]
    server.ignore_writes = True

    plan = apply(ready, server, ana, plan_id)

    assert plan["state"] == "failed"
    assert plan["steps"][0]["verified"] is False
    assert "does not read back" in plan["failure"]
    assert len(server.writes()) == 1


def test_the_plan_stops_when_a_label_changed_since_it_was_made(
    monkeypatch: pytest.MonkeyPatch, ready: Engine, ana: TestClient, server: LabelServer
) -> None:
    writes_on(monkeypatch, ana)
    plan_id = approved_plan(ana)["id"]
    docs = next(label for label in server.labels[ACME] if label["name"] == "docs")
    docs["color"] = "#123456"

    plan = apply(ready, server, ana, plan_id)

    assert statuses(plan) == ["applied", "applied", "failed"]
    assert "changed since" in plan["failure"]
    assert [m for m, _, _ in server.writes()] == ["POST", "PUT"]


def test_the_applier_stops_if_writes_are_turned_off_after_it_was_queued(
    monkeypatch: pytest.MonkeyPatch, ready: Engine, ana: TestClient, server: LabelServer
) -> None:
    writes_on(monkeypatch, ana)
    plan_id = approved_plan(ana)["id"]
    assert ana.post(f"{PLANS}/{plan_id}/apply").status_code == 202
    monkeypatch.setenv("BALUARTE_LABEL_WRITES", "false")

    assert Worker(ready, {APPLY_LABEL_PLAN: make_apply_label_plan(server.transport)}).run_once()

    plan = ana.get(f"{PLANS}/{plan_id}").json()
    assert plan["state"] == "failed" and "BALUARTE_LABEL_WRITES" in plan["failure"]
    assert server.writes() == []


# --- The write token ---------------------------------------------------------------------------


def test_the_write_token_is_never_returned_logged_or_audited(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    ready: Engine,
    ana: TestClient,
    server: LabelServer,
) -> None:
    caplog.set_level(logging.DEBUG)
    writes_on(monkeypatch, ana)
    status = ana.get("/api/v1/settings/label-write-token")
    plan = apply(ready, server, ana, approved_plan(ana)["id"])

    assert plan["state"] == "applied"
    assert status.json() == {
        "set": True,
        "writes_enabled": True,
        "scopes": ["api"],
        "expires_at": "2027-01-01",
        "token_name": "label-writer",
        "checked_at": status.json()["checked_at"],
        "check_error": None,
    }
    assert WRITE_TOKEN not in status.text
    assert WRITE_TOKEN not in caplog.text
    with Session(ready) as db:
        details = [str(d) for d in db.scalars(select(AuditEvent.details))]
    assert not any(WRITE_TOKEN in d for d in details)
    assert [a for a, _ in audit_actions(ready, "settings.label_write_token")] == [
        "settings.label_write_token_set"
    ]

    assert ana.delete("/api/v1/settings/label-write-token").status_code == 204
    assert ana.get("/api/v1/settings/label-write-token").json()["set"] is False


def test_a_token_without_the_api_scope_is_stored_with_the_reason(
    ana: TestClient, server: LabelServer
) -> None:
    server.token_info["scopes"] = ["read_api"]

    stored = ana.put("/api/v1/settings/label-write-token", json={"token": WRITE_TOKEN})

    assert stored.json()["set"] is True
    assert "api scope" in stored.json()["check_error"]
    blank = ana.put("/api/v1/settings/label-write-token", json={"token": "  "})
    assert blank.status_code == 422 and blank.json()["detail"]["code"] == "token_invalid"
