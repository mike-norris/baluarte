"""First-run setup and settings entered in the web UI (BAL-034, ADR-0008), against PostgreSQL."""

import base64
import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import httpx
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from gitlab_fake import FakeGitLab
from oidc_fake import CLIENT_ID, ISSUER, PUBLIC_URL, FakeIdP, make_settings
from sqlalchemy import Engine, select, text, update
from sqlalchemy.orm import Session
from test_rbac import SETUP_ROUTES

from baluarte.api.v1.gitlab import gitlab_transport
from baluarte.api.v1.setup import oidc_transport
from baluarte.auth.dependencies import (
    CSRF_HEADER,
    Principal,
    auth_settings,
    current_principal,
    database_engine,
    oidc_client,
)
from baluarte.auth.oidc import OidcClient
from baluarte.auth.roles import Role
from baluarte.auth.settings import AuthSettings
from baluarte.configuration import cli, effective
from baluarte.configuration.crypto import SecretUnavailable
from baluarte.configuration.setup import (
    FAILURE_WINDOW,
    issue_code,
    issue_startup_code,
    sha256_hex,
)
from baluarte.db.models import AuditEvent, SetupSession, SetupState, StoredSecret
from baluarte.integrations.gitlab.settings import load_gitlab_settings
from baluarte.main import create_app

pytestmark = pytest.mark.db

SETUP_COOKIE = "__Host-baluarte_setup"
CLIENT_SECRET = "setup-client-secret-1"
GITLAB_URL = "https://gitlab.example.test"
GITLAB_TOKEN = "glpat-setuptesttoken000001"


def oidc_body(**overrides: Any) -> dict[str, Any]:
    return {
        "public_url": PUBLIC_URL,
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "admin_groups": ["baluarte-admins", " ", "baluarte-admins"],
        "analyst_groups": ["baluarte-analysts"],
    } | overrides


def gitlab_body(**overrides: Any) -> dict[str, Any]:
    return {"url": GITLAB_URL, "token": GITLAB_TOKEN, "sync_interval_minutes": 60} | overrides


def write_key(path: Path) -> str:
    key = base64.b64encode(os.urandom(32)).decode()
    path.write_text(key + "\n")
    return key


