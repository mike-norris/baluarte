"""First-run setup: enter the identity provider and GitLab settings before anyone can sign in.

Mounted on the public router because no one can sign in yet. Instead of a session, these
endpoints need a setup session, started by entering the one-time setup code from the API log
(or from ``baluarte-setup-code``). Once setup is complete every endpoint except ``/status``
answers 409, and settings change only through the admin ``/settings`` endpoints. See ADR-0008.
"""

import hmac
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.api.v1.gitlab import gitlab_transport
from baluarte.auth.dependencies import CSRF_HEADER, SAFE_METHODS, database
from baluarte.configuration import service
from baluarte.configuration.schemas import (
    GitLabConfig,
    GitLabTest,
    GitLabTestResult,
    GitLabUpdate,
    OidcConfig,
    OidcTest,
    OidcTestResult,
    OidcUpdate,
    SetupCode,
    SetupSessionOut,
    SetupStatus,
)
from baluarte.configuration.setup import (
    SESSION_TTL,
    check_code,
    create_setup_session,
    end_setup_session,
    setup_csrf_token,
    setup_session_valid,
    setup_state,
)
from baluarte.settings import ConfigurationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/setup", tags=["setup"])

COOKIE = "baluarte_setup"
# Used over https: the prefix makes browsers require Secure, host-only and Path=/.
SECURE_COOKIE = "__Host-baluarte_setup"
# Audit actor for changes made in the setup wizard, before anyone has signed in.
ACTOR = "setup"

Database = Annotated[Session, Depends(database)]


def oidc_transport() -> httpx.BaseTransport | None:
    """The HTTP transport for identity provider discovery tests; None uses the network."""
    return None


def _secure(request: Request) -> bool:
    forwarded = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or forwarded == "https"


def _session_token(request: Request) -> str | None:
    return request.cookies.get(SECURE_COOKIE) or request.cookies.get(COOKIE)


def _cookie_options(request: Request) -> dict[str, Any]:
    return {"path": "/", "secure": _secure(request), "httponly": True, "samesite": "strict"}


def _cookie_name(request: Request) -> str:
    return SECURE_COOKIE if _secure(request) else COOKIE


def setup_open(db: Database) -> None:
    if setup_state(db) == "complete":
        raise HTTPException(status.HTTP_409_CONFLICT, "Setup is complete")


def setup_session(request: Request, db: Database, _: Annotated[None, Depends(setup_open)]) -> None:
    """The caller entered the setup code; state changes also need the CSRF header."""
    token = _session_token(request)
    if not token or not setup_session_valid(db, token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No valid setup session")
    if request.method in SAFE_METHODS:
        return
    origin = request.headers.get("Origin")
    host = request.headers.get("Host", "").rsplit(":", 1)[0].strip("[]").lower()
    if origin is not None and (urlsplit(origin).hostname or "") != host:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-origin request rejected")
    if not hmac.compare_digest(request.headers.get(CSRF_HEADER, ""), setup_csrf_token(token)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing or invalid CSRF token")


SetupSession = Depends(setup_session)


@contextmanager
def http_errors() -> Iterator[None]:
    try:
        yield
    except service.NotStored as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not configured") from exc
    except service.SettingsInvalid as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except service.SettingsConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ConfigurationError as exc:
        logger.error("setup: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


@router.get(
    "/status",
    response_model=SetupStatus,
    summary="Whether Baluarte still needs setting up",
    description="Public. Once setup is complete only `state` is reported.",
)
def read_status(request: Request, db: Database) -> SetupStatus:
    token = _session_token(request)
    with http_errors():
        return service.setup_status(db, bool(token) and setup_session_valid(db, token or ""))


@router.post(
    "/session",
    response_model=SetupSessionOut,
    summary="Start a setup session with the one-time setup code",
    description="Sets the setup cookie. Send the returned `csrf_token` as the "
    f"`{CSRF_HEADER}` header on every setup change. After 5 wrong codes within 15 minutes "
    "every attempt answers 429 until the window ends.",
    responses={
        401: {"description": "Wrong setup code"},
        409: {"description": "Setup is complete"},
        429: {"description": "Too many wrong codes"},
    },
    dependencies=[Depends(setup_open)],
)
def start_session(
    body: SetupCode, request: Request, response: Response, db: Database
) -> SetupSessionOut:
    result = check_code(db, body.code)
    if result != "ok":
        db.commit()
        # Logged, not audited, so anonymous requests cannot add rows to the audit trail.
        logger.warning("setup: setup code rejected (%s)", result)
        if result == "locked":
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many wrong setup codes")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid setup code")
    token = create_setup_session(db)
    audit.record(db, actor=ACTOR, action="setup.session_started", resource_type="setup")
    db.commit()
    response.set_cookie(
        _cookie_name(request),
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        **_cookie_options(request),
    )
    return SetupSessionOut(csrf_token=setup_csrf_token(token))


@router.delete("/session", status_code=status.HTTP_204_NO_CONTENT, summary="End the setup session")
def end_session(request: Request, db: Database) -> Response:
    token = _session_token(request)
    if token:
        end_setup_session(db, token)
        db.commit()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(_cookie_name(request), **_cookie_options(request))
    return response


@router.get(
    "/oidc",
    response_model=OidcConfig,
    summary="Identity provider settings entered so far",
    dependencies=[SetupSession],
)
def read_oidc(db: Database) -> OidcConfig:
    with http_errors():
        return service.oidc_config(db)


@router.put(
    "/oidc",
    response_model=OidcConfig,
    summary="Save the identity provider settings",
    dependencies=[SetupSession],
)
def write_oidc(body: OidcUpdate, db: Database) -> OidcConfig:
    with http_errors():
        return service.update_oidc(db, body, ACTOR, "setup")


@router.post(
    "/oidc/test",
    response_model=OidcTestResult,
    summary="Check the issuer's OIDC discovery document",
    dependencies=[SetupSession],
)
def test_oidc(
    body: OidcTest,
    transport: Annotated[httpx.BaseTransport | None, Depends(oidc_transport)],
) -> OidcTestResult:
    with http_errors():
        return service.check_oidc_discovery(body.issuer, transport)


@router.get(
    "/gitlab",
    response_model=GitLabConfig,
    summary="GitLab settings entered so far",
    dependencies=[SetupSession],
)
def read_gitlab(db: Database) -> GitLabConfig:
    with http_errors():
        return service.gitlab_config(db)


@router.put(
    "/gitlab",
    response_model=GitLabConfig,
    summary="Save the GitLab connection",
    dependencies=[SetupSession],
)
def write_gitlab(body: GitLabUpdate, db: Database) -> GitLabConfig:
    with http_errors():
        return service.update_gitlab(db, body, ACTOR, "setup")


@router.post(
    "/gitlab/test",
    response_model=GitLabTestResult,
    summary="Test a GitLab URL and token without saving them",
    dependencies=[SetupSession],
)
def test_gitlab(
    body: GitLabTest,
    db: Database,
    transport: Annotated[httpx.BaseTransport | None, Depends(gitlab_transport)],
) -> GitLabTestResult:
    with http_errors():
        return service.check_gitlab_connection(db, body, transport)
