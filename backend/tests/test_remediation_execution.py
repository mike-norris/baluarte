"""Applying approved remediation plans (BAL-032): the separate write token, idempotent steps,
verification through the read token, failure paths and the audit trail."""

import base64
import inspect
import logging
import os
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient
from gitlab_fake import TOKEN, URL
from sqlalchemy import Engine, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_evaluation_project import observe_protection, protection, set_probe
from test_evaluation_store import VISIBILITY_ONLY, World
from test_exceptions import client_as, user
from test_policy_schema import BASELINE

from baluarte.api.v1.gitlab import gitlab_transport
from baluarte.auth.roles import Role
from baluarte.db.models import AuditEvent, CollectionJob, Finding, GitLabProject, RemediationPlan
from baluarte.integrations.gitlab import remediation_writer
from baluarte.integrations.gitlab.jobs import DISCOVER_PROJECTS
from baluarte.integrations.gitlab.remediation_writer import RemediationWriter
from baluarte.integrations.gitlab.settings import GitLabSettings
from baluarte.jobs.worker import Worker
from baluarte.remediation.execute import make_apply_remediation_plan, protected_as_intended
from baluarte.remediation.plans import APPLY_REMEDIATION_PLAN

pytestmark = pytest.mark.db

PLANS = "/api/v1/remediation/plans"
WRITE_TOKEN = "fake-remediation-write-token-value"
LABEL_TOKEN = "fake-label-write-token-value"


