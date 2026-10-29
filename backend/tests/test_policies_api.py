import threading
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from oidc_fake import make_settings
from sqlalchemy import Engine, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_policy_schema import BASELINE

from baluarte.auth.dependencies import (
    Principal,
    auth_settings,
    current_principal,
    database_engine,
)
from baluarte.auth.roles import Role
from baluarte.db.models import AuditEvent, Policy
from baluarte.main import create_app
from baluarte.policy.loader import digest
from baluarte.policy.store import save_version

pytestmark = pytest.mark.db

CHANGED = BASELINE.replace("allowed: [private, internal]", "allowed: [private]")
OTHER = BASELINE.replace("name: default-gitlab-baseline", "name: strict")


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


def audit_rows(engine: Engine) -> list[AuditEvent]:
    with Session(engine) as db:
        return list(db.scalars(select(AuditEvent).order_by(AuditEvent.id)))


def policy_rows(engine: Engine) -> list[Policy]:
    with Session(engine) as db:
        return list(db.scalars(select(Policy).order_by(Policy.name, Policy.version)))


def test_validate_accepts_a_valid_baseline(migrated_engine: Engine) -> None:
    response = client_as(migrated_engine, Role.ANALYST).post(
        "/api/v1/policies/validate", json={"source_yaml": BASELINE}
    )

    assert response.status_code == 200
    assert response.json() == {
        "valid": True,
        "errors": [],
        "name": "default-gitlab-baseline",
        "yaml_digest": digest(BASELINE),
        "rule_ids": ["project-visibility", "default-branch-protected"],
    }
    assert policy_rows(migrated_engine) == []
    assert audit_rows(migrated_engine) == []


