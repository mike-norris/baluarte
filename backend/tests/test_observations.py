from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from fastapi.testclient import TestClient
from gitlab_fake import TOKEN, URL, FakeGitLab, group, paged, project, settings
from oidc_fake import make_settings
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from baluarte.api.v1.gitlab import gitlab_transport
from baluarte.auth.dependencies import Principal, auth_settings, current_principal, database_engine
from baluarte.auth.roles import Role
from baluarte.db.migrate import alembic_config
from baluarte.db.models import CollectionJob, CollectionRun, GitLabProject, Observation
from baluarte.evidence.observations import latest, payload_digest, verify
from baluarte.integrations.gitlab.client import GitLabClient
from baluarte.integrations.gitlab.discovery import Discovery
from baluarte.integrations.gitlab.groups import discover_groups
from baluarte.integrations.gitlab.jobs import (
    DETECT_CAPABILITIES,
    DISCOVER_PROJECTS,
    make_detect_capabilities,
    make_discover_projects,
)
from baluarte.integrations.gitlab.projects import discover_projects
from baluarte.jobs import queue
from baluarte.jobs.worker import Worker
from baluarte.main import create_app

PROJECT_FIELDS = {
    "gitlab_id",
    "path_with_namespace",
    "name",
    "namespace_gitlab_id",
    "namespace_kind",
    "visibility",
    "archived",
    "default_branch",
}

PAGES = [
    [
        project(10, "platform/api", 1, visibility="internal"),
        # Fields outside the allowlist must never reach evidence.
        project(11, "platform/web", 1, http_url_to_repo="https://x/y.git", runners_token="t0k"),
    ],
    [project(12, "alice/scratch", 900, kind="user")],
]


@pytest.fixture
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)


def fake_with(pages: list[list[dict[str, Any]]]) -> FakeGitLab:
    fake = FakeGitLab()
    fake.set("/projects", paged("/projects", pages))
    return fake


def discover(engine: Engine, fake: FakeGitLab) -> Discovery:
    client = GitLabClient(settings(), transport=fake.transport, sleep=lambda _: None)
    return discover_projects(client, engine, URL)


def observations(engine: Engine) -> list[Observation]:
    with Session(engine) as db:
        return list(db.scalars(select(Observation).order_by(Observation.id)))


def project_ids(engine: Engine) -> dict[int, int]:
    with Session(engine) as db:
        return {
            gitlab_id: row_id
            for gitlab_id, row_id in db.execute(select(GitLabProject.gitlab_id, GitLabProject.id))
        }


def age(engine: Engine) -> None:
    with Session(engine) as db:
        db.execute(
            text("UPDATE gitlab_project SET last_seen_at = last_seen_at - interval '1 hour'")
        )
        db.commit()


def test_digest_is_canonical_and_independent_of_key_order() -> None:
    first = {"visibility": "private", "gitlab_id": 10, "nested": {"b": 1, "a": [True, None]}}
    second = {"nested": {"a": [True, None], "b": 1}, "gitlab_id": 10, "visibility": "private"}

    assert payload_digest(first) == payload_digest(second)
    assert len(payload_digest(first)) == 64
    assert payload_digest(first) != payload_digest(first | {"visibility": "public"})


def test_verify_detects_a_changed_payload() -> None:
    payload = {"gitlab_id": 10, "visibility": "private"}
    row = Observation(payload=payload, payload_digest=payload_digest(payload))
    assert verify(row)

    row.payload = payload | {"visibility": "public"}
    assert not verify(row)
    assert verify(Observation(payload=None, payload_digest=None))
    assert not verify(Observation(payload=None, payload_digest="0" * 64))


@pytest.mark.db
def test_discovery_records_allowlisted_evidence_for_each_project(migrated_engine: Engine) -> None:
    discover(migrated_engine, fake_with(PAGES))

    rows = observations(migrated_engine)
    ids = project_ids(migrated_engine)
    assert sorted(row.project_id for row in rows if row.project_id) == sorted(ids.values())
    by_project = {row.project_id: row for row in rows}
    api = by_project[ids[10]]
    assert api.payload == {
        "gitlab_id": 10,
        "path_with_namespace": "platform/api",
        "name": "api",
        "namespace_gitlab_id": 1,
        "namespace_kind": "group",
        "visibility": "internal",
        "archived": False,
        "default_branch": "main",
    }
    assert (api.source, api.source_ref, api.collection_status) == (
        "GET /projects",
        "/projects/10",
        "ok",
    )
    assert api.group_id is None and api.error is None
    assert all(set(row.payload or {}) == PROJECT_FIELDS for row in rows)
    assert "t0k" not in str(by_project[ids[11]].payload)
    assert all(verify(row) for row in rows)
    assert len({row.collected_at for row in rows if row.collected_at}) >= 1