class Server:
    """A GitLab that keeps projects and their protected branches; writes change its state.

    Requests are recorded with the token that sent them and the form they carried. ``fail``
    answers the next request to a (method, path) with a status instead; ``ignore_writes``
    accepts writes without changing anything; ``conflict`` answers a protection POST with 409
    after creating the rule, as if someone else had just protected the branch.
    """

    def __init__(self) -> None:
        self.projects: dict[int, dict[str, Any]] = {}
        self.rules: dict[int, list[dict[str, Any]]] = {}
        self.requests: list[tuple[str, str, str | None, dict[str, str]]] = []
        self.failures: dict[tuple[str, str], int] = {}
        self.ignore_writes = False
        self.conflict = False

    def project(self, gitlab_id: int, visibility: str, default_branch: str = "main") -> None:
        self.projects[gitlab_id] = {
            "id": gitlab_id,
            "visibility": visibility,
            "archived": False,
            "default_branch": default_branch,
        }
        self.rules.setdefault(gitlab_id, [])

    def writes(self) -> list[tuple[str, str, str | None, dict[str, str]]]:
        return [r for r in self.requests if r[0] != "GET"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v4")
        token = request.headers.get("PRIVATE-TOKEN")
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.requests.append((request.method, path, token, form))
        if (request.method, path) in self.failures:
            code = self.failures.pop((request.method, path))
            return httpx.Response(code, json={"message": f"{code} refused"})
        if path == "/personal_access_tokens/self":
            return httpx.Response(
                200,
                json={"name": "remediator", "scopes": ["api"], "active": True, "revoked": False},
            )
        parts = path.strip("/").split("/")
        gitlab_id = int(parts[1])
        project = self.projects.get(gitlab_id)
        if project is None:
            return httpx.Response(404, json={"message": "404 Project Not Found"})
        if len(parts) == 2 and request.method == "GET":
            return httpx.Response(200, json=project)
        if len(parts) == 2 and request.method == "PUT":
            if not self.ignore_writes:
                project["visibility"] = form["visibility"]
            return httpx.Response(200, json=project)
        if parts[2:] == ["protected_branches"] and request.method == "GET":
            return httpx.Response(200, json=self.rules[gitlab_id])
        if parts[2:] == ["protected_branches"] and request.method == "POST":
            rule = {
                "name": form["name"],
                **{
                    f"{kind}_access_levels": [
                        {"access_level": int(form[f"{kind}_access_level"]), "user_id": None}
                    ]
                    for kind in ("push", "merge", "unprotect")
                },
            }
            if any(r["name"] == form["name"] for r in self.rules[gitlab_id]):
                return httpx.Response(409, json={"message": "Protected branch already exists"})
            if not self.ignore_writes:
                self.rules[gitlab_id].append(rule)
            if self.conflict:
                return httpx.Response(409, json={"message": "Protected branch already exists"})
            return httpx.Response(201, json=rule)
        return httpx.Response(405, json={"message": "405 Method Not Allowed"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("BALUARTE_REMEDIATION_WRITES", raising=False)
    monkeypatch.delenv("BALUARTE_LABEL_WRITES", raising=False)
    monkeypatch.delenv("BALUARTE_SECRET_KEY_PREVIOUS_FILE", raising=False)
    key = tmp_path / "secret-key"
    key.write_text(base64.b64encode(os.urandom(32)).decode() + "\n")
    monkeypatch.setenv("BALUARTE_SECRET_KEY_FILE", str(key))


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every request must go to the fake GitLab; the real network transport fails the test."""

    def refuse(self: httpx.BaseTransport, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP request: {request.method} {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)


@pytest.fixture
def server() -> Server:
    return Server()


@pytest.fixture
def policy() -> str:
    """The baseline assigned to the instance; parametrize with BASELINE for branch protection."""
    return VISIBILITY_ONLY


@pytest.fixture
def world(migrated_engine: Engine, policy: str) -> World:
    world = World(migrated_engine)
    world.assign(policy)
    if policy == BASELINE:
        set_probe(world.engine, world.instance_id, "supported")
    return world


def unprotected(world: World, server: Server) -> None:
    """Project 1 is private with an unprotected default branch, in the evidence and in GitLab."""
    world.project(1, "private", default_branch="main")
    observe_protection(world.engine, world.instance_id, 1, protection("main", False))
    world.run()
    server.project(1, "private")


@pytest.fixture
def people(world: World) -> dict[str, int]:
    return {name: user(world.engine, name) for name in ("ana", "bea", "cy")}


def as_user(world: World, user_id: int, server: Server, role: Role) -> TestClient:
    client = client_as(world.engine, user_id, role)
    client.app.dependency_overrides[gitlab_transport] = lambda: server.transport  # type: ignore[attr-defined]
    return client


@pytest.fixture
def ana(world: World, people: dict[str, int], server: Server) -> TestClient:
    return as_user(world, people["ana"], server, Role.ANALYST)


@pytest.fixture
def bea(world: World, people: dict[str, int], server: Server) -> TestClient:
    return as_user(world, people["bea"], server, Role.ADMIN)


def finding_ids(world: World, *gitlab_ids: int) -> list[int]:
    with Session(world.engine) as db:
        rows = db.execute(
            select(GitLabProject.gitlab_id, Finding.id).join(
                GitLabProject, GitLabProject.id == Finding.project_id
            )
        ).all()
    found = dict(rows)
    return [found[gitlab_id] for gitlab_id in gitlab_ids]


def approved(world: World, ana: TestClient, bea: TestClient, *gitlab_ids: int) -> dict[str, Any]:
    created = ana.post(PLANS, json={"scope": {"finding_ids": finding_ids(world, *gitlab_ids)}})
    assert created.status_code == 201, created.text
    plan_id = created.json()["id"]
    assert ana.post(f"{PLANS}/{plan_id}/submit").status_code == 200
    decided = bea.post(
        f"{PLANS}/{plan_id}/approve", json={"plan_digest": created.json()["plan_digest"]}
    )
    assert decided.status_code == 200, decided.text
    result: dict[str, Any] = decided.json()
    return result


def writes_on(monkeypatch: pytest.MonkeyPatch, admin: TestClient) -> None:
    monkeypatch.setenv("BALUARTE_REMEDIATION_WRITES", "true")
    stored = admin.put("/api/v1/settings/remediation-write-token", json={"token": WRITE_TOKEN})
    assert stored.status_code == 200, stored.text


def run(world: World, server: Server) -> None:
    handler = make_apply_remediation_plan(server.transport)
    assert Worker(world.engine, {APPLY_REMEDIATION_PLAN: handler}).run_once()


def apply(world: World, server: Server, client: TestClient, plan_id: int) -> dict[str, Any]:
    started = client.post(f"{PLANS}/{plan_id}/apply")
    assert started.status_code == 202, started.text
    assert started.json()["state"] == "applying"
    run(world, server)
    result: dict[str, Any] = client.get(f"{PLANS}/{plan_id}").json()
    return result


def statuses(plan: dict[str, Any]) -> list[str]:
    return [step["status"] for step in plan["progress"]]


def audit_actions(world: World, plan_id: int) -> list[tuple[str, dict[str, Any] | None]]:
    with Session(world.engine) as db:
        return [
            (row.action, row.details)
            for row in db.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.resource_type == "remediation_plan",
                    AuditEvent.resource_id == str(plan_id),
                )
                .order_by(AuditEvent.id)
            )
        ]


@pytest.fixture
def public(world: World, server: Server) -> Iterator[None]:
    """Project 1 is public in Baluarte's evidence and in GitLab."""
    world.project(1, "public")
    world.run()
    server.project(1, "public")
    yield


# --- Applying -----------------------------------------------------------------------------------


@pytest.mark.usefixtures("public")
def test_a_public_project_is_restricted_and_verified(
    monkeypatch: pytest.MonkeyPatch, world: World, server: Server, ana: TestClient, bea: TestClient
) -> None:
    writes_on(monkeypatch, bea)
    plan = approved(world, ana, bea, 1)
    assert plan["can_apply"] == {"allowed": True, "reasons": []}
    server.requests.clear()

    done = apply(world, server, ana, plan["id"])

    assert done["state"] == "applied"
    assert done["applied_by_user_id"] == plan["created_by_user_id"]
    (step,) = done["progress"]
    assert step["status"] == "applied"
    assert (step["request_status"], step["verified"]) == (200, True)
    assert step["observed"] == {"visibility": "private"}
    # Read with the read token, written once with the write token, read back with the read token.
    assert [(m, p, t) for m, p, t, _ in server.requests] == [
        ("GET", "/projects/1", TOKEN),
        ("PUT", "/projects/1", WRITE_TOKEN),
        ("GET", "/projects/1", TOKEN),
    ]
    assert server.writes()[0][3] == {"visibility": "private"}
    assert server.projects[1]["visibility"] == "private"
    actions = [action for action, _ in audit_actions(world, plan["id"])]
    assert actions == [
        "remediation_plan.created",
        "remediation_plan.submitted",
        "remediation_plan.approved",
        "remediation_plan.applying",
        "remediation_plan.write",
        "remediation_plan.step",
        "remediation_plan.applied",
    ]
    write = dict(audit_actions(world, plan["id"]))["remediation_plan.write"]
    assert write is not None
    assert (write["method"], write["path"], write["status"]) == ("PUT", "/projects/1", 200)
    assert write["idempotency_key"] == plan["plan"]["steps"][0]["idempotency_key"]
    # Findings resolve through a fresh read, not because the write succeeded.
    with Session(world.engine) as db:
        kinds = set(db.scalars(select(CollectionJob.kind)))
        assert DISCOVER_PROJECTS in kinds
        finding = db.get_one(Finding, plan["plan"]["steps"][0]["finding_id"])
        assert finding.state == "open"


@pytest.mark.parametrize("policy", [BASELINE])
def test_an_unprotected_default_branch_is_protected_and_verified(
    monkeypatch: pytest.MonkeyPatch, world: World, server: Server, ana: TestClient, bea: TestClient
) -> None:
    unprotected(world, server)
    writes_on(monkeypatch, bea)
    plan = approved(world, ana, bea, 1)
    server.requests.clear()

    done = apply(world, server, ana, plan["id"])

    assert done["state"] == "applied", done
    assert done["progress"][0]["request_status"] == 201
    assert done["progress"][0]["observed"] == {"default_branch": "main", "rules": ["main"]}
    ((method, path, token, form),) = server.writes()
    assert (method, path, token) == ("POST", "/projects/1/protected_branches", WRITE_TOKEN)
    assert form == {
        "name": "main",
        "push_access_level": "40",
        "merge_access_level": "40",
        "unprotect_access_level": "40",
    }


@pytest.mark.usefixtures("public")
def test_a_change_already_made_is_skipped_without_writing(
    monkeypatch: pytest.MonkeyPatch, world: World, server: Server, ana: TestClient, bea: TestClient
) -> None:
    writes_on(monkeypatch, bea)
    plan = approved(world, ana, bea, 1)
    server.projects[1]["visibility"] = "private"  # someone made the change by hand

    done = apply(world, server, ana, plan["id"])

    assert done["state"] == "applied"
    assert statuses(done) == ["skipped"]
    assert done["progress"][0]["verified"] is True
    assert server.writes() == []


def test_an_out_of_date_step_is_not_written_and_the_rest_still_run(
    monkeypatch: pytest.MonkeyPatch, world: World, server: Server, ana: TestClient, bea: TestClient
) -> None:
    for gitlab_id in (1, 2):
        world.project(gitlab_id, "public")
        server.project(gitlab_id, "public")
    world.run()
    writes_on(monkeypatch, bea)
    plan = approved(world, ana, bea, 1, 2)
    server.projects[1]["visibility"] = "internal"  # changed since the plan, not to its value

    done = apply(world, server, ana, plan["id"])

    assert done["state"] == "applied"
    assert statuses(done) == ["out_of_date", "applied"]
    assert "now internal, not public" in done["progress"][0]["message"]
    assert [path for _, path, _, _ in server.writes()] == ["/projects/2"]
    assert server.projects[1]["visibility"] == "internal"


def test_a_refused_write_is_sent_once_and_stops_the_plan(
    monkeypatch: pytest.MonkeyPatch, world: World, server: Server, ana: TestClient, bea: TestClient
) -> None:
    for gitlab_id in (1, 2):
        world.project(gitlab_id, "public")
        server.project(gitlab_id, "public")
    world.run()
    writes_on(monkeypatch, bea)
    plan = approved(world, ana, bea, 1, 2)
    server.failures[("PUT", "/projects/1")] = 403

    done = apply(world, server, ana, plan["id"])

    assert done["state"] == "failed"
    assert (done["failed_step"], statuses(done)) == (1, ["failed", "not_run"])
    assert (done["progress"][0]["request_status"], done["progress"][0]["verified"]) == (403, False)
    assert "PUT /projects/1 failed: GitLab answered 403" in done["failure"]
    # Never retried, and the plan stopped before project 2.
    assert [(m, p) for m, p, _, _ in server.writes()] == [("PUT", "/projects/1")]
    assert server.projects[2]["visibility"] == "public"
    assert "remediation_plan.failed" in [a for a, _ in audit_actions(world, plan["id"])]
    # A failed plan is not applied again.
    again = ana.post(f"{PLANS}/{plan['id']}/apply")
    assert again.status_code == 409
    assert again.json()["detail"]["reasons"] == ["not_approved"]


@pytest.mark.parametrize("policy", [BASELINE])
def test_a_conflict_is_resolved_by_reading_back(
    monkeypatch: pytest.MonkeyPatch, world: World, server: Server, ana: TestClient, bea: TestClient
) -> None:
    unprotected(world, server)
    server.conflict = True
    writes_on(monkeypatch, bea)
    plan = approved(world, ana, bea, 1)

    done = apply(world, server, ana, plan["id"])

    assert done["state"] == "applied"
    step = done["progress"][0]
    assert (step["status"], step["request_status"], step["verified"]) == ("applied", 409, True)
    assert "but a read shows the intended value" in step["message"]
    assert len(server.writes()) == 1


@pytest.mark.usefixtures("public")
def test_a_write_that_does_not_read_back_fails(
    monkeypatch: pytest.MonkeyPatch, world: World, server: Server, ana: TestClient, bea: TestClient
) -> None:
    writes_on(monkeypatch, bea)
    plan = approved(world, ana, bea, 1)
    server.ignore_writes = True

    done = apply(world, server, ana, plan["id"])

    assert done["state"] == "failed"
    assert done["progress"][0]["verified"] is False
    assert done["failure"] == "The write was accepted but does not read back as intended."


@pytest.mark.usefixtures("public")
def test_an_unreadable_project_fails_without_writing(
    monkeypatch: pytest.MonkeyPatch, world: World, server: Server, ana: TestClient, bea: TestClient
) -> None:
    writes_on(monkeypatch, bea)
    plan = approved(world, ana, bea, 1)
    server.failures[("GET", "/projects/1")] = 403

    done = apply(world, server, ana, plan["id"])

    assert done["state"] == "failed"
    assert "platform/p1 could not be read" in done["failure"]
    assert server.writes() == []


@pytest.mark.usefixtures("public")
def test_turning_writes_off_before_the_job_runs_fails_the_plan(
    monkeypatch: pytest.MonkeyPatch, world: World, server: Server, ana: TestClient, bea: TestClient
) -> None:
    writes_on(monkeypatch, bea)
    plan = approved(world, ana, bea, 1)
    assert ana.post(f"{PLANS}/{plan['id']}/apply").status_code == 202
    monkeypatch.setenv("BALUARTE_REMEDIATION_WRITES", "false")
    server.requests.clear()

    run(world, server)

    done = ana.get(f"{PLANS}/{plan['id']}").json()
    assert done["state"] == "failed" and "BALUARTE_REMEDIATION_WRITES" in done["failure"]
    assert server.requests == []


# --- Refusals ------------------------------------------------------------------------------------


@pytest.mark.usefixtures("public")
def test_applying_needs_the_switch_the_token_and_an_approved_plan(
    monkeypatch: pytest.MonkeyPatch, world: World, server: Server, ana: TestClient, bea: TestClient
) -> None:
    created = ana.post(PLANS, json={"scope": {"finding_ids": finding_ids(world, 1)}}).json()
    assert created["can_apply"]["reasons"] == [
        "remediation_writes_disabled",
        "write_token_missing",
        "not_approved",
    ]
    plan = approved(world, ana, bea, 1)

    refused = ana.post(f"{PLANS}/{plan['id']}/apply")

    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["reasons"] == ["remediation_writes_disabled", "write_token_missing"]
    assert "BALUARTE_REMEDIATION_WRITES" in detail["message"]
    # The label write token and switch do not allow remediation.
    monkeypatch.setenv("BALUARTE_LABEL_WRITES", "true")
    stored = bea.put("/api/v1/settings/label-write-token", json={"token": LABEL_TOKEN})
    assert stored.status_code == 200
    again = ana.post(f"{PLANS}/{plan['id']}/apply").json()["detail"]
    assert again["reasons"] == ["remediation_writes_disabled", "write_token_missing"]
    assert server.writes() == []
    with Session(world.engine) as db:
        assert db.get_one(RemediationPlan, plan["id"]).state == "approved"


@pytest.mark.usefixtures("public")
def test_a_stale_plan_or_stale_collection_is_not_applied(
    monkeypatch: pytest.MonkeyPatch, world: World, server: Server, ana: TestClient, bea: TestClient
) -> None:
    writes_on(monkeypatch, bea)
    plan = approved(world, ana, bea, 1)
    world.collection("failed", timedelta(minutes=1))

    refused = ana.post(f"{PLANS}/{plan['id']}/apply")
    assert refused.json()["detail"]["reasons"] == ["projects_not_current"]

    world.collection("succeeded", timedelta(seconds=1))
    with Session(world.engine) as db:
        project_id = db.scalars(select(GitLabProject.id).where(GitLabProject.gitlab_id == 1)).one()
    world.observe(project_id, "internal")  # newer evidence than the plan was built on

    stale = ana.post(f"{PLANS}/{plan['id']}/apply").json()["detail"]
    assert stale["reasons"] == ["stale"]
    assert stale["stale"] == ["Step 1: newer evidence exists for the project."]
    assert server.writes() == []


@pytest.mark.usefixtures("public")
def test_two_plans_never_apply_the_same_change_at_once(
    monkeypatch: pytest.MonkeyPatch, world: World, server: Server, ana: TestClient, bea: TestClient
) -> None:
    writes_on(monkeypatch, bea)
    first = approved(world, ana, bea, 1)
    second = approved(world, ana, bea, 1)
    assert (
        first["plan"]["steps"][0]["idempotency_key"]
        == second["plan"]["steps"][0]["idempotency_key"]
    )
    assert ana.post(f"{PLANS}/{first['id']}/apply").status_code == 202

    refused = ana.post(f"{PLANS}/{second['id']}/apply")

    assert refused.status_code == 409
    assert refused.json()["detail"]["reasons"] == ["already_applying"]
    run(world, server)
    # Once the first has finished, the same change in the second is already made: no write.
    done = apply(world, server, ana, second["id"])
    assert statuses(done) == ["skipped"]
    assert len(server.writes()) == 1


@pytest.mark.usefixtures("public")
def test_only_remediation_planners_apply(
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    people: dict[str, int],
    server: Server,
    ana: TestClient,
    bea: TestClient,
) -> None:
    writes_on(monkeypatch, bea)
    plan = approved(world, ana, bea, 1)
    auditor = as_user(world, people["cy"], server, Role.AUDITOR)

    assert auditor.post(f"{PLANS}/{plan['id']}/apply").status_code == 403
    assert auditor.get("/api/v1/settings/remediation-write-token").status_code == 403
    assert (
        ana.put("/api/v1/settings/remediation-write-token", json={"token": "x"}).status_code == 403
    )


# --- The write token -----------------------------------------------------------------------------


def test_the_write_token_is_sealed_checked_audited_and_never_shown(
    world: World, server: Server, bea: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    empty = bea.get("/api/v1/settings/remediation-write-token").json()
    assert (empty["set"], empty["writes_enabled"]) == (False, False)

    stored = bea.put("/api/v1/settings/remediation-write-token", json={"token": WRITE_TOKEN})

    assert stored.status_code == 200
    assert stored.json()["set"] and stored.json()["scopes"] == ["api"]
    assert WRITE_TOKEN not in stored.text
    assert WRITE_TOKEN not in bea.get("/api/v1/settings/remediation-write-token").text
    assert WRITE_TOKEN not in caplog.text
    # Checked with the token itself.
    assert server.requests[-1][:3] == ("GET", "/personal_access_tokens/self", WRITE_TOKEN)
    # Separate from the label write token.
    assert bea.get("/api/v1/settings/label-write-token").json()["set"] is False
    with Session(world.engine) as db:
        sealed = db.execute(
            text("SELECT ciphertext FROM app_secret WHERE name = 'remediation_write_token'")
        ).scalar_one()
        assert WRITE_TOKEN.encode() not in bytes(sealed)
    assert bea.delete("/api/v1/settings/remediation-write-token").status_code == 204
    assert bea.delete("/api/v1/settings/remediation-write-token").status_code == 404
    with Session(world.engine) as db:
        actions = list(
            db.scalars(
                select(AuditEvent.action)
                .where(AuditEvent.action.startswith("settings.remediation_write_token"))
                .order_by(AuditEvent.id)
            )
        )
    assert actions == [
        "settings.remediation_write_token_set",
        "settings.remediation_write_token_removed",
    ]


# --- Guarantees ----------------------------------------------------------------------------------


def test_the_writer_can_only_restrict_visibility_and_protect_a_branch() -> None:
    public = {
        name
        for name, _ in inspect.getmembers(RemediationWriter, inspect.isfunction)
        if not name.startswith("_")
    }
    assert public == {"close", "restrict_visibility", "protect_branch", "token_self"}
    assert "DELETE" not in inspect.getsource(remediation_writer)
    with RemediationWriter(GitLabSettings(url=URL, token=TOKEN), WRITE_TOKEN) as writer:
        with pytest.raises(ValueError, match="only ever restricted"):
            writer.restrict_visibility(1, "public")
        with pytest.raises(ValueError, match="without wildcards"):
            writer.protect_branch(1, "release/*")


def test_only_an_exact_maintainer_rule_counts_as_protected_as_intended() -> None:
    maintainers = [{"access_level": 40, "user_id": None}]
    rule = {"name": "main", "push_access_levels": maintainers, "merge_access_levels": maintainers}

    assert protected_as_intended([rule], "main")
    assert protected_as_intended([{**rule, "unprotect_access_levels": maintainers}], "main")
    assert not protected_as_intended([{**rule, "name": "*"}], "main")
    assert not protected_as_intended(
        [{**rule, "push_access_levels": [{"access_level": 30, "user_id": None}]}], "main"
    )
    assert not protected_as_intended(
        [{**rule, "merge_access_levels": [{"access_level": 40, "user_id": 5}]}], "main"
    )
    assert not protected_as_intended([{**rule, "unprotect_access_levels": []}], "main")


@pytest.mark.usefixtures("public")
def test_the_database_keeps_applying_records(
    monkeypatch: pytest.MonkeyPatch, world: World, server: Server, ana: TestClient, bea: TestClient
) -> None:
    writes_on(monkeypatch, bea)
    plan = approved(world, ana, bea, 1)
    with Session(world.engine) as db, pytest.raises(DBAPIError, match="applying_has_start"):
        db.execute(
            update(RemediationPlan).where(RemediationPlan.id == plan["id"]).values(state="applying")
        )
    done = apply(world, server, ana, plan["id"])
    assert done["state"] == "applied"

    for change in (
        {"state": "applying"},
        {"progress": []},
        {"decision_note": "rewritten"},
        {"applied_by_user_id": None},
    ):
        with Session(world.engine) as db, pytest.raises(DBAPIError):
            db.execute(
                update(RemediationPlan).where(RemediationPlan.id == plan["id"]).values(**change)
            )
