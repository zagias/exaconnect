"""Carrier fault and maintenance notices, and trouble tickets (ADR 0026).

Carriers post notices about their OWN links only, in Connect's native
JSON, as TMF621 trouble tickets or as TMF688 events. A notice lands on
every affected site's event timeline (one event per site, so each
customer sees only its own sites) and goes out as a carrier.* event.

Planned maintenance moves traffic pre-emptively: from a minute before the
window starts until it ends, the routing engine treats the link as down
(routing/runner.py asks `under_maintenance`), so classes leave it through
the normal engine with a logged reason, and come back after the usual
hold time once the window is over.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import audit
from .publish import MAINTENANCE_LEAD_S, _iso, event_row

KINDS = ("maintenance", "fault", "trouble")
STATUSES = ("scheduled", "open", "in_progress", "resolved", "cancelled", "closed")
CLOSED = ("resolved", "cancelled", "closed")
SOURCES = ("portal", "api", "email", "tmf621", "tmf688", "sonata", "connect")


class NoticeError(ValueError):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def _links(conn, link_ids: list) -> list[dict]:
    if not link_ids:
        return []
    return conn.execute(
        """SELECT l.id, l.customer_id, l.site_id, l.carrier_id, l.path, s.name AS site, p.label,
                  c.name AS carrier, n.id AS node_id
           FROM links l JOIN sites s ON s.id = l.site_id JOIN paths p ON p.name = l.path
           JOIN carriers c ON c.id = l.carrier_id LEFT JOIN nodes n ON n.site_id = l.site_id
           WHERE l.id = ANY(%s::uuid[])""",
        ([str(x) for x in link_ids],),
    ).fetchall()


def check_links(conn, carrier_id: Any, link_ids: list) -> list[dict]:
    """The links, all of which must belong to the carrier."""
    ids = list(dict.fromkeys(str(x) for x in link_ids))
    try:
        rows = _links(conn, ids)
    except psycopg.errors.InvalidTextRepresentation:
        raise NoticeError("A link id is not valid.", 422) from None
    found = {str(r["id"]): r for r in rows}
    if len(found) != len(ids):
        raise NoticeError("One or more links do not exist.", 422)
    if any(str(r["carrier_id"]) != str(carrier_id) for r in rows):
        raise NoticeError("A carrier can only post notices about its own links.", 403)
    return rows


def create(
    conn: psycopg.Connection,
    *,
    carrier_id: Any,
    kind: str,
    title: str,
    actor: str,
    link_ids: list,
    description: str = "",
    severity: str | None = None,
    starts_at: dt.datetime | None = None,
    ends_at: dt.datetime | None = None,
    move_traffic: bool = True,
    external_id: str = "",
    source: str = "api",
    raw: dict | None = None,
    status: str | None = None,
) -> dict:
    if kind not in ("maintenance", "fault"):
        raise NoticeError("A carrier notice is maintenance or a fault.")
    if not link_ids:
        raise NoticeError("Say which of your links the notice is about.")
    links = check_links(conn, carrier_id, link_ids)
    if kind == "maintenance":
        if starts_at is None or ends_at is None:
            raise NoticeError("Planned maintenance needs a start and an end.")
        if ends_at <= starts_at:
            raise NoticeError("The maintenance window must end after it starts.")
        if ends_at - starts_at > dt.timedelta(days=7):
            raise NoticeError("A maintenance window can be at most 7 days.")
    if external_id:
        dup = conn.execute(
            "SELECT * FROM connect_notices WHERE carrier_id = %s AND external_id = %s", (carrier_id, external_id)
        ).fetchone()
        if dup is not None:
            raise NoticeError(f"There is already a notice with the reference {external_id}.", 409)
    status = status or ("scheduled" if kind == "maintenance" else "open")
    severity = severity or ("warning" if kind == "maintenance" else "critical")
    row = conn.execute(
        """INSERT INTO connect_notices (carrier_id, kind, status, severity, title, description, link_ids, starts_at,
                                        ends_at, move_traffic, external_id, source, raw, created_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s::uuid[], %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (
            carrier_id,
            kind,
            status,
            severity,
            title[:200],
            description[:4000],
            [str(r["id"]) for r in links],
            starts_at,
            ends_at,
            move_traffic,
            external_id[:120],
            source,
            Jsonb(raw or {}),
            actor,
        ),
    ).fetchone()
    audit.record(conn, actor, f"notice.{kind}", row["title"], None, {"id": row["id"], "links": len(links)})
    _announce(conn, row, links, "carrier_notice")
    return row


