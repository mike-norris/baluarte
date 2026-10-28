"""Connection and permission diagnostics for the configured GitLab token.

Each check is ``ok``, ``warning``, ``failed`` or ``unknown``. A check that could not run is
``unknown``, never ``ok``, and the overall status is only ``ok`` when every check is.

GitLab behaviour these checks rely on (verified against mocked responses only; see
docs/GITLAB_INTEGRATION.md):

- ``GET /version`` requires authentication and returns ``version`` such as ``17.5.1-ee``.
- ``GET /personal_access_tokens/self`` (GitLab 15.5+) describes the calling token, including
  group and project access tokens: ``scopes``, ``active``, ``revoked``, ``expires_at``.
- ``GET /user`` includes ``is_admin`` only when the caller is an administrator.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel

from baluarte.integrations.gitlab.client import GitLabClient, GitLabError, NotFound

Status = Literal["ok", "warning", "failed", "unknown"]

READ_SCOPES = frozenset({"read_api"})
# Scopes that let the token change GitLab. v0.1 only reads (ADR-0003).
WRITE_SCOPES = frozenset(
    {
        "api",
        "write_repository",
        "write_registry",
        "sudo",
        "admin_mode",
        "create_runner",
        "manage_runner",
        "k8s_proxy",
        "ai_features",
    }
)
EXPIRY_WARNING = timedelta(days=30)


class Check(BaseModel):
    id: str
    status: Status
    summary: str
    details: dict[str, Any] = {}


class DiagnosticsReport(BaseModel):
    status: Literal["ok", "warning", "failed"]
    checked_at: datetime
    gitlab_url: str | None
    version: str | None
    checks: list[Check]


def overall(checks: list[Check]) -> Literal["ok", "warning", "failed"]:
    statuses = {check.status for check in checks}
    if "failed" in statuses:
        return "failed"
    if statuses - {"ok"}:
        return "warning"
    return "ok"


def not_configured(reason: str) -> DiagnosticsReport:
    return DiagnosticsReport(
        status="failed",
        checked_at=datetime.now(UTC),
        gitlab_url=None,
        version=None,
        checks=[Check(id="configuration", status="failed", summary=reason)],
    )


def _skipped(check_id: str, because: str) -> Check:
    return Check(id=check_id, status="unknown", summary=f"Not run: {because}")


def _error(check_id: str, error: GitLabError) -> Check:
    return Check(id=check_id, status="failed", summary=str(error), details={"reason": error.reason})


def check_connectivity(client: GitLabClient) -> tuple[Check, str | None]:
    try:
        body = client.get_json("/version")
    except GitLabError as error:
        return _error("connectivity", error), None
    version = body.get("version") if isinstance(body, dict) else None
    if not isinstance(version, str):
        return Check(
            id="connectivity",
            status="unknown",
            summary="Connected, but /version did not report a version",
        ), None
    return Check(
        id="connectivity",
        status="ok",
        summary=f"Connected to GitLab {version}",
        details={"version": version},
    ), version


def check_token(client: GitLabClient, today: date) -> Check:
    try:
        token = client.get_json("/personal_access_tokens/self")
    except NotFound:
        return Check(
            id="token",
            status="unknown",
            summary="Token details are unavailable (GitLab before 15.5, or not a personal, "
            "group or project access token); scopes and expiry cannot be checked",
        )
    except GitLabError as error:
        return _error("token", error)
    if not isinstance(token, dict):
        return Check(id="token", status="unknown", summary="Unexpected token details response")

    scopes = sorted(str(scope) for scope in token.get("scopes") or [])
    expires_at = token.get("expires_at")
    details: dict[str, Any] = {
        "name": token.get("name"),
        "scopes": scopes,
        "expires_at": expires_at,
    }
    if token.get("revoked") or token.get("active") is False:
        return Check(
            id="token", status="failed", summary="Token is revoked or inactive", details=details
        )

    expiry: date | None = None
    if isinstance(expires_at, str):
        try:
            expiry = date.fromisoformat(expires_at[:10])
        except ValueError:
            expiry = None
    if expiry is not None and expiry < today:
        return Check(
            id="token", status="failed", summary=f"Token expired on {expiry}", details=details
        )

    granted = set(scopes)
    if not granted & (READ_SCOPES | {"api"}):
        return Check(
            id="token",
            status="failed",
            summary="Token lacks the read_api scope Baluarte needs",
            details=details,
        )
    excess = sorted(granted & WRITE_SCOPES)
    if excess:
        return Check(
            id="token",
            status="warning",
            summary=f"Token has write scopes ({', '.join(excess)}); Baluarte needs only read_api",
            details=details,
        )
    if expiry is not None and expiry - today <= EXPIRY_WARNING:
        return Check(
            id="token", status="warning", summary=f"Token expires on {expiry}", details=details
        )
    return Check(id="token", status="ok", summary="Token has read_api only", details=details)


def check_user(client: GitLabClient) -> Check:
    try:
        user = client.get_json("/user")
    except GitLabError as error:
        return _error("user", error)
    if not isinstance(user, dict):
        return Check(id="user", status="unknown", summary="Unexpected user response")
    details = {
        "username": user.get("username"),
        "bot": bool(user.get("bot")),
        "is_admin": bool(user.get("is_admin")),
        "state": user.get("state"),
    }
    if user.get("state") not in (None, "active"):
        return Check(
            id="user",
            status="failed",
            summary=f"Token user is {user.get('state')}",
            details=details,
        )
    if user.get("is_admin"):
        return Check(
            id="user",
            status="warning",
            summary="Token belongs to an administrator; prefer a non-admin or auditor user "
            "with read_api",
            details=details,
        )
    return Check(
        id="user", status="ok", summary=f"Token user is {user.get('username')}", details=details
    )


def check_group_visibility(client: GitLabClient) -> Check:
    try:
        groups = client.get_json("/groups", {"per_page": 1, "all_available": "true"})
    except GitLabError as error:
        return _error("group_visibility", error)
    if not isinstance(groups, list):
        return Check(id="group_visibility", status="unknown", summary="Unexpected groups response")
    if not groups:
        return Check(
            id="group_visibility",
            status="warning",
            summary="The token can see no groups; inventory would be empty",
        )
    return Check(id="group_visibility", status="ok", summary="The token can list groups")


def run_diagnostics(
    client: GitLabClient,
    gitlab_url: str,
    today: Callable[[], date] = lambda: datetime.now(UTC).date(),
) -> DiagnosticsReport:
    connectivity, version = check_connectivity(client)
    checks = [connectivity]
    if connectivity.status == "failed":
        because = "GitLab could not be reached with this token"
        checks += [_skipped(name, because) for name in ("token", "user", "group_visibility")]
    else:
        checks += [check_token(client, today()), check_user(client), check_group_visibility(client)]
    return DiagnosticsReport(
        status=overall(checks),
        checked_at=datetime.now(UTC),
        gitlab_url=gitlab_url,
        version=version,
        checks=checks,
    )
