"""The operating profile: settings, exceptions, attestations, posture and retention."""

import hashlib
import secrets
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from oidc_fake import make_settings
from sqlalchemy import Engine, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from baluarte.auth.dependencies import (
    Principal,
    auth_settings,
    current_principal,
    database_engine,
)
from baluarte.auth.roles import Role
from baluarte.compliance import posture
from baluarte.compliance.catalogue import CONTROLS, FRAMEWORK_IDS, minimum
from baluarte.compliance.retention import RETENTION_EXPIRE, expire
from baluarte.configuration import effective
from baluarte.db.models import (
    AppUser,
    AuditEvent,
    CollectionJob,
    ComplianceAttestation,
    ComplianceException,
    GitLabInstance,
    GitLabProject,
    Observation,
    UserSession,
)
from baluarte.main import create_app
from baluarte.settings import ConfigurationError

CSRF = {"X-CSRF-Token": "csrf"}

SETTINGS = {
    "audit_retention_days": 365,
    "evidence_retention_days": 365,
    "job_retention_days": 90,
    "session_idle_minutes": 30,
    "session_lifetime_minutes": 480,
    "require_review": True,
    "legal_hold": False,
    "backup_interval_hours": 24,
}


def utc_today() -> date:
    return datetime.now(UTC).date()


def body(frameworks: list[str], **settings: Any) -> dict[str, Any]:
    return {"frameworks": frameworks, "settings": SETTINGS | settings}


# The catalogue, without a database


def test_every_control_maps_to_every_framework() -> None:
    for control in CONTROLS:
        assert set(control.mappings) == set(FRAMEWORK_IDS), control.id
        assert all(control.mappings.values()), control.id
        if control.kind == "attestation":
            assert control.attestation_days is not None, control.id
        if control.kind == "setting":
            assert control.attestation_days is None, control.id
        if control.kind == "check":
            assert control.id in posture.CHECKS, control.id
        assert (control.kind == "setting") == (control.setting is not None)


def test_the_strictest_requirement_wins() -> None:
    idle = minimum("session_idle_minutes", ["soc2", "nist_800_53_moderate"])
    assert idle is not None
    assert (idle.op, idle.value, idle.frameworks) == ("<=", 15, ("nist_800_53_moderate",))
    retention = minimum("audit_retention_days", ["soc2", "iso27001"])
    assert retention is not None
    assert (retention.op, retention.value, retention.frameworks) == (
        ">=",
        365,
        ("soc2", "iso27001"),
    )
    assert minimum("session_idle_minutes", []) is None
    assert minimum("job_retention_days", FRAMEWORK_IDS) is None


# Through the API


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