@pytest.mark.db
def test_unchanged_data_is_not_stored_again(migrated_engine: Engine) -> None:
    discover(migrated_engine, fake_with(PAGES))
    discover(migrated_engine, fake_with(PAGES))
    assert len(observations(migrated_engine)) == 3

    changed = [[project(10, "platform/api", 1, visibility="public"), PAGES[0][1]], PAGES[1]]
    discover(migrated_engine, fake_with(changed))

    rows = observations(migrated_engine)
    assert len(rows) == 4
    with Session(migrated_engine) as db:
        newest = latest(db, rows[0].instance_id, "project_id", project_ids(migrated_engine)[10])
    assert newest is not None
    assert newest.payload is not None and newest.payload["visibility"] == "public"
    assert newest.id == rows[-1].id


@pytest.mark.db
@pytest.mark.parametrize(
    ("lookup", "status", "error"),
    [
        (httpx.Response(404), "not_found", "/projects/12 was not found (404)"),
        (httpx.Response(403), "forbidden", "the token may not read /projects/12 (403)"),
        (httpx.Response(500), "failed", "could not confirm: "),
    ],
)
def test_resources_that_could_not_be_read_are_evidence_too(
    migrated_engine: Engine, lookup: httpx.Response, status: str, error: str
) -> None:
    discover(migrated_engine, fake_with(PAGES))
    age(migrated_engine)
    fake = fake_with([PAGES[0]])
    fake.set("/projects/12", lookup)

    discover(migrated_engine, fake)

    newest = observations(migrated_engine)[-1]
    assert newest.project_id == project_ids(migrated_engine)[12]
    assert (newest.source, newest.source_ref) == ("GET /projects/:id", "/projects/12")
    assert newest.collection_status == status
    assert newest.payload is None and newest.payload_digest is None
    assert newest.error is not None and newest.error.startswith(error)
    assert TOKEN not in newest.error


@pytest.mark.db
def test_repeated_failure_is_recorded_once_and_recovery_again(migrated_engine: Engine) -> None:
    discover(migrated_engine, fake_with(PAGES))
    for _ in range(2):
        age(migrated_engine)
        fake = fake_with([PAGES[0]])
        fake.set("/projects/12", httpx.Response(500))
        discover(migrated_engine, fake)
    assert [row.collection_status for row in observations(migrated_engine)][-2:] == ["ok", "failed"]
    assert len(observations(migrated_engine)) == 4

    discover(migrated_engine, fake_with(PAGES))

    rows = observations(migrated_engine)
    assert len(rows) == 5
    assert (rows[-1].collection_status, rows[-1].payload) == ("ok", rows[2].payload)


@pytest.mark.db
def test_groups_are_observed_with_their_own_allowlist(migrated_engine: Engine) -> None:
    fake = FakeGitLab()
    fake.set("/groups", paged("/groups", [[group(1, "platform", web_url="https://x/platform")]]))
    client = GitLabClient(settings(), transport=fake.transport, sleep=lambda _: None)

    discover_groups(client, migrated_engine, URL)

    (row,) = observations(migrated_engine)
    assert row.group_id is not None and row.project_id is None
    assert (row.source, row.source_ref) == ("GET /groups", "/groups/1")
    assert row.payload == {
        "gitlab_id": 1,
        "parent_gitlab_id": None,
        "full_path": "platform",
        "name": "platform",
        "visibility": "private",
    }


@pytest.mark.db
def test_observations_name_the_run_and_job_that_read_them(
    migrated_engine: Engine, gitlab_env: None
) -> None:
    with Session(migrated_engine) as db:
        job_id = queue.enqueue(db, DISCOVER_PROJECTS, "test", max_attempts=1)
        db.commit()
    handler = make_discover_projects(fake_with(PAGES).transport)
    assert Worker(migrated_engine, {DISCOVER_PROJECTS: handler}).run_once()

    with Session(migrated_engine) as db:
        run = db.scalars(select(CollectionRun)).one()
        assert db.get_one(CollectionJob, job_id).state == "succeeded"
    rows = observations(migrated_engine)
    assert len(rows) == 3
    assert {(row.collection_run_id, row.collection_job_id) for row in rows} == {(run.id, job_id)}


def run_capabilities(engine: Engine, fake: FakeGitLab) -> None:
    with Session(engine) as db:
        queue.enqueue(db, DETECT_CAPABILITIES, f"test-{datetime.now(UTC)}", max_attempts=1)
        db.commit()
    handler = make_detect_capabilities(fake.transport)
    assert Worker(engine, {DETECT_CAPABILITIES: handler}).run_once()


@pytest.mark.db
def test_capability_snapshots_are_instance_evidence(
    migrated_engine: Engine, gitlab_env: None
) -> None:
    run_capabilities(migrated_engine, FakeGitLab())
    run_capabilities(migrated_engine, FakeGitLab())

    (row,) = observations(migrated_engine)
    assert (row.project_id, row.group_id, row.source) == (None, None, "capabilities")
    assert row.collection_status == "ok"
    assert row.payload is not None
    assert set(row.payload) == {"version", "edition", "tier", "probes"}
    assert row.payload["version"] == "17.5.1-ee"
    assert verify(row)

    failing = FakeGitLab()
    failing.set("/version", httpx.Response(401))
    run_capabilities(migrated_engine, failing)

    newest = observations(migrated_engine)[-1]
    assert (newest.collection_status, newest.payload) == ("failed", None)
    assert newest.error == "GitLab rejected the token (401)"


