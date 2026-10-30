"""Deny-by-default routing and permission checks. No database: the principal is injected."""

from datetime import UTC, datetime
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from oidc_fake import make_settings

from baluarte.auth.dependencies import (
    Principal,
    auth_settings,
    current_principal,
    database,
    require,
)
from baluarte.auth.roles import Permission, Role
from baluarte.main import create_app

PUBLIC_ROUTES = {
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/ready"),
    ("GET", "/api/v1/auth/login"),
    ("GET", "/api/v1/auth/callback"),
}
# First-run setup needs a setup session instead (tests/test_setup.py checks each route).
SETUP_ROUTES = {
    ("GET", "/api/v1/setup/status"),
    ("POST", "/api/v1/setup/session"),
    ("DELETE", "/api/v1/setup/session"),
    ("GET", "/api/v1/setup/oidc"),
    ("PUT", "/api/v1/setup/oidc"),
    ("POST", "/api/v1/setup/oidc/test"),
    ("GET", "/api/v1/setup/gitlab"),
    ("PUT", "/api/v1/setup/gitlab"),
    ("POST", "/api/v1/setup/gitlab/test"),
}


def principal(*roles: Role) -> Principal:
    return Principal(
        user_id=1,
        subject="user-1",
        email=None,
        display_name=None,
        roles=frozenset(roles),
        session_id=1,
        session_expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        csrf_token="csrf",
    )


def api_routes(app: FastAPI) -> set[tuple[str, str]]:
    """Every API operation, including any hidden from the OpenAPI schema."""
    return {
        (method, context.path)
        for context in iter_route_contexts(app.routes)
        if context.path.startswith("/api/")
        for method in context.methods or ()
    }


class NoDatabase:
    """Stands in for a session; any use means a check ran after the database was touched."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"database used: {name}")


@pytest.fixture
def app() -> FastAPI:
    app = create_app()
    app.dependency_overrides[auth_settings] = lambda: make_settings()
    app.dependency_overrides[database] = NoDatabase
    return app


def test_every_route_outside_the_allowlist_rejects_anonymous_requests(app: FastAPI) -> None:
    client = TestClient(app, base_url="https://baluarte.example.test")
    routes = api_routes(app)
    assert routes >= PUBLIC_ROUTES | SETUP_ROUTES
    assert {route for route in routes if route[1].startswith("/api/v1/setup")} == SETUP_ROUTES
    protected = routes - PUBLIC_ROUTES - SETUP_ROUTES
    assert protected, "expected at least one protected route"

    for method, path in protected:
        response = client.request(method, path)
        assert response.status_code == 401, (method, path)


def test_protected_routes_are_unavailable_when_auth_is_not_configured() -> None:
    client = TestClient(create_app())

    response = client.get("/api/v1/auth/me")

    assert response.status_code == 503
    assert response.json() == {"detail": "Authentication is not configured"}


def test_client_supplied_identity_headers_are_ignored(app: FastAPI) -> None:
    client = TestClient(app, base_url="https://baluarte.example.test")

    response = client.get("/api/v1/auth/me", headers={"X-Roles": "admin", "X-User": "root"})

    assert response.status_code == 401


@pytest.fixture
def guarded() -> FastAPI:
    app = FastAPI()

    @app.post("/policies")
    def write_policy(
        who: Annotated[Principal, Depends(require(Permission.POLICY_WRITE))],
    ) -> dict[str, str]:
        return {"subject": who.subject}

    @app.get("/settings")
    def manage(
        who: Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))],
    ) -> dict[str, str]:
        return {"subject": who.subject}

    return app


@pytest.mark.parametrize(
    ("roles", "path", "method", "expected"),
    [
        ((Role.ANALYST,), "/policies", "POST", 200),
        ((Role.ADMIN,), "/policies", "POST", 200),
        ((Role.AUDITOR,), "/policies", "POST", 403),
        ((Role.ADMIN,), "/settings", "GET", 200),
        ((Role.ANALYST, Role.AUDITOR), "/settings", "GET", 403),
        ((), "/settings", "GET", 403),
    ],
)
def test_permission_checks(
    guarded: FastAPI, roles: tuple[Role, ...], path: str, method: str, expected: int
) -> None:
    guarded.dependency_overrides[current_principal] = lambda: principal(*roles)

    response = TestClient(guarded).request(method, path)

    assert response.status_code == expected
    if expected == 403:
        assert response.json() == {"detail": "Insufficient permissions"}
