"""Abuse and account protection (ADR 0030).

- Unusual use is watched every five minutes by a durable job: many failed
  sign-ins, a sudden spike in messages sent, a jump in metered usage. An API
  key used from a new address (or a new country, when a GeoIP database is
  configured) is noticed as it happens.
- Each finding is an alert, raised once (dedupe key), shown to the business
  (Security tab, `security.alert` event and webhook) and to ExaCarib (the
  admin alert list).
- Clear abuse of an API key (used from many new addresses within minutes)
  locks the key for a while; a business admin can unlock it early.
- Hard spend caps reuse the usage limits (ADR 0016): a hard monthly limit on
  `message_out:<channel>`, or on `message_out` for every channel together,
  stops sending.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

import psycopg

from ... import audit
from .. import channels, events, jobs, usage

log = logging.getLogger("exaconnect.commai.enterprise")

events.register("security.alert")

DEFAULTS: dict[str, float] = {
    "failed_signins": 10,  # failed sign-ins of the business's accounts within 15 minutes
    "send_spike_factor": 5,  # messages sent in the last hour against the hourly average of the week before
    "send_spike_min": 50,  # ...and at least this many in the hour
    "usage_jump_factor": 3,  # a meter's use today against its daily average of the week before
    "usage_jump_min": 100,  # ...and at least this much today
    "key_new_addresses": 5,  # one API key used from this many new addresses within 10 minutes: lock it
    "key_lock_minutes": 60,
}
TICK_S = 300


def thresholds(conn: psycopg.Connection, customer_id: Any) -> dict[str, float]:
    row = conn.execute(
        "SELECT thresholds FROM commai_security_settings WHERE customer_id = %s", (customer_id,)
    ).fetchone()
    out = dict(DEFAULTS)
    for k, v in ((row or {}).get("thresholds") or {}).items():
        if k in out and isinstance(v, int | float) and v > 0:
            out[k] = float(v)
    return out


def country_of(ip: str) -> str:
    """ISO country of an address, '' when unknown. Uses a MaxMind-format
    database only when EXA_GEOIP_DB names one and the reader is installed;
    otherwise countries are not known and only new addresses are reported."""
    path = os.environ.get("EXA_GEOIP_DB", "")
    if not path:
        return ""
    try:
        import geoip2.database  # type: ignore[import-not-found]

        with geoip2.database.Reader(path) as r:
            return r.country(ip).country.iso_code or ""
    except Exception:  # noqa: BLE001 - no database, no reader, private address
        return ""


# ---- alerts ---------------------------------------------------------------------------------


def raise_alert(
    conn: psycopg.Connection,
    customer_id: Any,
    kind: str,
    summary: str,
    *,
    dedupe: str,
    severity: str = "warning",
    evidence: dict | None = None,
    action_taken: str = "",
) -> dict | None:
    """Record an alert once per dedupe key. Returns it when it is new."""
    from psycopg.types.json import Jsonb

    row = conn.execute(
        """INSERT INTO commai_security_alerts (customer_id, kind, severity, summary, evidence, action_taken, dedupe_key)
           VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (customer_id, dedupe_key) DO NOTHING RETURNING *""",
        (customer_id, kind, severity, summary, Jsonb(evidence or {}), action_taken, dedupe),
    ).fetchone()
    if row is None:
        return None
    events.emit(
        conn,
        customer_id,
        "security.alert",
        {"alert_id": str(row["id"]), "kind": kind, "severity": severity, "summary": summary},
        str(row["id"]),
    )
    audit.record(conn, "system:protect", "security.alert", kind, customer_id, {"summary": summary, **(evidence or {})})
    log.warning("security alert for %s: %s", customer_id, summary)
    return row


# ---- API keys -----------------------------------------------------------------------------


def key_seen(conn: psycopg.Connection, customer_id: Any, key_id: Any, ip: str) -> None:
    """Note where a key was used; alert on a new address or country, lock on clear abuse."""
    row = conn.execute(
        """INSERT INTO commai_api_key_sightings (key_id, customer_id, ip, country) VALUES (%s, %s, %s, '')
           ON CONFLICT (key_id, ip) DO UPDATE SET last_seen_at = now(), hits = commai_api_key_sightings.hits + 1
             WHERE commai_api_key_sightings.last_seen_at < now() - interval '1 minute'
           RETURNING (xmax = 0) AS inserted""",
        (key_id, customer_id, ip),
    ).fetchone()
    if not row or not row["inserted"]:
        return
    key = conn.execute("SELECT id, prefix, name, locked_until FROM api_keys WHERE id = %s", (key_id,)).fetchone()
    country = country_of(ip)
    if country:
        conn.execute(
            "UPDATE commai_api_key_sightings SET country = %s WHERE key_id = %s AND ip = %s", (country, key_id, ip)
        )
    before = conn.execute(
        "SELECT ip, country FROM commai_api_key_sightings WHERE key_id = %s AND ip <> %s", (key_id, ip)
    ).fetchall()
    if not before:
        return  # the first address a key is used from is not news
    t = thresholds(conn, customer_id)
    label = f"{key['name']} ({key['prefix']}…)"
    known = {b["country"] for b in before if b["country"]}
    if country and known and country not in known:
        raise_alert(
            conn,
            customer_id,
            "api_key.new_country",
            f"API key {label} was used from a new country ({country}).",
            dedupe=f"key-country:{key_id}:{country}",
            evidence={"key": key["prefix"], "ip": ip, "country": country, "earlier": sorted(known)},
        )
    else:
        raise_alert(
            conn,
            customer_id,
            "api_key.new_address",
            f"API key {label} was used from a new address ({ip}).",
            dedupe=f"key-ip:{key_id}:{ip}",
            severity="info",
            evidence={"key": key["prefix"], "ip": ip},
        )
    recent = conn.execute(
        """SELECT count(*) AS n FROM commai_api_key_sightings
           WHERE key_id = %s AND first_seen_at > now() - interval '10 minutes'""",
        (key_id,),
    ).fetchone()["n"]
    if recent >= t["key_new_addresses"] and not (key["locked_until"] and key["locked_until"] > dt.datetime.now(dt.UTC)):
        minutes = int(t["key_lock_minutes"])
        reason = f"used from {recent} new addresses within 10 minutes"
        conn.execute(
            """UPDATE api_keys SET locked_until = now() + make_interval(mins => %s), locked_reason = %s
               WHERE id = %s""",
            (minutes, reason, key_id),
        )
        raise_alert(
            conn,
            customer_id,
            "api_key.locked",
            f"API key {label} was locked for {minutes} minutes: {reason}.",
            dedupe=f"key-lock:{key_id}:{dt.datetime.now(dt.UTC):%Y%m%d%H%M}",
            severity="critical",
            evidence={"key": key["prefix"], "addresses": recent},
            action_taken=f"locked for {minutes} minutes",
        )


def unlock_key(conn: psycopg.Connection, customer_id: Any, key_id: Any) -> dict | None:
    return conn.execute(
        """UPDATE api_keys k SET locked_until = NULL, locked_reason = ''
           FROM users u WHERE k.id = %s AND u.id = k.user_id AND u.customer_id = %s RETURNING k.id, k.prefix, k.name""",
        (key_id, customer_id),
    ).fetchone()


def keys(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    return conn.execute(
        """SELECT k.id, k.name, k.prefix, u.email AS owner, k.created_at, k.last_used_at, k.locked_until,
                  k.locked_reason, (k.locked_until IS NOT NULL AND k.locked_until > now()) AS locked,
                  (SELECT count(*) FROM commai_api_key_sightings s WHERE s.key_id = k.id) AS addresses
           FROM api_keys k JOIN users u ON u.id = k.user_id
           WHERE u.customer_id = %s AND k.revoked_at IS NULL ORDER BY k.created_at DESC""",
        (customer_id,),
    ).fetchall()


# ---- spend caps -------------------------------------------------------------------------------


def check_send(conn: psycopg.Connection, conv: dict) -> None:
    """Hard monthly limits on outbound messages stop sending (SendBlocked)."""
    ch = channels.get(conv["channel"])
    if not getattr(ch, "external", False):
        return
    cid = conv["customer_id"]
    if not usage.allowed(conn, cid, f"message_out:{conv['channel']}"):
        raise channels.SendBlocked(
            f"Your monthly limit for {ch.label} messages has been reached. An admin can raise it under usage limits."
        )
    lim = conn.execute(
        "SELECT monthly_hard FROM usage_limits WHERE customer_id = %s AND meter = 'message_out'", (cid,)
    ).fetchone()
    if lim and lim["monthly_hard"] is not None:
        used = conn.execute(
            """SELECT coalesce(sum(quantity), 0) AS q FROM usage_records
               WHERE customer_id = %s AND meter LIKE 'message_out:%%' AND at >= date_trunc('month', now())""",
            (cid,),
        ).fetchone()["q"]
        if float(used) + 1 > float(lim["monthly_hard"]):
            raise channels.SendBlocked(
                "Your monthly limit for outgoing messages has been reached. An admin can raise it under usage limits."
            )


# ---- watching -------------------------------------------------------------------------------


def watch(conn: psycopg.Connection, customer_id: Any, now: dt.datetime | None = None) -> list[dict]:
    """Look for unusual use in one business. Returns the alerts it raised."""
    now = now or dt.datetime.now(dt.UTC)
    t = thresholds(conn, customer_id)
    raised: list[dict | None] = []

    emails = [
        r["email"].lower() for r in conn.execute("SELECT email FROM users WHERE customer_id = %s", (customer_id,))
    ]
    failed = conn.execute(
        """SELECT count(*) AS n, count(DISTINCT detail->>'ip') AS ips FROM audit_log
           WHERE action = 'login_failed' AND at > %s - interval '15 minutes' AND at <= %s
             AND (customer_id = %s OR lower(target) = ANY(%s))""",
        (now, now, customer_id, emails),
    ).fetchone()
    if failed["n"] >= t["failed_signins"]:
        raised.append(
            raise_alert(
                conn,
                customer_id,
                "signin.failures",
                f"{failed['n']} failed sign-ins to your organisation's accounts in 15 minutes.",
                dedupe=f"failed:{int(now.timestamp()) // 900}",
                evidence={"failed": failed["n"], "addresses": failed["ips"]},
            )
        )

    sent = conn.execute(
        """SELECT count(*) FILTER (WHERE created_at > %(now)s - interval '1 hour') AS last_hour,
                  count(*) FILTER (WHERE created_at <= %(now)s - interval '1 hour') AS week
           FROM messages WHERE customer_id = %(c)s AND direction = 'out'
             AND created_at > %(now)s - interval '7 days 1 hour' AND created_at <= %(now)s""",
        {"c": customer_id, "now": now},
    ).fetchone()
    hourly = sent["week"] / (7 * 24)
    if sent["last_hour"] >= t["send_spike_min"] and sent["last_hour"] >= t["send_spike_factor"] * max(hourly, 1):
        raised.append(
            raise_alert(
                conn,
                customer_id,
                "send.spike",
                f"{sent['last_hour']} messages sent in the last hour, against about {hourly:.0f} an hour last week.",
                dedupe=f"send:{int(now.timestamp()) // 3600}",
                evidence={"last_hour": sent["last_hour"], "hourly_average": round(hourly, 1)},
            )
        )

    for r in conn.execute(
        """SELECT meter,
                  coalesce(sum(quantity) FILTER (WHERE at > %(now)s - interval '1 day'), 0) AS today,
                  coalesce(sum(quantity) FILTER (WHERE at <= %(now)s - interval '1 day'), 0) AS week
           FROM usage_records WHERE customer_id = %(c)s AND at > %(now)s - interval '8 days' AND at <= %(now)s
           GROUP BY meter""",
        {"c": customer_id, "now": now},
    ).fetchall():
        today, daily = float(r["today"]), float(r["week"]) / 7
        if today >= t["usage_jump_min"] and today >= t["usage_jump_factor"] * max(daily, 1):
            raised.append(
                raise_alert(
                    conn,
                    customer_id,
                    "usage.jump",
                    f"Use of {r['meter']} in the last day is {today:g}, against about {daily:.0f} a day last week.",
                    dedupe=f"usage:{r['meter']}:{now:%Y%m%d}",
                    evidence={"meter": r["meter"], "today": today, "daily_average": round(daily, 1)},
                )
            )
    return [a for a in raised if a]


# ---- the periodic job ---------------------------------------------------------------------------


def ensure_tick(conn: psycopg.Connection) -> None:
    bucket = int(dt.datetime.now(dt.UTC).timestamp()) // TICK_S
    jobs.enqueue(conn, "enterprise.tick", {}, dedupe_key=f"enterprise.tick:{bucket}")


@jobs.handler("enterprise.tick")
def _tick(conn: psycopg.Connection, job: dict):
    from . import governance

    for c in conn.execute("SELECT id FROM customers").fetchall():
        try:
            with conn.transaction():
                watch(conn, c["id"])
                governance.schedule_retention(conn, c["id"])
        except Exception:  # noqa: BLE001 - one business must not stop the others
            log.exception("enterprise watch failed for %s", c["id"])
    conn.execute("DELETE FROM commai_exports WHERE expires_at < now()")
    bucket = int(dt.datetime.now(dt.UTC).timestamp()) // TICK_S + 1
    jobs.enqueue(conn, "enterprise.tick", {}, dedupe_key=f"enterprise.tick:{bucket}", delay_s=TICK_S)
    return None
