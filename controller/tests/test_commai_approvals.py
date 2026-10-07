"""The approvals queue and the impact preview worked out at propose time (ADR 0033)."""

import datetime as dt

from psycopg.types.json import Jsonb

from exaconnect_controller import db

from .commai_helpers import base, business, connect_app, run_jobs

TOMORROW = (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).date()


def _book(client, b, hour: int) -> str:
    start = dt.datetime.combine(TOMORROW, dt.time(hour), tzinfo=dt.UTC).isoformat()
    r = client.post(
        f"{base(b)}/actions",
        json={"app": "sim_calendar", "action": "book", "inputs": {"start": start, "name": "Dee", "contact": "d@x.org"}},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 201, r.text
    run_jobs()
    with db.tx() as conn:
        res = conn.execute("SELECT result FROM action_runs WHERE id = %s", (r.json()["id"],)).fetchone()["result"]
    return res["booking_id"]


def test_sensitive_action_waits_with_an_impact_preview(client):
    b = business(client)
    u, h, h2 = base(b), b["agent"]["h"], b["agent2"]["h"]
    connect_app(b["id"], "sim_calendar", ["book", "cancel"])
    booking = _book(client, b, 10)
    r = client.post(
        f"{u}/actions", json={"app": "sim_calendar", "action": "cancel", "inputs": {"booking_id": booking}}, headers=h
    )
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["status"] == "awaiting_approval"
    p = run["preview"]
    assert p["dry_run"] is True and p["summary"].startswith("Cancels in Example calendar")
    assert p["changes"] == [{"label": "Booking", "value": booking}]
    assert p["checks"][0]["ok"] is True and "Dee" in p["checks"][0]["detail"]
    assert any("not told automatically" in n for n in p["notes"])
    assert p["reversible"] is False
    # Nothing happened yet: the booking is still there.
    with db.tx() as conn:
        d = conn.execute("SELECT data FROM sim_records WHERE id = %s", (booking,)).fetchone()["data"]
        assert "cancelled_at" not in d
    q = client.get(f"{u}/approvals", headers=h2).json()
    assert q["count"] == 1 and q["actions"][0]["id"] == run["id"]
    assert q["actions"][0]["preview"]["summary"] == p["summary"]
    assert q["actions"][0]["own_proposal"] is False
    assert client.get(f"{u}/approvals", headers=h).json()["actions"][0]["own_proposal"] is True
    # The proposer can't approve; someone else can.
    assert client.post(f"{u}/actions/{run['id']}/approve", headers=h).status_code == 403
    assert client.post(f"{u}/actions/{run['id']}/approve", headers=h2).status_code == 200
    run_jobs()
    assert client.get(f"{u}/actions/{run['id']}", headers=h).json()["status"] == "succeeded"
    assert client.get(f"{u}/approvals", headers=h).json()["count"] == 0


def test_preview_flags_a_likely_failure_and_can_be_refreshed(client):
    b = business(client)
    u, h, h2 = base(b), b["agent"]["h"], b["agent2"]["h"]
    connect_app(b["id"], "sim_calendar", ["book", "cancel"])
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO commai_settings (customer_id, config) VALUES (%s, %s) ON CONFLICT (customer_id) DO UPDATE"
            " SET config = EXCLUDED.config",
            (b["id"], Jsonb({"approval_required": ["sim_calendar.book"]})),
        )
    taken = _book_direct(b, 11)
    start = dt.datetime.combine(TOMORROW, dt.time(11), tzinfo=dt.UTC).isoformat()
    r = client.post(
        f"{u}/actions",
        json={"app": "sim_calendar", "action": "book", "inputs": {"start": start, "name": "Al", "contact": "a@x.org"}},
        headers=h,
    )
    run = r.json()
    assert run["status"] == "awaiting_approval"
    free = {c["label"]: c for c in run["preview"]["checks"]}
    assert free["Time is free"]["ok"] is False
    assert any("likely to fail" in n for n in run["preview"]["notes"])
    # The slot frees up; refreshing the preview shows it.
    with db.tx() as conn:
        conn.execute(
            "UPDATE sim_records SET data = data || jsonb_build_object('cancelled_at', now()) WHERE id = %s", (taken,)
        )
    again = client.post(f"{u}/actions/{run['id']}/preview", headers=h2)
    assert again.status_code == 200
    assert {c["label"]: c for c in again.json()["checks"]}["Time is free"]["ok"] is True
    # Another business can't see or refresh it.
    other = business(client, "Other Co")
    assert client.post(f"{u}/actions/{run['id']}/preview", headers=other["agent"]["h"]).status_code == 403
    assert client.get(f"{base(other)}/approvals", headers=other["agent"]["h"]).json()["count"] == 0


def test_preview_never_blocks_when_the_app_is_failing(client):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    connect_app(b["id"], "sim_calendar", ["cancel"], settings={"simulate_failure": "provider"})
    r = client.post(
        f"{u}/actions", json={"app": "sim_calendar", "action": "cancel", "inputs": {"booking_id": "x"}}, headers=h
    )
    assert r.status_code == 201
    checks = r.json()["preview"]["checks"]
    assert checks[0]["ok"] is False and "fail" in checks[0]["detail"]


def _book_direct(b, hour: int) -> str:
    start = dt.datetime.combine(TOMORROW, dt.time(hour), tzinfo=dt.UTC).isoformat()
    with db.tx() as conn:
        return str(
            conn.execute(
                """INSERT INTO sim_records (customer_id, app, kind, idempotency_key, data)
                   VALUES (%s, 'sim_calendar', 'booking', %s, %s) RETURNING id""",
                (b["id"], f"direct:{hour}", Jsonb({"start": start, "name": "X", "contact": "x"})),
            ).fetchone()["id"]
        )
