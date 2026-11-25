# ruff: noqa: F811 (pytest fixtures imported from other test modules)
"""Template change sets and saved selections (M14.3)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_evaluation_access import GOOD_SETTINGS, approval_rule
from test_evaluation_project import fresh, instance_id  # noqa: F401
from test_template_drift import (  # noqa: F401
    client,
    gitlab_env,
    observe_approvals,
    project_id,
    user,
    world,
)

from baluarte.auth.roles import Role
from baluarte.db.models import (
    AuditEvent,
    ConfigurationTemplate,
    GitLabInstance,
    GitLabProject,
    TemplateChangeSet,
)
from baluarte.policy.assignments import Target
from baluarte.templates.store import assign

pytestmark = pytest.mark.db

SITE = "platform/site"  # 101: public, unprotected, 3 approvals, authors may approve
API = "platform/api"  # 100: private, protected, exactly 2 approvals, authors may not approve
LEGACY = "platform/legacy"  # 102: visibility and approvals not collected

SETTINGS = {
    "visibility": "private",
    "default_branch_protected": True,
    "merge_requests": {"min_approvals": 2, "forbid_author_approval": True},
}


def create(engine: Engine, user_id: int, **body: Any) -> Any:
    payload = {"title": "Tighten platform", "selection": {"projects": [SITE, API, LEGACY]}}
    payload.update(body)
    if not {"template", "settings", "effective", "topics"} & payload.keys():
        payload["settings"] = SETTINGS
    return client(engine, user_id, Role.ANALYST).post("/api/v1/template-change-sets", json=payload)


def by_path(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {target["project"]["path"]: target for target in document["targets"]}


def test_a_change_set_shows_before_and_after_from_evidence(
    migrated_engine: Engine, world: int
) -> None:
    ana = user(migrated_engine, "ana")
    response = create(migrated_engine, ana)

    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["state"], body["targets"], body["changes"]) == ("proposed", 3, 4)
    targets = by_path(body["document"])
    site = {change["key"]: change for change in targets[SITE]["changes"]}
    assert {key: (c["before"], c["after"]) for key, c in site.items()} == {
        "visibility": ("public", "private"),
        "default_branch_protected": (False, True),
        "merge_requests.min_approvals": (3, 2),
        "merge_requests.forbid_author_approval": (False, True),
    }
    assert [w["code"] for w in site["visibility"]["warnings"]] == ["removes_access"]
    assert [w["code"] for w in site["merge_requests.min_approvals"]["warnings"]] == [
        "weakens_protection"
    ]
    assert [w["code"] for w in site["default_branch_protected"]["warnings"]] == ["disrupts_work"]
    assert site["visibility"]["observed_at"] is not None
    # Removing access and lowering approvals make the set sensitive.
    assert body["sensitive"] is True
    assert body["approvals_needed"] == 2

    assert targets[API]["changes"] == []
    assert sorted(targets[API]["unchanged"]) == sorted(
        [
            "visibility",
            "default_branch_protected",
            "merge_requests.min_approvals",
            "merge_requests.forbid_author_approval",
        ]
    )
    # Missing evidence is skipped as unknown, never guessed.
    legacy = {skip["key"]: skip["code"] for skip in targets[LEGACY]["skipped"]}
    assert legacy["visibility"] == "unknown"
    assert legacy["merge_requests.min_approvals"] == "unknown"

    with Session(migrated_engine) as db:
        actions = db.scalars(
            select(AuditEvent.action).where(AuditEvent.resource_type == "template_change_set")
        ).all()
    assert actions == ["template_change_set.created"]


def test_baseline_conflicts_and_unsupported_settings_are_skipped(
    migrated_engine: Engine, world: int
) -> None:
    ana = user(migrated_engine, "ana")
    response = create(
        migrated_engine,
        ana,
        selection={"projects": [API]},
        settings={
            "visibility": "public",
            "default_branch_protected": False,
            "security_features": ["secret_scanning"],
            "merge_requests": {"min_approvals": 1},
        },
    )

    assert response.status_code == 201, response.text
    target = response.json()["document"]["targets"][0]
    skipped = {skip["key"]: skip for skip in target["skipped"]}
    assert skipped["visibility"]["code"] == "conflicts_with_baseline"
    assert "allows only private, internal" in skipped["visibility"]["reason"]
    assert skipped["default_branch_protected"]["code"] == "conflicts_with_baseline"
    assert skipped["security_features"]["code"] == "unsupported"
    # Only lowering approvals remains, which weakens protection.
    assert [c["key"] for c in target["changes"]] == ["merge_requests.min_approvals"]
    assert response.json()["sensitive"] is True

    nothing = create(
        migrated_engine, ana, selection={"projects": [API]}, settings={"visibility": "public"}
    )
    assert nothing.status_code == 422
    assert "Nothing would change" in nothing.json()["detail"]


def test_requests_are_validated(migrated_engine: Engine, world: int) -> None:
    ana = user(migrated_engine, "ana")
    assert create(migrated_engine, ana, selection={}).status_code == 422
    assert create(migrated_engine, ana, template="service@1", settings=SETTINGS).status_code == 422
    assert create(migrated_engine, ana, effective=True, settings=SETTINGS).status_code == 422
    assert create(migrated_engine, ana, template="nope@1").status_code == 422
    bad = create(migrated_engine, ana, settings={"visibility": "secret"})
    assert bad.status_code == 422
    assert bad.json()["detail"]["errors"][0]["path"].startswith("settings")
    nobody = create(migrated_engine, ana, selection={"groups": ["elsewhere"]})
    assert nobody.status_code == 422
    auditor = client(migrated_engine, ana, Role.AUDITOR).post(
        "/api/v1/template-change-sets",
        json={"title": "x", "selection": {"projects": [SITE]}, "settings": SETTINGS},
    )
    assert auditor.status_code == 403


def test_a_sensitive_set_needs_two_other_approvers(migrated_engine: Engine, world: int) -> None:
    ana, bea, cy = (user(migrated_engine, name) for name in ("ana", "bea", "cy"))
    created = create(migrated_engine, ana).json()
    path = f"/api/v1/template-change-sets/{created['id']}"
    digest = created["digest"]

    own = client(migrated_engine, ana, Role.ADMIN).post(f"{path}/approve", json={"digest": digest})
    assert own.status_code == 403
    wrong = client(migrated_engine, bea, Role.ADMIN).post(
        f"{path}/approve", json={"digest": "0" * 64}
    )
    assert wrong.status_code == 409
    analyst = client(migrated_engine, bea, Role.ANALYST).post(
        f"{path}/approve", json={"digest": digest}
    )
    assert analyst.status_code == 403

    first = client(migrated_engine, bea, Role.ADMIN).post(
        f"{path}/approve", json={"digest": digest, "note": "Checked the site."}
    )
    assert first.status_code == 200, first.text
    assert (first.json()["state"], first.json()["approvals_needed"]) == ("proposed", 1)
    assert first.json()["first_approved_by"]["display_name"] == "Bea"

    again = client(migrated_engine, bea, Role.ADMIN).post(
        f"{path}/approve", json={"digest": digest}
    )
    assert again.status_code == 403
    second = client(migrated_engine, cy, Role.ADMIN).post(
        f"{path}/approve", json={"digest": digest}
    )
    assert second.status_code == 200, second.text
    assert (second.json()["state"], second.json()["approvals_needed"]) == ("approved", 0)

    listed = client(migrated_engine, ana, Role.AUDITOR).get(
        "/api/v1/template-change-sets", params={"state": "approved"}
    )
    assert [row["id"] for row in listed.json()["change_sets"]] == [created["id"]]
    with Session(migrated_engine) as db:
        actions = db.scalars(
            select(AuditEvent.action)
            .where(AuditEvent.resource_type == "template_change_set")
            .order_by(AuditEvent.id)
        ).all()
        assert actions == [
            "template_change_set.created",
            "template_change_set.first_approval",
            "template_change_set.approved",
        ]
        with pytest.raises(DBAPIError, match="cannot change"):
            db.execute(
                text("UPDATE template_change_set SET title = 'other' WHERE id = :id"),
                {"id": created["id"]},
            )
        db.rollback()
        with pytest.raises(DBAPIError, match="cannot be deleted"):
            db.execute(
                text("DELETE FROM template_change_set WHERE id = :id"), {"id": created["id"]}
            )
        db.rollback()


def test_approval_is_refused_when_the_evidence_moved(migrated_engine: Engine, world: int) -> None:
    ana, bea = user(migrated_engine, "ana"), user(migrated_engine, "bea")
    created = create(
        migrated_engine,
        ana,
        selection={"projects": [SITE]},
        settings={"merge_requests": {"forbid_author_approval": True}},
    ).json()
    assert created["sensitive"] is False
    assert created["approvals_needed"] == 1

    # Someone fixed it by hand after the set was built.
    observe_approvals(
        migrated_engine,
        world,
        101,
        {
            "branch": "main",
            "branch_protected": False,
            "settings": GOOD_SETTINGS,
            "rules": [approval_rule(3)],
        },
    )
    stale = client(migrated_engine, bea, Role.ADMIN).post(
        f"/api/v1/template-change-sets/{created['id']}/approve",
        json={"digest": created["digest"]},
    )
    assert stale.status_code == 409
    assert "today's evidence" in stale.json()["detail"]


def test_reject_and_withdraw(migrated_engine: Engine, world: int) -> None:
    ana, bea = user(migrated_engine, "ana"), user(migrated_engine, "bea")
    first = create(migrated_engine, ana).json()
    second = create(migrated_engine, ana).json()
    admin = client(migrated_engine, bea, Role.ADMIN)

    assert (
        admin.post(f"/api/v1/template-change-sets/{first['id']}/reject", json={}).status_code == 422
    )
    rejected = admin.post(
        f"/api/v1/template-change-sets/{first['id']}/reject", json={"note": "Too broad."}
    )
    assert rejected.json()["state"] == "rejected"
    assert (
        admin.post(
            f"/api/v1/template-change-sets/{first['id']}/approve", json={"digest": first["digest"]}
        ).status_code
        == 409
    )

    other = client(migrated_engine, bea, Role.ANALYST).post(
        f"/api/v1/template-change-sets/{second['id']}/withdraw"
    )
    assert other.status_code == 403
    withdrawn = client(migrated_engine, ana, Role.ANALYST).post(
        f"/api/v1/template-change-sets/{second['id']}/withdraw"
    )
    assert withdrawn.json()["state"] == "withdrawn"
    assert withdrawn.json()["ended_at"] is not None


def test_template_source_and_exceptions(migrated_engine: Engine, world: int) -> None:
    ana, bea = user(migrated_engine, "ana"), user(migrated_engine, "bea")
    expires = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    requested = client(migrated_engine, ana, Role.ANALYST).post(
        "/api/v1/template-exceptions",
        json={
            "project_id": project_id(migrated_engine, 101),
            "setting": "visibility",
            "justification": "A public documentation site.",
            "expires_at": expires,
        },
    )
    client(migrated_engine, bea, Role.ADMIN).post(
        f"/api/v1/template-exceptions/{requested.json()['id']}/approve", json={}
    )

    body = create(migrated_engine, ana, template="service@1", selection={"projects": [SITE]}).json()
    assert body["source"] == "service@1"
    target = body["document"]["targets"][0]
    skipped = {skip["key"]: skip["code"] for skip in target["skipped"]}
    assert skipped["visibility"] == "excepted"
    assert skipped["security_features"] == "unsupported"
    assert "visibility" not in {change["key"] for change in target["changes"]}


def test_selection_criteria_and_saved_selections(migrated_engine: Engine, world: int) -> None:
    ana = user(migrated_engine, "ana")
    planner = client(migrated_engine, ana, Role.ANALYST)

    def preview(selection: dict[str, Any]) -> list[str]:
        response = planner.post("/api/v1/template-selections/preview", json=selection)
        assert response.status_code == 200, response.text
        return [project["path"] for project in response.json()["listed"]]

    group = preview({"groups": ["platform"]})
    assert SITE in group and API in group
    assert "platform/gone" not in group  # missing projects are never targets
    assert "bob/notes" not in group
    assert preview({"groups": ["platform"], "drift": ["drift"]}) == [SITE]
    assert preview({"template": "service", "projects": [API, SITE]}) == [API, SITE]
    assert preview({"projects": [API], "findings": "open"}) == []
    assert planner.post("/api/v1/template-selections/preview", json={}).status_code == 422
    assert (
        planner.post("/api/v1/template-selections/preview", json={"tags": ["nokey"]}).status_code
        == 422
    )

    saved = planner.post(
        "/api/v1/template-selections",
        json={
            "name": "Drifting platform",
            "selection": {"groups": ["platform"], "drift": ["drift"]},
        },
    )
    assert saved.status_code == 201, saved.text
    duplicate = planner.post(
        "/api/v1/template-selections",
        json={"name": "Drifting platform", "selection": {"groups": ["platform"]}},
    )
    assert duplicate.status_code == 409
    listed = client(migrated_engine, ana, Role.AUDITOR).get("/api/v1/template-selections").json()
    assert [row["name"] for row in listed["selections"]] == ["Drifting platform"]
    assert listed["selections"][0]["selection"]["drift"] == ["drift"]
    deleted = planner.delete(f"/api/v1/template-selections/{saved.json()['id']}")
    assert deleted.status_code == 204
    with Session(migrated_engine) as db:
        assert db.scalars(select(TemplateChangeSet)).first() is None
        actions = db.scalars(
            select(AuditEvent.action).where(AuditEvent.resource_type == "saved_selection")
        ).all()
    assert sorted(actions) == ["saved_selection.created", "saved_selection.deleted"]


def test_development_mode_relaxes_only_the_creators_own_approvals(
    migrated_engine: Engine, world: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BALUARTE_DEVELOPMENT_MODE", "true")
    ana, bea = user(migrated_engine, "ana"), user(migrated_engine, "bea")
    other = create(migrated_engine, ana).json()
    admin = client(migrated_engine, bea, Role.ADMIN)
    path = f"/api/v1/template-change-sets/{other['id']}/approve"
    assert admin.post(path, json={"digest": other["digest"]}).status_code == 200
    assert admin.post(path, json={"digest": other["digest"]}).status_code == 403

    own = create(migrated_engine, ana).json()
    creator = client(migrated_engine, ana, Role.ADMIN)
    path = f"/api/v1/template-change-sets/{own['id']}/approve"
    assert creator.post(path, json={"digest": own["digest"]}).status_code == 200
    approved = creator.post(path, json={"digest": own["digest"]})
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "approved"
    assert approved.json()["development_decision"] is True


def test_effective_settings_remediate_drift(migrated_engine: Engine, world: int) -> None:
    ana, bea = user(migrated_engine, "ana"), user(migrated_engine, "bea")
    with Session(migrated_engine) as db:
        instance = db.get_one(GitLabInstance, world)
        template = db.scalars(select(ConfigurationTemplate)).one()
        site = Target(target_type="project", target_id=101, path=SITE, state="active")
        assign(
            db,
            instance,
            template,
            site,
            {"merge_requests": {"min_approvals": 3}},
            "The site takes three approvals while it is migrated.",
            "test",
        )
        db.commit()

    response = create(
        migrated_engine, ana, effective=True, selection={"drift": ["drift", "unknown"]}
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["source"], body["source_kind"]) == (None, "effective")
    assert body["document"]["source"]["effective"] is True
    targets = by_path(body["document"])
    # Only projects drifting or unknown are chosen; the aligned API project is left alone.
    assert API not in targets
    changes = {change["key"]: change for change in targets[SITE]["changes"]}
    # The project's own override (3 approvals) is wanted, not the template's 2.
    assert "merge_requests.min_approvals" in targets[SITE]["unchanged"]
    assert (changes["visibility"]["before"], changes["visibility"]["after"]) == (
        "public",
        "private",
    )
    assert changes["visibility"]["decided_by"]["template"] == "service@1"
    assert changes["visibility"]["decided_by"]["origin"] == "template"
    assert changes["visibility"]["decided_by"]["assigned_to"] == SITE

    # Changing what is in force makes the set stale.
    with Session(migrated_engine) as db:
        instance = db.get_one(GitLabInstance, world)
        template = db.scalars(select(ConfigurationTemplate)).one()
        assign(db, instance, template, site, None, None, "test")
        db.commit()
    stale = client(migrated_engine, bea, Role.ADMIN).post(
        f"/api/v1/template-change-sets/{body['id']}/approve", json={"digest": body["digest"]}
    )
    assert stale.status_code == 409, stale.text


def test_effective_settings_need_a_template_in_force(migrated_engine: Engine, fresh: int) -> None:
    ana = user(migrated_engine, "ana")
    response = create(migrated_engine, ana, effective=True, selection={"projects": [SITE]})
    assert response.status_code == 422
    assert "No template sets anything" in response.json()["detail"]


def test_tags_become_topics_only_for_the_keys_asked(migrated_engine: Engine, world: int) -> None:
    ana = user(migrated_engine, "ana")
    planner = client(migrated_engine, ana, Role.ANALYST)
    with Session(migrated_engine) as db:
        for gitlab_id, topics in ((100, ["team:payments", "tier:1"]), (101, ["docs", "tier:2"])):
            db.execute(
                update(GitLabProject)
                .where(GitLabProject.gitlab_id == gitlab_id)
                .values(topics=topics)
            )
        db.commit()
    for key, value, target in (("team", "payments", 100), ("team", "web", 101), ("tier", "3", 101)):
        tag = planner.post("/api/v1/tags", json={"key": key, "value": value}).json()
        planner.post(
            f"/api/v1/tags/{tag['id']}/assign",
            json={"targets": [{"type": "project", "gitlab_id": target}]},
        )

    response = create(migrated_engine, ana, topics={"keys": ["team"]})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["source_kind"] == "topics" and body["sensitive"] is False
    targets = by_path(body["document"])
    assert targets[API]["unchanged"] == ["topics"]
    site = targets[SITE]["changes"][0]
    # Only team topics are managed; docs and tier:2 stay although tier=3 is tagged.
    assert (site["before"], site["after"]) == (["docs", "tier:2"], ["docs", "team:web", "tier:2"])
    assert [s["code"] for s in targets[LEGACY]["skipped"]] == ["unknown"]

    assert create(migrated_engine, ana, topics={"keys": ["Team"]}).status_code == 422
    assert create(migrated_engine, ana, topics={"keys": []}).status_code == 422
    both = create(migrated_engine, ana, topics={"keys": ["team"]}, settings=SETTINGS)
    assert both.status_code == 422
