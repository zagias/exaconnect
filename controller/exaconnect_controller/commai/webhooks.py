"""Signed, retried, de-duplicated webhooks (ADR 0016).

Each delivery is a POST of {"id", "type", "created_at", "customer_id", "data"}
with these headers:

  X-ExaCarib-Event-Id   the event id; the same event always has the same id,
                        so a receiver de-duplicates on it
  X-ExaCarib-Timestamp  unix seconds when this attempt was signed
  X-ExaCarib-Signature  v1=<hex HMAC-SHA256 of "<timestamp>.<body>" with the
                        endpoint's secret>

Receivers should reject timestamps more than five minutes old. A delivery
that fails is retried with backoff (up to six attempts) and every attempt is
logged.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

import psycopg

from . import jobs

TOLERANCE_S = 300


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"v1={mac}"


def verify(secret: str, timestamp: str, body: bytes, signature: str, now: float | None = None) -> bool:
    """What a receiver does. Kept here so the SDK and tests share it."""
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((now or time.time()) - ts) > TOLERANCE_S:
        return False
    return hmac.compare_digest(sign(secret, ts, body), signature)


class UnsafeURL(ValueError):
    pass


def check_url(url: str) -> None:
    """Webhooks go to public HTTPS addresses only, so a business cannot point
    the controller at its own private network. EXA_WEBHOOK_ALLOW_PRIVATE=1
    lifts this for the lab."""
    p = urllib.parse.urlparse(url)
    allow_private = os.environ.get("EXA_WEBHOOK_ALLOW_PRIVATE") == "1"
    if p.scheme != "https" and not (allow_private and p.scheme == "http"):
        raise UnsafeURL("Webhook addresses must start with https://.")
    if not p.hostname:
        raise UnsafeURL("That address has no host name.")
    if allow_private:
        return
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnsafeURL(f"{p.hostname} does not resolve.") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise UnsafeURL(f"{p.hostname} points to a private address.")


def _post(url: str, body: bytes, headers: dict[str, str], timeout_s: float = 10) -> int:
    check_url(url)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:  # noqa: S310 - checked above
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


# Tests replace this to capture deliveries.
sender = _post


def payload(event: dict) -> bytes:
    return json.dumps(
        {
            "id": str(event["id"]),
            "type": event["type"],
            "created_at": event["at"].isoformat(),
            "customer_id": str(event["customer_id"]),
            "subject": event["subject"],
            "data": event["data"],
        },
        separators=(",", ":"),
        default=str,
    ).encode()


@jobs.handler("webhook.fanout")
def _fanout(conn: psycopg.Connection, job: dict) -> None:
    event = conn.execute("SELECT * FROM commai_events WHERE id = %s", (job["payload"]["event_id"],)).fetchone()
    if event is None:
        return
    endpoints = conn.execute(
        """SELECT id FROM webhook_endpoints WHERE customer_id = %s AND active
           AND ('*' = ANY(events) OR %s = ANY(events) OR split_part(%s, '.', 1) || '.*' = ANY(events))""",
        (event["customer_id"], event["type"], event["type"]),
    ).fetchall()
    for ep in endpoints:
        row = conn.execute(
            """INSERT INTO webhook_deliveries (endpoint_id, customer_id, event_id, event_type)
               VALUES (%s, %s, %s, %s) ON CONFLICT (endpoint_id, event_id) DO NOTHING RETURNING id""",
            (ep["id"], event["customer_id"], event["id"], event["type"]),
        ).fetchone()
        if row:
            jobs.enqueue(
                conn,
                "webhook.deliver",
                {"delivery_id": row["id"]},
                customer_id=event["customer_id"],
                dedupe_key=f"deliver:{row['id']}",
            )


@jobs.handler("webhook.deliver")
def _deliver(conn: psycopg.Connection, job: dict) -> None:
    d = conn.execute(
        """SELECT d.*, e.url, e.secret, e.active, e.format, e.standard_headers FROM webhook_deliveries d
           JOIN webhook_endpoints e ON e.id = d.endpoint_id WHERE d.id = %s FOR UPDATE OF d""",
        (job["payload"]["delivery_id"],),
    ).fetchone()
    if d is None or d["status"] == "delivered" or not d["active"]:
        return
    event = conn.execute("SELECT * FROM commai_events WHERE id = %s", (d["event_id"],)).fetchone()
    from .standards import webhooks_std

    ts = int(time.time())
    # ADR 0034: the endpoint's format (ExaCarib v1 or CloudEvents) and, when
    # asked, Standard Webhooks headers. The v1 signature is always there.
    body, headers = webhooks_std.render(d, event, payload(event), ts)
    try:
        code = sender(d["url"], body, headers)
        error = "" if 200 <= code < 300 else f"answered {code}"
    except Exception as e:  # noqa: BLE001 - recorded on the delivery
        code, error = None, f"{type(e).__name__}: {e}"[:500]
    conn.execute(
        """UPDATE webhook_deliveries SET attempts = attempts + 1, response_code = %s, last_error = %s,
                  status = CASE WHEN %s = '' THEN 'delivered' ELSE status END,
                  delivered_at = CASE WHEN %s = '' THEN now() ELSE delivered_at END
           WHERE id = %s""",
        (code, error, error, error, d["id"]),
    )
    if error:
        if job["attempts"] >= job["max_attempts"]:
            conn.execute("UPDATE webhook_deliveries SET status = 'failed' WHERE id = %s", (d["id"],))
            return None
        # Keep the attempt record, then come back later.
        return jobs.Later(error, delay_s=30)
    return None


@jobs.on_dead("webhook.deliver")
def _dead(job: dict, error: str) -> None:
    from .. import db

    with db.tx() as conn:
        conn.execute(
            "UPDATE webhook_deliveries SET status = 'failed', last_error = %s WHERE id = %s AND status <> 'delivered'",
            (error[:500], job["payload"]["delivery_id"]),
        )