def update(conn: psycopg.Connection, notice: dict, changes: dict, actor: str) -> dict:
    allowed = {"status", "title", "description", "severity", "starts_at", "ends_at", "move_traffic"}
    changes = {k: v for k, v in changes.items() if k in allowed and v is not None}
    if "status" in changes and changes["status"] not in STATUSES:
        raise NoticeError(f"Status is one of {', '.join(STATUSES)}.")
    if not changes:
        return notice
    starts = changes.get("starts_at", notice["starts_at"])
    ends = changes.get("ends_at", notice["ends_at"])
    if notice["kind"] == "maintenance" and starts and ends and ends <= starts:
        raise NoticeError("The maintenance window must end after it starts.")
    sets = ", ".join(f"{k} = %({k})s" for k in changes)
    row = conn.execute(
        f"""UPDATE connect_notices SET {sets}, updated_at = now(),
                resolved_at = CASE WHEN %(closing)s THEN now() ELSE resolved_at END
            WHERE id = %(id)s RETURNING *""",
        {**changes, "id": notice["id"], "closing": changes.get("status") in CLOSED},
    ).fetchone()
    audit.record(conn, actor, "notice.update", row["title"], None, {"id": row["id"], "changes": list(changes)})
    if changes.get("status") in CLOSED and notice["status"] not in CLOSED:
        _announce(conn, row, _links(conn, list(row["link_ids"])), "carrier_notice")
    elif {"starts_at", "ends_at", "title", "severity"} & set(changes):
        _announce(conn, row, _links(conn, list(row["link_ids"])), "carrier_notice")
    return row


def _announce(conn, notice: dict, links: list[dict], kind: str) -> None:
    """One event per affected site, each carrying only that site's links."""
    by_site: dict[Any, list[dict]] = {}
    for lk in links:
        by_site.setdefault(lk["site_id"], []).append(lk)
    for site_id, lks in by_site.items():
        first = lks[0]
        event_row(
            conn,
            first["customer_id"],
            first["node_id"],
            kind,
            {
                "notice": notice["id"],
                "kind": notice["kind"],
                "status": notice["status"],
                "severity": notice["severity"],
                "title": notice["title"],
                "carrier": first["carrier"],
                "carrier_id": str(first["carrier_id"]),
                "site": first["site"],
                "site_id": str(site_id),
                "links": [{"id": str(lk["id"]), "path": lk["path"], "label": lk["label"]} for lk in lks],
                "starts_at": _iso(notice["starts_at"]) or None,
                "ends_at": _iso(notice["ends_at"]) or None,
            },
        )


def windows(conn: psycopg.Connection, now: dt.datetime) -> tuple[int, int]:
    """Mark maintenance windows started and ended, and announce each."""
    started = conn.execute(
        """UPDATE connect_notices SET window_state = 'started', status = 'in_progress', updated_at = now()
           WHERE kind = 'maintenance' AND window_state = '' AND status NOT IN ('resolved', 'cancelled', 'closed')
             AND starts_at - make_interval(secs => %s) <= %s AND ends_at > %s
           RETURNING *""",
        (MAINTENANCE_LEAD_S, now, now),
    ).fetchall()
    for n in started:
        _announce(conn, n, _links(conn, list(n["link_ids"])), "maintenance_start")
    ended = conn.execute(
        """UPDATE connect_notices SET window_state = 'ended',
                  status = CASE WHEN status IN ('cancelled', 'closed') THEN status ELSE 'resolved' END,
                  resolved_at = coalesce(resolved_at, now()), updated_at = now()
           WHERE kind = 'maintenance' AND window_state = 'started' AND (ends_at <= %s OR status IN ('cancelled', 'closed'))
           RETURNING *""",
        (now,),
    ).fetchall()
    for n in ended:
        _announce(conn, n, _links(conn, list(n["link_ids"])), "maintenance_end")
    return len(started), len(ended)


