from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from gitlab_fake import TOKEN, URL
from oidc_fake import make_settings
from sqlalchemy import Engine, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_policy_schema import BASELINE

from baluarte.auth.dependencies import (
    Principal,
    auth_settings,
    current_principal,
    database_engine,
)
from baluarte.auth.roles import Role
from baluarte.db.models import AuditEvent, GitLabGroup, GitLabProject, Policy, PolicyAssignment
from baluarte.integrations.gitlab.instances import instance_for
from baluarte.main import create_app
from baluarte.policy.loader import digest
from baluarte.policy.store import save_version

pytestmark = pytest.mark.db

DEFAULT = "default-gitlab-baseline"
CHANGED = BASELINE.replace("allowed: [private, internal]", "allowed: [private]")
NARROWED = BASELINE.replace('groups: ["*"]', 'groups: ["platform/backend"]')
STRICT = BASELINE.replace(f"name: {DEFAULT}", "name: strict")
BACKEND_ONLY = NARROWED.replace(f"name: {DEFAULT}", "name: backend-only")

# (gitlab_id, full_path, parent, state)
GROUPS = [
    (10, "platform", None, "active"),
    (11, "platform/backend", 10, "active"),
    (12, "platform/backend/api", 11, "active"),
    (20, "orphans/sub", 99, "active"),
    (30, "gone", None, "missing"),
    (31, "gone/child", 30, "active"),
    (32, "hidden", None, "inaccessible"),
    (40, "loop-a", 41, "active"),
    (41, "loop-b", 40, "active"),
]
# (gitlab_id, path_with_namespace, namespace, state)
PROJECTS = [
    (100, "platform/backend/api/svc", 12, "active"),
    (101, "platform/web", 10, "active"),
    (102, "alice/dotfiles", 500, "active"),
    (103, "orphans/sub/tool", 20, "active"),
    (104, "gone/child/app", 31, "active"),
    (105, "platform/old", 10, "missing"),
]


@pytest.fixture
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)


@pytest.fixture
def instance_id(migrated_engine: Engine, gitlab_env: None) -> int:
    """Seeds the inventory and the policy versions used below."""
    with Session(migrated_engine) as db:
        instance = instance_for(db, URL)
        for gitlab_id, path, parent, state in GROUPS:
            db.execute(
                insert(GitLabGroup).values(
                    instance_id=instance.id,
                    gitlab_id=gitlab_id,
                    parent_gitlab_id=parent,
                    full_path=path,
                    name=path.rsplit("/", 1)[-1],
                    state=state,
                )
            )
        for gitlab_id, path, namespace, state in PROJECTS:
            db.execute(
                insert(GitLabProject).values(
                    instance_id=instance.id,
                    gitlab_id=gitlab_id,
                    namespace_gitlab_id=namespace,
                    path_with_namespace=path,
                    name=path.rsplit("/", 1)[-1],
                    state=state,
                )
            )
        for source in (BASELINE, CHANGED, NARROWED, STRICT, BACKEND_ONLY):
            save_version(db, source)
        db.commit()
        return instance.id


def client_as(engine: Engine, *roles: Role) -> TestClient:
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
    return TestClient(app)


def assign(
    client: TestClient, name: str, version: int, target_type: str, target_id: int | None = None
) -> Any:
    body: dict[str, Any] = {"policy_name": name, "version": version, "target_type": target_type}
    if target_id is not None:
        body["target_id"] = target_id
    return client.post("/api/v1/policy-assignments", json=body)


def audit_rows(engine: Engine) -> list[AuditEvent]:
    with Session(engine) as db:
        return list(db.scalars(select(AuditEvent).order_by(AuditEvent.id)))


def assignment_rows(engine: Engine) -> list[PolicyAssignment]:
    with Session(engine) as db:
        return list(db.scalars(select(PolicyAssignment).order_by(PolicyAssignment.id)))


def summary(resolution: dict[str, Any], key: str = "considered") -> list[tuple[Any, ...]]:
    return [
        (
            entry["name"],
            entry["version"],
            entry["assigned_to"]["target_type"],
            entry["assigned_to"]["target_id"],
            entry["outcome"],
        )
        for entry in resolution[key]
    ]


