# ruff: noqa: F811 (pytest fixtures imported from other test modules)
"""Applying template change sets (M14.4): the switch and the separate write token, stages,
verification, concurrent changes, retries, cancelling, resuming and rolling back."""

import base64
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_evaluation_access import GOOD_SETTINGS, approval_rule
from test_evaluation_project import fresh, instance_id  # noqa: F401
from test_template_change_sets import API, SETTINGS, SITE
from test_template_drift import (  # noqa: F401
    client,
    gitlab_env,
    observe_approvals,
    project_id,
    user,
    world,
)

from baluarte.api.v1.gitlab import gitlab_transport
from baluarte.auth.roles import Role
from baluarte.db.models import AuditEvent, CollectionJob, GitLabProject, TemplateChangeItem
from baluarte.integrations.gitlab.jobs import DISCOVER_PROJECTS
from baluarte.jobs.worker import Worker
from baluarte.templates.execute import (
    APPLY_CHANGE_SET,
    ROLLBACK_CHANGE_SET,
    make_apply_change_set,
    make_rollback_change_set,
)

pytestmark = pytest.mark.db

SETS = "/api/v1/template-change-sets"
WRITE_TOKEN = "fake-template-write-token-value"
RAISE = {"merge_requests": {"min_approvals": 4}}  # 100: 2 -> 4, 101: 3 -> 4; not sensitive


