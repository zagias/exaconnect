from fastapi.testclient import TestClient

from exaconnect_controller.main import create_app
from exaconnect_controller.settings import Settings


def client(tmp_path) -> TestClient:
    return TestClient(create_app(Settings(database_url="", data_dir=str(tmp_path))))


def test_healthz(tmp_path):
    r = client(tmp_path).get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_version(tmp_path):
    r = client(tmp_path).get("/api/v1/version")
    assert r.status_code == 200
    assert r.json()["service"] == "exaconnect-controller"


def test_openapi_is_published(tmp_path):
    r = client(tmp_path).get("/api/v1/openapi.json")
    assert r.status_code == 200
    assert r.json()["info"]["title"] == "ExaCarib Connect API"


def test_ca_is_created_once_and_served(tmp_path):
    c = client(tmp_path)
    first = c.get("/api/v1/ca.pem").text
    assert first.startswith("-----BEGIN CERTIFICATE-----")
    assert client(tmp_path).get("/api/v1/ca.pem").text == first
    assert (tmp_path / "tls" / "server.crt").exists()