@pytest.fixture
def engine(migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    for name in ("BALUARTE_SESSION_IDLE_MINUTES", "BALUARTE_SESSION_LIFETIME_MINUTES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(effective, "engine_provider", lambda: migrated_engine)
    posture.reset_audit_cache()
    yield migrated_engine
    posture.reset_audit_cache()


def actions(engine: Engine) -> list[str]:
    with Session(engine) as db:
        return list(db.scalars(select(AuditEvent.action).order_by(AuditEvent.id)))


def statuses(response: Any) -> dict[str, str]:
    return {item["id"]: item["status"] for item in response.json()["controls"]}


@pytest.mark.db
def test_the_defaults_until_a_profile_is_saved(engine: Engine) -> None:
    profile = client(engine, 1, Role.AUDITOR).get("/api/v1/compliance/profile")

    assert profile.status_code == 200
    assert profile.json() == {
        "frameworks": [],
        "configured": False,
        "settings": SETTINGS | {"session_idle_minutes": 60, "require_review": False},
        "sources": dict.fromkeys(SETTINGS, "default"),
        "minimums": {},
        "updated_by": None,
        "updated_at": None,
    }
    result = client(engine, 1, Role.AUDITOR).get("/api/v1/compliance/posture")
    assert result.status_code == 200
    assert result.json()["configured"] is False
    # Nothing is enforced without a framework; each control's own result is kept.
    assert set(statuses(result).values()) == {"not_applicable"}
    assert result.json()["summary"]["not_applicable"] == len(CONTROLS)
    by_id = {item["id"]: item for item in result.json()["controls"]}
    assert by_id["session-idle"]["evidence"]["result"] == "met"


@pytest.mark.db
def test_reading_needs_compliance_read_and_changing_needs_settings_manage(engine: Engine) -> None:
    analyst = client(engine, 1, Role.ANALYST)
    auditor = client(engine, 2, Role.AUDITOR)

    for path in ("catalogue", "profile", "posture", "exceptions", "attestations"):
        assert analyst.get(f"/api/v1/compliance/{path}").status_code == 403
        assert auditor.get(f"/api/v1/compliance/{path}").status_code == 200
    assert auditor.put("/api/v1/compliance/profile", json=body(["soc2"])).status_code == 403
    assert (
        auditor.post(
            "/api/v1/compliance/attestations",
            json={"control_id": "backup-offsite", "statement": "Backed up nightly"},
        ).status_code
        == 403
    )


@pytest.mark.db
def test_saving_a_profile_sets_the_settings_and_review(engine: Engine) -> None:
    admin = client(engine, 1, Role.ADMIN)

    saved = admin.put(
        "/api/v1/compliance/profile",
        json=body(["soc2", "nist_800_53_moderate"], session_idle_minutes=15),
    )

    assert saved.status_code == 200, saved.text
    assert saved.json()["frameworks"] == ["soc2", "nist_800_53_moderate"]
    assert saved.json()["configured"] is True
    assert saved.json()["updated_by"] == "user:1"
    assert saved.json()["minimums"]["session_idle_minutes"] == {
        "op": "<=",
        "value": 15,
        "frameworks": ["nist_800_53_moderate"],
    }
    assert saved.json()["sources"]["require_review"] == "database"
    assert admin.get("/api/v1/settings/governance").json() == {"require_review": True}
    assert actions(engine)[-2:] == ["settings.governance_updated", "settings.compliance_updated"]
    with Session(engine) as db:
        event = db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc())).first()
        assert event is not None
        assert event.details["previous"]["configured"] is False
        assert event.details["settings"]["session_idle_minutes"] == 15
    # Sessions follow the profile.
    limited = effective._with_session_limits(make_settings())
    assert limited is not None
    assert limited.session_idle == timedelta(minutes=15)
    assert limited.session_lifetime == timedelta(minutes=480)


@pytest.mark.db
def test_a_value_below_the_minimum_is_refused(engine: Engine) -> None:
    admin = client(engine, 1, Role.ADMIN)

    refused = admin.put(
        "/api/v1/compliance/profile",
        json=body(["nist_800_53_moderate"], session_idle_minutes=30, require_review=False),
    )

    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["code"] == "below_minimum"
    assert detail["violations"] == [
        {
            "control_id": "session-idle",
            "setting": "session_idle_minutes",
            "value": 30,
            "required": {"op": "<=", "value": 15},
            "frameworks": ["nist_800_53_moderate"],
        },
        {
            "control_id": "change-review",
            "setting": "require_review",
            "value": False,
            "required": {"op": "==", "value": True},
            "frameworks": ["nist_800_53_moderate"],
        },
    ]
    assert "settings.compliance_updated" not in actions(engine)


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"audit_retention_days": 7}, "between 30 and 3650"),
        ({"session_idle_minutes": 600}, "cannot exceed"),
        ({"legal_hold": "yes"}, "true or false"),
        ({"job_retention_days": 1.5}, "whole number"),
    ],
)
@pytest.mark.db
def test_invalid_values_are_rejected(
    engine: Engine, settings: dict[str, Any], message: str
) -> None:
    response = client(engine, 1, Role.ADMIN).put(
        "/api/v1/compliance/profile", json=body([], **settings)
    )

    assert response.status_code == 422
    assert message in response.json()["detail"]


