"""Viewing, changing and testing the identity provider and GitLab settings (ADR-0008).

Shared by the setup wizard and the admin Settings page. Secret values are accepted but never
returned. A stored secret is only ever sent to the host it was entered for: changing the issuer
or the GitLab URL requires entering the secret again.
"""

import logging
from typing import Any, Literal

import httpx
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.auth.roles import Role
from baluarte.auth.settings import (
    AuthSettings,
    deployment_auth_present,
    issuer_url,
    load_auth_settings,
    stored_auth_settings,
)
from baluarte.configuration import effective
from baluarte.configuration.crypto import SecretBox, load_secret_box
from baluarte.configuration.schemas import (
    CheckResult,
    GitLabConfig,
    GitLabTest,
    GitLabTestResult,
    GitLabUpdate,
    GitLabUser,
    OidcConfig,
    OidcTestResult,
    OidcUpdate,
    SectionStatus,
    SetupStatus,
)
from baluarte.configuration.setup import setup_state
from baluarte.configuration.store import (
    GITLAB,
    GITLAB_TOKEN,
    OIDC,
    OIDC_CLIENT_SECRET,
    auth_from_value,
    delete_secret,
    delete_setting,
    read_secret_value,
    read_setting,
    secret_stored,
    write_secret,
    write_setting,
)
from baluarte.integrations.gitlab.client import GitLabClient
from baluarte.integrations.gitlab.diagnostics import Check, run_diagnostics
from baluarte.integrations.gitlab.settings import (
    GitLabSettings,
    checked_interval,
    checked_timeout,
    deployment_gitlab_present,
    gitlab_url,
    load_deployment_gitlab_settings,
)
from baluarte.logs import register_secret
from baluarte.settings import ConfigurationError

logger = logging.getLogger(__name__)

Via = Literal["setup", "settings"]

DISCOVERY_TIMEOUT = httpx.Timeout(10.0)
MAX_SCOPES_REPORTED = 100
CANNOT_STORE = (
    "BALUARTE_SECRET_KEY_FILE is not configured, so secrets cannot be stored; see docs/SECURITY.md"
)


class SettingsConflict(Exception):
    """The change is not allowed in the current state (HTTP 409)."""


class SettingsInvalid(Exception):
    """The values are invalid (HTTP 422)."""


class NotStored(Exception):
    """Nothing is stored for this section (HTTP 404)."""


def _secret_box() -> SecretBox | None:
    try:
        return load_secret_box()
    except ConfigurationError as exc:
        raise SettingsConflict(str(exc)) from exc


def secret_key_configured() -> bool:
    try:
        return load_secret_box() is not None
    except ConfigurationError:
        return False


def _finish(db: Session) -> None:
    db.commit()
    effective.invalidate()


# Status


def setup_status(db: Session, has_session: bool) -> SetupStatus:
    state = setup_state(db)
    if state == "complete":
        return SetupStatus(
            state=state, setup_session=False, secret_key_configured=None, oidc=None, gitlab=None
        )
    oidc_stored = read_setting(db, OIDC) is not None
    if deployment_gitlab_present():
        gitlab = SectionStatus(source="deployment", configured=True)
    elif read_setting(db, GITLAB) is not None:
        gitlab = SectionStatus(source="database", configured=True)
    else:
        gitlab = SectionStatus(source=None, configured=False)
    return SetupStatus(
        state=state,
        setup_session=has_session,
        secret_key_configured=secret_key_configured(),
        oidc=SectionStatus(source="database" if oidc_stored else None, configured=oidc_stored),
        gitlab=gitlab,
    )


# Identity provider


