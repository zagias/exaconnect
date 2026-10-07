# ruff: noqa: F811  (pytest fixtures imported from test_commai_automation_helpers)
"""Workflow schedule and manual triggers, the trigger API, and the exception
path (jump to a handler step, notify on failure) (ADR 0039)."""

import datetime as dt

from exaconnect_controller import db
from exaconnect_controller.commai.automation import workflows

from .commai_helpers import allow_tools, base, business, connect_app
from .test_commai_automation_helpers import run, secrets_env  # noqa: F401


def _create(client, b, definition: dict, publish: bool = True) -> str:
    u, h = base(b), b["agent"]["h"]
    r = client.post(f"{u}/workflows", json={"definition": definition}, headers=h)
    assert r.status_code == 201, r.text
    assert not r.json()["problems"], r.json()["problems"]
    wf_id = r.json()["workflow"]["id"]
    if publish:
        p = client.post(f"{u}/workflows/{wf_id}/publish", json={"grant_tools": True}, headers=h)
        assert p.status_code == 200, p.text
    return wf_id


def _conv(client, b, text="Hello"):
    from exaconnect_controller.commai import inbox

    with db.tx() as conn:
        return inbox.receive(conn, b["id"], "web", "+18685550111", text, name="Ana")["conversation"]


def _tick():
    """Fire the due schedule timer once, then run what it started."""
    from exaconnect_controller.commai import jobs

    with db.tx() as conn:
        conn.execute("UPDATE jobs SET run_after = now() WHERE kind = 'workflow.schedule' AND status = 'queued'")
    jobs.run_pending()
    run(["workflow.step"])


def _runs(wf_id):
    with db.tx() as conn:
        return conn.execute(
            "SELECT * FROM commai_workflow_runs WHERE workflow_id = %s AND NOT test ORDER BY started_at", (wf_id,)
        ).fetchall()


def _steps(run_id):
    with db.tx() as conn:
        return conn.execute(
            "SELECT step_id, status, summary FROM commai_workflow_run_steps WHERE run_id = %s ORDER BY id", (run_id,)
        ).fetchall()


def test_schedule_validation_and_next_fire_times():
    v = workflows.validate(
        None, None, {"name": "x", "trigger": {"type": "schedule", "schedule": {"every_minutes": 2}}, "steps": []}
    )
    assert any("5 minutes" in p for p in v["problems"])
    v = workflows.validate(
        None,
        None,
        {
            "name": "Morning",
            "trigger": {"type": "schedule", "schedule": {"at": "9:05", "days": ["Monday", "fri"]}},
            "steps": [{"type": "add_note", "body": "x"}],
        },
    )
    assert v["definition"]["trigger"]["schedule"] == {"at": "09:05", "days": ["mon", "fri"]}
    assert v["definition"]["trigger"]["event"] == ""
    assert any("scheduled run has none" in w for w in v["warnings"])
    assert workflows.preview(v["definition"])[0] == "At 09:05 on Mon, Fri (business time zone)"
    # Wednesday 7 Oct 2026 10:00 in Port of Spain (UTC-4): next is Friday 09:05 local.
    after = dt.datetime(2026, 10, 7, 14, 0, tzinfo=dt.UTC)
    nxt = workflows.next_fire({"at": "09:05", "days": ["mon", "fri"]}, "America/Port_of_Spain", after)
    assert nxt == dt.datetime(2026, 10, 9, 13, 5, tzinfo=dt.UTC)
    assert workflows.next_fire({"every_minutes": 30}, "UTC", after) == after + dt.timedelta(minutes=30)


