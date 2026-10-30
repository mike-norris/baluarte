"""End-to-end sign-in, session and sign-out against PostgreSQL and the fake identity provider."""

from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from oidc_fake import PUBLIC_URL, FakeIdP, make_settings
from sqlalchemy import Engine, select, text, update
from sqlalchemy.orm import Session

from baluarte.api.v1.auth import safe_return_to
from baluarte.audit import details_digest
from baluarte.auth.dependencies import CSRF_HEADER, auth_settings, database_engine, oidc_client
from baluarte.auth.settings import AuthSettings
from baluarte.db.models import AppUser, AuditEvent, OidcLogin, UserSession
from baluarte.main import create_app

SESSION_COOKIE = "__Host-baluarte_session"
LOGIN_COOKIE = "__Host-baluarte_login"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "/"),
        ("/findings?state=open#top", "/findings?state=open#top"),
        ("//evil.example.test/", "/"),
        ("/\\evil.example.test", "/"),
        ("https://evil.example.test/", "/"),
        ("findings", "/"),
        ("/\tfoo", "/"),
        ("/%0d%0aSet-Cookie:x", "/%0d%0aSet-Cookie:x"),
    ],
)
def test_safe_return_to(value: str | None, expected: str) -> None:
    assert safe_return_to(value) == expected


class Harness:
    def __init__(self, engine: Engine, settings: AuthSettings) -> None:
        self.engine = engine
        self.settings = settings
        self.idp = FakeIdP()
        oidc = self.idp.client(settings)
        app = create_app()
        app.dependency_overrides[auth_settings] = lambda: settings
        app.dependency_overrides[oidc_client] = lambda: oidc
        app.dependency_overrides[database_engine] = lambda: engine
        self.client = TestClient(app, base_url=PUBLIC_URL, follow_redirects=False)

    def start(self, return_to: str | None = None) -> dict[str, str]:
        params = {"return_to": return_to} if return_to else {}
        response = self.client.get("/api/v1/auth/login", params=params)
        assert response.status_code == 302
        return self.idp.authorize(response.headers["location"])

    def callback(self, **params: str) -> httpx.Response:
        return self.client.get("/api/v1/auth/callback", params=params)

    def sign_in(self, return_to: str | None = None) -> httpx.Response:
        query = self.start(return_to)
        return self.callback(state=query["state"], code=self.idp.code)

    def me(self) -> httpx.Response:
        return self.client.get("/api/v1/auth/me")

    def audit(self) -> list[tuple[str, str, dict[str, Any]]]:
        with Session(self.engine) as db:
            rows = db.scalars(select(AuditEvent).order_by(AuditEvent.id))
            return [(row.actor, row.action, row.details) for row in rows]

    def sql(self, statement: Any) -> None:
        with Session(self.engine) as db:
            db.execute(statement)
            db.commit()


def redirect_error(response: httpx.Response) -> str:
    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert location.path == "/sign-in"
    return parse_qs(location.query)["error"][0]


@pytest.fixture
def harness(migrated_engine: Engine) -> Iterator[Harness]:
    harness = Harness(migrated_engine, make_settings())
    yield harness
    harness.client.close()


pytestmark = pytest.mark.db


def test_sign_in_creates_a_session(harness: Harness) -> None:
    response = harness.sign_in(return_to="/findings")

    assert response.status_code == 303
    assert response.headers["location"] == "/findings"
    cookie = next(c for c in response.headers.get_list("set-cookie") if SESSION_COOKIE in c)
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/" in cookie
    assert "Domain" not in cookie

    me = harness.me()
    assert me.status_code == 200
    body = me.json()
    # The caller's own internal ID, the one the audit log names below.
    assert body["user_id"] == 1
    assert body["subject"] == "user-1"
    assert body["email"] == "ada@example.test"
    assert body["display_name"] == "Ada Lovelace"
    assert body["roles"] == ["analyst"]
    assert "policy:write" in body["permissions"]
    assert "settings:manage" not in body["permissions"]
    assert len(body["csrf_token"]) == 64

    assert harness.audit() == [("user:1", "auth.login", {"roles": ["analyst"]})]


def test_only_digests_of_bearer_values_are_stored(harness: Harness) -> None:
    query = harness.start()
    with Session(harness.engine) as db:
        login = db.scalars(select(OidcLogin)).one()
        assert query["state"] not in login.state_digest

    harness.callback(state=query["state"], code=harness.idp.code)
    token = harness.client.cookies[SESSION_COOKIE]
    with harness.engine.connect() as connection:
        dump = " ".join(
            str(value)
            for table in ("user_session", "oidc_login", "audit_event", "app_user")
            for row in connection.execute(text(f"SELECT * FROM {table}"))  # noqa: S608
            for value in row
        )
    assert token not in dump
    assert query["state"] not in dump


def test_audit_digest_matches_details(harness: Harness) -> None:
    harness.sign_in()

    with Session(harness.engine) as db:
        event = db.scalars(select(AuditEvent)).one()
        assert event.details_digest == details_digest(event.details)


def test_repeat_sign_in_updates_the_user(harness: Harness) -> None:
    harness.sign_in()
    harness.idp.claims["email"] = "ada@new.example.test"
    harness.sign_in()

    with Session(harness.engine) as db:
        users = db.scalars(select(AppUser)).all()
        assert [user.email for user in users] == ["ada@new.example.test"]
        assert len(db.scalars(select(UserSession)).all()) == 2


