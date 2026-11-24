"""Sign-in, sign-out and current-user endpoints.

``/auth/login`` and ``/auth/callback`` are browser navigations, so failures redirect to the
web app's ``/sign-in`` page with a coarse error code. The detailed reason goes to the audit
trail and logs, never to the browser.
"""

import hmac
import logging
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.auth import mfa, totp
from baluarte.auth.dependencies import (
    CSRF_HEADER,
    Principal,
    auth_settings,
    check_csrf,
    current_principal,
    database,
    oidc_client,
)
from baluarte.auth.oidc import OidcClient, OidcError, new_pending_login
from baluarte.auth.roles import Permission, Role
from baluarte.auth.sessions import (
    LOGIN_TTL,
    ActiveSession,
    complete_mfa,
    create_session,
    csrf_token,
    resolve_session,
    revoke_session,
    revoke_token,
    save_pending_login,
    take_pending_login,
    upsert_user,
)
from baluarte.auth.settings import AuthSettings
from baluarte.configuration.crypto import SecretBox, SecretUnavailable, load_secret_box
from baluarte.configuration.setup import complete_setup
from baluarte.db.models import AppUser, UserSession, Workspace
from baluarte.settings import development_mode
from baluarte.workspaces import roles_by_workspace

logger = logging.getLogger(__name__)

# Reachable without a session. Everything else is mounted behind current_principal.
public_router = APIRouter(prefix="/auth", tags=["auth"])
router = APIRouter(prefix="/auth", tags=["auth"])

Settings = Annotated[AuthSettings, Depends(auth_settings)]
Database = Annotated[Session, Depends(database)]
Client = Annotated[OidcClient, Depends(oidc_client)]
SignedIn = Annotated[Principal, Depends(current_principal)]

SIGN_IN_PAGE = "/sign-in"
# Where a signed-in session enters its TOTP code (the web app's page).
MFA_PAGE = "/mfa"
_PROVIDER_ERROR = re.compile(r"^[a-z_]{1,64}$")


class CurrentUser(BaseModel):
    # The internal ID that audit events and exports name as ``user:<id>``; the caller's own.
    user_id: int
    subject: str
    email: str | None
    display_name: str | None
    roles: list[Role]
    permissions: list[Permission]
    session_expires_at: datetime
    # How a second factor was proven at sign-in ("provider": the identity provider's amr;
    # "totp" or "recovery": built-in TOTP), or null when none was.
    mfa_method: Literal["provider", "totp", "recovery"] | None = None
    csrf_token: str
    # The deployment runs in development mode: review may be off whatever the profile.
    development_mode: bool = False
    # The workspace this request acted in (``X-Baluarte-Workspace``, or the caller's default);
    # ``roles`` and ``permissions`` are the caller's there.
    workspace: str | None = None
    workspaces: list["WorkspaceAccess"] = []


class WorkspaceAccess(BaseModel):
    slug: str
    name: str
    roles: list[Role]


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


def _record_sign_in(
    db: Session,
    settings: AuthSettings,
    user_id: int,
    roles: frozenset[Role] | frozenset[str],
    workspace_roles: Mapping[Any, Any],
    mfa_details: dict[str, Any],
) -> None:
    """Audit a completed sign-in; the first admin to sign in with stored settings completes
    setup."""
    audit.record(
        db,
        actor=f"user:{user_id}",
        action="auth.login",
        resource_type="session",
        details={
            "roles": sorted(roles),
            "workspaces": {
                str(workspace_id): sorted(held) for workspace_id, held in workspace_roles.items()
            },
            **mfa_details,
        },
    )
    if Role.ADMIN in roles and settings.source == "database" and complete_setup(db, user_id):
        audit.record(
            db,
            actor=f"user:{user_id}",
            action="setup.completed",
            resource_type="setup",
            details={"issuer": settings.issuer},
        )


