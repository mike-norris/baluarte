"""The auditable issue register: collection, selection rules, decisions (roadmap M12.1)."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from gitlab_fake import TOKEN, URL, FakeGitLab, paged, settings
from oidc_fake import make_settings
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from baluarte.auth.dependencies import Principal, auth_settings, current_principal, database_engine
from baluarte.auth.roles import Role
from baluarte.db.models import (
    AuditEvent,
    CollectionJob,
    CollectionRun,
    GitLabInstance,
    GitLabProject,
    Observation,
    RegisterCursor,
    RegisterDecision,
    RegisterIssue,
    RegisterRule,
    Workspace,
)
from baluarte.evidence.observations import verify
from baluarte.integrations.gitlab import register_issues
from baluarte.integrations.gitlab.client import GitLabClient
from baluarte.integrations.gitlab.jobs import COLLECT_ISSUES, make_collect_issues, queue_issues
from baluarte.jobs.worker import Worker
from baluarte.main import create_app
from baluarte.workspaces import default_workspace_id

pytestmark = pytest.mark.db

ISSUES = "/projects/42/issues"


def issue(iid: int, labels: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "id": 1000 + iid,
        "iid": iid,
        "issue_type": "issue",
        "title": f"Issue {iid}",
        "state": "opened",
        "labels": labels or [],
        "author": {"username": "ada"},
        "assignees": [{"username": "grace"}],
        "milestone": {"title": "Q4"},
        "web_url": f"{URL}/acme/api/-/issues/{iid}",
        "confidential": False,
        "created_at": "2026-10-01T10:00:00Z",
        "updated_at": f"2026-11-{iid:02d}T10:00:00Z",
        "closed_at": None,
        "description": "not kept in M12.1",
    } | extra


@pytest.fixture
def world(migrated_engine: Engine) -> dict[str, int]:
    with Session(migrated_engine) as db:
        connection = GitLabInstance(base_url=URL, workspace_id=default_workspace_id(db))
        db.add(connection)
        db.flush()
        project = GitLabProject(
            instance_id=connection.id,
            gitlab_id=42,
            namespace_gitlab_id=7,
            path_with_namespace="acme/api",
            name="api",
        )
        other = GitLabProject(
            instance_id=connection.id,
            gitlab_id=43,
            namespace_gitlab_id=7,
            path_with_namespace="acme/web",
            name="web",
        )
        db.add_all([project, other])
        db.commit()
        return {"connection": connection.id, "project": project.id, "other": other.id}


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


def collect(engine: Engine, world: dict[str, int], fake: FakeGitLab) -> Any:
    with GitLabClient(settings(), transport=fake.transport) as client:
        return register_issues.collect(client, engine, world["connection"])


def serve(fake: FakeGitLab, *issues: dict[str, Any]) -> None:
    fake.set(ISSUES, paged(ISSUES, [list(issues)]))
    fake.set("/projects/43/issues", paged("/projects/43/issues", [[]]))


def add_rule(engine: Engine, **body: Any) -> dict[str, Any]:
    response = client_as(engine, Role.AUDITOR).post(
        "/api/v1/register/rules", json={"name": "Security", "category": "engineering"} | body
    )
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    return result


def register(engine: Engine, **params: Any) -> dict[str, Any]:
    response = client_as(engine, Role.MANAGER).get("/api/v1/register/issues", params=params)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


# Collection


def test_nothing_is_read_without_a_register(migrated_engine: Engine, world: dict[str, int]) -> None:
    fake = FakeGitLab({})
    serve(fake, issue(1))

    result = collect(migrated_engine, world, fake)

    assert result.active is False
    assert fake.requests == []


def test_issues_are_read_with_provenance_and_then_incrementally(
    migrated_engine: Engine, world: dict[str, int]
) -> None:
    add_rule(migrated_engine, labels=["security"])
    fake = FakeGitLab({})
    serve(
        fake,
        issue(1, ["security"]),
        issue(2, ["docs"], state="closed", closed_at="2026-11-02T12:00:00Z"),
    )

    first = collect(migrated_engine, world, fake)

    assert (first.projects, first.issues, dict(first.reads)) == (2, 2, {"ok": 2})
    with Session(migrated_engine) as db:
        stored = db.get_one(RegisterIssue, (world["project"], 2))
        observation = db.get_one(Observation, stored.observation_id)
        cursor = db.get_one(RegisterCursor, world["project"])
    assert (stored.state, stored.labels, stored.author, stored.assignees) == (
        "closed",
        ["docs"],
        "ada",
        ["grace"],
    )
    assert stored.closed_at == datetime(2026, 11, 2, 12, tzinfo=UTC)
    assert verify(observation)
    assert observation.source == register_issues.SOURCE
    assert "description" not in observation.payload["issues"][0]
    assert cursor.read_through == datetime(2026, 11, 2, 10, tzinfo=UTC)
    first_params = fake.requests[0].url.params
    assert "updated_after" not in first_params and "state" not in first_params

    serve(fake, issue(1, ["security", "p1"], updated_at="2026-11-05T10:00:00Z"))
    collect(migrated_engine, world, fake)

    again = [r for r in fake.requests if r.url.path.endswith(ISSUES)][-1]
    assert again.url.params["updated_after"] == "2026-11-02T09:55:00Z"
    with Session(migrated_engine) as db:
        assert db.get_one(RegisterIssue, (world["project"], 1)).labels == ["p1", "security"]
        assert db.get_one(RegisterIssue, (world["project"], 2)).state == "closed"


def test_a_failed_read_keeps_what_was_stored(
    migrated_engine: Engine, world: dict[str, int]
) -> None:
    add_rule(migrated_engine, labels=["security"])
    fake = FakeGitLab({})
    serve(fake, issue(1, ["security"]))
    collect(migrated_engine, world, fake)

    fake.set(ISSUES, httpx.Response(403, json={"message": "403 Forbidden"}))
    result = collect(migrated_engine, world, fake)

    assert result.reads["forbidden"] == 1
    with Session(migrated_engine) as db:
        assert db.get(RegisterIssue, (world["project"], 1)) is not None
        latest = db.scalars(
            select(Observation)
            .where(Observation.source == register_issues.SOURCE)
            .order_by(Observation.id.desc())
        ).first()
    assert latest is not None and latest.collection_status in ("forbidden", "ok")


# Selection and decisions


@pytest.fixture
def collected(migrated_engine: Engine, world: dict[str, int]) -> dict[str, int]:
    add_rule(
        migrated_engine, name="Security issues", category="security_finding", labels=["security"]
    )
    fake = FakeGitLab({})
    serve(
        fake,
        issue(1, ["security"]),
        issue(2, ["docs"]),
        issue(3, ["security", "remediation"], issue_type="incident"),
        issue(
            4,
            [],
            state="closed",
            closed_at="2026-09-15T00:00:00Z",
            created_at="2026-09-01T00:00:00Z",
        ),
    )
    collect(migrated_engine, world, fake)
    return world


def test_rules_select_issues_and_say_why(
    migrated_engine: Engine, collected: dict[str, int]
) -> None:
    page = register(migrated_engine)

    assert page["active"] is True
    assert [item["iid"] for item in page["items"]] == [3, 1]
    first = page["items"][1]
    assert first["category"] == "security_finding"
    assert first["why"] == [
        {
            "kind": "rule",
            "rule_id": 1,
            "rule_name": "Security issues",
            "decision_id": None,
            "reason": None,
            "decided_by": None,
            "decided_at": None,
        }
    ]


def test_a_rule_meets_all_its_criteria_and_the_oldest_rule_sets_the_category(
    migrated_engine: Engine, collected: dict[str, int]
) -> None:
    add_rule(
        migrated_engine,
        name="Incidents in the API",
        category="remediation",
        issue_types=["incident"],
        project_ids=[collected["project"]],
    )

    page = register(migrated_engine)
    by_iid = {item["iid"]: item for item in page["items"]}

    assert by_iid[3]["category"] == "security_finding"
    assert [w["rule_name"] for w in by_iid[3]["why"]] == ["Security issues", "Incidents in the API"]
    assert [i["iid"] for i in register(migrated_engine, category="remediation")["items"]] == []
    assert [i["iid"] for i in register(migrated_engine, rule_id=2)["items"]] == [3]


def test_inclusion_and_exclusion_need_reasons_and_stay_visible(
    migrated_engine: Engine, collected: dict[str, int]
) -> None:
    auditor = client_as(migrated_engine, Role.AUDITOR)
    project = collected["project"]

    short = auditor.post(
        "/api/v1/register/decisions",
        json={"project_id": project, "iid": 2, "decision": "include", "reason": "because"},
    )
    included = auditor.post(
        "/api/v1/register/decisions",
        json={
            "project_id": project,
            "iid": 2,
            "decision": "include",
            "category": "exception",
            "reason": "Documents the accepted risk on access reviews",
        },
    )
    twice = auditor.post(
        "/api/v1/register/decisions",
        json={"project_id": project, "iid": 2, "decision": "exclude", "reason": "x" * 30},
    )
    excluded = auditor.post(
        "/api/v1/register/decisions",
        json={
            "project_id": project,
            "iid": 1,
            "decision": "exclude",
            "reason": "Duplicate of issue 3, tracked there",
        },
    )

    assert short.status_code == 422
    assert included.status_code == 201
    assert twice.status_code == 409
    assert excluded.status_code == 201
    page = register(migrated_engine)
    assert {i["iid"]: i["category"] for i in page["items"]} == {
        2: "exception",
        3: "security_finding",
    }
    with_excluded = {i["iid"]: i for i in register(migrated_engine, include_excluded=True)["items"]}
    assert with_excluded[1]["excluded"] is True
    assert with_excluded[1]["why"][-1]["kind"] == "excluded"
    assert with_excluded[1]["why"][-1]["reason"] == "Duplicate of issue 3, tracked there"

    detail = auditor.get(f"/api/v1/register/issues/{project}/1").json()
    assert detail["auditable"] is False
    ended = auditor.post(f"/api/v1/register/decisions/{excluded.json()['id']}/end")
    assert ended.json()["ended_by"] == "user:1"
    detail = auditor.get(f"/api/v1/register/issues/{project}/1").json()
    assert detail["auditable"] is True
    assert [d["decision"] for d in detail["decisions"]] == ["exclude"]

    with Session(migrated_engine) as db:
        actions = [
            e.action
            for e in db.scalars(
                select(AuditEvent)
                .where(AuditEvent.action.like("register.%"))
                .order_by(AuditEvent.id)
            )
        ]
    assert actions == [
        "register.rule_created",
        "register.issue_included",
        "register.issue_excluded",
        "register.decision_ended",
    ]


def test_an_included_issue_not_read_yet_is_pending_and_queues_a_read(
    migrated_engine: Engine, collected: dict[str, int]
) -> None:
    response = client_as(migrated_engine, Role.ANALYST).post(
        "/api/v1/register/decisions",
        json={
            "project_id": collected["other"],
            "iid": 9,
            "decision": "include",
            "reason": "Change approval evidence for the payments release",
        },
    )

    assert response.status_code == 201
    page = register(migrated_engine)
    assert [(p["project"], p["iid"]) for p in page["pending"]] == [("acme/web", 9)]
    with Session(migrated_engine) as db:
        kinds = list(db.scalars(select(CollectionJob.kind)))
    assert COLLECT_ISSUES in kinds


def test_filters_by_period_label_assignee_and_text(
    migrated_engine: Engine, collected: dict[str, int]
) -> None:
    add_rule(
        migrated_engine, name="Closed", category="engineering", project_ids=[collected["project"]]
    )

    def iids(**params: Any) -> list[int]:
        return sorted(i["iid"] for i in register(migrated_engine, **params)["items"])

    assert iids() == [1, 2, 3, 4]
    assert iids(period_start="2026-10-01T00:00:00Z") == [1, 2, 3]
    assert iids(period_end="2026-09-30T00:00:00Z") == [4]
    assert iids(label="remediation") == [3]
    assert iids(assignee="grace") == [1, 2, 3, 4]
    assert iids(q="issue 2") == [2]
    assert iids(state="closed") == [4]
    assert register(migrated_engine, limit=2)["total"] == 4


def test_ending_a_rule_takes_its_issues_out(
    migrated_engine: Engine, collected: dict[str, int]
) -> None:
    auditor = client_as(migrated_engine, Role.AUDITOR)

    ended = auditor.post("/api/v1/register/rules/1/end")

    assert ended.status_code == 200
    assert register(migrated_engine)["items"] == []
    rules = auditor.get("/api/v1/register/rules", params={"include_ended": True}).json()["rules"]
    assert rules[0]["ended_by"] == "user:1"
    assert auditor.get("/api/v1/register/rules").json()["rules"] == []


def test_rules_need_criteria_and_projects_of_the_workspace(
    migrated_engine: Engine, world: dict[str, int]
) -> None:
    auditor = client_as(migrated_engine, Role.AUDITOR)

    empty = auditor.post("/api/v1/register/rules", json={"name": "All", "category": "engineering"})
    unknown = auditor.post(
        "/api/v1/register/rules",
        json={"name": "Elsewhere", "category": "engineering", "project_ids": [9999]},
    )
    add_rule(migrated_engine, name="Docs", labels=["docs"])
    duplicate = auditor.post(
        "/api/v1/register/rules", json={"name": "docs", "category": "engineering", "labels": ["x"]}
    )

    assert empty.status_code == 422
    assert unknown.status_code == 422
    assert duplicate.status_code == 409


def test_managers_read_the_register_but_do_not_change_it(
    migrated_engine: Engine, world: dict[str, int]
) -> None:
    manager = client_as(migrated_engine, Role.MANAGER)

    assert manager.get("/api/v1/register/issues").status_code == 200
    assert (
        manager.post(
            "/api/v1/register/rules", json={"name": "x", "category": "engineering", "labels": ["a"]}
        ).status_code
        == 403
    )
    assert manager.post("/api/v1/register/collect").status_code == 403


def test_rules_and_decisions_are_ended_never_rewritten_or_deleted(
    migrated_engine: Engine, collected: dict[str, int]
) -> None:
    with Session(migrated_engine) as db, pytest.raises(DBAPIError, match="can only be ended"):
        db.execute(text("UPDATE register_rule SET labels = '{other}'"))
    with Session(migrated_engine) as db, pytest.raises(DBAPIError, match="cannot be deleted"):
        db.execute(text("DELETE FROM register_rule"))
    with Session(migrated_engine) as db:
        db.add(
            RegisterDecision(
                workspace_id=default_workspace_id(db),
                project_id=collected["project"],
                issue_iid=1,
                decision="exclude",
                reason="A reason long enough for the check",
                decided_by="user:1",
            )
        )
        db.commit()
    with Session(migrated_engine) as db, pytest.raises(DBAPIError, match="cannot be deleted"):
        db.execute(text("DELETE FROM register_decision"))


def test_another_workspace_s_register_is_separate(
    migrated_engine: Engine, collected: dict[str, int]
) -> None:
    with Session(migrated_engine) as db:
        acme = Workspace(slug="acme", name="Acme")
        db.add(acme)
        db.flush()
        db.add(
            RegisterRule(
                workspace_id=acme.id,
                name="Everything labelled docs",
                category="engineering",
                labels=["docs"],
                created_by="user:2",
            )
        )
        db.commit()

    # The default workspace's register is unchanged by acme's rule.
    assert [i["iid"] for i in register(migrated_engine)["items"]] == [3, 1]


# The collection job


@pytest.fixture
def gitlab_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)


def run_job(engine: Engine, fake: FakeGitLab, key: str) -> None:
    with Session(engine) as db:
        queue_issues(db, key)
        db.commit()
    assert Worker(engine, {COLLECT_ISSUES: make_collect_issues(fake.transport)}).run_once()


def test_the_job_skips_without_a_register_and_records_an_issues_run_with_one(
    migrated_engine: Engine, world: dict[str, int], gitlab_environment: None
) -> None:
    fake = FakeGitLab({})
    serve(fake, issue(1, ["security"]))

    run_job(migrated_engine, fake, "first")
    with Session(migrated_engine) as db:
        assert db.scalars(select(CollectionRun)).first() is None
    assert fake.requests == []

    add_rule(migrated_engine, labels=["security"])
    run_job(migrated_engine, fake, "second")

    with Session(migrated_engine) as db:
        run = db.scalars(select(CollectionRun).where(CollectionRun.kind == "issues")).one()
    assert (run.status, run.seen, run.error) == ("succeeded", 2, None)
    assert run.counts["issues"] == 1