@pytest.fixture
def secret_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in ("BALUARTE_PUBLIC_URL", "BALUARTE_OIDC_ISSUER", "BALUARTE_OIDC_CLIENT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("BALUARTE_GITLAB_URL", raising=False)
    monkeypatch.delenv("BALUARTE_SECRET_KEY_PREVIOUS_FILE", raising=False)
    key = tmp_path / "secret-key"
    write_key(key)
    monkeypatch.setenv("BALUARTE_SECRET_KEY_FILE", str(key))
    return key


class Harness:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.idp = FakeIdP()
        self.gitlab = FakeGitLab()
        self.csrf = ""
        idp = self.idp

        def fake_oidc_client(
            settings: Annotated[AuthSettings, Depends(auth_settings)],
        ) -> OidcClient:
            return idp.client(settings)

        app = create_app()
        app.dependency_overrides[database_engine] = lambda: engine
        app.dependency_overrides[oidc_client] = fake_oidc_client
        app.dependency_overrides[oidc_transport] = lambda: httpx.MockTransport(idp)
        app.dependency_overrides[gitlab_transport] = lambda: self.gitlab.transport
        self.app = app
        self.client = TestClient(app, base_url=PUBLIC_URL, follow_redirects=False)

    def code(self) -> str:
        with Session(self.engine) as db:
            code = issue_code(db)
            db.commit()
        assert code is not None
        return code

    def start(self) -> httpx.Response:
        response = self.client.post("/api/v1/setup/session", json={"code": self.code()})
        assert response.status_code == 200, response.text
        self.csrf = response.json()["csrf_token"]
        return response

    def status(self) -> dict[str, Any]:
        response = self.client.get("/api/v1/setup/status")
        assert response.status_code == 200
        body: dict[str, Any] = response.json()
        return body

    def put(self, path: str, body: dict[str, Any]) -> httpx.Response:
        return self.client.put(path, json=body, headers={CSRF_HEADER: self.csrf})

    def post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        return self.client.post(path, json=body, headers={CSRF_HEADER: self.csrf})

    def sign_in(self, *groups: str) -> httpx.Response:
        self.idp.claims["groups"] = list(groups)
        response = self.client.get("/api/v1/auth/login")
        assert response.status_code == 302, response.text
        query = self.idp.authorize(response.headers["location"])
        return self.client.get(
            "/api/v1/auth/callback", params={"state": query["state"], "code": self.idp.code}
        )

    def audit(self) -> list[tuple[str, str, dict[str, Any]]]:
        with Session(self.engine) as db:
            rows = db.scalars(select(AuditEvent).order_by(AuditEvent.id))
            return [(row.actor, row.action, row.details) for row in rows]

    def dump(self) -> str:
        with self.engine.connect() as connection:
            return " ".join(
                str(value)
                for table in ("app_setting", "app_secret", "audit_event", "setup_state")
                for row in connection.execute(text(f"SELECT * FROM {table}"))  # noqa: S608
                for value in row
            )


@pytest.fixture
def harness(
    migrated_engine: Engine, secret_key: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Harness]:
    monkeypatch.setattr(effective, "engine_provider", lambda: migrated_engine)
    harness = Harness(migrated_engine)
    yield harness
    harness.client.close()


# State and the setup code


def test_a_new_installation_needs_setup(harness: Harness) -> None:
    assert harness.status() == {
        "state": "needs_setup",
        "setup_session": False,
        "secret_key_configured": True,
        "oidc": {"source": None, "configured": False},
        "gitlab": {"source": None, "configured": False},
    }
    # Nothing can sign in yet.
    assert harness.client.get("/api/v1/auth/login").status_code == 503


def test_the_code_starts_a_setup_session(harness: Harness) -> None:
    response = harness.start()

    cookie = next(c for c in response.headers.get_list("set-cookie") if SETUP_COOKIE in c)
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/" in cookie
    assert "Max-Age=3600" in cookie
    assert len(harness.csrf) == 64
    assert harness.status()["setup_session"] is True
    with Session(harness.engine) as db:
        stored = db.scalars(select(SetupSession.token_digest)).one()
    assert stored == sha256_hex(harness.client.cookies[SETUP_COOKIE])
    assert harness.audit() == [("setup", "setup.session_started", {})]


def test_the_code_is_accepted_in_any_case_and_spacing(harness: Harness) -> None:
    code = harness.code()
    loose = code.lower().replace("-", " ")

    assert harness.client.post("/api/v1/setup/session", json={"code": loose}).status_code == 200


def test_only_the_code_digest_is_stored(harness: Harness) -> None:
    code = harness.code()
    with Session(harness.engine) as db:
        row = db.get_one(SetupState, 1)
        assert row.code_digest == sha256_hex(code)
    assert code not in harness.dump()


def test_wrong_codes_are_limited(harness: Harness) -> None:
    code = harness.code()
    for _ in range(5):
        response = harness.client.post("/api/v1/setup/session", json={"code": "AAAA-BBBB-CCCC"})
        assert response.status_code == 401

    # Locked, even for the right code, until the window ends.
    locked = harness.client.post("/api/v1/setup/session", json={"code": code})
    assert locked.status_code == 429
    with Session(harness.engine) as db:
        db.execute(
            update(SetupState).values(
                failures_since=SetupState.failures_since - FAILURE_WINDOW - timedelta(seconds=1)
            )
        )
        db.commit()
    assert harness.client.post("/api/v1/setup/session", json={"code": code}).status_code == 200


def test_a_new_code_replaces_the_old_one(harness: Harness) -> None:
    old = harness.code()
    harness.code()

    response = harness.client.post("/api/v1/setup/session", json={"code": old})

    assert response.status_code == 401


def test_the_startup_code_is_logged_once(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="baluarte.configuration.setup"):
        assert issue_startup_code(harness.engine)

    message = caplog.records[-1].getMessage()
    code = message.split("setup code ")[1].split(".")[0]
    assert harness.client.post("/api/v1/setup/session", json={"code": code}).status_code == 200


@pytest.mark.parametrize(
    ("method", "path"),
    sorted(
        SETUP_ROUTES
        - {
            ("GET", "/api/v1/setup/status"),
            ("POST", "/api/v1/setup/session"),
            ("DELETE", "/api/v1/setup/session"),
        }
    ),
)
def test_setup_routes_need_a_setup_session(harness: Harness, method: str, path: str) -> None:
    response = harness.client.request(method, path, json={})

    assert response.status_code == 401
    assert response.json() == {"detail": "No valid setup session"}


def test_changes_need_the_csrf_token_and_a_same_origin(harness: Harness) -> None:
    harness.start()

    missing = harness.client.put("/api/v1/setup/oidc", json=oidc_body())
    cross = harness.client.put(
        "/api/v1/setup/oidc",
        json=oidc_body(),
        headers={CSRF_HEADER: harness.csrf, "Origin": "https://evil.example.test"},
    )
    same = harness.client.put(
        "/api/v1/setup/oidc",
        json=oidc_body(),
        headers={CSRF_HEADER: harness.csrf, "Origin": PUBLIC_URL},
    )

    assert missing.status_code == 403
    assert cross.status_code == 403
    assert same.status_code == 200


def test_ending_the_setup_session(harness: Harness) -> None:
    harness.start()

    assert harness.client.delete("/api/v1/setup/session").status_code == 204
    assert harness.client.get("/api/v1/setup/oidc").status_code == 401


# Identity provider


def test_saving_the_identity_provider(harness: Harness) -> None:
    harness.start()
    assert harness.client.get("/api/v1/setup/oidc").status_code == 404

    response = harness.put("/api/v1/setup/oidc", oidc_body())

    assert response.status_code == 200, response.text
    assert response.json() == {
        "source": "database",
        "public_url": PUBLIC_URL,
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "client_secret": "configured",
        "scopes": "openid profile email",
        "roles_claim": "groups",
        "admin_groups": ["baluarte-admins"],
        "analyst_groups": ["baluarte-analysts"],
        "auditor_groups": [],
        "redirect_uri": f"{PUBLIC_URL}/api/v1/auth/callback",
    }
    assert harness.client.get("/api/v1/setup/oidc").json() == response.json()
    assert harness.status()["state"] == "awaiting_admin_sign_in"
    assert harness.status()["oidc"] == {"source": "database", "configured": True}
    # The secret is sealed, never stored or audited in the clear.
    assert CLIENT_SECRET not in harness.dump()
    with Session(harness.engine) as db:
        sealed = db.get_one(StoredSecret, "oidc_client_secret")
        assert sealed.updated_by == "setup"
    actor, action, details = harness.audit()[-1]
    assert (actor, action) == ("setup", "settings.oidc_updated")
    assert details["via"] == "setup"
    assert details["client_secret_replaced"] is True
    assert details["admin_groups"] == ["baluarte-admins"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"issuer": "http://idp.example.test"}, "issuer must be an https URL"),
        ({"public_url": "http://baluarte.example.test"}, "public_url must use https"),
        ({"public_url": "https://baluarte.example.test/app"}, "public_url must be an origin"),
        ({"scopes": "profile email"}, "scopes must include openid"),
        ({"admin_groups": [" "]}, "admin_groups must name at least one group"),
        ({"client_id": "  "}, "client_id must not be empty"),
        ({"clear_client_secret": True}, "not both"),
    ],
)
def test_invalid_identity_provider_settings(
    harness: Harness, overrides: dict[str, Any], message: str
) -> None:
    harness.start()

    response = harness.put("/api/v1/setup/oidc", oidc_body(**overrides))

    assert response.status_code == 422
    assert message in response.json()["detail"]


