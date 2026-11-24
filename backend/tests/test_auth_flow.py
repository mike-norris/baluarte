"""End-to-end sign-in, session and sign-out against PostgreSQL and the fake identity provider."""

import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from oidc_fake import PUBLIC_URL, FakeIdP, make_settings
from sqlalchemy import Engine, func, select, text, update
from sqlalchemy.orm import Session

from baluarte.api.v1 import auth as auth_api
from baluarte.api.v1.auth import safe_return_to
from baluarte.audit import details_digest
from baluarte.auth import totp
from baluarte.auth.dependencies import CSRF_HEADER, auth_settings, database_engine, oidc_client
from baluarte.auth.sessions import sha256_hex
from baluarte.auth.settings import AuthSettings, mfa_policy
from baluarte.configuration.crypto import SecretBox
from baluarte.db.models import (
    AppUser,
    AuditEvent,
    OidcLogin,
    UserRecoveryCode,
    UserSession,
    UserTotp,
)
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

    assert harness.audit() == [
        (
            "user:1",
            "auth.login",
            {"roles": ["analyst"], "workspaces": {"1": ["analyst"]}, "mfa": False, "amr": []},
        )
    ]


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


def mfa_harness(engine: Engine, preset: str = "generic", required: bool = True) -> Harness:
    return Harness(engine, make_settings(mfa=mfa_policy(preset, required)))


def test_sign_in_records_mfa_proven_by_amr(harness: Harness) -> None:
    harness.idp.claims["amr"] = ["pwd", "mfa", "otp"]

    harness.sign_in()

    assert harness.me().json()["mfa_method"] == "provider"
    details = harness.audit()[-1][2]
    assert details["mfa"] is True
    assert details["amr"] == ["mfa", "otp", "pwd"]


def test_sign_in_without_mfa_is_allowed_while_not_required(harness: Harness) -> None:
    harness.idp.claims["amr"] = ["pwd"]

    harness.sign_in()

    assert harness.me().json()["mfa_method"] is None
    assert harness.audit()[-1][2]["amr"] == ["pwd"]


@pytest.mark.parametrize("amr", [None, ["pwd"], "mfa", [], [{"mfa": True}]])
def test_required_mfa_refuses_a_token_that_does_not_prove_it(
    migrated_engine: Engine, amr: Any
) -> None:
    harness = mfa_harness(migrated_engine)
    try:
        if amr is None:
            harness.idp.claims.pop("amr", None)
        else:
            harness.idp.claims["amr"] = amr

        response = harness.sign_in()

        assert redirect_error(response) == "mfa_required"
        assert harness.audit() == [
            ("oidc:user-1", "auth.login_failed", {"reason": "mfa_not_proven"})
        ]
        with Session(harness.engine) as db:
            assert db.scalars(select(UserSession)).all() == []
    finally:
        harness.client.close()


def test_required_mfa_is_read_from_the_id_token_not_userinfo(migrated_engine: Engine) -> None:
    harness = mfa_harness(migrated_engine)
    try:
        del harness.idp.claims["groups"]
        harness.idp.userinfo_claims = {"groups": ["baluarte-auditors"], "amr": ["mfa"]}

        assert redirect_error(harness.sign_in()) == "mfa_required"
    finally:
        harness.client.close()


@pytest.mark.parametrize(
    ("preset", "acr"),
    [
        ("okta", "urn:okta:loa:2fa:any"),
        ("auth0", "http://schemas.openid.net/pape/policies/2007/06/multi-factor"),
    ],
)
def test_presets_ask_for_mfa_and_accept_it(migrated_engine: Engine, preset: str, acr: str) -> None:
    harness = mfa_harness(migrated_engine, preset)
    try:
        query = harness.start()
        assert query["acr_values"] == acr
        harness.idp.claims["amr"] = ["mfa"]

        response = harness.callback(state=query["state"], code=harness.idp.code)

        assert response.status_code == 303
        assert harness.me().json()["mfa_method"] == "provider"
    finally:
        harness.client.close()


# Built-in TOTP (docs/SECURITY.md#built-in-totp)


