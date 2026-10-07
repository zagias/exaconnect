"""My settings: a member of staff's own CommAI settings (ADR 0037).

Everything here acts on one person: the user id comes from the signed-in
session (or key), never from the request, so nobody can change another
person's profile, notifications, availability or sessions. Calls reuse the
voice self-service (ADR 0021), which is "own" by construction too.

Availability feeds routing: only 'online' people are picked for new work
(commai_members.available, read by inbox.route).
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from .. import events, inbox
from ..ai import language

events.register("member.availability_changed")

KINDS = ("assignment", "mention", "sla_warning")
WAYS = ("in_app", "email")
AVAILABILITY = ("online", "away", "offline")
DEFAULT_NOTIFY = {
    "assignment": {"in_app": True, "email": False},
    "mention": {"in_app": True, "email": True},
    "sla_warning": {"in_app": True, "email": False},
}
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
SLA_WARN_MINUTES = 15


class SelfServiceError(inbox.InboxError):
    """A self-service request refused; `code` maps to an HTTP status."""


def _member(conn: psycopg.Connection, customer_id: Any, user_id: Any) -> None:
    conn.execute(
        "INSERT INTO commai_members (customer_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (customer_id, user_id),
    )


def prefs(conn: psycopg.Connection, customer_id: Any, user_id: Any) -> dict:
    """The person's preferences (created with defaults on first read)."""
    conn.execute(
        "INSERT INTO ss_staff_prefs (customer_id, user_id, notify) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
        (customer_id, user_id, Jsonb(DEFAULT_NOTIFY)),
    )
    row = conn.execute(
        "SELECT * FROM ss_staff_prefs WHERE customer_id = %s AND user_id = %s", (customer_id, user_id)
    ).fetchone()
    row["notify"] = {k: {**DEFAULT_NOTIFY[k], **(row["notify"] or {}).get(k, {})} for k in KINDS}
    return row


def profile(conn: psycopg.Connection, customer_id: Any, user_id: Any) -> dict:
    u = conn.execute(
        """SELECT u.id, u.email, u.display_name, u.given_name, u.family_name, u.provisioned_by,
                  u.totp_enabled_at IS NOT NULL AS two_step,
                  COALESCE(m.seat, 'agent') AS seat, COALESCE(m.languages, '{en}') AS languages
           FROM users u LEFT JOIN commai_members m ON m.user_id = u.id AND m.customer_id = %s
           WHERE u.id = %s""",
        (customer_id, user_id),
    ).fetchone()
    p = prefs(conn, customer_id, user_id)
    s = inbox.settings(conn, customer_id)
    return {
        "profile": {
            "email": u["email"],
            "display_name": u["display_name"],
            "given_name": u["given_name"],
            "family_name": u["family_name"],
            # Names from the company directory are changed there, not here.
            "managed_by_directory": u["provisioned_by"] == "scim",
            "seat": u["seat"],
            "two_step": bool(u["two_step"]),
        },
        "language": p["language"],
        "languages_spoken": list(u["languages"]),
        "language_choices": [{"code": c, "name": n} for c, n in language.NAMES.items()],
        "availability": p["availability"],
        "notify": p["notify"],
        "quiet_hours": {"start": p["quiet_start"], "end": p["quiet_end"]},
        "timezone": p["timezone"] or s["timezone"],
    }


