from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine

from baluarte.api.v1.ready import optional_engine
from baluarte.main import create_app


def client_for(engine: Engine | None) -> TestClient:
    app = create_app()
    app.dependency_overrides[optional_engine] = lambda: engine
    return TestClient(app)


@pytest.fixture
def unreachable_engine() -> Iterator[Engine]:
    # Port 1 on loopback refuses connections immediately.
    engine = create_engine(
        "postgresql+psycopg://baluarte:not-a-real-password@127.0.0.1:1/baluarte",
        connect_args={"connect_timeout": 2},
    )
    yield engine
    engine.dispose()


def test_not_configured_is_not_ready() -> None:
    response = client_for(None).get("/api/v1/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": "not_configured", "migrations": "unknown"},
    }


def test_unreachable_database_is_not_ready_and_leaks_nothing(
    unreachable_engine: Engine,
) -> None:
    response = client_for(unreachable_engine).get("/api/v1/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": "unavailable", "migrations": "unknown"},
    }
    assert "not-a-real-password" not in response.text
    assert "127.0.0.1" not in response.text


@pytest.mark.db
def test_unmigrated_database_is_not_ready(empty_engine: Engine) -> None:
    response = client_for(empty_engine).get("/api/v1/ready")

    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "ok", "migrations": "pending"}


@pytest.mark.db
def test_migrated_database_is_ready(migrated_engine: Engine) -> None:
    response = client_for(migrated_engine).get("/api/v1/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": "ok", "migrations": "ok"},
    }


def test_openapi_documents_ready_responses() -> None:
    schema = client_for(None).get("/openapi.json").json()

    responses = schema["paths"]["/api/v1/ready"]["get"]["responses"]
    assert set(responses) >= {"200", "503"}
