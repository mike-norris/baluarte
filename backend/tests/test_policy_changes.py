"""Two-person review of baseline changes: proposals, decisions and the evidence they leave."""

from datetime import UTC, datetime

import pytest
import test_policy_assignments
from fastapi.testclient import TestClient
from oidc_fake import make_settings
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from test_policy_schema import BASELINE as SOURCE

from baluarte.auth.dependencies import (
    Principal,
    auth_settings,
    current_principal,
    database_engine,
)
from baluarte.auth.roles import Role
from baluarte.db.models import (
    AuditEvent,
    CollectionJob,
    GitLabInstance,
    GitLabProject,
    Policy,
    PolicyAssignment,
    PolicyChange,
)
from baluarte.main import create_app
from baluarte.policy.assignments import resolve_project

pytestmark = pytest.mark.db

# The inventory and baselines seeded for the assignment tests.
gitlab_env = test_policy_assignments.gitlab_env
instance_id = test_policy_assignments.instance_id
DEFAULT = test_policy_assignments.DEFAULT

FRESH = SOURCE.replace(f"name: {DEFAULT}", "name: fresh")
FRESH_V2 = FRESH.replace("allowed: [private, internal]", "allowed: [private]")
FRESH_V3 = FRESH.replace("allowed: [private, internal]", "allowed: [internal]")
CSRF = {"X-CSRF-Token": "csrf"}


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


def require_review(engine: Engine, required: bool = True) -> None:
    response = client(engine, 9, Role.ADMIN).put(
        "/api/v1/settings/governance", json={"require_review": required}
    )
    assert response.status_code == 200
    assert response.json() == {"require_review": required}


def actions(engine: Engine) -> list[str]:
    with Session(engine) as db:
        return list(db.scalars(select(AuditEvent.action).order_by(AuditEvent.id)))


def change_rows(engine: Engine) -> list[PolicyChange]:
    with Session(engine) as db:
        return list(db.scalars(select(PolicyChange).order_by(PolicyChange.id)))


def versions(engine: Engine, name: str) -> list[int]:
    with Session(engine) as db:
        return list(db.scalars(select(Policy.version).where(Policy.name == name)))


def in_force(engine: Engine, gitlab_id: int) -> list[str]:
    """Policy names in force for a project: exactly what evaluation judges it against."""
    with Session(engine) as db:
        project = db.scalars(
            select(GitLabProject).where(GitLabProject.gitlab_id == gitlab_id)
        ).one()
        instance = db.get(GitLabInstance, project.instance_id)
        assert instance is not None
        return [entry.name for entry in resolve_project(db, instance, project).policies]


def queued_evaluations(engine: Engine) -> int:
    with Session(engine) as db:
        return len(
            db.scalars(select(CollectionJob).where(CollectionJob.kind == "policy.evaluate")).all()
        )


def test_review_is_off_by_default_and_changes_are_recorded_as_self_approved(
    migrated_engine: Engine, instance_id: int
) -> None:
    analyst = client(migrated_engine, 1, Role.ANALYST)

    saved = analyst.post("/api/v1/policies", json={"source_yaml": FRESH})
    assigned = analyst.post(
        "/api/v1/policy-assignments",
        json={"policy_name": "fresh", "version": 1, "target_type": "group", "target_id": 10},
    )

    assert saved.status_code == 201
    review = saved.json()["review"]
    assert review["self_approved"] is True
    assert review["reviewed"] is False
    assert review["proposed_by"]["actor"] == "user:1"
    assert review["decided_by"]["actor"] == "user:1"
    assert assigned.status_code == 201
    assert assigned.json()["review"]["self_approved"] is True
    assert "fresh" in in_force(migrated_engine, 101)

    removed = analyst.delete(f"/api/v1/policy-assignments/{assigned.json()['id']}")

    assert removed.status_code == 204
    rows = change_rows(migrated_engine)
    assert [(row.kind, row.state, row.self_approved, row.review_required) for row in rows] == [
        ("version", "approved", True, False),
        ("assign", "approved", True, False),
        ("unassign", "approved", True, False),
    ]
    assert all(row.decided_by_user_id == row.proposed_by_user_id == 1 for row in rows)
    page = analyst.get("/api/v1/policy-changes").json()
    assert page["review_required"] is False
    assert [item["kind"] for item in page["items"]] == ["unassign", "assign", "version"]
    with Session(migrated_engine) as db:
        created = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "policy.version_created")
        ).one()
    assert created.details["change_id"] == rows[0].id
    assert created.details["self_approved"] is True