def update(conn: psycopg.Connection, customer_id: Any, user_id: Any, changes: dict) -> dict:
    """Change the person's own settings. `changes` holds only fields they may set."""
    prefs(conn, customer_id, user_id)
    _member(conn, customer_id, user_id)
    vals: dict = {}
    if "language" in changes:
        if changes["language"] not in language.NAMES:
            raise SelfServiceError("Choose one of the listed languages.", 422)
        vals["language"] = changes["language"]
    if "notify" in changes:
        n = changes["notify"] or {}
        if set(n) - set(KINDS) or any(set(v or {}) - set(WAYS) for v in n.values()):
            raise SelfServiceError("Notifications are assignment, mention and sla_warning; in_app or email.", 422)
        cur = prefs(conn, customer_id, user_id)["notify"]
        vals["notify"] = Jsonb({k: {**cur[k], **{w: bool(x) for w, x in (n.get(k) or {}).items()}} for k in KINDS})
    if "quiet_hours" in changes:
        q = changes["quiet_hours"] or {}
        start, end = str(q.get("start") or ""), str(q.get("end") or "")
        if (start or end) and not (HHMM.match(start) and HHMM.match(end) and start != end):
            raise SelfServiceError("Quiet hours are two different times like 22:00 and 07:00, or none.", 422)
        vals["quiet_start"], vals["quiet_end"] = start, end
    if "timezone" in changes:
        tz = str(changes["timezone"] or "")
        if tz:
            try:
                ZoneInfo(tz)
            except Exception as e:  # noqa: BLE001
                raise SelfServiceError("That time zone isn't known. Use a name like America/Port_of_Spain.", 422) from e
        vals["timezone"] = tz
    if "availability" in changes:
        vals["availability"] = set_availability(conn, customer_id, user_id, changes["availability"])
    if vals:
        sets = ", ".join(f"{k} = %({k})s" for k in vals)
        conn.execute(
            f"UPDATE ss_staff_prefs SET {sets}, updated_at = now() WHERE customer_id = %(c)s AND user_id = %(u)s",
            {**vals, "c": customer_id, "u": user_id},
        )
    names = {k: str(changes[k]).strip()[:200] for k in ("display_name", "given_name", "family_name") if k in changes}
    if "languages_spoken" in changes:
        spoken = [c for c in changes["languages_spoken"] or [] if c in language.NAMES]
        if not spoken:
            raise SelfServiceError("Choose at least one language you answer customers in.", 422)
        conn.execute(
            "UPDATE commai_members SET languages = %s WHERE customer_id = %s AND user_id = %s",
            (spoken, customer_id, user_id),
        )
    if names:
        u = conn.execute("SELECT provisioned_by FROM users WHERE id = %s", (user_id,)).fetchone()
        if u["provisioned_by"] == "scim":
            raise SelfServiceError("Your name comes from your company directory. Change it there.", 409)
        sets = ", ".join(f"{k} = %({k})s" for k in names)
        conn.execute(f"UPDATE users SET {sets}, updated_at = now() WHERE id = %(u)s", {**names, "u": user_id})
    return profile(conn, customer_id, user_id)


def set_availability(conn: psycopg.Connection, customer_id: Any, user_id: Any, value: str) -> str:
    """online, away or offline. Only online people get new conversations."""
    if value not in AVAILABILITY:
        raise SelfServiceError("Availability is online, away or offline.", 422)
    _member(conn, customer_id, user_id)
    before = conn.execute(
        "SELECT available FROM commai_members WHERE customer_id = %s AND user_id = %s", (customer_id, user_id)
    ).fetchone()
    conn.execute(
        "UPDATE commai_members SET available = %s WHERE customer_id = %s AND user_id = %s",
        (value == "online", customer_id, user_id),
    )
    if before is None or before["available"] != (value == "online"):
        events.emit(
            conn,
            customer_id,
            "member.availability_changed",
            {"user_id": str(user_id), "availability": value},
            user_id,
        )
    return value


# ---- notifications ---------------------------------------------------------------------


def in_quiet_hours(p: dict, now: dt.datetime, business_tz: str) -> bool:
    start, end = p.get("quiet_start") or "", p.get("quiet_end") or ""
    if not (start and end):
        return False
    try:
        tz = ZoneInfo(p.get("timezone") or business_tz or "UTC")
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    hm = now.astimezone(tz).strftime("%H:%M")
    return start <= hm < end if start < end else (hm >= start or hm < end)


def should_notify(p: dict, kind: str, way: str, now: dt.datetime, business_tz: str = "UTC") -> bool:
    """Whether to tell this person about `kind` by `way` now. Quiet hours hold
    back email; in-app notices wait in the list either way."""
    notify = {k: {**DEFAULT_NOTIFY[k], **(p.get("notify") or {}).get(k, {})} for k in KINDS}
    if not notify.get(kind, {}).get(way):
        return False
    return not (way == "email" and in_quiet_hours(p, now, business_tz))


