"""Policy exceptions: waiving known failures, separation of duties, expiry and the API (BAL-033)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from gitlab_fake import TOKEN, URL
from oidc_fake import make_settings
from sqlalchemy import Engine, delete, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from test_evaluation_store import World

from baluarte.auth.dependencies import Principal, auth_settings, current_principal, database_engine
from baluarte.auth.roles import Role
from baluarte.db.models import (
    AppUser,
    AuditEvent,
    CollectionJob,
    Finding,
    GitLabProject,
    PolicyException,
)
from baluarte.main import create_app
from baluarte.policy.evaluation.runner import EVALUATE

pytestmark = pytest.mark.db

POLICY = "default-gitlab-baseline"
RULE = "project-visibility"


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
    return world


def user(engine: Engine, name: str) -> int:
    with Session(engine) as db:
        row = AppUser(issuer="https://idp.example", subject=name, display_name=name.title())
        db.add(row)
        db.commit()
        return row.id


def add_exception(
    world: World,
    project_id: int,
    requester: int,
    *,
    state: str = "approved",
    approver: int | None = None,
    requested_ago: timedelta = timedelta(days=1),
    expires_in: timedelta = timedelta(days=30),
    rule_id: str = RULE,
) -> int:
    """Insert an exception directly (the trigger guards updates, not inserts)."""
    with Session(world.engine) as db:
        now = db.execute(select(func.now())).scalar_one()
        decided = state in ("approved", "rejected")
        exception_id = db.execute(
            insert(PolicyException)
            .values(
                instance_id=world.instance_id,
                project_id=project_id,
                policy_name=POLICY,
                rule_id=rule_id,
                justification="Public documentation site, reviewed by security",
                state=state,
                requested_by_user_id=requester,
                requested_at=now - requested_ago,
                expires_at=now + expires_in,
                decided_by_user_id=approver if decided else None,
                decided_at=now - requested_ago / 2 if decided else None,
            )
            .returning(PolicyException.id)
        ).scalar_one()
        db.commit()
        return exception_id


@pytest.fixture
def people(world: World) -> dict[str, int]:
    return {name: user(world.engine, name) for name in ("ana", "bea", "cy")}


# --- Evaluation ------------------------------------------------------------------------------


def test_an_approved_exception_waives_a_backed_failure(
    world: World, people: dict[str, int]
) -> None:
    project_id = world.project(1, "public")
    exception_id = add_exception(world, project_id, people["ana"], approver=people["bea"])

    run = world.run()

    (evaluation,) = world.evaluations(project_id)
    assert (evaluation.outcome, evaluation.exception_id) == ("excepted", exception_id)
    # The waived failure keeps what was expected, what was seen and the evidence.
    assert evaluation.observed == "public"
    assert evaluation.expected == {"allowed": ["private", "internal"]}
    assert evaluation.observation_id is not None
    assert f"Waived by approved exception {exception_id}" in evaluation.reason
    (finding,) = world.findings()
    assert (finding.state, finding.exception_id, finding.latest_outcome) == (
        "excepted",
        exception_id,
        "excepted",
    )
    assert run.counts == {"outcomes": {"excepted": 1}, "findings": {"excepted": 1}}


def test_an_exception_moves_an_open_finding_to_excepted_and_a_pass_resolves_it(
    world: World, people: dict[str, int]
) -> None:
    project_id = world.project(1, "public")
    world.run()
    assert world.findings()[0].state == "open"
    exception_id = add_exception(world, project_id, people["ana"], approver=people["bea"])

    world.run()
    (finding,) = world.findings()
    assert (finding.state, finding.exception_id) == ("excepted", exception_id)

    world.observe(project_id, "private")
    world.run()
    (finding,) = world.findings()
    assert (finding.state, finding.resolution, finding.exception_id) == (
        "resolved",
        "passed",
        None,
    )
    assert world.evaluations(project_id)[-1].outcome == "pass"


@pytest.mark.parametrize("state", ["requested", "rejected"])
def test_only_an_approved_exception_waives(
    world: World, people: dict[str, int], state: str
) -> None:
    project_id = world.project(1, "public")
    add_exception(world, project_id, people["ana"], state=state, approver=people["bea"])

    world.run()

    (evaluation,) = world.evaluations(project_id)
    assert (evaluation.outcome, evaluation.exception_id) == ("fail", None)
    assert world.findings()[0].state == "open"


def test_an_exception_for_another_rule_waives_nothing(world: World, people: dict[str, int]) -> None:
    project_id = world.project(1, "public")
    add_exception(world, project_id, people["ana"], approver=people["bea"], rule_id="other")

    world.run()

    assert world.evaluations(project_id)[0].outcome == "fail"


def test_unknown_is_never_excepted(world: World, people: dict[str, int]) -> None:
    project_id = world.project(1, None)
    add_exception(world, project_id, people["ana"], approver=people["bea"])

    world.run()

    (evaluation,) = world.evaluations(project_id)
    assert (evaluation.outcome, evaluation.exception_id) == ("unknown", None)
    assert world.findings() == []


def test_stale_collection_is_not_excepted_but_keeps_a_waived_finding(
    world: World, people: dict[str, int]
) -> None:
    project_id = world.project(1, "public")
    exception_id = add_exception(world, project_id, people["ana"], approver=people["bea"])
    world.run()
    with Session(world.engine) as db:
        db.execute(text("DELETE FROM collection_run"))
        db.commit()
    world.collection("succeeded", age=timedelta(days=30))

    world.run()

    latest = world.evaluations(project_id)[-1]
    assert (latest.outcome, latest.exception_id) == ("unknown", None)
    assert "collection_stale" in latest.unknown_reasons
    # The exception is still in force, so the finding stays waived until a decided result.
    (finding,) = world.findings()
    assert (finding.state, finding.exception_id, finding.latest_outcome) == (
        "excepted",
        exception_id,
        "unknown",
    )


def test_an_unknown_result_after_the_exception_ended_reopens_the_finding(
    world: World, people: dict[str, int]
) -> None:
    project_id = world.project(1, "public")
    exception_id = add_exception(world, project_id, people["ana"], approver=people["bea"])
    world.run()
    with Session(world.engine) as db:
        # Simulate an exception that ended without the reopening step having run.
        db.execute(text("ALTER TABLE policy_exception DISABLE TRIGGER policy_exception_transition"))
        db.execute(
            update(PolicyException)
            .where(PolicyException.id == exception_id)
            .values(state="revoked", ended_at=func.now(), ended_by_user_id=people["bea"])
        )
        db.execute(text("ALTER TABLE policy_exception ENABLE TRIGGER policy_exception_transition"))
        db.commit()
    world.observe(project_id, None)

    world.run()

    (finding,) = world.findings()
    assert (finding.state, finding.exception_id, finding.latest_outcome) == (
        "open",
        None,
        "unknown",
    )


def test_expiry_ends_the_waiver_on_the_next_run(world: World, people: dict[str, int]) -> None:
    project_id = world.project(1, "public")
    exception_id = add_exception(
        world,
        project_id,
        people["ana"],
        approver=people["bea"],
        requested_ago=timedelta(days=10),
        expires_in=timedelta(seconds=2),
    )
    world.run()
    assert world.findings()[0].state == "excepted"
    with Session(world.engine) as db:
        # Move the expiry into the past (the check only bounds it after the request).
        db.execute(text("ALTER TABLE policy_exception DISABLE TRIGGER policy_exception_transition"))
        db.execute(
            update(PolicyException)
            .where(PolicyException.id == exception_id)
            .values(expires_at=func.now() - timedelta(seconds=1))
        )
        db.execute(text("ALTER TABLE policy_exception ENABLE TRIGGER policy_exception_transition"))
        db.commit()

    run = world.run()

    with Session(world.engine) as db:
        exception = db.get_one(PolicyException, exception_id)
        assert (exception.state, exception.ended_by_user_id) == ("expired", None)
        assert exception.ended_at is not None
        actions = db.scalars(
            select(AuditEvent.action).where(AuditEvent.resource_id == str(exception_id))
        ).all()
    assert "policy_exception.expired" in actions
    latest = world.evaluations(project_id)[-1]
    assert (latest.outcome, latest.exception_id) == ("fail", None)
    (finding,) = world.findings()
    assert (finding.state, finding.exception_id) == ("open", None)
    assert run.counts["findings"] == {"exception_expired": 1}


def test_a_waived_finding_on_a_missing_project_is_still_evaluated(
    world: World, people: dict[str, int]
) -> None:
    project_id = world.project(1, "public")
    add_exception(world, project_id, people["ana"], approver=people["bea"])
    world.run()
    with Session(world.engine) as db:
        db.execute(
            update(GitLabProject).where(GitLabProject.id == project_id).values(state="missing")
        )
        db.commit()

    world.run()

    latest = world.evaluations(project_id)[-1]
    assert (latest.outcome, latest.unknown_reasons) == ("unknown", ["target_missing"])


def test_a_rule_that_no_longer_applies_resolves_a_waived_finding(
    world: World, people: dict[str, int]
) -> None:
    project_id = world.project(1, "public")
    add_exception(world, project_id, people["ana"], approver=people["bea"])
    world.run()
    with Session(world.engine) as db:
        db.execute(text("DELETE FROM policy_assignment"))
        db.commit()

    world.run()

    (finding,) = world.findings()
    assert (finding.state, finding.resolution, finding.exception_id) == (
        "resolved",
        "no_longer_applies",
        None,
    )


# --- Database rules --------------------------------------------------------------------------


def test_what_was_requested_cannot_change(world: World, people: dict[str, int]) -> None:
    project_id = world.project(1, "public")
    exception_id = add_exception(world, project_id, people["ana"], state="requested")

    for values in (
        {"justification": "something else"},
        {"expires_at": datetime.now(UTC) + timedelta(days=60)},
        {"rule_id": "other"},
    ):
        with Session(world.engine) as db, pytest.raises(DBAPIError, match="cannot change"):
            db.execute(
                update(PolicyException).where(PolicyException.id == exception_id).values(**values)
            )


def test_exceptions_cannot_be_deleted(world: World, people: dict[str, int]) -> None:
    project_id = world.project(1, "public")
    exception_id = add_exception(world, project_id, people["ana"], state="requested")

    with Session(world.engine) as db, pytest.raises(DBAPIError, match="cannot be deleted"):
        db.execute(delete(PolicyException).where(PolicyException.id == exception_id))


@pytest.mark.parametrize(
    ("start", "values", "message"),
    [
        ("approved", {"state": "requested"}, "cannot change from approved to requested"),
        ("rejected", {"state": "approved"}, "cannot change from rejected to approved"),
        ("approved", {"state": "rejected"}, "cannot change from approved to rejected"),
        ("approved", {"state": "expired", "ended_at": func.now()}, "cannot expire before"),
        ("approved", {"decision_note": "edited"}, "cannot change from approved to approved"),
    ],
)
def test_only_documented_transitions_are_allowed(
    world: World, people: dict[str, int], start: str, values: dict[str, Any], message: str
) -> None:
    project_id = world.project(1, "public")
    exception_id = add_exception(
        world, project_id, people["ana"], state=start, approver=people["bea"]
    )

    with Session(world.engine) as db, pytest.raises(DBAPIError, match=message):
        db.execute(
            update(PolicyException).where(PolicyException.id == exception_id).values(**values)
        )


def test_an_expired_request_cannot_be_approved(world: World, people: dict[str, int]) -> None:
    project_id = world.project(1, "public")
    exception_id = add_exception(world, project_id, people["ana"], state="requested")
    with Session(world.engine) as db:
        db.execute(text("ALTER TABLE policy_exception DISABLE TRIGGER policy_exception_transition"))
        db.execute(
            update(PolicyException)
            .where(PolicyException.id == exception_id)
            .values(expires_at=func.now() - timedelta(seconds=1))
        )
        db.execute(text("ALTER TABLE policy_exception ENABLE TRIGGER policy_exception_transition"))
        db.commit()

    with Session(world.engine) as db, pytest.raises(DBAPIError, match="expired"):
        db.execute(
            update(PolicyException)
            .where(PolicyException.id == exception_id)
            .values(state="approved", decided_by_user_id=people["bea"], decided_at=func.now())
        )


def test_the_requester_cannot_decide_their_own_request(
    world: World, people: dict[str, int]
) -> None:
    project_id = world.project(1, "public")
    exception_id = add_exception(world, project_id, people["ana"], state="requested")

    with (
        Session(world.engine) as db,
        pytest.raises(IntegrityError, match="ck_policy_exception_decided_by_another_user"),
    ):
        db.execute(
            update(PolicyException)
            .where(PolicyException.id == exception_id)
            .values(state="approved", decided_by_user_id=people["ana"], decided_at=func.now())
        )


def test_expiry_is_bounded(world: World, people: dict[str, int]) -> None:
    project_id = world.project(1, "public")

    with pytest.raises(IntegrityError, match="ck_policy_exception_expiry_bounded"):
        add_exception(
            world,
            project_id,
            people["ana"],
            state="requested",
            requested_ago=timedelta(0),
            expires_in=timedelta(days=366),
        )


def test_one_active_exception_per_project_and_rule(world: World, people: dict[str, int]) -> None:
    project_id = world.project(1, "public")
    add_exception(world, project_id, people["ana"], state="rejected", approver=people["bea"])
    add_exception(world, project_id, people["ana"], state="requested")

    with pytest.raises(IntegrityError, match="uq_policy_exception_active"):
        add_exception(world, project_id, people["cy"], state="approved", approver=people["bea"])


# --- API -------------------------------------------------------------------------------------


def client_as(engine: Engine, user_id: int, *roles: Role) -> TestClient:
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


def body(**overrides: Any) -> dict[str, Any]:
    return {
        "project_gitlab_id": 1,
        "policy_name": POLICY,
        "rule_id": RULE,
        "justification": "Public documentation site, reviewed by security",
        "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        **overrides,
    }


def request(world: World, requester: int) -> dict[str, Any]:
    response = client_as(world.engine, requester, Role.ANALYST).post(
        "/api/v1/exceptions", json=body()
    )
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    return result


def audit_actions(engine: Engine, exception_id: int) -> list[str]:
    with Session(engine) as db:
        return list(
            db.scalars(
                select(AuditEvent.action)
                .where(
                    AuditEvent.resource_type == "policy_exception",
                    AuditEvent.resource_id == str(exception_id),
                )
                .order_by(AuditEvent.id)
            )
        )


def test_request_approve_and_waive_over_the_api(world: World, people: dict[str, int]) -> None:
    world.project(1, "public")
    world.run()

    requested = request(world, people["ana"])

    assert requested["state"] == "requested"
    assert requested["in_force"] is False
    assert requested["project"] == {"gitlab_id": 1, "path": "platform/p1", "state": "active"}
    assert requested["requested_by"] == {"user_id": people["ana"], "display_name": "Ana"}
    approver = client_as(world.engine, people["bea"], Role.ADMIN)
    approved = approver.post(
        f"/api/v1/exceptions/{requested['id']}/approve", json={"note": "Accepted risk"}
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "approved"
    assert approved.json()["in_force"] is True
    assert approved.json()["decided_by"]["user_id"] == people["bea"]
    with Session(world.engine) as db:
        queued = db.scalars(select(CollectionJob.kind).where(CollectionJob.state == "queued"))
        assert EVALUATE in queued.all()

    world.run()

    findings = approver.get("/api/v1/findings?state=excepted").json()["items"]
    (finding,) = findings
    assert finding["state"] == "excepted"
    assert finding["exception"]["id"] == requested["id"]
    assert finding["exception"]["in_force"] is True
    assert finding["latest_evaluation"]["outcome"] == "excepted"
    assert finding["latest_evaluation"]["exception_id"] == requested["id"]
    assert approver.get("/api/v1/findings?state=open").json()["items"] == []
    assert audit_actions(world.engine, requested["id"]) == [
        "policy_exception.requested",
        "policy_exception.approved",
    ]


def test_the_requester_cannot_approve_their_own_request(
    world: World, people: dict[str, int]
) -> None:
    world.project(1, "public")
    requested = request(world, people["ana"])
    self_approver = client_as(world.engine, people["ana"], Role.ADMIN)

    for action, payload in (("approve", {}), ("reject", {"note": "no"})):
        response = self_approver.post(
            f"/api/v1/exceptions/{requested['id']}/{action}", json=payload
        )
        assert response.status_code == 403
        assert "someone else" in response.json()["detail"]
    assert audit_actions(world.engine, requested["id"]) == ["policy_exception.requested"]


@pytest.mark.parametrize(
    ("role", "method", "path"),
    [
        (Role.AUDITOR, "post", "/api/v1/exceptions"),
        (Role.ANALYST, "post", "/api/v1/exceptions/{id}/approve"),
        (Role.ANALYST, "post", "/api/v1/exceptions/{id}/reject"),
        (Role.ANALYST, "post", "/api/v1/exceptions/{id}/revoke"),
        (Role.AUDITOR, "post", "/api/v1/exceptions/{id}/approve"),
    ],
)
def test_permissions(
    world: World, people: dict[str, int], role: Role, method: str, path: str
) -> None:
    world.project(1, "public")
    requested = request(world, people["ana"])
    client = client_as(world.engine, people["bea"], role)

    response = getattr(client, method)(
        path.format(id=requested["id"]), json=body() | {"note": "why"}
    )

    assert response.status_code == 403


def test_every_role_can_read_exceptions(world: World, people: dict[str, int]) -> None:
    world.project(1, "public")
    requested = request(world, people["ana"])

    for role in Role:
        client = client_as(world.engine, people["cy"], role)
        assert client.get(f"/api/v1/exceptions/{requested['id']}").status_code == 200
        assert client.get("/api/v1/exceptions").json()["items"][0]["id"] == requested["id"]
    assert client_as(world.engine, people["cy"]).get("/api/v1/exceptions").status_code == 403


@pytest.mark.parametrize(
    ("overrides", "status_code"),
    [
        ({"project_gitlab_id": 99}, 404),
        ({"policy_name": "unknown-policy"}, 404),
        ({"rule_id": "no-such-rule"}, 404),
        ({"expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()}, 422),
        ({"expires_at": (datetime.now(UTC) + timedelta(days=366)).isoformat()}, 422),
        ({"expires_at": "2030-01-01T00:00:00"}, 422),
        ({"justification": "short"}, 422),
    ],
)
def test_invalid_requests(
    world: World, people: dict[str, int], overrides: dict[str, Any], status_code: int
) -> None:
    world.project(1, "public")

    response = client_as(world.engine, people["ana"], Role.ANALYST).post(
        "/api/v1/exceptions", json=body(**overrides)
    )

    assert response.status_code == status_code, response.text


def test_a_second_active_request_conflicts(world: World, people: dict[str, int]) -> None:
    world.project(1, "public")
    request(world, people["ana"])

    response = client_as(world.engine, people["cy"], Role.ANALYST).post(
        "/api/v1/exceptions", json=body()
    )

    assert response.status_code == 409


def test_reject_needs_a_note_and_is_final(world: World, people: dict[str, int]) -> None:
    world.project(1, "public")
    requested = request(world, people["ana"])
    approver = client_as(world.engine, people["bea"], Role.ADMIN)
    url = f"/api/v1/exceptions/{requested['id']}"

    assert approver.post(f"{url}/reject", json={}).status_code == 422
    rejected = approver.post(f"{url}/reject", json={"note": "Make it private instead"})
    assert rejected.status_code == 200
    assert (rejected.json()["state"], rejected.json()["decision_note"]) == (
        "rejected",
        "Make it private instead",
    )

    assert approver.post(f"{url}/approve", json={}).status_code == 409
    assert approver.post(f"{url}/revoke", json={"note": "x"}).status_code == 409
    # A new request is possible once the earlier one is no longer active.
    assert request(world, people["ana"])["id"] != requested["id"]


def test_revoking_reopens_the_finding_at_once(world: World, people: dict[str, int]) -> None:
    world.project(1, "public")
    requested = request(world, people["ana"])
    approver = client_as(world.engine, people["bea"], Role.ADMIN)
    approver.post(f"/api/v1/exceptions/{requested['id']}/approve", json={})
    world.run()
    assert world.findings()[0].state == "excepted"

    revoked = approver.post(
        f"/api/v1/exceptions/{requested['id']}/revoke", json={"note": "Site now hosts secrets"}
    )

    assert revoked.status_code == 200
    assert revoked.json()["state"] == "revoked"
    assert revoked.json()["ended_by"]["user_id"] == people["bea"]
    (finding,) = world.findings()
    assert (finding.state, finding.exception_id) == ("open", None)
    assert audit_actions(world.engine, requested["id"])[-1] == "policy_exception.revoked"
    world.run()
    assert world.evaluations()[-1].outcome == "fail"


def test_an_exception_past_expiry_reads_as_expired_before_the_next_run(
    world: World, people: dict[str, int]
) -> None:
    project_id = world.project(1, "public")
    exception_id = add_exception(world, project_id, people["ana"], approver=people["bea"])
    world.run()
    with Session(world.engine) as db:
        db.execute(text("ALTER TABLE policy_exception DISABLE TRIGGER policy_exception_transition"))
        db.execute(
            update(PolicyException)
            .where(PolicyException.id == exception_id)
            .values(expires_at=func.now() - timedelta(seconds=1))
        )
        db.execute(text("ALTER TABLE policy_exception ENABLE TRIGGER policy_exception_transition"))
        db.commit()
    reader = client_as(world.engine, people["cy"], Role.AUDITOR)

    exception = reader.get(f"/api/v1/exceptions/{exception_id}").json()
    assert (exception["state"], exception["in_force"]) == ("expired", False)
    assert reader.get("/api/v1/exceptions?state=expired").json()["items"][0]["id"] == exception_id
    assert reader.get("/api/v1/exceptions?state=approved").json()["items"] == []
    # The stored finding still says excepted; the API reports it open.
    with Session(world.engine) as db:
        assert db.scalars(select(Finding.state)).one() == "excepted"
    (finding,) = reader.get("/api/v1/findings?state=open").json()["items"]
    assert finding["state"] == "open"
    assert finding["exception"] == {
        "id": exception_id,
        "expires_at": finding["exception"]["expires_at"],
        "in_force": False,
    }
    assert reader.get("/api/v1/findings?state=excepted").json()["items"] == []
    approver = client_as(world.engine, people["cy"], Role.ADMIN)
    assert (
        approver.post(f"/api/v1/exceptions/{exception_id}/revoke", json={"note": "x"}).status_code
        == 409
    )


def test_unknown_exception_is_404(world: World, people: dict[str, int]) -> None:
    admin = client_as(world.engine, people["bea"], Role.ADMIN)

    assert admin.get("/api/v1/exceptions/999").status_code == 404
    assert admin.post("/api/v1/exceptions/999/approve", json={}).status_code == 404