class GitLab:
    """Projects 100 (api) and 101 (site) as the stored evidence describes them; writes change
    the state. ``fail`` answers the next request to (method, path) with a status instead."""

    def __init__(self) -> None:
        self.projects = {
            100: {
                "id": 100,
                "visibility": "private",
                "archived": False,
                "default_branch": "main",
                "topics": ["team:payments"],
            },
            101: {
                "id": 101,
                "visibility": "public",
                "archived": False,
                "default_branch": "main",
                "topics": ["docs", "team:old"],
            },
        }
        self.protections: dict[int, list[dict[str, Any]]] = {
            100: [self._protection("main", 40)],
            101: [],
        }
        self.approvals = {
            100: dict(GOOD_SETTINGS),
            101: {**GOOD_SETTINGS, "merge_requests_author_approval": True},
        }
        self.rules: dict[int, list[dict[str, Any]]] = {
            100: [self._rule(1, 2)],
            101: [self._rule(2, 3)],
        }
        self.next_rule = 10
        self.requests: list[tuple[str, str, str | None, dict[str, str]]] = []
        self.failures: dict[tuple[str, str], int] = {}

    @staticmethod
    def _protection(name: str, level: int) -> dict[str, Any]:
        return {
            "name": name,
            **{
                f"{kind}_access_levels": [
                    {"access_level": level, "user_id": None, "group_id": None}
                ]
                for kind in ("push", "merge", "unprotect")
            },
        }

    @staticmethod
    def _rule(rule_id: int, required: int, name: str | None = None) -> dict[str, Any]:
        return {
            "id": rule_id,
            "name": name or f"regular-{required}",
            "rule_type": "regular",
            "approvals_required": required,
            "applies_to_all_protected_branches": False,
            "protected_branches": [],
        }

    def writes(self) -> list[tuple[str, str, str | None, dict[str, str]]]:
        return [r for r in self.requests if r[0] != "GET"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path.removeprefix("/api/v4"))
        token = request.headers.get("PRIVATE-TOKEN")
        if request.headers.get("content-type", "").startswith("application/json"):
            form = json.loads(request.content)
        else:
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        method = request.method
        self.requests.append((method, path, token, form))
        if (method, path) in self.failures:
            code = self.failures.pop((method, path))
            return httpx.Response(code, json={"message": f"{code} refused"})
        if path == "/personal_access_tokens/self":
            return httpx.Response(200, json={"name": "templates", "scopes": ["api"]})
        parts = path.strip("/").split("/")
        gitlab_id = int(parts[1])
        project = self.projects[gitlab_id]
        rest = parts[2:]
        if not rest and method == "GET":
            return httpx.Response(200, json=project)
        if not rest and method == "PUT":
            project.update(form)
            return httpx.Response(200, json=project)
        if rest == ["protected_branches"] and method == "GET":
            return httpx.Response(200, json=self.protections[gitlab_id])
        if rest == ["protected_branches"] and method == "POST":
            level = int(form["push_access_level"])
            self.protections[gitlab_id].append(self._protection(form["name"], level))
            return httpx.Response(201, json={})
        if rest[:1] == ["protected_branches"] and method == "DELETE":
            name = "/".join(rest[1:])
            self.protections[gitlab_id] = [
                p for p in self.protections[gitlab_id] if p["name"] != name
            ]
            return httpx.Response(204)
        if rest == ["approvals"] and method == "GET":
            return httpx.Response(200, json=self.approvals[gitlab_id])
        if rest == ["approvals"] and method == "POST":
            for key, value in form.items():
                self.approvals[gitlab_id][key] = value == "true"
            return httpx.Response(201, json=self.approvals[gitlab_id])
        if rest == ["approval_rules"] and method == "GET":
            return httpx.Response(200, json=self.rules[gitlab_id])
        if rest == ["approval_rules"] and method == "POST":
            self.next_rule += 1
            rule = self._rule(self.next_rule, int(form["approvals_required"]), form["name"])
            rule["rule_type"] = form["rule_type"]
            self.rules[gitlab_id].append(rule)
            return httpx.Response(201, json=rule)
        if rest[:1] == ["approval_rules"] and method in ("PUT", "DELETE"):
            rule_id = int(rest[1])
            if method == "DELETE":
                self.rules[gitlab_id] = [r for r in self.rules[gitlab_id] if r["id"] != rule_id]
                return httpx.Response(204)
            for rule in self.rules[gitlab_id]:
                if rule["id"] == rule_id:
                    rule["approvals_required"] = int(form["approvals_required"])
                    return httpx.Response(200, json=rule)
        return httpx.Response(405, json={"message": "405 Method Not Allowed"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture(autouse=True)
def secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("BALUARTE_TEMPLATE_WRITES", raising=False)
    monkeypatch.delenv("BALUARTE_SECRET_KEY_PREVIOUS_FILE", raising=False)
    key = tmp_path / "secret-key"
    key.write_text(base64.b64encode(os.urandom(32)).decode() + "\n")
    monkeypatch.setenv("BALUARTE_SECRET_KEY_FILE", str(key))


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every request must go to the fake GitLab."""

    def refuse(self: httpx.BaseTransport, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP request: {request.method} {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)


@pytest.fixture
def gitlab() -> GitLab:
    return GitLab()


def as_admin(engine: Engine, user_id: int, gitlab: GitLab) -> TestClient:
    admin = client(engine, user_id, Role.ADMIN)
    admin.app.dependency_overrides[gitlab_transport] = lambda: gitlab.transport  # type: ignore[attr-defined]
    return admin


def writes_on(monkeypatch: pytest.MonkeyPatch, admin: TestClient) -> None:
    monkeypatch.setenv("BALUARTE_TEMPLATE_WRITES", "true")
    stored = admin.put("/api/v1/settings/template-write-token", json={"token": WRITE_TOKEN})
    assert stored.status_code == 200, stored.text


_people = iter(range(1, 1000))


def approved(engine: Engine, settings: dict[str, Any], projects: list[str]) -> dict[str, Any]:
    n = next(_people)
    ana, bea, cy = (user(engine, f"{name}{n}") for name in ("ana", "bea", "cy"))
    created = client(engine, ana, Role.ANALYST).post(
        SETS,
        json={
            "title": "Raise approvals",
            "rationale": "Align the platform projects with the service template.",
            "selection": {"projects": projects},
            "settings": settings,
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    for approver in (bea, cy)[: body["approvals_needed"]]:
        decided = client(engine, approver, Role.ADMIN).post(
            f"{SETS}/{body['id']}/approve", json={"digest": body["digest"]}
        )
        assert decided.status_code == 200, decided.text
    result: dict[str, Any] = decided.json()
    assert result["state"] == "approved"
    return result


def run(engine: Engine, gitlab: GitLab, kind: str = APPLY_CHANGE_SET) -> None:
    handlers = {
        APPLY_CHANGE_SET: make_apply_change_set(gitlab.transport, sleep=lambda _: None),
        ROLLBACK_CHANGE_SET: make_rollback_change_set(gitlab.transport, sleep=lambda _: None),
    }
    assert Worker(engine, {kind: handlers[kind]}).run_once()


def items(body: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [(i["project_path"], i["setting"], i["status"]) for i in body["items"]]


def actions(engine: Engine, change_set_id: int) -> list[str]:
    with Session(engine) as db:
        return list(
            db.scalars(
                select(AuditEvent.action)
                .where(
                    AuditEvent.resource_type == "template_change_set",
                    AuditEvent.resource_id == str(change_set_id),
                )
                .order_by(AuditEvent.id)
            )
        )


def test_applying_is_refused_without_the_switch_and_the_token(
    migrated_engine: Engine, world: int, gitlab: GitLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = approved(migrated_engine, RAISE, [API, SITE])
    admin = as_admin(migrated_engine, user(migrated_engine, "dee"), gitlab)
    path = f"{SETS}/{body['id']}"

    assert admin.get(path).json()["write_refusals"] == ["writes_disabled", "token_missing"]
    refused = admin.post(f"{path}/apply", json={"stage_targets": 2})
    assert refused.status_code == 409
    assert "BALUARTE_TEMPLATE_WRITES" in refused.json()["detail"]

    monkeypatch.setenv("BALUARTE_TEMPLATE_WRITES", "true")
    refused = admin.post(f"{path}/apply", json={"stage_targets": 2})
    assert refused.status_code == 409
    assert "No template write token" in refused.json()["detail"]
    # An analyst plans but never applies.
    analyst = client(migrated_engine, user(migrated_engine, "eve"), Role.ANALYST)
    assert analyst.post(f"{path}/apply", json={"stage_targets": 2}).status_code == 403
    assert gitlab.requests == []

    # The token routes never return the token.
    writes_on(monkeypatch, admin)
    status = admin.get("/api/v1/settings/template-write-token").json()
    assert (status["set"], status["writes_enabled"]) == (True, True)
    assert WRITE_TOKEN not in str(status)
    assert admin.delete("/api/v1/settings/template-write-token").status_code == 204
    assert admin.delete("/api/v1/settings/template-write-token").status_code == 404


def test_stages_apply_verify_and_finish(
    migrated_engine: Engine, world: int, gitlab: GitLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = approved(migrated_engine, RAISE, [API, SITE])
    admin = as_admin(migrated_engine, user(migrated_engine, "dee"), gitlab)
    writes_on(monkeypatch, admin)
    path = f"{SETS}/{body['id']}"

    started = admin.post(f"{path}/apply", json={"stage_targets": 1})
    assert started.status_code == 200, started.text
    assert (started.json()["state"], started.json()["stage_targets"]) == ("applying", 1)
    assert started.json()["changing_targets"] == 2
    run(migrated_engine, gitlab)

    paused = admin.get(path).json()
    assert paused["state"] == "paused"
    first, second = paused["items"]
    assert (first["status"], first["observed"]) == ("applied", 4)
    assert second["status"] == "pending"
    # Only the first target was written, once, with the template write token.
    assert [(m, p, t) for m, p, t, _ in gitlab.writes()] == [
        ("PUT", f"/projects/{first_id(first)}/approval_rules/{rule_of(first)}", WRITE_TOKEN)
    ]

    continued = admin.post(f"{path}/continue", json={})
    assert continued.status_code == 200, continued.text
    run(migrated_engine, gitlab)
    done = admin.get(path).json()
    assert done["state"] == "finished"
    assert {i["status"] for i in done["items"]} == {"applied"}
    assert all(r["approvals_required"] == 4 for rules in gitlab.rules.values() for r in rules)
    assert all(token == WRITE_TOKEN for _, _, token, _ in gitlab.writes())
    assert {token for m, _, token, _ in gitlab.requests if m == "GET"} != {WRITE_TOKEN}

    assert actions(migrated_engine, body["id"]) == [
        "template_change_set.created",
        "template_change_set.approved",
        "template_change_set.apply_started",
        "template_change_set.write",
        "template_change_set.item",
        "template_change_set.paused",
        "template_change_set.continued",
        "template_change_set.write",
        "template_change_set.item",
        "template_change_set.finished",
    ]
    with Session(migrated_engine) as db:
        kinds = db.scalars(select(CollectionJob.kind)).all()
    assert kinds.count(DISCOVER_PROJECTS) == 2
    assert admin.post(f"{path}/continue", json={}).status_code == 409

    # The evidence check waits for collection after the writes, then follows it.
    assert {v["status"] for v in done["verification"]} == {"awaiting_collection"}
    for gitlab_id, required in ((100, 4), (101, 2)):
        observe_approvals(
            migrated_engine,
            world,
            gitlab_id,
            {
                "branch": "main",
                "branch_protected": True,
                "settings": GOOD_SETTINGS,
                "rules": [approval_rule(required)],
            },
        )
    with Session(migrated_engine) as db:  # discovery saw both projects again
        db.execute(update(GitLabProject).values(last_seen_at=func.now()))
        db.commit()
    checked = {v["project_path"]: v for v in admin.get(path).json()["verification"]}
    assert (checked[API]["status"], checked[API]["observed"]) == ("verified", 4)
    assert (checked[SITE]["status"], checked[SITE]["observed"]) == ("drift", 2)


def first_id(item: dict[str, Any]) -> int:
    return 100 if item["project_path"] == API else 101


def rule_of(item: dict[str, Any]) -> int:
    return 1 if item["project_path"] == API else 2


def test_concurrent_changes_are_flagged_and_lost_writes_retried(
    migrated_engine: Engine, world: int, gitlab: GitLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = approved(migrated_engine, RAISE, [API, SITE])
    admin = as_admin(migrated_engine, user(migrated_engine, "dee"), gitlab)
    writes_on(monkeypatch, admin)
    # Someone set the api project to 5 approvals after the set was built.
    gitlab.rules[100][0]["approvals_required"] = 5
    # The site's write is refused once as unavailable; it is read again and retried.
    gitlab.failures[("PUT", "/projects/101/approval_rules/2")] = 503

    assert admin.post(f"{SETS}/{body['id']}/apply", json={"stage_targets": 2}).status_code == 200
    run(migrated_engine, gitlab)

    done = admin.get(f"{SETS}/{body['id']}").json()
    assert done["state"] == "finished"
    found = {i["project_path"]: i for i in done["items"]}
    assert found[API]["status"] == "out_of_date"
    assert "now shows 5" in found[API]["message"]
    assert gitlab.rules[100][0]["approvals_required"] == 5
    assert (found[SITE]["status"], found[SITE]["attempts"]) == ("applied", 2)
    assert [m for m, p, _, _ in gitlab.writes() if p.startswith("/projects/100")] == []


def test_a_full_set_applies_and_rolls_back_in_reverse(
    migrated_engine: Engine, world: int, gitlab: GitLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = (
        dict(gitlab.projects[101]),
        list(gitlab.protections[101]),
        dict(gitlab.approvals[101]),
        [dict(r) for r in gitlab.rules[101]],
    )
    body = approved(migrated_engine, SETTINGS, [SITE])
    admin = as_admin(migrated_engine, user(migrated_engine, "dee"), gitlab)
    writes_on(monkeypatch, admin)
    path = f"{SETS}/{body['id']}"

    assert admin.post(f"{path}/apply", json={"stage_targets": 1}).status_code == 200
    run(migrated_engine, gitlab)
    done = admin.get(path).json()
    assert done["state"] == "finished"
    assert [i["status"] for i in done["items"]] == ["applied"] * 4
    assert gitlab.projects[101]["visibility"] == "private"
    assert [p["name"] for p in gitlab.protections[101]] == ["main"]
    assert gitlab.approvals[101]["merge_requests_author_approval"] is False
    assert gitlab.rules[101][0]["approvals_required"] == 2

    rolled = admin.post(f"{path}/rollback")
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["state"] == "rolling_back"
    run(migrated_engine, gitlab, ROLLBACK_CHANGE_SET)
    back = admin.get(path).json()
    assert back["state"] == "rolled_back"
    assert {i["rollback_status"] for i in back["items"]} == {"rolled_back"}
    assert (
        gitlab.projects[101],
        gitlab.protections[101],
        gitlab.approvals[101],
        gitlab.rules[101],
    ) == original
    # Rollback ran in reverse: approvals, rule, protection, visibility.
    rollback_writes = gitlab.writes()[-4:]
    assert [
        (m, p.split("/")[3] if p.count("/") > 2 else "project") for m, p, _, _ in rollback_writes
    ] == [
        ("POST", "approvals"),
        ("PUT", "approval_rules"),
        ("DELETE", "protected_branches"),
        ("PUT", "project"),
    ]
    assert actions(migrated_engine, body["id"])[-2:] == [
        "template_change_set.item_rolled_back",
        "template_change_set.rolled_back",
    ]


def test_cancel_stops_and_rollback_leaves_later_changes(
    migrated_engine: Engine, world: int, gitlab: GitLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = approved(migrated_engine, RAISE, [API, SITE])
    admin = as_admin(migrated_engine, user(migrated_engine, "dee"), gitlab)
    writes_on(monkeypatch, admin)
    path = f"{SETS}/{body['id']}"
    assert admin.post(f"{path}/apply", json={"stage_targets": 1}).status_code == 200
    run(migrated_engine, gitlab)

    cancelled = admin.post(f"{path}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"
    assert [i["status"] for i in cancelled.json()["items"]] == ["applied", "cancelled"]
    assert admin.post(f"{path}/continue", json={}).status_code == 409

    # Someone changed the applied setting again; rollback leaves it.
    applied = cancelled.json()["items"][0]
    rule = gitlab.rules[first_id(applied)][0]
    rule["approvals_required"] = 6
    assert admin.post(f"{path}/rollback").status_code == 200
    run(migrated_engine, gitlab, ROLLBACK_CHANGE_SET)
    back = admin.get(path).json()
    assert back["state"] == "rolled_back"
    assert back["items"][0]["rollback_status"] == "not_rolled_back"
    assert rule["approvals_required"] == 6


def test_switching_writes_off_halts_a_run_and_a_stopped_write_resumes(
    migrated_engine: Engine, world: int, gitlab: GitLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = approved(migrated_engine, RAISE, [API, SITE])
    admin = as_admin(migrated_engine, user(migrated_engine, "dee"), gitlab)
    writes_on(monkeypatch, admin)
    path = f"{SETS}/{body['id']}"
    assert admin.post(f"{path}/apply", json={"stage_targets": 2}).status_code == 200

    monkeypatch.setenv("BALUARTE_TEMPLATE_WRITES", "false")
    run(migrated_engine, gitlab)
    halted = admin.get(path).json()
    assert halted["state"] == "paused"
    assert "BALUARTE_TEMPLATE_WRITES" in halted["halt_reason"]
    # Only the token check when it was stored; nothing was read or written for the set.
    assert [p for _, p, _, _ in gitlab.requests] == ["/personal_access_tokens/self"]
    assert admin.post(f"{path}/continue", json={}).status_code == 409

    # A worker stopped after writing the first change but before recording it.
    monkeypatch.setenv("BALUARTE_TEMPLATE_WRITES", "true")
    with Session(migrated_engine) as db:
        first = db.scalars(
            select(TemplateChangeItem)
            .where(TemplateChangeItem.change_set_id == body["id"])
            .order_by(TemplateChangeItem.position)
        ).first()
        assert first is not None
        db.execute(
            update(TemplateChangeItem)
            .where(TemplateChangeItem.id == first.id)
            .values(status="writing", previous={"value": first.before})
        )
        db.commit()
        gitlab.rules[100 if first.project_path == API else 101][0]["approvals_required"] = 4

    assert admin.post(f"{path}/continue", json={}).status_code == 200
    run(migrated_engine, gitlab)
    done = admin.get(path).json()
    assert done["state"] == "finished"
    assert done["halt_reason"] is None
    assert done["items"][0]["message"] == "Found applied when the run resumed."
    assert len(gitlab.writes()) == 1  # only the second change was written


def test_a_stale_or_busy_set_is_refused_and_items_are_immutable(
    migrated_engine: Engine, world: int, gitlab: GitLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    one = approved(migrated_engine, RAISE, [API])
    other = approved(migrated_engine, RAISE, [SITE])
    admin = as_admin(migrated_engine, user(migrated_engine, "dee"), gitlab)
    writes_on(monkeypatch, admin)

    assert admin.post(f"{SETS}/{one['id']}/apply", json={"stage_targets": 1}).status_code == 200
    busy = admin.post(f"{SETS}/{other['id']}/apply", json={"stage_targets": 1})
    assert busy.status_code == 409
    assert "Another change set" in busy.json()["detail"]
    run(migrated_engine, gitlab)

    # The evidence moved: the site now requires 5 approvals.
    observe_approvals(
        migrated_engine,
        world,
        101,
        {
            "branch": "main",
            "branch_protected": False,
            "settings": {**GOOD_SETTINGS, "merge_requests_author_approval": True},
            "rules": [{**GitLab._rule(2, 5), "protected_branches": []}],
        },
    )
    stale = admin.post(f"{SETS}/{other['id']}/apply", json={"stage_targets": 1})
    assert stale.status_code == 409
    assert "no longer gives" in stale.json()["detail"]

    with Session(migrated_engine) as db:
        item_id = db.scalars(
            select(TemplateChangeItem.id).where(TemplateChangeItem.change_set_id == one["id"])
        ).one()
        with pytest.raises(DBAPIError, match="cannot change"):
            db.execute(
                text("UPDATE template_change_item SET after = '9' WHERE id = :id"), {"id": item_id}
            )
        db.rollback()
        with pytest.raises(DBAPIError, match="cannot be deleted"):
            db.execute(text("DELETE FROM template_change_item WHERE id = :id"), {"id": item_id})
        db.rollback()
        with pytest.raises(DBAPIError, match="cannot change from finished to applying"):
            db.execute(
                text("UPDATE template_change_set SET state = 'applying' WHERE id = :id"),
                {"id": one["id"]},
            )
        db.rollback()


def topics_set(engine: Engine, projects: list[str]) -> dict[str, Any]:
    """An approved set synchronizing the team tag to topics; the site carries team=web."""
    n = next(_people)
    ana, bea = (user(engine, f"{name}{n}") for name in ("ana", "bea"))
    with Session(engine) as db:
        for gitlab_id, topics in ((100, ["team:payments"]), (101, ["docs", "team:old"])):
            db.execute(
                update(GitLabProject)
                .where(GitLabProject.gitlab_id == gitlab_id)
                .values(topics=topics)
            )
        db.commit()
    planner = client(engine, ana, Role.ANALYST)
    tag = planner.post("/api/v1/tags", json={"key": "team", "value": "web"}).json()
    assigned = planner.post(
        f"/api/v1/tags/{tag['id']}/assign",
        json={"targets": [{"type": "project", "gitlab_id": 101}]},
    )
    assert assigned.status_code == 200, assigned.text
    created = planner.post(
        SETS,
        json={
            "title": "Team topics",
            "rationale": "Align the platform projects with the service template.",
            "selection": {"projects": projects},
            "topics": {"keys": ["team"]},
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    decided = client(engine, bea, Role.ADMIN).post(
        f"{SETS}/{body['id']}/approve", json={"digest": body["digest"]}
    )
    assert decided.json()["state"] == "approved", decided.text
    result: dict[str, Any] = decided.json()
    return result


def test_tags_are_written_to_topics_and_rolled_back(
    migrated_engine: Engine, world: int, gitlab: GitLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = topics_set(migrated_engine, [API, SITE])
    # The API project has no team tag, so its team topic goes; the site gets team:web.
    changes = {
        t["project"]["path"]: (t["changes"][0]["before"], t["changes"][0]["after"])
        for t in body["document"]["targets"]
    }
    assert changes == {
        API: (["team:payments"], []),
        SITE: (["docs", "team:old"], ["docs", "team:web"]),
    }
    admin = as_admin(migrated_engine, user(migrated_engine, "dee"), gitlab)
    writes_on(monkeypatch, admin)
    path = f"{SETS}/{body['id']}"
    assert admin.post(f"{path}/apply", json={"stage_targets": 2}).status_code == 200
    run(migrated_engine, gitlab)

    done = admin.get(path).json()
    assert done["state"] == "finished"
    assert {i["status"] for i in done["items"]} == {"applied"}
    assert gitlab.projects[100]["topics"] == []
    assert gitlab.projects[101]["topics"] == ["docs", "team:web"]
    assert [(m, p) for m, p, _, _ in gitlab.writes()] == [
        ("PUT", "/projects/100"),
        ("PUT", "/projects/101"),
    ]
    assert all(token == WRITE_TOKEN for _, _, token, _ in gitlab.writes())

    # The evidence check follows the next discovery's topics.
    assert {v["status"] for v in done["verification"]} == {"awaiting_collection"}
    with Session(migrated_engine) as db:
        for gitlab_id, topics in ((100, []), (101, ["docs", "team:old"])):
            db.execute(
                update(GitLabProject)
                .where(GitLabProject.gitlab_id == gitlab_id)
                .values(topics=topics, last_seen_at=func.now())
            )
        db.commit()
    checked = {v["project_path"]: v["status"] for v in admin.get(path).json()["verification"]}
    assert checked == {API: "verified", SITE: "drift"}

    assert admin.post(f"{path}/rollback", json={}).status_code == 200
    run(migrated_engine, gitlab, ROLLBACK_CHANGE_SET)
    assert gitlab.projects[100]["topics"] == ["team:payments"]
    assert gitlab.projects[101]["topics"] == ["docs", "team:old"]
