"""Microsoft 365 / Outlook connector through Microsoft Graph v1.0 (ADR 0034):
calendar free times, bookings and cancellations, and mail.

Microsoft does not offer CalDAV for Exchange Online, so Outlook calendars use
Graph:

- Free times: ``GET /me/calendarView?startDateTime=&endDateTime=`` (paged by
  ``@odata.nextLink``); events shown as free or cancelled don't count.
- Booking: ``POST /me/events`` with a ``transactionId`` derived from the
  action's idempotency key. Graph de-duplicates on it, so a retry returns the
  first event instead of making a second.
- Cancel: ``POST /me/events/{id}/cancel`` (sensitive: a person approves).
- Mail: ``POST /me/sendMail`` with a single-value extended property holding
  a short reference; a retry first looks for that reference in Sent Items
  (``$filter=singleValueExtendedProperties/Any(...)``), so a mail is never
  sent twice. ``GET /me/messages`` finds recent mail from an address.
- Change notifications: Graph checks the address with ``validationToken``
  (echoed back as text) and every notification carries the ``clientState``
  CommAI set, checked on arrival.

Errors are ``{"error": {"code", "message"}}``: InvalidAuthenticationToken is
an expired sign-in, ErrorAccessDenied or Authorization_RequestDenied a
missing permission; throttling answers 429 with Retry-After.

Not live until ExaCarib registers an Entra ID app (EXA_MS365_CLIENT_ID,
EXA_MS365_CLIENT_SECRET) and switches ``feature/integration-microsoft365``
on; until then a connection runs on a stand-in that answers like Graph.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import urllib.parse
import uuid
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from . import ActionSpec, ConnectorError, Field
from .kit import KitConnector, Request, Setting, Simulator, ref, register, same
from .simulated import _parse_time

GRAPH = "https://graph.microsoft.com/v1.0"
STANDIN = "https://standin.graph.microsoft.com/v1.0"
REF_PROP = "String {6b1e5a8c-2b0e-4b8e-9c3f-0e1a2b3c4d5e} Name CommAIRef"


def transaction_id(key: str) -> str:
    return str(uuid.UUID(hashlib.sha256(key.encode()).hexdigest()[:32]))


def _graph_time(t: dt.datetime) -> dict:
    return {"dateTime": t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"}


def _parse_graph_time(v: dict) -> dt.datetime:
    t = dt.datetime.fromisoformat(str(v.get("dateTime", "")).split(".")[0])
    tz = v.get("timeZone") or "UTC"
    try:
        zone = ZoneInfo(tz) if tz != "UTC" else dt.UTC
    except Exception:  # noqa: BLE001 - Windows zone names: treat as UTC (we ask for UTC)
        zone = dt.UTC
    return t.replace(tzinfo=zone)


class Microsoft365(KitConnector):
    app = "microsoft365"
    label = "Microsoft 365 (Outlook)"
    category = "calendar"
    description = "Outlook calendar free times, bookings and cancellations, and sending mail, through Microsoft Graph."
    settings_fields = (
        Setting("tenant", "Microsoft tenant", r"[A-Za-z0-9.-]{1,100}"),
        Setting("open_hour", "Opening hour", r"\d{1,2}"),
        Setting("close_hour", "Closing hour", r"\d{1,2}"),
        Setting("slot_minutes", "Slot length (minutes)", r"\d{1,3}"),
    )
    needs_from_exacarib = (
        "An Entra ID (Azure AD) multi-tenant app with delegated Graph permissions "
        "Calendars.ReadWrite, Mail.Send, Mail.Read, User.Read and offline_access."
    )
    webhooks = "Graph change notifications for calendar events and mail, checked by their clientState."
    docs_url = "https://learn.microsoft.com/graph/api/overview"
    actions = {
        "find_slots": ActionSpec("find_slots", "Find free times", "read", fields=(Field("date", "Date", "date"),)),
        "find_messages": ActionSpec(
            "find_messages", "Find recent mail from someone", "read", fields=(Field("email", "From (email)", "email"),)
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
        "send_email": ActionSpec(
            "send_email",
            "Send an email",
            "create",
            fields=(Field("to", "To", "email"), Field("subject", "Subject"), Field("body", "Message", "text")),
        ),
        "cancel": ActionSpec(
            "cancel", "Cancel an appointment", "cancel", sensitive=True, fields=(Field("booking_id", "Booking"),)
        ),
    }
    mapping_targets = {
        "appointment": [
            "subject",
            "body.content",
            "start.dateTime",
            "end.dateTime",
            "location.displayName",
            "attendees.emailAddress.address",
        ]
    }
    golive_criteria = {
        "tenant-test": "Tested against a Microsoft 365 developer tenant: bookings, cancel, mail and notifications.",
        "admin-consent": "The admin-consent page for businesses' IT teams is written and checked.",
    }

    def __init__(self):
        self.simulator = GraphStandIn()

    def base_url(self, conn, connection: dict) -> str:
        return STANDIN if self.simulated(connection) else GRAPH

    def cause(self, status: int, body: Any) -> str:
        code = str(((body or {}).get("error") or {}).get("code", "")) if isinstance(body, dict) else ""
        if status == 401 or code in ("InvalidAuthenticationToken", "TokenExpired"):
            return "expired_signin"
        if status == 403 or code in ("ErrorAccessDenied", "Authorization_RequestDenied"):
            return "permission"
        if status == 404 or code == "ErrorItemNotFound":
            return "input"
        return super().cause(status, body)

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

    def busy(self, conn, connection: dict, start: dt.datetime, end: dt.datetime, raw: bool = False) -> list:
        evs = self.paginate(
            conn,
            connection,
            "/me/calendarView",
            params={
                "startDateTime": start.astimezone(dt.UTC).isoformat().replace("+00:00", "Z"),
                "endDateTime": end.astimezone(dt.UTC).isoformat().replace("+00:00", "Z"),
                "$select": "id,start,end,showAs,isCancelled,transactionId",
                "$top": 50,
            },
            items=lambda b: b.get("value", []) if isinstance(b, dict) else [],
            next_page=lambda r: r.body.get("@odata.nextLink") if isinstance(r.body, dict) else None,
            max_items=500,
        )
        evs = [e for e in evs if not e.get("isCancelled") and e.get("showAs", "busy") != "free"]
        if raw:
            return evs
        return [
            (_parse_graph_time(e["start"]), _parse_graph_time(e["end"]))
            for e in evs
            if not e.get("isCancelled") and e.get("showAs", "busy") != "free"
        ]

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
        if action == "find_slots":
            day = dt.date.fromisoformat(inputs["date"])
            t = dt.datetime.combine(day, dt.time(cfg["open"]), tzinfo=cfg["tz"])
            end = dt.datetime.combine(day, dt.time(cfg["close"]), tzinfo=cfg["tz"])
            busy = self.busy(conn, connection, t, end)
            step, now, slots = dt.timedelta(minutes=cfg["slot"]), dt.datetime.now(dt.UTC), []
            while t + step <= end:
                if t > now and not any(b0 < t + step and t < b1 for b0, b1 in busy):
                    slots.append(t.astimezone(dt.UTC).isoformat())
                t += step
            return {"slots": slots, "source": "Outlook calendar"}
        if action == "find_messages":
            q = inputs["email"].replace("'", "''")
            r = self.call(
                conn,
                connection,
                "GET",
                "/me/messages",
                params={
                    "$filter": f"from/emailAddress/address eq '{q}'",
                    "$top": 10,
                    "$select": "id,subject,receivedDateTime,bodyPreview,from",
                },
            )
            msgs = r.body.get("value", []) if isinstance(r.body, dict) else []
            return {
                "messages": [{k: m.get(k) for k in ("id", "subject", "receivedDateTime", "bodyPreview")} for m in msgs]
            }
        if action == "book":
            start = _parse_time(inputs["start"]).astimezone(cfg["tz"])
            finish = start + dt.timedelta(minutes=cfg["slot"])
            if not (cfg["open"] <= start.hour and (finish.hour, finish.minute) <= (cfg["close"], 0)):
                raise ConnectorError(
                    f"That time is outside opening hours ({cfg['open']}:00 to {cfg['close']}:00).", "input"
                )
            event = {
                "subject": f"{inputs.get('reason') or 'Appointment'}: {inputs['name']}",
                "body": {
                    "contentType": "text",
                    "content": f"Contact: {inputs['contact']}\nBooked through ExaCarib CommAI.",
                },
                "start": _graph_time(start),
                "end": _graph_time(finish),
                "transactionId": transaction_id(key),
                "showAs": "busy",
            }
            if "@" in str(inputs["contact"]):
                event["attendees"] = [
                    {"emailAddress": {"address": inputs["contact"], "name": inputs["name"]}, "type": "required"}
                ]
            if self.dry(connection):
                return {"dry_run": True, "would_send": {"POST /me/events": event}}
            known = self.known(conn, connection, key)
            if known:
                return {
                    "booking_id": known["object_id"],
                    "start": start.astimezone(dt.UTC).isoformat(),
                    "replayed": True,
                }
            taken = self.busy(conn, connection, start, finish, raw=True)
            ours = next((e for e in taken if e.get("transactionId") == event["transactionId"]), None)
            if ours:  # created by an earlier attempt whose record was lost
                self.remember(conn, connection, key, "booking", ours["id"])
                return {"booking_id": ours["id"], "start": start.astimezone(dt.UTC).isoformat(), "replayed": True}
            if taken:
                raise ConnectorError("That time is already booked.", "input")
            r = self.call(conn, connection, "POST", "/me/events", json_body=event, ok=(200, 201))
            self.remember(conn, connection, key, "booking", r.body["id"])
            return {
                "booking_id": r.body["id"],
                "start": start.astimezone(dt.UTC).isoformat(),
                "link": r.body.get("webLink", ""),
            }
        if action == "send_email":
            reference = ref(key)
            msg = {
                "message": {
                    "subject": inputs["subject"],
                    "body": {"contentType": "Text", "content": inputs["body"]},
                    "toRecipients": [{"emailAddress": {"address": inputs["to"]}}],
                    "singleValueExtendedProperties": [{"id": REF_PROP, "value": reference}],
                },
                "saveToSentItems": True,
            }
            if self.dry(connection):
                return {"dry_run": True, "would_send": {"POST /me/sendMail": msg}}
            if self.known(conn, connection, key):
                return {"sent": True, "reference": reference, "replayed": True}
            flt = f"singleValueExtendedProperties/Any(ep: ep/id eq '{REF_PROP}' and ep/value eq '{reference}')"
            r = self.call(
                conn, connection, "GET", "/me/mailFolders/sentitems/messages", params={"$filter": flt, "$select": "id"}
            )
            if isinstance(r.body, dict) and r.body.get("value"):
                self.remember(conn, connection, key, "mail", reference)
                return {"sent": True, "reference": reference, "replayed": True}
            self.call(conn, connection, "POST", "/me/sendMail", json_body=msg, ok=(202,))
            self.remember(conn, connection, key, "mail", reference)
            return {"sent": True, "reference": reference}
        if action == "cancel":
            bid = str(inputs["booking_id"])
            if self.dry(connection):
                return {"dry_run": True, "would_send": {"POST": f"/me/events/{bid}/cancel"}}
            r = self.call(
                conn,
                connection,
                "POST",
                f"/me/events/{urllib.parse.quote(bid, safe='')}/cancel",
                json_body={"comment": "Cancelled through ExaCarib CommAI."},
                ok=(202, 200, 204),
                allow=(404,),
            )
            if r.status == 404:
                raise ConnectorError("No such booking.", "input")
            return {"booking_id": bid, "cancelled": True}
        raise ConnectorError(f"Unknown action {action}.", "input")

    health_path = "/me?$select=id,displayName"

    # Change notifications.
    def webhook_handshake(self, headers: dict, body: bytes, query: dict):
        token = query.get("validationToken")
        if token is not None:
            return 200, "text/plain", token
        return None

    def verify_webhook(self, conn, connection: dict, hook: dict, headers: dict, body: bytes, query: dict) -> bool:
        try:
            items = json.loads(body or b"{}").get("value", [])
        except (ValueError, AttributeError):
            return False
        return bool(items) and all(same(str(i.get("clientState", "")), hook["secret"][:128]) for i in items)

    def webhook_events(self, body: bytes, headers: dict) -> list[dict]:
        out = []
        for i in json.loads(body).get("value", []):
            res = str(i.get("resource", ""))
            kind = "event" if "/events" in res.lower() else "message" if "/messages" in res.lower() else "resource"
            out.append(
                {
                    "type": f"microsoft365.{kind}.{i.get('changeType', 'updated')}",
                    "id": ":".join(
                        [
                            str(i.get("subscriptionId", "")),
                            str((i.get("resourceData") or {}).get("id", res)),
                            str(i.get("changeType", "")),
                        ]
                    ),
                    "data": {
                        "resource": res,
                        "change": i.get("changeType"),
                        "id": (i.get("resourceData") or {}).get("id"),
                    },
                }
            )
        return out

    def sample_inputs(self, action: str) -> dict:
        tomorrow = (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).date()
        return {"find_slots": {"date": tomorrow.isoformat()}, "find_messages": {"email": "test@example.com"}}.get(
            action, {}
        )


class GraphStandIn(Simulator):
    app = "microsoft365"

    def error_body(self, status: int, message: str) -> Any:
        code = {
            401: "InvalidAuthenticationToken",
            403: "ErrorAccessDenied",
            404: "ErrorItemNotFound",
            429: "TooManyRequests",
        }
        return {"error": {"code": code.get(status, "ServiceNotAvailable"), "message": message}}

    @Simulator.route("GET", r"/v1\.0/me$")
    def me(self, conn, connection, req: Request, m):
        return 200, {"id": "standin-user", "displayName": "Stand-in user", "mail": "user@standin.example"}

    @Simulator.route("GET", r"/v1\.0/me/calendarView$")
    def calendar_view(self, conn, connection, req: Request, m):
        q = req.query
        a = dt.datetime.fromisoformat(q["startDateTime"].replace("Z", "+00:00"))
        b = dt.datetime.fromisoformat(q["endDateTime"].replace("Z", "+00:00"))
        rows = [
            e
            for e in self.all(conn, connection, "event")
            if _parse_graph_time(e["start"]) < b and a < _parse_graph_time(e["end"])
        ]
        skip = int(q.get("$skip", "0"))
        page = rows[skip : skip + 2]
        body: dict = {"value": page}
        if skip + 2 < len(rows):
            body["@odata.nextLink"] = f"{STANDIN}/me/calendarView?" + urllib.parse.urlencode({**q, "$skip": skip + 2})
        return 200, body

    @Simulator.route("POST", r"/v1\.0/me/events$")
    def create_event(self, conn, connection, req: Request, m):
        body = req.body if isinstance(req.body, dict) else {}
        tid = body.get("transactionId")
        if tid:
            prev = next((e for e in self.all(conn, connection, "event") if e.get("transactionId") == tid), None)
            if prev:
                return 201, prev
        eid = "AAMk" + self.new_id(conn, connection, "event")
        ev = {"id": eid, "isCancelled": False, "webLink": f"https://outlook.office365.com/owa/?itemid={eid}", **body}
        self.put(conn, connection, "event", eid, ev)
        return 201, ev

    @Simulator.route("POST", r"/v1\.0/me/events/([^/]+)/cancel$")
    def cancel(self, conn, connection, req: Request, m):
        eid = urllib.parse.unquote(m.group(1))
        ev = self.get(conn, connection, "event", eid)
        if not ev:
            return 404, self.error_body(404, "The specified object was not found in the store.")
        self.delete(conn, connection, "event", eid)
        return 202, ""

    @Simulator.route("POST", r"/v1\.0/me/sendMail$")
    def send_mail(self, conn, connection, req: Request, m):
        msg = (req.body or {}).get("message") or {}
        if not msg.get("toRecipients"):
            return 400, {"error": {"code": "ErrorInvalidRecipients", "message": "At least one recipient is required."}}
        mid = "AAMkmsg" + self.new_id(conn, connection, "sent")
        self.put(conn, connection, "sent", mid, {"id": mid, **msg})
        return 202, ""

    @Simulator.route("GET", r"/v1\.0/me/mailFolders/sentitems/messages$")
    def sent(self, conn, connection, req: Request, m):
        flt = req.query.get("$filter", "")
        want = flt.rsplit("ep/value eq '", 1)[-1].rstrip("')") if "ep/value eq '" in flt else None
        rows = [
            {"id": s["id"]}
            for s in self.all(conn, connection, "sent")
            if want is None or any(p.get("value") == want for p in s.get("singleValueExtendedProperties", []))
        ]
        return 200, {"value": rows}

    @Simulator.route("GET", r"/v1\.0/me/messages$")
    def messages(self, conn, connection, req: Request, m):
        return 200, {"value": self.all(conn, connection, "inbox")}


register(Microsoft365())