@pytest.mark.db
def test_a_setting_the_environment_fixes_cannot_change(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BALUARTE_SESSION_IDLE_MINUTES", "20")
    admin = client(engine, 1, Role.ADMIN)

    refused = admin.put("/api/v1/compliance/profile", json=body(["soc2"]))
    kept = admin.put("/api/v1/compliance/profile", json=body(["soc2"], session_idle_minutes=20))
    # Below NIST's 15, but the environment decides: shown in the posture, not refused.
    nist = admin.put(
        "/api/v1/compliance/profile",
        json=body(["nist_800_53_moderate"], session_idle_minutes=20),
    )

    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "deployment_setting"
    assert refused.json()["detail"]["settings"] == ["session_idle_minutes"]
    assert kept.status_code == 200
    assert kept.json()["sources"]["session_idle_minutes"] == "deployment"
    assert nist.status_code == 200
    item = next(
        c
        for c in admin.get("/api/v1/compliance/posture").json()["controls"]
        if c["id"] == "session-idle"
    )
    assert item["status"] == "not_met"
    assert "environment sets it" in item["detail"]


@pytest.mark.db
def test_an_exception_lets_a_setting_go_below_the_minimum(engine: Engine) -> None:
    admin = client(engine, 1, Role.ADMIN)
    assert (
        admin.put(
            "/api/v1/compliance/profile", json=body(["soc2"], require_review=False)
        ).status_code
        == 409
    )
    assert admin.put("/api/v1/compliance/profile", json=body(["soc2"])).status_code == 200
    # Review is on now, so the exception needs a second person.
    expires = (utc_today() + timedelta(days=30)).isoformat()

    requested = admin.post(
        "/api/v1/compliance/exceptions",
        json={
            "control_id": "change-review",
            "justification": "Only one administrator until the second hire starts",
            "expires_on": expires,
        },
    )
    assert requested.status_code == 201, requested.text
    exception = requested.json()
    assert exception["state"] == "proposed"
    assert exception["in_force"] is False
    assert exception["requested_by"] == "user:1"
    own = admin.post(
        f"/api/v1/compliance/exceptions/{exception['id']}/approve", json={"note": None}
    )
    assert own.status_code == 409
    assert own.json()["detail"]["code"] == "not_allowed"
    assert (
        admin.put("/api/v1/settings/governance", json={"require_review": False}).status_code == 409
    )

    approved = client(engine, 2, Role.ADMIN).post(
        f"/api/v1/compliance/exceptions/{exception['id']}/approve", json={"note": None}
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["in_force"] is True
    assert approved.json()["decided_by"] == "user:2"
    assert approved.json()["self_approved"] is False

    turned_off = admin.put("/api/v1/settings/governance", json={"require_review": False})
    assert turned_off.status_code == 200
    controls = {c["id"]: c for c in admin.get("/api/v1/compliance/posture").json()["controls"]}
    assert controls["change-review"]["status"] == "excepted"
    assert controls["change-review"]["exception"] == {"id": exception["id"], "expires_on": expires}

    revoked = admin.post(f"/api/v1/compliance/exceptions/{exception['id']}/revoke", json={})
    assert revoked.status_code == 200
    assert revoked.json()["state"] == "revoked"
    after = {
        c["id"]: c["status"] for c in admin.get("/api/v1/compliance/posture").json()["controls"]
    }
    assert after["change-review"] == "not_met"
    assert [a for a in actions(engine) if a.startswith("compliance.")] == [
        "compliance.exception_requested",
        "compliance.exception_approved",
        "compliance.exception_revoked",
    ]


@pytest.mark.db
def test_without_review_an_exception_is_self_approved(engine: Engine) -> None:
    admin = client(engine, 1, Role.ADMIN)

    response = admin.post(
        "/api/v1/compliance/exceptions",
        json={
            "control_id": "tls-database",
            "justification": "Database on the same host; TLS planned for Q1",
            "expires_on": (utc_today() + timedelta(days=60)).isoformat(),
        },
    )

    assert response.status_code == 201
    assert response.json()["state"] == "approved"
    assert response.json()["self_approved"] is True
    assert response.json()["decided_by"] == "user:1"
    assert response.json()["in_force"] is True


@pytest.mark.parametrize(
    ("overrides", "status_code"),
    [
        ({"control_id": "no-such-control"}, 422),
        ({"expires_on": utc_today().isoformat()}, 422),
        ({"expires_on": (utc_today() + timedelta(days=400)).isoformat()}, 422),
        ({"justification": "short"}, 422),
    ],
)
@pytest.mark.db
def test_exception_requests_are_checked(
    engine: Engine, overrides: dict[str, Any], status_code: int
) -> None:
    request = {
        "control_id": "backups",
        "justification": "Backups move to the new provider next month",
        "expires_on": (utc_today() + timedelta(days=30)).isoformat(),
    } | overrides

    response = client(engine, 1, Role.ADMIN).post("/api/v1/compliance/exceptions", json=request)

    assert response.status_code == status_code


@pytest.mark.db
def test_rejecting_needs_a_note_and_another_person(engine: Engine) -> None:
    admin = client(engine, 1, Role.ADMIN)
    assert admin.put("/api/v1/compliance/profile", json=body(["iso27001"])).status_code == 200
    exception = admin.post(
        "/api/v1/compliance/exceptions",
        json={
            "control_id": "tls-web",
            "justification": "Internal only during the pilot",
            "expires_on": (utc_today() + timedelta(days=10)).isoformat(),
        },
    ).json()
    other = client(engine, 2, Role.ADMIN)

    assert (
        other.post(
            f"/api/v1/compliance/exceptions/{exception['id']}/reject", json={"note": None}
        ).status_code
        == 422
    )
    rejected = other.post(
        f"/api/v1/compliance/exceptions/{exception['id']}/reject", json={"note": "Use TLS now"}
    )
    assert rejected.status_code == 200
    assert rejected.json()["state"] == "rejected"
    assert rejected.json()["note"] == "Use TLS now"
    again = other.post(f"/api/v1/compliance/exceptions/{exception['id']}/approve", json={})
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "conflict"


@pytest.mark.db
def test_the_database_enforces_the_exception_lifecycle(engine: Engine) -> None:
    with Session(engine) as db:
        db.add(
            ComplianceException(
                control_id="backups",
                justification="Restore test scheduled next week",
                expires_on=utc_today() + timedelta(days=5),
                state="proposed",
                review_required=True,
                self_approved=False,
                requested_by="user:1",
                requested_by_user_id=1,
            )
        )
        db.commit()
    for statement in (
        "UPDATE compliance_exception SET state = 'approved', decided_by = 'user:1', "
        "decided_by_user_id = 1, decided_at = now()",
        "UPDATE compliance_exception SET justification = 'Something else entirely'",
        "DELETE FROM compliance_exception",
    ):
        with pytest.raises(DBAPIError), engine.begin() as connection:
            connection.execute(text(statement))
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE compliance_exception SET state = 'approved', decided_by = 'user:2', "
                "decided_by_user_id = 2, decided_at = now()"
            )
        )
    with pytest.raises(DBAPIError), engine.begin() as connection:
        connection.execute(text("UPDATE compliance_exception SET state = 'rejected'"))


