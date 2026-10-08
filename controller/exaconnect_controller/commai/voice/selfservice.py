"""Staff self-service and "say what you want" (ADR 0021).

Each person changes only their own forwarding, do not disturb and voicemail.
The API resolves "own" from the signed-in account, never from the request,
so there is no way to name someone else's extension here.

`parse()` turns a sentence such as "forward my calls to my mobile until 5"
into the exact change. It is a deterministic parser (no model); the change
is stored as a proposal and applied only when the same person confirms it.
Anything it doesn't understand is said so, never guessed.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from ... import audit
from .. import events, jobs
from ..inbox import settings
from . import billing
from .common import VoiceError, digits, now, policy
from .config import OpError, norm_e164

events.register("voice.settings_changed")

SELF_FIELDS = ("forward_to", "forward_until", "dnd", "dnd_until", "voicemail_greeting", "voicemail_to_email")


def mine(conn: psycopg.Connection, cid: Any, user_id: Any) -> dict:
    row = conn.execute(
        """SELECT v.id, v.name, v.extension, v.mobile, v.email, v.forward_to, v.forward_until, v.dnd, v.dnd_until,
                  v.voicemail_greeting, v.voicemail_to_email, v.site_id, s.name AS site, v.team_id
           FROM voice_users v LEFT JOIN voice_sites s ON s.id = v.site_id
           WHERE v.customer_id = %s AND v.user_id = %s AND v.status = 'active' ORDER BY v.created_at LIMIT 1""",
        (cid, user_id),
    ).fetchone()
    if row is None:
        raise VoiceError("You don't have a phone extension yet. Ask your company's voice admin.", 404)
    return row


def _forward_target(conn, cid, value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if re.fullmatch(r"\d{2,6}", value):
        if not conn.execute(
            "SELECT 1 FROM voice_users WHERE customer_id = %s AND extension = %s AND status = 'active'", (cid, value)
        ).fetchone():
            raise VoiceError(f"There is no extension {value}.", 422)
        return value
    try:
        e164 = norm_e164(value)
    except OpError as e:
        raise VoiceError(str(e), 422) from e
    check = billing.authorise(conn, cid, e164)
    if not check["allowed"]:
        raise VoiceError(f"Calls can't be forwarded there: {check['reason']}", 422)
    return e164


def update_mine(conn: psycopg.Connection, cid: Any, user_id: Any, changes: dict, actor: str) -> dict:
    """Change the signed-in person's own settings."""
    me = mine(conn, cid, user_id)
    bad = [k for k in changes if k not in SELF_FIELDS]
    if bad:
        raise VoiceError(f"You can change only: {', '.join(SELF_FIELDS)}.", 422)
    vals: dict = {}
    t = now()
    if "forward_to" in changes:
        vals["forward_to"] = _forward_target(conn, cid, changes["forward_to"] or "")
        if vals["forward_to"] == me["extension"]:
            raise VoiceError("You can't forward calls to yourself.", 422)
        if not vals["forward_to"]:
            vals["forward_until"] = None
    for key in ("forward_until", "dnd_until"):
        if key in changes and key not in vals:
            v = changes[key]
            if isinstance(v, str) and v:
                v = dt.datetime.fromisoformat(v)
            if v is not None and v.tzinfo is None:
                raise VoiceError("Give the time with its time zone.", 422)
            if v is not None and v <= t:
                raise VoiceError("That time has already passed.", 422)
            vals[key] = v or None
    if "dnd" in changes:
        vals["dnd"] = bool(changes["dnd"])
        if not vals["dnd"]:
            vals["dnd_until"] = None
    if "voicemail_greeting" in changes:
        vals["voicemail_greeting"] = str(changes["voicemail_greeting"] or "").strip()[:500]
    if "voicemail_to_email" in changes:
        vals["voicemail_to_email"] = bool(changes["voicemail_to_email"])
        if vals["voicemail_to_email"] and not me["email"]:
            raise VoiceError("Your extension has no email address. Ask your voice admin to add it.", 422)
    if not vals:
        return me
    conn.execute(
        f"UPDATE voice_users SET {', '.join(f'{k} = %s' for k in vals)} WHERE id = %s", (*vals.values(), me["id"])
    )
    me = mine(conn, cid, user_id)
    for key in ("forward_until", "dnd_until"):
        if me[key]:
            jobs.enqueue(
                conn,
                "voice.settings_expire",
                {"voice_user_id": str(me["id"]), "field": key},
                customer_id=cid,
                dedupe_key=f"voice.expire:{me['id']}:{key}:{me[key].isoformat()}",
                delay_s=max(0.0, (me[key] - t).total_seconds()),
            )
    jobs.enqueue(conn, "voice.render", {"customer_id": str(cid)}, customer_id=cid)
    audit.record(
        conn, actor, "commai.voice.self", str(me["id"]), cid, {"fields": sorted(vals)}
    )  # the values may be personal numbers: not logged
    events.emit(
        conn, cid, "voice.settings_changed", {"voice_user_id": str(me["id"]), "fields": sorted(vals)}, str(me["id"])
    )
    return me


