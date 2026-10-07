"""International revenue share fraud protection (ADR 0027), on top of the
stage 4 fraud limits (daily spend cap, blocked premium prefixes, international
on or off, unusual-volume alerts) in billing.py.

Checked before every outbound call that is not an emergency call:
- the calling number must be allowed to call out (emergency address rules);
- international calling must not be suspended;
- high-risk destinations for the business's country are blocked unless the
  business has allowed that prefix (ExaCarib keeps the list, per calling
  country, '*' for all);
- outside business hours, international calls are allowed, alerted or blocked;
- daily caps on international calls and international spend;
- a sudden spike (this hour's international calls well above the usual hourly
  rate) suspends international calling until a voice admin with spend
  permission restores it.

"International" means the destination is in another country than the
business's own (its first site), so a call from Trinidad to Jamaica counts,
even though both are +1.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from ... import audit
from .. import events
from . import countries, emergency
from .common import VoiceError, digits, money, s

events.register("voice.fraud_suspended", "voice.fraud_restored")

AFTER_HOURS = ("allow", "alert", "block")
DEFAULT_HOURS = {d: [["07:00", "19:00"]] for d in ("mon", "tue", "wed", "thu", "fri")}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def origin(conn: psycopg.Connection, cid: Any) -> dict:
    row = conn.execute(
        "SELECT country, timezone FROM voice_sites WHERE customer_id = %s ORDER BY created_at, name LIMIT 1", (cid,)
    ).fetchone()
    return {"country": row["country"] if row else "TT", "timezone": row["timezone"] if row else "America/Port_of_Spain"}


def is_international(number: str, home: str) -> bool:
    d = digits(number)
    if len(d) < 7:
        return False
    return countries.country_of(d) != home


def high_risk(conn: psycopg.Connection, home: str, number: str) -> dict | None:
    return conn.execute(
        """SELECT * FROM voice_high_risk_destinations WHERE origin IN ('*', %s) AND %s LIKE prefix || '%%'
           ORDER BY length(prefix) DESC LIMIT 1""",
        (home, digits(number)),
    ).fetchone()


def settings(conn: psycopg.Connection, cid: Any) -> dict:
    from .billing import fraud_limits

    lim = fraud_limits(conn, cid)
    for k in ("intl_daily_cap",):
        lim[k] = s(lim[k]) if lim[k] is not None else None
    lim["spike_factor"] = s(lim["spike_factor"])
    lim["origin"] = origin(conn, cid)["country"]
    return lim


def set_settings(conn: psycopg.Connection, cid: Any, values: dict, actor: str) -> dict:
    cur = settings(conn, cid)
    cap = values.get("intl_daily_cap", cur["intl_daily_cap"])
    cap = money(cap) if cap not in (None, "") else None
    if cap is not None and cap < 0:
        raise VoiceError("The international cap can't be negative.", 422)
    ah = values.get("after_hours_international", cur["after_hours_international"])
    if ah not in AFTER_HOURS:
        raise VoiceError("After hours, international calls are allow, alert or block.", 422)
    hours_id = values.get("hours_id", cur["hours_id"])
    if (
        hours_id
        and not conn.execute("SELECT 1 FROM voice_hours WHERE id = %s AND customer_id = %s", (hours_id, cid)).fetchone()
    ):
        raise VoiceError("Those business hours aren't in your phone system.", 422)
    factor = money(values.get("spike_factor", cur["spike_factor"]))
    if factor < 2:
        raise VoiceError("The spike factor is at least 2.", 422)
    allowed = sorted({digits(p) for p in values.get("allowed_high_risk", cur["allowed_high_risk"]) if digits(p)})
    conn.execute(
        """UPDATE voice_fraud_limits SET intl_daily_cap = %s, intl_daily_calls = %s, after_hours_international = %s,
             hours_id = %s, spike_factor = %s, spike_min_calls = %s, allowed_high_risk = %s, updated_by = %s,
             updated_at = now() WHERE customer_id = %s""",
        (
            cap,
            max(1, int(values.get("intl_daily_calls", cur["intl_daily_calls"]))),
            ah,
            hours_id or None,
            factor,
            max(3, int(values.get("spike_min_calls", cur["spike_min_calls"]))),
            allowed,
            actor,
            cid,
        ),
    )
    return settings(conn, cid)


def _open_now(conn, cid, lim: dict, at: dt.datetime) -> bool:
    sched, tz, holidays = DEFAULT_HOURS, origin(conn, cid)["timezone"], []
    if lim.get("hours_id"):
        h = conn.execute("SELECT * FROM voice_hours WHERE id = %s", (lim["hours_id"],)).fetchone()
        if h:
            sched, tz, holidays = h["schedule"] or {}, h["timezone"], h["holidays"] or []
    local = at.astimezone(ZoneInfo(tz))
    if local.date().isoformat() in holidays:
        return False
    hhmm = local.strftime("%H:%M")
    return any(a <= hhmm < b for a, b in sched.get(DAYS[local.weekday()], []))


def _intl_today(conn, cid) -> dict:
    row = conn.execute(
        """SELECT count(*) AS calls, coalesce(sum((SELECT sum(amount) FROM voice_charges c WHERE c.cdr_id = d.id)), 0)
                  AS spend
           FROM voice_cdrs d WHERE d.customer_id = %s AND d.direction = 'outbound' AND d.international
             AND d.status = 'completed' AND d.started_at >= date_trunc('day', now())""",
        (cid,),
    ).fetchone()
    return {"calls": row["calls"], "spend": money(row["spend"])}


def _rates(conn, cid, lim: dict) -> dict:
    since = lim["restored_at"] or dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
    row = conn.execute(
        """SELECT count(*) FILTER (WHERE started_at > greatest(now() - interval '1 hour', %s)) AS last_hour,
                  count(*) FILTER (WHERE started_at <= now() - interval '1 hour') AS week
           FROM voice_cdrs WHERE customer_id = %s AND direction = 'outbound' AND international
             AND started_at > now() - interval '7 days'""",
        (since, cid),
    ).fetchone()
    return {"last_hour": row["last_hour"], "hourly_baseline": row["week"] / (7 * 24 - 1)}


def suspend(conn: psycopg.Connection, cid: Any, reason: str) -> None:
    conn.execute(
        """UPDATE voice_fraud_limits SET intl_suspended = true, suspended_at = now(), suspended_reason = %s
           WHERE customer_id = %s AND NOT intl_suspended""",
        (reason[:300], cid),
    )
    events.emit(conn, cid, "voice.fraud_suspended", {"reason": reason})
    emergency.notify(
        conn,
        cid,
        "fraud",
        "International calling suspended",
        f"{reason} Check recent calls, then restore international calling from Voice → Fraud.",
    )
    audit.record(conn, "system:voice", "commai.voice.intl_suspended", "", cid, {"reason": reason})


def restore(conn: psycopg.Connection, cid: Any, actor: str, note: str = "") -> dict:
    row = conn.execute(
        """UPDATE voice_fraud_limits SET intl_suspended = false, restored_by = %s, restored_at = now()
           WHERE customer_id = %s AND intl_suspended RETURNING suspended_reason""",
        (actor, cid),
    ).fetchone()
    if row is None:
        raise VoiceError("International calling is not suspended.", 409)
    events.emit(conn, cid, "voice.fraud_restored", {"by": actor, "note": note[:300]})
    audit.record(
        conn, actor, "commai.voice.intl_restored", "", cid, {"note": note[:300], "was": row["suspended_reason"]}
    )
    return settings(conn, cid)


def _alert_once(conn, cid, reason: str, data: dict) -> None:
    if not conn.execute(
        """SELECT 1 FROM commai_events WHERE customer_id = %s AND type = 'voice.fraud_alert'
           AND data->>'reason' = %s AND at > now() - interval '1 hour'""",
        (cid, reason),
    ).fetchone():
        events.emit(conn, cid, "voice.fraud_alert", {"reason": reason, **data})


def check(
    conn: psycopg.Connection,
    cid: Any,
    to_number: str,
    *,
    voice_user_id: Any = None,
    from_number: str | None = None,
    at: dt.datetime | None = None,
) -> dict | None:
    """-> {"allowed": False, "reason"} to stop the call, or None to let it go on."""
    at = at or dt.datetime.now(dt.UTC)
    if from_number and len(digits(from_number)) > 6:
        n = conn.execute(
            """SELECT * FROM voice_numbers WHERE customer_id = %s AND status <> 'removed'
               AND regexp_replace(e164, '\\D', '', 'g') = %s""",
            (cid, digits(from_number)),
        ).fetchone()
        if n is not None and not n["outbound_enabled"]:
            return {
                "allowed": False,
                "code": "number_outbound_off",
                "reason": n["outbound_reason"] or "This number can't make outbound calls yet.",
            }
    home = origin(conn, cid)["country"]
    if not is_international(to_number, home):
        return None
    lim = settings(conn, cid)
    if lim["intl_suspended"]:
        return {
            "allowed": False,
            "code": "intl_suspended",
            "reason": "International calling is suspended after unusual activity. A voice admin can restore it.",
        }
    hr = high_risk(conn, home, to_number)
    if hr and not any(digits(to_number).startswith(p) for p in lim["allowed_high_risk"]):
        return {
            "allowed": False,
            "code": "high_risk",
            "reason": f"Calls to {hr['name'] or '+' + hr['prefix']} are blocked as a high-risk destination "
            f"(+{hr['prefix']}). A voice admin can allow it.",
        }
    if not _open_now(conn, cid, lim, at):
        if lim["after_hours_international"] == "block":
            return {
                "allowed": False,
                "code": "after_hours",
                "reason": "International calls are blocked outside business hours.",
            }
        if lim["after_hours_international"] == "alert":
            _alert_once(conn, cid, "after_hours_international", {"to": digits(to_number)[:4] + "…"})
    today = _intl_today(conn, cid)
    if today["calls"] >= lim["intl_daily_calls"]:
        return {
            "allowed": False,
            "code": "intl_daily_calls",
            "reason": f"Today's international calls have reached the cap of {lim['intl_daily_calls']}.",
        }
    if lim["intl_daily_cap"] is not None and today["spend"] >= money(lim["intl_daily_cap"]):
        return {
            "allowed": False,
            "code": "intl_daily_cap",
            "reason": f"Today's international spend has reached the cap of {lim['intl_daily_cap']}.",
        }
    r = _rates(conn, cid, lim)
    now_count = r["last_hour"] + 1
    threshold = Decimal(str(max(r["hourly_baseline"], 1.0))) * money(lim["spike_factor"])
    if now_count >= lim["spike_min_calls"] and now_count > threshold:
        suspend(
            conn,
            cid,
            f"{now_count} international calls in the last hour, "
            f"against about {r['hourly_baseline']:.1f} an hour usually.",
        )
        return {
            "allowed": False,
            "code": "intl_spike",
            "reason": "International calling is suspended after a sudden rise in calls. A voice admin can restore it.",
        }
    return None


def status(conn: psycopg.Connection, cid: Any) -> dict:
    lim = settings(conn, cid)
    home = lim["origin"]
    return {
        "settings": lim,
        "today": {k: (s(v) if isinstance(v, Decimal) else v) for k, v in _intl_today(conn, cid).items()},
        "rates": _rates(
            conn, cid, conn.execute("SELECT * FROM voice_fraud_limits WHERE customer_id = %s", (cid,)).fetchone()
        ),
        "high_risk": conn.execute(
            "SELECT * FROM voice_high_risk_destinations WHERE origin IN ('*', %s) ORDER BY prefix", (home,)
        ).fetchall(),
        "open_now": _open_now(conn, cid, lim, dt.datetime.now(dt.UTC)),
    }


def list_high_risk(conn) -> list[dict]:
    return conn.execute("SELECT * FROM voice_high_risk_destinations ORDER BY origin, prefix").fetchall()


def save_high_risk(conn, origin_cc: str, prefix: str, name: str, reason: str, actor: str) -> dict:
    p = digits(prefix)
    if not p:
        raise VoiceError("Give the destination prefix, like 252.", 422)
    o = (origin_cc or "*").upper()[:2] if origin_cc != "*" else "*"
    return conn.execute(
        """INSERT INTO voice_high_risk_destinations (origin, prefix, name, reason, created_by)
           VALUES (%s, %s, %s, %s, %s) ON CONFLICT (origin, prefix) DO UPDATE SET name = EXCLUDED.name,
             reason = EXCLUDED.reason RETURNING *""",
        (o, p, name[:120], reason[:300], actor),
    ).fetchone()


def delete_high_risk(conn, origin_cc: str, prefix: str) -> None:
    conn.execute(
        "DELETE FROM voice_high_risk_destinations WHERE origin = %s AND prefix = %s", (origin_cc, digits(prefix))
    )
