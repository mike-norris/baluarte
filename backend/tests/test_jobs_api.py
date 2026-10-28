from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from baluarte.auth.dependencies import Principal, current_principal, database_engine
from baluarte.auth.roles import Role
from baluarte.jobs import queue
from baluarte.main import create_app

pytestmark = pytest.mark.db


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
    return TestClient(app)


@pytest.fixture
def jobs(migrated_engine: Engine) -> list[int]:
    with Session(migrated_engine) as db:
        ids = [
            queue.enqueue(db, "inventory.sync", f"group-{n}", {"token_hint": "never-shown"})
            for n in range(3)
        ]
        ids.append(queue.enqueue(db, "export.build", "export-1"))
        db.commit()
        claimed = queue.claim(db, "worker-a", timedelta(minutes=5), ["export.build"])
        assert claimed is not None
        queue.fail(db, claimed, "manifest could not be written", retry=False)
    return ids


@pytest.mark.parametrize("role", [Role.ANALYST, Role.AUDITOR])
def test_only_admins_can_list_jobs(migrated_engine: Engine, role: Role) -> None:
    response = client_as(migrated_engine, role).get("/api/v1/jobs")

    assert response.status_code == 403


def test_lists_newest_first_without_payloads(migrated_engine: Engine, jobs: list[int]) -> None:
    response = client_as(migrated_engine, Role.ADMIN).get("/api/v1/jobs")

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["items"]] == sorted(jobs, reverse=True)
    assert body["next_before_id"] is None
    assert "payload" not in body["items"][0]
    assert "never-shown" not in response.text
    failed = body["items"][0]
    assert (failed["kind"], failed["state"], failed["attempt"]) == ("export.build", "failed", 1)
    assert failed["error"] == "manifest could not be written"


def test_filters_by_state_and_kind(migrated_engine: Engine, jobs: list[int]) -> None:
    client = client_as(migrated_engine, Role.ADMIN)

    failed = client.get("/api/v1/jobs", params={"state": "failed"}).json()["items"]
    inventory = client.get("/api/v1/jobs", params={"kind": "inventory.sync"}).json()["items"]

    assert [item["kind"] for item in failed] == ["export.build"]
    assert len(inventory) == 3


def test_pages_with_before_id(migrated_engine: Engine, jobs: list[int]) -> None:
    client = client_as(migrated_engine, Role.ADMIN)

    first = client.get("/api/v1/jobs", params={"limit": 3}).json()
    second = client.get(
        "/api/v1/jobs", params={"limit": 3, "before_id": first["next_before_id"]}
    ).json()

    assert len(first["items"]) == 3
    assert first["next_before_id"] == first["items"][-1]["id"]
    assert [item["id"] for item in second["items"]] == [min(jobs)]
    assert second["next_before_id"] is None


@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": 101}, {"state": "stuck"}, {"before_id": 0}],
)
def test_rejects_invalid_parameters(migrated_engine: Engine, params: dict[str, object]) -> None:
    response = client_as(migrated_engine, Role.ADMIN).get("/api/v1/jobs", params=params)

    assert response.status_code == 422
