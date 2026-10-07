"""CloudEvents 1.0 and Standard Webhooks for CommAI's webhooks (ADR 0028).

Outbound, per endpoint:

- ``format = 'exacarib'`` keeps the original body; ``'cloudevents'`` sends a
  CloudEvents 1.0 structured JSON event (``application/cloudevents+json``)
  with ``specversion``, ``id`` (the event id, the same on every attempt),
  ``source`` (``/commai/customers/<id>``), ``type`` (``com.exacarib.commai.<type>``),
  ``subject``, ``time``, ``datacontenttype`` and ``data``.
- Every delivery carries the original ``X-ExaCarib-Signature: v1=<hex>``.
- ``standard_headers`` adds the Standard Webhooks headers ``webhook-id``,
  ``webhook-timestamp`` and ``webhook-signature: v1,<base64 HMAC-SHA256 of
  "<id>.<timestamp>.<body>">``. The key is the endpoint secret's bytes; the
  ``whsec_`` form of the same key (``standard_secret``) works with the
  Standard Webhooks libraries.

Inbound hooks check the same two signatures (see ``inbound.py``).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time

TOLERANCE_S = 300
CE_PREFIX = "com.exacarib.commai."


def key_bytes(secret: str) -> bytes:
    """The HMAC key of a ``whsec_`` secret: its base64 payload decoded, else the text itself."""
    if secret.startswith("whsec_"):
        raw = secret[6:]
        for decode in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                return decode(raw + "=" * (-len(raw) % 4))
            except (binascii.Error, ValueError):
                continue
    return secret.encode()


def standard_secret(secret: str) -> str:
    """The same key in the Standard Webhooks ``whsec_<base64>`` form."""
    return "whsec_" + base64.b64encode(key_bytes(secret)).decode()


def sign_standard(secret: str, msg_id: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(key_bytes(secret), f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


def verify_standard(secret: str, headers: dict, body: bytes, now: float | None = None) -> bool:
    h = {k.lower(): v for k, v in headers.items()}
    msg_id, ts, sigs = h.get("webhook-id", ""), h.get("webhook-timestamp", ""), h.get("webhook-signature", "")
    if not (msg_id and ts and sigs):
        return False
    try:
        t = int(ts)
    except ValueError:
        return False
    if abs((now or time.time()) - t) > TOLERANCE_S:
        return False
    want = sign_standard(secret, msg_id, t, body)
    return any(hmac.compare_digest(want, s) for s in sigs.split() if s.startswith("v1,"))


def sign_v1(secret: str, timestamp: int, body: bytes) -> str:
    return "v1=" + hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def verify_v1(secret: str, headers: dict, body: bytes, now: float | None = None) -> bool:
    h = {k.lower(): v for k, v in headers.items()}
    ts, sig = h.get("x-exacarib-timestamp", ""), h.get("x-exacarib-signature", "")
    try:
        t = int(ts)
    except ValueError:
        return False
    if abs((now or time.time()) - t) > TOLERANCE_S:
        return False
    return hmac.compare_digest(sign_v1(secret, t, body), sig)


def cloudevent(event: dict) -> dict:
    return {
        "specversion": "1.0",
        "id": str(event["id"]),
        "source": f"/commai/customers/{event['customer_id']}",
        "type": CE_PREFIX + event["type"],
        "subject": str(event.get("subject") or ""),
        "time": event["at"].isoformat(),
        "datacontenttype": "application/json",
        "data": event["data"],
    }


def render(endpoint: dict, event: dict, body_v1: bytes, timestamp: int) -> tuple[bytes, dict]:
    """The body and headers of one delivery to ``endpoint`` (its format and options)."""
    fmt = endpoint.get("format") or "exacarib"
    if fmt == "cloudevents":
        body = json.dumps(cloudevent(event), separators=(",", ":"), default=str).encode()
        ctype = "application/cloudevents+json; charset=utf-8"
    else:
        body, ctype = body_v1, "application/json"
    headers = {
        "Content-Type": ctype,
        "User-Agent": "ExaCarib-Webhooks/1",
        "X-ExaCarib-Event-Id": str(event["id"]),
        "X-ExaCarib-Event-Type": event["type"],
        "X-ExaCarib-Timestamp": str(timestamp),
        "X-ExaCarib-Signature": sign_v1(endpoint["secret"], timestamp, body),
    }
    if endpoint.get("standard_headers"):
        headers["webhook-id"] = f"msg_{event['id']}"
        headers["webhook-timestamp"] = str(timestamp)
        headers["webhook-signature"] = sign_standard(endpoint["secret"], f"msg_{event['id']}", timestamp, body)
    return body, headers


def parse_cloudevent(headers: dict, body: bytes) -> dict | None:
    """A CloudEvent from an inbound request, structured or binary mode; None if it isn't one."""
    h = {k.lower(): v for k, v in headers.items()}
    ctype = h.get("content-type", "")
    if "application/cloudevents+json" in ctype:
        try:
            ev = json.loads(body or b"{}")
        except ValueError:
            return None
        if isinstance(ev, dict) and ev.get("specversion") == "1.0" and ev.get("id") and ev.get("type"):
            return ev
        return None
    if h.get("ce-specversion") == "1.0" and h.get("ce-id") and h.get("ce-type"):
        try:
            data = json.loads(body) if body and "json" in ctype else (body or b"").decode("utf-8", "replace")
        except ValueError:
            data = (body or b"").decode("utf-8", "replace")
        return {
            "specversion": "1.0",
            "id": h["ce-id"],
            "type": h["ce-type"],
            "source": h.get("ce-source", ""),
            "subject": h.get("ce-subject", ""),
            "time": h.get("ce-time", ""),
            "data": data,
        }
    return None
