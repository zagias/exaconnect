"""CloudEvents 1.0 over HTTP, signed the Standard Webhooks way.

CloudEvents (https://github.com/cloudevents/spec, v1.0.2) in both HTTP modes:

- structured: the whole event is the body, ``Content-Type:
  application/cloudevents+json``;
- binary: the event's ``data`` is the body (``application/json``) and the
  other attributes travel as ``ce-*`` headers.

Standard Webhooks (https://www.standardwebhooks.com) signs every delivery:

- ``webhook-id``: the event id, the same on every retry, so receivers de-duplicate on it;
- ``webhook-timestamp``: unix seconds of this attempt;
- ``webhook-signature``: ``v1,<base64 HMAC-SHA256 of "<id>.<timestamp>.<body>">``,
  keyed with the base64 bytes after the ``whsec_`` prefix of the secret.

Receivers should reject timestamps more than five minutes from their clock.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

SPEC_VERSION = "1.0"
TOLERANCE_S = 300
SECRET_PREFIX = "whsec_"


def new_secret() -> str:
    return SECRET_PREFIX + base64.b64encode(secrets.token_bytes(24)).decode()


def _key(secret: str) -> bytes:
    raw = secret.removeprefix(SECRET_PREFIX)
    try:
        return base64.b64decode(raw, validate=True)
    except ValueError:
        # A secret that is not base64 (pasted from elsewhere): use its bytes.
        return raw.encode()


def sign(secret: str, msg_id: str, timestamp: int, body: bytes) -> str:
    to_sign = f"{msg_id}.{timestamp}.".encode() + body
    mac = hmac.new(_key(secret), to_sign, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


def verify(secret: str, headers: dict[str, str], body: bytes, now: float | None = None) -> bool:
    """What a receiver does (the SDK and the tests share it). Header names are case-insensitive."""
    h = {k.lower(): v for k, v in headers.items()}
    msg_id, ts, sigs = h.get("webhook-id", ""), h.get("webhook-timestamp", ""), h.get("webhook-signature", "")
    try:
        t = int(ts)
    except ValueError:
        return False
    if not msg_id or abs((now if now is not None else time.time()) - t) > TOLERANCE_S:
        return False
    want = sign(secret, msg_id, t, body)
    # Several signatures may be sent, space separated, while a secret rotates.
    return any(hmac.compare_digest(want, s) for s in sigs.split())


def iso(t: Any) -> str:
    if isinstance(t, dt.datetime):
        return t.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
    return str(t)


def event(ev: dict) -> dict[str, Any]:
    """The CloudEvent for a connect_events row."""
    out: dict[str, Any] = {
        "specversion": SPEC_VERSION,
        "id": ev["id"],
        "source": ev["source"],
        "type": ev["type"],
        "time": iso(ev["time"]),
        "datacontenttype": "application/json",
        "data": ev["data"],
    }
    if ev.get("subject"):
        out["subject"] = ev["subject"]
    # Extension attributes (lower-case alphanumerics only, per the spec).
    out["severity"] = ev.get("severity", "info")
    out["action"] = ev.get("action", "notify")
    if ev.get("dedup_key"):
        out["dedupkey"] = ev["dedup_key"]
    if ev.get("customer_id"):
        out["organisationid"] = str(ev["customer_id"])
    return out


def dumps(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), default=str, sort_keys=False).encode()


def structured(ce: dict) -> tuple[dict[str, str], bytes]:
    return {"Content-Type": "application/cloudevents+json; charset=utf-8"}, dumps(ce)


def binary(ce: dict) -> tuple[dict[str, str], bytes]:
    headers = {"Content-Type": ce.get("datacontenttype", "application/json")}
    for k, v in ce.items():
        if k in ("data", "datacontenttype"):
            continue
        headers[f"ce-{k}"] = str(v)
    return headers, dumps(ce.get("data"))


def from_binary(headers: dict[str, str], body: bytes) -> dict[str, Any]:
    """Rebuild an event from binary mode (what a receiver does)."""
    out: dict[str, Any] = {}
    for k, v in headers.items():
        if k.lower().startswith("ce-"):
            out[k.lower()[3:]] = v
    out["datacontenttype"] = headers.get("Content-Type") or headers.get("content-type") or "application/json"
    out["data"] = json.loads(body) if body else None
    return out


def signed(secret: str, msg_id: str, headers: dict[str, str], body: bytes, now: float | None = None) -> dict:
    ts = int(now if now is not None else time.time())
    return {
        **headers,
        "webhook-id": msg_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": sign(secret, msg_id, ts, body),
    }