def notifications(conn: psycopg.Connection, customer_id: Any, user_id: Any, email: str, limit: int = 30) -> list[dict]:
    """The person's in-app notices, built from recorded events: conversations
    assigned to them, notes that mention them, and their own conversations close
    to a service target. Only kinds they switched on for in-app."""
    p = prefs(conn, customer_id, user_id)
    on = {k for k in KINDS if p["notify"][k]["in_app"]}
    out: list[dict] = []
    if "assignment" in on:
        for r in conn.execute(
            """SELECT e.at, e.data->>'conversation_id' AS conversation_id FROM commai_events e
               WHERE e.customer_id = %s AND e.type = 'conversation.assigned' AND e.data->>'assignee_id' = %s
               ORDER BY e.seq DESC LIMIT %s""",
            (customer_id, str(user_id), limit),
        ).fetchall():
            out.append(
                {
                    "kind": "assignment",
                    "at": r["at"],
                    "conversation_id": r["conversation_id"],
                    "text": "A conversation was assigned to you.",
                }
            )
    if "mention" in on:
        for r in conn.execute(
            """SELECT created_at AS at, conversation_id FROM commai_notes
               WHERE customer_id = %s AND (%s = ANY(mentions) OR %s = ANY(mentions))
               ORDER BY created_at DESC LIMIT %s""",
            (customer_id, email, str(user_id), limit),
        ).fetchall():
            out.append(
                {
                    "kind": "mention",
                    "at": r["at"],
                    "conversation_id": str(r["conversation_id"]),
                    "text": "You were mentioned in a private note.",
                }
            )
    if "sla_warning" in on:
        for r in conn.execute(
            """SELECT id, COALESCE(CASE WHEN first_reply_at IS NULL THEN first_reply_due END, resolve_due) AS due
               FROM conversations WHERE customer_id = %s AND assignee_id = %s AND state <> 'resolved'
               AND COALESCE(CASE WHEN first_reply_at IS NULL THEN first_reply_due END, resolve_due)
                   < now() + make_interval(mins => %s)
               ORDER BY 2 LIMIT %s""",
            (customer_id, user_id, SLA_WARN_MINUTES, limit),
        ).fetchall():
            late = r["due"] < dt.datetime.now(dt.UTC)
            out.append(
                {
                    "kind": "sla_warning",
                    "at": r["due"],
                    "conversation_id": str(r["id"]),
                    "text": "Past its service target." if late else "Due within 15 minutes.",
                }
            )
    out.sort(key=lambda n: n["at"], reverse=True)
    return out[:limit]


# ---- sessions ------------------------------------------------------------------------------


def sessions(conn: psycopg.Connection, user_id: Any, current_hash: str | None) -> list[dict]:
    rows = conn.execute(
        """SELECT token_hash, via, created_at, expires_at FROM sessions
           WHERE user_id = %s AND expires_at > now() ORDER BY created_at DESC""",
        (user_id,),
    ).fetchall()
    return [
        {
            "id": r["token_hash"][:16],
            "via": r["via"],
            "started": r["created_at"],
            "expires": r["expires_at"],
            "current": r["token_hash"] == current_hash,
        }
        for r in rows
    ]


def end_session(conn: psycopg.Connection, user_id: Any, session_id: str, current_hash: str | None) -> bool:
    if not re.fullmatch(r"[0-9a-f]{16}", session_id or ""):
        return False
    row = conn.execute(
        "DELETE FROM sessions WHERE user_id = %s AND left(token_hash, 16) = %s AND token_hash <> %s RETURNING 1",
        (user_id, session_id, current_hash or ""),
    ).fetchone()
    return row is not None


def end_other_sessions(conn: psycopg.Connection, user_id: Any, current_hash: str | None) -> int:
    rows = conn.execute(
        "DELETE FROM sessions WHERE user_id = %s AND token_hash <> %s RETURNING 1", (user_id, current_hash or "")
    ).fetchall()
    return len(rows)
