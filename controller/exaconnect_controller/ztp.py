"""Zero-touch installation of ExaCarib boxes (ADR 0050).

A box that has not joined Connect calls ``POST /api/v1/ztp/hello`` on the portal's
public address with its serial number, an Ed25519 public key it made on first
boot, the time, and a signature over ``exacarib-ztp-v1\\n<serial>\\n<time>``.

- The first key seen for a serial is kept; a later call with another key is
  refused and shown to staff as a conflict (a copied serial, or a box replaced),
  until staff release the serial.
- A serial nobody has assigned is listed for staff, and the box is told to wait.
- Once staff assign the serial to a customer's site, the answer carries a fresh
  one-time enrolment token, the agent gateway's address and the CA fingerprint:
  exactly what the box console's join step takes. The box enrols as any agent does.

The serial and key prove which box is calling, not that it may join: only a staff
assignment does that. Tokens are short-lived and at most one is issued a minute.
"""

from __future__ import annotations

import base64
import datetime as dt
import re
from typing import Any

import psycopg
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import audit, inventory

SIGN_WHAT = "exacarib-ztp-v1"
SKEW = dt.timedelta(minutes=10)
TOKEN_EVERY = dt.timedelta(minutes=1)
TOKEN_TTL_HOURS = 1
RETRY_S = 30
_CLEAN = re.compile(r"[^A-Z0-9._-]")


class ZTPError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def normalise(serial: str) -> str:
    """The same as the box's NormaliseSerial, so a typed serial matches."""
    s = "-".join((serial or "").upper().split())
    return _CLEAN.sub("", s)[:64]


def key_id(public_key: str | None) -> str | None:
    """The short form of a box key the box console shows."""
    if not public_key:
        return None
    b = public_key.rstrip("=")
    return f"{b[:8]}…{b[-4:]}"


def _verify(serial: str, public_key: str, at: str, signature: str) -> None:
    try:
        raw = base64.b64decode(public_key, validate=True)
        sig = base64.b64decode(signature, validate=True)
        Ed25519PublicKey.from_public_bytes(raw).verify(sig, f"{SIGN_WHAT}\n{serial}\n{at}".encode())
    except (ValueError, InvalidSignature):
        raise ZTPError(403, "The box's signature is not right.") from None
    try:
        when = dt.datetime.fromisoformat(at)
    except ValueError:
        raise ZTPError(400, "The time is not valid.") from None
    if when.tzinfo is None or abs(dt.datetime.now(dt.UTC) - when) > SKEW:
        raise ZTPError(403, "The box's clock is more than 10 minutes out; it needs the right time (NTP).")


def hello(conn: psycopg.Connection, body: dict, ip: str, agent_url: str, ca_fingerprint: str) -> dict[str, Any]:
    serial = normalise(body["serial"])
    if not serial or serial != body["serial"]:
        raise ZTPError(400, "The serial number is not valid.")
    _verify(serial, body["public_key"], body["at"], body["signature"])
    now = dt.datetime.now(dt.UTC)
    row = conn.execute("SELECT * FROM boxes WHERE serial = %s FOR UPDATE", (serial,)).fetchone()
    if row is None:
        conn.execute(
            """INSERT INTO boxes (serial, public_key, mac, model, version, last_ip, first_seen, last_seen, added_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'zero-touch')""",
            (serial, body["public_key"], body.get("mac"), body.get("model"), body.get("version"), ip, now, now),
        )
        return {
            "state": "waiting",
            "detail": "Waiting for ExaCarib to assign this box to a location.",
            "retry_s": RETRY_S,
        }
    if row["public_key"] and row["public_key"] != body["public_key"]:
        if row["key_conflict"] != body["public_key"]:
            conn.execute("UPDATE boxes SET key_conflict = %s WHERE serial = %s", (body["public_key"], serial))
            audit.record(conn, "zero-touch", "box.key_conflict", serial, row["customer_id"], {"ip": ip})
        raise ZTPError(403, "Another box already called home with this serial number. ExaCarib support can release it.")
    conn.execute(
        """UPDATE boxes SET public_key = %s, mac = coalesce(%s, mac), model = coalesce(%s, model),
                  version = coalesce(%s, version), last_ip = %s, first_seen = coalesce(first_seen, %s), last_seen = %s
           WHERE serial = %s""",
        (body["public_key"], body.get("mac"), body.get("model"), body.get("version"), ip, now, now, serial),
    )
    if row["site_id"] is None:
        return {
            "state": "waiting",
            "detail": "Waiting for ExaCarib to assign this box to a location.",
            "retry_s": RETRY_S,
        }
    site = conn.execute(
        "SELECT id, name, customer_id FROM sites WHERE id = %s AND kind = 'site'", (row["site_id"],)
    ).fetchone()
    if site is None:
        return {"state": "waiting", "detail": "The location this box was assigned to is gone.", "retry_s": RETRY_S}
    if row["token_at"] is not None and now - row["token_at"] < TOKEN_EVERY:
        return {"state": "waiting", "detail": "Joining; trying again shortly.", "retry_s": RETRY_S}
    token, _ = inventory.issue_token(conn, site["id"], f"zero-touch:{serial}", ttl_hours=TOKEN_TTL_HOURS)
    conn.execute("UPDATE boxes SET token_at = %s WHERE serial = %s", (now, serial))
    return {
        "state": "assigned",
        "site": site["name"],
        "controller": agent_url,
        "token": token,
        "ca_fingerprint": ca_fingerprint,
    }