def test_signing_in_again_ends_the_previous_session(harness: Harness) -> None:
    harness.sign_in()
    first = harness.client.cookies[SESSION_COOKIE]

    harness.sign_in()

    assert harness.client.cookies[SESSION_COOKIE] != first
    assert harness.me().status_code == 200
    harness.client.cookies.set(SESSION_COOKIE, first, domain="baluarte.example.test")
    assert harness.me().status_code == 401


def test_state_must_match_the_browser(harness: Harness) -> None:
    query = harness.start()
    harness.client.cookies.delete(LOGIN_COOKIE)

    response = harness.callback(state=query["state"], code=harness.idp.code)

    assert redirect_error(response) == "expired"
    assert harness.me().status_code == 401
    assert harness.audit() == []


def test_state_cannot_be_replayed(harness: Harness) -> None:
    query = harness.start()
    harness.callback(state=query["state"], code=harness.idp.code)
    harness.client.cookies.set(LOGIN_COOKIE, query["state"], domain="baluarte.example.test")

    response = harness.callback(state=query["state"], code=harness.idp.code)

    assert redirect_error(response) == "expired"


def test_expired_sign_in_is_rejected(harness: Harness) -> None:
    query = harness.start()
    harness.sql(update(OidcLogin).values(expires_at=OidcLogin.created_at))

    response = harness.callback(state=query["state"], code=harness.idp.code)

    assert redirect_error(response) == "expired"


def test_provider_error_is_audited(harness: Harness) -> None:
    query = harness.start()

    response = harness.callback(state=query["state"], error="access_denied")

    assert redirect_error(response) == "provider_denied"
    assert harness.audit() == [
        ("anonymous", "auth.login_failed", {"reason": "provider_error:access_denied"})
    ]


def test_invalid_id_token_is_audited_without_detail_in_redirect(harness: Harness) -> None:
    query = harness.start()
    harness.idp.nonce = "a-different-nonce"

    response = harness.callback(state=query["state"], code=harness.idp.code)

    assert redirect_error(response) == "sign_in_failed"
    assert harness.audit() == [
        ("anonymous", "auth.login_failed", {"reason": "id_token_nonce_mismatch"})
    ]
    assert harness.me().status_code == 401


def test_user_without_a_mapped_group_is_refused(harness: Harness) -> None:
    harness.idp.claims["groups"] = ["everyone"]

    response = harness.sign_in()

    assert redirect_error(response) == "not_authorized"
    assert harness.audit() == [("oidc:user-1", "auth.login_failed", {"reason": "no_mapped_role"})]
    with Session(harness.engine) as db:
        assert db.scalars(select(UserSession)).all() == []


def test_groups_from_userinfo_when_absent_from_id_token(harness: Harness) -> None:
    del harness.idp.claims["groups"]
    harness.idp.userinfo_claims = {"groups": ["baluarte-auditors"]}

    harness.sign_in()

    assert harness.me().json()["roles"] == ["auditor"]


def test_provider_outage_on_sign_in(harness: Harness) -> None:
    harness.idp.fail_with = httpx.ConnectError("refused")

    response = harness.client.get("/api/v1/auth/login")

    assert redirect_error(response) == "provider_unavailable"


def test_logout_requires_csrf_token(harness: Harness) -> None:
    harness.sign_in()
    csrf = harness.me().json()["csrf_token"]

    missing = harness.client.post("/api/v1/auth/logout")
    wrong = harness.client.post("/api/v1/auth/logout", headers={CSRF_HEADER: "0" * 64})
    cross_origin = harness.client.post(
        "/api/v1/auth/logout",
        headers={CSRF_HEADER: csrf, "Origin": "https://evil.example.test"},
    )

    assert [missing.status_code, wrong.status_code, cross_origin.status_code] == [403, 403, 403]
    assert harness.me().status_code == 200


def test_logout_revokes_the_session(harness: Harness) -> None:
    harness.sign_in()
    token = harness.client.cookies[SESSION_COOKIE]
    csrf = harness.me().json()["csrf_token"]

    response = harness.client.post(
        "/api/v1/auth/logout", headers={CSRF_HEADER: csrf, "Origin": PUBLIC_URL}
    )

    assert response.status_code == 204
    assert harness.audit()[-1][:2] == ("user:1", "auth.logout")
    harness.client.cookies.set(SESSION_COOKIE, token, domain="baluarte.example.test")
    assert harness.me().status_code == 401


def test_unknown_session_cookie_is_rejected(harness: Harness) -> None:
    harness.client.cookies.set(SESSION_COOKIE, "forged", domain="baluarte.example.test")

    assert harness.me().status_code == 401


@pytest.mark.parametrize(
    "change",
    [
        {
            "created_at": UserSession.created_at - timedelta(hours=9),
            "expires_at": UserSession.created_at - timedelta(hours=1),
        },
        {"last_seen_at": UserSession.created_at - timedelta(hours=2)},
    ],
    ids=["absolute-expiry", "idle-expiry"],
)
def test_expired_sessions_are_rejected(harness: Harness, change: dict[str, Any]) -> None:
    harness.sign_in()
    harness.sql(update(UserSession).values(**change))

    assert harness.me().status_code == 401


def test_session_activity_is_recorded(harness: Harness) -> None:
    harness.sign_in()
    harness.sql(
        update(UserSession).values(last_seen_at=UserSession.created_at - timedelta(minutes=5))
    )

    assert harness.me().status_code == 200

    with Session(harness.engine) as db:
        session = db.scalars(select(UserSession)).one()
        assert session.last_seen_at > session.created_at