def test_scheduled_workflow_runs_on_time_and_stops_when_paused(client, secrets_env):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    wf_id = _create(
        client,
        b,
        {
            "name": "Hourly check",
            "trigger": {"type": "schedule", "schedule": {"every_minutes": 60}},
            "steps": [
                {"type": "condition", "if": [{"field": "event.type", "op": "eq", "value": "workflow.scheduled"}]}
            ],
        },
    )
    got = client.get(f"{u}/workflows/{wf_id}", headers=h).json()
    assert got["schedule"]["next_at"] and got["preview"][0] == "Every 1 hour"
    run()
    assert _runs(wf_id) == []  # not due yet
    _tick()
    runs = _runs(wf_id)
    assert len(runs) == 1 and runs[0]["status"] == "done"
    with db.tx() as conn:
        sched = conn.execute("SELECT * FROM commai_workflow_schedules WHERE workflow_id = %s", (wf_id,)).fetchone()
        assert sched["last_at"] is not None and sched["next_at"] > sched["last_at"]
        ev = conn.execute(
            "SELECT * FROM commai_events WHERE customer_id = %s AND type = 'workflow.scheduled'", (b["id"],)
        ).fetchall()
        assert len(ev) == 1 and str(runs[0]["event_id"]) == str(ev[0]["id"])
    # The same timer firing again does nothing more; the next tick runs once.
    with db.tx() as conn:
        conn.execute("UPDATE jobs SET status = 'queued' WHERE kind = 'workflow.schedule'")
    _tick()
    assert len(_runs(wf_id)) == 2
    # Paused: the schedule is removed and a queued tick does nothing.
    assert client.post(f"{u}/workflows/{wf_id}/pause", headers=h).status_code == 200
    with db.tx() as conn:
        assert (
            conn.execute("SELECT 1 FROM commai_workflow_schedules WHERE workflow_id = %s", (wf_id,)).fetchone() is None
        )
    _tick()
    assert len(_runs(wf_id)) == 2
    assert client.post(f"{u}/workflows/{wf_id}/resume", headers=h).status_code == 200
    assert client.get(f"{u}/workflows/{wf_id}", headers=h).json()["schedule"]["next_at"]


def test_manual_trigger_through_the_api(client, secrets_env):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    conv = _conv(client, b)
    wf_id = _create(
        client,
        b,
        {
            "name": "Follow up by hand",
            "trigger": {"type": "manual"},
            "steps": [{"type": "add_note", "body": "Follow up about {{event.data.input.topic}}"}],
        },
        publish=False,
    )
    # Not live yet: refused.
    assert client.post(f"{u}/workflows/{wf_id}/trigger", json={}, headers=h).status_code == 409
    client.post(f"{u}/workflows/{wf_id}/publish", json={}, headers=h)
    r = client.post(
        f"{u}/workflows/{wf_id}/trigger",
        json={"conversation_id": str(conv["id"]), "data": {"topic": "the invoice"}},
        headers=h,
    )
    assert r.status_code == 201, r.text
    run(["workflow.step"])
    runs = _runs(wf_id)
    assert len(runs) == 1 and runs[0]["status"] == "done" and str(runs[0]["conversation_id"]) == str(conv["id"])
    with db.tx() as conn:
        note = conn.execute("SELECT body FROM commai_notes WHERE conversation_id = %s", (conv["id"],)).fetchone()
    assert note["body"] == "Follow up about the invoice"
    # Another business's conversation, or a key without write scope, is refused.
    other = business(client, "Other Co")
    oc = _conv(client, other)
    assert (
        client.post(f"{u}/workflows/{wf_id}/trigger", json={"conversation_id": str(oc["id"])}, headers=h).status_code
        == 404
    )
    assert client.post(f"{u}/workflows/{wf_id}/trigger", json={}, headers=other["agent"]["h"]).status_code == 403
    assert client.post(f"{u}/workflows/{wf_id}/trigger", json={}, headers=b["internal"]["h"]).status_code == 403
    with db.tx() as conn:
        assert conn.execute(
            "SELECT 1 FROM audit_log WHERE action = 'commai.workflow.trigger' AND customer_id = %s", (b["id"],)
        ).fetchone()