@jobs.handler("voice.settings_expire")
def _expire(conn: psycopg.Connection, job: dict):
    """Forwarding or do not disturb set 'until' a time ends at that time."""
    p = job["payload"]
    field = p["field"]
    if field not in ("forward_until", "dnd_until"):
        return None
    row = conn.execute(
        f"SELECT id, customer_id, {field} AS until FROM voice_users WHERE id = %s", (p["voice_user_id"],)
    ).fetchone()
    if row is None or row["until"] is None:
        return None
    wait = (row["until"] - now()).total_seconds()
    if wait > 0:
        return jobs.Later("not yet", wait)
    if field == "forward_until":
        conn.execute("UPDATE voice_users SET forward_to = '', forward_until = NULL WHERE id = %s", (row["id"],))
    else:
        conn.execute("UPDATE voice_users SET dnd = false, dnd_until = NULL WHERE id = %s", (row["id"],))
    jobs.enqueue(conn, "voice.render", {"customer_id": str(row["customer_id"])}, customer_id=row["customer_id"])
    return None


def calls(conn, cid, voice_user_id, limit: int = 100) -> list[dict]:
    return conn.execute(
        """SELECT id, call_id, direction, from_number, to_number, started_at, ended_at, seconds, status,
                  (recording_ref <> '') AS has_recording
           FROM voice_cdrs WHERE customer_id = %s AND voice_user_id = %s ORDER BY ended_at DESC LIMIT %s""",
        (cid, voice_user_id, limit),
    ).fetchall()


def recordings(conn, cid, voice_user_id, is_admin: bool) -> list[dict]:
    """Recordings follow the company's policy: none, own calls, or admins only."""
    rule = policy(conn, cid)["recording_access"]
    if rule == "none" or (rule == "admins" and not is_admin):
        raise VoiceError("Your company's policy doesn't let you play call recordings.", 403)
    return conn.execute(
        """SELECT call_id, direction, from_number, to_number, ended_at, seconds, recording_ref
           FROM voice_cdrs WHERE customer_id = %s AND voice_user_id = %s AND recording_ref <> ''
           ORDER BY ended_at DESC LIMIT 100""",
        (cid, voice_user_id),
    ).fetchall()


# ---- "say what you want" -------------------------------------------------------------

_NUM_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "half an": 0.5,
    "an": 1,
    "a": 1,
}
_TIME_RE = re.compile(
    r"\b(?:until|till|til|'til)\s+(?P<day>tomorrow\s+(?:at\s+)?)?"
    r"(?P<time>noon|midday|midnight|\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?|\d{1,2}\s*o'?clock)"
    r"(?P<day2>\s+tomorrow)?\b"
)
_FOR_RE = re.compile(
    r"\bfor\s+(?:the\s+next\s+)?(?P<n>\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|half an|an|a)\s+(?P<unit>hours?|hrs?|minutes?|mins?)\b"
)


def _when(text: str, t: dt.datetime) -> tuple[dt.datetime | None, str]:
    """The end time in a sentence, if any, and the text without it."""
    m = _FOR_RE.search(text)
    if m:
        n = m.group("n")
        n = float(n) if n[0].isdigit() else _NUM_WORDS[n]
        delta = dt.timedelta(hours=n) if m.group("unit").startswith("h") else dt.timedelta(minutes=n)
        return t + delta, (text[: m.start()] + text[m.end() :]).strip()
    m = _TIME_RE.search(text)
    if not m:
        return None, text
    raw = m.group("time").replace(".", "").replace("o'clock", "").replace("oclock", "").strip()
    if raw in ("noon", "midday"):
        hour, minute = 12, 0
    elif raw == "midnight":
        hour, minute = 0, 0
    else:
        mm = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", raw)
        hour, minute, ampm = int(mm.group(1)), int(mm.group(2) or 0), mm.group(3)
        if hour > 23 or minute > 59:
            return None, text
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        elif ampm is None and 1 <= hour <= 7:
            hour += 12  # "until 5" during the working day means 5 in the afternoon
    candidate = t.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if m.group("day") or m.group("day2"):
        candidate += dt.timedelta(days=1)
    elif candidate <= t:
        candidate += dt.timedelta(days=1)
    return candidate, (text[: m.start()] + text[m.end() :]).strip()


def _fmt(at: dt.datetime, t: dt.datetime) -> str:
    day = (
        "today"
        if at.date() == t.date()
        else ("tomorrow" if at.date() == t.date() + dt.timedelta(days=1) else at.strftime("%a %-d %b"))
    )
    return f"{at.strftime('%-I:%M %p').lower()} {day}"


