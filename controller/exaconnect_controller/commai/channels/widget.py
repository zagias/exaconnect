"""Website chat (ADR 0018): publishable keys, allowed origins, visitor
sessions, signed-in customers, business hours and attachments.

Visitors are kept apart from the business's known customers:

- An anonymous visitor gets a fresh identity ("visitor:<random>") and a
  short-lived session token signed with a key derived from the widget secret.
  The visitor sees only what was said in that identity. Typing an email
  address into the widget records it on the contact but links nothing and
  shows no history.
- A customer signed in on the business's own site arrives with a token the
  site signed with its widget secret (HS256 JWT: sub, email, name, exp). Only
  then does the widget use the verified identity "user:<sub>" and show that
  customer's past conversations.

Customer-facing reads here only ever touch the messages table, never notes.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

SESSION_TTL_S = 2 * 3600
MAX_FILE_BYTES = 2 * 1024 * 1024
FILE_TYPES = {
    "image/png": [b"\x89PNG\r\n\x1a\n"],
    "image/jpeg": [b"\xff\xd8\xff"],
    "image/gif": [b"GIF87a", b"GIF89a"],
    "image/webp": [b"RIFF"],
    "application/pdf": [b"%PDF-"],
    "text/plain": [],
}
MAX_INSTALL_ORIGINS = 50
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_SETTINGS = {
    "title": "Chat with us",
    "greeting": "Hello. How can we help today?",
    "colour": "#155EEF",
    "position": "right",
    "offline_message": "We're away just now. Leave a message and we'll reply by email.",
    "hours": {},  # {"mon": ["09:00", "17:00"], ...}; empty means always open
    "callbacks": True,
    "attachments": True,
    "ask_contact": "after_first",  # "before" | "after_first" | "never"
    "ai_calls": False,  # visitors may talk to the AI agent (commai/visitor_calls.py)
}


class WidgetError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def new_keys() -> tuple[str, str]:
    return "wk_" + secrets.token_urlsafe(18), "wsk_" + secrets.token_urlsafe(32)


def settings_of(key: dict) -> dict:
    return {**DEFAULT_SETTINGS, **(key.get("settings") or {})}


def check_settings(s: dict) -> dict:
    """Validate the editable widget settings. Raises WidgetError."""
    out = {k: v for k, v in s.items() if k in DEFAULT_SETTINGS}
    if "colour" in out and not re.fullmatch(r"#[0-9A-Fa-f]{6}", str(out["colour"])):
        raise WidgetError("The colour must look like #155EEF.")
    if "position" in out and out["position"] not in ("left", "right"):
        raise WidgetError("Position is left or right.")
    if "ask_contact" in out and out["ask_contact"] not in ("before", "after_first", "never"):
        raise WidgetError("ask_contact is before, after_first or never.")
    if "ai_calls" in out:
        out["ai_calls"] = bool(out["ai_calls"])
    for k in ("title", "greeting", "offline_message"):
        if k in out:
            out[k] = str(out[k])[:300]
    if "hours" in out:
        hours = out["hours"] or {}
        if not isinstance(hours, dict) or set(hours) - set(DAYS):
            raise WidgetError("Hours are given per day: mon, tue ... sun.")
        for day, span in hours.items():
            if span is None:
                continue
            if (
                not isinstance(span, list)
                or len(span) != 2
                or not all(isinstance(t, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t) for t in span)
                or span[0] >= span[1]
            ):
                raise WidgetError(f"Hours for {day} must be two times like 09:00 and 17:00, opening first.")
    return out


def normalise_origin(o: str) -> str:
    o = o.strip().rstrip("/").lower()
    if not re.fullmatch(r"https?://(\*\.)?[a-z0-9.-]+(:\d{1,5})?", o):
        raise WidgetError(f"{o or 'That'} is not an origin. Write it like https://www.example.com.")
    return o


def origin_allowed(key: dict, origin: str) -> bool:
    origin = (origin or "").strip().rstrip("/").lower()
    if not origin:
        return False
    for a in key["allowed_origins"] or []:
        if a == origin:
            return True
        if "://*." in a:
            scheme, rest = a.split("://*.", 1)
            if origin.startswith(scheme + "://") and origin.split("://", 1)[1].endswith("." + rest):
                return True
    return False


def open_now(key: dict, timezone: str, now: dt.datetime | None = None) -> bool:
    hours = settings_of(key)["hours"] or {}
    if not hours:
        return True
    try:
        tz = ZoneInfo(timezone)
    except Exception:  # noqa: BLE001 - a bad zone name falls back to UTC
        tz = ZoneInfo("UTC")
    local = (now or dt.datetime.now(dt.UTC)).astimezone(tz)
    span = hours.get(DAYS[local.weekday()])
    if not span:
        return False
    hm = local.strftime("%H:%M")
    return span[0] <= hm < span[1]


def hours_text(key: dict) -> str:
    hours = settings_of(key)["hours"] or {}
    if not hours:
        return "Open every day"
    parts = []
    for d in DAYS:
        span = hours.get(d)
        parts.append(f"{d.title()} {span[0]}–{span[1]}" if span else f"{d.title()} closed")
    return ", ".join(parts)


# ---- sessions --------------------------------------------------------------------------------


def _session_key(key: dict) -> bytes:
    return hmac.new(key["secret"].encode(), b"exacarib-widget-session", hashlib.sha256).digest()


def issue_session(key: dict, address: str, signed_in: bool, name: str = "") -> dict:
    exp = int(time.time()) + SESSION_TTL_S
    payload = b64(json.dumps({"k": key["public_key"], "a": address, "s": signed_in, "n": name, "exp": exp}).encode())
    sig = b64(hmac.new(_session_key(key), payload.encode(), hashlib.sha256).digest())
    return {"token": f"{payload}.{sig}", "expires_at": exp, "signed_in": signed_in}


def read_session(key: dict, token: str) -> dict:
    """{"a": address, "s": signed_in, "exp"}. Raises WidgetError(401)."""
    try:
        payload, sig = token.split(".", 1)
        want = b64(hmac.new(_session_key(key), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, want):
            raise ValueError
        data = json.loads(unb64(payload))
    except (ValueError, json.JSONDecodeError) as e:
        raise WidgetError("Your chat session is not valid. Reload the page.", 401) from e
    if data.get("k") != key["public_key"]:
        raise WidgetError("Your chat session is for another website.", 401)
    if int(data.get("exp", 0)) < time.time():
        raise WidgetError("Your chat session has expired. Reload the page.", 401)
    return data


def verify_user_token(key: dict, token: str) -> dict:
    """Verify the HS256 JWT the business's site signed with its widget secret.
    Returns its claims (sub, email, name). Raises WidgetError(401)."""
    try:
        h, p, s = token.split(".")
        header = json.loads(unb64(h))
        claims = json.loads(unb64(p))
    except (ValueError, json.JSONDecodeError) as e:
        raise WidgetError("The signed-in customer token is not a valid JWT.", 401) from e
    if header.get("alg") != "HS256":
        raise WidgetError("The signed-in customer token must be signed with HS256.", 401)
    want = b64(hmac.new(key["secret"].encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(s, want):
        raise WidgetError("The signed-in customer token's signature does not match this widget's secret.", 401)
    exp = claims.get("exp")
    if not isinstance(exp, int | float) or exp < time.time():
        raise WidgetError("The signed-in customer token has expired.", 401)
    if exp > time.time() + 24 * 3600:
        raise WidgetError("The signed-in customer token must expire within 24 hours.", 401)
    sub = str(claims.get("sub") or "").strip()
    if not sub or len(sub) > 200:
        raise WidgetError("The signed-in customer token has no sub.", 401)
    return {"sub": sub, "email": str(claims.get("email") or "")[:255], "name": str(claims.get("name") or "")[:200]}


def sign_user_token(secret: str, claims: dict) -> str:
    """What the business's site does (documented for them; used by tests)."""
    h = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    p = b64(json.dumps(claims).encode())
    return f"{h}.{p}." + b64(hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())


