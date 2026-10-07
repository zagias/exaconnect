"""What a proposed action will do, worked out before anyone approves it (ADR 0033).

When an action has to wait for a person, the service works out its impact at
propose time and stores it on the run, so the approver sees the change and
what it touches:

  {"summary": "...", "changes": [{"label", "value"}], "checks": [{"label", "ok", "detail"}],
   "reversible": bool, "dry_run": bool, "at": "..."}

Checks only read: a calendar's free/busy answer, whether the booking to
cancel exists, whether a contact with that email is already in the CRM.
Nothing is written to any outside system. A check that can't run says so
(ok = None) rather than guessing.

A connector may offer its own `preview(conn, connection, action, inputs)`
returning {"checks": [...], "notes": [...]}; it is used when present.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import psycopg

from . import connectors

KIND_WORDS = {
    "read": "Looks up",
    "create": "Creates",
    "update": "Changes",
    "cancel": "Cancels",
    "refund": "Refunds",
    "delete": "Deletes",
}
CONSEQUENCE = {
    "cancel": "The customer is not told automatically unless the workflow or reply says so.",
    "refund": "Money goes back to the customer. A refund can't be taken back from here.",
    "delete": "The record is removed from the other system and can't be restored from here.",
    "update": "The record in the other system is changed.",
    "create": "A new record is added to the other system.",
}


def _when(v: str) -> str:
    try:
        t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return str(v)
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.UTC)
    return t.astimezone(dt.UTC).strftime("%a %-d %b %Y %H:%M UTC")


def _sim_calendar(conn, connection: dict, action: str, inputs: dict) -> list[dict]:
    from .connectors.simulated import SLOT_MINUTES

    s = connection.get("settings") or {}
    if s.get("simulate_failure"):
        return [{"label": "Calendar answering", "ok": False, "detail": f"Set to fail ({s['simulate_failure']})."}]
    cid = connection["customer_id"]
    if action == "book":
        start = dt.datetime.fromisoformat(inputs["start"])
        open_h, close_h = int(s.get("open_hour", 9)), int(s.get("close_hour", 17))
        taken = conn.execute(
            """SELECT 1 FROM sim_records WHERE customer_id = %s AND app = 'sim_calendar' AND kind = 'booking'
               AND data->>'start' = %s AND NOT (data ? 'cancelled_at')""",
            (cid, start.isoformat()),
        ).fetchone()
        return [
            {
                "label": "Within opening hours",
                "ok": open_h <= start.astimezone(dt.UTC).hour < close_h,
                "detail": f"Open {open_h}:00 to {close_h}:00 UTC; {SLOT_MINUTES}-minute slot.",
            },
            {
                "label": "Time is free",
                "ok": not taken,
                "detail": "Already booked." if taken else "No booking at that time.",
            },
        ]
    if action == "cancel":
        row = conn.execute(
            "SELECT data FROM sim_records WHERE id::text = %s AND customer_id = %s AND app = 'sim_calendar'",
            (str(inputs.get("booking_id", "")), cid),
        ).fetchone()
        if row is None:
            return [{"label": "Booking exists", "ok": False, "detail": "No booking with that id."}]
        d = row["data"]
        if d.get("cancelled_at"):
            return [{"label": "Booking exists", "ok": False, "detail": "That booking is already cancelled."}]
        return [
            {
                "label": "Booking exists",
                "ok": True,
                "detail": f"{d.get('name', '')} ({d.get('contact', '')}) on {_when(d.get('start', ''))}.",
            }
        ]
    return []


def _sim_crm(conn, connection: dict, action: str, inputs: dict) -> list[dict]:
    if action != "create_lead" or not inputs.get("email"):
        return []
    row = conn.execute(
        """SELECT 1 FROM sim_records WHERE customer_id = %s AND app = 'sim_crm' AND kind = 'lead'
           AND lower(data->>'email') = lower(%s)""",
        (connection["customer_id"], inputs["email"]),
    ).fetchone()
    return [
        {
            "label": "New to the CRM",
            "ok": not row,
            "detail": "A lead with this email already exists; another would be added." if row else "No lead yet.",
        }
    ]


def _google_calendar(conn, connection: dict, action: str, inputs: dict) -> list[dict]:
    c = connectors.get("google_calendar")
    if action != "book":
        return []
    cfg = c._settings(conn, connection)
    start = dt.datetime.fromisoformat(inputs["start"]).astimezone(cfg["tz"])
    finish = start + dt.timedelta(minutes=cfg["slot"])
    busy = c._busy(conn, connection, cfg, start, finish)
    return [
        {
            "label": "Within opening hours",
            "ok": cfg["open"] <= start.hour and (finish.hour, finish.minute) <= (cfg["close"], 0),
            "detail": f"Open {cfg['open']}:00 to {cfg['close']}:00 ({cfg['tz'].key}).",
        },
        {
            "label": "Time is free (Google free/busy)",
            "ok": not busy,
            "detail": "Google shows the time as busy." if busy else "Google shows the time as free.",
        },
    ]


def _hubspot(conn, connection: dict, action: str, inputs: dict) -> list[dict]:
    if action not in ("create_contact", "create_lead") or not inputs.get("email"):
        return []
    hit = connectors.get("hubspot")._search(conn, connection, "contacts", "email", "EQ", inputs["email"])
    return [
        {
            "label": "Contact already in HubSpot",
            "ok": True,
            "detail": "Found: the existing contact is used, not a new one."
            if hit
            else "Not found: a contact is added.",
        }
    ]


BUILT_IN = {
    "sim_calendar": _sim_calendar,
    "sim_crm": _sim_crm,
    "google_calendar": _google_calendar,
    "hubspot": _hubspot,
}


def preview(
    conn: psycopg.Connection,
    customer_id: Any,
    connector: connectors.Connector,
    connection: dict,
    spec: connectors.ActionSpec,
    inputs: dict,
) -> dict:
    """The approver's view of a proposed action. Reads only."""
    changes = [
        {"label": f.label, "value": str(inputs[f.name])[:300]}
        for f in spec.fields
        if f.name in inputs and inputs[f.name] not in (None, "")
    ]
    lead = KIND_WORDS.get(spec.kind, "Runs")
    what = spec.label[0].lower() + spec.label[1:]
    summary = f"{lead} in {connector.label}: {what}."
    if spec.kind == "refund" and inputs.get("amount"):
        summary = f"Refunds {inputs.get('currency', '')} {inputs['amount']} through {connector.label}.".replace(
            "  ", " "
        )
    checks: list[dict] = []
    notes: list[str] = []
    fn = BUILT_IN.get(connector.app)
    own = getattr(connector, "preview", None)
    try:
        with conn.transaction():  # a savepoint: a failed read leaves the proposal intact
            if callable(own):
                got = own(conn, connection, spec.name, inputs) or {}
                checks.extend(got.get("checks") or [])
                notes.extend(got.get("notes") or [])
            elif fn is not None:
                checks.extend(fn(conn, connection, spec.name, inputs))
    except connectors.ConnectorError as e:
        checks.append({"label": f"Checked with {connector.label}", "ok": None, "detail": f"Could not check: {e}"})
    except Exception as e:  # noqa: BLE001 - a preview never blocks a proposal
        checks.append(
            {"label": f"Checked with {connector.label}", "ok": None, "detail": f"Could not check ({type(e).__name__})."}
        )
    if spec.kind in CONSEQUENCE:
        notes.append(CONSEQUENCE[spec.kind])
    problems = [c for c in checks if c.get("ok") is False]
    if problems:
        notes.append("If approved now, this is likely to fail: " + "; ".join(c["detail"] for c in problems))
    return {
        "summary": summary,
        "changes": changes,
        "checks": checks,
        "notes": notes,
        "reversible": spec.kind in ("read", "create", "update"),
        "dry_run": True,
        "at": dt.datetime.now(dt.UTC).isoformat(),
    }


def refresh(conn: psycopg.Connection, customer_id: Any, run: dict) -> dict:
    """Work the preview out again (the calendar may have changed since)."""
    try:
        connector = connectors.get(run["app"])
    except KeyError:
        return {"summary": f"{run['app']}.{run['action']}", "changes": [], "checks": [], "notes": [], "dry_run": True}
    spec = connector.actions.get(run["action"])
    c = connectors.connection(conn, customer_id, run["app"])
    if spec is None or c is None:
        return {
            "summary": f"{connector.label}: {run['action']}",
            "changes": [],
            "checks": [{"label": "Integration connected", "ok": False, "detail": "It is no longer connected."}],
            "notes": [],
            "dry_run": True,
        }
    out = preview(conn, customer_id, connector, {**c, "test": run["test"]}, spec, run["inputs"])
    conn.execute("UPDATE action_runs SET preview = %s WHERE id = %s", (_jsonb(out), run["id"]))
    return out


def _jsonb(v: Any):
    from psycopg.types.json import Jsonb

    return Jsonb(v)
