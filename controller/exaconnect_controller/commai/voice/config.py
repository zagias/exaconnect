"""The phone system: adds, moves and changes (ADR 0021).

Every change, from a form, a CSV, a scheduled job, a sentence or an order, is
a list of operations ("ops") applied by `apply()`:

- each op runs in its own savepoint, so every row of a bulk change is
  checked and all errors come back together; if any op fails, nothing applies;
- the price impact is worked out from the ops; if the bill changes, the
  caller must hold spend permission and must have seen that exact price;
- the provider is only called once all checks pass, with idempotency keys
  derived from the version, so a retried save never orders a second number;
- a saved change becomes a numbered version with a full snapshot; the
  routing part (ring groups, queues, hours, menus, AI rules and where each
  number rings) can be rolled back to any version;
- a move updates the user's emergency address, and that of their numbers,
  from the new site.

Ops (fields in brackets are optional):
  add_site name [timezone address_line1 address_line2 city island country postcode]
  update_site site [same fields]
  add_user name [extension email mobile site team portal_email]
  update_user user [name email mobile portal_email]
  move_user user [site team extension]
  remove_user user
  add_number [area target_type target]
  remove_number number
  assign_number number target_type [target]
  add_device user kind [mac model]
  remove_device device
  save_ring_group name extension [id strategy members ring_seconds]
  delete_ring_group id
  save_queue name extension [id strategy members max_wait_s]
  delete_queue id
  save_hours name [id timezone schedule holidays]
  delete_hours id
  save_menu name extension [id greeting options hours closed_target]
  delete_menu id
  save_ai_rule name condition [id hours number fallback enabled]
  delete_ai_rule id
`user` is a voice user id or an extension; `site`/`team`/`hours` an id or a
name; `number` an id or an E.164 number; `members` ids or extensions.
"""

from __future__ import annotations

import re
import secrets
import uuid
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ... import audit
from ...security import token_hash
from .. import events, jobs
from . import billing, countries, emergency
from . import provider as providers
from .common import VoiceError, digits

events.register(
    "voice.config_changed",
    "voice.config_rolled_back",
    "voice.user_moved",
    "voice.change_scheduled",
    "voice.change_failed",
)

