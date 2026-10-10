"""One organisation, set up in one place (ADR 0043).

- Company details on the customer record.
- Locations: each branch or office entered once. A location may stand behind
  a Connect site (the box at that branch), a phone site (its emergency
  address) and a Jibsy location (its opening hours). Saving a location keeps
  those in step; records made elsewhere are joined to a location by name.
- "Connect this location": a Connect site made from a location, with the
  network numbers (ASN, overlay address, interfaces) chosen automatically.
- The set-up checklist, worked out from what exists."""

from __future__ import annotations

import ipaddress
import re
from typing import Any

import psycopg

from . import audit, desired, inventory

ADDRESS = ("address_line1", "address_line2", "city", "island", "country", "postcode")
FIELDS = (*ADDRESS, "timezone", "latitude", "longitude")
COMPANY = ("name", "country", "timezone", "address", "phone", "website")

# Interfaces and paths for links added from the portal, in the order they are filled.
TERRESTRIAL = (("carrier-a", "eth1"), ("carrier-b", "eth2"))
SATELLITE = ("sat", "eth3")
SAT_TYPES = ("leo", "geo")
PRIVATE_ASN = (65001, 65534)
OVERLAY_HOSTS = (11, 254)


class OrgSetupError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _products(conn: psycopg.Connection, cid: Any) -> list[str]:
    row = conn.execute("SELECT products FROM customers WHERE id = %s", (cid,)).fetchone()
    if row is None:
        raise OrgSetupError("Organisation not found.", 404)
    return list(row["products"] or [])


def _has_table(conn: psycopg.Connection, name: str) -> bool:
    return conn.execute("SELECT to_regclass(%s) AS t", (name,)).fetchone()["t"] is not None


# ---- company details -------------------------------------------------------------------


def company(conn: psycopg.Connection, cid: Any) -> dict:
    row = conn.execute(
        f"SELECT id, {', '.join(COMPANY)}, products, created_at, setup_done_at FROM customers WHERE id = %s", (cid,)
    ).fetchone()
    if row is None:
        raise OrgSetupError("Organisation not found.", 404)
    return row


def save_company(conn: psycopg.Connection, cid: Any, actor: str, changes: dict) -> dict:
    vals = {k: str(v).strip() for k, v in changes.items() if k in COMPANY and v is not None}
    if "name" in vals and not vals["name"]:
        raise OrgSetupError("Your organisation needs a name.")
    if "country" in vals:
        vals["country"] = vals["country"].upper()[:2]
    if not vals:
        return company(conn, cid)
    if (
        "name" in vals
        and conn.execute(
            "SELECT 1 FROM customers WHERE lower(name) = lower(%s) AND id <> %s", (vals["name"], cid)
        ).fetchone()
    ):
        raise OrgSetupError("Another organisation already uses that name.", 409)
    sets = ", ".join(f"{k} = %s" for k in vals)
    conn.execute(f"UPDATE customers SET {sets} WHERE id = %s", (*vals.values(), cid))
    audit.record(conn, actor, "org.company.update", vals.get("name", ""), cid, vals)
    return company(conn, cid)


# ---- locations -------------------------------------------------------------------------


def _find_or_make(conn, cid, name: str, vals: dict) -> Any:
    """The location called `name` (any case), made if missing; empty fields filled from `vals`."""
    name = name.strip()[:120] or "Main location"
    row = conn.execute(
        "SELECT * FROM org_locations WHERE customer_id = %s AND lower(name) = lower(%s)", (cid, name)
    ).fetchone()
    vals = {k: v for k, v in vals.items() if k in FIELDS and v not in (None, "")}
    if row is None:
        cols = ["customer_id", "name", *vals]
        return conn.execute(
            f"INSERT INTO org_locations ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
            (cid, name, *vals.values()),
        ).fetchone()["id"]
    fill = {k: v for k, v in vals.items() if row.get(k) in (None, "") or (k == "timezone" and not row.get(k))}
    if fill:
        sets = ", ".join(f"{k} = %s" for k in fill)
        conn.execute(f"UPDATE org_locations SET {sets} WHERE id = %s", (*fill.values(), row["id"]))
    return row["id"]