def test_shape_errors_are_422(harness: Harness) -> None:
    harness.start()

    assert harness.put("/api/v1/setup/oidc", oidc_body(admin_groups=[])).status_code == 422
    assert harness.put("/api/v1/setup/oidc", oidc_body(client_secret="")).status_code == 422


def test_the_secret_must_be_entered_again_for_a_new_issuer(harness: Harness) -> None:
    harness.start()
    harness.put("/api/v1/setup/oidc", oidc_body())

    kept = harness.put("/api/v1/setup/oidc", oidc_body(client_secret=None, client_id="other"))
    moved = harness.put(
        "/api/v1/setup/oidc", oidc_body(client_secret=None, issuer="https://other.example.test")
    )
    cleared = harness.put(
        "/api/v1/setup/oidc",
        oidc_body(
            client_secret=None, issuer="https://other.example.test", clear_client_secret=True
        ),
    )

    assert kept.status_code == 200
    assert kept.json()["client_secret"] == "configured"
    assert moved.status_code == 422
    assert "client secret again" in moved.json()["detail"]
    assert cleared.status_code == 200
    assert cleared.json()["client_secret"] == "not_set"


def test_secrets_need_the_key(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BALUARTE_SECRET_KEY_FILE")
    harness.start()
    assert harness.status()["secret_key_configured"] is False

    with_secret = harness.put("/api/v1/setup/oidc", oidc_body())
    public_client = harness.put("/api/v1/setup/oidc", oidc_body(client_secret=None))

    assert with_secret.status_code == 409
    assert "BALUARTE_SECRET_KEY_FILE" in with_secret.json()["detail"]
    assert public_client.status_code == 200
    assert public_client.json()["client_secret"] == "not_set"


def test_discovery_test(harness: Harness) -> None:
    harness.start()

    ok = harness.post("/api/v1/setup/oidc/test", {"issuer": ISSUER})
    harness.idp.discovery["issuer"] = "https://elsewhere.example.test"
    mismatch = harness.post("/api/v1/setup/oidc/test", {"issuer": ISSUER})
    harness.idp.fail_with = httpx.ConnectError("refused")
    unreachable = harness.post("/api/v1/setup/oidc/test", {"issuer": ISSUER})
    invalid = harness.post("/api/v1/setup/oidc/test", {"issuer": "http://idp.example.test"})

    assert ok.json() == {
        "ok": True,
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "scopes_supported": [],
        "error": None,
    }
    assert mismatch.json()["ok"] is False
    assert "must match" in mismatch.json()["error"]
    assert unreachable.json()["ok"] is False
    assert unreachable.json()["error"] == "The identity provider could not be reached"
    assert invalid.status_code == 422


# GitLab


def test_saving_gitlab(harness: Harness) -> None:
    harness.start()
    assert harness.client.get("/api/v1/setup/gitlab").status_code == 404

    response = harness.put("/api/v1/setup/gitlab", gitlab_body())

    assert response.status_code == 200, response.text
    assert response.json() == {
        "source": "database",
        "url": GITLAB_URL,
        "token": "configured",
        "timeout_seconds": 15.0,
        "sync_interval_minutes": 60,
    }
    assert GITLAB_TOKEN not in harness.dump()
    # The API and worker use it at once in this process, and within seconds elsewhere.
    settings = load_gitlab_settings()
    assert settings is not None
    assert (settings.url, settings.token, settings.source) == (GITLAB_URL, GITLAB_TOKEN, "database")
    assert harness.status()["gitlab"] == {"source": "database", "configured": True}
    actor, action, details = harness.audit()[-1]
    assert (actor, action) == ("setup", "settings.gitlab_updated")
    assert details == {
        "via": "setup",
        "url": GITLAB_URL,
        "timeout_seconds": 15.0,
        "sync_interval_minutes": 60,
        "token_replaced": True,
    }


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"url": "http://gitlab.example.test"}, "url must be an https URL"),
        ({"url": "https://root:pw@gitlab.example.test"}, "must not contain credentials"),
        ({"timeout_seconds": 0}, "timeout_seconds must be between 1 and 120"),
        ({"sync_interval_minutes": 5}, "sync_interval_minutes must be 0 (off)"),
        ({"token": None}, "A read_api token is required"),
    ],
)
def test_invalid_gitlab_settings(harness: Harness, overrides: dict[str, Any], message: str) -> None:
    harness.start()

    response = harness.put("/api/v1/setup/gitlab", gitlab_body(**overrides))

    assert response.status_code == 422
    assert message in response.json()["detail"]


