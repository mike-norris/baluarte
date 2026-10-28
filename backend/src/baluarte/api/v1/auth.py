"""Sign-in, sign-out and current-user endpoints.

``/auth/login`` and ``/auth/callback`` are browser navigations, so failures redirect to the
web app's ``/sign-in`` page with a coarse error code. The detailed reason goes to the audit
trail and logs, never to the browser.
"""

import hmac
import logging
import re
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.auth.dependencies import (
    CSRF_HEADER,
    Principal,
    auth_settings,
    current_principal,
    database,
    oidc_client,
)
from baluarte.auth.oidc import OidcClient, OidcError, new_pending_login
from baluarte.auth.roles import Permission, Role, roles_from_claim
from baluarte.auth.sessions import (
    LOGIN_TTL,
    create_session,
    revoke_session,
    revoke_token,
    save_pending_login,
    take_pending_login,
    upsert_user,
)
from baluarte.auth.settings import AuthSettings
from baluarte.db.models import UserSession

logger = logging.getLogger(__name__)

# Reachable without a session. Everything else is mounted behind current_principal.
public_router = APIRouter(prefix="/auth", tags=["auth"])
router = APIRouter(prefix="/auth", tags=["auth"])

Settings = Annotated[AuthSettings, Depends(auth_settings)]
Database = Annotated[Session, Depends(database)]
Client = Annotated[OidcClient, Depends(oidc_client)]
SignedIn = Annotated[Principal, Depends(current_principal)]

SIGN_IN_PAGE = "/sign-in"
_PROVIDER_ERROR = re.compile(r"^[a-z_]{1,64}$")


class CurrentUser(BaseModel):
    subject: str
    email: str | None
    display_name: str | None
    roles: list[Role]
    permissions: list[Permission]
    session_expires_at: datetime
    csrf_token: str