def join_up(conn: psycopg.Connection, cid: Any) -> None:
    """Give every Connect site, phone site and Jibsy location that has no
    location one: the location of the same name, made if there is none."""
    # Connect sites first: their time zone is set on purpose, Jibsy's is often the default.
    for s in conn.execute(
        """SELECT * FROM sites WHERE customer_id = %s AND kind = 'site' AND org_location_id IS NULL
           ORDER BY created_at""",
        (cid,),
    ).fetchall():
        lid = _find_or_make(
            conn,
            cid,
            s["location"] or s["name"],
            {"city": s["location"], "timezone": s["timezone"], "latitude": s["latitude"], "longitude": s["longitude"]},
        )
        conn.execute("UPDATE sites SET org_location_id = %s WHERE id = %s", (lid, s["id"]))
    if _has_table(conn, "voice_sites"):
        for v in conn.execute(
            "SELECT * FROM voice_sites WHERE customer_id = %s AND org_location_id IS NULL ORDER BY created_at", (cid,)
        ).fetchall():
            lid = _find_or_make(conn, cid, v["name"], {k: v[k] for k in (*ADDRESS, "timezone")})
            conn.execute("UPDATE voice_sites SET org_location_id = %s WHERE id = %s", (lid, v["id"]))
    if _has_table(conn, "commai_locations"):
        for c in conn.execute(
            "SELECT * FROM commai_locations WHERE customer_id = %s AND org_location_id IS NULL ORDER BY created_at",
            (cid,),
        ).fetchall():
            lid = _find_or_make(
                conn,
                cid,
                c["name"],
                {"address_line1": c["address"], "country": c["country"], "timezone": c["timezone"]},
            )
            conn.execute("UPDATE commai_locations SET org_location_id = %s WHERE id = %s", (lid, c["id"]))


def locations(conn: psycopg.Connection, cid: Any) -> list[dict]:
    """Every location, with what stands behind it in each app."""
    join_up(conn, cid)
    rows = conn.execute("SELECT * FROM org_locations WHERE customer_id = %s ORDER BY lower(name)", (cid,)).fetchall()
    sites = conn.execute(
        """SELECT s.id, s.name, s.org_location_id, n.id AS node_id, n.last_seen,
                  coalesce(n.last_seen > now() - interval '30 seconds', false) AS online,
                  (SELECT count(*) FROM links l WHERE l.site_id = s.id) AS links
           FROM sites s LEFT JOIN nodes n ON n.site_id = s.id
           WHERE s.customer_id = %s AND s.kind = 'site' AND s.org_location_id IS NOT NULL""",
        (cid,),
    ).fetchall()
    phones = (
        conn.execute(
            """SELECT v.id, v.name, v.org_location_id, v.emergency_status,
                      (SELECT count(*) FROM voice_users u WHERE u.site_id = v.id AND u.status = 'active') AS people
               FROM voice_sites v WHERE v.customer_id = %s AND v.org_location_id IS NOT NULL""",
            (cid,),
        ).fetchall()
        if _has_table(conn, "voice_sites")
        else []
    )
    hours = (
        conn.execute(
            """SELECT c.id, c.name, c.org_location_id, c.is_primary,
                      (SELECT count(*) FROM commai_opening_hours h WHERE h.location_id = c.id) AS intervals
               FROM commai_locations c WHERE c.customer_id = %s AND c.org_location_id IS NOT NULL""",
            (cid,),
        ).fetchall()
        if _has_table(conn, "commai_locations") and _has_table(conn, "commai_opening_hours")
        else []
    )
    for r in rows:
        r["connect"] = next((s for s in sites if s["org_location_id"] == r["id"]), None)
        r["phone"] = next((p for p in phones if p["org_location_id"] == r["id"]), None)
        r["hours"] = next((h for h in hours if h["org_location_id"] == r["id"]), None)
    return rows


def _clean(body: dict) -> dict:
    vals = {k: body[k] for k in FIELDS if k in body}
    for k in (*ADDRESS, "timezone"):
        if k in vals:
            vals[k] = str(vals[k] or "").strip()[:200]
    if "country" in vals:
        vals["country"] = vals["country"].upper()[:2]
    if "timezone" in vals:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        if not vals["timezone"]:
            vals["timezone"] = "America/Port_of_Spain"
        try:
            ZoneInfo(vals["timezone"])
        except (ZoneInfoNotFoundError, ValueError):
            raise OrgSetupError(f"{vals['timezone']} is not a time zone (try America/Port_of_Spain).") from None
    for k, lo, hi in (("latitude", -90, 90), ("longitude", -180, 180)):
        if k in vals and vals[k] not in (None, ""):
            v = float(vals[k])
            if not lo <= v <= hi:
                raise OrgSetupError(f"The {k} must be between {lo} and {hi}.")
            vals[k] = v
        elif k in vals:
            vals[k] = None
    return vals