@pytest.mark.db
def test_attestations(engine: Engine) -> None:
    admin = client(engine, 1, Role.ADMIN)
    assert admin.put("/api/v1/compliance/profile", json=body(["soc2"])).status_code == 200

    refused = admin.post(
        "/api/v1/compliance/attestations",
        json={"control_id": "tls-web", "statement": "We promise it is TLS"},
    )
    attested = admin.post(
        "/api/v1/compliance/attestations",
        json={
            "control_id": "backup-offsite",
            "statement": "Backups copied nightly to versioned object storage",
        },
    )

    assert refused.status_code == 422
    assert attested.status_code == 201
    row = attested.json()
    assert row["attested_by"] == "user:1"
    expires = datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["attested_at"])
    assert expires == timedelta(days=90)
    controls = {c["id"]: c for c in admin.get("/api/v1/compliance/posture").json()["controls"]}
    assert controls["backup-offsite"]["status"] == "met"
    assert controls["backup-offsite"]["attestation"]["id"] == row["id"]
    assert controls["identity-mfa"]["status"] == "needs_attestation"
    listed = admin.get("/api/v1/compliance/attestations", params={"control_id": "backup-offsite"})
    assert [a["id"] for a in listed.json()] == [row["id"]]
    assert "compliance.attested" in actions(engine)
    # An expired attestation no longer counts; rows cannot be changed.
    with Session(engine) as db:
        db.add(
            ComplianceAttestation(
                control_id="identity-mfa",
                statement="MFA enforced in Okta",
                attested_by="user:1",
                attested_by_user_id=1,
                attested_at=datetime.now(UTC) - timedelta(days=400),
                expires_at=datetime.now(UTC) - timedelta(days=35),
            )
        )
        db.commit()
    controls = {c["id"]: c for c in admin.get("/api/v1/compliance/posture").json()["controls"]}
    assert controls["identity-mfa"]["status"] == "needs_attestation"
    assert "expired" in controls["identity-mfa"]["detail"]
    with pytest.raises(DBAPIError), engine.begin() as connection:
        connection.execute(text("UPDATE compliance_attestation SET statement = 'changed text'"))


