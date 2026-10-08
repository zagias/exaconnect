"""Simulated calendar and CRM: real behaviour, stored in sim_records, for test
mode, the lab and businesses trying Jibsy before they connect a real app.

Bookings respect business hours and existing bookings, so the AI cannot
"invent availability". A connection can be told to fail
(settings.simulate_failure = "expired_signin" | "permission" | "provider")
to rehearse a broken integration.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import ActionSpec, Connector, ConnectorError, Field, register

SLOT_MINUTES = 30


def _parse_time(v: Any) -> dt.datetime:
    if isinstance(v, dt.datetime):
        t = v
    else:
        try:
            t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError as e:
            raise ValueError("Start time must be a date and time like 2026-10-07T14:00:00Z.") from e
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.UTC)
    return t


def _fail_if_told(connection: dict) -> None:
    mode = (connection.get("settings") or {}).get("simulate_failure")
    if mode == "expired_signin":
        raise ConnectorError("The calendar sign-in has expired.", "expired_signin")
    if mode == "permission":
        raise ConnectorError("The calendar refused: missing permission to write events.", "permission")
    if mode == "provider":
        raise ConnectorError("The calendar service answered 503.", "provider")


class SimulatedCalendar(Connector):
    app = "sim_calendar"
    label = "Example calendar"
    category = "example"
    description = "A built-in calendar for trying bookings before connecting Google Calendar."
    actions = {
        "find_slots": ActionSpec(
            "find_slots",
            "Find free times",
            "read",
            fields=(Field("date", "Date", "date"),),
        ),
        "book": ActionSpec(
            "book",
            "Book an appointment",
            "create",
            fields=(
                Field("start", "Start time", "datetime"),
                Field("name", "Customer name"),
                Field("contact", "Email or phone"),
                Field("reason", "Reason", required=False),
            ),
        ),
        "cancel": ActionSpec(
            "cancel",
            "Cancel an appointment",
            "cancel",
            sensitive=True,
            fields=(Field("booking_id", "Booking"),),
        ),
    }

    def validate(self, action: str, inputs: dict) -> dict:
        out = super().validate(action, inputs)
        if action == "book":
            start = _parse_time(out["start"])
            if start < dt.datetime.now(dt.UTC):
                raise ValueError("That time has already passed.")
            if start.minute % SLOT_MINUTES or start.second:
                raise ValueError(f"Bookings start on the hour or half hour ({SLOT_MINUTES}-minute slots).")
            out["start"] = start.isoformat()
        if action == "find_slots":
            try:
                out["date"] = dt.date.fromisoformat(str(out["date"])).isoformat()
            except ValueError as e:
                raise ValueError("Date must look like 2026-10-07.") from e
        return out

    def _hours(self, connection: dict) -> tuple[int, int]:
        s = connection.get("settings") or {}
        return int(s.get("open_hour", 9)), int(s.get("close_hour", 17))

    def _taken(self, conn, customer_id: Any) -> set[str]:
        rows = conn.execute(
            """SELECT data->>'start' AS start FROM sim_records WHERE customer_id = %s AND app = %s
               AND kind = 'booking' AND NOT (data ? 'cancelled_at')""",
            (customer_id, self.app),
        ).fetchall()
        return {r["start"] for r in rows}

    def execute(self, conn: psycopg.Connection, connection: dict, action: str, inputs: dict, key: str) -> dict:
        _fail_if_told(connection)
        cid = connection["customer_id"]
        open_h, close_h = self._hours(connection)
        if action == "find_slots":
            day = dt.date.fromisoformat(inputs["date"])
            taken = self._taken(conn, cid)
            slots = []
            t = dt.datetime.combine(day, dt.time(open_h), tzinfo=dt.UTC)
            end = dt.datetime.combine(day, dt.time(close_h), tzinfo=dt.UTC)
            while t < end:
                if t > dt.datetime.now(dt.UTC) and t.isoformat() not in taken:
                    slots.append(t.isoformat())
                t += dt.timedelta(minutes=SLOT_MINUTES)
            return {"slots": slots}
        if action == "book":
            existing = conn.execute(
                "SELECT * FROM sim_records WHERE customer_id = %s AND app = %s AND idempotency_key = %s",
                (cid, self.app, key),
            ).fetchone()
            if existing:
                return {"booking_id": str(existing["id"]), **existing["data"], "replayed": True}
            start = _parse_time(inputs["start"])
            if not (open_h <= start.hour < close_h):
                raise ConnectorError(f"That time is outside opening hours ({open_h}:00 to {close_h}:00 UTC).", "input")
            if start.isoformat() in self._taken(conn, cid):
                raise ConnectorError("That time is already booked.", "input")
            data = {
                "start": start.isoformat(),
                "name": inputs["name"],
                "contact": inputs["contact"],
                "reason": inputs.get("reason", ""),
            }
            row = conn.execute(
                """INSERT INTO sim_records (customer_id, app, kind, idempotency_key, data)
                   VALUES (%s, %s, 'booking', %s, %s) RETURNING id""",
                (cid, self.app, key, Jsonb(data)),
            ).fetchone()
            return {"booking_id": str(row["id"]), **data}
        if action == "cancel":
            row = conn.execute(
                """UPDATE sim_records SET data = data || jsonb_build_object('cancelled_at', now())
                   WHERE id::text = %s AND customer_id = %s AND app = %s RETURNING id""",
                (inputs["booking_id"], cid, self.app),
            ).fetchone()
            if row is None:
                raise ConnectorError("No such booking.", "input")
            return {"booking_id": str(row["id"]), "cancelled": True}
        raise ConnectorError(f"Unknown action {action}.", "input")

    def health(self, conn, connection: dict) -> dict:
        try:
            _fail_if_told(connection)
        except ConnectorError as e:
            return {"ok": False, "cause": e.cause, "detail": str(e)}
        return {"ok": True, "cause": "", "detail": "Built-in calendar answering."}


class SimulatedCRM(Connector):
    app = "sim_crm"
    label = "Example CRM"
    category = "example"
    description = "A built-in CRM for trying leads and tickets before connecting HubSpot."
    actions = {
        "find_contact": ActionSpec(
            "find_contact", "Look up a contact", "read", fields=(Field("email", "Email", "email"),)
        ),
        "create_lead": ActionSpec(
            "create_lead",
            "Create a lead",
            "create",
            fields=(Field("name", "Name"), Field("email", "Email", "email"), Field("notes", "Notes", "text", False)),
        ),
        "create_ticket": ActionSpec(
            "create_ticket",
            "Create a ticket",
            "create",
            fields=(Field("subject", "Subject"), Field("description", "Description", "text", False)),
        ),
    }

    def execute(self, conn, connection: dict, action: str, inputs: dict, key: str) -> dict:
        _fail_if_told(connection)
        cid = connection["customer_id"]
        if action == "find_contact":
            row = conn.execute(
                """SELECT id, data FROM sim_records WHERE customer_id = %s AND app = %s AND kind = 'lead'
                   AND lower(data->>'email') = lower(%s) LIMIT 1""",
                (cid, self.app, inputs["email"]),
            ).fetchone()
            return {"found": bool(row), "contact": row["data"] if row else None}
        kind = {"create_lead": "lead", "create_ticket": "ticket"}.get(action)
        if not kind:
            raise ConnectorError(f"Unknown action {action}.", "input")
        row = conn.execute(
            """INSERT INTO sim_records (customer_id, app, kind, idempotency_key, data) VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (customer_id, app, idempotency_key) DO UPDATE SET kind = EXCLUDED.kind
               RETURNING id, data""",
            (cid, self.app, kind, key, Jsonb(inputs)),
        ).fetchone()
        return {f"{kind}_id": str(row["id"]), **row["data"]}

    def health(self, conn, connection: dict) -> dict:
        try:
            _fail_if_told(connection)
        except ConnectorError as e:
            return {"ok": False, "cause": e.cause, "detail": str(e)}
        return {"ok": True, "cause": "", "detail": "Built-in CRM answering."}


register(SimulatedCalendar())
register(SimulatedCRM())
