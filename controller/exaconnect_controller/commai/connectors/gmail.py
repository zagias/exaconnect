"""Gmail / Google Workspace mail through the Gmail API v1 (ADR 0034).

- Send: ``POST /gmail/v1/users/me/messages/send`` with the RFC 5322 message
  base64url-encoded in ``raw``. The Message-ID is derived from the action's
  idempotency key; a retry first searches ``rfc822msgid:<id>`` (Gmail keeps
  sent mail), so a mail is never sent twice.
- Read: ``GET /messages?q=from:<address> newer_than:30d`` (paged by
  ``nextPageToken``), then ``GET /messages/{id}?format=metadata``.
- Push: Gmail ``users.watch`` publishes to a Google Cloud Pub/Sub topic whose
  push subscription calls CommAI's app hook address; the subscription URL
  carries the hook's token as ``?token=`` (checked on every delivery). The
  notification names the mailbox and a ``historyId``.

Sign-in uses ExaCarib's Google OAuth app (the same one as Google Calendar)
with the gmail.send and gmail.readonly scopes, which Google treats as
restricted: the app needs Google's verification before businesses outside
ExaCarib can use it.

Not live until EXA_GOOGLE_CLIENT_ID and EXA_GOOGLE_CLIENT_SECRET are set,
Google has verified the scopes, and ``feature/integration-gmail`` is on;
until then a connection runs on a stand-in.

Generic mailboxes (any provider, including Gmail with an app password) are
the IMAP/SMTP option of the email channel instead (channels/mailbox.py).
"""

from __future__ import annotations

import base64
import hashlib
import json
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as default_policy
from typing import Any

import psycopg

from . import ActionSpec, ConnectorError, Field
from .kit import KitConnector, Request, Simulator, register, same

API = "https://gmail.googleapis.com/gmail/v1/users/me"
STANDIN = "https://standin.gmail.googleapis.com/gmail/v1/users/me"


def message_id(key: str) -> str:
    return f"<commai.{hashlib.sha256(key.encode()).hexdigest()[:32]}@mail.exacarib.com>"


class Gmail(KitConnector):
    app = "gmail"
    label = "Gmail (Google Workspace)"
    category = "email"
    description = "Send email from a Gmail or Google Workspace mailbox and find recent mail from a customer."
    needs_from_exacarib = (
        "The Google OAuth app (shared with Google Calendar) with the gmail.send and gmail.readonly scopes, "
        "verified by Google (restricted scopes), and a Pub/Sub topic for push."
    )
    webhooks = "Gmail push through Google Cloud Pub/Sub, checked by the hook token in the push address."
    docs_url = "https://developers.google.com/gmail/api/reference/rest"
    actions = {
        "find_messages": ActionSpec(
            "find_messages", "Find recent mail from someone", "read", fields=(Field("email", "From (email)", "email"),)
        ),
        "send_email": ActionSpec(
            "send_email",
            "Send an email",
            "create",
            fields=(Field("to", "To", "email"), Field("subject", "Subject"), Field("body", "Message", "text")),
        ),
    }
    golive_criteria = {
        "google-verification": "Google has verified the OAuth app for the restricted Gmail scopes.",
    }

    def __init__(self):
        self.simulator = GmailStandIn()

    def base_url(self, conn, connection: dict) -> str:
        return STANDIN if self.simulated(connection) else API

    def cause(self, status: int, body: Any) -> str:
        err = (body or {}).get("error") if isinstance(body, dict) else None
        reason = ""
        if isinstance(err, dict) and err.get("errors"):
            reason = str(err["errors"][0].get("reason", ""))
        if status == 403 and reason in ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"):
            return "provider"
        if status == 403 and reason in ("insufficientPermissions", "forbidden"):
            return "permission"
        return super().cause(status, body)

    def execute(self, conn: psycopg.Connection, connection: dict, action: str, inputs: dict, key: str) -> dict:
        if action == "find_messages":
            q = f"from:{inputs['email']} newer_than:30d"
            ids = list(
                self.paginate(
                    conn,
                    connection,
                    "/messages",
                    params={"q": q, "maxResults": 10},
                    items=lambda b: b.get("messages", []) if isinstance(b, dict) else [],
                    next_page=lambda r: (
                        {"pageToken": r.body["nextPageToken"]}
                        if isinstance(r.body, dict) and r.body.get("nextPageToken")
                        else None
                    ),
                    max_items=10,
                )
            )
            out = []
            for m in ids:
                r = self.call(
                    conn,
                    connection,
                    "GET",
                    f"/messages/{m['id']}",
                    params={"format": "metadata", "metadataHeaders": ["Subject", "Date", "From"]},
                )
                hdrs = {h["name"]: h["value"] for h in ((r.body or {}).get("payload") or {}).get("headers", [])}
                out.append(
                    {
                        "id": m["id"],
                        "subject": hdrs.get("Subject", ""),
                        "date": hdrs.get("Date", ""),
                        "snippet": r.body.get("snippet", ""),
                    }
                )
            return {"messages": out}
        if action == "send_email":
            mid = message_id(key)
            msg = EmailMessage()
            msg["To"] = inputs["to"]
            msg["Subject"] = inputs["subject"]
            msg["Message-ID"] = mid
            msg.set_content(inputs["body"])
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
            if self.dry(connection):
                return {
                    "dry_run": True,
                    "would_send": {"to": inputs["to"], "subject": inputs["subject"], "message_id": mid},
                }
            if self.known(conn, connection, key):
                return {"sent": True, "message_id": mid, "replayed": True}
            r = self.call(
                conn, connection, "GET", "/messages", params={"q": f"rfc822msgid:{mid.strip('<>')}", "maxResults": 1}
            )
            found = (r.body or {}).get("messages") if isinstance(r.body, dict) else None
            if found:
                self.remember(conn, connection, key, "mail", found[0]["id"])
                return {"sent": True, "message_id": mid, "gmail_id": found[0]["id"], "replayed": True}
            r = self.call(conn, connection, "POST", "/messages/send", json_body={"raw": raw})
            self.remember(conn, connection, key, "mail", r.body["id"])
            return {"sent": True, "message_id": mid, "gmail_id": r.body["id"], "thread_id": r.body.get("threadId", "")}
        raise ConnectorError(f"Unknown action {action}.", "input")

    health_path = "/profile"

    # Pub/Sub push: the token in the push address is the hook's secret.
    def verify_webhook(self, conn, connection: dict, hook: dict, headers: dict, body: bytes, query: dict) -> bool:
        return same(str(query.get("token", "")), hook["secret"])

    def webhook_events(self, body: bytes, headers: dict) -> list[dict]:
        d = json.loads(body)
        msg = d.get("message") or {}
        data = json.loads(base64.b64decode(msg.get("data", "") + "==").decode() or "{}")
        return [
            {
                "type": "gmail.mailbox.changed",
                "id": str(msg.get("messageId") or msg.get("message_id") or data.get("historyId")),
                "data": {"email": data.get("emailAddress"), "history_id": data.get("historyId")},
            }
        ]

    def sample_inputs(self, action: str) -> dict:
        return {"find_messages": {"email": "test@example.com"}}.get(action, {})