# ---- files -------------------------------------------------------------------------------------


def check_file(name: str, content_type: str, data: bytes) -> None:
    if len(data) == 0:
        raise WidgetError("The file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise WidgetError(f"Files can be up to {MAX_FILE_BYTES // (1024 * 1024)} MB.", 413)
    if content_type not in FILE_TYPES:
        raise WidgetError("You can send images (PNG, JPEG, GIF, WebP), PDFs and plain text.", 415)
    magic = FILE_TYPES[content_type]
    if magic and not any(data.startswith(m) for m in magic):
        raise WidgetError("The file's contents don't match its type.", 415)
    if content_type == "text/plain":
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise WidgetError("Plain text files must be UTF-8.", 415) from e
    if not name or len(name) > 120 or "/" in name or "\\" in name:
        raise WidgetError("Give the file a short name.")


def visitor_conversations(conn: psycopg.Connection, key: dict, address: str) -> list[dict]:
    return conn.execute(
        """SELECT c.id, c.state, c.subject, c.created_at, c.last_message_at FROM conversations c
           JOIN contact_identities ci ON ci.id = c.identity_id
           WHERE c.customer_id = %s AND ci.channel = 'web' AND ci.address = %s
           ORDER BY c.created_at DESC LIMIT 50""",
        (key["customer_id"], address),
    ).fetchall()


def own_conversation(conn: psycopg.Connection, key: dict, address: str, conversation_id: Any) -> dict:
    row = conn.execute(
        """SELECT c.* FROM conversations c JOIN contact_identities ci ON ci.id = c.identity_id
           WHERE c.id = %s AND c.customer_id = %s AND ci.channel = 'web' AND ci.address = %s""",
        (conversation_id, key["customer_id"], address),
    ).fetchone()
    if row is None:
        raise WidgetError("Conversation not found.", 404)
    return row


def public_message(m: dict) -> dict:
    """A message as the visitor sees it: no staff emails, no internal fields."""
    who = {"contact": "you", "user": "team", "ai": "assistant", "system": "system", "workflow": "system"}
    return {
        "id": str(m["id"]),
        "from": who.get(m["author_kind"], "system"),
        "body": m["body"],
        "attachments": [
            {k: a.get(k) for k in ("id", "name", "type", "size") if k in a}
            for a in (m["attachments"] or [])
            if isinstance(a, dict) and a.get("id")
        ],
        "status": m["status"] if m["direction"] == "out" else "",
        "at": m["created_at"].isoformat(),
    }