def _one_line(loc: dict) -> str:
    parts = [loc.get(k) or "" for k in ("address_line1", "address_line2", "city", "island", "postcode")]
    return ", ".join(p for p in parts if p)


def save_location(conn: psycopg.Connection, cid: Any, actor: str, body: dict, location_id: Any = None) -> dict:
    """Add or change a location, and keep the phone site and Jibsy location behind it in step."""
    products = _products(conn, cid)
    join_up(conn, cid)
    name = str(body.get("name", "")).strip()[:120]
    vals = _clean(body)
    if location_id is None:
        if not name:
            raise OrgSetupError("A location needs a name, such as Head office.")
        if conn.execute(
            "SELECT 1 FROM org_locations WHERE customer_id = %s AND lower(name) = lower(%s)", (cid, name)
        ).fetchone():
            raise OrgSetupError("There is already a location with that name.", 409)
        cols = ["customer_id", "name", *vals]
        loc = conn.execute(
            f"INSERT INTO org_locations ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING *",
            (cid, name, *vals.values()),
        ).fetchone()
    else:
        old = conn.execute(
            "SELECT * FROM org_locations WHERE id = %s AND customer_id = %s", (location_id, cid)
        ).fetchone()
        if old is None:
            raise OrgSetupError("Location not found.", 404)
        if (
            name
            and name.lower() != old["name"].lower()
            and conn.execute(
                "SELECT 1 FROM org_locations WHERE customer_id = %s AND lower(name) = lower(%s)", (cid, name)
            ).fetchone()
        ):
            raise OrgSetupError("There is already a location with that name.", 409)
        if name:
            vals["name"] = name
        sets = ", ".join(f"{k} = %s" for k in vals) + (", " if vals else "") + "updated_at = now()"
        loc = conn.execute(
            f"UPDATE org_locations SET {sets} WHERE id = %s RETURNING *", (*vals.values(), location_id)
        ).fetchone()
    _sync(conn, cid, actor, loc, products)
    audit.record(conn, actor, "org.location." + ("create" if location_id is None else "update"), loc["name"], cid, vals)
    return loc