def test_validate_reports_errors_without_saving(migrated_engine: Engine) -> None:
    source = BASELINE.replace("allowed: [private, internal]", "allowed: [private, secret]")

    response = client_as(migrated_engine, Role.ADMIN).post(
        "/api/v1/policies/validate", json={"source_yaml": source}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert body["errors"] == [
        {
            "path": "spec.rules[0].allowed[1]",
            "message": "Input should be 'private', 'internal' or 'public'",
        }
    ]
    assert body["name"] is None
    assert policy_rows(migrated_engine) == []


def test_create_saves_version_one_and_audits(migrated_engine: Engine) -> None:
    response = client_as(migrated_engine, Role.ANALYST).post(
        "/api/v1/policies", json={"source_yaml": BASELINE}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "default-gitlab-baseline"
    assert body["version"] == 1
    assert body["source_yaml"] == BASELINE
    assert body["yaml_digest"] == digest(BASELINE)
    assert body["document"]["spec"]["rules"][0]["allowed"] == ["private", "internal"]
    [row] = policy_rows(migrated_engine)
    assert (row.source_yaml, row.yaml_digest) == (BASELINE, digest(BASELINE))
    [event] = audit_rows(migrated_engine)
    assert event.actor == "user:1"
    assert event.action == "policy.version_created"
    assert event.resource_type == "policy"
    assert event.resource_id == "default-gitlab-baseline@1"
    assert event.details["version"] == 1
    assert event.details["yaml_digest"] == digest(BASELINE)
    assert "source_yaml" not in event.details


def test_same_name_creates_the_next_version(migrated_engine: Engine) -> None:
    client = client_as(migrated_engine, Role.ADMIN)
    client.post("/api/v1/policies", json={"source_yaml": BASELINE})

    response = client.post("/api/v1/policies", json={"source_yaml": CHANGED})

    assert response.status_code == 201
    assert response.json()["version"] == 2
    rows = policy_rows(migrated_engine)
    assert [(r.version, r.source_yaml) for r in rows] == [(1, BASELINE), (2, CHANGED)]
    assert [e.resource_id for e in audit_rows(migrated_engine)] == [
        "default-gitlab-baseline@1",
        "default-gitlab-baseline@2",
    ]


def test_identical_source_returns_the_latest_version(migrated_engine: Engine) -> None:
    client = client_as(migrated_engine, Role.ADMIN)
    client.post("/api/v1/policies", json={"source_yaml": BASELINE})

    response = client.post("/api/v1/policies", json={"source_yaml": BASELINE})

    assert response.status_code == 200
    assert response.json()["version"] == 1
    assert len(policy_rows(migrated_engine)) == 1
    assert len(audit_rows(migrated_engine)) == 1


def test_reverting_to_an_older_source_is_a_new_version(migrated_engine: Engine) -> None:
    client = client_as(migrated_engine, Role.ADMIN)
    for source in (BASELINE, CHANGED):
        client.post("/api/v1/policies", json={"source_yaml": source})

    response = client.post("/api/v1/policies", json={"source_yaml": BASELINE})

    assert response.status_code == 201
    assert response.json()["version"] == 3


def test_create_rejects_an_invalid_baseline(migrated_engine: Engine) -> None:
    source = BASELINE.replace("required: true", "required: true\n      required: false")

    response = client_as(migrated_engine, Role.ADMIN).post(
        "/api/v1/policies", json={"source_yaml": source}
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "errors": [{"path": "(document)", "message": "duplicate key 'required' (line 15)"}]
        }
    }
    assert policy_rows(migrated_engine) == []
    assert audit_rows(migrated_engine) == []


@pytest.mark.parametrize("path", ["/api/v1/policies", "/api/v1/policies/validate"])
@pytest.mark.parametrize("body", [{}, {"source_yaml": ""}, {"source_yaml": 1}])
def test_malformed_requests_are_rejected(
    migrated_engine: Engine, path: str, body: dict[str, object]
) -> None:
    response = client_as(migrated_engine, Role.ADMIN).post(path, json=body)

    assert response.status_code == 422
    assert policy_rows(migrated_engine) == []


@pytest.mark.parametrize("path", ["/api/v1/policies", "/api/v1/policies/validate"])
@pytest.mark.parametrize("roles", [(Role.AUDITOR,), ()])
def test_writing_needs_policy_write(
    migrated_engine: Engine, path: str, roles: tuple[Role, ...]
) -> None:
    response = client_as(migrated_engine, *roles).post(path, json={"source_yaml": BASELINE})

    assert response.status_code == 403
    assert policy_rows(migrated_engine) == []
    assert audit_rows(migrated_engine) == []


def test_list_and_read_versions(migrated_engine: Engine) -> None:
    writer = client_as(migrated_engine, Role.ANALYST)
    for source in (BASELINE, CHANGED, OTHER):
        writer.post("/api/v1/policies", json={"source_yaml": source})
    reader = client_as(migrated_engine, Role.AUDITOR)

    policies = reader.get("/api/v1/policies").json()
    versions = reader.get("/api/v1/policies/default-gitlab-baseline/versions").json()
    first = reader.get("/api/v1/policies/default-gitlab-baseline/versions/1")

    assert [(p["name"], p["latest_version"], p["versions"]) for p in policies] == [
        ("default-gitlab-baseline", 2, 2),
        ("strict", 1, 1),
    ]
    assert [v["version"] for v in versions] == [2, 1]
    assert "source_yaml" not in versions[0]
    assert first.status_code == 200
    assert first.json()["source_yaml"] == BASELINE
    assert first.json()["yaml_digest"] == digest(BASELINE)
    limited = reader.get("/api/v1/policies/default-gitlab-baseline/versions?limit=1").json()
    assert [v["version"] for v in limited] == [2]


def test_empty_list(migrated_engine: Engine) -> None:
    assert client_as(migrated_engine, Role.AUDITOR).get("/api/v1/policies").json() == []


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/v1/policies/missing/versions", 404),
        ("/api/v1/policies/default-gitlab-baseline/versions/9", 404),
        ("/api/v1/policies/default-gitlab-baseline/versions/0", 422),
        ("/api/v1/policies/Not_A_Name/versions", 422),
    ],
)
def test_missing_or_bad_lookups(migrated_engine: Engine, path: str, expected: int) -> None:
    client_as(migrated_engine, Role.ADMIN).post("/api/v1/policies", json={"source_yaml": BASELINE})

    assert client_as(migrated_engine, Role.AUDITOR).get(path).status_code == expected


def test_reading_needs_findings_read(migrated_engine: Engine) -> None:
    client = client_as(migrated_engine)

    assert client.get("/api/v1/policies").status_code == 403
    assert client.get("/api/v1/policies/x/versions").status_code == 403
    assert client.get("/api/v1/policies/x/versions/1").status_code == 403


def test_saved_versions_cannot_be_updated(migrated_engine: Engine) -> None:
    client_as(migrated_engine, Role.ADMIN).post("/api/v1/policies", json={"source_yaml": BASELINE})

    with Session(migrated_engine) as db, pytest.raises(DBAPIError, match="immutable evidence"):
        db.execute(update(Policy).values(source_yaml=CHANGED, yaml_digest=digest(CHANGED)))


def test_concurrent_saves_get_distinct_versions(migrated_engine: Engine) -> None:
    sources = [
        BASELINE.replace("required: true", f"required: true\n      severity: {s}")
        for s in ("low", "high", "critical", "medium")
    ]
    barrier = threading.Barrier(len(sources))
    failures: list[BaseException] = []

    def save(source: str) -> None:
        try:
            with Session(migrated_engine) as db:
                barrier.wait()
                save_version(db, source)
                db.commit()
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=save, args=(s,)) for s in sources]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert sorted(r.version for r in policy_rows(migrated_engine)) == [1, 2, 3, 4]