def safe_return_to(value: str | None) -> str:
    """Accept only a same-origin absolute path, so sign-in cannot redirect off-site."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    if any(ord(char) < 0x21 or ord(char) == 0x7F for char in value):
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return "/"
    return value


def _cookie_options(settings: AuthSettings) -> dict[str, Any]:
    return {"path": "/", "secure": settings.secure_cookies, "httponly": True, "samesite": "lax"}


def _to_sign_in_page(settings: AuthSettings, error: str) -> RedirectResponse:
    response = RedirectResponse(
        f"{SIGN_IN_PAGE}?{urlencode({'error': error})}", status.HTTP_303_SEE_OTHER
    )
    response.delete_cookie(settings.login_cookie, **_cookie_options(settings))
    return response


def _failed(
    db: Session,
    settings: AuthSettings,
    error: str,
    reason: str,
    subject: str | None = None,
) -> RedirectResponse:
    logger.warning("auth: sign-in failed (%s)", reason)
    audit.record(
        db,
        actor=f"oidc:{subject}" if subject else "anonymous",
        action="auth.login_failed",
        resource_type="session",
        details={"reason": reason},
    )
    db.commit()
    return _to_sign_in_page(settings, error)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


@public_router.get(
    "/login",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    summary="Start OIDC sign-in",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Not configured"}},
)
def login(
    settings: Settings,
    db: Database,
    client: Client,
    return_to: Annotated[str | None, Query(max_length=2048)] = None,
) -> RedirectResponse:
    pending = new_pending_login()
    try:
        location = client.authorization_url(pending)
    except OidcError as exc:
        logger.warning("auth: sign-in could not start (%s)", exc.reason)
        return _to_sign_in_page(settings, "provider_unavailable")
    save_pending_login(db, pending, safe_return_to(return_to))
    db.commit()
    response = RedirectResponse(location, status.HTTP_302_FOUND)
    # Binds the callback to the browser that started sign-in (login CSRF protection).
    response.set_cookie(
        settings.login_cookie,
        pending.state,
        max_age=int(LOGIN_TTL.total_seconds()),
        **_cookie_options(settings),
    )
    return response


@public_router.get(
    "/callback",
    status_code=status.HTTP_303_SEE_OTHER,
    response_class=RedirectResponse,
    summary="Complete OIDC sign-in (identity provider redirect target)",
)
def callback(
    request: Request,
    settings: Settings,
    db: Database,
    client: Client,
    state: Annotated[str | None, Query(max_length=512)] = None,
    code: Annotated[str | None, Query(max_length=4096)] = None,
    error: Annotated[str | None, Query(max_length=256)] = None,
) -> RedirectResponse:
    browser_state = request.cookies.get(settings.login_cookie)
    if not state or not browser_state or not hmac.compare_digest(state, browser_state):
        # Not tied to a sign-in this browser started. Logged but not audited, so anonymous
        # requests cannot add rows to the append-only audit trail.
        logger.warning("auth: callback without a matching sign-in")
        return _to_sign_in_page(settings, "expired")
    pending = take_pending_login(db, state)
    if pending is None:
        db.rollback()
        logger.warning("auth: callback for an unknown or expired sign-in")
        return _to_sign_in_page(settings, "expired")

    if error is not None:
        code_name = error if _PROVIDER_ERROR.match(error) else "unrecognised"
        return _failed(db, settings, "provider_denied", f"provider_error:{code_name}")
    if not code:
        return _failed(db, settings, "sign_in_failed", "missing_code")

    try:
        tokens = client.exchange_code(code, pending.code_verifier)
        claims = client.validate_id_token(tokens["id_token"], pending.nonce)
        subject: str = claims["sub"]
        profile = claims
        access_token = tokens.get("access_token")
        if settings.roles_claim not in claims and isinstance(access_token, str):
            profile = {**client.userinfo(access_token, subject), **claims}
    except OidcError as exc:
        return _failed(db, settings, "sign_in_failed", exc.reason)

    roles = roles_from_claim(profile.get(settings.roles_claim), settings.group_roles)
    if not roles:
        return _failed(db, settings, "not_authorized", "no_mapped_role", subject)

    user = upsert_user(
        db,
        settings.issuer,
        subject,
        email=_text(profile.get("email")),
        display_name=_text(profile.get("name")) or _text(profile.get("preferred_username")),
    )
    # A fresh session per sign-in; any session this browser already had is ended.
    previous = request.cookies.get(settings.session_cookie)
    if previous:
        revoke_token(db, previous)
    token = create_session(db, user, roles, settings)
    db.flush()
    audit.record(
        db,
        actor=f"user:{user.id}",
        action="auth.login",
        resource_type="session",
        details={"roles": sorted(roles)},
    )
    db.commit()

    response = RedirectResponse(pending.return_to, status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        settings.session_cookie,
        token,
        max_age=int(settings.session_lifetime.total_seconds()),
        **_cookie_options(settings),
    )
    response.delete_cookie(settings.login_cookie, **_cookie_options(settings))
    return response


@router.get("/me", response_model=CurrentUser, summary="Current user and session")
def me(principal: SignedIn) -> CurrentUser:
    return CurrentUser(
        subject=principal.subject,
        email=principal.email,
        display_name=principal.display_name,
        roles=sorted(principal.roles),
        permissions=sorted(principal.permissions),
        session_expires_at=principal.session_expires_at,
        csrf_token=principal.csrf_token,
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out",
    description=f"Requires the `{CSRF_HEADER}` header with the token from `GET /auth/me`.",
)
def logout(principal: SignedIn, settings: Settings, db: Database) -> Response:
    revoke_session(db, db.get_one(UserSession, principal.session_id))
    audit.record(
        db,
        actor=principal.actor,
        action="auth.logout",
        resource_type="session",
        resource_id=str(principal.session_id),
    )
    db.commit()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(settings.session_cookie, **_cookie_options(settings))
    return response