def _no(text: str) -> bool:
    return bool(re.search(r"\b(stop|cancel|turn off|switch off|end|disable|don'?t|do not send|no more|off)\b", text))


def parse(
    conn: psycopg.Connection, cid: Any, text: str, me: dict | None, is_admin: bool, at: dt.datetime | None = None
) -> dict:
    """-> {"understood": bool, "scope": "self" | "admin", "changes" | "ops", "summary", "message"}"""
    tz = ZoneInfo(settings(conn, cid)["timezone"] or "America/Port_of_Spain")
    t = (at or now()).astimezone(tz)
    raw = " ".join((text or "").split())
    low = raw.lower().rstrip(".!")

    def need_me():
        if me is None:
            raise VoiceError("You don't have a phone extension yet. Ask your company's voice admin.", 404)

    # Voicemail greeting (keep the person's own words and case).
    m = re.search(
        r"\b(?:set|change|make)\s+my\s+(?:voicemail\s+)?greeting\s+(?:to|say|says)\s+[\"'“]?(.+?)[\"'”]?$", raw, re.I
    )
    if m:
        need_me()
        g = m.group(1).strip()
        return {
            "understood": True,
            "scope": "self",
            "changes": {"voicemail_greeting": g},
            "summary": f"Set your voicemail greeting to “{g}”.",
        }

    # Stop forwarding.
    if re.search(r"\bforward", low) and _no(low.split(" to ")[0]):
        need_me()
        return {
            "understood": True,
            "scope": "self",
            "changes": {"forward_to": ""},
            "summary": "Stop forwarding your calls.",
        }

    # Forward my calls to ...
    if re.search(r"\b(forward|divert|redirect|send)\b.*\bcalls?\b", low) or low.startswith("forward"):
        need_me()
        until, rest = _when(low, t)
        m = re.search(r"\bto\s+(?:my\s+)?(.+)$", rest)
        if not m:
            return {
                "understood": False,
                "message": "Where should your calls go? For example “forward my calls to my mobile until 5”.",
            }
        dest_text = m.group(1).strip()
        if re.match(r"(mobile|cell|cellphone|cell phone|phone)\b", dest_text):
            if not me.get("mobile"):
                return {
                    "understood": False,
                    "message": "Your extension has no mobile number. Give the number instead, "
                    "or ask your voice admin to add it.",
                }
            dest, label = me["mobile"], f"your mobile ({me['mobile']})"
        elif mm := re.match(r"(?:extension|ext\.?|x)\s*(\d{2,6})\b", dest_text):
            dest, label = mm.group(1), f"extension {mm.group(1)}"
        else:
            d = digits(dest_text)
            if len(d) < 7:
                return {
                    "understood": False,
                    "message": "I couldn't read the number. Try “forward my calls to +1 868 555 0199”.",
                }
            dest = norm_e164(d)
            label = dest
        changes: dict = {"forward_to": dest}
        summary = f"Forward your calls to {label}"
        if until:
            changes["forward_until"] = until.isoformat()
            summary += f" until {_fmt(until, t)}"
        return {"understood": True, "scope": "self", "changes": changes, "summary": summary + "."}

    # Do not disturb.
    if re.search(r"\b(do not disturb|dnd|don'?t disturb)\b", low):
        need_me()
        head = re.sub(r"\b(do not disturb|don'?t disturb)\b", "dnd", low)
        if _no(head) and not re.search(r"\b(turn on|switch on|put on|set)\b", head):
            return {
                "understood": True,
                "scope": "self",
                "changes": {"dnd": False},
                "summary": "Turn off do not disturb.",
            }
        until, _ = _when(low, t)
        changes = {"dnd": True}
        summary = "Turn on do not disturb"
        if until:
            changes["dnd_until"] = until.isoformat()
            summary += f" until {_fmt(until, t)}"
        return {"understood": True, "scope": "self", "changes": changes, "summary": summary + "."}

    # Voicemail to email.
    if re.search(r"\bvoice ?mails?\b", low) and re.search(r"\be-?mail", low.replace("voicemail", "")):
        need_me()
        on = not _no(low)
        return {
            "understood": True,
            "scope": "self",
            "changes": {"voicemail_to_email": on},
            "summary": "Send your voicemail to your email." if on else "Stop sending your voicemail to email.",
        }

    # Admin: move someone to another site or team.
    m = re.match(r"^move\s+(.+?)\s+to\s+(?:the\s+)?(.+?)(?:\s+(site|office|team))?$", low)
    if m:
        if not is_admin:
            return {"understood": False, "message": "Only your company's voice admins can move people."}
        who = _find_user(conn, cid, m.group(1))
        if isinstance(who, str):
            return {"understood": False, "message": who}
        place = m.group(2)
        kind = m.group(3)
        site = (
            None
            if kind == "team"
            else conn.execute(
                "SELECT id, name FROM voice_sites WHERE customer_id = %s AND lower(name) = %s", (cid, place)
            ).fetchone()
        )
        team = (
            None
            if kind in ("site", "office")
            else conn.execute(
                "SELECT id, name FROM commai_teams WHERE customer_id = %s AND lower(name) = %s", (cid, place)
            ).fetchone()
        )
        if site:
            return {
                "understood": True,
                "scope": "admin",
                "ops": [{"op": "move_user", "user": who["extension"], "site": str(site["id"])}],
                "summary": f"Move {who['name']} (ext {who['extension']}) to the {site['name']} site. "
                "Their emergency address changes to that site's address.",
            }
        if team:
            return {
                "understood": True,
                "scope": "admin",
                "ops": [{"op": "move_user", "user": who["extension"], "team": str(team["id"])}],
                "summary": f"Move {who['name']} (ext {who['extension']}) to the {team['name']} team.",
            }
        return {"understood": False, "message": f"There is no site or team called {place}."}

    # Admin: remove someone.
    m = re.match(r"^(?:remove|delete)\s+(?:user\s+)?(.+?)(?:'s\s+extension)?$", low)
    if m:
        if not is_admin:
            return {"understood": False, "message": "Only your company's voice admins can remove people."}
        who = _find_user(conn, cid, m.group(1))
        if isinstance(who, str):
            return {"understood": False, "message": who}
        return {
            "understood": True,
            "scope": "admin",
            "ops": [{"op": "remove_user", "user": who["extension"]}],
            "summary": f"Remove {who['name']} (ext {who['extension']}). Their monthly charge stops today.",
        }

    return {
        "understood": False,
        "message": "I didn't understand that. I can forward your calls, turn do not disturb on or off, set your "
        "voicemail greeting and send voicemail to email.",
    }


