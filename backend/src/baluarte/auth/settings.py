"""Authentication settings from the deployment's environment, or entered in the web UI.

Authentication is either fully configured or not configured at all. A partial configuration is
a ConfigurationError, so a typo never leaves the API running with weaker checks. Settings
entered in the web UI (ADR-0008) go through the same checks as the environment.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal, get_args
from urllib.parse import urlsplit

from baluarte.auth.roles import Role
from baluarte.settings import ConfigurationError, env, read_secret

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
CALLBACK_PATH = "/api/v1/auth/callback"

SESSION_IDLE_ENV = "BALUARTE_SESSION_IDLE_MINUTES"
SESSION_LIFETIME_ENV = "BALUARTE_SESSION_LIFETIME_MINUTES"
# Used when neither the environment nor the operating profile (baluarte.compliance) sets them.
DEFAULT_SESSION_IDLE_MINUTES = 60
DEFAULT_SESSION_LIFETIME_MINUTES = 8 * 60

_REQUIRED = ("BALUARTE_PUBLIC_URL", "BALUARTE_OIDC_ISSUER", "BALUARTE_OIDC_CLIENT_ID")
_GROUP_VARIABLES = {
    Role.ADMIN: "BALUARTE_OIDC_ADMIN_GROUPS",
    Role.ANALYST: "BALUARTE_OIDC_ANALYST_GROUPS",
    Role.AUDITOR: "BALUARTE_OIDC_AUDITOR_GROUPS",
    Role.MANAGER: "BALUARTE_OIDC_MANAGER_GROUPS",
}


# Multi-factor authentication (docs/SECURITY.md#multi-factor-authentication). The identity
# provider proves MFA in the ID token's ``amr`` claim (RFC 8176); a preset knows how a provider
# says it and how to ask for it (``acr_values``). Values here are names, never secrets.
Preset = Literal["generic", "okta", "auth0"]
PRESETS: tuple[Preset, ...] = get_args(Preset)
MFA_AMR = frozenset({"mfa"})


@dataclass(frozen=True)
class PresetDefaults:
    label: str
    # Sent as acr_values to ask the provider for MFA; None sends nothing.
    acr_values: str | None
    # amr values that prove a second factor.
    amr: frozenset[str]


PRESET_DEFAULTS: dict[Preset, PresetDefaults] = {
    "generic": PresetDefaults("Any OpenID Connect provider", None, MFA_AMR),
    # Okta Identity Engine: any two factors.
    "okta": PresetDefaults("Okta", "urn:okta:loa:2fa:any", MFA_AMR),
    # Auth0 asks for MFA with the PAPE multi-factor policy and reports it as amr ["mfa"].
    "auth0": PresetDefaults(
        "Auth0", "http://schemas.openid.net/pape/policies/2007/06/multi-factor", MFA_AMR
    ),
}

PRESET_ENV = "BALUARTE_OIDC_PRESET"
REQUIRE_MFA_ENV = "BALUARTE_OIDC_REQUIRE_MFA"
MFA_AMR_ENV = "BALUARTE_OIDC_MFA_AMR"
MFA_TOTP_ENV = "BALUARTE_OIDC_MFA_TOTP"
ACR_VALUES_ENV = "BALUARTE_OIDC_ACR_VALUES"
_AMR_VALUE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_ACR_VALUES = re.compile(r"^[\x21-\x7e]+( [\x21-\x7e]+)*$")


@dataclass(frozen=True)
class MfaPolicy:
    preset: Preset = "generic"
    # Sign-in is refused unless the ID token proves MFA.
    required: bool = False
    amr: frozenset[str] = MFA_AMR
    acr_values: str | None = None
    # When MFA is required and the ID token does not prove it, ask for a code from the user's
    # authenticator app instead of refusing (docs/SECURITY.md#built-in-totp).
    totp: bool = False

    def proven(self, amr: object) -> list[str] | None:
        """The ID token's amr values when one of them proves MFA, else None."""
        values = [value for value in amr if isinstance(value, str)] if isinstance(amr, list) else []
        return values if self.amr.intersection(values) else None


def mfa_policy(
    preset: str,
    required: bool,
    amr: Iterable[str] | None = None,
    acr_values: str | None = None,
    names: tuple[str, str, str] = ("preset", "mfa_amr", "acr_values"),
    totp: bool = False,
) -> MfaPolicy:
    """Check an MFA policy. Empty ``amr`` and ``acr_values`` take the preset's."""
    preset_name, amr_name, acr_name = names
    chosen = preset.strip().lower() or "generic"
    if chosen not in PRESETS:
        raise ConfigurationError(f"{preset_name} must be one of {', '.join(PRESETS)}")
    defaults = PRESET_DEFAULTS[chosen]
    values = frozenset(value.strip() for value in (amr or ()) if value.strip())
    if len(values) > 20 or any(not _AMR_VALUE.match(value) for value in values):
        raise ConfigurationError(
            f"{amr_name} must be up to 20 amr values of letters, digits and . _ : -"
        )
    acr = " ".join((acr_values or "").split())
    if acr and (len(acr) > 512 or not _ACR_VALUES.match(acr)):
        raise ConfigurationError(f"{acr_name} must be space-separated printable values")
    return MfaPolicy(
        preset=chosen,
        required=required,
        amr=values or defaults.amr,
        acr_values=acr or defaults.acr_values,
        totp=totp,
    )


def _flag(name: str) -> bool:
    raw = env(name).lower()
    if raw in ("", "false"):
        return False
    if raw == "true":
        return True
    raise ConfigurationError(f"{name} must be true or false")


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
    mfa: MfaPolicy = MfaPolicy()

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
    """The environment's session limits, or the defaults. The operating profile replaces the
    defaults (``baluarte.configuration.effective``); the environment always wins."""
    idle = _minutes(SESSION_IDLE_ENV, DEFAULT_SESSION_IDLE_MINUTES)
    lifetime = _minutes(SESSION_LIFETIME_ENV, DEFAULT_SESSION_LIFETIME_MINUTES)
    if idle > lifetime:
        raise ConfigurationError(f"{SESSION_IDLE_ENV} cannot exceed {SESSION_LIFETIME_ENV}")
    return idle, lifetime


def deployment_session_minutes() -> tuple[int | None, int | None]:
    """The idle and absolute session limits the environment fixes, in minutes; None for each it
    does not set."""
    idle = _minutes(SESSION_IDLE_ENV, 1) if env(SESSION_IDLE_ENV) else None
    lifetime = _minutes(SESSION_LIFETIME_ENV, 1) if env(SESSION_LIFETIME_ENV) else None
    return (
        int(idle.total_seconds() // 60) if idle is not None else None,
        int(lifetime.total_seconds() // 60) if lifetime is not None else None,
    )


def stored_auth_settings(
    *,
    public_url_value: str,
    issuer: str,
    client_id: str,
    client_secret: str | None,
    scopes: str,
    roles_claim: str,
    groups: Mapping[str, Iterable[str]],
    mfa: MfaPolicy | None = None,
) -> AuthSettings:
    """Settings entered in the web UI, checked as strictly as the environment. ``mfa`` comes
    from ``mfa_policy``, which checks it."""
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
        mfa=mfa or MfaPolicy(),
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
        mfa=mfa_policy(
            env(PRESET_ENV),
            _flag(REQUIRE_MFA_ENV),
            env(MFA_AMR_ENV).split(","),
            env(ACR_VALUES_ENV),
            (PRESET_ENV, MFA_AMR_ENV, ACR_VALUES_ENV),
            _flag(MFA_TOTP_ENV),
        ),
    )