def test_assign_pins_a_version_and_audits(migrated_engine: Engine, instance_id: int) -> None:
    response = assign(client_as(migrated_engine, Role.ANALYST), DEFAULT, 1, "group", 11)

    assert response.status_code == 201
    body = response.json()
    assert {key: body[key] for key in ("policy_name", "version", "target_type", "target_id")} == {
        "policy_name": DEFAULT,
        "version": 1,
        "target_type": "group",
        "target_id": 11,
    }
    assert body["yaml_digest"] == digest(BASELINE)
    assert (body["target_path"], body["target_state"]) == ("platform/backend", "active")
    [event] = audit_rows(migrated_engine)
    assert event.action == "policy.assigned"
    assert event.resource_type == "policy_assignment"
    assert event.resource_id == str(body["id"])
    assert event.details["policy_name"] == DEFAULT
    assert event.details["version"] == 1
    assert event.details["target_path"] == "platform/backend"
    assert "replaced_version" not in event.details


def test_assigning_the_same_version_again_changes_nothing(
    migrated_engine: Engine, instance_id: int
) -> None:
    client = client_as(migrated_engine, Role.ADMIN)
    first = assign(client, DEFAULT, 1, "project", 100)

    again = assign(client, DEFAULT, 1, "project", 100)

    assert again.status_code == 200
    assert again.json() == first.json()
    assert len(assignment_rows(migrated_engine)) == 1
    assert len(audit_rows(migrated_engine)) == 1


def test_another_version_replaces_the_pinned_one(migrated_engine: Engine, instance_id: int) -> None:
    client = client_as(migrated_engine, Role.ADMIN)
    first = assign(client, DEFAULT, 1, "instance").json()

    response = assign(client, DEFAULT, 2, "instance")

    assert response.status_code == 201
    assert response.json()["version"] == 2
    assert response.json()["target_id"] == instance_id
    assert response.json()["target_path"] == URL
    [row] = assignment_rows(migrated_engine)
    assert row.id == response.json()["id"]
    replaced = audit_rows(migrated_engine)[-1]
    assert replaced.details["replaced_assignment_id"] == first["id"]
    assert replaced.details["replaced_version"] == 1


def test_different_policies_share_a_target(migrated_engine: Engine, instance_id: int) -> None:
    client = client_as(migrated_engine, Role.ADMIN)

    assert assign(client, DEFAULT, 1, "group", 10).status_code == 201
    assert assign(client, "strict", 1, "group", 10).status_code == 201

    assert len(assignment_rows(migrated_engine)) == 2


def test_auditors_cannot_assign_or_unassign(migrated_engine: Engine, instance_id: int) -> None:
    created = assign(client_as(migrated_engine, Role.ADMIN), DEFAULT, 1, "group", 10).json()
    auditor = client_as(migrated_engine, Role.AUDITOR)

    assert assign(auditor, DEFAULT, 1, "group", 11).status_code == 403
    assert auditor.delete(f"/api/v1/policy-assignments/{created['id']}").status_code == 403
    assert len(assignment_rows(migrated_engine)) == 1


@pytest.mark.parametrize(
    ("name", "version", "target_type", "target_id", "status", "detail"),
    [
        (DEFAULT, 9, "group", 10, 404, "No such policy version"),
        ("absent", 1, "group", 10, 404, "No such policy version"),
        (DEFAULT, 1, "group", 999, 404, "No such group in the inventory"),
        (DEFAULT, 1, "project", 10, 404, "No such project in the inventory"),
        (DEFAULT, 1, "group", 30, 409, "The group is missing"),
        (DEFAULT, 1, "group", 32, 409, "The group is inaccessible"),
        (DEFAULT, 1, "project", 105, 409, "The project is missing"),
    ],
)
def test_assignment_needs_a_version_and_a_visible_target(
    migrated_engine: Engine,
    instance_id: int,
    name: str,
    version: int,
    target_type: str,
    target_id: int,
    status: int,
    detail: str,
) -> None:
    client = client_as(migrated_engine, Role.ADMIN)

    response = assign(client, name, version, target_type, target_id)

    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert assignment_rows(migrated_engine) == []
    assert audit_rows(migrated_engine) == []