def secret_box() -> SecretBox | None:
    """The key that seals TOTP secrets. Overridden in tests."""
    return load_secret_box()


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

    # MFA is read from the ID token itself, never from userinfo.
    amr = settings.mfa.proven(claims.get("amr"))
    claimed = claims.get("amr")
    presented: list[Any] = claimed if isinstance(claimed, list) else []
    # What the provider said, for the audit trail: short strings only, at most 20.
    amr_seen = sorted({v for v in presented if isinstance(v, str) and 0 < len(v) <= 64})[:20]
    # Required but not proven: built-in TOTP when it is on, else refused.
    needs_totp = settings.mfa.required and amr is None
    if needs_totp and not settings.mfa.totp:
        return _failed(db, settings, "mfa_required", "mfa_not_proven", subject)
    if needs_totp and secret_box() is None:
        return _failed(db, settings, "mfa_required", "totp_without_secret_key", subject)

    workspace_roles = roles_by_workspace(
        db, profile.get(settings.roles_claim), settings.group_roles
    )
    roles = frozenset().union(*workspace_roles.values())
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
    token = create_session(
        db,
        user,
        roles,
        settings,
        workspace_roles,
        "provider" if amr is not None else None,
        mfa_pending=needs_totp,
    )
    db.flush()
    if needs_totp:
        # Not signed in yet: the session waits for a code, and the sign-in is audited then.
        audit.record(
            db,
            actor=f"user:{user.id}",
            action="auth.mfa_challenged",
            resource_type="session",
            details={"amr": amr_seen, "enrolled": mfa.enrolled(db, user.id)},
        )
        destination = f"{MFA_PAGE}?{urlencode({'return_to': pending.return_to})}"
    else:
        _record_sign_in(
            db,
            settings,
            user.id,
            roles,
            workspace_roles,
            {"mfa": amr is not None, "amr": amr_seen},
        )
        destination = pending.return_to
    db.commit()

    response = RedirectResponse(destination, status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        settings.session_cookie,
        token,
        max_age=int(settings.session_lifetime.total_seconds()),
        **_cookie_options(settings),
    )
    response.delete_cookie(settings.login_cookie, **_cookie_options(settings))
    return response


@router.get("/me", response_model=CurrentUser, summary="Current user and session")
def me(principal: SignedIn, db: Annotated[Session, Depends(database)]) -> CurrentUser:
    return CurrentUser(
        user_id=principal.user_id,
        subject=principal.subject,
        email=principal.email,
        display_name=principal.display_name,
        roles=sorted(principal.roles),
        permissions=sorted(principal.permissions),
        session_expires_at=principal.session_expires_at,
        mfa_method=_method(principal.mfa_method),
        csrf_token=principal.csrf_token,
        development_mode=development_mode(),
        workspace=principal.workspace_slug,
        workspaces=[
            WorkspaceAccess(
                slug=row.slug, name=row.name, roles=sorted(principal.workspaces[row.id])
            )
            for row in db.scalars(
                select(Workspace)
                .where(Workspace.id.in_(principal.workspaces.keys()))
                .order_by(Workspace.id)
            )
        ],
    )


def _method(value: str | None) -> Literal["provider", "totp", "recovery"] | None:
    if value == "provider":
        return "provider"
    if value == "totp":
        return "totp"
    if value == "recovery":
        return "recovery"
    return None


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


# Built-in TOTP (docs/SECURITY.md#built-in-totp). These routes take only a session that has
# signed in and still has to enter its code, and are rate limited with the rest of /auth.


def pending_mfa(request: Request, settings: Settings, db: Database) -> ActiveSession:
    token = request.cookies.get(settings.session_cookie)
    active = resolve_session(db, token, settings, pending=True) if token else None
    if active is None or token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No sign-in is waiting for a code")
    if request.method != "GET":
        check_csrf(request, settings, csrf_token(token))
    return active


Pending = Annotated[ActiveSession, Depends(pending_mfa)]


def _box() -> SecretBox:
    box = secret_box()
    if box is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Built-in TOTP is not available")
    return box


class MfaStatus(BaseModel):
    # Whether the user has an authenticator; if not, the next step sets one up.
    enrolled: bool
    account: str
    csrf_token: str
    max_failures: int = totp.MAX_FAILURES
    failures: int


class MfaEnrollment(BaseModel):
    # Base32, for typing into an app that cannot scan.
    secret: str
    uri: str
    # The QR code as rows of "1" (dark) and "0" (light), without the quiet zone.
    qr: list[str]


