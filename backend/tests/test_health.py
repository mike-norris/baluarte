from fastapi.testclient import TestClient

from baluarte import __version__
from baluarte.main import create_app


def client() -> TestClient:
    return TestClient(create_app())


def test_health_reports_ok_and_version() -> None:
    response = client().get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_health_rejects_unsupported_method() -> None:
    response = client().post("/api/v1/health")

    assert response.status_code == 405


def test_unversioned_path_is_not_served() -> None:
    response = client().get("/health")

    assert response.status_code == 404


def test_openapi_contract_documents_health() -> None:
    schema = client().get("/openapi.json").json()

    operation = schema["paths"]["/api/v1/health"]["get"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/HealthResponse"
    }
    assert schema["components"]["schemas"]["HealthResponse"]["required"] == ["status", "version"]
