"""FastAPI dependencies that authenticate requests and enforce permissions.

Routers under the protected API depend on ``current_principal``, so a new endpoint requires a
session unless it is deliberately added to the public router.
"""

import hmac
import logging
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated

import httpx
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from baluarte.auth.oidc import OidcClient
from baluarte.auth.roles import Permission, Role, permissions_for
from baluarte.auth.sessions import csrf_token, resolve_session
from baluarte.auth.settings import AuthSettings
from baluarte.config import ConfigurationError
from baluarte.configuration.effective import effective_auth_settings
from baluarte.db.models import GitLabInstance, Workspace
from baluarte.db.session import get_engine
from baluarte.workspaces import default_workspace_id, stored_roles, use

logger = logging.getLogger(__name__)

CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Which workspace (by slug) and connection (by ID) a request acts in; optional.
WORKSPACE_HEADER = "X-Baluarte-Workspace"
CONNECTION_HEADER = "X-Baluarte-Connection"
IDP_TIMEOUT = httpx.Timeout(10.0)

_UNAUTHENTICATED = HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")


def auth_settings() -> AuthSettings:
    """The sign-in settings in force: the deployment's, else those entered in the web UI."""
    try:
        settings = effective_auth_settings()
    except ConfigurationError as exc:
        logger.error("auth: %s", exc)
        settings = None
    if settings is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Authentication is not configured")
    return settings


_client_lock = threading.Lock()
_client: tuple[AuthSettings, OidcClient, httpx.Client] | None = None


def _oidc_client(settings: AuthSettings) -> OidcClient:
    """One client per process, rebuilt when the settings change in the web UI."""
    global _client
    with _client_lock:
        if _client is not None and _client[0] == settings:
            return _client[1]
        if _client is not None:
            _client[2].close()
        # Certificate verification stays on; trust_env lets SSL_CERT_FILE add a private CA.
        http = httpx.Client(timeout=IDP_TIMEOUT, follow_redirects=False)
        _client = (settings, OidcClient(settings, http), http)
        return _client[1]


def oidc_client(settings: Annotated[AuthSettings, Depends(auth_settings)]) -> OidcClient:
    return _oidc_client(settings)


def database_engine() -> Engine:
    try:
        return get_engine()
    except ConfigurationError as exc:
        logger.error("auth: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Database is not configured"
        ) from exc


def database(engine: Annotated[Engine, Depends(database_engine)]) -> Iterator[Session]:
    with Session(engine, expire_on_commit=False) as session:
        yield session


@dataclass(frozen=True)
class Principal:
    user_id: int
    subject: str
    email: str | None
    display_name: str | None
    roles: frozenset[Role]
    session_id: int
    session_expires_at: datetime
    csrf_token: str
    # The workspace the request acts in; ``roles`` are the caller's roles there. Tests that build
    # a principal directly act in the default workspace.
    workspace_id: int | None = None
    workspace_slug: str | None = None
    workspaces: Mapping[int, frozenset[Role]] = field(default_factory=dict)
    # Whether that workspace is the default one, where deployment settings are managed.
    in_default_workspace: bool = True
    # How a second factor was proven at sign-in, or None.
    mfa_method: str | None = None

    @property
    def permissions(self) -> frozenset[Permission]:
        return permissions_for(self.roles)

    @property
    def actor(self) -> str:
        return f"user:{self.user_id}"


def _check_csrf(request: Request, settings: AuthSettings, expected: str) -> None:
    origin = request.headers.get("Origin")
    if origin is not None and origin != settings.public_url:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-origin request rejected")
    supplied = request.headers.get(CSRF_HEADER, "")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing or invalid CSRF token")


def current_principal(
    request: Request,
    settings: Annotated[AuthSettings, Depends(auth_settings)],
    db: Annotated[Session, Depends(database)],
) -> Principal:
    token = request.cookies.get(settings.session_cookie)
    if not token:
        raise _UNAUTHENTICATED
    active = resolve_session(db, token, settings)
    if active is None:
        raise _UNAUTHENTICATED
    csrf = csrf_token(token)
    if request.method not in SAFE_METHODS:
        _check_csrf(request, settings, csrf)
    default_id = default_workspace_id(db)
    held = stored_roles(active.session.workspace_roles, active.session.roles, default_id)
    workspace = _workspace(request, db, held)
    use(db, workspace.id, _connection(request, db, workspace.id))
    return Principal(
        user_id=active.user.id,
        subject=active.user.subject,
        email=active.user.email,
        display_name=active.user.display_name,
        roles=held[workspace.id],
        session_id=active.session.id,
        session_expires_at=active.session.expires_at,
        mfa_method=active.session.mfa_method,
        csrf_token=csrf,
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        workspaces=held,
        in_default_workspace=workspace.id == default_id,
    )


def _workspace(request: Request, db: Session, held: Mapping[int, frozenset[Role]]) -> Workspace:
    """The workspace the request names, or the caller's default one; 403 without a role there."""
    slug = request.headers.get(WORKSPACE_HEADER)
    if slug:
        row = db.scalars(select(Workspace).where(Workspace.slug == slug)).first()
        if row is None or row.id not in held:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You have no role in this workspace")
        return row
    if not held:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You have no role in any workspace")
    default_id = default_workspace_id(db)
    return db.get_one(Workspace, default_id if default_id in held else min(held))


def _connection(request: Request, db: Session, workspace_id: int) -> int | None:
    """The connection the request names, checked to be in the workspace; None when none named."""
    raw = request.headers.get(CONNECTION_HEADER)
    if not raw:
        return None
    if not raw.isdigit():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{CONNECTION_HEADER} must be an ID")
    row = db.get(GitLabInstance, int(raw))
    if row is None or row.removed_at is not None or row.workspace_id != workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such connection in this workspace")
    return row.id


def require_deployment(permission: Permission) -> Callable[[Principal], Principal]:
    """Like ``require``, for deployment-wide settings: only while acting in the default workspace.

    Administrators of other workspaces hold ``settings:manage`` for their own connections, not for
    the identity provider, the primary GitLab, write tokens, notifications or Baluarte's own
    compliance, which every workspace shares.
    """

    def dependency(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
        if permission not in principal.permissions:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")
        if not principal.in_default_workspace:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Deployment settings are managed from the default workspace",
            )
        return principal

    return dependency


def require(permission: Permission) -> Callable[[Principal], Principal]:
    """Dependency factory: the request's principal must hold ``permission``."""

    def dependency(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
        if permission not in principal.permissions:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")
        return principal

    return dependency