class GmailStandIn(Simulator):
    app = "gmail"

    def error_body(self, status: int, message: str) -> Any:
        reason = {401: "authError", 403: "insufficientPermissions", 429: "rateLimitExceeded"}.get(
            status, "backendError"
        )
        return {"error": {"code": status, "message": message, "errors": [{"reason": reason, "message": message}]}}

    @Simulator.route("GET", r"/users/me/profile$")
    def profile(self, conn, connection, req: Request, m):
        return 200, {"emailAddress": "mailbox@standin.example", "messagesTotal": 0, "historyId": "1"}

    @Simulator.route("GET", r"/users/me/messages$")
    def list_messages(self, conn, connection, req: Request, m):
        q = req.query.get("q", "")
        rows = self.all(conn, connection, "message")
        if q.startswith("rfc822msgid:"):
            want = "<" + q.split(":", 1)[1] + ">"
            rows = [r for r in rows if r["message_id"] == want]
        elif q.startswith("from:"):
            who = q.split()[0][5:].lower()
            rows = [r for r in rows if who in r.get("from", "").lower()]
        start = int(req.query.get("pageToken", "0"))
        page = rows[start : start + 2]
        body: dict = {
            "messages": [{"id": r["id"], "threadId": r["threadId"]} for r in page],
            "resultSizeEstimate": len(rows),
        }
        if start + 2 < len(rows):
            body["nextPageToken"] = str(start + 2)
        if not page:
            body.pop("messages")
        return 200, body

    @Simulator.route("GET", r"/users/me/messages/([\w-]+)$")
    def get_message(self, conn, connection, req: Request, m):
        r = self.get(conn, connection, "message", m.group(1))
        if not r:
            return 404, self.error_body(404, "Requested entity was not found.")
        headers = [{"name": k, "value": r[k.lower()]} for k in ("Subject", "From", "Date") if r.get(k.lower())]
        return 200, {
            "id": r["id"],
            "threadId": r["threadId"],
            "snippet": r.get("snippet", ""),
            "payload": {"headers": headers},
        }

    @Simulator.route("POST", r"/users/me/messages/send$")
    def send(self, conn, connection, req: Request, m):
        raw = str((req.body or {}).get("raw", ""))
        try:
            parsed = BytesParser(policy=default_policy).parsebytes(
                base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
            )
        except ValueError:
            return 400, self.error_body(400, "Invalid raw message")
        if not parsed["To"]:
            return 400, self.error_body(400, "Invalid To header")
        gid = self.new_id(conn, connection, "message")
        self.put(
            conn,
            connection,
            "message",
            gid,
            {
                "id": gid,
                "threadId": gid,
                "message_id": str(parsed["Message-ID"]),
                "subject": str(parsed["Subject"]),
                "to": str(parsed["To"]),
                "from": "mailbox@standin.example",
                "snippet": parsed.get_body(("plain",)).get_content()[:100] if parsed.get_body(("plain",)) else "",
                "labelIds": ["SENT"],
            },
        )
        return 200, {"id": gid, "threadId": gid, "labelIds": ["SENT"]}


register(Gmail())
