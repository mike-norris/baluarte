"""Dry-run remediation plans: generation, skips, approval, staleness, expiry, no writes."""

import inspect
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from gitlab_fake import TOKEN, URL
from sqlalchemy import Engine, delete, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from test_evaluation_project import observe_protection, protection, set_probe
from test_evaluation_store import VISIBILITY_ONLY, World
from test_exceptions import add_exception, client_as, user
from test_policy_schema import BASELINE

from baluarte.api.v1 import remediation as remediation_api
from baluarte.audit import details_digest
from baluarte.auth.roles import Role
from baluarte.db.models import AuditEvent, Finding, GitLabProject, RemediationPlan
from baluarte.integrations.gitlab import client as gitlab_client
from baluarte.integrations.gitlab.settings import GitLabSettings
from baluarte.remediation import plans
from baluarte.remediation.plans import NothingToPlan, Scope

pytestmark = pytest.mark.db

PLANS = "/api/v1/remediation/plans"


@pytest.fixture(autouse=True)
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)


@pytest.fixture(autouse=True)
def no_http(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Any HTTP request from any code path fails the test: planning never calls GitLab."""
    sent: list[httpx.Request] = []

    def refuse(self: httpx.BaseTransport, request: httpx.Request) -> httpx.Response:
        sent.append(request)
        raise AssertionError(f"unexpected HTTP request: {request.method} {request.url}")

    # The real network transport; the test client uses its own in-process transport.
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    return sent


@pytest.fixture
def world(migrated_engine: Engine) -> World:
    world = World(migrated_engine)
    world.assign()
    return world


@pytest.fixture
def people(world: World) -> dict[str, int]:
    return {name: user(world.engine, name) for name in ("ana", "bea", "cy")}


def findings(world: World) -> dict[int, Finding]:
    """Findings by project GitLab ID."""
    with Session(world.engine) as db:
        rows = db.execute(
            select(GitLabProject.gitlab_id, Finding).join(
                GitLabProject, GitLabProject.id == Finding.project_id
            )
        ).all()
        return {gitlab_id: finding for gitlab_id, finding in rows}


def build(world: World, **scope: Any) -> plans._Built:
    with Session(world.engine) as db:
        return plans.build(db, world.instance_id, Scope(**scope))


def create(world: World, creator: int, ttl: timedelta = timedelta(hours=24), **scope: Any) -> int:
    with Session(world.engine) as db:
        instance = db.get_one(plans.GitLabInstance, world.instance_id)
        row = plans.create(db, instance, Scope(**scope), ttl, creator, f"user:{creator}")
        db.commit()
        return row.id


def plan_row(world: World, plan_id: int) -> RemediationPlan:
    with Session(world.engine) as db:
        return db.get_one(RemediationPlan, plan_id)


def audit_actions(engine: Engine, plan_id: int) -> list[str]:
    with Session(engine) as db:
        return list(
            db.scalars(
                select(AuditEvent.action)
                .where(
                    AuditEvent.resource_type == "remediation_plan",
                    AuditEvent.resource_id == str(plan_id),
                )
                .order_by(AuditEvent.id)
            )
        )


# --- Generation --------------------------------------------------------------------------------


def test_a_public_project_gets_a_restrict_visibility_step(world: World) -> None:
    world.project(1, "public")
    world.run()
    finding = findings(world)[1]

    (step,) = build(world, finding_ids=(finding.id,)).steps

    assert step["action"] == "restrict_project_visibility"
    # The least exposed level the policy allows ([private, internal]).
    assert step["current"] == {"visibility": "public"}
    assert step["intended"] == {"visibility": "private"}
    assert step["request"] == {
        "method": "PUT",
        "path": "/projects/1",
        "body": {"visibility": "private"},
    }
    assert step["verify"] == {
        "method": "GET",
        "path": "/projects/1",
        "expect": {"visibility": "private"},
    }
    assert step["target"] == {"project_gitlab_id": 1, "project_path": "platform/p1"}
    assert step["finding_id"] == finding.id
    assert step["evaluation_id"] == finding.latest_evaluation_id
    assert step["observation_id"] is not None
    assert step["expected"] == {"allowed": ["private", "internal"]}
    assert step["rule"]["id"] == "project-visibility"
    assert "GET /projects/1 still reports visibility public" in step["preconditions"]
    assert any("Non-members lose access" in effect for effect in step["effects"])
    assert step["requires"]["role"] == "Owner"
    assert re.fullmatch(r"restrict_project_visibility:\d+:1:[0-9a-f]{16}", step["idempotency_key"])


def test_an_unprotected_default_branch_gets_a_protect_step(migrated_engine: Engine) -> None:
    world = World(migrated_engine)
    world.assign(BASELINE)
    set_probe(world.engine, world.instance_id, "supported")
    world.project(1, "private", default_branch="main")
    observe_protection(world.engine, world.instance_id, 1, protection("main", False))
    world.run()
    finding = findings(world)[1]
    assert (finding.rule_id, finding.state) == ("default-branch-protected", "open")

    (step,) = build(world, finding_ids=(finding.id,)).steps

    assert step["action"] == "protect_default_branch"
    assert step["request"] == {
        "method": "POST",
        "path": "/projects/1/protected_branches",
        "body": {
            "name": "main",
            "push_access_level": 40,
            "merge_access_level": 40,
            "unprotect_access_level": 40,
        },
    }
    assert step["current"] == {"branch": "main", "protected": False}
    assert step["intended"]["protected"] is True
    assert (
        "GET /projects/1/protected_branches still has no rule matching main, wildcards included"
        in step["preconditions"]
    )
    assert any("409" in effect for effect in step["effects"])
    assert step["observation_source"] == "GET /projects/:id/protected_branches"


def test_the_same_change_always_gets_the_same_idempotency_key(world: World) -> None:
    world.project(1, "public")
    world.run()
    finding_id = findings(world)[1].id

    first = build(world, finding_ids=(finding_id,)).steps[0]["idempotency_key"]
    again = build(world, finding_ids=(finding_id,)).steps[0]["idempotency_key"]

    assert first == again


def test_unknown_excepted_and_resolved_findings_are_never_planned(
    world: World, people: dict[str, int]
) -> None:
    unknown = world.project(1, "public")
    excepted = world.project(2, "public")
    resolved = world.project(3, "public")
    world.run()
    # Project 1 is now unreported: its finding stays open, but the latest result is unknown.
    world.observe(unknown, None)
    add_exception(world, excepted, people["ana"], approver=people["bea"])
    world.observe(resolved, "private")
    world.run()
    by_project = findings(world)
    assert by_project[1].latest_outcome == "unknown"
    assert by_project[2].state == "excepted"
    assert by_project[3].state == "resolved"

    built = build(world, finding_ids=tuple(finding.id for finding in by_project.values()))

    assert built.steps == []
    reasons = {skipped["project_path"]: skipped["reason_code"] for skipped in built.skipped}
    assert reasons == {
        "platform/p1": "latest_unknown",
        "platform/p2": "excepted",
        "platform/p3": "resolved",
    }


def test_filters_select_unresolved_findings_and_report_skips(world: World) -> None:
    world.project(1, "public")
    world.project(2, "internal")  # allowed: no finding
    unknown = world.project(3, "public")
    world.run()
    world.observe(unknown, None)
    world.run()

    built = build(world, policy="default-gitlab-baseline")

    assert [step["target"]["project_gitlab_id"] for step in built.steps] == [1]
    assert [skipped["reason_code"] for skipped in built.skipped] == ["latest_unknown"]


def test_an_unknown_finding_id_is_reported(world: World) -> None:
    built = build(world, finding_ids=(999,))

    assert built.skipped[0]["reason_code"] == "not_found"


def test_a_change_that_would_widen_access_is_skipped(migrated_engine: Engine) -> None:
    world = World(migrated_engine)
    world.assign(VISIBILITY_ONLY.replace("allowed: [private, internal]", "allowed: [public]"))
    world.project(1, "private")
    world.run()

    built = build(world, finding_ids=(findings(world)[1].id,))

    assert built.steps == []
    assert built.skipped[0]["reason_code"] == "would_widen_access"


def test_an_archived_project_is_skipped(world: World) -> None:
    project_id = world.project(1, "public")
    world.run()
    with Session(world.engine) as db:
        db.execute(
            update(GitLabProject).where(GitLabProject.id == project_id).values(archived=True)
        )
        db.commit()

    built = build(world, finding_ids=(findings(world)[1].id,))

    assert built.skipped[0]["reason_code"] == "project_archived"


def test_a_failure_judged_on_superseded_evidence_is_skipped(world: World) -> None:
    project_id = world.project(1, "public")
    world.run()
    # Newer evidence, not yet evaluated.
    world.observe(project_id, "internal")

    built = build(world, finding_ids=(findings(world)[1].id,))

    assert built.skipped[0]["reason_code"] == "evidence_not_latest"


def test_tampered_evidence_is_never_planned(world: World) -> None:
    project_id = world.project(1, "public")
    world.run()
    with Session(world.engine) as db:
        db.execute(text("ALTER TABLE observation DISABLE TRIGGER USER"))
        db.execute(
            text(
                "UPDATE observation SET payload = jsonb_set(payload, '{visibility}', "
                "'\"internal\"') WHERE project_id = :id"
            ),
            {"id": project_id},
        )
        db.execute(text("ALTER TABLE observation ENABLE TRIGGER USER"))
        db.commit()

    built = build(world, finding_ids=(findings(world)[1].id,))

    assert built.skipped[0]["reason_code"] == "evidence_digest_mismatch"


def test_a_scope_with_too_many_findings_is_refused(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    for gitlab_id in (1, 2, 3):
        world.project(gitlab_id, "public")
    world.run()
    monkeypatch.setattr(plans, "MAX_PLAN_STEPS", 2)

    with pytest.raises(plans.ScopeTooLarge):
        build(world, policy="default-gitlab-baseline")


def test_nothing_to_plan_creates_nothing(world: World, people: dict[str, int]) -> None:
    with pytest.raises(NothingToPlan) as raised:
        create(world, people["ana"], finding_ids=(999,))

    assert raised.value.skipped[0]["reason_code"] == "not_found"
    with Session(world.engine) as db:
        assert db.scalar(select(func.count()).select_from(RemediationPlan)) == 0


# --- The stored plan ---------------------------------------------------------------------------


def test_a_stored_plan_is_digested_and_audited(world: World, people: dict[str, int]) -> None:
    world.project(1, "public")
    world.run()

    plan_id = create(world, people["ana"], finding_ids=(findings(world)[1].id,))

    row = plan_row(world, plan_id)
    assert (row.state, row.steps, row.skipped) == ("draft", 1, 0)
    assert row.plan_digest == details_digest(row.plan)
    assert plans.digest_valid(row)
    assert row.plan["dry_run"] is True
    assert row.plan["format"] == "baluarte.remediation-plan/v1"
    assert "BAL-032" in row.plan["execution"]
    assert "group visibility" in row.plan["excluded_actions"]
    assert audit_actions(world.engine, plan_id) == ["remediation_plan.created"]
    assert plans.summary(row.plan) == [
        "~ platform/p1: visibility public -> private (PUT /projects/1; "
        f"finding {findings(world)[1].id})"
    ]


def test_a_plan_cannot_be_changed_or_deleted(world: World, people: dict[str, int]) -> None:
    world.project(1, "public")
    world.run()
    plan_id = create(world, people["ana"], finding_ids=(findings(world)[1].id,))

    for statement in (
        update(RemediationPlan).where(RemediationPlan.id == plan_id).values(plan={"steps": []}),
        update(RemediationPlan)
        .where(RemediationPlan.id == plan_id)
        .values(expires_at=func.now() + timedelta(hours=60)),
        update(RemediationPlan).where(RemediationPlan.id == plan_id).values(state="approved"),
        delete(RemediationPlan).where(RemediationPlan.id == plan_id),
    ):
        with Session(world.engine) as db, pytest.raises(DBAPIError):
            db.execute(statement)
            db.commit()


def test_the_database_requires_another_approver_and_a_bound_digest(
    world: World, people: dict[str, int]
) -> None:
    world.project(1, "public")
    world.run()
    plan_id = create(world, people["ana"], finding_ids=(findings(world)[1].id,))
    with Session(world.engine) as db:
        db.execute(
            update(RemediationPlan)
            .where(RemediationPlan.id == plan_id)
            .values(state="submitted", submitted_at=func.now())
        )
        db.commit()
    row = plan_row(world, plan_id)

    for values in (
        {"decided_by_user_id": people["ana"], "approved_digest": row.plan_digest},
        {"decided_by_user_id": people["bea"], "approved_digest": "0" * 64},
        {"decided_by_user_id": people["bea"], "approved_digest": None},
    ):
        with Session(world.engine) as db, pytest.raises(IntegrityError):
            db.execute(
                update(RemediationPlan)
                .where(RemediationPlan.id == plan_id)
                .values(state="approved", decided_at=func.now(), **values)
            )
            db.commit()


def expired_plan(world: World, creator: int) -> int:
    """A plan created 25 hours ago that expired an hour ago (inserts bypass the trigger)."""
    world.project(1, "public")
    world.run()
    plan_id = create(world, creator, finding_ids=(findings(world)[1].id,))
    row = plan_row(world, plan_id)
    with Session(world.engine) as db:
        new_id = db.execute(
            insert(RemediationPlan)
            .values(
                instance_id=row.instance_id,
                state="submitted",
                created_by_user_id=creator,
                created_at=func.now() - timedelta(hours=25),
                expires_at=func.now() - timedelta(hours=1),
                plan=row.plan,
                plan_digest=row.plan_digest,
                steps=row.steps,
                skipped=row.skipped,
                submitted_at=func.now() - timedelta(hours=24),
            )
            .returning(RemediationPlan.id)
        ).scalar_one()
        db.commit()
        return new_id


def test_expiry_takes_effect_at_expires_at_and_is_audited(
    world: World, people: dict[str, int]
) -> None:
    plan_id = expired_plan(world, people["ana"])
    admin = client_as(world.engine, people["bea"], Role.ADMIN)

    assert admin.get(f"{PLANS}/{plan_id}").json()["state"] == "expired"
    assert [item["id"] for item in admin.get(f"{PLANS}?state=expired").json()["items"]] == [plan_id]
    approve = admin.post(
        f"{PLANS}/{plan_id}/approve", json={"plan_digest": plan_row(world, plan_id).plan_digest}
    )
    assert approve.status_code == 409
    assert "expired" in approve.json()["detail"]

    with Session(world.engine) as db:
        assert plan_id in plans.expire_due(db)
        db.commit()
    row = plan_row(world, plan_id)
    assert (row.state, row.ended_at is not None) == ("expired", True)
    assert audit_actions(world.engine, plan_id) == ["remediation_plan.expired"]


# --- API -------------------------------------------------------------------------------------


def create_over_api(world: World, creator: int, role: Role = Role.ANALYST) -> dict[str, Any]:
    response = client_as(world.engine, creator, role).post(
        PLANS, json={"scope": {"policy": "default-gitlab-baseline"}}
    )
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    return result


def test_create_submit_and_approve_over_the_api(
    world: World, people: dict[str, int], no_http: list[httpx.Request]
) -> None:
    world.project(1, "public")
    world.run()

    created = create_over_api(world, people["ana"])

    assert created["state"] == "draft"
    assert (created["steps"], created["digest_valid"], created["stale_reasons"]) == (1, True, [])
    assert created["summary"][0].startswith("~ platform/p1: visibility public -> private")
    analyst = client_as(world.engine, people["ana"], Role.ANALYST)
    submitted = analyst.post(f"{PLANS}/{created['id']}/submit")
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["state"] == "submitted"

    admin = client_as(world.engine, people["bea"], Role.ADMIN)
    approved = admin.post(
        f"{PLANS}/{created['id']}/approve",
        json={"plan_digest": created["plan_digest"], "note": "Reviewed with the owners"},
    )

    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert (body["state"], body["approved_digest"]) == ("approved", created["plan_digest"])
    assert body["decided_by_user_id"] == people["bea"]
    assert audit_actions(world.engine, created["id"]) == [
        "remediation_plan.created",
        "remediation_plan.submitted",
        "remediation_plan.approved",
    ]
    # Approving changed nothing in GitLab: no HTTP request was made at any point.
    assert no_http == []
    with Session(world.engine) as db:
        assert db.get_one(GitLabProject, findings(world)[1].project_id).visibility == "public"


def test_only_the_creator_submits_and_never_approves_their_own_plan(
    world: World, people: dict[str, int]
) -> None:
    world.project(1, "public")
    world.run()
    created = create_over_api(world, people["ana"], Role.ADMIN)
    other = client_as(world.engine, people["bea"], Role.ADMIN)
    assert other.post(f"{PLANS}/{created['id']}/submit").status_code == 403

    creator = client_as(world.engine, people["ana"], Role.ADMIN)
    assert creator.post(f"{PLANS}/{created['id']}/submit").status_code == 200
    own = creator.post(
        f"{PLANS}/{created['id']}/approve", json={"plan_digest": created["plan_digest"]}
    )

    assert own.status_code == 403
    assert plan_row(world, created["id"]).state == "submitted"


def test_approval_must_name_the_reviewed_digest(world: World, people: dict[str, int]) -> None:
    world.project(1, "public")
    world.run()
    created = create_over_api(world, people["ana"])
    client_as(world.engine, people["ana"], Role.ANALYST).post(f"{PLANS}/{created['id']}/submit")
    admin = client_as(world.engine, people["bea"], Role.ADMIN)

    wrong = admin.post(f"{PLANS}/{created['id']}/approve", json={"plan_digest": "0" * 64})
    missing = admin.post(f"{PLANS}/{created['id']}/approve", json={})

    assert wrong.status_code == 409
    assert "digest" in wrong.json()["detail"]["reasons"][0]
    assert missing.status_code == 422
    assert plan_row(world, created["id"]).state == "submitted"


def test_a_stale_plan_cannot_be_approved(world: World, people: dict[str, int]) -> None:
    project_id = world.project(1, "public")
    world.run()
    created = create_over_api(world, people["ana"])
    client_as(world.engine, people["ana"], Role.ANALYST).post(f"{PLANS}/{created['id']}/submit")
    # Someone fixed it by hand; the next evaluation resolves the finding.
    world.observe(project_id, "private")
    world.run()
    admin = client_as(world.engine, people["bea"], Role.ADMIN)

    assert admin.get(f"{PLANS}/{created['id']}").json()["stale_reasons"] == [
        "Step 1: the finding is no longer open and failing."
    ]
    refused = admin.post(
        f"{PLANS}/{created['id']}/approve", json={"plan_digest": created["plan_digest"]}
    )

    assert refused.status_code == 409
    assert refused.json()["detail"]["reasons"] == [
        "Step 1: the finding is no longer open and failing."
    ]
    # Rejecting a stale plan is still possible.
    rejected = admin.post(f"{PLANS}/{created['id']}/reject", json={"note": "Fixed by hand"})
    assert rejected.json()["state"] == "rejected"


def test_newer_evidence_makes_a_plan_stale(world: World, people: dict[str, int]) -> None:
    project_id = world.project(1, "public")
    world.run()
    created = create_over_api(world, people["ana"])
    client_as(world.engine, people["ana"], Role.ANALYST).post(f"{PLANS}/{created['id']}/submit")
    world.observe(project_id, "internal")

    refused = client_as(world.engine, people["bea"], Role.ADMIN).post(
        f"{PLANS}/{created['id']}/approve", json={"plan_digest": created["plan_digest"]}
    )

    assert refused.status_code == 409
    assert refused.json()["detail"]["reasons"] == ["Step 1: newer evidence exists for the project."]


def test_plans_need_a_current_projects_collection(world: World, people: dict[str, int]) -> None:
    world.project(1, "public")
    world.run()
    created = create_over_api(world, people["ana"])
    client_as(world.engine, people["ana"], Role.ANALYST).post(f"{PLANS}/{created['id']}/submit")
    world.collection("failed", timedelta(minutes=1))
    analyst = client_as(world.engine, people["ana"], Role.ANALYST)

    refused_create = analyst.post(PLANS, json={"scope": {"policy": "default-gitlab-baseline"}})
    refused_approval = client_as(world.engine, people["bea"], Role.ADMIN).post(
        f"{PLANS}/{created['id']}/approve", json={"plan_digest": created["plan_digest"]}
    )

    assert refused_create.status_code == 409
    assert "failed" in refused_create.json()["detail"]
    assert refused_approval.status_code == 409
    assert "projects collection is failed" in refused_approval.json()["detail"]["reasons"][0]


def test_reject_needs_a_note_and_someone_else(world: World, people: dict[str, int]) -> None:
    world.project(1, "public")
    world.run()
    created = create_over_api(world, people["ana"], Role.ADMIN)
    creator = client_as(world.engine, people["ana"], Role.ADMIN)
    creator.post(f"{PLANS}/{created['id']}/submit")

    assert creator.post(f"{PLANS}/{created['id']}/reject", json={"note": "No"}).status_code == 403
    admin = client_as(world.engine, people["bea"], Role.ADMIN)
    assert admin.post(f"{PLANS}/{created['id']}/reject", json={}).status_code == 422
    rejected = admin.post(f"{PLANS}/{created['id']}/reject", json={"note": "Too broad"})

    assert rejected.json()["state"] == "rejected"
    assert rejected.json()["approved_digest"] is None
    again = admin.post(
        f"{PLANS}/{created['id']}/approve", json={"plan_digest": created["plan_digest"]}
    )
    assert again.status_code == 409


def test_a_draft_cannot_be_approved(world: World, people: dict[str, int]) -> None:
    world.project(1, "public")
    world.run()
    created = create_over_api(world, people["ana"])

    response = client_as(world.engine, people["bea"], Role.ADMIN).post(
        f"{PLANS}/{created['id']}/approve", json={"plan_digest": created["plan_digest"]}
    )

    assert response.status_code == 409


@pytest.mark.parametrize(
    ("role", "method", "path", "expected"),
    [
        (Role.AUDITOR, "post", PLANS, 403),
        (Role.AUDITOR, "get", PLANS, 200),
        (Role.ANALYST, "post", f"{PLANS}/1/approve", 403),
        (Role.ANALYST, "post", f"{PLANS}/1/reject", 403),
        (Role.AUDITOR, "post", f"{PLANS}/1/submit", 403),
    ],
)
def test_permissions(
    world: World,
    people: dict[str, int],
    role: Role,
    method: str,
    path: str,
    expected: int,
) -> None:
    client = client_as(world.engine, people["cy"], role)
    body = {"scope": {"finding_ids": [1]}, "plan_digest": "0" * 64, "note": "x"}
    response = client.get(path) if method == "get" else client.post(path, json=body)

    assert response.status_code == expected


def test_nothing_in_scope_is_a_422_with_the_reasons(world: World, people: dict[str, int]) -> None:
    world.project(1, "internal")
    world.run()

    response = client_as(world.engine, people["ana"], Role.ANALYST).post(
        PLANS, json={"scope": {"finding_ids": [42]}}
    )

    assert response.status_code == 422
    assert response.json()["detail"]["skipped"][0]["reason_code"] == "not_found"


@pytest.mark.parametrize(
    "scope",
    [
        {},
        {"finding_ids": [1], "policy": "default-gitlab-baseline"},
        {"finding_ids": list(range(1, 102))},
    ],
)
def test_invalid_scopes(world: World, people: dict[str, int], scope: dict[str, Any]) -> None:
    response = client_as(world.engine, people["ana"], Role.ANALYST).post(
        PLANS, json={"scope": scope}
    )

    assert response.status_code == 422


def test_unknown_plan_is_404(world: World, people: dict[str, int]) -> None:
    client = client_as(world.engine, people["ana"], Role.ADMIN)

    assert client.get(f"{PLANS}/999").status_code == 404
    assert client.post(f"{PLANS}/999/submit").status_code == 404


def test_there_is_no_execution_endpoint(world: World, people: dict[str, int]) -> None:
    paths = client_as(world.engine, people["ana"], Role.ADMIN).get("/openapi.json").json()["paths"]

    assert {path for path in paths if "remediation" in path} == {
        "/api/v1/remediation/plans",
        "/api/v1/remediation/plans/{plan_id}",
        "/api/v1/remediation/plans/{plan_id}/submit",
        "/api/v1/remediation/plans/{plan_id}/approve",
        "/api/v1/remediation/plans/{plan_id}/reject",
    }


# --- No writes to GitLab -----------------------------------------------------------------------


WRITE_CALL = re.compile(r"\.(post|put|patch|delete|request|send|stream)\(")


@pytest.mark.parametrize("module", [plans, remediation_api])
def test_planning_code_uses_no_http_client(module: object) -> None:
    source = inspect.getsource(module)  # type: ignore[arg-type]

    assert "httpx" not in source
    assert "GitLabClient" not in source
    assert "urllib" not in source
    # FastAPI route decorators are the only ``.post(`` in the router module.
    calls = [line.strip() for line in source.splitlines() if WRITE_CALL.search(line)]
    assert all(line.startswith("@router.post(") for line in calls), calls


def test_the_no_http_guard_catches_a_gitlab_call(no_http: list[httpx.Request]) -> None:
    settings = GitLabSettings(url=URL, token=TOKEN)

    with gitlab_client.GitLabClient(settings) as client, pytest.raises(AssertionError):
        client.get("/version")

    assert [request.method for request in no_http] == ["GET"]
    no_http.clear()


def test_the_gitlab_client_can_only_send_get() -> None:
    public = {
        name
        for name, _ in inspect.getmembers(gitlab_client.GitLabClient, inspect.isfunction)
        if not name.startswith("_")
    }
    source = inspect.getsource(gitlab_client)

    assert public == {"close", "get", "get_json", "paginate"}
    assert re.findall(r"self\._http\.(\w+)\(", source) == ["close", "get"]
