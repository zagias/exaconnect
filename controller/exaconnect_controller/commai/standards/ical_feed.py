"""The iCalendar feed of a business's bookings, and .ics invites (ADR 0028).

- Feed: a secret address (``/api/v1/commai/ical/{token}.ics``) any calendar
  app can subscribe to (Google, Outlook, Apple). Only the token's SHA-256 is
  stored; making a new one stops the old address working.
- Invites: each confirmed booking has a signed link to an iTIP REQUEST (or
  CANCEL once cancelled) ``.ics``, which confirmations can carry. The
  signature is an HMAC with the business's own key, so links can't be
  guessed; making a new feed address keeps links already sent working.

Bookings are the actions that booked a time (a create action with a start)
and succeeded outside test mode; a later cancel of the same booking id marks
the event CANCELLED.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import secrets
from typing import Any

import psycopg

from . import ical

DEFAULT_MINUTES = 30
DOMAIN = "connect.exacarib"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _base() -> str:
    return os.environ.get("EXA_PUBLIC_URL", "").rstrip("/")


def get(conn: psycopg.Connection, customer_id: Any) -> dict | None:
    return conn.execute("SELECT * FROM commai_ical_feeds WHERE customer_id = %s", (customer_id,)).fetchone()


def _ensure_key(conn: psycopg.Connection, customer_id: Any) -> str:
    row = get(conn, customer_id)
    if row:
        return row["sign_key"]
    # A feed row with an unusable token: invite links work, the feed is not published yet.
    conn.execute(
        """INSERT INTO commai_ical_feeds (customer_id, token_hash, sign_key, created_by)
           VALUES (%s, %s, %s, 'system') ON CONFLICT (customer_id) DO NOTHING""",
        (customer_id, "unpublished:" + secrets.token_hex(16), secrets.token_urlsafe(32)),
    )
    return get(conn, customer_id)["sign_key"]


def publish(conn: psycopg.Connection, customer_id: Any, actor: str) -> str:
    """A new feed token (shown once). Any earlier address stops working; the invite
    signing key is kept, so invite links already sent keep working."""
    key = _ensure_key(conn, customer_id)
    token = "icf_" + secrets.token_urlsafe(24)
    conn.execute(
        "UPDATE commai_ical_feeds SET token_hash = %s, sign_key = %s, created_by = %s, created_at = now()"
        " WHERE customer_id = %s",
        (_hash(token), key, actor, customer_id),
    )
    return token


def unpublish(conn: psycopg.Connection, customer_id: Any) -> None:
    conn.execute(
        "UPDATE commai_ical_feeds SET token_hash = %s WHERE customer_id = %s",
        ("unpublished:" + secrets.token_hex(16), customer_id),
    )


def published(row: dict | None) -> bool:
    return bool(row) and not row["token_hash"].startswith("unpublished:")


# ---- bookings as events ------------------------------------------------------------------


def _start(run: dict) -> dt.datetime | None:
    raw = (run["result"] or {}).get("start") or (run["inputs"] or {}).get("start")
    try:
        t = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.UTC)


def _event(run: dict, cancelled: set[str], business: str) -> ical.Event | None:
    start = _start(run)
    if start is None:
        return None
    res, inp = run["result"] or {}, run["inputs"] or {}
    end_raw = res.get("end")
    try:
        end = dt.datetime.fromisoformat(str(end_raw).replace("Z", "+00:00")) if end_raw else None
    except ValueError:
        end = None
    end = end or start + dt.timedelta(minutes=int(inp.get("duration_minutes") or DEFAULT_MINUTES))
    contact = str(inp.get("contact") or inp.get("email") or "")
    name = str(inp.get("name") or "")
    bid = str(res.get("booking_id") or res.get("event_id") or "")
    is_cancelled = bool(bid) and bid in cancelled
    return ical.Event(
        uid=f"{run['id']}@{DOMAIN}",
        start=start,
        end=end,
        summary=f"{inp.get('reason') or 'Appointment'}: {name}".strip(": ") if name else "Appointment",
        description=f"Booked with {business} through ExaCarib Connect." + (f"\nContact: {contact}" if contact else ""),
        status="CANCELLED" if is_cancelled else "CONFIRMED",
        attendees=[(contact, name)] if "@" in contact else [],
        sequence=1 if is_cancelled else 0,
    )


def _runs(conn: psycopg.Connection, customer_id: Any, run_id: Any = None) -> tuple[list[dict], set[str]]:
    runs = conn.execute(
        """SELECT * FROM action_runs WHERE customer_id = %s AND status = 'succeeded' AND NOT test
             AND inputs ? 'start' AND NOT (result ? 'dry_run')
             AND (%s::uuid IS NULL OR id = %s::uuid)
             AND created_at > now() - interval '400 days'
           ORDER BY created_at LIMIT 5000""",
        (customer_id, run_id, run_id),
    ).fetchall()
    cancelled = {
        str(r["bid"])
        for r in conn.execute(
            """SELECT inputs->>'booking_id' AS bid FROM action_runs
               WHERE customer_id = %s AND status = 'succeeded' AND NOT test AND inputs ? 'booking_id'
                 AND action IN ('cancel', 'cancel_booking', 'cancel_event')""",
            (customer_id,),
        ).fetchall()
    }
    return runs, cancelled


def _business(conn, customer_id: Any) -> str:
    row = conn.execute("SELECT name FROM customers WHERE id = %s", (customer_id,)).fetchone()
    return row["name"] if row else "the business"


def feed(conn: psycopg.Connection, token: str) -> str | None:
    row = conn.execute("SELECT * FROM commai_ical_feeds WHERE token_hash = %s", (_hash(token),)).fetchone()
    if row is None:
        return None
    cid = row["customer_id"]
    runs, cancelled = _runs(conn, cid)
    business = _business(conn, cid)
    events = [e for e in (_event(r, cancelled, business) for r in runs) if e]
    return ical.calendar(events, name=f"{business} bookings")


def _sig(key: str, run_id: Any) -> str:
    return hmac.new(key.encode(), f"invite:{run_id}".encode(), hashlib.sha256).hexdigest()[:32]


def invite_url(conn: psycopg.Connection, customer_id: Any, run_id: Any) -> str:
    key = _ensure_key(conn, customer_id)
    path = f"/api/v1/commai/ical/invites/{run_id}.ics?sig={_sig(key, run_id)}"
    return (_base() + path) if _base() else path


def invite_url_for_key(conn: psycopg.Connection, customer_id: Any, idempotency_key: str) -> str:
    """The invite link of the action run with this idempotency key ('' if none)."""
    row = conn.execute(
        "SELECT id FROM action_runs WHERE customer_id = %s AND idempotency_key = %s",
        (customer_id, idempotency_key),
    ).fetchone()
    return invite_url(conn, customer_id, row["id"]) if row else ""


def invite(conn: psycopg.Connection, run_id: str, sig: str) -> str | None:
    """The .ics for one booking (REQUEST, or CANCEL once cancelled), if the link is genuine."""
    run = conn.execute("SELECT customer_id FROM action_runs WHERE id::text = %s", (run_id,)).fetchone()
    if run is None:
        return None
    feed_row = get(conn, run["customer_id"])
    if feed_row is None or not hmac.compare_digest(_sig(feed_row["sign_key"], run_id), sig or ""):
        return None
    runs, cancelled = _runs(conn, run["customer_id"], run_id)
    if not runs:
        return None
    e = _event(runs[0], cancelled, _business(conn, run["customer_id"]))
    if e is None:
        return None
    return ical.calendar([e], method="CANCEL" if e.status == "CANCELLED" else "REQUEST")