@pytest.mark.db
def test_observations_cannot_be_updated(migrated_engine: Engine) -> None:
    discover(migrated_engine, fake_with(PAGES))

    with Session(migrated_engine) as db, pytest.raises(DBAPIError, match=r"append-only|UPDATE"):
        db.execute(text("UPDATE observation SET payload = '{}'::jsonb"))
        db.flush()


@pytest.mark.db
def test_downgrade_removes_the_run_reference(migrated_engine: Engine) -> None:
    with migrated_engine.begin() as connection:
        config = alembic_config()
        config.attributes["connection"] = connection
        command.downgrade(config, "0007")

    columns = {column["name"] for column in inspect(migrated_engine).get_columns("observation")}
    assert "collection_run_id" not in columns
    indexes = {index["name"] for index in inspect(migrated_engine).get_indexes("observation")}
    assert "ix_observation_instance_collected" not in indexes


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
    app.dependency_overrides[gitlab_transport] = lambda: FakeGitLab().transport
    return TestClient(app)


@pytest.mark.db
@pytest.mark.parametrize("role", [Role.ADMIN, Role.ANALYST, Role.AUDITOR])
def test_api_lists_project_evidence_newest_first(
    migrated_engine: Engine, gitlab_env: None, role: Role
) -> None:
    discover(migrated_engine, fake_with(PAGES))
    changed = [[project(10, "platform/api", 1, visibility="public"), PAGES[0][1]], PAGES[1]]
    discover(migrated_engine, fake_with(changed))
    api = client_as(migrated_engine, role)

    first = api.get("/api/v1/gitlab/projects/10/observations", params={"limit": 1})

    assert first.status_code == 200
    body = first.json()
    (newest,) = body["items"]
    assert newest["payload"]["visibility"] == "public"
    assert newest["digest_valid"] is True
    assert newest["source_ref"] == "/projects/10"
    second = api.get(
        "/api/v1/gitlab/projects/10/observations",
        params={"limit": 1, "before_id": body["next_before_id"]},
    ).json()
    assert [item["payload"]["visibility"] for item in second["items"]] == ["internal"]
    assert second["next_before_id"] is None


@pytest.mark.db
def test_api_reports_a_tampered_digest(migrated_engine: Engine, gitlab_env: None) -> None:
    discover(migrated_engine, fake_with(PAGES))
    with migrated_engine.begin() as connection:
        # Simulates a privileged change that bypasses the trigger.
        connection.execute(text("SET LOCAL session_replication_role = replica"))
        connection.execute(
            text(
                "UPDATE observation SET payload = jsonb_set(payload, '{visibility}', "
                "'\"public\"') WHERE source_ref = '/projects/10'"
            )
        )

    items = (
        client_as(migrated_engine, Role.AUDITOR)
        .get("/api/v1/gitlab/projects/10/observations")
        .json()["items"]
    )

    assert [item["digest_valid"] for item in items] == [False]


@pytest.mark.db
def test_api_group_and_instance_evidence(migrated_engine: Engine, gitlab_env: None) -> None:
    api = client_as(migrated_engine, Role.ANALYST)
    assert api.get("/api/v1/gitlab/instance/observations").json() == {
        "items": [],
        "next_before_id": None,
    }
    fake = FakeGitLab()
    fake.set("/groups", paged("/groups", [[group(1, "platform")]]))
    discover_groups(GitLabClient(settings(), transport=fake.transport), migrated_engine, URL)
    run_capabilities(migrated_engine, FakeGitLab())

    groups = api.get("/api/v1/gitlab/groups/1/observations").json()["items"]
    instance = api.get("/api/v1/gitlab/instance/observations").json()["items"]

    assert [item["source_ref"] for item in groups] == ["/groups/1"]
    assert [item["source"] for item in instance] == ["capabilities"]


@pytest.mark.db
def test_api_unknown_targets_and_permissions(migrated_engine: Engine, gitlab_env: None) -> None:
    discover(migrated_engine, fake_with(PAGES))

    assert (
        client_as(migrated_engine, Role.AUDITOR)
        .get("/api/v1/gitlab/projects/999/observations")
        .status_code
        == 404
    )
    assert (
        client_as(migrated_engine, Role.AUDITOR)
        .get("/api/v1/gitlab/groups/999/observations")
        .status_code
        == 404
    )
    for path in ("projects/10", "groups/1", "instance"):
        response = client_as(migrated_engine).get(f"/api/v1/gitlab/{path}/observations")
        assert response.status_code == 403


@pytest.mark.db
def test_api_without_gitlab_configured(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BALUARTE_GITLAB_URL", raising=False)
    api = client_as(migrated_engine, Role.ADMIN)

    assert api.get("/api/v1/gitlab/projects/10/observations").status_code == 404
    assert api.get("/api/v1/gitlab/instance/observations").json()["items"] == []