# Checks


def context(**overrides: Any) -> posture.Context:
    report = SimpleNamespace(status="fresh", alert=None, population=None)
    values: dict[str, Any] = {
        "public_url": lambda: "https://baluarte.example.test",
        "gitlab_url": lambda: "https://gitlab.example.test",
        "database_ssl_mode": lambda: "verify-full",
        "secret_key_configured": lambda: True,
        "freshness": lambda _db: report,
    } | overrides
    return posture.Context(**values)


def results(engine: Engine, ctx: posture.Context) -> dict[str, posture.ControlPosture]:
    with Session(engine) as db:
        return {item.control.id: item for item in posture.evaluate(db, ctx).controls}


def save_profile(engine: Engine, frameworks: list[str]) -> None:
    response = client(engine, 1, Role.ADMIN).put(
        "/api/v1/compliance/profile", json=body(frameworks)
    )
    assert response.status_code == 200, response.text


@pytest.mark.db
def test_url_and_key_checks(engine: Engine) -> None:
    save_profile(engine, ["soc2"])

    good = results(engine, context())
    bad = results(
        engine,
        context(
            public_url=lambda: "http://localhost:8080",
            gitlab_url=lambda: None,
            secret_key_configured=lambda: False,
        ),
    )

    for control in ("tls-web", "tls-gitlab", "secrets-sealed"):
        assert good[control].status == "met", control
        assert bad[control].status == "not_met", control
    assert bad["tls-gitlab"].detail == "The GitLab URL is not configured."
    # The test database connection is not encrypted, whatever the URL says.
    assert good["tls-database"].status == "not_met"
    assert good["tls-database"].evidence["connection_encrypted"] is False


@pytest.mark.db
def test_a_check_that_cannot_run_is_not_met(engine: Engine) -> None:
    save_profile(engine, ["soc2"])

    def broken(_db: Session) -> Any:
        raise ConfigurationError("BALUARTE_GITLAB_URL is invalid")

    found = results(engine, context(freshness=broken))

    assert found["collection-health"].status == "not_met"
    assert "could not run" in found["collection-health"].detail
    # The failure did not stop the other checks.
    assert found["tls-web"].status == "met"


@pytest.mark.db
def test_audit_integrity_detects_a_changed_event(engine: Engine) -> None:
    save_profile(engine, ["soc2"])
    assert results(engine, context())["audit-integrity"].status == "met"
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE audit_event DISABLE TRIGGER audit_event_immutable"))
        changed = connection.execute(
            text("UPDATE audit_event SET details = '{\"forged\": true}' WHERE id = 1 RETURNING id")
        ).scalar_one()
        connection.execute(text("ALTER TABLE audit_event ENABLE TRIGGER audit_event_immutable"))
    posture.reset_audit_cache()

    found = results(engine, context())["audit-integrity"]

    assert found.status == "not_met"
    assert found.evidence["invalid_ids"] == [changed]