def test_a_proposed_version_takes_effect_only_when_someone_else_approves_it(
    migrated_engine: Engine, instance_id: int
) -> None:
    require_review(migrated_engine)
    proposer = client(migrated_engine, 1, Role.ANALYST)
    reviewer = client(migrated_engine, 2, Role.ANALYST)

    proposed = proposer.post("/api/v1/policies", json={"source_yaml": FRESH})

    assert proposed.status_code == 202
    change = proposed.json()
    assert change["state"] == "proposed"
    assert change["kind"] == "version"
    assert change["version"] is None
    assert change["source_yaml"] == FRESH
    assert versions(migrated_engine, "fresh") == []
    pending = proposer.get("/api/v1/policy-changes", params={"state": "proposed"}).json()
    assert pending["review_required"] is True
    assert [item["id"] for item in pending["items"]] == [change["id"]]

    approved = reviewer.post(
        f"/api/v1/policy-changes/{change['id']}/approve", json={"note": "Matches the standard"}
    )

    assert approved.status_code == 200
    body = approved.json()
    assert body["state"] == "approved"
    assert body["version"] == 1
    assert body["decided_by"]["actor"] == "user:2"
    assert body["decision_note"] == "Matches the standard"
    assert versions(migrated_engine, "fresh") == [1]
    version = proposer.get("/api/v1/policies/fresh/versions/1").json()
    assert version["review"]["reviewed"] is True
    assert version["review"]["self_approved"] is False
    assert version["review"]["decided_by"]["actor"] == "user:2"
    assert actions(migrated_engine)[-3:] == [
        "policy.change_proposed",
        "policy.change_approved",
        "policy.version_created",
    ]


def test_a_new_version_is_compared_with_the_one_in_force(
    migrated_engine: Engine, instance_id: int
) -> None:
    analyst = client(migrated_engine, 1, Role.ANALYST)
    assert analyst.post("/api/v1/policies", json={"source_yaml": FRESH}).status_code == 201
    require_review(migrated_engine)

    change = analyst.post("/api/v1/policies", json={"source_yaml": FRESH_V2}).json()

    assert change["base_version"] == 1
    detail = analyst.get(f"/api/v1/policy-changes/{change['id']}").json()
    assert detail["base_source_yaml"] == FRESH
    assert detail["source_yaml"] == FRESH_V2


def test_the_proposer_cannot_decide_their_own_change(
    migrated_engine: Engine, instance_id: int
) -> None:
    require_review(migrated_engine)
    proposer = client(migrated_engine, 1, Role.ADMIN)
    change = proposer.post("/api/v1/policies", json={"source_yaml": FRESH}).json()

    approve = proposer.post(f"/api/v1/policy-changes/{change['id']}/approve", json={})
    reject = proposer.post(f"/api/v1/policy-changes/{change['id']}/reject", json={"note": "no"})

    assert approve.status_code == 403
    assert reject.status_code == 403
    assert "someone other than" in approve.json()["detail"]
    assert versions(migrated_engine, "fresh") == []
    assert change_rows(migrated_engine)[-1].state == "proposed"


