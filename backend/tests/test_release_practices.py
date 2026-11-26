# ruff: noqa: F811 (pytest fixtures imported from test_policy_assignments)
"""Release practices: presets, where they apply, and what releases show (RG.2)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session
from test_policy_assignments import client_as, gitlab_env, instance_id  # noqa: F401

from baluarte.auth.roles import Role
from baluarte.db.models import AuditEvent, ChangeRecord, GitLabProject, Observation
from baluarte.evidence.observations import payload_digest
from baluarte.releases.practices import (
    Deployment,
    PracticeInvalid,
    PracticeSettings,
    observe,
    validated,
)

NOW = datetime(2026, 11, 20, tzinfo=UTC)


def deployed(
    ref: str | None, tag: bool | None, days: int = 1, env: str = "production"
) -> Deployment:
    return Deployment(environment=env, ref=ref, tag=tag, finished_at=NOW - timedelta(days=days))


# --- presets and parameters ---


def test_presets_give_defaults_and_keep_where_production_comes_from() -> None:
    tag = validated("release_tag", None)
    assert (tag.production_from, tag.ref_patterns, tag.required_approvals) == (
        "tag",
        ["*-release"],
        1,
    )
    assert not tag.creator_may_approve and not tag.deployer_may_approve
    assert tag.require_merge_request_review

    patterns = validated("release_tag", {"ref_patterns": ["release-*", "*-release", "*-release"]})
    assert patterns.ref_patterns == ["*-release", "release-*"]
    assert validated("merge_request", {}).production_from == "default_branch"
    assert validated("custom", {"production_from": "branch", "ref_patterns": ["hotfix/*"]})

    for preset, settings, message in [
        ("release_tag", {"production_from": "default_branch"}, "use a custom practice"),
        ("custom", {}, "needs production_from"),
        ("merge_request", {"ref_patterns": ["x"]}, "not the default branch"),
        ("release_tag", {"ref_patterns": []}, "at least one ref pattern"),
        ("release_tag", {"required_approvals": 11}, "less than or equal to 10"),
        ("release_tag", {"surprise": True}, "Extra inputs"),
        ("nightly", {}, "unknown preset"),
    ]:
        with pytest.raises(PracticeInvalid, match=message):
            validated(preset, settings)


# --- what releases show ---


def test_releases_follow_differ_or_are_unknown_against_the_practice() -> None:
    tag = validated("release_tag", {"ref_patterns": ["*-release", "202*"]})

    status, seen = observe(tag, [deployed("v2-release", True), deployed("2026.1", True, 3)], "main")
    assert status == "follows"
    assert (seen.deployments, seen.expected, seen.other, seen.by_kind) == (2, 2, 0, {"tag": 2})
    assert seen.last_deployed_at == NOW - timedelta(days=1)

    mixed = [deployed("main", False), deployed("v1-beta", True, 2), deployed("v2-release", True)]
    status, seen = observe(tag, mixed, "main")
    assert status == "differs"
    assert seen.other == 2 and seen.other_refs == ["main", "v1-beta"]
    assert seen.by_kind == {"default_branch": 1, "tag": 2}

    status, seen = observe(tag, [deployed("v2-release", True), deployed("old", None)], "main")
    assert (status, seen.unknown) == ("unknown", 1)

    # Only the practice's environments count.
    assert observe(tag, [deployed("main", False, env="staging")], "main")[0] == "no_releases"
    assert observe(None, [deployed("main", False)], "main")[0] == "unassigned"


def test_merge_request_and_release_branch_practices() -> None:
    merge = validated("merge_request", None)
    assert observe(merge, [deployed("main", False)], "main")[0] == "follows"
    assert observe(merge, [deployed("v1-release", True)], "main")[0] == "differs"

    branch = validated("release_branch", {"ref_patterns": ["release/*", "main"]})
    assert observe(branch, [deployed("release/1.2", False)], "main")[0] == "follows"
    # A branch pattern may name the default branch.
    assert observe(branch, [deployed("main", False)], "main")[0] == "follows"
    assert observe(branch, [deployed("feature/x", False)], "main")[0] == "differs"
    assert observe(branch, [deployed("release/1.2", True)], "main")[0] == "differs"


# --- the API ---


def create(client: TestClient, name: str, preset: str, **extra: Any) -> Any:
    return client.post("/api/v1/release-practices", json={"name": name, "preset": preset, **extra})


def assign(
    client: TestClient, connection: int, name: str, target_type: str, target_id: int | None = None
) -> Any:
    body: dict[str, Any] = {"connection_id": connection, "name": name, "target_type": target_type}
    if target_id is not None:
        body["target_id"] = target_id
    return client.post("/api/v1/release-practice-assignments", json=body)


def actions(engine: Engine) -> list[str]:
    with Session(engine) as db:
        return [
            e.action
            for e in db.scalars(select(AuditEvent).order_by(AuditEvent.id))
            if e.action.startswith("release_practice.")
        ]


def record_deployment(
    engine: Engine, gitlab_id: int, deployment_id: int, ref: str, tag: bool | None
) -> None:
    with Session(engine) as db:
        project = db.scalars(
            select(GitLabProject).where(GitLabProject.gitlab_id == gitlab_id)
        ).one()
        payload = {
            "kind": "deployment",
            "id": deployment_id,
            "environment": "production",
            "ref": ref,
            "sha": "a" * 40,
            "tag": tag,
        }
        observation = Observation(
            instance_id=project.instance_id,
            project_id=project.id,
            source="GET production deployment",
            source_ref=f"/projects/{gitlab_id}/deployments/{deployment_id}",
            collected_at=func.now(),
            collection_status="ok",
            payload=payload,
            payload_digest=payload_digest(payload),
        )
        db.add(observation)
        db.flush()
        db.add(
            ChangeRecord(
                instance_id=project.instance_id,
                project_id=project.id,
                kind="deployment",
                deployment_id=deployment_id,
                sha="a" * 40,
                shas=["a" * 40],
                changed_at=func.now() - timedelta(days=1),
                observation_id=observation.id,
                outcome="unknown",
                reasons=["seeded"],
                complete=True,
            )
        )
        db.commit()


@pytest.mark.db
def test_practices_are_created_changed_and_deleted_with_an_audit_trail(
    migrated_engine: Engine, instance_id: int
) -> None:
    client = client_as(migrated_engine, Role.ANALYST)

    created = create(client, "tag-releases", "release_tag", description="Releases from tags")
    assert created.status_code == 201, created.text
    assert created.json()["settings"]["ref_patterns"] == ["*-release"]
    assert created.json()["preset_label"] == "Protected release tag with deployment approval"
    assert create(client, "tag-releases", "merge_request").status_code == 422
    assert create(client, "bad", "custom").status_code == 422

    listed = client.get("/api/v1/release-practices").json()
    assert [p["preset"] for p in listed["presets"]] == [
        "merge_request",
        "release_tag",
        "release_branch",
        "custom",
    ]
    assert listed["presets"][3]["settings"] is None
    assert [p["name"] for p in listed["practices"]] == ["tag-releases"]

    changed = client.put(
        "/api/v1/release-practices/tag-releases",
        json={"settings": {"ref_patterns": ["*-release", "20*-release"]}, "description": "Tags"},
    )
    assert changed.status_code == 200
    assert changed.json()["settings"]["ref_patterns"] == ["*-release", "20*-release"]
    assert (
        client.put(
            "/api/v1/release-practices/tag-releases",
            json={"settings": {"production_from": "branch", "ref_patterns": ["x"]}},
        ).status_code
        == 422
    )
    assert client.put("/api/v1/release-practices/none", json={}).status_code == 404

    assert client.delete("/api/v1/release-practices/tag-releases").status_code == 204
    assert actions(migrated_engine) == [
        "release_practice.created",
        "release_practice.updated",
        "release_practice.deleted",
    ]
    with Session(migrated_engine) as db:
        updated = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "release_practice.updated")
        ).one()
        assert updated.details["before"]["settings"]["ref_patterns"] == ["*-release"]


@pytest.mark.db
def test_reading_needs_findings_read_and_writing_needs_policy_write(
    migrated_engine: Engine, instance_id: int
) -> None:
    auditor = client_as(migrated_engine, Role.AUDITOR)
    assert auditor.get("/api/v1/release-practices").status_code == 200
    assert auditor.get("/api/v1/release-practices/projects").status_code == 200
    assert create(auditor, "x", "merge_request").status_code == 403
    assert assign(auditor, instance_id, "x", "instance").status_code == 403


@pytest.mark.db
def test_the_nearest_assignment_decides_and_releases_are_compared(
    migrated_engine: Engine, instance_id: int
) -> None:
    client = client_as(migrated_engine, Role.ANALYST)
    create(client, "merge-requests", "merge_request")
    create(client, "tag-releases", "release_tag")
    assert assign(client, instance_id, "merge-requests", "instance").status_code == 201
    group = assign(client, instance_id, "tag-releases", "group", 10)
    assert group.status_code == 201, group.text
    assert group.json()["target"]["path"] == "platform"
    assert assign(client, instance_id, "tag-releases", "group", 30).status_code == 409
    assert assign(client, instance_id, "nope", "group", 10).status_code == 404

    # platform/backend/api/svc deployed a release tag; platform/web deployed main.
    with Session(migrated_engine) as db:
        for project in db.scalars(
            select(GitLabProject).where(GitLabProject.gitlab_id.in_([100, 101]))
        ):
            project.default_branch = "main"
        db.commit()
    record_deployment(migrated_engine, 100, 1, "v1-release", True)
    record_deployment(migrated_engine, 101, 2, "main", False)
    record_deployment(migrated_engine, 103, 3, "main", False)

    body = client.get("/api/v1/release-practices/projects").json()
    by_path = {row["path"]: row for row in body["projects"]}
    svc = by_path["platform/backend/api/svc"]
    assert (svc["practice"], svc["decided_by"]["path"], svc["status"]) == (
        "tag-releases",
        "platform",
        "follows",
    )
    web = by_path["platform/web"]
    assert (web["practice"], web["status"], web["observed"]["other_refs"]) == (
        "tag-releases",
        "differs",
        ["main"],
    )
    # Its default branch is not recorded, so whether it deployed it is not known.
    tool = by_path["orphans/sub/tool"]
    assert (tool["practice"], tool["decided_by"]["target_type"], tool["status"]) == (
        "merge-requests",
        "instance",
        "unknown",
    )
    assert by_path["alice/dotfiles"]["status"] == "no_releases"
    assert "platform/old" not in by_path  # missing projects are left out
    assert body["counts"]["differs"] == 1 and body["window_days"] == 90

    # A project's own assignment is nearer than its group's.
    own = assign(client, instance_id, "merge-requests", "project", 101)
    assert own.status_code == 201
    web = next(
        row
        for row in client.get("/api/v1/release-practices/projects?status=follows").json()[
            "projects"
        ]
        if row["path"] == "platform/web"
    )
    assert (web["practice"], web["decided_by"]["target_type"]) == ("merge-requests", "project")

    # Assigned practices are not deleted; unassigning is audited.
    assert client.delete("/api/v1/release-practices/merge-requests").status_code == 409
    listed = client.get("/api/v1/release-practice-assignments").json()["assignments"]
    assert len(listed) == 3
    for row in listed:
        if row["name"] == "merge-requests":
            assert (
                client.delete(f"/api/v1/release-practice-assignments/{row['id']}").status_code
                == 204
            )
    assert client.delete("/api/v1/release-practices/merge-requests").status_code == 204
    assert actions(migrated_engine).count("release_practice.unassigned") == 2


@pytest.mark.db
def test_reassigning_a_target_replaces_its_practice(
    migrated_engine: Engine, instance_id: int
) -> None:
    client = client_as(migrated_engine, Role.ANALYST)
    create(client, "a", "merge_request")
    create(client, "b", "release_branch")
    assign(client, instance_id, "a", "group", 10)
    again = assign(client, instance_id, "b", "group", 10)
    assert again.status_code == 201
    assert [
        row["name"]
        for row in client.get("/api/v1/release-practice-assignments").json()["assignments"]
    ] == ["b"]
    with Session(migrated_engine) as db:
        event = db.scalars(
            select(AuditEvent)
            .where(AuditEvent.action == "release_practice.assigned")
            .order_by(AuditEvent.id.desc())
        ).first()
        assert event is not None and event.details["replaced"]["name"] == "a"


def test_settings_round_trip() -> None:
    settings = PracticeSettings(production_from="tag", ref_patterns=["b", "a"])
    assert PracticeSettings.model_validate(settings.model_dump()).ref_patterns == ["a", "b"]
