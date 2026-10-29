"""Findings, evaluation results, evaluation freshness and queueing evaluations over the API."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from gitlab_fake import TOKEN, URL
from oidc_fake import make_settings
from sqlalchemy import Engine, delete, select, update
from sqlalchemy.orm import Session
from test_evaluation_store import NEXT_VERSION, World

from baluarte.auth.dependencies import Principal, auth_settings, current_principal, database_engine
from baluarte.auth.roles import Role
from baluarte.db.models import AuditEvent, CollectionJob, EvaluationRun, PolicyAssignment
from baluarte.main import create_app
from baluarte.policy.evaluation.runner import EVALUATE

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)


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


@pytest.fixture
def world(migrated_engine: Engine) -> World:
    world = World(migrated_engine)
    world.assign()
    return world


def get(engine: Engine, url: str, role: Role = Role.AUDITOR) -> Any:
    response = client_as(engine, role).get(url)
    assert response.status_code == 200, response.text
    return response.json()


def test_findings_list_with_their_latest_evaluation(world: World) -> None:
    world.project(1, "public")
    world.project(2, "private")
    world.run()

    body = get(world.engine, "/api/v1/findings")

    (item,) = body["items"]
    assert body["next_after_id"] is None
    assert item["project"] == {"gitlab_id": 1, "path": "platform/p1", "state": "active"}
    assert item["policy"]["name"] == "default-gitlab-baseline"
    assert item["policy"]["version"] == 1
    assert (item["rule_id"], item["severity"], item["state"]) == (
        "project-visibility",
        "medium",
        "open",
    )
    assert (item["latest_outcome"], item["resolution"], item["resolved_at"]) == (
        "fail",
        None,
        None,
    )
    evaluation = item["latest_evaluation"]
    assert evaluation["outcome"] == "fail"
    assert evaluation["expected"] == {"allowed": ["private", "internal"]}
    assert evaluation["observed"] == "public"
    assert evaluation["observation_id"] is not None
    assert evaluation["capability"] == {"name": None, "status": "not_required"}
    assert evaluation["collection"]["status"] == "fresh"
    assert evaluation["rule_type"] == "gitlab.project.visibility"


def test_findings_filters(world: World) -> None:
    first = world.project(1, "public")
    world.project(2, "public")
    world.run()
    world.observe(first, "private")
    world.run()

    def ids(query: str) -> list[int]:
        body = get(world.engine, f"/api/v1/findings?{query}")
        return [item["project"]["gitlab_id"] for item in body["items"]]

    assert ids("state=open") == [2]
    assert ids("state=resolved") == [1]
    assert ids("outcome=pass") == [1]
    assert ids("project=2") == [2]
    assert ids("policy=default-gitlab-baseline&rule=project-visibility") == [1, 2]
    assert ids("policy=other") == []
    assert ids("rule=other") == []
    assert ids("severity=medium") == [1, 2]
    assert ids("severity=high") == []


def test_findings_pages(world: World) -> None:
    for gitlab_id in (1, 2, 3):
        world.project(gitlab_id, "public")
    world.run()

    first = get(world.engine, "/api/v1/findings?limit=2")
    rest = get(world.engine, f"/api/v1/findings?limit=2&after_id={first['next_after_id']}")

    assert [i["project"]["gitlab_id"] for i in first["items"]] == [1, 2]
    assert [i["project"]["gitlab_id"] for i in rest["items"]] == [3]
    assert rest["next_after_id"] is None


def test_invalid_filters_are_rejected(world: World) -> None:
    client = client_as(world.engine, Role.ADMIN)

    assert client.get("/api/v1/findings?state=closed").status_code == 422
    assert client.get("/api/v1/findings?outcome=compliant").status_code == 422
    assert client.get("/api/v1/findings?limit=0").status_code == 422


def test_a_finding_and_its_history(world: World) -> None:
    project_id = world.project(1, "public")
    world.run()
    world.observe(project_id, "private")
    world.run()
    world.assign(NEXT_VERSION)
    world.run()
    finding_id = get(world.engine, "/api/v1/findings")["items"][0]["id"]

    finding = get(world.engine, f"/api/v1/findings/{finding_id}")
    history = get(world.engine, f"/api/v1/findings/{finding_id}/evaluations?limit=2")
    older = get(
        world.engine,
        f"/api/v1/findings/{finding_id}/evaluations?before_id={history['next_before_id']}",
    )

    # Version 2 allows only private, so the private project passes and the finding stays resolved.
    assert (finding["state"], finding["policy"]["version"]) == ("resolved", 2)
    assert [e["outcome"] for e in history["items"]] == ["pass", "pass"]
    assert [e["policy"]["version"] for e in history["items"]] == [2, 1]
    assert [e["outcome"] for e in older["items"]] == ["fail"]
    assert older["next_before_id"] is None


def test_an_unknown_finding_is_404(world: World) -> None:
    client = client_as(world.engine, Role.ADMIN)

    assert client.get("/api/v1/findings/999").status_code == 404
    assert client.get("/api/v1/findings/999/evaluations").status_code == 404


def test_a_project_shows_only_rules_from_its_latest_evaluation(world: World) -> None:
    world.project(1, "public")
    world.run()
    with Session(world.engine) as db:
        db.execute(delete(PolicyAssignment))
        db.commit()
    world.run()

    body = get(world.engine, "/api/v1/gitlab/projects/1/evaluations")

    # The policy no longer applies, so its old failure is not presented as current.
    assert body == {
        "project": {"gitlab_id": 1, "path": "platform/p1", "state": "active"},
        "items": [],
    }


def test_a_project_lists_its_latest_results(world: World) -> None:
    project_id = world.project(1, "public")
    world.run()
    world.observe(project_id, "private")
    world.run()

    body = get(world.engine, "/api/v1/gitlab/projects/1/evaluations")

    assert [(e["rule_id"], e["outcome"]) for e in body["items"]] == [("project-visibility", "pass")]
    assert (
        client_as(world.engine, Role.ADMIN)
        .get("/api/v1/gitlab/projects/404/evaluations")
        .status_code
        == 404
    )


def test_freshness_follows_the_evaluation_runs(world: World) -> None:
    assert get(world.engine, "/api/v1/evaluations/freshness")["status"] == "never"

    world.project(1, "public")
    world.run()
    fresh = get(world.engine, "/api/v1/evaluations/freshness")

    assert fresh["status"] == "fresh"
    assert fresh["last_run"]["status"] == "succeeded"
    assert fresh["last_run"]["counts"] == {"outcomes": {"fail": 1}, "findings": {"opened": 1}}
    assert fresh["last_success_at"] is not None
    assert (fresh["running"], fresh["queued"], fresh["behind_collection"]) == (
        False,
        False,
        False,
    )
    assert fresh["stale_after_minutes"] == 720

    with Session(world.engine) as db:
        db.execute(update(EvaluationRun).values(started_at=datetime.now(UTC) - timedelta(hours=13)))
        db.commit()
    assert get(world.engine, "/api/v1/evaluations/freshness")["status"] == "stale"


def test_a_failed_run_is_not_hidden_by_an_older_success(world: World) -> None:
    world.run()
    with Session(world.engine) as db:
        db.add(
            EvaluationRun(
                instance_id=world.instance_id,
                status="failed",
                finished_at=datetime.now(UTC),
                error="unexpected error: RuntimeError",
            )
        )
        db.commit()

    body = get(world.engine, "/api/v1/evaluations/freshness")

    assert body["status"] == "failed"
    assert body["last_run"]["error"] == "unexpected error: RuntimeError"
    assert body["last_success_at"] is not None


def test_freshness_says_when_results_predate_the_inventory(world: World) -> None:
    world.run()
    world.collection("succeeded", timedelta(0))

    assert get(world.engine, "/api/v1/evaluations/freshness")["behind_collection"] is True


def test_freshness_without_gitlab(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BALUARTE_GITLAB_URL")

    assert get(world.engine, "/api/v1/evaluations/freshness")["status"] == "not_configured"
    assert get(world.engine, "/api/v1/findings")["items"] == []


def test_run_history(world: World) -> None:
    world.run()
    world.run()

    body = get(world.engine, "/api/v1/evaluations/runs?limit=1")

    assert len(body["items"]) == 1
    assert body["next_before_id"] is not None
    older = get(world.engine, f"/api/v1/evaluations/runs?before_id={body['next_before_id']}")
    assert len(older["items"]) == 1


def test_queueing_an_evaluation_is_audited_and_shared_within_a_minute(
    migrated_engine: Engine,
) -> None:
    client = client_as(migrated_engine, Role.ANALYST)

    first = client.post("/api/v1/evaluations/run")
    second = client.post("/api/v1/evaluations/run")

    assert first.status_code == 202
    assert first.json() == second.json()
    with Session(migrated_engine) as db:
        job = db.scalars(select(CollectionJob)).one()
        events = list(db.scalars(select(AuditEvent)))
    assert job.kind == EVALUATE
    assert [event.action for event in events] == ["policy.evaluation_requested"] * 2
    assert events[0].details == {"job_id": job.id}
    assert get(migrated_engine, "/api/v1/evaluations/freshness")["queued"] is True


def test_queueing_an_evaluation_needs_gitlab(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BALUARTE_GITLAB_URL")

    response = client_as(migrated_engine, Role.ADMIN).post("/api/v1/evaluations/run")

    assert response.status_code == 409


def test_an_assignment_change_queues_one_evaluation(world: World) -> None:
    client = client_as(world.engine, Role.ANALYST)
    world.project(1, "private")
    with Session(world.engine) as db:
        db.execute(delete(PolicyAssignment))
        db.commit()

    created = client.post(
        "/api/v1/policy-assignments",
        json={"policy_name": "default-gitlab-baseline", "version": 1, "target_type": "instance"},
    )
    removed = client.delete(f"/api/v1/policy-assignments/{created.json()['id']}")

    assert (created.status_code, removed.status_code) == (201, 204)
    with Session(world.engine) as db:
        jobs = list(db.scalars(select(CollectionJob)))
    # The second change finds the first evaluation still waiting, and shares it.
    assert [job.kind for job in jobs] == [EVALUATE]


@pytest.mark.parametrize(
    "url",
    [
        "/api/v1/findings",
        "/api/v1/findings/1",
        "/api/v1/findings/1/evaluations",
        "/api/v1/gitlab/projects/1/evaluations",
        "/api/v1/evaluations/freshness",
        "/api/v1/evaluations/runs",
    ],
)
def test_reading_needs_findings_read(migrated_engine: Engine, url: str) -> None:
    assert client_as(migrated_engine).get(url).status_code == 403


@pytest.mark.parametrize("role", [Role.ADMIN, Role.ANALYST, Role.AUDITOR])
def test_every_role_can_read_findings(migrated_engine: Engine, role: Role) -> None:
    assert client_as(migrated_engine, role).get("/api/v1/findings").status_code == 200


@pytest.mark.parametrize("roles", [(), (Role.AUDITOR,)])
def test_queueing_needs_policy_write(migrated_engine: Engine, roles: tuple[Role, ...]) -> None:
    response = client_as(migrated_engine, *roles).post("/api/v1/evaluations/run")

    assert response.status_code == 403
    with Session(migrated_engine) as db:
        assert list(db.scalars(select(CollectionJob))) == []


def test_routes_need_a_session(migrated_engine: Engine) -> None:
    app = create_app()
    app.dependency_overrides[database_engine] = lambda: migrated_engine
    app.dependency_overrides[auth_settings] = lambda: make_settings()
    client = TestClient(app)

    assert client.get("/api/v1/findings").status_code == 401
    assert client.post("/api/v1/evaluations/run").status_code == 401
