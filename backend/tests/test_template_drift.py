# ruff: noqa: F811 (pytest fixtures imported from test_evaluation_project)
"""Template drift, alignment and template exceptions (M14.2)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from gitlab_fake import TOKEN, URL
from oidc_fake import make_settings
from sqlalchemy import Engine, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_evaluation_access import GOOD_SETTINGS, approval_rule
from test_evaluation_project import fresh, instance_id  # noqa: F401

from baluarte.auth.dependencies import (
    Principal,
    auth_settings,
    current_principal,
    database_engine,
)
from baluarte.auth.roles import Role
from baluarte.db.models import AppUser, GitLabInstance, GitLabProject
from baluarte.evidence.observations import Observed, record
from baluarte.integrations.gitlab.access import APPROVALS_SOURCE
from baluarte.main import create_app
from baluarte.policy.assignments import Target
from baluarte.templates.store import assign, save_version

pytestmark = pytest.mark.db

TEMPLATE = """apiVersion: baluarte.io/v1alpha1
kind: ConfigurationTemplate
metadata:
  name: service
spec:
  settings:
    visibility: private
    default_branch_protected: true
    merge_requests:
      min_approvals: 2
      forbid_author_approval: true
    security_features: [secret_scanning]
"""
REASON = "The documentation site is deliberately left unprotected."


@pytest.fixture(autouse=True)
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)


def observe_approvals(engine: Engine, instance_id: int, gitlab_id: int, payload: Any) -> None:
    with Session(engine) as db:
        row_id = db.scalars(
            select(GitLabProject.id).where(GitLabProject.gitlab_id == gitlab_id)
        ).one()
        observed = Observed(
            target_id=row_id,
            source=APPROVALS_SOURCE,
            source_ref=f"/projects/{gitlab_id}/approvals",
            collection_status="ok",
            payload=payload,
            error=None,
        )
        record(db, instance_id, "project_id", [observed])
        db.commit()


@pytest.fixture
def world(migrated_engine: Engine, fresh: int) -> int:
    """The template assigned to the connection; 101 overrides nothing yet."""
    with Session(migrated_engine) as db:
        probes = {
            "projects.list": {"status": "supported"},
            "protected_branches.read": {"status": "supported"},
            "merge_request_approvals.read": {"status": "supported"},
        }
        db.execute(
            update(GitLabInstance)
            .where(GitLabInstance.id == fresh)
            .values(capability_snapshot={"status": "ok", "probes": probes})
        )
        instance = db.get_one(GitLabInstance, fresh)
        template = save_version(db, TEMPLATE, "test").template
        root = Target(target_type="instance", target_id=fresh, path=URL, state=None)
        assign(db, instance, template, root, None, None, "test")
        db.commit()
    observe_approvals(
        migrated_engine,
        fresh,
        100,
        {
            "branch": "main",
            "branch_protected": True,
            "settings": GOOD_SETTINGS,
            "rules": [approval_rule(2)],
        },
    )
    observe_approvals(
        migrated_engine,
        fresh,
        101,
        {
            "branch": "main",
            "branch_protected": False,
            "settings": {**GOOD_SETTINGS, "merge_requests_author_approval": True},
            "rules": [approval_rule(3)],
        },
    )
    return fresh


def user(engine: Engine, name: str) -> int:
    with Session(engine) as db:
        row = AppUser(issuer="https://idp.example", subject=name, display_name=name.title())
        db.add(row)
        db.commit()
        return row.id


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
    return TestClient(app)


def project_id(engine: Engine, gitlab_id: int) -> int:
    with Session(engine) as db:
        return db.scalars(
            select(GitLabProject.id).where(GitLabProject.gitlab_id == gitlab_id)
        ).one()


def drift(engine: Engine, gitlab_id: int, user_id: int = 1) -> dict[str, Any]:
    response = client(engine, user_id, Role.AUDITOR).get(
        f"/api/v1/templates/drift/{project_id(engine, gitlab_id)}"
    )
    assert response.status_code == 200, response.text
    return response.json()


def statuses(body: dict[str, Any]) -> dict[str, str]:
    return {setting["key"]: setting["status"] for setting in body["settings"]}


def test_an_aligned_project(migrated_engine: Engine, world: int) -> None:
    body = drift(migrated_engine, 100)

    assert statuses(body) == {
        "visibility": "aligned",
        "default_branch_protected": "aligned",
        "merge_requests.min_approvals": "aligned",
        "merge_requests.forbid_author_approval": "aligned",
        # A GitHub setting: never aligned on GitLab.
        "security_features": "unsupported",
    }
    assert body["status"] == "aligned"
    assert body["counts"] == {"aligned": 4, "unsupported": 1}
    first = body["settings"][0]
    assert first["decided_by"]["template"] == "service"
    assert first["observed_at"] is not None


def test_a_drifting_project_and_exact_approvals(migrated_engine: Engine, world: int) -> None:
    body = drift(migrated_engine, 101)

    assert statuses(body) == {
        "visibility": "drift",
        "default_branch_protected": "drift",
        # Three approvals required where the template says exactly two.
        "merge_requests.min_approvals": "drift",
        "merge_requests.forbid_author_approval": "drift",
        "security_features": "unsupported",
    }
    approvals = next(s for s in body["settings"] if s["key"] == "merge_requests.min_approvals")
    assert "more than the 2 approvals" in approvals["reason"]
    assert body["status"] == "drift"


def test_missing_evidence_is_unknown_never_aligned(migrated_engine: Engine, world: int) -> None:
    body = drift(migrated_engine, 102)

    found = statuses(body)
    assert found["visibility"] == "unknown"
    assert found["merge_requests.min_approvals"] == "unknown"
    assert body["status"] == "unknown"


def test_false_means_the_setting_is_off(migrated_engine: Engine, world: int) -> None:
    with Session(migrated_engine) as db:
        instance = db.get_one(GitLabInstance, world)
        template = save_version(db, TEMPLATE, "test").template
        here = Target(target_type="project", target_id=101, path="platform/site", state="active")
        assign(db, instance, template, here, {"default_branch_protected": False}, REASON, "test")
        db.commit()

    assert statuses(drift(migrated_engine, 101))["default_branch_protected"] == "aligned"
    off = statuses(drift(migrated_engine, 100))
    assert off["default_branch_protected"] == "aligned"  # 100 keeps the connection's template


def test_an_approved_exception_waives_drift_but_not_unknown(
    migrated_engine: Engine, world: int
) -> None:
    ana, bea = user(migrated_engine, "ana"), user(migrated_engine, "bea")
    expires = (datetime.now(UTC) + timedelta(days=10)).isoformat()
    requester = client(migrated_engine, ana, Role.ANALYST)
    approver = client(migrated_engine, bea, Role.ADMIN)

    requested = requester.post(
        "/api/v1/template-exceptions",
        json={
            "project_id": project_id(migrated_engine, 101),
            "setting": "visibility",
            "justification": "A public documentation site.",
            "expires_at": expires,
        },
    )
    assert requested.status_code == 201, requested.text
    exception_id = requested.json()["id"]
    assert statuses(drift(migrated_engine, 101))["visibility"] == "drift"

    own = client(migrated_engine, ana, Role.ADMIN).post(
        f"/api/v1/template-exceptions/{exception_id}/approve", json={}
    )
    assert own.status_code == 403
    approved = approver.post(f"/api/v1/template-exceptions/{exception_id}/approve", json={})
    assert approved.status_code == 200
    assert approved.json()["state"] == "approved"
    assert approved.json()["expiring"] is True

    body = drift(migrated_engine, 101)
    visibility = next(s for s in body["settings"] if s["key"] == "visibility")
    assert visibility["status"] == "excepted"
    assert visibility["exception"]["expiring"] is True
    assert body["status"] == "drift"  # other settings still drift

    again = requester.post(
        "/api/v1/template-exceptions",
        json={
            "project_id": project_id(migrated_engine, 101),
            "setting": "visibility",
            "justification": "A public documentation site.",
            "expires_at": expires,
        },
    )
    assert again.status_code == 409

    revoked = approver.post(
        f"/api/v1/template-exceptions/{exception_id}/revoke", json={"note": "No longer public."}
    )
    assert revoked.status_code == 200
    assert statuses(drift(migrated_engine, 101))["visibility"] == "drift"
    listed = approver.get("/api/v1/template-exceptions?state=revoked").json()["exceptions"]
    assert [row["id"] for row in listed] == [exception_id]


def test_exceptions_need_a_setting_the_project_has(migrated_engine: Engine, world: int) -> None:
    ana = user(migrated_engine, "ana")
    body = {
        "project_id": project_id(migrated_engine, 101),
        "setting": "labels_consistent",
        "justification": "Not set by the template.",
        "expires_at": (datetime.now(UTC) + timedelta(days=10)).isoformat(),
    }
    response = client(migrated_engine, ana, Role.ANALYST).post(
        "/api/v1/template-exceptions", json=body
    )
    assert response.status_code == 404
    too_late = {
        **body,
        "setting": "visibility",
        "expires_at": (datetime.now(UTC) + timedelta(days=400)).isoformat(),
    }
    response = client(migrated_engine, ana, Role.ANALYST).post(
        "/api/v1/template-exceptions", json=too_late
    )
    assert response.status_code == 422
    auditor = client(migrated_engine, ana, Role.AUDITOR).post(
        "/api/v1/template-exceptions", json={**body, "setting": "visibility"}
    )
    assert auditor.status_code == 403


def test_template_exceptions_cannot_be_deleted(migrated_engine: Engine, world: int) -> None:
    ana = user(migrated_engine, "ana")
    client(migrated_engine, ana, Role.ANALYST).post(
        "/api/v1/template-exceptions",
        json={
            "project_id": project_id(migrated_engine, 101),
            "setting": "visibility",
            "justification": "A public documentation site.",
            "expires_at": (datetime.now(UTC) + timedelta(days=10)).isoformat(),
        },
    )
    with migrated_engine.connect() as connection, pytest.raises(DBAPIError):
        connection.execute(text("DELETE FROM template_exception"))
    with migrated_engine.connect() as connection, pytest.raises(DBAPIError):
        connection.execute(text("UPDATE template_exception SET setting = 'labels_consistent'"))


def test_alignment_across_the_connection(migrated_engine: Engine, world: int) -> None:
    response = client(migrated_engine, 1, Role.AUDITOR).get("/api/v1/templates/alignment")

    assert response.status_code == 200, response.text
    body = response.json()
    # Active projects: 100, 101, 102, 104, 105, 106 (103 is missing).
    assert body["projects"] == 6
    assert body["projects_with_templates"] == 6
    assert body["projects_aligned"] == 1
    assert body["projects_drifting"] == 1
    assert body["projects_unknown"] == 4
    assert body["attention"][0]["project"]["path"] == "platform/site"
    assert body["attention"][0]["status"] == "drift"
    [template] = body["by_template"]
    assert template["name"] == "service v1"
    assert template["projects"] == 6
    assert template["projects_drifting"] == 1
    assert {group["name"] for group in body["by_group"]} == {"platform", "alice", "bob"}
    # 104 (collection error) and 105 (incomplete ancestry) are unknown before unsupported.
    assert body["settings"]["unsupported"] == 4
    assert body["expiring_exceptions"] == 0


def test_a_project_without_templates_has_no_status(migrated_engine: Engine, fresh: int) -> None:
    body = drift(migrated_engine, 100)

    assert body["settings"] == []
    assert body["status"] is None
