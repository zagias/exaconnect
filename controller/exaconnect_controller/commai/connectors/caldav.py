"""CalDAV calendars (RFC 4791) with iCalendar events (RFC 5545), ADR 0028.

Works with any CalDAV server: iCloud (caldav.icloud.com), Fastmail
(caldav.fastmail.com), Nextcloud (/remote.php/dav), Zimbra, Radicale, and
Google's CalDAV endpoint. The business signs in with a user name and an app
password (stored encrypted); the calendar is found by discovery (principal,
calendar-home-set, first calendar) or given as ``collection_url``.

- Free times come from the calendar's own events (a calendar-query REPORT
  with a time range) and opening hours, so the AI cannot invent a slot.
- A booking is a PUT of a new event whose UID and file name come from the
  action's idempotency key, with ``If-None-Match: *``: a retry gets 412
  (already there) and returns the first booking instead of booking twice.
- Cancelling deletes the event (sensitive: a person approves).
- The booking result carries an .ics invite link for confirmations.

Off until ExaCarib switches ``feature/integration-caldav`` on; until then a
connection runs on a stand-in DAV server.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from zoneinfo import ZoneInfo

import psycopg

from ..standards import dav, ical
from . import ActionSpec, ConnectorError, Field
from .dav_common import DavConnector, DavStandIn
from .kit import Setting, register
from .simulated import _parse_time


def uid_for(key: str) -> str:
    return "commai-" + hashlib.sha256(key.encode()).hexdigest()[:32]


class CalDAV(DavConnector):
    app = "caldav"
    label = "CalDAV calendar"
    category = "calendar"
    description = "Find free times and book appointments in any CalDAV calendar: iCloud, Fastmail, Nextcloud, Zimbra."
    home_set = (dav.CALDAV, "calendar-home-set")
    collection_type = (dav.CALDAV, "calendar")
    settings_fields = DavConnector.settings_fields + (
        Setting("open_hour", "Opening hour", r"\d{1,2}"),
        Setting("close_hour", "Closing hour", r"\d{1,2}"),
        Setting("slot_minutes", "Slot length (minutes)", r"\d{1,3}"),
    )
    needs_from_exacarib = (
        "Nothing to register: each business enters its CalDAV server and an app password. "
        "ExaCarib switches the capability on after a test against iCloud, Fastmail and Nextcloud."
    )
    webhooks = "CalDAV has no push; free times are read when asked."
    docs_url = "https://www.rfc-editor.org/rfc/rfc4791"
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
    mapping_targets = {"appointment": ["SUMMARY", "DESCRIPTION", "DTSTART", "DTEND", "LOCATION", "ATTENDEE"]}
    golive_criteria = {
        "servers": "Booking, free times and cancel tested against iCloud, Fastmail and Nextcloud test accounts.",
    }

    def __init__(self):
        self.simulator = DavStandIn(self.app)

    def _cfg(self, conn, connection: dict) -> dict:
        s = connection.get("settings") or {}
        row = conn.execute(
            "SELECT timezone FROM commai_settings WHERE customer_id = %s", (connection["customer_id"],)
        ).fetchone()
        return {
            "open": int(s.get("open_hour", 9)),
            "close": int(s.get("close_hour", 17)),
            "slot": int(s.get("slot_minutes", 30)),
            "tz": ZoneInfo((row or {}).get("timezone") or "UTC"),
        }

    def busy(self, conn, connection: dict, url: str, start: dt.datetime, end: dt.datetime) -> list[tuple]:
        r = self.dav(conn, connection, "REPORT", url, dav.calendar_query(ical.utc(start), ical.utc(end)), depth="1")
        res, _ = dav.multistatus(r.body if isinstance(r.body, str) else "")
        out = []
        for x in res:
            data = x.text(dav.CALDAV, "calendar-data")
            if data:
                out += ical.busy_periods(data)
        return out

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
        cfg = self._cfg(conn, connection)
        url = self.collection(conn, connection)
        if action == "find_slots":
            day = dt.date.fromisoformat(inputs["date"])
            t = dt.datetime.combine(day, dt.time(cfg["open"]), tzinfo=cfg["tz"])
            end = dt.datetime.combine(day, dt.time(cfg["close"]), tzinfo=cfg["tz"])
            busy = self.busy(conn, connection, url, t, end)
            step, now, slots = dt.timedelta(minutes=cfg["slot"]), dt.datetime.now(dt.UTC), []
            while t + step <= end:
                if t > now and not any(b0 < t + step and t < b1 for b0, b1 in busy):
                    slots.append(t.astimezone(dt.UTC).isoformat())
                t += step
            return {"slots": slots, "source": "CalDAV calendar"}
        if action == "book":
            start = _parse_time(inputs["start"]).astimezone(cfg["tz"])
            finish = start + dt.timedelta(minutes=cfg["slot"])
            if not (cfg["open"] <= start.hour and (finish.hour, finish.minute) <= (cfg["close"], 0)):
                raise ConnectorError(
                    f"That time is outside opening hours ({cfg['open']}:00 to {cfg['close']}:00).", "input"
                )
            uid = uid_for(key)
            href = dav.join(url, f"{uid}.ics")
            event = ical.Event(
                uid=uid,
                start=start,
                end=finish,
                summary=f"{inputs.get('reason') or 'Appointment'}: {inputs['name']}",
                description=f"Contact: {inputs['contact']}\nBooked through ExaCarib CommAI.",
            )
            body = ical.calendar([event])
            if self.dry(connection):
                return {"dry_run": True, "would_send": {"PUT": href, "event": body}}
            existing = self.dav(conn, connection, "GET", href, ok=(200,), allow=(404,))
            if existing.status == 200:
                return self._booking(uid, start, replayed=True)
            if self.busy(conn, connection, url, start, finish):
                raise ConnectorError("That time is already booked.", "input")
            r = self.dav(
                conn,
                connection,
                "PUT",
                href,
                body.encode(),
                ctype="text/calendar; charset=utf-8",
                headers={"If-None-Match": "*"},
                ok=(200, 201, 204),
                allow=(412,),
            )
            if r.status == 412:  # created by an earlier attempt: never book twice
                return self._booking(uid, start, replayed=True)
            self.remember(conn, connection, key, "booking", uid)
            return self._booking(uid, start)
        if action == "cancel":
            bid = str(inputs["booking_id"])
            if not bid.replace("-", "").isalnum():
                raise ConnectorError("That is not a booking reference.", "input")
            href = dav.join(url, f"{bid}.ics")
            if self.dry(connection):
                return {"dry_run": True, "would_send": {"DELETE": href}}
            r = self.dav(conn, connection, "DELETE", href, ok=(200, 204), allow=(404, 410))
            if r.status == 404 and not self.known_booking(conn, connection, bid):
                raise ConnectorError("No such booking.", "input")
            return {"booking_id": bid, "cancelled": True}
        raise ConnectorError(f"Unknown action {action}.", "input")

    def known_booking(self, conn, connection: dict, uid: str) -> bool:
        return bool(
            conn.execute(
                "SELECT 1 FROM commai_connector_objects WHERE customer_id = %s AND app = %s AND object_id = %s",
                (connection["customer_id"], self.app, uid),
            ).fetchone()
        )

    @staticmethod
    def _booking(uid: str, start: dt.datetime, replayed: bool = False) -> dict:
        out = {"booking_id": uid, "start": start.astimezone(dt.UTC).isoformat(), "status": "confirmed"}
        if replayed:
            out["replayed"] = True
        return out

    def sample_inputs(self, action: str) -> dict:
        tomorrow = (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).date()
        return {"find_slots": {"date": tomorrow.isoformat()}}.get(action, {})


register(CalDAV())
