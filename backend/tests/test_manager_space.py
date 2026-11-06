"""The manager role and scopes, tags, the manager overview and trends."""

import base64
import itertools
import os
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from gitlab_fake import TOKEN, URL
from sqlalchemy import Engine, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_exceptions import client_as, user
from test_policy_schema import BASELINE

from baluarte import tags
from baluarte.auth.roles import ROLE_PERMISSIONS, Permission, Role, roles_from_claim
from baluarte.auth.settings import load_auth_settings
from baluarte.db.models import (
    AuditEvent,
    ChangeRecord,
    Evaluation,
    EvaluationRun,
    Finding,
    GitLabGroup,
    GitLabProject,
    ManagerScope,
    Observation,
    TimeEntry,
)
from baluarte.evidence.observations import payload_digest
from baluarte.integrations.gitlab.instances import instance_for
from baluarte.manager import scopes, trends
from baluarte.policy.changes import Actor
from baluarte.policy.store import save_version
from baluarte.scope import exclusions
from baluarte.scope.exclusions import Actor as ExclusionActor

pytestmark = pytest.mark.db

# acme > acme/team > acme/team/sub, and other.
GROUPS = ((10, "acme", None), (11, "acme/team", 10), (12, "acme/team/sub", 11), (20, "other", None))
PROJECTS = (
    (100, "acme/team/web", 11),
    (101, "acme/api", 10),
    (102, "acme/team/sub/svc", 12),
    (200, "other/site", 20),
)
ADMIN = Actor(None, "user:admin")


@pytest.fixture(autouse=True)
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    configure_gitlab(monkeypatch, tmp_path)


