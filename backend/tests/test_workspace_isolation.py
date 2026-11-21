"""Several workspaces in one deployment (roadmap M11.4).

People sign in through the fake identity provider with groups that grant roles in one workspace or
another, and every request is checked to see only its own workspace's data.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from oidc_fake import PUBLIC_URL, FakeIdP, make_settings
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from baluarte.auth.dependencies import (
    CONNECTION_HEADER,
    CSRF_HEADER,
    WORKSPACE_HEADER,
    auth_settings,
    database_engine,
    oidc_client,
)
from baluarte.db.models import AuditEvent, GitLabInstance, GitLabProject, Tag, Workspace
from baluarte.main import create_app
from baluarte.workspaces import default_workspace_id

pytestmark = pytest.mark.db

ACME_GROUPS = {"admin": ["acme-admins"], "analyst": ["acme-analysts"], "auditor": ["acme-auditors"]}


@dataclass
class World:
    engine: Engine
    default_id: int
    acme_id: int
    default_connection: int
    acme_connection: int
    default_tag: int
    acme_tag: int


@pytest.fixture
def world(migrated_engine: Engine) -> World:
    with Session(migrated_engine) as db:
        default_id = default_workspace_id(db)
        acme = Workspace(slug="acme", name="Acme", group_roles=ACME_GROUPS)
        db.add(acme)
        db.flush()
        connections: list[int] = []
        tags: list[int] = []
        for workspace_id, host, project in (
            (default_id, "gitlab.default.test", "home/web"),
            (acme.id, "gitlab.acme.test", "acme/api"),
        ):
            connection = GitLabInstance(base_url=f"https://{host}", workspace_id=workspace_id)
            db.add(connection)
            db.flush()
            db.add(
                GitLabProject(
                    instance_id=connection.id,
                    gitlab_id=42,
                    namespace_gitlab_id=7,
                    path_with_namespace=project,
                    name=project.split("/")[1],
                )
            )
            tag = Tag(
                workspace_id=workspace_id,
                instance_id=connection.id,
                key="team",
                value=project.split("/")[0],
                created_by="user:0",
            )
            db.add(tag)
            db.flush()
            connections.append(connection.id)
            tags.append(tag.id)
        db.commit()
        return World(
            migrated_engine, default_id, acme.id, connections[0], connections[1], tags[0], tags[1]
        )


class Person:
    """A signed-in browser for someone in ``groups``."""

    def __init__(self, engine: Engine, groups: list[str]) -> None:
        settings = make_settings()
        idp = FakeIdP()
        idp.claims["groups"] = groups
        idp.subject = "user-" + "-".join(groups)
        app = create_app()
        app.dependency_overrides[auth_settings] = lambda: settings
        app.dependency_overrides[oidc_client] = lambda: idp.client(settings)
        app.dependency_overrides[database_engine] = lambda: engine
        self.client = TestClient(app, base_url=PUBLIC_URL, follow_redirects=False)
        self.csrf = ""
        start = self.client.get("/api/v1/auth/login")
        query = idp.authorize(start.headers["location"])
        signed_in = self.client.get(
            "/api/v1/auth/callback", params={"state": query["state"], "code": idp.code}
        )
        assert signed_in.status_code == 303, signed_in.headers.get("location")
        self.csrf = self.me().json()["csrf_token"]

    def headers(self, workspace: str | None, connection: int | None = None) -> dict[str, str]:
        headers = {CSRF_HEADER: self.csrf}
        if workspace is not None:
            headers[WORKSPACE_HEADER] = workspace
        if connection is not None:
            headers[CONNECTION_HEADER] = str(connection)
        return headers

    def me(self, workspace: str | None = None) -> Any:
        return self.client.get("/api/v1/auth/me", headers=self.headers(workspace))

    def get(self, path: str, workspace: str | None = None, connection: int | None = None) -> Any:
        return self.client.get(path, headers=self.headers(workspace, connection))

    def send(self, method: str, path: str, body: Any, workspace: str | None = None) -> Any:
        return self.client.request(method, path, json=body, headers=self.headers(workspace))


@pytest.fixture
def people(world: World) -> Iterator[dict[str, Person]]:
    people = {
        "home": Person(world.engine, ["baluarte-analysts"]),
        "acme": Person(world.engine, ["acme-analysts"]),
        "both": Person(world.engine, ["baluarte-admins", "acme-auditors"]),
        "acme_admin": Person(world.engine, ["acme-admins"]),
    }
    yield people
    for person in people.values():
        person.client.close()


def test_sign_in_grants_roles_per_workspace(people: dict[str, Person]) -> None:
    both = people["both"].me().json()

    assert both["workspace"] == "default"
    assert both["roles"] == ["admin"]
    assert {w["slug"]: w["roles"] for w in both["workspaces"]} == {
        "default": ["admin"],
        "acme": ["auditor"],
    }
    in_acme = people["both"].me("acme").json()
    assert (in_acme["workspace"], in_acme["roles"]) == ("acme", ["auditor"])
    assert "settings:manage" not in in_acme["permissions"]
    acme = people["acme"].me().json()
    assert (acme["workspace"], acme["roles"]) == ("acme", ["analyst"])


def test_no_role_in_a_workspace_is_refused(people: dict[str, Person]) -> None:
    assert people["home"].me("acme").status_code == 403
    assert people["acme"].me("default").status_code == 403
    assert people["home"].me("no-such-workspace").status_code == 403


def test_lists_show_only_the_workspace_s_own_records(
    world: World, people: dict[str, Person]
) -> None:
    home_tags = people["home"].get("/api/v1/tags").json()["items"]
    acme_tags = people["acme"].get("/api/v1/tags").json()["items"]
    home_projects = people["home"].get("/api/v1/gitlab/projects").json()["items"]
    acme_projects = people["acme"].get("/api/v1/gitlab/projects").json()["items"]

    assert [tag["id"] for tag in home_tags] == [world.default_tag]
    assert [tag["id"] for tag in acme_tags] == [world.acme_tag]
    assert [p["path_with_namespace"] for p in home_projects] == ["home/web"]
    assert [p["path_with_namespace"] for p in acme_projects] == ["acme/api"]


def test_another_workspace_s_records_are_not_found_by_id(
    world: World, people: dict[str, Person]
) -> None:
    assert people["acme"].get(f"/api/v1/tags/{world.default_tag}").status_code == 404
    assert people["home"].get(f"/api/v1/tags/{world.acme_tag}").status_code == 404
    assert people["acme"].get(f"/api/v1/tags/{world.acme_tag}").status_code == 200


def test_a_connection_from_another_workspace_cannot_be_chosen(
    world: World, people: dict[str, Person]
) -> None:
    both = people["both"]

    wrong = both.get("/api/v1/gitlab/projects", "acme", world.default_connection)
    right = both.get("/api/v1/gitlab/projects", "acme", world.acme_connection)

    assert wrong.status_code == 404
    assert right.status_code == 200
    assert [p["path_with_namespace"] for p in right.json()["items"]] == ["acme/api"]


def test_connections_are_listed_per_workspace(world: World, people: dict[str, Person]) -> None:
    both = people["both"]

    home = both.get("/api/v1/connections").json()["connections"]
    acme = both.get("/api/v1/connections", "acme").json()["connections"]

    assert [c["id"] for c in home] == [world.default_connection]
    assert [c["id"] for c in acme] == [world.acme_connection]


def test_audit_events_name_their_workspace_and_stay_in_it(
    world: World, people: dict[str, Person]
) -> None:
    created = people["acme_admin"].send(
        "POST", "/api/v1/tags", {"key": "cost-center", "value": "cc-1"}
    )
    assert created.status_code == 201, created.text

    with Session(world.engine) as db:
        event = db.scalars(select(AuditEvent).where(AuditEvent.action == "tags.created")).one()
    assert event.workspace_id == world.acme_id
    acme_actions = [
        e["action"] for e in people["both"].get("/api/v1/audit-events", "acme").json()["items"]
    ]
    home_actions = [e["action"] for e in people["both"].get("/api/v1/audit-events").json()["items"]]
    assert "tags.created" in acme_actions
    assert "tags.created" not in home_actions
    # Sign-ins happen outside any workspace and belong to the default one.
    assert "auth.login" in home_actions
    assert "auth.login" not in acme_actions


def test_records_created_in_a_workspace_belong_to_it(
    world: World, people: dict[str, Person]
) -> None:
    created = people["acme_admin"].send(
        "POST", "/api/v1/tags", {"key": "team", "value": "platform"}
    )

    with Session(world.engine) as db:
        tag = db.get_one(Tag, created.json()["id"])
    assert (tag.workspace_id, tag.instance_id) == (world.acme_id, world.acme_connection)


def test_only_deployment_administrators_manage_workspaces(
    world: World, people: dict[str, Person]
) -> None:
    body = {"slug": "beta", "name": "Beta", "group_roles": {"analyst": ["beta-analysts"]}}

    refused = people["acme_admin"].send("POST", "/api/v1/workspaces", body)
    created = people["both"].send("POST", "/api/v1/workspaces", body)
    again = people["both"].send("POST", "/api/v1/workspaces", body)
    renamed = people["both"].send("PATCH", "/api/v1/workspaces/beta", {"name": "Beta Corp"})
    default_groups = people["both"].send(
        "PATCH", "/api/v1/workspaces/default", {"group_roles": {"admin": ["x"]}}
    )

    assert refused.status_code == 403
    assert created.status_code == 201
    assert created.json()["group_roles"] == {"analyst": ["beta-analysts"]}
    assert again.status_code == 409
    assert renamed.json()["name"] == "Beta Corp"
    assert default_groups.status_code == 409
    listed = people["both"].get("/api/v1/workspaces").json()
    assert listed["can_manage"] is True
    assert [w["slug"] for w in listed["workspaces"]] == ["default", "acme", "beta"]
    acme_view = people["acme"].get("/api/v1/workspaces").json()
    assert acme_view["can_manage"] is False
    assert [w["slug"] for w in acme_view["workspaces"]] == ["acme"]
    assert acme_view["workspaces"][0]["group_roles"] is None
    assert [c["id"] for c in acme_view["workspaces"][0]["connections"]] == [world.acme_connection]


def test_a_new_workspace_s_groups_apply_from_the_next_sign_in(
    world: World, people: dict[str, Person]
) -> None:
    people["both"].send(
        "POST",
        "/api/v1/workspaces",
        {"slug": "beta", "name": "Beta", "group_roles": {"analyst": ["baluarte-analysts"]}},
    )

    assert people["home"].me("beta").status_code == 403
    again = Person(world.engine, ["baluarte-analysts"])
    assert again.me("beta").json()["roles"] == ["analyst"]
    again.client.close()


def test_a_url_connected_in_another_workspace_is_refused(
    world: World, people: dict[str, Person]
) -> None:
    response = people["acme_admin"].send(
        "POST",
        "/api/v1/connections",
        {"url": "https://gitlab.default.test", "token": "glpat-x"},
    )

    assert response.status_code == 409
    assert "another workspace" in response.json()["detail"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/settings",
        "/api/v1/settings/oidc",
        "/api/v1/settings/gitlab",
        "/api/v1/settings/notifications",
        "/api/v1/settings/audit-shipping",
    ],
)
def test_deployment_settings_stay_with_the_default_workspace(
    people: dict[str, Person], path: str
) -> None:
    acme_admin = people["acme_admin"].get(path)
    deployment_admin_in_acme = people["both"].get(path, "acme")
    deployment_admin = people["both"].get(path)

    assert acme_admin.status_code == 403
    assert deployment_admin_in_acme.status_code == 403
    assert deployment_admin.status_code not in (401, 403)


def test_people_listed_for_manager_scopes_are_the_workspace_s(
    people: dict[str, Person],
) -> None:
    acme = people["acme_admin"].get("/api/v1/manager/users").json()["items"]

    assert {tuple(person["roles"]) for person in acme} == {("analyst",), ("auditor",), ("admin",)}
    assert len(acme) == 3  # acme analyst, the auditor holding both, the acme admin