def _sync(conn, cid, actor: str, loc: dict, products: list[str]) -> None:
    lid = loc["id"]
    # Connect: the site's time zone, place name and map position follow the location.
    sites = conn.execute("SELECT id FROM sites WHERE org_location_id = %s", (lid,)).fetchall()
    if sites:
        conn.execute(
            """UPDATE sites SET timezone = %s, location = %s,
                      latitude = coalesce(%s, latitude), longitude = coalesce(%s, longitude)
               WHERE org_location_id = %s""",
            (loc["timezone"], loc["city"] or loc["name"], loc["latitude"], loc["longitude"], lid),
        )
        desired.refresh(conn, cid)
    if "commai" not in products:
        return
    # Jibsy: the location its opening hours belong to.
    if _has_table(conn, "commai_locations"):
        hours = conn.execute("SELECT id FROM commai_locations WHERE org_location_id = %s", (lid,)).fetchone()
        vals = (loc["name"], loc["country"], loc["timezone"], _one_line(loc))
        if hours:
            conn.execute(
                "UPDATE commai_locations SET name = %s, country = %s, timezone = %s, address = %s WHERE id = %s",
                (*vals, hours["id"]),
            )
        else:
            primary = not conn.execute(
                "SELECT 1 FROM commai_locations WHERE customer_id = %s AND is_primary", (cid,)
            ).fetchone()
            conn.execute(
                """INSERT INTO commai_locations (customer_id, name, country, timezone, address, is_primary,
                                                 org_location_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (cid, *vals, primary, lid),
            )
    # Phone: the site whose address emergency calls give. Only with a street address.
    if not (_has_table(conn, "voice_sites") and loc["address_line1"]):
        return
    from .commai import entitlements
    from .commai.voice import config as voice

    if not entitlements.enabled(conn, cid, "voice"):
        return
    phone = conn.execute("SELECT * FROM voice_sites WHERE org_location_id = %s", (lid,)).fetchone()
    fields = {k: loc[k] or "" for k in voice.SITE_FIELDS}
    if phone is None:
        op = {"op": "add_site", "name": loc["name"], **fields}
    else:
        changed = {k: v for k, v in fields.items() if str(phone[k] or "") != v}
        if phone["name"] != loc["name"]:
            changed["name"] = loc["name"]
        if not changed:
            return
        op = {"op": "update_site", "site": str(phone["id"]), **changed}
    out = voice.apply(conn, cid, [op], actor=actor, summary=f"Location {loc['name']}", check_price=False)
    if out.get("errors"):
        raise OrgSetupError("Phone system: " + "; ".join(e["error"] for e in out["errors"]))
    if phone is None:
        sid = (out.get("results") or [{}])[0].get("site_id")
        if sid:
            conn.execute("UPDATE voice_sites SET org_location_id = %s WHERE id = %s", (lid, sid))


def delete_location(conn: psycopg.Connection, cid: Any, actor: str, location_id: Any) -> None:
    loc = conn.execute("SELECT * FROM org_locations WHERE id = %s AND customer_id = %s", (location_id, cid)).fetchone()
    if loc is None:
        raise OrgSetupError("Location not found.", 404)
    if conn.execute("SELECT 1 FROM sites WHERE org_location_id = %s", (location_id,)).fetchone():
        raise OrgSetupError(
            "This location has a Connect site. Ask ExaCarib to remove the site first, so its links stop billing.", 409
        )
    if (
        _has_table(conn, "voice_sites")
        and conn.execute("SELECT 1 FROM voice_sites WHERE org_location_id = %s", (location_id,)).fetchone()
    ):
        raise OrgSetupError(
            "Phones and numbers use this location for emergency calls. Move them to another site in Phone first.", 409
        )
    if _has_table(conn, "commai_locations"):
        conn.execute("DELETE FROM commai_locations WHERE org_location_id = %s", (location_id,))
    conn.execute("DELETE FROM org_locations WHERE id = %s", (location_id,))
    audit.record(conn, actor, "org.location.delete", loc["name"], cid)


# ---- connecting a location to the network ------------------------------------------------


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    s = s if s and s[0].isalnum() else f"site-{s}".strip("-")
    return s[:28].strip("-") or "site"


def _free(used: set[int], lo: int, hi: int, what: str) -> int:
    for n in range(lo, hi + 1):
        if n not in used:
            return n
    raise OrgSetupError(f"No free {what} left for this organisation. Ask ExaCarib.", 409)


def connect_location(
    conn: psycopg.Connection, cid: Any, actor: str, location_id: Any, links: list[dict], lan_prefixes: list[str]
) -> dict:
    """Make the Connect site for a location: name, ASN, overlay address and
    interfaces chosen here; each link takes the next free path. Returns the site."""
    if "connect" not in _products(conn, cid):
        raise OrgSetupError("Your organisation doesn't have the Connect plan.", 403)
    loc = conn.execute("SELECT * FROM org_locations WHERE id = %s AND customer_id = %s", (location_id, cid)).fetchone()
    if loc is None:
        raise OrgSetupError("Location not found.", 404)
    if conn.execute("SELECT 1 FROM sites WHERE org_location_id = %s", (location_id,)).fetchone():
        raise OrgSetupError("This location already has a Connect site.", 409)
    if not links:
        raise OrgSetupError("Add at least one internet link: the carrier and its speed.")
    sat = [lk for lk in links if lk.get("underlay_type") in SAT_TYPES]
    land = [lk for lk in links if lk.get("underlay_type") not in SAT_TYPES]
    if len(land) > len(TERRESTRIAL) or len(sat) > 1:
        raise OrgSetupError("A location takes up to two terrestrial links and one satellite link.")
    try:
        prefixes = [str(ipaddress.ip_network(p.strip())) for p in lan_prefixes if p.strip()]
    except ValueError as e:
        raise OrgSetupError(f"Office network: {e}.") from None
    taken = conn.execute("SELECT name, asn, overlay_host FROM sites WHERE customer_id = %s", (cid,)).fetchall()
    names = {t["name"] for t in taken}
    base = _slug(loc["name"])
    name, n = base, 2
    while name in names:
        name, n = f"{base}-{n}", n + 1
    asn = _free({t["asn"] for t in taken}, *PRIVATE_ASN, "network number")
    host = _free({t["overlay_host"] for t in taken}, *OVERLAY_HOSTS, "overlay address")
    sid = inventory.upsert_site(
        conn,
        cid,
        actor,
        name=name,
        kind="site",
        location=loc["city"] or loc["name"],
        timezone=loc["timezone"] or "UTC",
        asn=asn,
        lan_prefixes=prefixes,
        overlay_host=host,
        latitude=loc["latitude"],
        longitude=loc["longitude"],
    )
    conn.execute("UPDATE sites SET org_location_id = %s WHERE id = %s", (location_id, sid))
    plan = [(lk, *TERRESTRIAL[i]) for i, lk in enumerate(land)] + [(lk, *SATELLITE) for lk in sat]
    for lk, path, iface in plan:
        carrier = str(lk.get("carrier") or "").strip()[:120]
        if not carrier:
            raise OrgSetupError("Each link needs its carrier's name.")
        mbps = float(lk.get("commit_mbps") or 0)
        if mbps < 0 or mbps > 100_000:
            raise OrgSetupError("Speeds are in Mbps, between 0 and 100,000.")
        inventory.upsert_link(
            conn,
            cid,
            sid,
            actor,
            carrier_id=inventory.ensure_carrier(conn, carrier, actor),
            path=path,
            underlay_type=lk.get("underlay_type") or "fibre",
            underlay_interface=iface,
            underlay_ip=None,
            commit_mbps=mbps,
            cost_per_mbps=0,
            burst_price=0,
        )
    audit.record(conn, actor, "org.location.connect", loc["name"], cid, {"site": name, "links": len(plan)})
    return {"site_id": sid, "name": name, "asn": asn, "overlay_host": host}


def has_hub(conn: psycopg.Connection, cid: Any) -> bool:
    return conn.execute("SELECT 1 FROM sites WHERE customer_id = %s AND kind = 'pop'", (cid,)).fetchone() is not None


# ---- the set-up checklist ------------------------------------------------------------------


def checklist(conn: psycopg.Connection, cid: Any) -> dict:
    """The set-up steps for an organisation and how far each has got."""
    co = company(conn, cid)
    products = list(co["products"] or [])
    locs = locations(conn, cid)
    members = conn.execute("SELECT count(*) AS n FROM org_memberships WHERE customer_id = %s", (cid,)).fetchone()["n"]
    invites = conn.execute(
        """SELECT count(*) AS n FROM org_invites WHERE customer_id = %s AND revoked_at IS NULL
           AND accepted_at IS NULL AND expires_at > now()""",
        (cid,),
    ).fetchone()["n"]
    steps = [
        {
            "id": "company",
            "title": "Company details",
            "done": bool(co["name"] and co["country"] and co["timezone"]),
            "detail": "Name, country, time zone and main contact details.",
            "to": "/org/company",
        },
        {
            "id": "locations",
            "title": "Add your locations",
            "done": len(locs) > 0,
            "detail": (
                f"{len(locs)} added: " + ", ".join(lc["name"] for lc in locs[:4]) + ("…" if len(locs) > 4 else "") + "."
                if locs
                else "Each branch or office, once. Connect, phones and opening hours all use this list."
            ),
            "to": "/org/locations",
        },
        {
            "id": "people",
            "title": "Invite your people",
            "done": members > 1 or invites > 0,
            "detail": "Choose each person's role and the apps they can open.",
            "to": "/account/people",
        },
    ]
    if "connect" in products:
        connected = [lc for lc in locs if lc["connect"]]
        enrolled = [lc for lc in connected if lc["connect"]["node_id"]]
        steps.append(
            {
                "id": "connect",
                "app": "connect",
                "title": "Connect your locations",
                "done": bool(locs) and len(enrolled) == len(locs),
                "detail": (
                    f"{len(enrolled)} of {len(locs)} locations have their ExaCarib box installed."
                    if locs
                    else "Add carrier links and install the ExaCarib box at each location."
                ),
                "to": "/org/locations",
                "hub_ready": has_hub(conn, cid),
            }
        )
    if "commai" in products:
        started = any(
            conn.execute(q, (cid,)).fetchone()
            for q, t in (
                ("SELECT 1 FROM channel_accounts WHERE customer_id = %s LIMIT 1", "channel_accounts"),
                ("SELECT 1 FROM voice_users WHERE customer_id = %s LIMIT 1", "voice_users"),
                (
                    "SELECT 1 FROM commai_onboarding_drafts WHERE customer_id = %s AND status = 'approved' LIMIT 1",
                    "commai_onboarding_drafts",
                ),
            )
            if _has_table(conn, t)
        )
        steps.append(
            {
                "id": "jibsy",
                "app": "commai",
                "title": "Set up conversations and phones",
                "done": started,
                "detail": "Channels, phone extensions and your AI agent. Uses the locations and people above.",
                "to": "/commai/setup",
            }
        )
    done = sum(1 for s in steps if s["done"])
    return {
        "organisation": co["name"],
        "steps": steps,
        "done": done,
        "total": len(steps),
        "complete": done == len(steps) or co["setup_done_at"] is not None,
        "dismissed_at": co["setup_done_at"],
    }
