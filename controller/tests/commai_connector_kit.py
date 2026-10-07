"""Shared test helpers for kit connectors (ADR 0028), usable by every connector's tests.

- ``FakeHTTP``: a fake transport (method + URL regex routes) whose handlers may
  return (status, body) or (status, body, headers), recording every call.
- ``go_live(app, customer_id)``: mark an app's go-live criteria met and pilot it.
- ``real_env``: secrets key, public URL and every OAuth client id/secret set.
- ``connect_real`` / ``connect_simulated``: a connection on the real app (an
  encrypted token or credentials) or on its stand-in.
- ``propose_and_run``: an action through the action service, jobs run.
"""

from __future__ import annotations

import json
import re
import urllib.parse

import pytest
from cryptography.fernet import Fernet
from psycopg.types.json import Jsonb

from exaconnect_controller import db
from exaconnect_controller.commai import actions, connectors, golive
from exaconnect_controller.commai.automation import http, oauth, vault
from exaconnect_controller.commai.connectors import kit

from .commai_helpers import run_jobs


class FakeHTTP:
    def __init__(self):
        self.routes: list[tuple[str, re.Pattern, object]] = []
        self.calls: list[dict] = []

    def on(self, method: str, pattern: str, handler) -> None:
        self.routes.insert(0, (method, re.compile(pattern), handler))

    def __call__(self, method, url, headers, data, timeout):
        body = None
        if data:
            text = data.decode()
            ctype = headers.get("Content-Type", "")
            if ctype == "application/json":
                body = json.loads(text)
            elif ctype == "application/x-www-form-urlencoded":
                body = dict(urllib.parse.parse_qsl(text))
            else:
                body = text
        call = {"method": method, "url": url, "headers": headers, "body": body}
        self.calls.append(call)
        for m, pat, h in self.routes:
            if m == method and pat.search(url):
                out = h(call) if callable(h) else h
                status, payload = out[0], out[1]
                return http.Response(status, payload, out[2] if len(out) > 2 else {})
        return http.Response(404, {"error": {"message": f"no fake route for {method} {url}"}}, {})

    def count(self, method: str, pattern: str) -> int:
        return sum(1 for c in self.calls if c["method"] == method and re.search(pattern, c["url"]))

    def last(self, method: str, pattern: str) -> dict:
        return [c for c in self.calls if c["method"] == method and re.search(pattern, c["url"])][-1]


@pytest.fixture
def fake(monkeypatch):
    f = FakeHTTP()
    monkeypatch.setattr(http, "transport", f)
    monkeypatch.setattr(kit, "sleep", lambda s: None)
    return f


@pytest.fixture
def real_env(monkeypatch):
    monkeypatch.setenv("EXA_SECRETS_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("EXA_PUBLIC_URL", "https://connect.example.org")
    for p in oauth.PROVIDERS.values():
        monkeypatch.setenv(f"{p.env}_CLIENT_ID", f"{p.app}-client-id")
        monkeypatch.setenv(f"{p.env}_CLIENT_SECRET", f"{p.app}-client-secret")


def go_live(app: str, customer_id: str | None = None) -> None:
    c = connectors.get(app)
    key = c.golive_key
    with db.tx() as conn:
        golive.sync(conn)
        for r in conn.execute(
            "SELECT criterion FROM commai_capability_criteria WHERE kind = 'feature' AND key = %s", (key,)
        ).fetchall():
            golive.check(conn, "feature", key, r["criterion"], True, "test run", "user:test")
        if customer_id:
            golive.set_status(conn, "feature", key, "pilot", "user:test", [customer_id])
        else:
            golive.set_status(conn, "feature", key, "on", "user:test")


def connect_real(
    customer_id: str,
    app: str,
    allowed: list[str],
    *,
    token: str = "",
    creds: dict | None = None,
    settings: dict | None = None,
    status: str = "live",
) -> dict:
    """A connection on the real app with an encrypted OAuth token (or credentials)."""
    with db.tx() as conn:
        row = conn.execute(
            """INSERT INTO integration_connections (customer_id, app, status, allowed_actions, settings, test_mode,
                                                    auth_status, auth_method, approved_at, approved_by)
               VALUES (%s, %s, %s, %s, %s, false, 'signed_in', %s, now(), 'user:test')
               ON CONFLICT (customer_id, app) DO UPDATE SET status = EXCLUDED.status,
                 allowed_actions = EXCLUDED.allowed_actions, settings = EXCLUDED.settings,
                 auth_method = EXCLUDED.auth_method, auth_status = 'signed_in'
               RETURNING *""",
            (customer_id, app, status, allowed, Jsonb(settings or {}), "credentials" if creds else "oauth"),
        ).fetchone()
        secret = creds if creds else {"access_token": token or "tok-" + app, "refresh_token": "ref-" + app}
        ref = vault.put(conn, customer_id, f"test:{app}", secret)
        conn.execute(
            "UPDATE integration_connections SET secret_ref = %s, token_expires_at = now() + interval '1 hour'"
            " WHERE id = %s",
            (ref, row["id"]),
        )
        if creds:
            conn.execute("UPDATE integration_connections SET token_expires_at = NULL WHERE id = %s", (row["id"],))
        return conn.execute("SELECT * FROM integration_connections WHERE id = %s", (row["id"],)).fetchone()


def connect_simulated(customer_id: str, app: str, allowed: list[str], settings: dict | None = None) -> None:
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO integration_connections (customer_id, app, status, allowed_actions, settings, test_mode,
                                                    auth_status, auth_method)
               VALUES (%s, %s, 'live', %s, %s, false, 'not_needed', 'simulated')
               ON CONFLICT (customer_id, app) DO UPDATE SET allowed_actions = EXCLUDED.allowed_actions,
                 settings = EXCLUDED.settings, auth_method = 'simulated', status = 'live'""",
            (customer_id, app, allowed, Jsonb(settings or {})),
        )


def propose_and_run(customer_id: str, app: str, action: str, inputs: dict, key: str, approve_as: str = "") -> dict:
    """Propose as a person, approve if needed (as another person), run the job, return the run."""
    with db.tx() as conn:
        run = actions.propose(
            conn,
            customer_id,
            role="person",
            app=app,
            action=action,
            inputs=inputs,
            actor="user:proposer",
            idempotency_key=key,
        )
        if run["status"] == "awaiting_approval":
            assert approve_as, "this action needs approval"
            actions.approve(conn, customer_id, run["id"], approver=approve_as)
    run_jobs()
    with db.tx() as conn:
        return conn.execute("SELECT * FROM action_runs WHERE id = %s", (run["id"],)).fetchone()


def execute_direct(customer_id: str, app: str, action: str, inputs: dict, key: str) -> dict:
    """Run a connector action straight through (no job): for retries that must reuse a key."""
    c = connectors.get(app)
    with db.tx() as conn:
        row = connectors.connection(conn, customer_id, app)
        return c.execute(conn, {**row, "test": False}, action, c.validate(action, inputs), key)