def _oidc_out(settings: AuthSettings, secret: bool) -> OidcConfig:
    return OidcConfig(
        source=settings.source,
        public_url=settings.public_url,
        issuer=settings.issuer,
        client_id=settings.client_id,
        client_secret="configured" if secret else "not_set",
        scopes=settings.scopes,
        roles_claim=settings.roles_claim,
        admin_groups=sorted(settings.group_roles.get(Role.ADMIN, ())),
        analyst_groups=sorted(settings.group_roles.get(Role.ANALYST, ())),
        auditor_groups=sorted(settings.group_roles.get(Role.AUDITOR, ())),
        redirect_uri=settings.redirect_uri,
    )


def oidc_config(db: Session) -> OidcConfig:
    """The identity provider settings in force; NotStored when there are none."""
    if deployment_auth_present():
        settings = load_auth_settings()
        if settings is None:  # pragma: no cover - present means configured or an error
            raise NotStored
        return _oidc_out(settings, settings.client_secret is not None)
    value = read_setting(db, OIDC)
    if value is None:
        raise NotStored
    return _oidc_out(auth_from_value(value, None), secret_stored(db, OIDC_CLIENT_SECRET))


def update_oidc(db: Session, update: OidcUpdate, actor: str, via: Via) -> OidcConfig:
    if deployment_auth_present():
        raise SettingsConflict("Identity provider settings are managed by the deployment")
    if update.client_secret is not None and update.clear_client_secret:
        raise SettingsInvalid("Send client_secret or clear_client_secret, not both")
    box = _secret_box()
    if update.client_secret is not None and box is None:
        raise SettingsConflict(CANNOT_STORE)

    groups = {
        "admin": sorted({group.strip() for group in update.admin_groups if group.strip()}),
        "analyst": sorted({group.strip() for group in update.analyst_groups if group.strip()}),
        "auditor": sorted({group.strip() for group in update.auditor_groups if group.strip()}),
    }
    try:
        checked = stored_auth_settings(
            public_url_value=update.public_url.strip(),
            issuer=update.issuer.strip(),
            client_id=update.client_id,
            client_secret=None,
            scopes=update.scopes,
            roles_claim=update.roles_claim,
            groups=groups,
        )
    except ConfigurationError as exc:
        raise SettingsInvalid(str(exc)) from exc

    previous = read_setting(db, OIDC)
    keeps_secret = update.client_secret is None and not update.clear_client_secret
    if (
        keeps_secret
        and secret_stored(db, OIDC_CLIENT_SECRET)
        and (previous is None or previous.get("issuer") != checked.issuer)
    ):
        raise SettingsInvalid("Enter the client secret again when changing the issuer, or clear it")

    write_setting(
        db,
        OIDC,
        {
            "public_url": checked.public_url,
            "issuer": checked.issuer,
            "client_id": checked.client_id,
            "scopes": checked.scopes,
            "roles_claim": checked.roles_claim,
            "groups": groups,
        },
        actor,
    )
    if update.client_secret is not None and box is not None:
        write_secret(db, OIDC_CLIENT_SECRET, update.client_secret, box, actor)
    elif update.clear_client_secret:
        delete_secret(db, OIDC_CLIENT_SECRET)
    audit.record(
        db,
        actor=actor,
        action="settings.oidc_updated",
        resource_type="settings",
        resource_id=OIDC,
        details={
            "via": via,
            "public_url": checked.public_url,
            "issuer": checked.issuer,
            "client_id": checked.client_id,
            "scopes": checked.scopes,
            "roles_claim": checked.roles_claim,
            "admin_groups": groups["admin"],
            "analyst_groups": groups["analyst"],
            "auditor_groups": groups["auditor"],
            "client_secret_replaced": update.client_secret is not None,
            "client_secret_cleared": update.clear_client_secret,
        },
    )
    _finish(db)
    return oidc_config(db)


