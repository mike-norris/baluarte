from datetime import timedelta
from pathlib import Path

import pytest

from baluarte.auth.roles import Role
from baluarte.auth.settings import load_auth_settings
from baluarte.config import ConfigurationError

BASE = {
    "BALUARTE_PUBLIC_URL": "https://baluarte.example.test",
    "BALUARTE_OIDC_ISSUER": "https://idp.example.test/realms/main/",
    "BALUARTE_OIDC_CLIENT_ID": "baluarte",
    "BALUARTE_OIDC_ADMIN_GROUPS": "platform-admins, security ,",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        *BASE,
        "BALUARTE_OIDC_CLIENT_SECRET_FILE",
        "BALUARTE_OIDC_ANALYST_GROUPS",
        "BALUARTE_OIDC_AUDITOR_GROUPS",
        "BALUARTE_OIDC_SCOPES",
        "BALUARTE_OIDC_ROLES_CLAIM",
        "BALUARTE_SESSION_IDLE_MINUTES",
        "BALUARTE_SESSION_LIFETIME_MINUTES",
    ):
        monkeypatch.delenv(name, raising=False)


def configure(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    for name, value in {**BASE, **overrides}.items():
        monkeypatch.setenv(name, value)


def test_unconfigured_returns_none() -> None:
    assert load_auth_settings() is None


def test_valid_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch)

    settings = load_auth_settings()

    assert settings is not None
    assert settings.issuer == "https://idp.example.test/realms/main"
    assert settings.redirect_uri == "https://baluarte.example.test/api/v1/auth/callback"
    assert settings.group_roles[Role.ADMIN] == {"platform-admins", "security"}
    assert settings.group_roles[Role.AUDITOR] == frozenset()
    assert settings.client_secret is None
    assert settings.scopes == "openid profile email"
    assert settings.roles_claim == "groups"
    assert settings.session_idle == timedelta(minutes=60)
    assert settings.session_lifetime == timedelta(hours=8)
    assert settings.secure_cookies
    assert settings.session_cookie == "__Host-baluarte_session"


def test_partial_configuration_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BALUARTE_OIDC_ISSUER", BASE["BALUARTE_OIDC_ISSUER"])

    with pytest.raises(ConfigurationError, match="BALUARTE_PUBLIC_URL"):
        load_auth_settings()


def test_loopback_http_uses_plain_cookie_names(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch, BALUARTE_PUBLIC_URL="http://127.0.0.1:8080/")

    settings = load_auth_settings()

    assert settings is not None
    assert settings.public_url == "http://127.0.0.1:8080"
    assert not settings.secure_cookies
    assert settings.session_cookie == "baluarte_session"


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("BALUARTE_PUBLIC_URL", "http://baluarte.example.test", "https"),
        ("BALUARTE_PUBLIC_URL", "https://baluarte.example.test/app", "no path"),
        ("BALUARTE_PUBLIC_URL", "baluarte.example.test", "absolute"),
        ("BALUARTE_OIDC_ISSUER", "http://idp.example.test", "https"),
        ("BALUARTE_OIDC_SCOPES", "profile email", "openid"),
        ("BALUARTE_SESSION_IDLE_MINUTES", "0", "positive"),
        ("BALUARTE_SESSION_IDLE_MINUTES", "soon", "whole number"),
        ("BALUARTE_SESSION_IDLE_MINUTES", "600", "cannot exceed"),
        ("BALUARTE_OIDC_ADMIN_GROUPS", " , ", "No groups"),
    ],
)
def test_invalid_values_are_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, message: str
) -> None:
    configure(monkeypatch, **{name: value})

    with pytest.raises(ConfigurationError, match=message):
        load_auth_settings()


def test_client_secret_is_read_from_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = tmp_path / "client-secret"
    secret.write_text("s3cret\n")
    configure(monkeypatch, BALUARTE_OIDC_CLIENT_SECRET_FILE=str(secret))

    settings = load_auth_settings()

    assert settings is not None
    assert settings.client_secret == "s3cret"


@pytest.mark.parametrize("content", [None, "  \n"])
def test_unreadable_or_empty_secret_file_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str | None
) -> None:
    secret = tmp_path / "client-secret"
    if content is not None:
        secret.write_text(content)
    configure(monkeypatch, BALUARTE_OIDC_CLIENT_SECRET_FILE=str(secret))

    with pytest.raises(ConfigurationError, match="BALUARTE_OIDC_CLIENT_SECRET_FILE"):
        load_auth_settings()