def _find_user(conn, cid, ref: str) -> dict | str:
    ref = ref.strip()
    ext = re.sub(r"^(?:extension|ext\.?)\s*", "", ref)
    rows = conn.execute(
        """SELECT id, name, extension FROM voice_users WHERE customer_id = %s AND status = 'active'
           AND (extension = %s OR lower(name) = %s OR lower(split_part(name, ' ', 1)) = %s)""",
        (cid, ext, ref, ref),
    ).fetchall()
    if not rows:
        return f"There is nobody called {ref}."
    if len(rows) > 1:
        return f"More than one person matches {ref}: say their extension."
    return rows[0]


def propose(conn, cid, user_id, text: str, parsed: dict) -> dict:
    row = conn.execute(
        """INSERT INTO voice_chat_proposals (customer_id, user_id, text, ops, summary, scope)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, summary, scope, ops, expires_at""",
        (
            cid,
            user_id,
            text[:500],
            Jsonb(parsed.get("ops") or parsed.get("changes")),
            parsed["summary"],
            parsed["scope"],
        ),
    ).fetchone()
    return row


QUESTION = re.compile(r"(?i)^\s*(why|how|what|when|where|who|which|is|are|does|did|has|have)\b|\?\s*$")


def looks_like_change(text: str) -> bool:
    """A request ("forward my calls to my mobile") rather than a question ("why do my calls go to voicemail?")."""
    return bool((text or "").strip()) and not QUESTION.search(text)


def propose_text(conn: psycopg.Connection, cid: Any, user: Any, text: str) -> dict:
    """The same proposal "say what you want" makes (ADR 0039): the platform
    assistant uses it so a voice change typed there gets the same exact
    change, price impact and confirm step. Nothing changes until the same
    person confirms it at /voice/say/{id}/confirm.
    -> {"understood": False, "message"} or {"understood": True, "id", "summary", "scope", "change", ...}"""
    from . import config, perms

    try:
        me = mine(conn, cid, user.id)
    except VoiceError:
        me = None
    is_admin = perms.is_voice_admin(conn, user, cid)
    parsed = parse(conn, cid, text, me, is_admin)
    if not parsed["understood"]:
        return {"understood": False, "message": parsed["message"]}
    out: dict = {"understood": True, "summary": parsed["summary"], "scope": parsed["scope"]}
    if parsed["scope"] == "admin":
        dry = config.apply(conn, cid, parsed["ops"], actor=user.actor, dry_run=True)
        if dry["errors"]:
            return {"understood": False, "message": dry["errors"][0]["error"]}
        out["price_impact"] = dry["price_impact"]
    prop = propose(conn, cid, user.id, text, parsed)
    out.update({"id": str(prop["id"]), "change": prop["ops"], "expires_at": prop["expires_at"]})
    return out
