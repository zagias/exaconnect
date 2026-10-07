"""Inbound webhooks (ADR 0034).

**Generic hooks** let any system (Zapier, Make, n8n, a business's own code)
start Jibsy workflows. Each hook has its own address
(``/api/v1/commai/hooks/<token>``) and secret. A delivery must be signed one
of two ways, the same ways Jibsy signs its own webhooks:

- Standard Webhooks: ``webhook-id``, ``webhook-timestamp``,
  ``webhook-signature: v1,<base64 HMAC-SHA256 of "<id>.<timestamp>.<body>">``
  (the key is the ``whsec_`` secret's base64 payload);
- ExaCarib v1: ``X-ExaCarib-Timestamp`` and
  ``X-ExaCarib-Signature: v1=<hex HMAC-SHA256 of "<timestamp>.<body>">``.

Timestamps more than five minutes off are refused. The body may be a
CloudEvent (structured or binary mode) or any JSON object. Each accepted
delivery is recorded once per delivery id (webhook-id, ce-id or
X-ExaCarib-Event-Id) and becomes an ``inbound_webhook.received`` event, which
workflows can use as their trigger. Nothing in the body is ever run.

**App hooks** carry an app's own change notifications (Zoho, Pipedrive,
Microsoft Graph, Calendly, Gmail...), at ``/api/v1/commai/integration-hooks/<token>``.
The app's connector checks the app's own signature (``verify_webhook``) and
reads its events, recorded as ``integration.event``.
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
from typing import Any

import psycopg

from .. import connectors, events
from . import webhooks_std

MAX_BODY = 256_000

events.register("inbound_webhook.received", "integration.event")
log = logging.getLogger(__name__)


class HookError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def new_secret() -> str:
    return "whsec_" + base64.b64encode(secrets.token_bytes(32)).decode()


def create(conn: psycopg.Connection, customer_id: Any, name: str, event_type: str, actor: str) -> dict:
    n = conn.execute(
        "SELECT count(*) AS n FROM commai_inbound_hooks WHERE customer_id = %s AND kind = 'generic'", (customer_id,)
    ).fetchone()["n"]
    if n >= 20:
        raise HookError("A business can have up to 20 inbound webhooks.", 400)
    return conn.execute(
        """INSERT INTO commai_inbound_hooks (customer_id, kind, name, event_type, token, secret, created_by)
           VALUES (%s, 'generic', %s, %s, %s, %s, %s) RETURNING *""",
        (customer_id, name, event_type, "ih_" + secrets.token_urlsafe(24), new_secret(), actor),
    ).fetchone()


def app_hook(conn: psycopg.Connection, customer_id: Any, app: str, actor: str = "") -> dict:
    """The (one) hook address an app sends its notifications to, made on first use."""
    row = conn.execute(
        "SELECT * FROM commai_inbound_hooks WHERE customer_id = %s AND kind = 'app' AND app = %s", (customer_id, app)
    ).fetchone()
    if row:
        return row
    return conn.execute(
        """INSERT INTO commai_inbound_hooks (customer_id, kind, app, name, token, secret, created_by)
           VALUES (%s, 'app', %s, %s, %s, %s, %s) RETURNING *""",
        (customer_id, app, app, "ah_" + secrets.token_urlsafe(24), new_secret(), actor),
    ).fetchone()


def public(row: dict, base: str = "", secret: bool = False) -> dict:
    out = {
        k: row[k]
        for k in (
            "id",
            "kind",
            "app",
            "name",
            "event_type",
            "active",
            "received_count",
            "rejected_count",
            "last_received_at",
            "last_rejected_at",
            "last_error",
            "created_at",
        )
    }
    path = "integration-hooks" if row["kind"] == "app" else "hooks"
    out["url"] = f"{base}/api/v1/commai/{path}/{row['token']}"
    out["secret_hint"] = "…" + row["secret"][-4:]
    if secret:
        out["secret"] = row["secret"]
    return out


def _reject(conn, hook: dict, why: str, code: int = 401) -> HookError:
    conn.execute(
        """UPDATE commai_inbound_hooks SET rejected_count = rejected_count + 1, last_rejected_at = now(),
                  last_error = %s WHERE id = %s""",
        (why, hook["id"]),
    )
    return HookError(why, code)


def _once(conn, hook: dict, delivery_id: str) -> bool:
    row = conn.execute(
        "INSERT INTO commai_inbound_receipts (hook_id, delivery_id) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING 1",
        (hook["id"], delivery_id[:200]),
    ).fetchone()
    return row is not None


def _accepted(conn, hook: dict) -> None:
    conn.execute(
        "UPDATE commai_inbound_hooks SET received_count = received_count + 1, last_received_at = now(), last_error = ''"
        " WHERE id = %s",
        (hook["id"],),
    )


def receive(conn: psycopg.Connection, token: str, headers: dict, body: bytes, query: dict) -> tuple[int, str, str]:
    """Handle one delivery. Returns (status, content type, body) for the sender.
    Raises HookError (and records the rejection) when it is refused."""
    hook = conn.execute("SELECT * FROM commai_inbound_hooks WHERE token = %s FOR UPDATE", (token,)).fetchone()
    if hook is None or not hook["active"]:
        raise HookError("No such webhook.", 404)
    if len(body or b"") > MAX_BODY:
        raise _reject(conn, hook, "The delivery is too large.", 413)
    if hook["kind"] == "app":
        return _receive_app(conn, hook, headers, body, query)
    h = {k.lower(): v for k, v in headers.items()}
    if "webhook-signature" in h:
        ok, delivery = webhooks_std.verify_standard(hook["secret"], headers, body), h.get("webhook-id", "")
    elif "x-exacarib-signature" in h:
        ok, delivery = webhooks_std.verify_v1(hook["secret"], headers, body), h.get("x-exacarib-event-id", "")
    else:
        raise _reject(conn, hook, "The delivery is not signed.")
    if not ok:
        raise _reject(conn, hook, "The signature does not match, or the timestamp is more than five minutes off.")
    ce = webhooks_std.parse_cloudevent(headers, body)
    if ce is not None:
        delivery = delivery or f"ce:{ce.get('source', '')}:{ce['id']}"
        data, etype, source = ce.get("data"), str(ce["type"]), str(ce.get("source", ""))
    else:
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            raise _reject(conn, hook, "The body is not JSON.", 422) from None
        etype, source = hook["event_type"] or "webhook", ""
    if not delivery:
        raise _reject(conn, hook, "The delivery has no id (webhook-id, ce-id or X-ExaCarib-Event-Id).", 422)
    # The v1 signature covers the timestamp and body but not the event id
    # header, so the same signed request sent again under another id is a
    # replay: each v1 signature is accepted once.
    v1_replay = "webhook-signature" not in h and not _once(conn, hook, "v1sig:" + h["x-exacarib-signature"][:190])
    if v1_replay or not _once(conn, hook, delivery):
        return 200, "application/json", json.dumps({"ok": True, "duplicate": True})
    event_id = events.emit(
        conn,
        hook["customer_id"],
        "inbound_webhook.received",
        {
            "hook_id": str(hook["id"]),
            "hook": hook["name"],
            "type": etype[:200],
            "source": source[:200],
            "delivery_id": delivery[:200],
            "data": data if isinstance(data, dict | list) else {"value": data},
        },
        hook["id"],
    )
    conn.execute(
        "UPDATE commai_inbound_receipts SET event_id = %s WHERE hook_id = %s AND delivery_id = %s",
        (event_id, hook["id"], delivery[:200]),
    )
    _accepted(conn, hook)
    return 202, "application/json", json.dumps({"ok": True, "event_id": event_id})


def _on_event(conn, c, connection: dict, event: dict) -> None:
    """Let the connector act on one verified, first-time event (for example, sync a
    ticket). Its failure is logged and never loses the recorded event."""
    fn = getattr(c, "on_webhook_event", None)
    if fn is None:
        return
    try:
        with conn.transaction():
            fn(conn, connection, event)
    except Exception as e:  # noqa: BLE001 - a connector's follow-up must not refuse the delivery
        log.warning("%s on_webhook_event failed: %s", c.app, type(e).__name__)


def _receive_app(conn, hook: dict, headers: dict, body: bytes, query: dict) -> tuple[int, str, str]:
    try:
        c = connectors.get(hook["app"])
    except KeyError:
        raise HookError("No such webhook.", 404) from None
    handshake = c.webhook_handshake(headers, body, query) if hasattr(c, "webhook_handshake") else None
    if handshake is not None:
        return handshake
    connection = connectors.connection(conn, hook["customer_id"], hook["app"])
    if connection is None:
        raise _reject(conn, hook, "The app is not connected.", 404)
    if not (hasattr(c, "verify_webhook") and c.verify_webhook(conn, connection, hook, headers, body, query)):
        raise _reject(conn, hook, f"{c.label}'s signature does not match.")
    try:
        items = c.webhook_events(body, headers)
    except (ValueError, KeyError, TypeError):
        raise _reject(conn, hook, "The delivery could not be read.", 422) from None
    ids = []
    for it in items:
        if not _once(conn, hook, f"{hook['app']}:{it['id']}"):
            continue
        ids.append(
            events.emit(
                conn,
                hook["customer_id"],
                "integration.event",
                {"app": hook["app"], "type": it["type"], "id": str(it["id"])[:200], "data": it.get("data") or {}},
                hook["app"],
            )
        )
        _on_event(conn, c, connection, it)
    _accepted(conn, hook)
    return 200, "application/json", json.dumps({"ok": True, "events": len(ids)})
