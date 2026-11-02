"""Queued/retried actions must honour permissions at execution time."""

import datetime as dt
from contextlib import nullcontext

import pytest

from exaconnect_controller import db
from exaconnect_controller.commai import actions, connectors

from .commai_helpers import allow_tools, business, connect_app, run_jobs


class CountingConnector:
    label = "Test calendar"
    actions = {"book": connectors.ActionSpec("book", "Book", "create")}

    def __init__(self):
        self.calls = 0

    def execute(self, *args):
        self.calls += 1
        return {"booking_id": "created"}


class ActionConnection:
    def __init__(self, run):
        self.run = run

    def execute(self, sql, args):
        self.row = self.run if sql.startswith("SELECT * FROM action_runs") else None
        return self

    def fetchone(self):
        return self.row

    def transaction(self):
        return nullcontext()


def execute_fixture(monkeypatch, *, status="approved", test=False, role="customer_agent"):
    run = {
        "id": "action",
        "customer_id": "customer",
        "conversation_id": None,
        "status": status,
        "app": "calendar",
        "action": "book",
        "role": role,
        "test": test,
        "inputs": {},
        "idempotency_key": "stable-key",
        "on_success": {},
    }
    connection = {"status": "live", "allowed_actions": ["book"]}
    connector = CountingConnector()
    failures = []
    allowed = {"role": True}
    monkeypatch.setattr(actions.connectors, "get", lambda _: connector)
    monkeypatch.setattr(actions.connectors, "connection", lambda *_: connection)
    monkeypatch.setattr(actions, "role_may", lambda _c, _cid, r, *_: r == "person" or allowed["role"])
    monkeypatch.setattr(actions, "_fail", lambda _c, _run, error, cause: failures.append((error, cause)))
    monkeypatch.setattr(actions.events, "emit", lambda *_: None)
    job = {"payload": {"run_id": run["id"]}, "attempts": 1, "max_attempts": 4}
    return ActionConnection(run), job, connection, connector, allowed, failures


@pytest.mark.parametrize("status", ["approved", "executing"])
@pytest.mark.parametrize("revoked", ["role", "action", "connection"])
def test_revoked_permissions_stop_initial_and_retried_connector_io(monkeypatch, status, revoked):
    conn, job, connection, connector, allowed, failures = execute_fixture(monkeypatch, status=status)
    if revoked == "role":
        allowed["role"] = False
    elif revoked == "action":
        connection["allowed_actions"] = []
    else:
        connection["status"] = "paused"

    assert actions._execute(conn, job) is None

    assert connector.calls == 0
    assert len(failures) == 1 and failures[0][1] == "policy"
    assert connection["status"] == ("paused" if revoked == "connection" else "live")


@pytest.mark.parametrize("test_mode,status", [(False, "live"), (True, "authorised"), (True, "testing")])
def test_authorised_execution_and_test_mode_still_work(monkeypatch, test_mode, status):
    conn, job, connection, connector, _, failures = execute_fixture(monkeypatch, test=test_mode)
    connection["status"] = status
    actions._execute(conn, job)
    assert connector.calls == 1 and failures == []


def test_a_test_only_connection_cannot_execute_a_live_action(monkeypatch):
    conn, job, connection, connector, _, failures = execute_fixture(monkeypatch)
    connection["status"] = "testing"
    actions._execute(conn, job)
    assert connector.calls == 0 and failures[0][1] == "policy"


def test_removed_connector_definition_fails_closed(monkeypatch):
    conn, job, _, connector, _, failures = execute_fixture(monkeypatch)

    def removed(_):
        raise KeyError("calendar")

    monkeypatch.setattr(actions.connectors, "get", removed)
    actions._execute(conn, job)
    assert connector.calls == 0 and failures[0][1] == "policy"


def test_person_action_does_not_require_an_ai_tool_grant(monkeypatch):
    conn, job, _, connector, allowed, failures = execute_fixture(monkeypatch, role="person")
    allowed["role"] = False
    actions._execute(conn, job)
    assert connector.calls == 1 and failures == []


@pytest.mark.parametrize("revoked,retry", [("role", False), ("action", False), ("connection", False), ("role", True)])
def test_committed_revocation_prevents_a_queued_booking(client, revoked, retry):
    b = business(client, people=())
    connect_app(b["id"], "sim_calendar", ["book"], settings={"simulate_failure": "provider"} if retry else {})
    allow_tools(b["id"], "customer_agent", ["sim_calendar.book"])
    start = (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    with db.tx() as conn:
        run = actions.propose(
            conn,
            b["id"],
            role="customer_agent",
            app="sim_calendar",
            action="book",
            inputs={"start": start.isoformat(), "name": "Ana", "contact": "ana@example.org"},
            actor="ai:customer_agent",
        )
    if retry:
        run_jobs()
        with db.tx() as conn:
            assert (
                conn.execute("SELECT status FROM action_runs WHERE id = %s", (run["id"],)).fetchone()["status"]
                == "approved"
            )
    with db.tx() as conn:
        if revoked == "role":
            conn.execute("DELETE FROM commai_role_tools WHERE customer_id = %s AND role = 'customer_agent'", (b["id"],))
        elif revoked == "action":
            conn.execute("UPDATE integration_connections SET allowed_actions = '{}' WHERE customer_id = %s", (b["id"],))
        else:
            conn.execute("UPDATE integration_connections SET status = 'paused' WHERE customer_id = %s", (b["id"],))

    run_jobs()

    with db.tx() as conn:
        done = conn.execute("SELECT status, error FROM action_runs WHERE id = %s", (run["id"],)).fetchone()
        assert done["status"] == "failed" and "(policy)" in done["error"]
        assert conn.execute("SELECT 1 FROM sim_records WHERE customer_id = %s", (b["id"],)).fetchone() is None
        current = conn.execute(
            "SELECT status FROM integration_connections WHERE customer_id = %s", (b["id"],)
        ).fetchone()
        assert current["status"] == ("paused" if revoked == "connection" else "live")