def under_maintenance(conn: psycopg.Connection, link_ids: list, now: dt.datetime) -> dict[str, str]:
    """Links whose maintenance window is on (or a minute away), with a reason for the decision log."""
    if not link_ids:
        return {}
    rows = conn.execute(
        """SELECT n.id, n.title, n.ends_at, c.name AS carrier, unnest(n.link_ids) AS link_id
           FROM connect_notices n JOIN carriers c ON c.id = n.carrier_id
           WHERE n.kind = 'maintenance' AND n.move_traffic AND n.status NOT IN ('resolved', 'cancelled', 'closed')
             AND n.starts_at - make_interval(secs => %s) <= %s AND n.ends_at > %s
             AND n.link_ids && %s::uuid[]""",
        (MAINTENANCE_LEAD_S, now, now, [str(x) for x in link_ids]),
    ).fetchall()
    out: dict[str, str] = {}
    for r in rows:
        if str(r["link_id"]) in {str(x) for x in link_ids}:
            ends = r["ends_at"].astimezone(dt.UTC).strftime("%d %b %H:%M UTC")
            out[str(r["link_id"])] = f"planned maintenance by {r['carrier']} until {ends}: {r['title']}"
    return out


# ---- views ---------------------------------------------------------------------


def public(n: dict, own_link_ids: set[str] | None = None) -> dict:
    """A notice for an API reader. Customers see only their own links in it."""
    links = [str(x) for x in n["link_ids"]]
    if own_link_ids is not None:
        links = [x for x in links if x in own_link_ids]
    return {
        "id": n["id"],
        "carrier_id": n["carrier_id"],
        "carrier": n.get("carrier"),
        "kind": n["kind"],
        "status": n["status"],
        "severity": n["severity"],
        "title": n["title"],
        "description": n["description"],
        "link_ids": links,
        "starts_at": n["starts_at"],
        "ends_at": n["ends_at"],
        "move_traffic": n["move_traffic"],
        "window_state": n["window_state"],
        "external_id": n["external_id"],
        "source": n["source"],
        "created_at": n["created_at"],
        "updated_at": n["updated_at"],
        "resolved_at": n["resolved_at"],
    }


def list_for(conn, user, notice_id: int | None = None, limit: int = 200) -> list[dict]:
    """Notices the user may see: admins all, carriers their own, customers those touching their links."""
    q = """SELECT n.*, c.name AS carrier FROM connect_notices n LEFT JOIN carriers c ON c.id = n.carrier_id
           WHERE n.kind <> 'trouble' AND (%(id)s::bigint IS NULL OR n.id = %(id)s)"""
    args: dict[str, Any] = {"id": notice_id, "l": limit}
    own: set[str] | None = None
    if user.role == "carrier":
        q += " AND n.carrier_id = %(k)s"
        args["k"] = user.carrier_id
    elif user.role == "customer":
        own = {
            str(r["id"]) for r in conn.execute("SELECT id FROM links WHERE customer_id = %s", (user.customer_id,))
        }
        q += " AND n.link_ids && %(own)s::uuid[]"
        args["own"] = list(own)
    rows = conn.execute(q + " ORDER BY n.id DESC LIMIT %(l)s", args).fetchall()
    return [public(r, own) for r in rows]
