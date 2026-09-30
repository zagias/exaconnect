from fastapi.testclient import TestClient

from exaconnect_controller.main import create_app


def client() -> TestClient:
    return TestClient(create_app())


def test_healthz():
    r = client().get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_version():
    r = client().get("/api/v1/version")
    assert r.status_code == 200
    assert r.json()["service"] == "exaconnect-controller"


def test_openapi_is_published():
    r = client().get("/api/v1/openapi.json")
    assert r.status_code == 200
    assert r.json()["info"]["title"] == "ExaConnect controller"
