import os
import time

import psycopg
import pytest
from fastapi.testclient import TestClient

from exaconnect_controller import db
from exaconnect_controller.main import create_app
from exaconnect_controller.settings import Settings

DB_URL = os.environ.get("EXA_TEST_DATABASE_URL", "")
PROXY_SECRET = "test-proxy-secret"
ADMIN = ("admin@example.org", "correct horse battery staple")


def _reset(url: str) -> None:
    # TimescaleDB's background workers (job scheduler, telemetry) can hold
    # catalog locks while the schema is dropped, which Postgres resolves by
    # cancelling the DROP as a deadlock. Pause them, and retry if one wins.
    with psycopg.connect(url, autocommit=True) as conn:
        for attempt in range(5):
            try:
                if conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'").fetchone():
                    try:
                        conn.execute("SELECT _timescaledb_functions.stop_background_workers()")
                    except psycopg.Error:
                        pass  # older TimescaleDB or not a superuser: the retry still covers it
                conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
                break
            except psycopg.errors.DeadlockDetected:
                if attempt == 4:
                    raise
                time.sleep(0.5)
        conn.execute("CREATE SCHEMA public")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=DB_URL,
        data_dir=str(tmp_path / "data"),
        proxy_secret=PROXY_SECRET,
        admin_email=ADMIN[0],
        admin_password=ADMIN[1],
        routing_interval_s=0,
    )


@pytest.fixture
def client(settings):
    if not DB_URL:
        pytest.skip("set EXA_TEST_DATABASE_URL to run database tests")
    db.close()
    _reset(DB_URL)
    with TestClient(create_app(settings)) as c:
        yield c
    db.close()


@pytest.fixture
def admin_headers(client):
    r = client.post("/api/v1/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


# Fake servers and switches for the integration tests (tests/test_integrations_*.py).
from .integrations_helpers import fake, live, vault  # noqa: E402, F401
