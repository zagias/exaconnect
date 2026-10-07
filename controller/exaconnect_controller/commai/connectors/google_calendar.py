"""Google Calendar connector (ADR 0020): free/busy lookup and bookings.

Written from the public Calendar API v3 (freeBusy.query, events.insert,
events.get, events.delete). NOT LIVE until ExaCarib registers a Google OAuth
app and sets EXA_GOOGLE_CLIENT_ID, EXA_GOOGLE_CLIENT_SECRET and
EXA_PUBLIC_URL (see automation/oauth.py). Tested only against a fake HTTP
layer.

- Availability always comes from Google's free/busy answer and the
  business's opening hours, so the AI cannot invent a free slot.
- A booking's event id is derived from the action's idempotency key, so a
  retry hits Google's "already exists" (409) and returns the first booking
  instead of making a second one.
- In test mode, bookings go to `settings.test_calendar_id` when one is set;
  otherwise the request is checked and shown but not sent.

Connection settings: calendar_id (default "primary"), open_hour, close_hour
(business hours in the business's time zone), slot_minutes (default 30),
test_calendar_id.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import urllib.parse
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from . import ActionSpec, Connector, ConnectorError, Field, register
from .simulated import _parse_time

API = "https://www.googleapis.com/calendar/v3"


def event_id(key: str) -> str:
    """Google event ids use base32hex characters (a-v, 0-9), 5 to 1024 long."""
    return base64.b32hexencode(hashlib.sha256(key.encode()).digest()).decode().lower().rstrip("=")


def _cause(status: int, body: Any) -> str:
    err = body.get("error") if isinstance(body, dict) else None
    errs = (err.get("errors") or []) if isinstance(err, dict) else []
    reason = errs[0].get("reason", "") if errs and isinstance(errs[0], dict) else ""
    if status == 401:
        return "expired_signin"
    if status == 403 and reason not in ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"):
        return "permission"
    if status == 404:
        return "mapping"
    if status == 400:
        return "input"
    return "provider"


def _message(status: int, body: Any) -> str:
    msg = ""
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        msg = str(body["error"].get("message", ""))[:200]
    return f"Google Calendar answered {status}" + (f": {msg}" if msg else ".")


class GoogleCalendar(Connector):
    app = "google_calendar"
    label = "Google Calendar"
    description = "Look up free times and book appointments in a Google calendar."
    auth = "oauth"
    category = "calendar"
    actions = {
        "find_slots": ActionSpec("find_slots", "Find free times", "read", fields=(Field("date", "Date", "date"),)),
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
            "cancel", "Cancel an appointment", "cancel", sensitive=True, fields=(Field("booking_id", "Booking"),)
        ),
    }
    # Fields a business can map (automation/integrations.py suggests them).
    mapping_targets = {
        "appointment": ["summary", "description", "start.dateTime", "end.dateTime", "location", "attendees.email"],
    }

    # ---- helpers ----------------------------------------------------------------

    def _token(self, conn, connection: dict) -> str:
        from ..automation import oauth

        return oauth.access_token(conn, connection)

    def _call(self, conn, connection: dict, method: str, path: str, **kw):
        from ..automation import http

        try:
            return http.request(method, API + path, token=self._token(conn, connection), **kw)
        except http.NetworkError as e:
            raise ConnectorError(str(e), "provider") from None

    def _settings(self, conn, connection: dict) -> dict:
        s = connection.get("settings") or {}
        tz = "UTC"
        row = conn.execute(
            "SELECT timezone FROM commai_settings WHERE customer_id = %s", (connection["customer_id"],)
        ).fetchone()
        if row and row["timezone"]:
            tz = row["timezone"]
        calendar = s.get("calendar_id") or "primary"
        if connection.get("test") and s.get("test_calendar_id"):
            calendar = s["test_calendar_id"]
        return {
            "calendar": calendar,
            "open": int(s.get("open_hour", 9)),
            "close": int(s.get("close_hour", 17)),
            "slot": int(s.get("slot_minutes", 30)),
            "tz": ZoneInfo(tz),
        }

    def _busy(self, conn, connection: dict, cfg: dict, start: dt.datetime, end: dt.datetime) -> list[tuple]:
        r = self._call(
            conn,
            connection,
            "POST",
            "/freeBusy",
            json_body={
                "timeMin": start.astimezone(dt.UTC).isoformat().replace("+00:00", "Z"),
                "timeMax": end.astimezone(dt.UTC).isoformat().replace("+00:00", "Z"),
                "items": [{"id": cfg["calendar"]}],
            },
        )
        if r.status != 200:
            raise ConnectorError(_message(r.status, r.body), _cause(r.status, r.body))
        cal = (r.body.get("calendars") or {}).get(cfg["calendar"]) or {}
        if cal.get("errors"):
            reason = cal["errors"][0].get("reason", "")
            raise ConnectorError(
                f"Google Calendar can't read calendar {cfg['calendar']!r} ({reason}).",
                "mapping" if reason == "notFound" else "permission",
            )
        return [(_parse_time(b["start"]), _parse_time(b["end"])) for b in cal.get("busy") or []]

    # ---- the connector ----------------------------------------------------------

    def validate(self, action: str, inputs: dict) -> dict:
        out = super().validate(action, inputs)
        if action == "book":
            start = _parse_time(out["start"])
            if start < dt.datetime.now(dt.UTC):
                raise ValueError("That time has already passed.")
            out["start"] = start.isoformat()
        if action == "find_slots":
            try:
                out["date"] = dt.date.fromisoformat(str(out["date"])).isoformat()
            except ValueError as e:
                raise ValueError("Date must look like 2026-10-07.") from e
        return out

    def execute(self, conn: psycopg.Connection, connection: dict, action: str, inputs: dict, key: str) -> dict:
        cfg = self._settings(conn, connection)
        if action == "find_slots":
            day = dt.date.fromisoformat(inputs["date"])
            t = dt.datetime.combine(day, dt.time(cfg["open"]), tzinfo=cfg["tz"])
            end = dt.datetime.combine(day, dt.time(cfg["close"]), tzinfo=cfg["tz"])
            busy = self._busy(conn, connection, cfg, t, end)
            step = dt.timedelta(minutes=cfg["slot"])
            now = dt.datetime.now(dt.UTC)
            slots = []
            while t + step <= end:
                if t > now and not any(b0 < t + step and t < b1 for b0, b1 in busy):
                    slots.append(t.astimezone(dt.UTC).isoformat())
                t += step
            return {"slots": slots, "source": "Google Calendar free/busy"}
        if action == "book":
            start = _parse_time(inputs["start"]).astimezone(cfg["tz"])
            finish = start + dt.timedelta(minutes=cfg["slot"])
            if not (cfg["open"] <= start.hour and (finish.hour, finish.minute) <= (cfg["close"], 0)):
                raise ConnectorError(
                    f"That time is outside opening hours ({cfg['open']}:00 to {cfg['close']}:00).", "input"
                )
            eid = event_id(key)
            body = {
                "id": eid,
                "summary": f"{inputs.get('reason') or 'Appointment'}: {inputs['name']}",
                "description": f"Contact: {inputs['contact']}\nBooked through ExaCarib CommAI.",
                "start": {"dateTime": start.isoformat()},
                "end": {"dateTime": finish.isoformat()},
                "extendedProperties": {"private": {"commai_key": hashlib.sha256(key.encode()).hexdigest()[:32]}},
            }
            if connection.get("test") and not (connection.get("settings") or {}).get("test_calendar_id"):
                return {"dry_run": True, "would_send": {"calendar": cfg["calendar"], "event": body}}
            cal = urllib.parse.quote(cfg["calendar"], safe="")
            existing = self._call(conn, connection, "GET", f"/calendars/{cal}/events/{eid}")
            if existing.status == 200 and isinstance(existing.body, dict):
                return self._booking(existing.body, replayed=True)
            if self._busy(conn, connection, cfg, start, finish):
                raise ConnectorError("That time is already booked.", "input")
            r = self._call(conn, connection, "POST", f"/calendars/{cal}/events", json_body=body)
            if r.status == 409:  # created by an earlier attempt: never book twice
                again = self._call(conn, connection, "GET", f"/calendars/{cal}/events/{eid}")
                if again.status == 200:
                    return self._booking(again.body, replayed=True)
            if r.status not in (200, 201):
                raise ConnectorError(_message(r.status, r.body), _cause(r.status, r.body))
            return self._booking(r.body)
        if action == "cancel":
            cal = urllib.parse.quote(cfg["calendar"], safe="")
            bid = urllib.parse.quote(str(inputs["booking_id"]), safe="")
            if connection.get("test") and not (connection.get("settings") or {}).get("test_calendar_id"):
                return {"dry_run": True, "would_send": {"delete": inputs["booking_id"]}}
            r = self._call(conn, connection, "DELETE", f"/calendars/{cal}/events/{bid}")
            if r.status in (200, 204, 410):  # 410: already cancelled
                return {"booking_id": inputs["booking_id"], "cancelled": True}
            if r.status == 404:
                raise ConnectorError("No such booking.", "input")
            raise ConnectorError(_message(r.status, r.body), _cause(r.status, r.body))
        raise ConnectorError(f"Unknown action {action}.", "input")

    @staticmethod
    def _booking(ev: dict, replayed: bool = False) -> dict:
        out = {
            "booking_id": ev.get("id", ""),
            "start": (ev.get("start") or {}).get("dateTime", ""),
            "status": ev.get("status", ""),
            "link": ev.get("htmlLink", ""),
        }
        if replayed:
            out["replayed"] = True
        return out

    def health(self, conn, connection: dict) -> dict:
        """A cheap read: the calendar's metadata."""
        cfg = self._settings(conn, connection)
        try:
            r = self._call(conn, connection, "GET", f"/calendars/{urllib.parse.quote(cfg['calendar'], safe='')}")
        except ConnectorError as e:
            return {"ok": False, "cause": e.cause, "detail": str(e)}
        if r.status == 200:
            return {
                "ok": True,
                "cause": "",
                "detail": f"Calendar {r.body.get('summary', cfg['calendar'])!r} answering.",
            }
        return {"ok": False, "cause": _cause(r.status, r.body), "detail": _message(r.status, r.body)}

    def sample_inputs(self, action: str) -> dict:
        tomorrow = (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).date()
        return {"find_slots": {"date": tomorrow.isoformat()}}.get(action, {})


register(GoogleCalendar())
