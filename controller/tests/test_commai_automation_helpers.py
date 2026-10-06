"""Helpers (no tests) for the CommAI automation tests (ADR 0020): a fake HTTP layer for
the Google Calendar and HubSpot connectors, secure-storage keys, and a job
runner that fires only what a test asks for (so timers don't fire early)."""

from __future__ import annotations

import json
import re
import urllib.parse

import pytest
from cryptography.fernet import Fernet

from exaconnect_controller import db
from exaconnect_controller.commai import jobs
from exaconnect_controller.commai.automation import http, workflows


class FakeHTTP:
    """Routes (method, url regex) to handlers; records every call."""

    def __init__(self):
        self.routes: list[tuple[str, re.Pattern, object]] = []
        self.calls: list[dict] = []

    def on(self, method: str, pattern: str, handler) -> None:
        self.routes.insert(0, (method, re.compile(pattern), handler))

    def __call__(self, method, url, headers, data, timeout):
        body = None
        if data:
            text = data.decode()
            if headers.get("Content-Type") == "application/json":
                body = json.loads(text)
            else:
                body = dict(urllib.parse.parse_qsl(text))
        call = {"method": method, "url": url, "headers": headers, "body": body}
        self.calls.append(call)
        for m, pat, h in self.routes:
            if m == method and pat.search(url):
                out = h(call) if callable(h) else h
                status, payload = out
                return http.Response(status, payload, {})
        return http.Response(404, {"error": {"message": "not found"}}, {})

    def count(self, method: str, pattern: str) -> int:
        return sum(1 for c in self.calls if c["method"] == method and re.search(pattern, c["url"]))


@pytest.fixture
def fake_http(monkeypatch):
    f = FakeHTTP()
    monkeypatch.setattr(http, "transport", f)
    return f


@pytest.fixture
def secrets_env(monkeypatch):
    monkeypatch.setenv("EXA_SECRETS_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("EXA_PUBLIC_URL", "https://connect.example.org")
    monkeypatch.setenv("EXA_GOOGLE_CLIENT_ID", "google-client-id")
    monkeypatch.setenv("EXA_GOOGLE_CLIENT_SECRET", "google-client-secret")
    monkeypatch.setenv("EXA_HUBSPOT_CLIENT_ID", "hubspot-client-id")
    monkeypatch.setenv("EXA_HUBSPOT_CLIENT_SECRET", "hubspot-client-secret")
    monkeypatch.setattr(workflows, "GAP_WAIT_S", 0)


def run(kinds: list[str] | None = None, rounds: int = 6) -> int:
    """Run ready jobs, making the dispatcher (and any `kinds`) ready now.
    Timers (delayed workflow steps) are not touched."""
    n = 0
    for _ in range(rounds):
        with db.tx() as conn:
            conn.execute(
                "UPDATE jobs SET run_after = now() WHERE status = 'queued' AND (kind = 'workflow.dispatch'"
                " OR kind = ANY(%s))",
                (kinds or [],),
            )
        ran = jobs.run_pending()
        n += ran
        if ran <= 1:  # only the dispatcher went round again
            more = 0
            with db.tx() as conn:
                more = conn.execute(
                    "SELECT count(*) AS n FROM jobs WHERE status = 'queued' AND run_after <= now()"
                ).fetchone()["n"]
            if not more:
                break
    return n


def fire_timers(run_id) -> None:
    """Make a run's delayed steps (waits, reminders, time-outs) due now."""
    with db.tx() as conn:
        conn.execute(
            "UPDATE jobs SET run_after = now() WHERE status = 'queued' AND kind = 'workflow.step'"
            " AND payload->>'run_id' = %s",
            (str(run_id),),
        )
    run()