@pytest.fixture
def app_role(engine: Engine) -> Iterator[str]:
    """A role like the one baluarte-migrate creates: data access, insert-only audit trail."""
    name = f"baluarte_app_{secrets.token_hex(4)}"
    with engine.begin() as connection:
        connection.execute(text(f"CREATE ROLE {name} NOLOGIN"))
        connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {name}"))
        connection.execute(text(f"GRANT SELECT, INSERT ON audit_event TO {name}"))
        connection.execute(
            text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON app_setting, collection_job TO {name}")
        )
        connection.execute(text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {name}"))
    yield name
    with engine.begin() as connection:
        connection.execute(text(f"DROP OWNED BY {name}"))
        connection.execute(text(f"DROP ROLE {name}"))


@pytest.mark.db
def test_audit_protection_is_met_only_for_a_data_only_role(engine: Engine, app_role: str) -> None:
    ctx = context()
    with Session(engine) as db:
        owner = posture._audit_protection(db, ctx)
    with Session(engine) as db:
        db.execute(text(f"SET LOCAL ROLE {app_role}"))
        restricted = posture._audit_protection(db, ctx)
        # The role cannot delete an event, even claiming to be the retention function.
        db.execute(text("SELECT set_config('baluarte.audit_expiry', 'on', true)"))
        with pytest.raises(DBAPIError):
            db.execute(text("DELETE FROM audit_event"))

    assert owner.status == "not_met"
    assert restricted.status == "met"
    assert restricted.evidence == {
        "role": app_role,
        "superuser": False,
        "owns_audit_table": False,
        "update": False,
        "delete": False,
        "truncate": False,
    }


# Retention


def old_event(db: Session, days: int, action: str = "old.event") -> AuditEvent:
    from baluarte.audit import details_digest

    details = {"n": days}
    row = AuditEvent(
        actor="system:test",
        action=action,
        resource_type="test",
        details=details,
        details_digest=details_digest(details),
        occurred_at=func.now() - timedelta(days=days),
    )
    db.add(row)
    db.flush()
    return row


@pytest.mark.db
def test_retention_deletes_only_what_has_expired(engine: Engine) -> None:
    save_profile(engine, ["soc2"])
    with Session(engine) as db:
        expired = [old_event(db, 400), old_event(db, 380)]
        kept_event = old_event(db, 300)
        instance = GitLabInstance(base_url="https://gitlab.example.test")
        db.add(instance)
        db.flush()
        project = GitLabProject(
            instance_id=instance.id,
            gitlab_id=1,
            namespace_gitlab_id=1,
            path_with_namespace="g/p",
            name="p",
        )
        db.add(project)
        db.flush()

        def observe(days: int) -> int:
            row = Observation(
                instance_id=instance.id,
                project_id=project.id,
                source="GET /projects/:id",
                collected_at=func.now() - timedelta(days=days),
                collection_status="ok",
            )
            db.add(row)
            db.flush()
            return row.id

        superseded, newest_old = observe(500), observe(450)
        finished = CollectionJob(
            kind="gitlab.discover_projects",
            idempotency_key="old-finished",
            state="succeeded",
            created_at=func.now() - timedelta(days=100),
            finished_at=func.now() - timedelta(days=100),
        )
        queued = CollectionJob(
            kind="gitlab.discover_projects",
            idempotency_key="old-queued",
            created_at=func.now() - timedelta(days=100),
        )
        db.add_all([finished, queued])
        user = AppUser(issuer="https://idp.example.test", subject="s")
        db.add(user)
        db.flush()
        db.add(
            UserSession(
                token_digest="a" * 64,
                user_id=user.id,
                roles=["admin"],
                created_at=func.now() - timedelta(days=40),
                expires_at=func.now() - timedelta(days=39),
            )
        )
        db.commit()
        expired_digests = [row.details_digest for row in expired]
        expired_ids = [row.id for row in expired]
        ids = {"kept_event": kept_event.id, "finished": finished.id, "queued": queued.id}

        details = expire(db)
        db.commit()

        assert details["legal_hold"] is False
        assert details["deleted"]["observation"] == 1
        assert details["deleted"]["collection_job"] == 1
        assert details["deleted"]["user_session"] == 1
        assert details["deleted"]["audit_event"] == 2
        assert details["audit_events"] == {
            "deleted": 2,
            "first_id": expired_ids[0],
            "last_id": expired_ids[1],
            "digest": hashlib.sha256("".join(expired_digests).encode()).hexdigest(),
        }
        observations = set(db.scalars(select(Observation.id)))
        assert superseded not in observations
        assert newest_old in observations
        jobs = set(db.scalars(select(CollectionJob.id)))
        assert ids["finished"] not in jobs
        assert ids["queued"] in jobs
        events = set(db.scalars(select(AuditEvent.id)))
        assert ids["kept_event"] in events
        assert not events & set(expired_ids)
        last = db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc())).first()
        assert last is not None
        assert (last.actor, last.action) == ("system:retention", "retention.expired")
        assert last.details["audit_events"]["digest"] == details["audit_events"]["digest"]


