from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from oidc_fake import make_settings

from baluarte.auth.dependencies import Principal, auth_settings, current_principal
from baluarte.auth.roles import Role
from baluarte.main import create_app


def client_as(*roles: Role) -> TestClient:
    app = create_app()
    app.dependency_overrides[current_principal] = lambda: Principal(
        user_id=1,
        subject="user-1",
        email=None,
        display_name=None,
        roles=frozenset(roles),
        session_id=1,
        session_expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        csrf_token="csrf",
    )
    app.dependency_overrides[auth_settings] = lambda: make_settings()
    return TestClient(app)


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    password = tmp_path / "db-password"
    password.write_text("db-password-value")
    monkeypatch.setenv(
        "BALUARTE_DATABASE_URL",
        "postgresql://baluarte@db.example.test:5433/baluarte?sslmode=verify-full",
    )
    monkeypatch.setenv("BALUARTE_DATABASE_PASSWORD_FILE", str(password))
    monkeypatch.setenv("BALUARTE_LOG_LEVEL", "warning")
    monkeypatch.delenv("BALUARTE_LOG_FORMAT", raising=False)


@pytest.mark.parametrize("role", [Role.ANALYST, Role.AUDITOR])
def test_only_admins_read_settings(role: Role) -> None:
    assert client_as(role).get("/api/v1/settings").status_code == 403


def test_settings_show_configuration_without_secret_values(configured: None) -> None:
    response = client_as(Role.ADMIN).get("/api/v1/settings")

    assert response.status_code == 200
    body = response.json()
    assert body["logging"] == {"level": "WARNING", "format": "json"}
    assert body["database"] == {
        "host": "db.example.test",
        "port": 5433,
        "name": "baluarte",
        "user": "baluarte",
        "ssl_mode": "verify-full",
        "password": "configured",
    }
    auth = body["authentication"]
    assert auth["client_secret"] == "configured"
    assert auth["group_roles"]["admin"] == ["baluarte-admins"]
    assert auth["session_idle_minutes"] == 60
    assert auth["session_lifetime_minutes"] == 480
    assert "db-password-value" not in response.text
    assert "test-client-secret" not in response.text


def test_unconfigured_database_is_reported_as_null(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BALUARTE_DATABASE_URL", raising=False)

    body = client_as(Role.ADMIN).get("/api/v1/settings").json()

    assert body["database"] is None