def test_the_token_must_be_entered_again_for_a_new_url(harness: Harness) -> None:
    harness.start()
    harness.put("/api/v1/setup/gitlab", gitlab_body())

    same = harness.put("/api/v1/setup/gitlab", gitlab_body(token=None, sync_interval_minutes=0))
    moved = harness.put(
        "/api/v1/setup/gitlab", gitlab_body(token=None, url="https://other.example.test")
    )

    assert same.status_code == 200
    assert same.json()["sync_interval_minutes"] == 0
    assert moved.status_code == 422


def test_connection_test(harness: Harness) -> None:
    harness.start()
    harness.gitlab.json(
        "/user", body={"id": 1, "username": "root", "state": "active", "is_admin": True}
    )

    response = harness.post("/api/v1/setup/gitlab/test", {"url": GITLAB_URL, "token": GITLAB_TOKEN})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["version"] == "17.5.1-ee"
    assert body["user"] == {"username": "root", "is_admin": True}
    assert body["token_scopes"] == ["read_api"]
    assert body["token_expires_at"] == "2027-06-30"
    assert {check["name"]: check["status"] for check in body["checks"]} == {
        "connectivity": "pass",
        "token": "pass",
        "user": "warn",
        "group_visibility": "pass",
    }
    assert body["error"] is None
    assert harness.gitlab.requests[0].headers["PRIVATE-TOKEN"] == GITLAB_TOKEN
    # Testing stores nothing.
    assert harness.client.get("/api/v1/setup/gitlab").status_code == 404