@pytest.mark.db
def test_legal_hold_deletes_nothing(engine: Engine) -> None:
    response = client(engine, 1, Role.ADMIN).put(
        "/api/v1/compliance/profile", json=body(["soc2"], legal_hold=True)
    )
    assert response.status_code == 200
    with Session(engine) as db:
        old = old_event(db, 900).id
        db.commit()
        details = expire(db)
        db.commit()
        assert details["legal_hold"] is True
        assert details["deleted"] == {}
        assert db.get(AuditEvent, old) is not None


@pytest.mark.db
def test_the_expiry_function_is_the_only_way_to_delete_events(
    engine: Engine, app_role: str
) -> None:
    with Session(engine) as db:
        old_event(db, 400)
        db.commit()
    # Recent events can never be expired, whatever the caller asks.
    with pytest.raises(DBAPIError), engine.begin() as connection:
        connection.execute(text("SELECT * FROM baluarte_expire_audit(now() - interval '1 day')"))
    # Without EXECUTE, the application role cannot run it either.
    with pytest.raises(DBAPIError), engine.begin() as connection:
        connection.execute(text(f"SET LOCAL ROLE {app_role}"))
        connection.execute(text("SELECT * FROM baluarte_expire_audit(now() - interval '1 year')"))
    with engine.begin() as connection:
        connection.execute(
            text(f"GRANT EXECUTE ON FUNCTION baluarte_expire_audit(timestamptz) TO {app_role}")
        )
    with engine.begin() as connection:
        connection.execute(text(f"SET LOCAL ROLE {app_role}"))
        row = connection.execute(
            text("SELECT * FROM baluarte_expire_audit(now() - interval '1 year')")
        ).one()
    assert row.deleted == 1


@pytest.mark.db
def test_retention_running_follows_the_job(engine: Engine) -> None:
    save_profile(engine, ["soc2"])
    assert results(engine, context())["retention-running"].status == "not_met"
    with Session(engine) as db:
        db.execute(
            insert(CollectionJob).values(
                kind=RETENTION_EXPIRE,
                idempotency_key="retention.expire:1",
                state="succeeded",
                finished_at=func.now() - timedelta(hours=3),
            )
        )
        db.commit()

    assert results(engine, context())["retention-running"].status == "met"

    with Session(engine) as db:
        db.execute(update(CollectionJob).values(finished_at=func.now() - timedelta(hours=49)))
        db.commit()
    assert results(engine, context())["retention-running"].status == "not_met"


@pytest.mark.db
def test_the_export_manifest_states_the_operating_profile(engine: Engine) -> None:
    save_profile(engine, ["iso27001"])
    with Session(engine) as db:
        summary = posture.operations_summary(db, context())

    assert summary["configured"] is True
    assert summary["frameworks"] == ["iso27001"]
    assert summary["settings"]["require_review"] is True
    assert sum(summary["posture"]["summary"].values()) == len(CONTROLS)
    assert {c["id"] for c in summary["posture"]["controls"]} == {c.id for c in CONTROLS}
    assert summary["exceptions_in_force"] == []