def check_oidc_discovery(issuer: str, transport: httpx.BaseTransport | None) -> OidcTestResult:
    """Fetch the issuer's discovery document. Nothing is stored."""
    try:
        expected = issuer_url(issuer.strip(), "issuer")
    except ConfigurationError as exc:
        raise SettingsInvalid(str(exc)) from exc

    def failed(error: str, reported: str | None = None) -> OidcTestResult:
        return OidcTestResult(
            ok=False,
            issuer=reported,
            authorization_endpoint=None,
            scopes_supported=[],
            error=error,
        )

    url = expected + "/.well-known/openid-configuration"
    # Certificate verification stays on; trust_env lets SSL_CERT_FILE add a private CA.
    with httpx.Client(
        transport=transport, timeout=DISCOVERY_TIMEOUT, follow_redirects=False
    ) as client:
        try:
            response = client.get(url, headers={"Accept": "application/json"})
        except httpx.ConnectError as exc:
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                return failed(
                    "TLS certificate verification failed; for a private CA set SSL_CERT_FILE"
                )
            return failed("The identity provider could not be reached")
        except httpx.TimeoutException:
            return failed("The identity provider did not answer in time")
        except httpx.HTTPError as exc:
            return failed(f"Discovery failed ({type(exc).__name__})")
    if response.status_code != 200:
        return failed(f"Discovery returned HTTP {response.status_code} for {url}")
    try:
        document = response.json()
    except ValueError:
        return failed("Discovery did not return JSON")
    if not isinstance(document, dict):
        return failed("Discovery did not return a JSON object")
    reported = document.get("issuer")
    reported_issuer = reported if isinstance(reported, str) else None
    if reported_issuer != expected:
        return failed(
            f"The provider reports issuer {reported_issuer!r}; it must match {expected!r} exactly",
            reported_issuer,
        )
    endpoint = document.get("authorization_endpoint")
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        return failed("Discovery has no https authorization_endpoint", reported_issuer)
    scopes = document.get("scopes_supported")
    supported = (
        [scope for scope in scopes if isinstance(scope, str)][:MAX_SCOPES_REPORTED]
        if isinstance(scopes, list)
        else []
    )
    return OidcTestResult(
        ok=True,
        issuer=reported_issuer,
        authorization_endpoint=endpoint,
        scopes_supported=supported,
        error=None,
    )


# GitLab


def _gitlab_out(settings: GitLabSettings, token: bool) -> GitLabConfig:
    return GitLabConfig(
        source=settings.source,
        url=settings.url,
        token="configured" if token else "not_set",
        timeout_seconds=settings.timeout_seconds,
        sync_interval_minutes=settings.sync_interval_minutes,
    )


def gitlab_config(db: Session) -> GitLabConfig:
    """The GitLab settings in force; NotStored when there are none."""
    if deployment_gitlab_present():
        settings = load_deployment_gitlab_settings()
        if settings is None:  # pragma: no cover - present means configured or an error
            raise NotStored
        return _gitlab_out(settings, True)
    value = read_setting(db, GITLAB)
    if value is None:
        raise NotStored
    return GitLabConfig(
        source="database",
        url=str(value.get("url", "")),
        token="configured" if secret_stored(db, GITLAB_TOKEN) else "not_set",
        timeout_seconds=float(value.get("timeout_seconds", 15)),
        sync_interval_minutes=int(value.get("sync_interval_minutes", 360)),
    )


def update_gitlab(db: Session, update: GitLabUpdate, actor: str, via: Via) -> GitLabConfig:
    if deployment_gitlab_present():
        raise SettingsConflict("GitLab settings are managed by the deployment")
    try:
        url = gitlab_url(update.url.strip(), "url")
        timeout = checked_timeout(update.timeout_seconds, "timeout_seconds")
        interval = checked_interval(update.sync_interval_minutes, "sync_interval_minutes")
    except ConfigurationError as exc:
        raise SettingsInvalid(str(exc)) from exc
    box = _secret_box()
    previous = read_setting(db, GITLAB)
    if update.token is None:
        if not secret_stored(db, GITLAB_TOKEN):
            raise SettingsInvalid("A read_api token is required")
        if previous is None or previous.get("url") != url:
            raise SettingsInvalid("Enter the token again when changing the GitLab URL")
    elif box is None:
        raise SettingsConflict(CANNOT_STORE)

    write_setting(
        db,
        GITLAB,
        {"url": url, "timeout_seconds": timeout, "sync_interval_minutes": interval},
        actor,
    )
    if update.token is not None and box is not None:
        write_secret(db, GITLAB_TOKEN, update.token, box, actor)
    audit.record(
        db,
        actor=actor,
        action="settings.gitlab_updated",
        resource_type="settings",
        resource_id=GITLAB,
        details={
            "via": via,
            "url": url,
            "timeout_seconds": timeout,
            "sync_interval_minutes": interval,
            "token_replaced": update.token is not None,
        },
    )
    _finish(db)
    return gitlab_config(db)


