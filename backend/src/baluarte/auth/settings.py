"""Authentication settings from the deployment's environment, or entered in the web UI.

Authentication is either fully configured or not configured at all. A partial configuration is
a ConfigurationError, so a typo never leaves the API running with weaker checks. Settings
entered in the web UI (ADR-0008) go through the same checks as the environment.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal
from urllib.parse import urlsplit

from baluarte.auth.roles import Role
from baluarte.settings import ConfigurationError, env, read_secret

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
    client_secret: str | None = field(repr=False)
    scopes: str
    roles_claim: str
    group_roles: dict[Role, frozenset[str]]
    session_idle: timedelta
    session_lifetime: timedelta
    # Where the settings came from: the environment, or the web UI (ADR-0008).
    source: Literal["deployment", "database"] = "deployment"

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


def public_url(raw: str, name: str = "BALUARTE_PUBLIC_URL") -> str:
    parts = urlsplit(raw)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ConfigurationError(f"{name} must be an absolute http(s) URL")
    if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
        raise ConfigurationError(f"{name} must use https unless it is a loopback host")
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
        raise ConfigurationError(f"{name} must be an origin with no path")
    return f"{parts.scheme}://{parts.netloc}"


def issuer_url(raw: str, name: str = "BALUARTE_OIDC_ISSUER") -> str:
    parts = urlsplit(raw)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        raise ConfigurationError(f"{name} must be an https URL")
    return raw.rstrip("/")


def checked_scopes(raw: str, name: str = "BALUARTE_OIDC_SCOPES") -> str:
    scopes = " ".join(raw.split()) or "openid profile email"
    if "openid" not in scopes.split():
        raise ConfigurationError(f"{name} must include openid")
    return scopes


def group_list(raw: Iterable[str]) -> frozenset[str]:
    return frozenset(group.strip() for group in raw if group.strip())


def _minutes(name: str, default: int) -> timedelta:
    raw = env(name)
    try:
        value = int(raw) if raw else default
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a whole number of minutes") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be positive")
    return timedelta(minutes=value)


def deployment_auth_present() -> bool:
    """Whether the environment configures sign-in, even partially (and so wins over the UI)."""
    return any(env(name) for name in _REQUIRED)


def session_lifetimes() -> tuple[timedelta, timedelta]:
    idle = _minutes("BALUARTE_SESSION_IDLE_MINUTES", 60)
    lifetime = _minutes("BALUARTE_SESSION_LIFETIME_MINUTES", 8 * 60)
    if idle > lifetime:
        raise ConfigurationError(
            "BALUARTE_SESSION_IDLE_MINUTES cannot exceed BALUARTE_SESSION_LIFETIME_MINUTES"
        )
    return idle, lifetime


def stored_auth_settings(
    *,
    public_url_value: str,
    issuer: str,
    client_id: str,
    client_secret: str | None,
    scopes: str,
    roles_claim: str,
    groups: Mapping[str, Iterable[str]],
) -> AuthSettings:
    """Settings entered in the web UI, checked as strictly as the environment."""
    if not client_id.strip():
        raise ConfigurationError("client_id must not be empty")
    group_roles = {role: group_list(groups.get(role.value, ())) for role in _GROUP_VARIABLES}
    if not group_roles[Role.ADMIN]:
        raise ConfigurationError("admin_groups must name at least one group")
    idle, lifetime = session_lifetimes()
    return AuthSettings(
        public_url=public_url(public_url_value, "public_url"),
        issuer=issuer_url(issuer, "issuer"),
        client_id=client_id.strip(),
        client_secret=client_secret,
        scopes=checked_scopes(scopes, "scopes"),
        roles_claim=roles_claim.strip() or "groups",
        group_roles=group_roles,
        session_idle=idle,
        session_lifetime=lifetime,
        source="database",
    )


def load_auth_settings() -> AuthSettings | None:
    """The deployment's settings, or None when its environment does not configure sign-in."""
    present = [name for name in _REQUIRED if env(name)]
    if not present:
        return None
    if len(present) != len(_REQUIRED):
        missing = ", ".join(name for name in _REQUIRED if name not in present)
        raise ConfigurationError(f"Authentication is partially configured; missing {missing}")

    client_secret = read_secret("BALUARTE_OIDC_CLIENT_SECRET")

    group_roles = {
        role: group_list(env(name).split(",")) for role, name in _GROUP_VARIABLES.items()
    }
    if not any(group_roles.values()):
        names = ", ".join(_GROUP_VARIABLES.values())
        raise ConfigurationError(f"No groups are mapped to roles; set at least one of {names}")

    scopes = checked_scopes(env("BALUARTE_OIDC_SCOPES"))
    idle, lifetime = session_lifetimes()

    return AuthSettings(
        public_url=public_url(env("BALUARTE_PUBLIC_URL")),
        issuer=issuer_url(env("BALUARTE_OIDC_ISSUER")),
        client_id=env("BALUARTE_OIDC_CLIENT_ID"),
        client_secret=client_secret,
        scopes=scopes,
        roles_claim=env("BALUARTE_OIDC_ROLES_CLAIM") or "groups",
        group_roles=group_roles,
        session_idle=idle,
        session_lifetime=lifetime,
    )