SITE_FIELDS = ("timezone", "address_line1", "address_line2", "city", "island", "country", "postcode")
ADDRESS_FIELDS = ("address_line1", "address_line2", "city", "island", "country", "postcode")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
TARGET_TYPES = ("none", "user", "ring_group", "queue", "menu", "ai")
MAC_RE = re.compile(r"^[0-9a-f]{12}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class OpError(Exception):
    pass


def _uuid(v: Any) -> str | None:
    try:
        return str(uuid.UUID(str(v)))
    except (ValueError, TypeError, AttributeError):
        return None


def norm_mac(mac: str) -> str:
    m = re.sub(r"[^0-9a-fA-F]", "", mac or "").lower()
    if not MAC_RE.match(m):
        raise OpError("A desk phone needs its 12-digit MAC address, like 00:15:65:aa:bb:cc.")
    return m


def norm_e164(number: str) -> str:
    d = digits(number)
    if not str(number).strip().startswith("+"):  # "+" means it already has its country code
        if len(d) == 7:  # a local Trinidad number
            d = "1868" + d
        if len(d) == 10:
            d = "1" + d
    if not 8 <= len(d) <= 15:
        raise OpError(f"{number} is not a phone number in international format.")
    return "+" + d


# ---- lookups -------------------------------------------------------------------------


def _site(conn, cid, ref, required=True) -> dict | None:
    if ref in (None, ""):
        if required:
            raise OpError("Choose a site.")
        return None
    u = _uuid(ref)
    row = conn.execute(
        "SELECT * FROM voice_sites WHERE customer_id = %s AND (id::text = %s OR lower(name) = lower(%s))",
        (cid, u or "", str(ref)),
    ).fetchone()
    if row is None:
        raise OpError(f"There is no site called {ref}.")
    return row


def _team(conn, cid, ref) -> dict | None:
    if ref in (None, ""):
        return None
    u = _uuid(ref)
    row = conn.execute(
        "SELECT * FROM commai_teams WHERE customer_id = %s AND (id::text = %s OR lower(name) = lower(%s))",
        (cid, u or "", str(ref)),
    ).fetchone()
    if row is None:
        raise OpError(f"There is no team called {ref}.")
    return row


def _user(conn, cid, ref) -> dict:
    if ref in (None, ""):
        raise OpError("Say which user (an extension or id).")
    u = _uuid(ref)
    row = conn.execute(
        """SELECT * FROM voice_users WHERE customer_id = %s AND status = 'active'
           AND (id::text = %s OR extension = %s)""",
        (cid, u or "", str(ref)),
    ).fetchone()
    if row is None:
        raise OpError(f"There is no active user with extension or id {ref}.")
    return row


def _number(conn, cid, ref) -> dict:
    u = _uuid(ref)
    e164 = ""
    if not u:
        try:
            e164 = norm_e164(str(ref))
        except OpError:
            e164 = str(ref)
    row = conn.execute(
        """SELECT * FROM voice_numbers WHERE customer_id = %s AND status <> 'removed'
           AND (id::text = %s OR e164 = %s)""",
        (cid, u or "", e164),
    ).fetchone()
    if row is None:
        raise OpError(f"There is no number {ref} on your account.")
    return row


def _hours(conn, cid, ref) -> dict | None:
    if ref in (None, ""):
        return None
    u = _uuid(ref)
    row = conn.execute(
        "SELECT * FROM voice_hours WHERE customer_id = %s AND (id::text = %s OR lower(name) = lower(%s))",
        (cid, u or "", str(ref)),
    ).fetchone()
    if row is None:
        raise OpError(f"There are no business hours called {ref}.")
    return row


def _target(conn, cid, kind: str, ref) -> str | None:
    """Resolve a routing target to its id."""
    if kind not in TARGET_TYPES:
        raise OpError(f"Calls can go to: {', '.join(TARGET_TYPES)}.")
    if kind in ("none", "ai"):
        return None
    if kind == "user":
        return str(_user(conn, cid, ref)["id"])
    table = {"ring_group": "voice_ring_groups", "queue": "voice_queues", "menu": "voice_menus"}[kind]
    u = _uuid(ref)
    row = conn.execute(
        f"SELECT id FROM {table} WHERE customer_id = %s"
        " AND (id::text = %s OR lower(name) = lower(%s) OR extension = %s)",
        (cid, u or "", str(ref or ""), str(ref or "")),
    ).fetchone()
    if row is None:
        raise OpError(f"There is no {kind.replace('_', ' ')} {ref}.")
    return str(row["id"])


def _members(conn, cid, refs) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for r in refs or []:
        uid = _user(conn, cid, r)["id"]
        if uid not in out:
            out.append(uid)
    return out


def _check_ext(conn, cid, ext: str, exclude: Any = None) -> str:
    ext = str(ext or "").strip()
    if not re.fullmatch(r"\d{2,6}", ext):
        raise OpError("An extension is 2 to 6 digits.")
    if ext in billing.EMERGENCY or ext.startswith("9") or ext in emergency.business_numbers(conn, cid):
        raise OpError("Extensions can't start with 9 or be an emergency number.")
    ex = str(exclude) if exclude else ""
    for table, cond in (
        ("voice_users", "status = 'active'"),
        ("voice_ring_groups", "true"),
        ("voice_queues", "true"),
        ("voice_menus", "true"),
    ):
        if conn.execute(
            f"SELECT 1 FROM {table} WHERE customer_id = %s AND extension = %s AND {cond} AND id::text <> %s",
            (cid, ext, ex),
        ).fetchone():
            raise OpError(f"Extension {ext} is already in use.")
    return ext


def _next_ext(conn, cid) -> str:
    for n in range(200, 9000):
        try:
            return _check_ext(conn, cid, str(n))
        except OpError:
            continue
    raise OpError("No free extensions left.")


def site_address(site: dict | None) -> dict:
    if not site:
        return {}
    return {"site_id": str(site["id"]), "site": site["name"], **{k: site[k] for k in ADDRESS_FIELDS}}


def _portal_user(conn, cid, email) -> Any:
    if not email:
        return None
    row = conn.execute(
        "SELECT id FROM users WHERE lower(email) = lower(%s) AND customer_id = %s", (email, cid)
    ).fetchone()
    if row is None:
        raise OpError(f"No portal account {email} in your company.")
    return row["id"]


def _schedule(sched: dict) -> dict:
    out = {}
    for day, spans in (sched or {}).items():
        if day not in DAYS:
            raise OpError(f"Days are {', '.join(DAYS)}.")
        clean = []
        for span in spans or []:
            if len(span) != 2 or not all(TIME_RE.match(str(t)) for t in span) or span[0] >= span[1]:
                raise OpError(f"Opening times on {day} must be HH:MM to a later HH:MM.")
            clean.append([span[0], span[1]])
        out[day] = sorted(clean)
    return out


# ---- the ops -------------------------------------------------------------------------


class _Run:
    def __init__(self, conn, cid, actor, version, dry_run):
        self.conn, self.cid, self.actor, self.version, self.dry_run = conn, cid, actor, version, dry_run
        self.counts = {"users": 0, "numbers": 0, "desk_phones": 0, "softphones": 0, "new_numbers": 0}
        self.after: list = []  # provider calls, run once every check has passed
        self.results: list[dict] = []
        self.events: list[tuple] = []

    # sites
    def add_site(self, op):
        name = str(op.get("name", "")).strip()
        if not name:
            raise OpError("A site needs a name.")
        vals = {k: str(op.get(k) or "")[:200] for k in SITE_FIELDS}
        vals["timezone"] = vals["timezone"] or "America/Port_of_Spain"
        vals["country"] = (vals["country"] or "TT").upper()[:2]
        if not vals["address_line1"]:
            raise OpError("A site needs its street address for emergency calls.")
        row = self.conn.execute(
            f"""INSERT INTO voice_sites (customer_id, name, {", ".join(SITE_FIELDS)})
                VALUES (%s, %s, {", ".join(["%s"] * len(SITE_FIELDS))}) RETURNING id""",
            (self.cid, name[:120], *[vals[k] for k in SITE_FIELDS]),
        ).fetchone()
        self.after.append(("validate_site", str(row["id"])))  # checked with the provider (ADR 0027)
        return {"site_id": str(row["id"])}

    def update_site(self, op):
        site = _site(self.conn, self.cid, op.get("site"))
        vals = {k: str(op[k])[:200] for k in SITE_FIELDS if k in op}
        if "name" in op:
            vals["name"] = str(op["name"]).strip()[:120]
        if not vals:
            return {}
        sets = ", ".join(f"{k} = %s" for k in vals)
        address_changed = any(k in ADDRESS_FIELDS and vals[k] != site[k] for k in vals)
        if address_changed:
            sets += ", emergency_status = 'pending'"
        site = self.conn.execute(
            f"UPDATE voice_sites SET {sets} WHERE id = %s RETURNING *", (*vals.values(), site["id"])
        ).fetchone()
        if address_changed:
            self.after.append(("validate_site", str(site["id"])))
        if address_changed or "name" in vals:
            addr = Jsonb(site_address(site))
            self.conn.execute(
                "UPDATE voice_users SET emergency_address = %s WHERE site_id = %s AND status = 'active'",
                (addr, site["id"]),
            )
            self.conn.execute(
                "UPDATE voice_numbers SET emergency_address = %s WHERE site_id = %s AND status <> 'removed'",
                (addr, site["id"]),
            )
        return {"site_id": str(site["id"])}

    # users
    def add_user(self, op):
        name = str(op.get("name", "")).strip()
        if not name:
            raise OpError("A user needs a name.")
        ext = (
            _check_ext(self.conn, self.cid, op["extension"]) if op.get("extension") else _next_ext(self.conn, self.cid)
        )
        site = _site(self.conn, self.cid, op.get("site"))
        team = _team(self.conn, self.cid, op.get("team"))
        portal = _portal_user(self.conn, self.cid, op.get("portal_email"))
        row = self.conn.execute(
            """INSERT INTO voice_users (customer_id, user_id, name, email, mobile, extension, site_id, team_id,
                 sip_password, emergency_address, order_ref, billing_from)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       CASE WHEN %s::text IS NULL THEN now() END) RETURNING id, extension""",
            (
                self.cid,
                portal,
                name[:120],
                str(op.get("email") or "")[:200],
                norm_e164(op["mobile"]) if op.get("mobile") else "",
                ext,
                site["id"],
                team["id"] if team else None,
                secrets.token_urlsafe(18),
                Jsonb(site_address(site)),
                op.get("order_ref"),
                op.get("order_ref"),
            ),
        ).fetchone()
        if not op.get("order_ref"):  # an order's users are billed from activation
            self.counts["users"] += 1
        return {"voice_user_id": str(row["id"]), "extension": row["extension"]}

    def update_user(self, op):
        vu = _user(self.conn, self.cid, op.get("user"))
        vals = {}
        if "name" in op:
            vals["name"] = str(op["name"]).strip()[:120] or vu["name"]
        if "email" in op:
            vals["email"] = str(op["email"] or "")[:200]
        if "mobile" in op:
            vals["mobile"] = norm_e164(op["mobile"]) if op["mobile"] else ""
        if "portal_email" in op:
            vals["user_id"] = _portal_user(self.conn, self.cid, op["portal_email"])
        if vals:
            self.conn.execute(
                f"UPDATE voice_users SET {', '.join(f'{k} = %s' for k in vals)} WHERE id = %s",
                (*vals.values(), vu["id"]),
            )
        return {"voice_user_id": str(vu["id"])}

    def move_user(self, op):
        vu = _user(self.conn, self.cid, op.get("user"))
        sets, args = [], []
        moved_site = None
        if op.get("site") not in (None, ""):
            moved_site = _site(self.conn, self.cid, op["site"])
            sets += ["site_id = %s", "emergency_address = %s"]
            args += [moved_site["id"], Jsonb(site_address(moved_site))]
        if "team" in op:
            team = _team(self.conn, self.cid, op["team"])
            sets.append("team_id = %s")
            args.append(team["id"] if team else None)
        if op.get("extension"):
            sets.append("extension = %s")
            args.append(_check_ext(self.conn, self.cid, op["extension"], exclude=vu["id"]))
        if not sets:
            raise OpError("Say where to move the user: a site, team or extension.")
        self.conn.execute(f"UPDATE voice_users SET {', '.join(sets)} WHERE id = %s", (*args, vu["id"]))
        out = {"voice_user_id": str(vu["id"])}
        if moved_site:
            # The emergency address follows the person, and so do their numbers.
            nums = self.conn.execute(
                """UPDATE voice_numbers SET site_id = %s, emergency_address = %s
                   WHERE customer_id = %s AND target_type = 'user' AND target_id = %s AND status <> 'removed'
                   RETURNING e164""",
                (moved_site["id"], Jsonb(site_address(moved_site)), self.cid, vu["id"]),
            ).fetchall()
            out["emergency_address"] = site_address(moved_site)
            out["numbers_updated"] = [n["e164"] for n in nums]
            self.events.append(
                (
                    "voice.user_moved",
                    {"voice_user_id": str(vu["id"]), "site": moved_site["name"], "numbers": out["numbers_updated"]},
                )
            )
            # The new site's address is checked if it hasn't been, and outbound
            # calling follows the island's rules (ADR 0027).
            self.after.append(("moved", str(vu["id"]), str(moved_site["id"])))
        return out

    def remove_user(self, op):
        vu = _user(self.conn, self.cid, op.get("user"))
        self.conn.execute(
            """UPDATE voice_users SET status = 'removed', removed_at = now(),
                 billing_until = CASE WHEN billing_from IS NULL THEN NULL ELSE now() END WHERE id = %s""",
            (vu["id"],),
        )
        for table in ("voice_ring_groups", "voice_queues"):
            self.conn.execute(
                f"UPDATE {table} SET members = array_remove(members, %s) WHERE customer_id = %s", (vu["id"], self.cid)
            )
        self.conn.execute(
            "UPDATE voice_numbers SET target_type = 'none', target_id = NULL"
            " WHERE target_type = 'user' AND target_id = %s",
            (vu["id"],),
        )
        self.conn.execute("UPDATE voice_devices SET status = 'removed' WHERE voice_user_id = %s", (vu["id"],))
        if vu["billing_from"] is not None:
            self.counts["users"] -= 1
        return {"voice_user_id": str(vu["id"])}

    # numbers
    def add_number(self, op):
        target_type = op.get("target_type") or "none"
        target_id = _target(self.conn, self.cid, target_type, op.get("target"))
        site = _site(self.conn, self.cid, op.get("site"), required=False)
        if site is None and target_type == "user":
            vu = self.conn.execute("SELECT site_id FROM voice_users WHERE id = %s", (target_id,)).fetchone()
            site = _site(self.conn, self.cid, str(vu["site_id"]), required=False) if vu["site_id"] else None
        country = str(op.get("country") or "").upper()
        if country or providers.get().live:
            # Real numbers by country need that country switched on (ADR 0027).
            try:
                c = countries.require(self.conn, country, self.cid)
                area = countries.check_area(c, op.get("area"))
            except VoiceError as e:
                raise OpError(str(e)) from e
        else:
            area = op.get("area", "868")
        idx = len(self.results)
        placeholder = f"pending:{self.version}:{idx}"
        row = self.conn.execute(
            """INSERT INTO voice_numbers (customer_id, e164, source, status, target_type, target_id, site_id,
                 emergency_address, country) VALUES (%s, %s, 'new', 'pending', %s, %s, %s, %s, %s) RETURNING id""",
            (
                self.cid,
                placeholder,
                target_type,
                target_id,
                site["id"] if site else None,
                Jsonb(site_address(site)),
                country or "TT",
            ),
        ).fetchone()
        self.counts["numbers"] += 1
        self.counts["new_numbers"] += 1
        self.after.append(
            (
                "order_number",
                str(row["id"]),
                f"{self.cid}:v{self.version}:n{idx}",
                area,
                country or None,
                op.get("e164"),
            )
        )
        return {"number_id": str(row["id"]), "e164": "+1 868 555 01xx (given when saved)" if self.dry_run else None}

    def remove_number(self, op):
        num = _number(self.conn, self.cid, op.get("number"))
        self.conn.execute(
            """UPDATE voice_numbers SET status = 'removed', removed_at = now(),
                 billing_until = CASE WHEN billing_from IS NULL THEN NULL ELSE now() END WHERE id = %s""",
            (num["id"],),
        )
        if num["billing_from"] is not None:
            self.counts["numbers"] -= 1
        self.after.append(("release", num["e164"]))
        return {"number_id": str(num["id"]), "e164": num["e164"]}

    def assign_number(self, op):
        num = _number(self.conn, self.cid, op.get("number"))
        kind = op.get("target_type") or "none"
        target_id = _target(self.conn, self.cid, kind, op.get("target"))
        self.conn.execute(
            "UPDATE voice_numbers SET target_type = %s, target_id = %s WHERE id = %s", (kind, target_id, num["id"])
        )
        self.after.append(("refresh_number", str(num["id"])))
        return {"number_id": str(num["id"])}

    # devices
    def add_device(self, op):
        vu = _user(self.conn, self.cid, op.get("user"))
        kind = op.get("kind")
        if kind not in ("desk", "softphone"):
            raise OpError("A device is a desk phone or a softphone.")
        mac = norm_mac(op.get("mac", "")) if kind == "desk" else None
        if (
            mac
            and self.conn.execute(
                "SELECT 1 FROM voice_devices WHERE mac = %s AND status <> 'removed'", (mac,)
            ).fetchone()
        ):
            raise OpError(f"A desk phone with MAC {mac} is already set up.")
        token = secrets.token_urlsafe(24)
        row = self.conn.execute(
            """INSERT INTO voice_devices (customer_id, voice_user_id, kind, mac, model, token_hash, token_expires_at,
                 order_ref) VALUES (%s, %s, %s, %s, %s, %s,
                 CASE WHEN %s = 'softphone' THEN now() + interval '7 days' END, %s) RETURNING id""",
            (
                self.cid,
                vu["id"],
                kind,
                mac,
                str(op.get("model") or "")[:60],
                token_hash(token),
                kind,
                op.get("order_ref"),
            ),
        ).fetchone()
        self.counts["desk_phones" if kind == "desk" else "softphones"] += 1
        self.after.append(("device_fee", str(row["id"]), kind, vu))
        return {"device_id": str(row["id"]), "kind": kind, "mac": mac, "token": None if self.dry_run else token}

    def remove_device(self, op):
        u = _uuid(op.get("device"))
        row = (
            self.conn.execute(
                """UPDATE voice_devices SET status = 'removed', token_hash = '' WHERE id = %s AND customer_id = %s
               AND status <> 'removed' RETURNING id""",
                (u, self.cid),
            ).fetchone()
            if u
            else None
        )
        if row is None:
            raise OpError("There is no such device.")
        return {"device_id": str(row["id"])}

    # routing
    def _save(self, table, op, cols: dict):
        rid = _uuid(op.get("id"))
        name = str(op.get("name", "")).strip()[:80]
        if not name:
            raise OpError("Give it a name.")
        cols = {"name": name, **cols}
        if rid:
            row = self.conn.execute(
                f"UPDATE {table} SET {', '.join(f'{k} = %s' for k in cols)} WHERE id = %s AND customer_id = %s"
                " RETURNING id",
                (*cols.values(), rid, self.cid),
            ).fetchone()
            if row is None:
                raise OpError("It no longer exists.")
        else:
            row = self.conn.execute(
                f"INSERT INTO {table} (customer_id, {', '.join(cols)}) VALUES (%s, {', '.join(['%s'] * len(cols))})"
                " RETURNING id",
                (self.cid, *cols.values()),
            ).fetchone()
        return {"id": str(row["id"])}

    def _delete(self, table, op, kind=None):
        rid = _uuid(op.get("id"))
        row = (
            self.conn.execute(
                f"DELETE FROM {table} WHERE id = %s AND customer_id = %s RETURNING id", (rid, self.cid)
            ).fetchone()
            if rid
            else None
        )
        if row is None:
            raise OpError("It no longer exists.")
        if kind:
            self.conn.execute(
                "UPDATE voice_numbers SET target_type = 'none', target_id = NULL"
                " WHERE target_type = %s AND target_id = %s",
                (kind, rid),
            )
        return {"id": rid}

    def save_ring_group(self, op):
        strategy = op.get("strategy", "simultaneous")
        if strategy not in ("simultaneous", "sequential"):
            raise OpError("A ring group rings everyone at once (simultaneous) or in turn (sequential).")
        secs = int(op.get("ring_seconds", 20))
        if not 5 <= secs <= 120:
            raise OpError("Ring for 5 to 120 seconds.")
        return self._save(
            "voice_ring_groups",
            op,
            {
                "extension": _check_ext(self.conn, self.cid, op.get("extension"), exclude=_uuid(op.get("id"))),
                "strategy": strategy,
                "members": _members(self.conn, self.cid, op.get("members")),
                "ring_seconds": secs,
            },
        )

    def delete_ring_group(self, op):
        return self._delete("voice_ring_groups", op, "ring_group")

    def save_queue(self, op):
        strategy = op.get("strategy", "longest-idle-agent")
        if strategy not in ("longest-idle-agent", "ring-all", "round-robin"):
            raise OpError("Queue strategies: longest-idle-agent, ring-all, round-robin.")
        wait = int(op.get("max_wait_s", 300))
        if not 30 <= wait <= 3600:
            raise OpError("Callers can wait 30 seconds to an hour.")
        return self._save(
            "voice_queues",
            op,
            {
                "extension": _check_ext(self.conn, self.cid, op.get("extension"), exclude=_uuid(op.get("id"))),
                "strategy": strategy,
                "members": _members(self.conn, self.cid, op.get("members")),
                "max_wait_s": wait,
            },
        )

    def delete_queue(self, op):
        return self._delete("voice_queues", op, "queue")

    def save_hours(self, op):
        holidays = sorted({str(d)[:10] for d in op.get("holidays") or []})
        for d in holidays:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                raise OpError("Holidays are dates like 2026-12-25.")
        return self._save(
            "voice_hours",
            op,
            {
                "timezone": str(op.get("timezone") or "America/Port_of_Spain"),
                "schedule": Jsonb(_schedule(op.get("schedule") or {})),
                "holidays": Jsonb(holidays),
            },
        )

    def delete_hours(self, op):
        rid = _uuid(op.get("id"))
        if (
            rid
            and self.conn.execute(
                "SELECT 1 FROM voice_menus WHERE hours_id = %s UNION SELECT 1 FROM voice_ai_rules WHERE hours_id = %s",
                (rid, rid),
            ).fetchone()
        ):
            raise OpError("A menu or AI rule still uses these hours.")
        return self._delete("voice_hours", op)

    def _target_spec(self, spec) -> dict:
        if not spec:
            return {}
        kind = spec.get("type", "none")
        tid = _target(self.conn, self.cid, kind, spec.get("id") or spec.get("target"))
        return {"type": kind, "id": tid} if kind != "none" else {}

    def save_menu(self, op):
        options = {}
        for key, spec in (op.get("options") or {}).items():
            if not re.fullmatch(r"[0-9*#]", str(key)):
                raise OpError("Menu options are a single key: 0-9, * or #.")
            options[str(key)] = self._target_spec(spec)
        hours = _hours(self.conn, self.cid, op.get("hours"))
        return self._save(
            "voice_menus",
            op,
            {
                "extension": _check_ext(self.conn, self.cid, op.get("extension"), exclude=_uuid(op.get("id"))),
                "greeting": str(op.get("greeting") or "")[:1000],
                "options": Jsonb(dict(sorted(options.items()))),
                "hours_id": hours["id"] if hours else None,
                "closed_target": Jsonb(self._target_spec(op.get("closed_target"))),
            },
        )

    def delete_menu(self, op):
        return self._delete("voice_menus", op, "menu")

    def save_ai_rule(self, op):
        cond = op.get("condition")
        if cond not in ("after_hours", "no_answer", "busy", "always"):
            raise OpError("AI rules apply after hours, on no answer, when busy, or always.")
        hours = _hours(self.conn, self.cid, op.get("hours"))
        if cond == "after_hours" and not hours:
            raise OpError("Choose the business hours for an after-hours rule.")
        num = _number(self.conn, self.cid, op["number"]) if op.get("number") else None
        fallback = op.get("fallback", "voicemail")
        if fallback not in ("voicemail", "ring_group", "queue"):
            raise OpError("If the AI agent is unavailable, calls go to voicemail, a ring group or a queue.")
        return self._save(
            "voice_ai_rules",
            op,
            {
                "condition": cond,
                "hours_id": hours["id"] if hours else None,
                "number_id": num["id"] if num else None,
                "fallback": fallback,
                "enabled": bool(op.get("enabled", True)),
            },
        )

    def delete_ai_rule(self, op):
        return self._delete("voice_ai_rules", op)


OPS = {name for name in dir(_Run) if not name.startswith("_")}


# ---- snapshots, versions, diff -------------------------------------------------------


def snapshot(conn: psycopg.Connection, cid: Any) -> dict:
    """The whole phone system as plain data, in a stable order. Secrets are left out."""

    def rows(sql):
        return [
            {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in r.items()}
            for r in conn.execute(sql, (cid,)).fetchall()
        ]

    return {
        "sites": rows(
            f"SELECT id, name, {', '.join(SITE_FIELDS)}, emergency_status FROM voice_sites"
            " WHERE customer_id = %s ORDER BY name"
        ),
        "users": rows("""SELECT id, name, email, extension, site_id, team_id, user_id, mobile FROM voice_users
                         WHERE customer_id = %s AND status = 'active' ORDER BY extension"""),
        "numbers": rows("""SELECT id, e164, source, status, target_type, target_id, site_id FROM voice_numbers
                           WHERE customer_id = %s AND status <> 'removed' ORDER BY e164"""),
        "devices": rows("""SELECT id, voice_user_id, kind, mac, model, status FROM voice_devices
                           WHERE customer_id = %s AND status <> 'removed' ORDER BY kind, mac, id"""),
        "ring_groups": rows("""SELECT id, name, extension, strategy, members::text[] AS members, ring_seconds
                               FROM voice_ring_groups WHERE customer_id = %s ORDER BY extension"""),
        "queues": rows("""SELECT id, name, extension, strategy, members::text[] AS members, max_wait_s
                          FROM voice_queues WHERE customer_id = %s ORDER BY extension"""),
        "hours": rows(
            "SELECT id, name, timezone, schedule, holidays FROM voice_hours WHERE customer_id = %s ORDER BY name"
        ),
        "menus": rows("""SELECT id, name, extension, greeting, options, hours_id, closed_target FROM voice_menus
                         WHERE customer_id = %s ORDER BY extension"""),
        "ai_rules": rows("""SELECT id, name, condition, hours_id, number_id, fallback, enabled FROM voice_ai_rules
                            WHERE customer_id = %s ORDER BY name"""),
    }


LABEL = {
    "users": "extension",
    "ring_groups": "name",
    "queues": "name",
    "hours": "name",
    "menus": "name",
    "ai_rules": "name",
    "sites": "name",
    "numbers": "e164",
    "devices": "kind",
}


def diff(before: dict, after: dict) -> list[dict]:
    """What changed, per collection: added, removed and changed items."""
    out = []
    for coll in after:
        old = {r["id"]: r for r in before.get(coll, [])}
        new = {r["id"]: r for r in after[coll]}
        for rid, r in new.items():
            label = str(r.get(LABEL[coll], ""))
            if coll == "users":
                label = f"{r['name']} (ext {r['extension']})"
            if rid not in old:
                out.append({"area": coll, "change": "added", "item": label})
            elif r != old[rid]:
                fields = sorted(k for k in r if r[k] != old[rid].get(k))
                out.append({"area": coll, "change": "changed", "item": label, "fields": fields})
        for rid, r in old.items():
            if rid not in new:
                label = f"{r['name']} (ext {r['extension']})" if coll == "users" else str(r.get(LABEL[coll], ""))
                out.append({"area": coll, "change": "removed", "item": label})
    return out


def current_version(conn, cid) -> int:
    return conn.execute(
        "SELECT coalesce(max(version), 0) AS v FROM voice_config_versions WHERE customer_id = %s", (cid,)
    ).fetchone()["v"]


def _save_version(conn, cid, kind, summary, ops, snap, impact, actor) -> int:
    version = current_version(conn, cid) + 1
    conn.execute(
        """INSERT INTO voice_config_versions (customer_id, version, kind, summary, ops, snapshot, price_impact,
             created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (cid, version, kind, summary[:300], Jsonb(ops), Jsonb(snap), Jsonb(impact), actor),
    )
    jobs.enqueue(
        conn, "voice.render", {"customer_id": str(cid)}, customer_id=cid, dedupe_key=f"voice.render:{cid}:{version}"
    )
    return version


def _summary(ops: list[dict]) -> str:
    names = [op.get("op", "?").replace("_", " ") for op in ops]
    if len(names) <= 3:
        return ", ".join(names)
    return f"{len(names)} changes"


class _Rollback(Exception):
    pass


def apply(
    conn: psycopg.Connection,
    cid: Any,
    ops: list[dict],
    *,
    actor: str,
    kind: str = "change",
    summary: str = "",
    dry_run: bool = False,
    can_spend: bool = True,
    accepted_price: dict | None = None,
    check_price: bool = True,
) -> dict:
    """Check and apply a change. Returns {"ok", "errors", "price_impact",
    "diff", "results", "version"}. Raises VoiceError when the change can't be
    saved (403 without spend permission, 409 when the price wasn't seen)."""
    if not isinstance(ops, list) or not ops:
        raise VoiceError("There are no changes to make.", 422)
    if len(ops) > 2000:
        raise VoiceError("Up to 2,000 changes at once.", 422)
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('voice_config:' || %s))", (str(cid),))
    card = billing.card_at(conn, cid)
    before = snapshot(conn, cid)
    run = _Run(conn, cid, actor, current_version(conn, cid) + 1, dry_run)
    errors: list[dict] = []
    out: dict = {}
    try:
        with conn.transaction():
            for i, op in enumerate(ops):
                name = op.get("op") if isinstance(op, dict) else None
                if name not in OPS:
                    errors.append({"index": i, "op": name, "error": f"Unknown change {name!r}."})
                    run.results.append({})
                    continue
                try:
                    with conn.transaction():
                        run.results.append(getattr(run, name)(op) or {})
                except OpError as e:
                    errors.append({"index": i, "op": name, "error": str(e)})
                    run.results.append({})
                except psycopg.errors.UniqueViolation:
                    errors.append({"index": i, "op": name, "error": "That name or number is already in use."})
                    run.results.append({})
                except (psycopg.errors.DataError, ValueError, TypeError, KeyError) as e:
                    errors.append({"index": i, "op": name, "error": f"A value is missing or not valid ({e})."})
                    run.results.append({})
            impact = billing.price_impact(card, run.counts)
            after = snapshot(conn, cid)
            out = {
                "ok": not errors,
                "errors": errors,
                "price_impact": impact,
                "diff": diff(before, after),
                "results": run.results,
                "version": None,
            }
            if dry_run or errors:
                raise _Rollback
            if impact["changes_bill"] and check_price:
                if not can_spend:
                    raise VoiceError(
                        "This changes your bill. Ask someone in your company with spend permission to save it.", 403
                    )
                seen = accepted_price or {}
                if (seen.get("monthly_delta"), seen.get("one_time")) != (impact["monthly_delta"], impact["one_time"]):
                    raise VoiceError(
                        f"Check the price first: this adds {impact['monthly_delta']} a month and "
                        f"{impact['one_time']} once ({impact['currency']}).",
                        409,
                    )
            _after_checks(conn, cid, run)
            after = snapshot(conn, cid)
            out["diff"] = diff(before, after)
            out["version"] = _save_version(conn, cid, kind, summary or _summary(ops), ops, after, impact, actor)
            for etype, data in run.events:
                events.emit(conn, cid, etype, data)
            events.emit(
                conn,
                cid,
                "voice.config_changed",
                {"version": out["version"], "kind": kind, "changes": len(out["diff"])},
                str(out["version"]),
            )
            audit.record(
                conn,
                actor,
                "commai.voice.change",
                f"v{out['version']}",
                cid,
                {"kind": kind, "ops": len(ops), "price_impact": impact},
            )
    except _Rollback:
        pass
    return out


def _after_checks(conn, cid, run: _Run) -> None:
    """Provider calls and fees, once every op has passed its checks."""
    prov = providers.get()
    for item in run.after:
        if item[0] == "order_number":
            _, number_id, key, area, country, chosen = item
            got = (
                prov.order_number(conn, cid, key, area, country=country, e164=chosen)
                if country
                else prov.order_number(conn, cid, key, area)
            )
            check = prov.test_call(conn, got["e164"])
            conn.execute(
                "INSERT INTO voice_test_calls (customer_id, number_id, e164, ok, detail) VALUES (%s, %s, %s, %s, %s)",
                (cid, number_id, got["e164"], check["ok"], check["detail"]),
            )
            if not check["ok"]:
                raise VoiceError(f"The test call to {got['e164']} failed: {check['detail']} Nothing was saved.", 502)
            conn.execute(
                """UPDATE voice_numbers SET e164 = %s, provider = %s, provider_ref = %s, status = 'active',
                     billing_from = now() WHERE id = %s""",
                (got["e164"], prov.name, got["ref"], number_id),
            )
            billing.one_time_charge(conn, cid, f"new_number:{number_id}", "new_number", f"New number {got['e164']}")
            emergency.activate_outbound(conn, cid, number_id)
            for r in run.results:
                if r.get("number_id") == number_id:
                    r["e164"] = got["e164"]
        elif item[0] == "release":
            try:
                prov.release_number(conn, item[1])
            except providers.ProviderError:
                pass  # released on our side; the provider is told again at reconciliation
        elif item[0] == "validate_site":
            emergency.submit_site(conn, cid, item[1])
        elif item[0] == "moved":
            site = conn.execute("SELECT * FROM voice_sites WHERE id = %s", (item[2],)).fetchone()
            emergency.after_move(conn, cid, item[1], site)
        elif item[0] == "refresh_number":
            emergency.refresh_numbers(conn, cid, number_id=item[1])
        elif item[0] == "device_fee":
            _, device_id, kind, vu = item
            billing.one_time_charge(
                conn,
                cid,
                f"device:{device_id}",
                "desk_phone" if kind == "desk" else "softphone",
                "Desk phone set-up" if kind == "desk" else "Softphone set-up",
                voice_user_id=vu["id"],
                site_id=vu["site_id"],
            )


# ---- rollback ------------------------------------------------------------------------

ROUTING = {
    "ring_groups": ("voice_ring_groups", ("id", "name", "extension", "strategy", "members", "ring_seconds")),
    "queues": ("voice_queues", ("id", "name", "extension", "strategy", "members", "max_wait_s")),
    "hours": ("voice_hours", ("id", "name", "timezone", "schedule", "holidays")),
    "menus": ("voice_menus", ("id", "name", "extension", "greeting", "options", "hours_id", "closed_target")),
    "ai_rules": ("voice_ai_rules", ("id", "name", "condition", "hours_id", "number_id", "fallback", "enabled")),
}


def rollback(conn: psycopg.Connection, cid: Any, version: int, actor: str, dry_run: bool = False) -> dict:
    """Put routing (ring groups, queues, hours, menus, AI rules and where each
    number rings) back as it was at `version`. Users, numbers and devices are
    not brought back or removed: those change the bill and go through their own
    change. Members who have since left are dropped."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('voice_config:' || %s))", (str(cid),))
    row = conn.execute(
        "SELECT snapshot FROM voice_config_versions WHERE customer_id = %s AND version = %s", (cid, version)
    ).fetchone()
    if row is None:
        raise VoiceError(f"There is no version {version}.", 404)
    snap = row["snapshot"]
    before = snapshot(conn, cid)
    out: dict = {}
    try:
        with conn.transaction():
            active = {u["id"] for u in before["users"]}
            for coll in ("ai_rules", "menus", "queues", "ring_groups", "hours"):
                conn.execute(f"DELETE FROM {ROUTING[coll][0]} WHERE customer_id = %s", (cid,))
            for coll in ("hours", "ring_groups", "queues", "menus", "ai_rules"):
                table, cols = ROUTING[coll]
                for item in snap.get(coll, []):
                    vals = []
                    for c in cols:
                        v = item.get(c)
                        if c == "members":
                            v = [uuid.UUID(m) for m in v or [] if m in active]
                        elif isinstance(v, (dict, list)):
                            v = Jsonb(v)
                        vals.append(v)
                    conn.execute(
                        f"INSERT INTO {table} (customer_id, {', '.join(cols)})"
                        f" VALUES (%s, {', '.join(['%s'] * len(cols))})",
                        (cid, *vals),
                    )
            for n in snap.get("numbers", []):
                conn.execute(
                    """UPDATE voice_numbers SET target_type = %s, target_id = %s
                       WHERE id = %s AND customer_id = %s AND status <> 'removed'""",
                    (n["target_type"], n["target_id"], n["id"], cid),
                )
            after = snapshot(conn, cid)
            out = {"diff": diff(before, after), "version": None, "from_version": version}
            if dry_run:
                raise _Rollback
            out["version"] = _save_version(
                conn, cid, "rollback", f"Routing back to version {version}", [], after, {}, actor
            )
            events.emit(
                conn, cid, "voice.config_rolled_back", {"to": version, "version": out["version"]}, str(out["version"])
            )
            audit.record(conn, actor, "commai.voice.rollback", f"v{version}", cid, {"new_version": out["version"]})
    except _Rollback:
        pass
    return out


# ---- scheduled changes ---------------------------------------------------------------


def schedule(conn, cid, ops, run_at, actor: str, *, can_spend: bool, accepted_price: dict | None) -> dict:
    """Check a change now and set it to apply at `run_at` through the job queue.
    It is checked again when it runs."""
    preview = apply(conn, cid, ops, actor=actor, dry_run=True)
    if preview["errors"]:
        return {"ok": False, **preview}
    impact = preview["price_impact"]
    if impact["changes_bill"]:
        if not can_spend:
            raise VoiceError(
                "This changes your bill. Ask someone in your company with spend permission to save it.", 403
            )
        seen = accepted_price or {}
        if (seen.get("monthly_delta"), seen.get("one_time")) != (impact["monthly_delta"], impact["one_time"]):
            raise VoiceError(
                f"Check the price first: this adds {impact['monthly_delta']} a month and "
                f"{impact['one_time']} once ({impact['currency']}).",
                409,
            )
    row = conn.execute(
        """INSERT INTO voice_changes (customer_id, summary, ops, run_at, price_impact, created_by)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
        (cid, _summary(ops), Jsonb(ops), run_at, Jsonb(impact), actor),
    ).fetchone()
    delay = max(0.0, (run_at - conn.execute("SELECT now() AS n").fetchone()["n"]).total_seconds())
    jobs.enqueue(
        conn,
        "voice.apply_change",
        {"change_id": str(row["id"])},
        customer_id=cid,
        dedupe_key=f"voice.change:{row['id']}",
        delay_s=delay,
    )
    events.emit(conn, cid, "voice.change_scheduled", {"change_id": str(row["id"]), "run_at": run_at.isoformat()})
    audit.record(conn, actor, "commai.voice.schedule", str(row["id"]), cid, {"run_at": run_at.isoformat()})
    return {"ok": True, "change": row, **preview}


@jobs.handler("voice.apply_change")
def _run_scheduled(conn: psycopg.Connection, job: dict):
    change = conn.execute(
        "SELECT * FROM voice_changes WHERE id = %s FOR UPDATE", (job["payload"]["change_id"],)
    ).fetchone()
    if change is None or change["status"] != "scheduled":
        return None
    wait = conn.execute("SELECT extract(epoch FROM %s - now()) AS s", (change["run_at"],)).fetchone()["s"]
    if wait > 0:
        return jobs.Later("not due yet", float(wait))
    try:
        with conn.transaction():
            # The price was approved when it was scheduled; it is checked against
            # that approval, so a price list change in between stops it.
            out = apply(
                conn,
                change["customer_id"],
                change["ops"],
                actor=change["created_by"],
                kind="scheduled",
                summary=change["summary"],
                accepted_price=change["price_impact"],
            )
            if out["errors"]:
                raise VoiceError("; ".join(f"#{e['index'] + 1}: {e['error']}" for e in out["errors"]), 422)
    except VoiceError as e:
        conn.execute(
            "UPDATE voice_changes SET status = 'failed', error = %s, finished_at = now() WHERE id = %s",
            (str(e)[:1000], change["id"]),
        )
        events.emit(
            conn, change["customer_id"], "voice.change_failed", {"change_id": str(change["id"]), "error": str(e)[:300]}
        )
        return None
    conn.execute(
        "UPDATE voice_changes SET status = 'applied', version = %s, finished_at = now() WHERE id = %s",
        (out["version"], change["id"]),
    )
    return None