def disconnect_gitlab(db: Session, actor: str) -> None:
    if deployment_gitlab_present():
        raise SettingsConflict("GitLab settings are managed by the deployment")
    previous = read_setting(db, GITLAB)
    if previous is None:
        raise NotStored
    delete_setting(db, GITLAB)
    delete_secret(db, GITLAB_TOKEN)
    audit.record(
        db,
        actor=actor,
        action="settings.gitlab_disconnected",
        resource_type="settings",
        resource_id=GITLAB,
        details={"url": previous.get("url")},
    )
    _finish(db)


_CHECK_STATUS: dict[str, Literal["pass", "warn", "fail"]] = {
    "ok": "pass",
    "warning": "warn",
    "unknown": "warn",
    "failed": "fail",
}


def _token_to_test(db: Session, url: str, token: str | None) -> str:
    if token is not None:
        register_secret(token)
        return token
    if deployment_gitlab_present():
        settings = load_deployment_gitlab_settings()
        stored_url, stored_token = (settings.url, settings.token) if settings else (None, None)
    else:
        value = read_setting(db, GITLAB)
        stored_url = str(value.get("url")) if value else None
        stored_token = read_secret_value(db, GITLAB_TOKEN, _secret_box()) if value else None
    if stored_token is None or stored_url != url:
        raise SettingsInvalid("Enter a token to test this URL")
    return stored_token


def _detail(check: Check | None, key: str) -> Any:
    return check.details.get(key) if check is not None else None


def check_gitlab_connection(
    db: Session, body: GitLabTest, transport: httpx.BaseTransport | None
) -> GitLabTestResult:
    """Run the connection diagnostics with these values. Nothing is stored."""
    try:
        url = gitlab_url(body.url.strip(), "url")
    except ConfigurationError as exc:
        raise SettingsInvalid(str(exc)) from exc
    try:
        token = _token_to_test(db, url, body.token)
    except ConfigurationError as exc:
        raise SettingsConflict(str(exc)) from exc
    # No database transaction stays open while GitLab is called.
    db.rollback()
    settings = GitLabSettings(url=url, token=token)
    with GitLabClient(settings, transport=transport) as client:
        report = run_diagnostics(client, url)

    checks = {check.id: check for check in report.checks}
    user_check = checks.get("user")
    username = _detail(user_check, "username")
    token_check = checks.get("token")
    scopes = _detail(token_check, "scopes")
    expires_at = _detail(token_check, "expires_at")
    failed = next((check for check in report.checks if check.status == "failed"), None)
    return GitLabTestResult(
        ok=report.status != "failed",
        version=report.version,
        user=GitLabUser(username=username, is_admin=bool(_detail(user_check, "is_admin")))
        if isinstance(username, str)
        else None,
        token_scopes=[str(scope) for scope in scopes] if isinstance(scopes, list) else None,
        token_expires_at=expires_at if isinstance(expires_at, str) else None,
        checks=[
            CheckResult(name=check.id, status=_CHECK_STATUS[check.status], message=check.summary)
            for check in report.checks
        ],
        error=failed.summary if failed is not None else None,
    )