def test_the_database_refuses_self_approval_when_review_is_required(
    migrated_engine: Engine, instance_id: int
) -> None:
    require_review(migrated_engine)
    change = (
        client(migrated_engine, 1, Role.ANALYST)
        .post("/api/v1/policies", json={"source_yaml": FRESH})
        .json()
    )

    with pytest.raises(IntegrityError, match="decided_by_another_user"):
        with migrated_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE policy_change SET state = 'rejected', decided_by = 'user:1', "
                    "decided_by_user_id = 1, decided_at = now(), decision_note = 'mine' "
                    "WHERE id = :id"
                ),
                {"id": change["id"]},
            )


def test_the_database_keeps_changes_and_what_they_proposed(
    migrated_engine: Engine, instance_id: int
) -> None:
    require_review(migrated_engine)
    change = (
        client(migrated_engine, 1, Role.ANALYST)
        .post("/api/v1/policies", json={"source_yaml": FRESH})
        .json()
    )
    statements = [
        "DELETE FROM policy_change WHERE id = :id",
        "UPDATE policy_change SET source_yaml = 'other' WHERE id = :id",
        "UPDATE policy_change SET proposed_by_user_id = 2 WHERE id = :id",
    ]
    for statement in statements:
        with pytest.raises(DBAPIError, match=r"policy_change|policy change"):
            with migrated_engine.begin() as connection:
                connection.execute(text(statement), {"id": change["id"]})

    reviewer = client(migrated_engine, 2, Role.ANALYST)
    reviewer.post(f"/api/v1/policy-changes/{change['id']}/reject", json={"note": "Too broad"})
    with pytest.raises(DBAPIError, match="cannot change from rejected to approved"):
        with migrated_engine.begin() as connection:
            connection.execute(
                text("UPDATE policy_change SET state = 'approved' WHERE id = :id"),
                {"id": change["id"]},
            )


def test_a_rejected_change_needs_a_note_and_changes_nothing(
    migrated_engine: Engine, instance_id: int
) -> None:
    require_review(migrated_engine)
    change = (
        client(migrated_engine, 1, Role.ANALYST)
        .post("/api/v1/policies", json={"source_yaml": FRESH})
        .json()
    )
    reviewer = client(migrated_engine, 2, Role.ANALYST)

    assert (
        reviewer.post(f"/api/v1/policy-changes/{change['id']}/reject", json={}).status_code == 422
    )
    rejected = reviewer.post(
        f"/api/v1/policy-changes/{change['id']}/reject", json={"note": "Too broad"}
    )

    assert rejected.status_code == 200
    assert rejected.json()["state"] == "rejected"
    assert versions(migrated_engine, "fresh") == []
    assert actions(migrated_engine)[-1] == "policy.change_rejected"
    again = reviewer.post(f"/api/v1/policy-changes/{change['id']}/approve", json={})
    assert again.status_code == 409


def test_one_proposed_version_per_baseline_at_a_time(
    migrated_engine: Engine, instance_id: int
) -> None:
    require_review(migrated_engine)
    analyst = client(migrated_engine, 1, Role.ANALYST)
    first = analyst.post("/api/v1/policies", json={"source_yaml": FRESH_V2})

    same = analyst.post("/api/v1/policies", json={"source_yaml": FRESH_V2})
    other = analyst.post("/api/v1/policies", json={"source_yaml": FRESH_V3})

    assert same.status_code == 202
    assert same.json()["id"] == first.json()["id"]
    assert other.status_code == 409


