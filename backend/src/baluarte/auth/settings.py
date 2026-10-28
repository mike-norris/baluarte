"""Authentication settings read from the environment.

Authentication is either fully configured or not configured at all. A partial configuration is
a ConfigurationError, so a typo never leaves the API running with weaker checks.
"""

import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from baluarte.auth.roles import Role
from baluarte.config import ConfigurationError

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
CALLBACK_PATH = "/api/v1/auth/callback"

_REQUIRED = ("BALUARTE_PUBLIC_URL", "BALUARTE_OIDC_ISSUER", "BALUARTE_OIDC_CLIENT_ID")
_GROUP_VARIABLES = {
    Role.ADMIN: "BALUARTE_OIDC_ADMIN_GROUPS",
    Role.ANALYST: "BALUARTE_OIDC_ANALYST_GROUPS",
    Role.AUDITOR: "BALUARTE_OIDC_AUDITOR_GROUPS",
}


@dataclass(frozen=True)
class AuthSettings:
    public_url: str
    issuer: str
    client_id: str
    client_secret: str | None
    scopes: str
    roles_claim: str
    group_roles: dict[Role, frozenset[str]]
    session_idle: timedelta
    session_lifetime: timedelta

    @property
    def redirect_uri(self) -> str:
        return self.public_url + CALLBACK_PATH

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def session_cookie(self) -> str:
        # The __Host- prefix makes browsers reject the cookie unless it is Secure, host-only
        # and scoped to /, so a sibling subdomain cannot set or shadow it.
        return "__Host-baluarte_session" if self.secure_cookies else "baluarte_session"

    @property
    def login_cookie(self) -> str:
        return "__Host-baluarte_login" if self.secure_cookies else "baluarte_login"


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _public_url(raw: str) -> str:
    parts = urlsplit(raw)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ConfigurationError("BALUARTE_PUBLIC_URL must be an absolute http(s) URL")
    if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
        raise ConfigurationError("BALUARTE_PUBLIC_URL must use https unless it is a loopback host")
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
        raise ConfigurationError("BALUARTE_PUBLIC_URL must be an origin with no path")
    return f"{parts.scheme}://{parts.netloc}"


def _issuer(raw: str) -> str:
    parts = urlsplit(raw)
    if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment:
        raise ConfigurationError("BALUARTE_OIDC_ISSUER must be an https URL")
    return raw.rstrip("/")


def _minutes(name: str, default: int) -> timedelta:
    raw = _env(name)
    try:
        value = int(raw) if raw else default
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a whole number of minutes") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be positive")
    return timedelta(minutes=value)


def load_auth_settings() -> AuthSettings | None:
    """Return the settings, or None when authentication is not configured at all."""
    present = [name for name in _REQUIRED if _env(name)]
    if not present:
        return None
    if len(present) != len(_REQUIRED):
        missing = ", ".join(name for name in _REQUIRED if name not in present)
        raise ConfigurationError(f"Authentication is partially configured; missing {missing}")

    client_secret = None
    secret_file = _env("BALUARTE_OIDC_CLIENT_SECRET_FILE")
    if secret_file:
        try:
            client_secret = Path(secret_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigurationError("BALUARTE_OIDC_CLIENT_SECRET_FILE cannot be read") from exc
        if not client_secret:
            raise ConfigurationError("BALUARTE_OIDC_CLIENT_SECRET_FILE is empty")

    group_roles = {
        role: frozenset(group.strip() for group in _env(name).split(",") if group.strip())
        for role, name in _GROUP_VARIABLES.items()
    }
    if not any(group_roles.values()):
        names = ", ".join(_GROUP_VARIABLES.values())
        raise ConfigurationError(f"No groups are mapped to roles; set at least one of {names}")

    scopes = _env("BALUARTE_OIDC_SCOPES") or "openid profile email"
    if "openid" not in scopes.split():
        raise ConfigurationError("BALUARTE_OIDC_SCOPES must include openid")

    idle = _minutes("BALUARTE_SESSION_IDLE_MINUTES", 60)
    lifetime = _minutes("BALUARTE_SESSION_LIFETIME_MINUTES", 8 * 60)
    if idle > lifetime:
        raise ConfigurationError(
            "BALUARTE_SESSION_IDLE_MINUTES cannot exceed BALUARTE_SESSION_LIFETIME_MINUTES"
        )

    return AuthSettings(
        public_url=_public_url(_env("BALUARTE_PUBLIC_URL")),
        issuer=_issuer(_env("BALUARTE_OIDC_ISSUER")),
        client_id=_env("BALUARTE_OIDC_CLIENT_ID"),
        client_secret=client_secret,
        scopes=scopes,
        roles_claim=_env("BALUARTE_OIDC_ROLES_CLAIM") or "groups",
        group_roles=group_roles,
        session_idle=idle,
        session_lifetime=lifetime,
    )