class MfaCode(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    return_to: str | None = Field(default=None, max_length=2048)


class MfaVerified(BaseModel):
    return_to: str
    method: Literal["totp", "recovery"]
    # Shown once, when a new authenticator was confirmed; empty otherwise.
    recovery_codes: list[str]
    # After a recovery code was used: how many are left.
    recovery_codes_left: int | None


def _account(user: AppUser) -> str:
    return user.email or user.display_name or f"user {user.id}"


@public_router.get("/mfa", response_model=MfaStatus, summary="The sign-in waiting for a code")
def mfa_status(active: Pending, request: Request, settings: Settings, db: Database) -> MfaStatus:
    token = request.cookies.get(settings.session_cookie) or ""
    return MfaStatus(
        enrolled=mfa.enrolled(db, active.user.id),
        account=_account(active.user),
        csrf_token=csrf_token(token),
        failures=active.session.mfa_failures,
    )


@public_router.post(
    "/mfa/enroll",
    response_model=MfaEnrollment,
    summary="Set up an authenticator app",
    responses={status.HTTP_409_CONFLICT: {"description": "Already set up"}},
)
def mfa_enroll(active: Pending, db: Database) -> MfaEnrollment:
    started = mfa.start_enrollment(db, _box(), active.user)
    if started is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "An authenticator is already set up; ask an administrator to reset it if it is lost",
        )
    db.commit()
    return MfaEnrollment(secret=started.secret, uri=started.uri, qr=started.qr)


@public_router.post(
    "/mfa/verify",
    response_model=MfaVerified,
    summary="Enter a code to complete sign-in",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Wrong code"},
        status.HTTP_401_UNAUTHORIZED: {"description": "Too many wrong codes; sign in again"},
    },
)
def mfa_verify(
    body: MfaCode, active: Pending, settings: Settings, db: Database
) -> MfaVerified | JSONResponse:
    session, user = active.session, active.user
    try:
        verified = mfa.verify(db, _box(), user.id, body.code)
    except mfa.NotEnrolled as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "Set up an authenticator first") from exc
    except SecretUnavailable as exc:
        logger.error("auth: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "The authenticator secret cannot be read"
        ) from exc

    if verified is None:
        session.mfa_failures += 1
        ended = session.mfa_failures >= totp.MAX_FAILURES
        if ended:
            revoke_session(db, session)
        audit.record(
            db,
            actor=f"user:{user.id}",
            action="auth.mfa_failed",
            resource_type="session",
            resource_id=str(session.id),
            details={"failures": session.mfa_failures, "session_ended": ended},
        )
        db.commit()
        if ended:
            response = JSONResponse(
                {"detail": "Too many wrong codes. Sign in again."},
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
            response.delete_cookie(settings.session_cookie, **_cookie_options(settings))
            return response
        left = totp.MAX_FAILURES - session.mfa_failures
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"That code is not valid. {left} {'try' if left == 1 else 'tries'} left.",
        )

    token = complete_mfa(session, verified.method, settings)
    if verified.enrolled:
        audit.record(
            db,
            actor=f"user:{user.id}",
            action="auth.mfa_enrolled",
            resource_type="user",
            resource_id=str(user.id),
            details={"recovery_codes": len(verified.recovery_codes)},
        )
    details: dict[str, Any] = {"mfa": True, "amr": [], "mfa_method": verified.method}
    if verified.recovery_left is not None:
        details["recovery_codes_left"] = verified.recovery_left
    _record_sign_in(
        db, settings, user.id, frozenset(session.roles), session.workspace_roles, details
    )
    db.commit()

    response = JSONResponse(
        MfaVerified(
            return_to=safe_return_to(body.return_to),
            method="recovery" if verified.method == "recovery" else "totp",
            recovery_codes=verified.recovery_codes,
            recovery_codes_left=verified.recovery_left,
        ).model_dump()
    )
    response.set_cookie(
        settings.session_cookie,
        token,
        max_age=int(settings.session_lifetime.total_seconds()),
        **_cookie_options(settings),
    )
    return response