def test_a_proposed_assignment_is_not_evaluated_until_approved(
    migrated_engine: Engine, instance_id: int
) -> None:
    analyst = client(migrated_engine, 1, Role.ANALYST)
    analyst.post("/api/v1/policies", json={"source_yaml": FRESH})
    require_review(migrated_engine)
    jobs_before = queued_evaluations(migrated_engine)

    proposed = analyst.post(
        "/api/v1/policy-assignments",
        json={"policy_name": "fresh", "version": 1, "target_type": "group", "target_id": 10},
    )

    assert proposed.status_code == 202
    assert proposed.json()["kind"] == "assign"
    assert proposed.json()["target_path"] == "platform"
    assert "fresh" not in in_force(migrated_engine, 101)
    assert queued_evaluations(migrated_engine) == jobs_before

    reviewer = client(migrated_engine, 2, Role.ADMIN)
    approved = reviewer.post(f"/api/v1/policy-changes/{proposed.json()['id']}/approve", json={})

    assert approved.status_code == 200
    assert approved.json()["assignment_id"] is not None
    assert "fresh" in in_force(migrated_engine, 101)
    assert queued_evaluations(migrated_engine) == jobs_before + 1
    listed = analyst.get("/api/v1/policy-assignments", params={"policy_name": "fresh"}).json()
    assert listed["items"][0]["review"]["reviewed"] is True
    assert actions(migrated_engine)[-2:] == ["policy.change_approved", "policy.assigned"]

    removal = analyst.delete(f"/api/v1/policy-assignments/{approved.json()['assignment_id']}")

    assert removal.status_code == 202
    assert removal.json()["kind"] == "unassign"
    assert "fresh" in in_force(migrated_engine, 101)

    reviewer.post(f"/api/v1/policy-changes/{removal.json()['id']}/approve", json={})

    assert "fresh" not in in_force(migrated_engine, 101)
    with Session(migrated_engine) as db:
        assert (
            db.scalars(
                select(PolicyAssignment).where(PolicyAssignment.policy_name == "fresh")
            ).all()
            == []
        )


def test_an_assignment_whose_target_went_away_cannot_be_approved(
    migrated_engine: Engine, instance_id: int
) -> None:
    require_review(migrated_engine)
    analyst = client(migrated_engine, 1, Role.ANALYST)
    proposed = analyst.post(
        "/api/v1/policy-assignments",
        json={"policy_name": DEFAULT, "version": 1, "target_type": "project", "target_id": 101},
    ).json()
    with migrated_engine.begin() as connection:
        connection.execute(
            text("UPDATE gitlab_project SET state = 'missing' WHERE gitlab_id = 101")
        )

    response = client(migrated_engine, 2, Role.ANALYST).post(
        f"/api/v1/policy-changes/{proposed['id']}/approve", json={}
    )

    assert response.status_code == 409
    assert change_rows(migrated_engine)[-1].state == "proposed"


def test_only_admins_change_the_review_setting_and_it_is_audited(
    migrated_engine: Engine,
) -> None:
    analyst = client(migrated_engine, 1, Role.ANALYST)
    admin = client(migrated_engine, 9, Role.ADMIN)

    assert (
        analyst.put("/api/v1/settings/governance", json={"require_review": True}).status_code == 403
    )
    assert admin.get("/api/v1/settings/governance").json() == {"require_review": False}
    require_review(migrated_engine)
    require_review(migrated_engine, False)

    with Session(migrated_engine) as db:
        events = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "settings.governance_updated")
        ).all()
    assert [(event.details["require_review"], event.details["previous"]) for event in events] == [
        (True, False),
        (False, True),
    ]


def test_audit_events_report_whether_their_digest_still_verifies(
    migrated_engine: Engine,
) -> None:
    require_review(migrated_engine)
    auditor = client(migrated_engine, 3, Role.AUDITOR)

    [event] = auditor.get("/api/v1/audit-events").json()["items"]
    assert event["digest_valid"] is True
    assert auditor.get("/api/v1/audit-events/actions").json() == ["settings.governance_updated"]

    with migrated_engine.begin() as connection:
        # Bypasses the append-only trigger, as a database administrator could.
        connection.execute(text("SET LOCAL session_replication_role = replica"))
        connection.execute(text("UPDATE audit_event SET details = '{\"require_review\": false}'"))

    [tampered] = auditor.get("/api/v1/audit-events").json()["items"]
    assert tampered["digest_valid"] is False
    assert client(migrated_engine, 1, Role.ANALYST).get("/api/v1/audit-events").status_code == 403