def configure_gitlab(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("BALUARTE_TIME_PER_PERSON", raising=False)
    key = tmp_path / "secret-key"
    key.write_text(base64.b64encode(os.urandom(32)).decode() + "\n")
    monkeypatch.setenv("BALUARTE_SECRET_KEY_FILE", str(key))


def build(engine: Engine) -> Engine:
    with Session(engine) as db:
        instance_id = instance_for(db, URL).id
        for gitlab_id, path, parent in GROUPS:
            db.add(
                GitLabGroup(
                    instance_id=instance_id,
                    gitlab_id=gitlab_id,
                    full_path=path,
                    name=path.rsplit("/", 1)[-1],
                    parent_gitlab_id=parent,
                )
            )
        for gitlab_id, path, namespace in PROJECTS:
            db.add(
                GitLabProject(
                    instance_id=instance_id,
                    gitlab_id=gitlab_id,
                    namespace_gitlab_id=namespace,
                    namespace_kind="group",
                    path_with_namespace=path,
                    name=path.rsplit("/", 1)[-1],
                    visibility="private",
                    state="active",
                    default_branch="main",
                )
            )
        db.commit()
    return engine


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    return build(migrated_engine)


def instance_id(engine: Engine) -> int:
    with Session(engine) as db:
        return instance_for(db, URL).id


def row_ids(engine: Engine) -> dict[int, int]:
    """GitLab project ID to row ID."""
    with Session(engine) as db:
        return dict(db.execute(select(GitLabProject.gitlab_id, GitLabProject.id)).tuples().all())


def audit_actions(engine: Engine, prefix: str) -> list[tuple[str, dict[str, Any] | None]]:
    with Session(engine) as db:
        return [
            (row.action, row.details)
            for row in db.scalars(
                select(AuditEvent)
                .where(AuditEvent.action.startswith(prefix))
                .order_by(AuditEvent.id)
            )
        ]


def as_user(engine: Engine, user_id: int, *roles: Role) -> TestClient:
    return client_as(engine, user_id, *roles)


# --- Roles -------------------------------------------------------------------------------------


def test_the_manager_role_and_its_permissions() -> None:
    manager = ROLE_PERMISSIONS[Role.MANAGER]
    assert {
        Permission.MANAGER_READ,
        Permission.TIME_READ,
        Permission.TIME_READ_PEOPLE,
        Permission.TAGS_WRITE,
        Permission.FINDINGS_READ,
        Permission.INVENTORY_READ,
    } <= manager
    assert Permission.SETTINGS_MANAGE not in manager
    assert Permission.POLICY_WRITE not in manager
    analyst, auditor = ROLE_PERMISSIONS[Role.ANALYST], ROLE_PERMISSIONS[Role.AUDITOR]
    assert {Permission.MANAGER_READ, Permission.TIME_READ, Permission.TAGS_WRITE} <= analyst
    assert Permission.TIME_READ_PEOPLE not in analyst
    assert {Permission.MANAGER_READ, Permission.TIME_READ} <= auditor
    assert not {Permission.TIME_READ_PEOPLE, Permission.TAGS_WRITE} & auditor
    assert set(Permission) == ROLE_PERMISSIONS[Role.ADMIN]


def test_the_manager_role_is_mapped_from_identity_provider_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in {
        "BALUARTE_OIDC_ISSUER": "https://idp.example.test/realms/main",
        "BALUARTE_OIDC_CLIENT_ID": "baluarte",
        "BALUARTE_PUBLIC_URL": "https://baluarte.example.test",
        "BALUARTE_OIDC_ADMIN_GROUPS": "platform-admins",
        "BALUARTE_OIDC_MANAGER_GROUPS": "eng-managers, team-leads",
    }.items():
        monkeypatch.setenv(name, value)
    settings = load_auth_settings()
    assert settings is not None
    assert settings.group_roles[Role.MANAGER] == {"eng-managers", "team-leads"}
    assert roles_from_claim(["team-leads"], settings.group_roles) == {Role.MANAGER}
    assert roles_from_claim(["staff"], settings.group_roles) == frozenset()


def test_the_oidc_settings_api_round_trips_manager_groups(engine: Engine) -> None:
    admin = as_user(engine, user(engine, "admin"), Role.ADMIN)
    found = admin.get("/api/v1/settings/oidc")
    if found.status_code == 404:
        pytest.skip("OIDC settings are configured through the environment here")
    assert "manager_groups" in found.json()


# --- Manager scopes ----------------------------------------------------------------------------


def test_scopes_cover_subgroups_and_their_projects(engine: Engine) -> None:
    ana = user(engine, "ana")
    ids = row_ids(engine)
    with Session(engine) as db:
        scopes.assign(db, instance_id(engine), ana, [11], ADMIN)
        db.commit()
        found = scopes.people_scope(db, instance_id(engine), ana, frozenset({Role.MANAGER}))
        assert found.project_ids == {ids[100], ids[102]}
        assert not found.allows(ids[101]) and not found.allows(ids[200])
        everyone = scopes.people_scope(db, instance_id(engine), ana, frozenset({Role.ADMIN}))
        assert everyone.all and everyone.allows(ids[200])
        nobody = scopes.people_scope(db, instance_id(engine), 999, frozenset({Role.MANAGER}))
        assert not nobody.all and not nobody.project_ids


def test_scope_api_assigns_lists_and_ends(engine: Engine) -> None:
    admin_id, ana = user(engine, "admin"), user(engine, "ana")
    admin = as_user(engine, admin_id, Role.ADMIN)
    created = admin.post(
        "/api/v1/manager/scopes", json={"user_id": ana, "group_gitlab_ids": [11, 20]}
    )
    assert created.status_code == 201, created.text
    items = created.json()["items"]
    assert [i["group"]["path"] for i in items] == ["acme/team", "other"]
    assert items[0]["user"] == {"actor": f"user:{ana}", "user_id": ana, "display_name": "Ana"}
    # Assigning again keeps the open assignment.
    again = admin.post("/api/v1/manager/scopes", json={"user_id": ana, "group_gitlab_ids": [11]})
    assert again.json()["items"][0]["id"] == items[0]["id"]

    unknown = admin.post("/api/v1/manager/scopes", json={"user_id": ana, "group_gitlab_ids": [99]})
    assert unknown.status_code == 409
    assert unknown.json()["detail"]["code"] == "unknown_group"
    assert unknown.json()["detail"]["reasons"] == ["99"]
    nobody = admin.post("/api/v1/manager/scopes", json={"user_id": 9999, "group_gitlab_ids": [11]})
    assert nobody.json()["detail"]["code"] == "unknown_user"

    mine = as_user(engine, ana, Role.MANAGER).get("/api/v1/manager/scopes/me").json()
    assert mine["all"] is False and len(mine["items"]) == 2
    assert as_user(engine, admin_id, Role.ADMIN).get("/api/v1/manager/scopes/me").json()["all"]

    ended = admin.post(f"/api/v1/manager/scopes/{items[1]['id']}/end")
    assert ended.status_code == 200 and ended.json()["ended_at"] is not None
    twice = admin.post(f"/api/v1/manager/scopes/{items[1]['id']}/end")
    assert twice.status_code == 409 and twice.json()["detail"]["code"] == "already_ended"
    assert admin.post("/api/v1/manager/scopes/99999/end").status_code == 404
    active = admin.get("/api/v1/manager/scopes").json()["items"]
    assert [i["group"]["path"] for i in active] == ["acme/team"]
    assert len(admin.get("/api/v1/manager/scopes?state=all").json()["items"]) == 2

    assert [a for a, _ in audit_actions(engine, "manager.")] == [
        "manager.scope_assigned",
        "manager.scope_ended",
    ]
    users = admin.get("/api/v1/manager/users").json()["items"]
    assert {u["display_name"] for u in users} >= {"Admin", "Ana"}


@pytest.mark.parametrize("role", [Role.MANAGER, Role.ANALYST, Role.AUDITOR])
def test_only_administrators_change_scopes(engine: Engine, role: Role) -> None:
    ana = user(engine, "ana")
    client = as_user(engine, ana, role)
    assert client.get("/api/v1/manager/scopes").status_code == 200
    refused = client.post("/api/v1/manager/scopes", json={"user_id": ana, "group_gitlab_ids": [11]})
    assert refused.status_code == 403
    assert client.get("/api/v1/manager/users").status_code == 403


def test_the_database_keeps_scope_history(engine: Engine) -> None:
    ana = user(engine, "ana")
    with Session(engine) as db:
        row = scopes.assign(db, instance_id(engine), ana, [11], ADMIN)[0]
        db.commit()
        scope_id = row.id
    with Session(engine) as db, pytest.raises(DBAPIError):
        db.execute(update(ManagerScope).where(ManagerScope.id == scope_id).values(group_path="x"))
        db.flush()
    with Session(engine) as db, pytest.raises(DBAPIError):
        db.execute(text("DELETE FROM manager_scope WHERE id = :id"), {"id": scope_id})
        db.flush()
    with Session(engine) as db:
        scopes.end(db, scope_id, ADMIN)
        db.commit()
    with Session(engine) as db, pytest.raises(DBAPIError):
        db.execute(
            update(ManagerScope).where(ManagerScope.id == scope_id).values(ended_at=func.now())
        )
        db.flush()


# --- Tags --------------------------------------------------------------------------------------


def make_tag(engine: Engine, key: str, value: str) -> int:
    with Session(engine) as db:
        row = tags.create(db, instance_id(engine), key, value, None, None, ADMIN)
        db.commit()
        return row.id


def assign(engine: Engine, tag_id: int, *targets: tuple[str, int]) -> None:
    with Session(engine) as db:
        tags.assign(db, tag_id, [tags.Target(t, g) for t, g in targets], ADMIN)  # type: ignore[arg-type]
        db.commit()


def test_tag_names_and_filters() -> None:
    assert tags.valid_name("team") and tags.valid_name("cost-center.eu_1")
    assert not tags.valid_name("Team") and not tags.valid_name("-x") and not tags.valid_name("")
    assert not tags.valid_name("a" * 64)
    assert tags.parse_filter("team:payments") == ("team", "payments")
    assert tags.parse_filter("team") is None and tags.parse_filter("team:Pay") is None


def test_a_group_tag_reaches_subgroups_and_projects_and_the_nearest_wins(engine: Engine) -> None:
    ids = row_ids(engine)
    core, payments, edge = (make_tag(engine, "team", v) for v in ("core", "payments", "edge"))
    tier = make_tag(engine, "tier", "1")
    assign(engine, core, ("group", 10))
    assign(engine, payments, ("group", 11))
    assign(engine, edge, ("project", 102))
    assign(engine, tier, ("group", 10))
    with Session(engine) as db:
        index = tags.index(db, instance_id(engine))
    by_key = {
        gitlab_id: {a.key: (a.value, a.via, a.from_path) for a in index.of_project(ids[gitlab_id])}
        for gitlab_id in (100, 101, 102, 200)
    }
    assert by_key[101]["team"] == ("core", "group", "acme")
    # The nearer group wins over the top-level one.
    assert by_key[100]["team"] == ("payments", "group", "acme/team")
    # A direct assignment wins over any group.
    assert by_key[102]["team"] == ("edge", "project", "acme/team/sub/svc")
    assert by_key[102]["tier"] == ("1", "group", "acme")
    assert by_key[200] == {}
    assert index.projects_with([("team", "payments")]) == {ids[100]}
    assert index.projects_with([("team", "core"), ("tier", "1")]) == {ids[101]}
    assert index.groups_with([("team", "payments")]) == {11, 12}


def test_tags_api_creates_assigns_and_refuses_conflicts(engine: Engine) -> None:
    ana = as_user(engine, user(engine, "ana"), Role.ANALYST)
    invalid = ana.post("/api/v1/tags", json={"key": "Team", "value": "x"})
    assert invalid.status_code == 422 and invalid.json()["detail"]["code"] == "invalid_name"
    created = ana.post(
        "/api/v1/tags",
        json={"key": "team", "value": "payments", "description": "Payments", "color": "#1f75cb"},
    )
    assert created.status_code == 201, created.text
    payments = created.json()
    assert payments["color"] == "#1F75CB" and payments["groups"] == 0
    duplicate = ana.post("/api/v1/tags", json={"key": "team", "value": "payments"})
    assert duplicate.status_code == 409 and duplicate.json()["detail"]["code"] == "tag_exists"
    core = ana.post("/api/v1/tags", json={"key": "team", "value": "core"}).json()

    url = f"/api/v1/tags/{payments['id']}"
    assigned = ana.post(
        url + "/assign",
        json={
            "targets": [{"type": "group", "gitlab_id": 11}, {"type": "project", "gitlab_id": 101}]
        },
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["groups"] == 1 and assigned.json()["projects"] == 1
    assert [a["target_path"] for a in assigned.json()["assignments"]] == ["acme/api", "acme/team"]
    # Idempotent.
    again = ana.post(url + "/assign", json={"targets": [{"type": "group", "gitlab_id": 11}]})
    assert len(again.json()["assignments"]) == 2
    conflict = ana.post(
        f"/api/v1/tags/{core['id']}/assign",
        json={"targets": [{"type": "group", "gitlab_id": 11}]},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] | {"message": ""} == {
        "code": "key_conflict",
        "message": "",
        "reasons": ["acme/team"],
    }
    unknown = ana.post(url + "/assign", json={"targets": [{"type": "project", "gitlab_id": 9}]})
    assert unknown.json()["detail"]["code"] == "unknown_target"

    patched = ana.patch(url, json={"description": None})
    assert patched.json()["description"] is None and patched.json()["color"] == "#1F75CB"
    removed = ana.post(url + "/unassign", json={"targets": [{"type": "project", "gitlab_id": 101}]})
    assert [a["target_path"] for a in removed.json()["assignments"]] == ["acme/team"]
    listed = ana.get("/api/v1/tags").json()["items"]
    assert [(t["key"], t["value"]) for t in listed] == [("team", "core"), ("team", "payments")]

    assert [a for a, _ in audit_actions(engine, "tags.")] == [
        "tags.created",
        "tags.created",
        "tags.assigned",
        "tags.updated",
        "tags.unassigned",
    ]
    auditor = as_user(engine, user(engine, "aud"), Role.AUDITOR)
    assert auditor.get(url).status_code == 200
    assert auditor.post("/api/v1/tags", json={"key": "a", "value": "b"}).status_code == 403


def test_inventory_shows_and_filters_by_tags(engine: Engine) -> None:
    payments, tier = make_tag(engine, "team", "payments"), make_tag(engine, "tier", "1")
    assign(engine, payments, ("group", 11))
    assign(engine, tier, ("project", 100), ("project", 101))
    client = as_user(engine, user(engine, "ana"), Role.MANAGER)
    projects = client.get("/api/v1/gitlab/projects").json()["items"]
    web = next(p for p in projects if p["gitlab_id"] == 100)
    assert web["tags"] == [
        {"key": "team", "value": "payments", "via": "group", "from_path": "acme/team"},
        {"key": "tier", "value": "1", "via": "project", "from_path": "acme/team/web"},
    ]
    filtered = client.get("/api/v1/gitlab/projects?tag=team:payments&tag=tier:1").json()
    assert [p["gitlab_id"] for p in filtered["items"]] == [100]
    groups = client.get("/api/v1/gitlab/groups?tag=team:payments").json()["items"]
    assert sorted(g["gitlab_id"] for g in groups) == [11, 12]
    detail = client.get("/api/v1/gitlab/groups/12").json()
    assert detail["group"]["tags"][0]["from_path"] == "acme/team"
    bad = client.get("/api/v1/gitlab/projects?tag=team")
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid_tag_filter"


def test_findings_filter_by_tag(engine: Engine) -> None:
    world = Results(engine)
    run = world.run(datetime.now(UTC) - timedelta(hours=1))
    world.result(run, 100, "fail", "high", finding=True)
    world.result(run, 101, "fail", "low", finding=True)
    assign(engine, make_tag(engine, "team", "payments"), ("group", 11))
    client = as_user(engine, user(engine, "ana"), Role.ANALYST)
    found = client.get("/api/v1/findings?tag=team:payments").json()["items"]
    assert [f["project"]["gitlab_id"] for f in found] == [100]
    assert len(client.get("/api/v1/findings").json()["items"]) == 2


# --- Overview and trends -----------------------------------------------------------------------


class Results:
    """Evaluation runs, results, findings and changes inserted directly."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.instance_id = instance_id(engine)
        self.ids = row_ids(engine)
        self.iids = itertools.count(1)
        with Session(engine) as db:
            policy = save_version(db, BASELINE).policy
            self.policy_id, self.policy_name = policy.id, policy.name
            observation = Observation(
                instance_id=self.instance_id,
                project_id=self.ids[100],
                source="test",
                collected_at=func.now(),
                collection_status="ok",
                payload={},
                payload_digest=payload_digest({}),
            )
            db.add(observation)
            db.commit()
            self.observation_id = observation.id

    def run(self, finished_at: datetime) -> int:
        with Session(self.engine) as db:
            run = EvaluationRun(
                instance_id=self.instance_id,
                status="succeeded",
                started_at=finished_at - timedelta(minutes=1),
                finished_at=finished_at,
            )
            db.add(run)
            db.commit()
            return run.id

    def values(
        self, run: int, project: int, outcome: str, severity: str, rule_type: str
    ) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "project_id": self.ids[project] if project in self.ids else project,
            "evaluation_run_id": run,
            "observation_id": self.observation_id,
            "policy_id": self.policy_id,
            "policy_name": self.policy_name,
            "rule_id": f"{rule_type}-{severity}",
            "rule_type": rule_type,
            "severity": severity,
            "outcome": outcome,
            "reason": "test",
            "expected": {},
            "observed": {} if outcome in ("pass", "fail") else None,
            "unknown_reasons": [] if outcome != "unknown" else ["no_data"],
            "source": "test",
            "capability_status": "supported",
            "collection_kind": "projects",
            "collection_status": "ok",
            "ancestry": "complete",
        }

    def result(
        self,
        run: int,
        project: int,
        outcome: str,
        severity: str,
        rule_type: str = "project_visibility",
        finding: bool = False,
    ) -> None:
        with Session(self.engine) as db:
            evaluation_id = db.execute(
                insert(Evaluation)
                .values(**self.values(run, project, outcome, severity, rule_type))
                .returning(Evaluation.id)
            ).scalar_one()
            if finding:
                db.add(
                    Finding(
                        project_id=self.ids[project],
                        policy_id=self.policy_id,
                        policy_name=self.policy_name,
                        rule_id=f"{rule_type}-{severity}",
                        latest_evaluation_id=evaluation_id,
                        latest_outcome=outcome,
                        severity=severity,
                    )
                )
            db.commit()

    def change(self, project: int, kind: str, review: str | None, changed_at: datetime) -> None:
        with Session(self.engine) as db:
            db.add(
                ChangeRecord(
                    instance_id=self.instance_id,
                    project_id=self.ids[project],
                    kind=kind,
                    iid=next(self.iids) if kind == "merge_request" else None,
                    sha=os.urandom(20).hex(),
                    changed_at=changed_at,
                    observation_id=self.observation_id,
                    outcome=review or "unknown",
                    review_outcome=review,
                )
            )
            db.commit()

    def time(self, project: int, seconds: int, spent_at: datetime, username: str = "dev1") -> None:
        with Session(self.engine) as db:
            db.add(
                TimeEntry(
                    instance_id=self.instance_id,
                    project_id=self.ids[project],
                    timelog_id=f"gid://gitlab/Timelog/{os.urandom(6).hex()}",
                    username=username,
                    spent_at=spent_at,
                    seconds=seconds,
                    labels=[],
                    observation_id=self.observation_id,
                )
            )
            db.commit()


def test_overview_rows_per_child_group(engine: Engine) -> None:
    world = Results(engine)
    now = datetime.now(UTC)
    run = world.run(now - timedelta(hours=1))
    world.result(run, 100, "fail", "high", finding=True)
    world.result(run, 102, "fail", "critical", "branch_protection", finding=True)
    world.result(run, 101, "pass", "high")
    world.result(run, 200, "fail", "low", finding=True)
    world.change(100, "merge_request", "pass", now - timedelta(days=3))
    world.change(100, "direct_push", "fail", now - timedelta(days=40))
    world.change(101, "merge_request", None, now - timedelta(days=5))
    world.time(100, 3600, now - timedelta(days=2))
    world.time(101, 1800, now - timedelta(days=45))
    with Session(engine) as db:
        exclusions.propose(
            db,
            world.instance_id,
            [exclusions.Target("project", 101)],
            False,
            "Archived soon, not in scope",
            ExclusionActor(user(engine, "admin"), "user:admin"),
        )
        db.commit()

    client = as_user(engine, user(engine, "mia"), Role.MANAGER)
    top = client.get("/api/v1/manager/overview").json()
    assert [r["key"]["path"] for r in top["rows"]] == ["acme", "other"]
    acme = top["rows"][0]
    assert acme["projects"] == {"monitored": 2, "excluded": 1}
    assert acme["findings_open"] == {"critical": 1, "high": 1, "medium": 0, "low": 0, "total": 2}
    assert set(acme["controls_not_met"]) == {"soc2", "iso27001", "nist_800_53_moderate"}
    assert acme["changes"]["days_30"] == {
        "total": 1,
        "reviewed": 1,
        "direct_pushes": 0,
        "unknown": 0,
        "rate": 1.0,
    }
    assert acme["changes"]["days_90"]["total"] == 2 and acme["changes"]["days_90"]["rate"] == 0.5
    assert acme["time_30d"]["spent_seconds"] == 3600
    assert acme["label_drift"] is None
    assert top["current_only"] == ["time", "tags", "groups"]
    assert top["evaluation_run"]["id"] == run

    acme_rows = client.get("/api/v1/manager/overview?group=acme").json()
    assert [(r["key"]["path"], r["key"].get("direct_only", False)) for r in acme_rows["rows"]] == [
        ("acme/team", False),
        ("acme", True),
    ]
    assert acme_rows["parent"] == {"type": "group", "gitlab_id": 10, "path": "acme"}
    unknown = client.get("/api/v1/manager/overview?group=nope")
    assert unknown.status_code == 404


def test_overview_by_tag_value_and_as_of(engine: Engine) -> None:
    world = Results(engine)
    now = datetime.now(UTC)
    early = world.run(now - timedelta(days=10))
    world.result(early, 100, "fail", "medium")
    world.result(early, 200, "fail", "medium")
    late = world.run(now - timedelta(hours=1))
    world.result(late, 100, "pass", "medium")
    assign(engine, make_tag(engine, "team", "payments"), ("group", 11))
    assign(engine, make_tag(engine, "team", "web"), ("group", 20))
    client = as_user(engine, user(engine, "aud"), Role.AUDITOR)
    found = client.get("/api/v1/manager/overview?by_tag=team").json()
    keyed = {r["key"]["value"]: r for r in found["rows"]}
    assert set(keyed) == {"payments", "web", None}
    assert keyed["payments"]["projects"]["monitored"] == 2
    as_of = (now - timedelta(days=5)).isoformat()
    then = client.get("/api/v1/manager/overview", params={"by_tag": "team", "as_of": as_of}).json()
    assert then["evaluation_run"]["id"] == early
    by_value = {r["key"]["value"]: r for r in then["rows"]}
    assert by_value["payments"]["findings_open"]["medium"] == 1
    assert by_value["web"]["findings_open"]["medium"] == 1
    filtered = client.get("/api/v1/manager/overview?tag=team:payments").json()
    assert [r["key"]["path"] for r in filtered["rows"]] == ["acme", "other"]
    assert filtered["rows"][1]["projects"]["monitored"] == 0


def test_trend_buckets() -> None:
    weeks = trends.buckets("week", date(2026, 1, 7), date(2026, 1, 20), date(2026, 6, 1))
    assert weeks == [
        (date(2026, 1, 5), date(2026, 1, 12)),
        (date(2026, 1, 12), date(2026, 1, 19)),
        (date(2026, 1, 19), date(2026, 1, 26)),
    ]
    months = trends.buckets("month", None, None, date(2026, 3, 15))
    assert len(months) == 12 and months[0][0] == date(2025, 4, 1)
    assert months[-1] == (date(2026, 3, 1), date(2026, 4, 1))
    with pytest.raises(trends.TrendsRefused):
        trends.buckets("week", date(2023, 1, 1), date(2026, 1, 1), date(2026, 1, 1))
    with pytest.raises(trends.TrendsRefused):
        trends.buckets("month", date(2026, 2, 1), date(2026, 1, 1), date(2026, 1, 1))


def test_trends_points_follow_the_run_in_force(engine: Engine) -> None:
    world = Results(engine)
    today = datetime.now(UTC).date()
    monday = today - timedelta(days=today.weekday())
    at = datetime(monday.year, monday.month, monday.day, tzinfo=UTC)
    first = world.run(at - timedelta(days=12))
    world.result(first, 100, "fail", "high")
    world.result(first, 101, "fail", "low")
    second = world.run(at - timedelta(days=5))
    world.result(second, 100, "pass", "high")
    world.result(second, 101, "fail", "low")
    world.change(100, "merge_request", "pass", at - timedelta(days=6))
    world.change(100, "direct_push", "fail", at - timedelta(days=6))
    world.time(100, 600, at - timedelta(days=4))
    client = as_user(engine, user(engine, "mia"), Role.MANAGER)
    found = client.get(
        "/api/v1/manager/trends",
        params={"from": (at - timedelta(days=21)).date().isoformat(), "to": today.isoformat()},
    ).json()
    points = found["points"]
    assert [p["evaluation_run_id"] for p in points] == [None, first, second, second]
    assert points[0]["findings_open"] is None
    assert points[1]["findings_open"]["total"] == 2 and points[1]["rule_pass_rate"] == 0.0
    assert points[2]["findings_open"]["high"] == 0 and points[2]["rule_pass_rate"] == 0.5
    assert points[2]["change_review_rate"] == 0.5 and points[1]["change_review_rate"] is None
    assert points[2]["time_spent_seconds"] == 600 and points[3]["time_spent_seconds"] == 0
    team = client.get("/api/v1/manager/trends?group=acme/team").json()
    assert team["projects"] == 2
    tagged = client.get("/api/v1/manager/trends?bucket=month&tag=team:none").json()
    assert tagged["projects"] == 0 and len(tagged["points"]) == 12
    too_many = client.get("/api/v1/manager/trends?bucket=week&from=2020-01-01")
    assert too_many.status_code == 422 and too_many.json()["detail"]["code"] == "too_many_buckets"


def test_overview_and_trends_need_manager_read(engine: Engine) -> None:
    client = as_user(engine, user(engine, "nobody"))
    assert client.get("/api/v1/manager/overview").status_code == 403
    assert client.get("/api/v1/manager/trends").status_code == 403


def test_trends_stay_fast_on_two_years_of_weekly_runs(engine: Engine) -> None:
    """A moderately large synthetic dataset: 2,000 projects, 104 weekly runs with 5 results per
    project (about a million results), 50,000 changes and 100,000 time entries."""
    world = Results(engine)
    projects, weeks, rules = 2000, 104, 5
    with Session(engine) as db:
        db.execute(
            text(
                """
                INSERT INTO gitlab_project (instance_id, gitlab_id, namespace_gitlab_id,
                    namespace_kind, path_with_namespace, name, visibility, state)
                SELECT :instance, 10000 + n, 11, 'group', 'acme/team/p' || n, 'p' || n,
                    'private', 'active'
                FROM generate_series(1, :projects) AS n
                """
            ),
            {"instance": world.instance_id, "projects": projects},
        )
        db.execute(
            text(
                """
                INSERT INTO evaluation_run (instance_id, status, started_at, finished_at)
                SELECT :instance, 'succeeded', now() - (w || ' weeks')::interval,
                    now() - (w || ' weeks')::interval + interval '1 minute'
                FROM generate_series(1, :weeks) AS w
                """
            ),
            {"instance": world.instance_id, "weeks": weeks},
        )
        db.execute(
            text(
                """
                INSERT INTO evaluation (instance_id, project_id, evaluation_run_id, observation_id,
                    policy_id, policy_name, rule_id, rule_type, severity, outcome, reason,
                    expected, observed, source, capability_status, collection_kind,
                    collection_status, ancestry)
                SELECT :instance, p.id, r.id, :observation, :policy, :policy_name,
                    'rule-' || k, 'project_visibility',
                    (ARRAY['low','medium','high','critical'])[1 + (p.id + k) % 4],
                    CASE WHEN (p.id + r.id + k) % 5 = 0 THEN 'fail' ELSE 'pass' END,
                    'synthetic', '{}'::jsonb, '{}'::jsonb, 'test', 'supported', 'projects',
                    'ok', 'complete'
                FROM gitlab_project p
                CROSS JOIN evaluation_run r
                CROSS JOIN generate_series(1, :rules) AS k
                WHERE p.instance_id = :instance AND p.gitlab_id > 10000
                """
            ),
            {
                "instance": world.instance_id,
                "observation": world.observation_id,
                "policy": world.policy_id,
                "policy_name": world.policy_name,
                "rules": rules,
            },
        )
        db.execute(
            text(
                """
                INSERT INTO change_record (instance_id, project_id, kind, sha, changed_at,
                    observation_id, outcome, review_outcome)
                SELECT :instance, p.id, 'direct_push', md5(n::text || p.id::text),
                    now() - (n || ' days')::interval, :observation, 'fail',
                    CASE WHEN n % 3 = 0 THEN 'fail' ELSE 'pass' END
                FROM gitlab_project p CROSS JOIN generate_series(1, 25) AS n
                WHERE p.instance_id = :instance AND p.gitlab_id > 10000
                """
            ),
            {"instance": world.instance_id, "observation": world.observation_id},
        )
        db.execute(
            text(
                """
                INSERT INTO time_entry (instance_id, project_id, timelog_id, username, spent_at,
                    seconds, labels, observation_id)
                SELECT :instance, p.id, 'gid://gitlab/Timelog/' || p.id || '-' || n, 'dev1',
                    now() - (n * 7 || ' days')::interval, 1800, '{}', :observation
                FROM gitlab_project p CROSS JOIN generate_series(1, 50) AS n
                WHERE p.instance_id = :instance AND p.gitlab_id > 10000
                """
            ),
            {"instance": world.instance_id, "observation": world.observation_id},
        )
        db.commit()
        db.execute(text("ANALYZE"))
    client = as_user(engine, user(engine, "mia"), Role.MANAGER)
    start = (datetime.now(UTC).date() - timedelta(weeks=weeks)).isoformat()
    began = time.perf_counter()
    found = client.get("/api/v1/manager/trends", params={"from": start, "bucket": "week"})
    elapsed = time.perf_counter() - began
    assert found.status_code == 200, found.text
    points = found.json()["points"]
    assert len(points) == weeks + 1
    assert sum(1 for p in points if p["findings_open"]) >= weeks - 1
    assert elapsed < 5, f"trends took {elapsed:.1f}s"