class TotpHarness(Harness):
    def status(self) -> dict[str, Any]:
        response = self.client.get("/api/v1/auth/mfa")
        assert response.status_code == 200, response.text
        result: dict[str, Any] = response.json()
        return result

    def post(self, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
        csrf = self.status()["csrf_token"]
        return self.client.post(f"/api/v1/auth/mfa{path}", json=body, headers={CSRF_HEADER: csrf})

    def enroll(self) -> str:
        response = self.post("/enroll")
        assert response.status_code == 200, response.text
        secret: str = response.json()["secret"]
        return secret

    def verify(self, code: str, return_to: str | None = None) -> httpx.Response:
        return self.post("/verify", {"code": code, "return_to": return_to})


def now_code(secret: str, offset: int = 0) -> str:
    return totp.code_at(secret, totp.step_at(time.time()) + offset)


def wrong_code(secret: str) -> str:
    valid = {now_code(secret, offset) for offset in (-2, -1, 0, 1, 2)}
    return next(code for code in ("000000", "111111", "222222", "333333") if code not in valid)


@pytest.fixture
def totp_harness(migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[TotpHarness]:
    box = SecretBox(bytes(range(32)))
    monkeypatch.setattr(auth_api, "secret_box", lambda: box)
    harness = TotpHarness(
        migrated_engine, make_settings(mfa=mfa_policy("generic", True, totp=True))
    )
    yield harness
    harness.client.close()


def test_first_sign_in_sets_up_an_authenticator(totp_harness: TotpHarness) -> None:
    h = totp_harness
    response = h.sign_in("/findings")

    assert response.status_code == 303
    assert response.headers["location"] == "/mfa?return_to=%2Ffindings"
    assert h.me().status_code == 401
    assert h.status()["enrolled"] is False
    assert h.status()["account"] == "ada@example.test"
    started = h.post("/enroll").json()
    assert started["uri"].startswith("otpauth://totp/Baluarte:ada@example.test?secret=")
    assert len(started["qr"]) == len(started["qr"][0])
    pending_cookie = h.client.cookies[SESSION_COOKIE]

    assert h.verify(wrong_code(started["secret"])).status_code == 400
    verified = h.verify(now_code(started["secret"]), "/findings")

    assert verified.status_code == 200, verified.text
    body = verified.json()
    assert body["return_to"] == "/findings"
    assert body["method"] == "totp"
    assert len(body["recovery_codes"]) == 10
    assert h.client.cookies[SESSION_COOKIE] != pending_cookie
    assert h.me().json()["mfa_method"] == "totp"
    assert h.client.get("/api/v1/auth/mfa").status_code == 401
    actions = [action for _, action, _ in h.audit()]
    assert actions == [
        "auth.mfa_challenged",
        "auth.mfa_failed",
        "auth.mfa_enrolled",
        "auth.login",
    ]
    assert h.audit()[-1][2]["mfa_method"] == "totp"
    with Session(h.engine) as db:
        session = db.scalars(select(UserSession)).one()
        assert session.mfa_pending is False
        assert session.expires_at - session.last_seen_at == timedelta(hours=8)
        assert session.token_digest != sha256_hex(pending_cookie)
        stored = db.scalars(select(UserTotp)).one()
        assert started["secret"].encode() not in bytes(stored.ciphertext)
        digests = db.scalars(select(UserRecoveryCode.digest)).all()
        assert sorted(digests) == sorted(totp.recovery_digest(c) for c in body["recovery_codes"])


def test_a_code_and_a_recovery_code_each_work_once(totp_harness: TotpHarness) -> None:
    h = totp_harness
    h.sign_in()
    secret = h.enroll()
    code = now_code(secret)
    recovery = h.verify(code).json()["recovery_codes"]

    h.sign_in()
    assert h.status()["enrolled"] is True
    assert h.post("/enroll").status_code == 409
    assert h.verify(code).status_code == 400
    used = h.verify(recovery[0].upper())
    assert used.status_code == 200, used.text
    assert used.json()["method"] == "recovery"
    assert used.json()["recovery_codes_left"] == 9
    assert h.me().json()["mfa_method"] == "recovery"
    assert h.audit()[-1][2]["recovery_codes_left"] == 9

    h.sign_in()
    assert h.verify(recovery[0]).status_code == 400
    assert h.verify(now_code(secret, 1)).status_code == 200


def test_too_many_wrong_codes_end_the_sign_in(totp_harness: TotpHarness) -> None:
    h = totp_harness
    h.sign_in()
    secret = h.enroll()
    wrong = wrong_code(secret)

    for left in (4, 3, 2, 1):
        refused = h.verify(wrong)
        assert refused.status_code == 400
        assert f"{left} tr" in refused.json()["detail"]
    ended = h.verify(wrong)

    assert ended.status_code == 401
    assert h.client.get("/api/v1/auth/mfa").status_code == 401
    assert h.audit()[-1][2] == {"failures": 5, "session_ended": True}
    with Session(h.engine) as db:
        assert db.scalars(select(UserSession)).one().revoked_at is not None


def test_a_waiting_sign_in_grants_nothing_and_needs_csrf(totp_harness: TotpHarness) -> None:
    h = totp_harness
    h.sign_in()

    assert h.me().status_code == 401
    assert h.client.get("/api/v1/findings").status_code == 401
    assert h.client.post("/api/v1/auth/mfa/enroll").status_code == 403
    assert h.client.post("/api/v1/auth/mfa/verify", json={"code": "123456"}).status_code == 403


def test_a_waiting_sign_in_expires_after_ten_minutes(totp_harness: TotpHarness) -> None:
    h = totp_harness
    h.sign_in()
    with Session(h.engine) as db:
        session = db.scalars(select(UserSession)).one()
        assert session.expires_at - session.created_at == timedelta(minutes=10)
    h.sql(
        update(UserSession).values(
            created_at=text("now() - interval '11 minutes'"),
            expires_at=text("now() - interval '1 minute'"),
        )
    )

    assert h.client.get("/api/v1/auth/mfa").status_code == 401


def test_mfa_proven_by_the_provider_skips_totp(totp_harness: TotpHarness) -> None:
    h = totp_harness
    h.idp.claims["amr"] = ["mfa"]

    response = h.sign_in("/findings")

    assert response.headers["location"] == "/findings"
    assert h.me().json()["mfa_method"] == "provider"


def test_totp_without_the_secret_key_refuses_sign_in(
    totp_harness: TotpHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth_api, "secret_box", lambda: None)

    assert redirect_error(totp_harness.sign_in()) == "mfa_required"
    assert totp_harness.audit()[-1][2] == {"reason": "totp_without_secret_key"}


def test_an_administrator_resets_another_users_authenticator(totp_harness: TotpHarness) -> None:
    h = totp_harness
    h.sign_in()
    h.verify(now_code(h.enroll()))
    with Session(h.engine) as db:
        ada = db.scalars(select(AppUser)).one().id

    h.idp.subject = "user-2"
    h.idp.claims.update(email="grace@example.test", groups=["baluarte-admins"])
    h.sign_in()
    h.verify(now_code(h.enroll()))
    admin = h.me().json()
    headers = {CSRF_HEADER: admin["csrf_token"]}
    # Signing in as someone else in this browser ended Ada's session; bring it back, as if she
    # were signed in elsewhere.
    h.sql(update(UserSession).where(UserSession.user_id == ada).values(revoked_at=None))

    listed = h.client.get("/api/v1/settings/mfa/enrollments").json()
    assert [(row["email"], row["recovery_codes_left"]) for row in listed] == [
        ("ada@example.test", 10),
        ("grace@example.test", 10),
    ]
    own = h.client.delete(f"/api/v1/settings/mfa/enrollments/{admin['user_id']}", headers=headers)
    assert own.status_code == 403
    reset = h.client.delete(f"/api/v1/settings/mfa/enrollments/{ada}", headers=headers)
    assert reset.status_code == 204
    assert h.audit()[-1][1:] == ("settings.mfa_reset", {"sessions_ended": 1})
    again = h.client.delete(f"/api/v1/settings/mfa/enrollments/{ada}", headers=headers)
    assert again.status_code == 404
    with Session(h.engine) as db:
        assert db.scalars(select(UserTotp.user_id)).all() == [admin["user_id"]]
        assert (
            db.scalar(
                select(func.count())
                .select_from(UserRecoveryCode)
                .where(UserRecoveryCode.user_id == ada)
            )
            == 0
        )
        live = db.scalars(
            select(UserSession).where(UserSession.user_id == ada, UserSession.revoked_at.is_(None))
        ).all()
        assert live == []
