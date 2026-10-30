"""FastAPI dependencies that authenticate requests and enforce permissions.

Routers under the protected API depend on ``current_principal``, so a new endpoint requires a
session unless it is deliberately added to the public router.
"""

import hmac
import logging
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

import httpx
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from baluarte.auth.oidc import OidcClient
from baluarte.auth.roles import Permission, Role, permissions_for
from baluarte.auth.sessions import csrf_token, resolve_session
from baluarte.auth.settings import AuthSettings
from baluarte.config import ConfigurationError
from baluarte.configuration.effective import effective_auth_settings
from baluarte.db.session import get_engine

logger = logging.getLogger(__name__)

CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
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
    return Principal(
        user_id=active.user.id,
        subject=active.user.subject,
        email=active.user.email,
        display_name=active.user.display_name,
        roles=frozenset(Role(role) for role in active.session.roles),
        session_id=active.session.id,
        session_expires_at=active.session.expires_at,
        csrf_token=csrf,
    )


def require(permission: Permission) -> Callable[[Principal], Principal]:
    """Dependency factory: the request's principal must hold ``permission``."""

    def dependency(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
        if permission not in principal.permissions:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")
        return principal

    return dependency