def test_a_failed_connection_test(harness: Harness) -> None:
    harness.start()
    harness.gitlab.json("/version", status=401, body={"message": "401 Unauthorized"})

    body = harness.post(
        "/api/v1/setup/gitlab/test", {"url": GITLAB_URL, "token": GITLAB_TOKEN}
    ).json()

    assert body["ok"] is False
    assert body["error"]
    assert body["checks"][0]["status"] == "fail"


def test_the_stored_token_is_only_tested_against_its_url(harness: Harness) -> None:
    harness.start()
    harness.put("/api/v1/setup/gitlab", gitlab_body())

    stored = harness.post("/api/v1/setup/gitlab/test", {"url": GITLAB_URL, "token": None})
    elsewhere = harness.post(
        "/api/v1/setup/gitlab/test", {"url": "https://evil.example.test", "token": None}
    )

    assert stored.json()["ok"] is True
    assert harness.gitlab.requests[-1].headers["PRIVATE-TOKEN"] == GITLAB_TOKEN
    assert elsewhere.status_code == 422
    assert all(request.url.host == "gitlab.example.test" for request in harness.gitlab.requests)


# Precedence


def test_the_deployment_wins(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token = tmp_path / "token"
    token.write_text("deployment-token-value")
    monkeypatch.setenv("BALUARTE_GITLAB_URL", "https://deployed.example.test")
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    harness.start()

    assert harness.status()["gitlab"] == {"source": "deployment", "configured": True}
    assert harness.client.get("/api/v1/setup/gitlab").json()["source"] == "deployment"
    refused = harness.put("/api/v1/setup/gitlab", gitlab_body())
    assert refused.status_code == 409
    assert refused.json() == {"detail": "GitLab settings are managed by the deployment"}

    monkeypatch.setenv("BALUARTE_PUBLIC_URL", PUBLIC_URL)
    monkeypatch.setenv("BALUARTE_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("BALUARTE_OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("BALUARTE_OIDC_ADMIN_GROUPS", "admins")
    # Sign-in set by the deployment: nothing to set up, and nothing more disclosed.
    assert harness.status() == {
        "state": "complete",
        "setup_session": False,
        "secret_key_configured": None,
        "oidc": None,
        "gitlab": None,
    }
    assert harness.put("/api/v1/setup/oidc", oidc_body()).status_code == 409


# Completion


def test_the_first_admin_sign_in_completes_setup(harness: Harness) -> None:
    harness.start()
    harness.put("/api/v1/setup/oidc", oidc_body())

    analyst = harness.sign_in("baluarte-analysts")
    assert analyst.status_code == 303
    assert harness.status()["state"] == "awaiting_admin_sign_in"

    admin = harness.sign_in("baluarte-admins")

    assert admin.status_code == 303
    assert harness.status()["state"] == "complete"
    assert ("user:1", "setup.completed", {"issuer": ISSUER}) in harness.audit()
    with Session(harness.engine) as db:
        row = db.get_one(SetupState, 1)
        assert row.completed_by_user_id == 1
        assert row.code_digest is None
        assert db.scalars(select(SetupSession)).all() == []
    # The setup session ended and setup is locked for good.
    for method, path in SETUP_ROUTES - {("GET", "/api/v1/setup/status")}:
        if path == "/api/v1/setup/session" and method == "DELETE":
            continue
        response = harness.client.request(
            method, path, json={"code": "x"}, headers={CSRF_HEADER: harness.csrf}
        )
        assert response.status_code == 409, (method, path)
    with Session(harness.engine) as db:
        assert issue_code(db) is None
    assert sum(1 for _, action, _ in harness.audit() if action == "setup.completed") == 1


def test_sign_in_uses_the_stored_settings(harness: Harness) -> None:
    harness.start()
    harness.put("/api/v1/setup/oidc", oidc_body())

    harness.sign_in("baluarte-admins")

    token_request = next(r for r in harness.idp.requests if r.url.path == "/token")
    expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert token_request.headers["Authorization"] == f"Basic {expected}"
    me = harness.client.get("/api/v1/auth/me").json()
    assert me["roles"] == ["admin"]


# Admin Settings page


def admin_client(harness: Harness, *roles: Role) -> TestClient:
    # Signed in through settings already in force; GET /settings reports them.
    harness.app.dependency_overrides[auth_settings] = lambda: make_settings()
    harness.app.dependency_overrides[current_principal] = lambda: Principal(
        user_id=1,
        subject="user-1",
        email=None,
        display_name=None,
        roles=frozenset(roles),
        session_id=1,
        session_expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        csrf_token="csrf",
    )
    return harness.client


@pytest.mark.parametrize("role", [Role.ANALYST, Role.AUDITOR])
def test_only_admins_change_settings(harness: Harness, role: Role) -> None:
    client = admin_client(harness, role)

    assert client.get("/api/v1/settings/gitlab").status_code == 403
    assert client.put("/api/v1/settings/gitlab", json=gitlab_body()).status_code == 403
    assert client.delete("/api/v1/settings/gitlab").status_code == 403
    assert client.put("/api/v1/settings/oidc", json=oidc_body()).status_code == 403


def test_admins_change_and_disconnect_gitlab(harness: Harness) -> None:
    client = admin_client(harness, Role.ADMIN)
    assert client.get("/api/v1/settings/gitlab").status_code == 404

    saved = client.put("/api/v1/settings/gitlab", json=gitlab_body())
    tested = client.post("/api/v1/settings/gitlab/test", json={"url": GITLAB_URL})
    info = client.get("/api/v1/settings")

    assert saved.status_code == 200
    assert tested.json()["ok"] is True
    assert info.json()["gitlab"] == {
        "source": "database",
        "url": GITLAB_URL,
        "token": "configured",
        "timeout_seconds": 15.0,
        "sync_interval_minutes": 60,
    }
    assert info.json()["secret_key"] == "configured"

    assert client.delete("/api/v1/settings/gitlab").status_code == 204
    assert client.delete("/api/v1/settings/gitlab").status_code == 404
    assert load_gitlab_settings() is None
    with Session(harness.engine) as db:
        assert db.get(StoredSecret, "gitlab_token") is None
    actions = [(actor, action) for actor, action, _ in harness.audit()]
    assert actions == [
        ("user:1", "settings.gitlab_updated"),
        ("user:1", "settings.gitlab_disconnected"),
    ]
    assert harness.audit()[0][2]["via"] == "settings"


def test_admins_change_the_identity_provider(harness: Harness) -> None:
    client = admin_client(harness, Role.ADMIN)

    saved = client.put("/api/v1/settings/oidc", json=oidc_body(auditor_groups=["audit"]))
    read = client.get("/api/v1/settings/oidc")
    tested = client.post("/api/v1/settings/oidc/test", json={"issuer": ISSUER})

    assert saved.status_code == 200
    assert read.json()["auditor_groups"] == ["audit"]
    assert tested.json()["ok"] is True
    assert harness.audit()[-1][:2] == ("user:1", "settings.oidc_updated")


# Stored secrets


def test_a_stored_secret_needs_the_same_key(
    harness: Harness, secret_key: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.start()
    harness.put("/api/v1/setup/gitlab", gitlab_body())
    write_key(secret_key)
    effective.invalidate()

    with pytest.raises(SecretUnavailable, match="key that is not configured"):
        load_gitlab_settings()


def test_rotating_the_key(
    harness: Harness,
    secret_key: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    harness.start()
    harness.put("/api/v1/setup/gitlab", gitlab_body())
    previous = tmp_path / "previous-key"
    previous.write_text(secret_key.read_text())
    write_key(secret_key)
    monkeypatch.setenv("BALUARTE_SECRET_KEY_PREVIOUS_FILE", str(previous))
    database_env(monkeypatch, harness.engine, tmp_path)

    assert cli.reencrypt_secrets([]) == 0
    assert "Re-encrypted 1 of 1" in capsys.readouterr().out

    monkeypatch.delenv("BALUARTE_SECRET_KEY_PREVIOUS_FILE")
    effective.invalidate()
    settings = load_gitlab_settings()
    assert settings is not None
    assert settings.token == GITLAB_TOKEN


def database_env(monkeypatch: pytest.MonkeyPatch, engine: Engine, tmp_path: Path) -> None:
    url = engine.url
    password = tmp_path / "db-password"
    password.write_text(url.password or "")
    monkeypatch.setenv(
        "BALUARTE_DATABASE_URL",
        f"postgresql://{url.username}@{url.host}:{url.port}/{url.database}",
    )
    monkeypatch.setenv("BALUARTE_DATABASE_PASSWORD_FILE", str(password))


def test_setup_code_command(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    harness.start()
    database_env(monkeypatch, harness.engine, tmp_path)

    assert cli.setup_code([]) == 0
    # The code is the last line; the audit log line may come before it.
    code = capsys.readouterr().out.strip().splitlines()[-1]

    # The new code works and every earlier setup session has ended.
    assert harness.client.get("/api/v1/setup/oidc").status_code == 401
    assert harness.client.post("/api/v1/setup/session", json={"code": code}).status_code == 200
    with Session(harness.engine) as db:
        db.execute(text("INSERT INTO app_user (issuer, subject) VALUES ('i', 's')"))
        db.execute(
            update(SetupState).values(
                completed_at=text("now()"), completed_by_user_id=1, code_digest=None
            )
        )
        db.commit()
    assert cli.setup_code([]) == 1
    assert "already complete" in capsys.readouterr().err