def test_exception_path_jumps_to_a_handler_and_notifies(client, secrets_env):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    connect_app(b["id"], "sim_crm", ["create_lead"], settings={"simulate_failure": "provider"})
    allow_tools(b["id"], "workflow", ["sim_crm.create_lead"])
    defn = {
        "name": "Lead with fallback",
        "trigger": {"type": "manual"},
        "notify_on_failure": [b["agent2"]["email"]],
        "steps": [
            {
                "id": "lead",
                "type": "action",
                "app": "sim_crm",
                "action": "create_lead",
                "inputs": {"name": "Ana", "email": "ana@example.com"},
                "on_failure": "fallback",
            },
            {"id": "thanks", "type": "add_note", "body": "Lead created"},
            {
                "id": "fallback",
                "type": "add_note",
                "body": "Could not create the lead: {{failure.error}}",
                "only_on_failure": True,
            },
        ],
    }
    # Jumps must go forward to a real step.
    bad = workflows.validate(None, None, {**defn, "steps": [{**defn["steps"][0], "on_failure": "nowhere"}]})
    assert any("on failure" in p for p in bad["problems"])
    wf_id = _create(client, b, defn)
    prev = client.get(f"{u}/workflows/{wf_id}", headers=h).json()["preview"]
    assert "if it fails, go to fallback" in prev[1] and prev[3].startswith("3. Only after a failure")
    assert prev[-1] == f"If a run fails, tell {b['agent2']['email']}"
    conv = _conv(client, b)
    client.post(f"{u}/workflows/{wf_id}/trigger", json={"conversation_id": str(conv["id"])}, headers=h)
    # The action fails at the provider (retried, then given up).
    for _ in range(8):
        with db.tx() as conn:
            conn.execute("UPDATE jobs SET run_after = now() WHERE status = 'queued' AND kind = 'action.execute'")
        run()
    r = _runs(wf_id)[0]
    assert r["status"] == "done", r
    steps = {s["step_id"]: s for s in _steps(r["id"])}
    assert steps["lead"]["status"] == "failed"
    assert "thanks" not in steps  # skipped by the jump
    assert steps["fallback"]["status"] == "done"
    with db.tx() as conn:
        notes = [
            n["body"] for n in conn.execute("SELECT body FROM commai_notes WHERE conversation_id = %s", (conv["id"],))
        ]
    assert any(n.startswith("Could not create the lead: sim_crm.create_lead failed") for n in notes), notes


def test_failed_run_notifies_people_and_handler_steps_are_skipped_normally(client, secrets_env):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    wf_id = _create(
        client,
        b,
        {
            "name": "Assign or tell someone",
            "trigger": {"type": "manual"},
            "notify_on_failure": [b["agent2"]["email"]],
            "steps": [
                {"id": "note", "type": "add_note", "body": "Started"},
                {"id": "only", "type": "add_note", "body": "never", "only_on_failure": True},
                {
                    "id": "act",
                    "type": "action",
                    "app": "sim_crm",
                    "action": "create_lead",
                    "inputs": {"name": "A", "email": "a@x.org"},
                },
            ],
        },
        publish=False,
    )
    # Publish needs the app; force it live to see the failure path (the app was never connected).
    with db.tx() as conn:
        conn.execute("UPDATE commai_workflows SET status = 'live', live_version = 1 WHERE id = %s", (wf_id,))
    conv = _conv(client, b)
    client.post(f"{u}/workflows/{wf_id}/trigger", json={"conversation_id": str(conv["id"])}, headers=h)
    run(["workflow.step"])
    r = _runs(wf_id)[0]
    assert r["status"] == "failed"
    steps = {s["step_id"]: s for s in _steps(r["id"])}
    assert steps["only"]["status"] == "skipped"
    with db.tx() as conn:
        ev = conn.execute(
            "SELECT data FROM commai_events WHERE customer_id = %s AND type = 'workflow.exception'", (b["id"],)
        ).fetchone()
        assert ev["data"]["notify"] == [b["agent2"]["email"]] and "refused" in ev["data"]["error"]
        note = conn.execute(
            "SELECT body, mentions FROM commai_notes WHERE conversation_id = %s AND body LIKE %s",
            (conv["id"], "%stopped%"),
        ).fetchone()
        assert note["mentions"] == [b["agent2"]["email"]]
