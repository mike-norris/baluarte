"""Monitoring scope: excluding projects, groups and personal namespaces, review, inclusion again,
and the effects on evaluation, collection, counts, inventory and exports."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from gitlab_fake import TOKEN, URL
from oidc_fake import make_settings
from sqlalchemy import Engine, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_evaluation_store import World

from baluarte.auth.dependencies import (
    Principal,
    auth_settings,
    current_principal,
    database_engine,
)
from baluarte.auth.roles import Role
from baluarte.db.models import AuditEvent, GitLabGroup, GitLabProject, ScopeExclusion
from baluarte.integrations.gitlab import security_policies
from baluarte.integrations.gitlab.project_reads import targets
from baluarte.main import create_app
from baluarte.scope.exclusions import MAX_TARGETS, coverage, excluded_projects, exclusion_for

CSRF = {"X-CSRF-Token": "csrf"}
EXCLUSIONS = "/api/v1/scope/exclusions"
WHY = "Research sandbox, not part of the delivered product."
AGAIN = "The sandbox now ships code, so it is monitored again."


@pytest.fixture(autouse=True)
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)


@pytest.fixture
def world(migrated_engine: Engine) -> World:
    world = World(migrated_engine)
    world.assign()
    with Session(migrated_engine) as db:
        # platform (10) > platform/research (11) > platform/research/labs (12)
        db.add(
            GitLabGroup(
                instance_id=world.instance_id,
                gitlab_id=11,
                parent_gitlab_id=10,
                full_path="platform/research",
                name="research",
            )
        )
        db.add(
            GitLabGroup(
                instance_id=world.instance_id,
                gitlab_id=12,
                parent_gitlab_id=11,
                full_path="platform/research/labs",
                name="labs",
            )
        )
        db.commit()
    return world


def personal(world: World, gitlab_id: int, visibility: str = "public") -> int:
    project_id = world.project(gitlab_id, visibility, namespace=500)
    with Session(world.engine) as db:
        db.execute(
            update(GitLabProject)
            .where(GitLabProject.id == project_id)
            .values(namespace_kind="user", path_with_namespace=f"someone/p{gitlab_id}")
        )
        db.commit()
    return project_id


def client(engine: Engine, user_id: int, *roles: Role) -> TestClient:
    app = create_app()
    app.dependency_overrides[database_engine] = lambda: engine
    app.dependency_overrides[current_principal] = lambda: Principal(
        user_id=user_id,
        subject=f"user-{user_id}",
        email=None,
        display_name=None,
        roles=frozenset(roles),
        session_id=user_id,
        session_expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        csrf_token="csrf",
    )
    app.dependency_overrides[auth_settings] = lambda: make_settings()
    return TestClient(app, headers=CSRF)


def analyst(engine: Engine, user_id: int = 1) -> TestClient:
    return client(engine, user_id, Role.ANALYST)


def auditor(engine: Engine) -> TestClient:
    return client(engine, 7, Role.AUDITOR)


def require_review(engine: Engine) -> None:
    response = client(engine, 9, Role.ADMIN).put(
        "/api/v1/settings/governance", json={"require_review": True}
    )
    assert response.status_code == 200


def exclude(
    engine: Engine,
    *targets_: tuple[str, int],
    personal_namespaces: bool = False,
    user_id: int = 1,
) -> Any:
    return analyst(engine, user_id).post(
        EXCLUSIONS,
        json={
            "targets": [{"type": kind, "gitlab_id": gitlab_id} for kind, gitlab_id in targets_],
            "personal_namespaces": personal_namespaces,
            "justification": WHY,
        },
    )


def end(engine: Engine, *ids: int, user_id: int = 1) -> Any:
    return analyst(engine, user_id).post(
        f"{EXCLUSIONS}/end", json={"ids": list(ids), "justification": AGAIN}
    )


def codes(response: Any) -> list[str]:
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == detail["reasons"][0]["code"]
    return [reason["code"] for reason in detail["reasons"]]


def actions(engine: Engine) -> list[str]:
    with Session(engine) as db:
        return list(
            db.scalars(
                select(AuditEvent.action)
                .where(AuditEvent.action.like("scope.%"))
                .order_by(AuditEvent.id)
            )
        )


def listed(engine: Engine, kind: str = "projects", query: str = "") -> dict[str, Any]:
    response = auditor(engine).get(f"/api/v1/gitlab/{kind}{query}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# Coverage ------------------------------------------------------------------------------------


def test_a_group_covers_its_subgroups_and_their_projects_but_not_personal_ones(
    world: World,
) -> None:
    world.project(1, "public", namespace=10)
    world.project(2, "public", namespace=12)
    personal(world, 3)

    response = exclude(world.engine, ("group", 11))

    assert response.status_code == 201, response.text
    (row,) = response.json()["items"]
    with Session(world.engine) as db:
        scope = coverage(db, world.instance_id)
        assert scope.for_group(10) is None
        assert scope.for_group(11) == scope.for_group(12)
        excluded = excluded_projects(db, world.instance_id)
        projects = {
            p.gitlab_id: p for p in db.scalars(select(GitLabProject).order_by(GitLabProject.id))
        }
        covering = exclusion_for(db, projects[2])
        assert exclusion_for(db, projects[1]) is None
        assert exclusion_for(db, projects[3]) is None
    assert covering is not None
    assert (covering.exclusion_id, covering.via, covering.path) == (
        row["id"],
        "group",
        "platform/research",
    )
    assert list(excluded.values()) == [covering]


def test_the_project_itself_wins_over_its_group_and_personal_namespaces_cover_later_ones(
    world: World,
) -> None:
    world.project(2, "public", namespace=12)
    exclude(world.engine, ("group", 11))
    exclude(world.engine, ("project", 2), personal_namespaces=True)
    # Discovered after the exclusion was made.
    personal(world, 3)

    with Session(world.engine) as db:
        projects = {p.gitlab_id: p for p in db.scalars(select(GitLabProject))}
        direct = exclusion_for(db, projects[2])
        later = exclusion_for(db, projects[3])
    assert direct is not None and (direct.via, direct.path) == ("project", "platform/p2")
    assert later is not None and (later.via, later.path) == ("personal_namespaces", None)


# Changes and review --------------------------------------------------------------------------


def test_without_review_an_exclusion_applies_at_once_and_is_audited(world: World) -> None:
    world.project(1, "public")
    world.project(2, "public")

    response = exclude(world.engine, ("project", 1), ("group", 11))

    assert response.status_code == 201, response.text
    body = response.json()
    assert [(i["target_type"], i["target_id"], i["target_path"]) for i in body["items"]] == [
        ("project", 1, "platform/p1"),
        ("group", 11, "platform/research"),
    ]
    item = body["items"][0]
    assert item["batch_id"] == body["batch_id"]
    assert (item["state"], item["self_approved"], item["review_required"]) == (
        "active",
        True,
        False,
    )
    assert item["proposed_by"] == {"actor": "user:1", "user_id": 1, "display_name": None}
    assert item["decided_by"]["user_id"] == 1
    assert item["justification"] == WHY
    assert actions(world.engine) == ["scope.exclusion_proposed"]
    with Session(world.engine) as db:
        event = db.scalars(select(AuditEvent).where(AuditEvent.action.like("scope.%"))).one()
    assert event.resource_id == body["batch_id"]
    assert event.details["justification_sha256"] == hashlib.sha256(WHY.encode()).hexdigest()
    assert [t["target_path"] for t in event.details["targets"]] == [
        "platform/p1",
        "platform/research",
    ]
    listing = auditor(world.engine).get(f"{EXCLUSIONS}?state=active").json()
    assert listing["review_required"] is False
    assert [i["id"] for i in listing["items"]] == [i["id"] for i in reversed(body["items"])]


def test_with_review_a_batch_waits_for_someone_else(world: World) -> None:
    world.project(1, "public")
    require_review(world.engine)

    body = exclude(world.engine, ("project", 1)).json()
    batch = body["batch_id"]

    assert body["items"][0]["state"] == "proposed"
    with Session(world.engine) as db:
        assert excluded_projects(db, world.instance_id) == {}
    own = analyst(world.engine).post(f"{EXCLUSIONS}/batches/{batch}/approve", json={})
    assert own.status_code == 403
    assert own.json()["detail"]["code"] == "own_change"
    approved = analyst(world.engine, 2).post(
        f"{EXCLUSIONS}/batches/{batch}/approve", json={"note": "Agreed."}
    )
    assert approved.status_code == 200, approved.text
    item = approved.json()["items"][0]
    assert (item["state"], item["self_approved"], item["decision_note"]) == (
        "active",
        False,
        "Agreed.",
    )
    assert item["decided_by"]["user_id"] == 2
    again = analyst(world.engine, 2).post(f"{EXCLUSIONS}/batches/{batch}/approve", json={})
    assert codes(again) == ["not_proposed"]
    assert actions(world.engine) == ["scope.exclusion_proposed", "scope.exclusion_approved"]
    listing = auditor(world.engine).get(EXCLUSIONS).json()
    assert listing["review_required"] is True


def test_rejecting_a_batch_needs_a_note_and_excludes_nothing(world: World) -> None:
    world.project(1, "public")
    require_review(world.engine)
    batch = exclude(world.engine, ("project", 1)).json()["batch_id"]
    path = f"{EXCLUSIONS}/batches/{batch}/reject"

    assert analyst(world.engine, 2).post(path, json={}).status_code == 422
    response = analyst(world.engine, 2).post(path, json={"note": "Still in production use."})

    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["state"] == "rejected"
    with Session(world.engine) as db:
        assert excluded_projects(db, world.instance_id) == {}
    # A rejected target can be proposed again.
    assert exclude(world.engine, ("project", 1)).status_code == 201
    assert (
        analyst(world.engine, 2).post(f"{EXCLUSIONS}/batches/unknown/approve", json={}).status_code
        == 404
    )


def test_conflicts_are_refused_with_reason_codes(world: World) -> None:
    world.project(1, "public")
    exclude(world.engine, ("project", 1), personal_namespaces=True)

    assert codes(exclude(world.engine)) == ["no_targets"]
    assert codes(exclude(world.engine, ("project", 1), ("project", 1))) == ["duplicate_target"]
    assert codes(
        exclude(world.engine, ("project", 1), ("group", 99), personal_namespaces=True)
    ) == ["unknown_target", "already_excluded"]
    response = analyst(world.engine).post(
        EXCLUSIONS,
        json={
            "targets": [{"type": "project", "gitlab_id": n} for n in range(1, MAX_TARGETS + 2)],
            "justification": WHY,
        },
    )
    assert response.status_code == 422
    short = analyst(world.engine).post(
        EXCLUSIONS, json={"targets": [{"type": "group", "gitlab_id": 11}], "justification": "x"}
    )
    assert short.status_code == 422


def test_ending_brings_projects_back_without_a_second_person(world: World) -> None:
    world.project(1, "public")
    require_review(world.engine)
    batch = exclude(world.engine, ("project", 1)).json()["batch_id"]
    (row,) = (
        analyst(world.engine, 2)
        .post(f"{EXCLUSIONS}/batches/{batch}/approve", json={})
        .json()["items"]
    )

    response = end(world.engine, row["id"], user_id=2)

    assert response.status_code == 200, response.text
    (ended,) = response.json()["items"]
    assert (ended["state"], ended["end_justification"]) == ("ended", AGAIN)
    assert ended["ended_by"]["user_id"] == 2
    with Session(world.engine) as db:
        assert excluded_projects(db, world.instance_id) == {}
    assert codes(end(world.engine, row["id"])) == ["not_active"]
    assert codes(end(world.engine, 999_999)) == ["exclusion_unknown"]
    assert actions(world.engine)[-1] == "scope.exclusion_ended"
    # Ended, the target can be excluded again.
    assert exclude(world.engine, ("project", 1)).status_code == 201


def test_reading_needs_inventory_read_and_changing_needs_policy_write(world: World) -> None:
    world.project(1, "public")

    assert auditor(world.engine).get(EXCLUSIONS).status_code == 200
    refused = auditor(world.engine).post(
        EXCLUSIONS, json={"targets": [{"type": "project", "gitlab_id": 1}], "justification": WHY}
    )
    assert refused.status_code == 403
    assert (
        auditor(world.engine)
        .post(f"{EXCLUSIONS}/end", json={"ids": [1], "justification": AGAIN})
        .status_code
        == 403
    )


def test_the_trigger_keeps_exclusions_append_only(world: World) -> None:
    world.project(1, "public")
    (row,) = exclude(world.engine, ("project", 1)).json()["items"]

    def refused(statement: str) -> None:
        with world.engine.begin() as connection, pytest.raises(DBAPIError):
            connection.execute(text(statement), {"id": row["id"]})

    refused("DELETE FROM scope_exclusion WHERE id = :id")
    refused("UPDATE scope_exclusion SET justification = 'x' || justification WHERE id = :id")
    refused("UPDATE scope_exclusion SET state = 'proposed' WHERE id = :id")
    refused("UPDATE scope_exclusion SET target_id = 2 WHERE id = :id")
    # A second open exclusion of the same target is refused by the database as well.
    with Session(world.engine) as db:
        db.add(
            ScopeExclusion(
                instance_id=world.instance_id,
                target_type="project",
                target_id=1,
                target_path="platform/p1",
                batch_id="b",
                justification=WHY,
                state="active",
                review_required=False,
                self_approved=True,
                proposed_by="user:1",
                proposed_by_user_id=1,
                decided_by="user:1",
                decided_by_user_id=1,
                decided_at=datetime.now(UTC),
            )
        )
        with pytest.raises(DBAPIError):
            db.commit()


# Effects -------------------------------------------------------------------------------------


def test_an_excluded_project_is_not_evaluated_and_its_findings_resolve_then_reopen(
    world: World,
) -> None:
    excluded = world.project(1, "public")
    world.project(2, "public")
    world.run()
    assert [f.state for f in world.findings()] == ["open", "open"]
    (row,) = exclude(world.engine, ("project", 1)).json()["items"]

    run = world.run()

    first, second = world.findings()
    assert (first.state, first.resolution) == ("resolved", "project_excluded")
    assert second.state == "open"
    assert run.projects == 1
    assert run.counts["findings"] == {"project_excluded": 1}
    assert {e.project_id for e in world.evaluations() if e.evaluation_run_id == run.id} == {
        excluded + 1
    }

    end(world.engine, row["id"])
    world.run()

    first, _ = world.findings()
    assert (first.state, first.resolution) == ("open", None)


def test_collection_skips_excluded_projects_and_groups(world: World) -> None:
    world.project(1, "public")
    world.project(2, "public", namespace=12)
    personal(world, 3)
    exclude(world.engine, ("group", 11), personal_namespaces=True)

    read = [t.gitlab_id for t in targets(world.engine, world.instance_id)]
    policy_targets = security_policies._targets(world.engine, world.instance_id)

    assert read == [1]
    assert sorted(t.path for t in policy_targets) == ["platform", "platform/p1"]


def test_lists_hide_excluded_items_by_default_and_count_them(world: World) -> None:
    world.project(1, "public")
    world.project(2, "public", namespace=12)
    personal(world, 3)
    group_row = exclude(world.engine, ("group", 11)).json()["items"][0]

    included = listed(world.engine)
    everything = listed(world.engine, query="?monitoring=all")
    only = listed(world.engine, query="?monitoring=excluded")
    narrowed = listed(world.engine, query="?monitoring=included&namespace_kind=user")

    assert [p["gitlab_id"] for p in included["items"]] == [1, 3]
    assert included["excluded_hidden"] == 1
    assert included["items"][0]["monitoring"] == {
        "excluded": False,
        "exclusion_id": None,
        "via": None,
        "path": None,
    }
    assert [p["gitlab_id"] for p in everything["items"]] == [1, 2, 3]
    assert everything["excluded_hidden"] == 0
    assert [p["gitlab_id"] for p in only["items"]] == [2]
    assert only["items"][0]["monitoring"] == {
        "excluded": True,
        "exclusion_id": group_row["id"],
        "via": "group",
        "path": "platform/research",
    }
    assert narrowed["excluded_hidden"] == 0
    groups = listed(world.engine, "groups")
    assert [g["gitlab_id"] for g in groups["items"]] == [10]
    assert groups["excluded_hidden"] == 2
    labs = listed(world.engine, "groups", "?monitoring=excluded")["items"][1]
    assert labs["monitoring"]["via"] == "group"
    assert labs["monitoring"]["path"] == "platform/research"


def test_details_still_show_excluded_items(world: World) -> None:
    world.project(2, "public", namespace=12)
    exclude(world.engine, ("group", 11))

    project = auditor(world.engine).get("/api/v1/gitlab/projects/2").json()
    group = auditor(world.engine).get("/api/v1/gitlab/groups/12").json()

    assert project["project"]["monitoring"]["excluded"] is True
    assert project["not_evaluated"] == "excluded"
    assert group["group"]["monitoring"]["via"] == "group"


def test_counts_and_findings_leave_excluded_projects_out(world: World) -> None:
    world.project(1, "public")
    world.project(2, "public")
    world.run()
    exclude(world.engine, ("project", 1))

    # Before the next run, the latest run's results for the excluded project are not counted.
    summary = auditor(world.engine).get("/api/v1/evaluations/summary").json()
    findings = auditor(world.engine).get("/api/v1/findings").json()
    excluded = auditor(world.engine).get("/api/v1/findings?monitoring=excluded").json()
    every = auditor(world.engine).get("/api/v1/findings?monitoring=all").json()
    controls = auditor(world.engine).get("/api/v1/controls").json()["controls"]

    (baseline,) = summary["baselines"]
    assert baseline["projects"]["fail"] == 1
    assert [f["project"]["gitlab_id"] for f in findings["items"]] == [2]
    assert [f["project"]["gitlab_id"] for f in excluded["items"]] == [1]
    assert len(every["items"]) == 2
    access = next(c for c in controls if c["framework"] == "soc2" and c["id"] == "CC6.6")
    assert access["counts"]["projects"] == 1


def test_exports_flag_excluded_projects_and_record_the_scope(world: World) -> None:
    world.project(1, "public")
    world.project(2, "public")
    world.run()
    (row,) = exclude(world.engine, ("project", 1)).json()["items"]

    response = auditor(world.engine).post(
        "/api/v1/evidence/exports", json={"format": "json", "scope": {"all": True}}
    )

    assert response.status_code == 200, response.text
    document = response.json()
    inventory = {p["gitlab_id"]: p for p in document["inventory"]}
    assert (inventory[1]["monitoring_excluded"], inventory[1]["exclusion_id"]) == (
        True,
        row["id"],
    )
    assert inventory[1]["excluded_via"] == "project"
    assert inventory[2]["monitoring_excluded"] is False
    assert {e["project_gitlab_id"] for e in document["evaluations"]} == {2}
    assert {f["project_gitlab_id"] for f in document["findings"]} == {2}
    scope = document["manifest"]["monitoring_scope"]
    assert scope["excluded_projects"] == 1
    assert scope["exclusions"] == [
        {
            "id": row["id"],
            "target_type": "project",
            "target_id": 1,
            "target_path": "platform/p1",
            "since": scope["exclusions"][0]["since"],
            "justification_sha256": hashlib.sha256(WHY.encode()).hexdigest(),
        }
    ]
    assert scope["exclusions"][0]["since"] is not None