@pytest.mark.parametrize(
    "body",
    [
        {"policy_name": DEFAULT, "version": 1, "target_type": "instance", "target_id": 1},
        {"policy_name": DEFAULT, "version": 1, "target_type": "group"},
        {"policy_name": DEFAULT, "version": 1, "target_type": "namespace", "target_id": 1},
        {"policy_name": DEFAULT, "version": 0, "target_type": "instance"},
        {"policy_name": "Not Valid", "version": 1, "target_type": "instance"},
    ],
)
def test_malformed_assignments_are_rejected(
    migrated_engine: Engine, instance_id: int, body: dict[str, Any]
) -> None:
    response = client_as(migrated_engine, Role.ADMIN).post("/api/v1/policy-assignments", json=body)

    assert response.status_code == 422
    assert assignment_rows(migrated_engine) == []


def test_assigning_without_gitlab_is_a_conflict(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BALUARTE_GITLAB_URL", raising=False)
    with Session(migrated_engine) as db:
        save_version(db, BASELINE)
        db.commit()
    client = client_as(migrated_engine, Role.ADMIN)

    assert assign(client, DEFAULT, 1, "instance").status_code == 409
    listing = client.get("/api/v1/policy-assignments")
    assert listing.json() == {"items": [], "next_after_id": None}
    assert client.get("/api/v1/gitlab/projects/100/effective-policies").status_code == 404


def test_unassign_removes_and_audits(migrated_engine: Engine, instance_id: int) -> None:
    client = client_as(migrated_engine, Role.ANALYST)
    created = assign(client, DEFAULT, 1, "project", 101).json()

    response = client.delete(f"/api/v1/policy-assignments/{created['id']}")

    assert response.status_code == 204
    assert assignment_rows(migrated_engine) == []
    removed = audit_rows(migrated_engine)[-1]
    assert removed.action == "policy.unassigned"
    assert removed.resource_id == str(created["id"])
    assert removed.details["target_path"] == "platform/web"
    assert removed.details["version"] == 1
    assert client.delete(f"/api/v1/policy-assignments/{created['id']}").status_code == 404


def test_assignments_are_listed_for_every_role(migrated_engine: Engine, instance_id: int) -> None:
    admin = client_as(migrated_engine, Role.ADMIN)
    ids = [
        assign(admin, DEFAULT, 1, "instance").json()["id"],
        assign(admin, "strict", 1, "group", 10).json()["id"],
        assign(admin, DEFAULT, 2, "group", 10).json()["id"],
        assign(admin, "strict", 1, "project", 100).json()["id"],
    ]
    auditor = client_as(migrated_engine, Role.AUDITOR)

    def listed(**params: object) -> tuple[list[int], int | None]:
        response = auditor.get("/api/v1/policy-assignments", params=params)
        assert response.status_code == 200
        body = response.json()
        return [item["id"] for item in body["items"]], body["next_after_id"]

    assert listed() == (ids, None)
    assert listed(policy_name="strict") == ([ids[1], ids[3]], None)
    assert listed(target_type="group", target_id=10) == ([ids[1], ids[2]], None)
    assert listed(limit=2) == (ids[:2], ids[1])
    assert listed(limit=2, after_id=ids[1]) == (ids[2:], None)


def test_listing_shows_targets_that_went_missing(migrated_engine: Engine, instance_id: int) -> None:
    client = client_as(migrated_engine, Role.ADMIN)
    assign(client, DEFAULT, 1, "group", 11)
    with Session(migrated_engine) as db:
        group = db.scalars(select(GitLabGroup).where(GitLabGroup.gitlab_id == 11)).one()
        group.state = "missing"
        db.commit()

    [item] = client.get("/api/v1/policy-assignments").json()["items"]

    assert item["target_state"] == "missing"
    assert item["target_path"] == "platform/backend"


def test_nearest_assignment_wins_per_policy_name(migrated_engine: Engine, instance_id: int) -> None:
    admin = client_as(migrated_engine, Role.ADMIN)
    assign(admin, DEFAULT, 1, "instance")
    assign(admin, DEFAULT, 2, "group", 10)
    assign(admin, "strict", 1, "instance")

    response = client_as(migrated_engine, Role.AUDITOR).get(
        "/api/v1/gitlab/projects/100/effective-policies"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ancestry"] == "complete"
    assert body["gaps"] == []
    assert body["target"] == {
        "target_type": "project",
        "target_id": 100,
        "path": "platform/backend/api/svc",
        "state": "active",
    }
    assert [(level["target_type"], level["target_id"]) for level in body["path"]] == [
        ("project", 100),
        ("group", 12),
        ("group", 11),
        ("group", 10),
        ("instance", instance_id),
    ]
    assert summary(body) == [
        (DEFAULT, 2, "group", 10, "effective"),
        (DEFAULT, 1, "instance", instance_id, "overridden"),
        ("strict", 1, "instance", instance_id, "effective"),
    ]
    assert summary(body, "policies") == [
        (DEFAULT, 2, "group", 10, "effective"),
        ("strict", 1, "instance", instance_id, "effective"),
    ]


def test_project_assignment_overrides_its_groups(migrated_engine: Engine, instance_id: int) -> None:
    admin = client_as(migrated_engine, Role.ADMIN)
    assign(admin, DEFAULT, 2, "group", 12)
    assign(admin, DEFAULT, 1, "project", 100)

    body = admin.get("/api/v1/gitlab/projects/100/effective-policies").json()

    assert summary(body) == [
        (DEFAULT, 1, "project", 100, "effective"),
        (DEFAULT, 2, "group", 12, "overridden"),
    ]


def test_assignments_elsewhere_do_not_apply(migrated_engine: Engine, instance_id: int) -> None:
    admin = client_as(migrated_engine, Role.ADMIN)
    assign(admin, DEFAULT, 1, "group", 11)
    assign(admin, "strict", 1, "project", 100)

    body = admin.get("/api/v1/gitlab/projects/101/effective-policies").json()

    assert body["considered"] == []
    assert body["policies"] == []


def test_scope_limits_where_a_policy_applies(migrated_engine: Engine, instance_id: int) -> None:
    admin = client_as(migrated_engine, Role.ADMIN)
    assign(admin, "backend-only", 1, "instance")

    inside = admin.get("/api/v1/gitlab/projects/100/effective-policies").json()
    outside = admin.get("/api/v1/gitlab/projects/101/effective-policies").json()

    assert summary(inside, "policies") == [
        ("backend-only", 1, "instance", instance_id, "effective")
    ]
    assert summary(outside) == [("backend-only", 1, "instance", instance_id, "out_of_scope")]
    assert outside["policies"] == []


def test_out_of_scope_does_not_fall_back_to_a_farther_version(
    migrated_engine: Engine, instance_id: int
) -> None:
    admin = client_as(migrated_engine, Role.ADMIN)
    assign(admin, DEFAULT, 1, "instance")
    # Version 3 narrows the scope to platform/backend.
    assign(admin, DEFAULT, 3, "group", 10)

    body = admin.get("/api/v1/gitlab/projects/101/effective-policies").json()

    assert summary(body) == [
        (DEFAULT, 3, "group", 10, "out_of_scope"),
        (DEFAULT, 1, "instance", instance_id, "overridden"),
    ]
    assert body["policies"] == []


def test_group_resolution(migrated_engine: Engine, instance_id: int) -> None:
    admin = client_as(migrated_engine, Role.ADMIN)
    assign(admin, DEFAULT, 1, "instance")
    assign(admin, DEFAULT, 2, "group", 10)
    assign(admin, "strict", 1, "group", 11)
    assign(admin, "strict", 1, "group", 12)

    response = client_as(migrated_engine, Role.AUDITOR).get(
        "/api/v1/gitlab/groups/11/effective-policies"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ancestry"] == "complete"
    assert [(level["target_type"], level["target_id"]) for level in body["path"]] == [
        ("group", 11),
        ("group", 10),
        ("instance", instance_id),
    ]
    assert summary(body) == [
        (DEFAULT, 2, "group", 10, "effective"),
        (DEFAULT, 1, "instance", instance_id, "overridden"),
        ("strict", 1, "group", 11, "effective"),
    ]


def test_unknown_parent_makes_ancestry_incomplete(
    migrated_engine: Engine, instance_id: int
) -> None:
    admin = client_as(migrated_engine, Role.ADMIN)
    assign(admin, DEFAULT, 1, "instance")

    body = admin.get("/api/v1/gitlab/projects/103/effective-policies").json()

    assert body["ancestry"] == "incomplete"
    assert body["gaps"] == [{"gitlab_id": 99, "reason": "not_in_inventory"}]
    assert [(level["target_type"], level["target_id"]) for level in body["path"]] == [
        ("project", 103),
        ("group", 20),
        ("instance", instance_id),
    ]
    # What is known still resolves; the gap says it may not be everything.
    assert summary(body, "policies") == [(DEFAULT, 1, "instance", instance_id, "effective")]


def test_missing_ancestor_makes_ancestry_incomplete(
    migrated_engine: Engine, instance_id: int
) -> None:
    admin = client_as(migrated_engine, Role.ADMIN)
    assign(admin, "strict", 1, "group", 31)

    body = admin.get("/api/v1/gitlab/projects/104/effective-policies").json()

    assert body["ancestry"] == "incomplete"
    assert body["gaps"] == [{"gitlab_id": 30, "reason": "missing"}]
    assert [level["target_id"] for level in body["path"]] == [104, 31, 30, instance_id]
    assert summary(body, "policies") == [("strict", 1, "group", 31, "effective")]


def test_personal_project_reports_its_namespace_as_unknown(
    migrated_engine: Engine, instance_id: int
) -> None:
    admin = client_as(migrated_engine, Role.ADMIN)
    assign(admin, DEFAULT, 1, "instance")
    assign(admin, "backend-only", 1, "instance")
    assign(admin, "strict", 1, "project", 102)

    body = admin.get("/api/v1/gitlab/projects/102/effective-policies").json()

    assert body["ancestry"] == "incomplete"
    assert body["gaps"] == [{"gitlab_id": 500, "reason": "not_in_inventory"}]
    assert summary(body) == [
        ("backend-only", 1, "instance", instance_id, "out_of_scope"),
        (DEFAULT, 1, "instance", instance_id, "effective"),
        ("strict", 1, "project", 102, "effective"),
    ]


def test_missing_project_still_resolves(migrated_engine: Engine, instance_id: int) -> None:
    admin = client_as(migrated_engine, Role.ADMIN)
    assign(admin, DEFAULT, 1, "group", 10)

    body = admin.get("/api/v1/gitlab/projects/105/effective-policies").json()

    assert body["target"]["state"] == "missing"
    assert body["ancestry"] == "complete"
    assert summary(body, "policies") == [(DEFAULT, 1, "group", 10, "effective")]


def test_cycle_in_the_inventory_is_a_gap(migrated_engine: Engine, instance_id: int) -> None:
    body = (
        client_as(migrated_engine, Role.ADMIN)
        .get("/api/v1/gitlab/groups/40/effective-policies")
        .json()
    )

    assert body["ancestry"] == "incomplete"
    assert body["gaps"] == [{"gitlab_id": 40, "reason": "cycle"}]
    assert [level["target_id"] for level in body["path"]] == [40, 41, instance_id]


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/gitlab/projects/999/effective-policies",
        "/api/v1/gitlab/groups/999/effective-policies",
    ],
)
def test_unknown_targets_are_not_found(
    migrated_engine: Engine, instance_id: int, path: str
) -> None:
    assert client_as(migrated_engine, Role.ADMIN).get(path).status_code == 404


def test_database_keeps_one_version_per_policy_and_target(
    migrated_engine: Engine, instance_id: int
) -> None:
    with Session(migrated_engine) as db:
        v1, v2 = db.scalars(
            select(Policy.id).where(Policy.name == DEFAULT).order_by(Policy.version).limit(2)
        ).all()
        db.add(
            PolicyAssignment(
                instance_id=instance_id,
                policy_id=v1,
                policy_name=DEFAULT,
                target_type="group",
                target_id=10,
            )
        )
        db.commit()
        db.add(
            PolicyAssignment(
                instance_id=instance_id,
                policy_id=v2,
                policy_name=DEFAULT,
                target_type="group",
                target_id=10,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


@pytest.mark.parametrize(
    ("policy_name", "target_type", "target_offset"),
    [("strict", "group", 0), (DEFAULT, "instance", 1)],
)
def test_database_rejects_inconsistent_assignments(
    migrated_engine: Engine,
    instance_id: int,
    policy_name: str,
    target_type: str,
    target_offset: int,
) -> None:
    with Session(migrated_engine) as db:
        policy_id = db.scalars(select(Policy.id).where(Policy.name == DEFAULT).limit(1)).one()
        db.add(
            PolicyAssignment(
                instance_id=instance_id,
                policy_id=policy_id,
                policy_name=policy_name,
                target_type=target_type,
                target_id=instance_id + target_offset,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