def seen_in_telemetry(conn: psycopg.Connection, node: Any, serial: str) -> None:
    """A box that reports its serial with telemetry is listed against its node,
    however it joined. The first report after joining marks when it joined."""
    serial = normalise(serial)
    if not serial:
        return
    site = conn.execute("SELECT site_id FROM nodes WHERE id = %s", (node.id,)).fetchone()
    conn.execute(
        """INSERT INTO boxes (serial, customer_id, site_id, node_id, first_seen, last_seen, joined_at, added_by)
           VALUES (%(s)s, %(c)s, %(site)s, %(n)s, now(), now(), now(), 'box console')
           ON CONFLICT (serial) DO UPDATE SET node_id = %(n)s, customer_id = %(c)s, site_id = %(site)s,
             last_seen = now(), joined_at = CASE WHEN boxes.node_id IS DISTINCT FROM %(n)s THEN now()
                                                 ELSE coalesce(boxes.joined_at, now()) END""",
        {"s": serial, "c": node.customer_id, "site": site["site_id"] if site else None, "n": node.id},
    )
    # Another serial that still points at this node was the box this one replaced.
    conn.execute("UPDATE boxes SET node_id = NULL WHERE node_id = %s AND serial <> %s", (node.id, serial))


LIST_SQL = """
SELECT b.serial, b.public_key, b.key_conflict IS NOT NULL AS key_conflict, b.note, b.mac, b.model, b.version,
       b.last_ip, b.first_seen, b.last_seen, b.added_by, b.added_at, b.assigned_by, b.assigned_at, b.joined_at,
       b.customer_id, c.name AS organisation, b.site_id, s.name AS site, sl.name AS location,
       b.node_id, n.last_seen AS node_seen, n.agent_version
FROM boxes b
LEFT JOIN customers c ON c.id = b.customer_id
LEFT JOIN sites s ON s.id = b.site_id
LEFT JOIN org_locations sl ON sl.id = s.org_location_id
LEFT JOIN nodes n ON n.id = b.node_id
"""


def state(r: dict) -> str:
    now = dt.datetime.now(dt.UTC)
    if r["key_conflict"]:
        return "conflict"
    if r["node_id"]:
        return "online" if r["node_seen"] and now - r["node_seen"] < dt.timedelta(minutes=2) else "offline"
    if r["site_id"]:
        return "assigned"
    return "waiting"


def listing(conn: psycopg.Connection, serial: str | None = None) -> list[dict]:
    sql = LIST_SQL + (" WHERE b.serial = %s" if serial else "") + " ORDER BY b.last_seen DESC NULLS LAST, b.serial"
    rows = conn.execute(sql, (serial,) if serial else ()).fetchall()
    out = []
    for r in rows:
        r["key_id"] = key_id(r.pop("public_key"))
        r["state"] = state(r)
        out.append(r)
    return out
