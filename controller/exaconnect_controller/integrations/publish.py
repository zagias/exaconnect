"""One publish point for Connect events (ADR 0026).

Things happen where they always did: agents report BFD changes, the
routing engine records decisions, Storm Mode and insights write to the
events table, every API write lands in the audit log. Row triggers on
those tables (schema.sql) queue an ``integrations.publish`` job in the
writer's transaction, and only when some integration is listening, so the
hot paths do no extra work. The job turns the row into one or more
CloudEvents (connect_events), matches them against subscriptions (kind,
site, severity) and queues one delivery per match.

The tick (run every 15 s by runner.py) adds what no table records:
nodes going offline and coming back, and maintenance windows starting and
ending.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ..commai import jobs
from . import catalogue
from .catalogue import KINDS, RANK

OFFLINE_S = 90
MAINTENANCE_LEAD_S = 60  # classes leave a link this long before its window starts


def _id(*parts: Any) -> str:
    raw = json.dumps(parts, default=str, sort_keys=True).encode()
    return "evt-" + hashlib.sha256(raw).hexdigest()[:24]


def _iso(t: Any) -> str:
    if isinstance(t, dt.datetime):
        return t.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
    return str(t) if t is not None else ""


def _time(v: Any) -> dt.datetime:
    if isinstance(v, dt.datetime):
        return v
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return dt.datetime.now(dt.UTC)


# ---- emitting -------------------------------------------------------------------


def emit(
    conn: psycopg.Connection,
    name: str,
    *,
    customer_id: Any,
    data: dict,
    event_id: str | None = None,
    time: dt.datetime | None = None,
    site_id: Any = None,
    carrier_ids: list | None = None,
    severity: str | None = None,
    action: str | None = None,
    dedup_key: str = "",
    subject: str = "",
    test_integration: int | None = None,
) -> str | None:
    """Record one event and queue its deliveries. Returns its id (None if it existed)."""
    kind = KINDS[name]
    severity = severity or kind.severity
    action = action or kind.action
    event_id = event_id or _id(name, customer_id, data, _iso(time))
    data = {**data, "summary": data.get("summary") or kind.title}
    if site_id:
        data.setdefault("site_id", str(site_id))
    row = conn.execute(
        """INSERT INTO connect_events (id, type, source, subject, time, customer_id, carrier_ids, site_id, severity,
                                       dedup_key, action, data)
           VALUES (%s, %s, %s, %s, %s, %s, %s::uuid[], %s, %s, %s, %s, %s)
           ON CONFLICT (id) DO NOTHING RETURNING *""",
        (
            event_id,
            catalogue.type_of(name),
            catalogue.SOURCE + (f"/organisations/{customer_id}" if customer_id else ""),
            subject,
            time or dt.datetime.now(dt.UTC),
            customer_id,
            [str(c) for c in carrier_ids or []],
            site_id,
            severity,
            dedup_key,
            action,
            Jsonb(data),
        ),
    ).fetchone()
    if row is None:
        return None
    fanout(conn, row, only=test_integration)
    return event_id


def fanout(conn: psycopg.Connection, ev: dict, only: int | None = None) -> list[int]:
    """Queue a delivery to every subscription that wants this event."""
    name = catalogue.name_of(ev["type"])
    kind = KINDS.get(name)
    if only is not None:
        subs = conn.execute("SELECT * FROM connect_integrations WHERE id = %s", (only,)).fetchall()
    else:
        subs = conn.execute(
            """SELECT * FROM connect_integrations
               WHERE enabled AND cardinality(event_types) > 0
                 AND (customer_id = %s OR (carrier_id = ANY(%s::uuid[]) AND %s))""",
            (ev["customer_id"], [str(c) for c in ev["carrier_ids"] or []], bool(kind and kind.carrier)),
        ).fetchall()
    made: list[int] = []
    for sub in subs:
        if only is None:
            if not catalogue.matches(list(sub["event_types"]), name):
                continue
            if RANK.get(ev["severity"], 0) < RANK.get(sub["min_severity"], 0):
                continue
            if sub["site_ids"] and ev["site_id"] is not None and ev["site_id"] not in sub["site_ids"]:
                continue
            if sub["site_ids"] and ev["site_id"] is None and sub["customer_id"] is not None:
                continue
        row = conn.execute(
            """INSERT INTO connect_deliveries (integration_id, event_id, event_type, test)
               VALUES (%s, %s, %s, %s) ON CONFLICT (integration_id, event_id) DO NOTHING RETURNING id""",
            (sub["id"], ev["id"], name, only is not None),
        ).fetchone()
        if row is None:
            continue
        made.append(row["id"])
        if only is None:
            jobs.enqueue(
                conn,
                "integrations.deliver",
                {"delivery_id": row["id"]},
                customer_id=ev["customer_id"],
                dedupe_key=f"cdeliver:{row['id']}",
            )
    return made


# ---- rows to events -------------------------------------------------------------


def _site_of_node(conn, node_id: Any) -> dict | None:
    if not node_id:
        return None
    return conn.execute(
        "SELECT s.id, s.name, s.kind, n.name AS node FROM nodes n JOIN sites s ON s.id = n.site_id WHERE n.id = %s",
        (node_id,),
    ).fetchone()


def _link(conn, site_id: Any, path: str | None) -> dict | None:
    if not site_id or not path:
        return None
    return conn.execute(
        """SELECT l.id, l.carrier_id, c.name AS carrier, p.label FROM links l
           JOIN carriers c ON c.id = l.carrier_id JOIN paths p ON p.name = l.path
           WHERE l.site_id = %s AND l.path = %s""",
        (site_id, path),
    ).fetchone()


def from_event_row(conn: psycopg.Connection, r: dict) -> None:
    kind, detail = r["kind"], r["detail"] or {}
    at = _time(r["time"])
    site = _site_of_node(conn, r.get("node_id"))
    if site is None and detail.get("site_id"):
        site = conn.execute(
            "SELECT s.id, s.name, s.kind, n.name AS node FROM sites s LEFT JOIN nodes n ON n.site_id = s.id"
            " WHERE s.id = %s",
            (detail["site_id"],),
        ).fetchone()
    sid = site["id"] if site else None
    sname = site["name"] if site else detail.get("site", "")
    eid = _id("events", r["time"], r.get("node_id"), kind, detail)
    base = {"customer_id": r["customer_id"], "event_id": eid, "time": at, "site_id": sid}
    if kind in ("bfd_down", "bfd_up"):
        tunnel = detail.get("tunnel", "")
        p = conn.execute("SELECT name FROM paths WHERE tunnel = %s", (tunnel,)).fetchone()
        path = p["name"] if p else tunnel
        lk = _link(conn, sid, path)
        label = lk["label"] if lk else path
        down = kind == "bfd_down"
        data = {
            "site": sname,
            "path": path,
            "path_label": label,
            "tunnel": tunnel,
            "carrier": lk["carrier"] if lk else "",
            "link_id": str(lk["id"]) if lk else "",
            "summary": (
                f"{label} is down at {sname} (BFD). Classes move off it within a second."
                if down
                else f"{label} is back up at {sname}."
            ),
        }
        emit(
            conn,
            "path.down" if down else "path.up",
            data=data,
            carrier_ids=[lk["carrier_id"]] if lk else [],
            dedup_key=f"path:{sid}:{path}",
            subject=f"sites/{sname}/paths/{path}",
            **base,
        )
    elif kind in ("storm_on", "storm_off"):
        on = kind == "storm_on"
        emit(
            conn,
            "storm.on" if on else "storm.off",
            data={
                "site": detail.get("site") or sname,
                "by": detail.get("by", ""),
                "summary": f"Storm Mode {'on' if on else 'off'} at {detail.get('site') or sname}"
                f"{' by ' + detail['by'] if detail.get('by') else ''}.",
            },
            subject=f"sites/{detail.get('site') or sname}",
            **base,
        )
    elif kind == "insight":
        ins = conn.execute(
            "SELECT i.*, s.name AS site_name FROM insights i LEFT JOIN sites s ON s.id = i.site_id WHERE i.id = %s",
            (detail.get("insight"),),
        ).fetchone()
        hazard = detail.get("kind") in ("hazard", "storm_warning")
        data = {
            "insight": detail.get("insight"),
            "kind": detail.get("kind"),
            "title": detail.get("title", ""),
            "site": ins["site_name"] if ins and ins["site_name"] else "",
            "detail": ins["detail"] if ins else "",
            "example": bool(ins["example"]) if ins else False,
            "summary": detail.get("title", "Insight"),
        }
        base["site_id"] = ins["site_id"] if ins else None
        emit(
            conn,
            "hazard.alert" if hazard else "insight.raised",
            data=data,
            severity=detail.get("severity") or "info",
            dedup_key=f"insight:{detail.get('insight')}" if hazard else "",
            **base,
        )
    elif kind in ("enrolled", "node_revoked", "node_offline", "node_online", "config_failed"):
        name = {
            "enrolled": "node.enrolled",
            "node_revoked": "node.revoked",
            "node_offline": "node.offline",
            "node_online": "node.online",
            "config_failed": "config.apply_failed",
        }[kind]
        node = detail.get("node") or (site["node"] if site else "")
        words = {
            "node.enrolled": f"{node} enrolled and has its certificate.",
            "node.revoked": f"{node}'s certificate was revoked{' by ' + detail['by'] if detail.get('by') else ''}.",
            "node.offline": f"{node} has not reported for {OFFLINE_S} seconds. It keeps forwarding on its last map.",
            "node.online": f"{node} is reporting again.",
            "config.apply_failed": f"{node} could not apply desired state version {detail.get('version', '?')} "
            f"and kept the last good one: {str(detail.get('error', ''))[:200]}",
        }
        emit(
            conn,
            name,
            data={**{k: v for k, v in detail.items()}, "node": node, "site": sname, "summary": words[name]},
            dedup_key=f"node:{r.get('node_id')}" if name in ("node.offline", "node.online") else "",
            **base,
        )
    elif kind == "ddos_blocked":
        emit(
            conn,
            "ddos.blocked",
            data={
                **detail,
                "node": site["node"] if site else "",
                "summary": f"The PoP blocked {detail.get('address')} for flooding "
                f"({int(detail.get('expires_s') or 0) // 60} minutes).",
            },
            **base,
        )
    elif kind in ("carrier_notice", "maintenance_start", "maintenance_end"):
        _notice_event(conn, kind, detail, sname, base)


def _notice_event(conn, kind: str, detail: dict, sname: str, base: dict) -> None:
    carrier_ids = [detail["carrier_id"]] if detail.get("carrier_id") else []
    notice = detail.get("notice")
    data = {
        "notice": notice,
        "carrier": detail.get("carrier", ""),
        "title": detail.get("title", ""),
        "site": sname,
        "links": detail.get("links", []),
        "starts_at": detail.get("starts_at"),
        "ends_at": detail.get("ends_at"),
        "status": detail.get("status", ""),
    }
    key = f"notice:{notice}:{base['site_id']}"
    if kind == "maintenance_start":
        data["summary"] = f"{detail.get('carrier')} maintenance started at {sname}: {detail.get('title')}."
        emit(conn, "maintenance.started", data=data, carrier_ids=carrier_ids, dedup_key=f"maint:{key}", **base)
    elif kind == "maintenance_end":
        data["summary"] = f"{detail.get('carrier')} maintenance ended at {sname}: {detail.get('title')}."
        emit(conn, "maintenance.ended", data=data, carrier_ids=carrier_ids, dedup_key=f"maint:{key}", **base)
    elif detail.get("status") in ("resolved", "closed", "cancelled"):
        data["summary"] = f"{detail.get('carrier')} {detail.get('status')} its notice: {detail.get('title')}."
        emit(conn, "carrier.notice_resolved", data=data, carrier_ids=carrier_ids, dedup_key=key, **base)
    elif detail.get("kind") == "maintenance":
        data["summary"] = (
            f"{detail.get('carrier')} plans maintenance affecting {sname} from {detail.get('starts_at')} "
            f"to {detail.get('ends_at')}: {detail.get('title')}."
        )
        emit(
            conn,
            "carrier.maintenance",
            data=data,
            carrier_ids=carrier_ids,
            severity=detail.get("severity") or "warning",
            dedup_key=key,
            **base,
        )
    else:
        data["summary"] = f"{detail.get('carrier')} reports a fault affecting {sname}: {detail.get('title')}."
        emit(
            conn,
            "carrier.fault",
            data=data,
            carrier_ids=carrier_ids,
            severity=detail.get("severity") or "critical",
            dedup_key=key,
            **base,
        )


def _worst(inputs: dict, path: str | None) -> tuple[str, dict] | None:
    metrics = (((inputs or {}).get("paths") or {}).get(path or "") or {}).get("metrics") or {}
    if not metrics:
        return None

    def badness(kv):
        m = kv[1]
        lim = float(m.get("limit") or 0) or 1.0
        return max(float(m.get("now") or 0) / lim, float(m.get("ahead") or 0) / lim)

    return max(metrics.items(), key=badness)


def from_decision_row(conn: psycopg.Connection, r: dict) -> None:
    site = conn.execute("SELECT id, name FROM sites WHERE id = %s", (r["site_id"],)).fetchone()
    sname = site["name"] if site else ""
    at = _time(r["time"])
    cls = r["class_name"]
    lk = _link(conn, r["site_id"], r["from_path"])
    base = {"customer_id": r["customer_id"], "time": at, "site_id": r["site_id"]}
    worst = _worst(r.get("inputs") or {}, r["from_path"])
    breach_now = worst is not None and float(worst[1].get("now") or 0) >= float(worst[1].get("limit") or 1e18)
    key = f"sla:{r['site_id']}:{cls}"
    metric_data = {}
    if worst is not None:
        metric_data = {
            "metric": worst[0],
            "now": worst[1].get("now"),
            "forecast": worst[1].get("ahead"),
            "limit": worst[1].get("limit"),
        }
    common = {
        "site": sname,
        "class": cls,
        "path": r["from_path"],
        "path_label": lk["label"] if lk else (r["from_path"] or ""),
        "carrier": lk["carrier"] if lk else "",
        "link_id": str(lk["id"]) if lk else "",
        "decision": r["id"],
        "shadow": bool(r["shadow"]),
        "reason": r["reason"],
    }
    carriers = [lk["carrier_id"]] if lk else []
    if r["kind"] == "hold":
        emit(
            conn,
            "sla.breach",
            event_id=f"dec-{r['id']}-breach",
            data={**common, **metric_data, "summary": r["reason"]},
            carrier_ids=carriers,
            dedup_key=key,
            **base,
        )
        return
    if worst is not None and r["kind"] in ("move", "failover") and not r["shadow"]:
        if breach_now:
            emit(
                conn,
                "sla.breach",
                event_id=f"dec-{r['id']}-breach",
                data={**common, **metric_data, "summary": r["reason"]},
                carrier_ids=carriers,
                action="notify",  # already handled by this move; nothing stays open
                **base,
            )
        elif "forecast" in r["reason"]:
            emit(
                conn,
                "sla.breach_forecast",
                event_id=f"dec-{r['id']}-forecast",
                data={**common, **metric_data, "summary": r["reason"]},
                **base,
            )
    emit(
        conn,
        "routing.moved",
        event_id=f"dec-{r['id']}",
        data={
            **common,
            "kind": r["kind"],
            "from_path": r["from_path"],
            "to_path": r["to_path"],
            "engine": r["engine"],
            "summary": r["reason"],
        },
        severity="warning" if r["kind"] == "failover" else "info",
        action="notify" if r["shadow"] else "resolve",
        dedup_key=key,
        subject=f"sites/{sname}/classes/{cls}",
        **base,
    )


def from_audit_row(conn: psycopg.Connection, r: dict) -> None:
    emit(
        conn,
        "audit.recorded",
        event_id=f"audit-{r['id']}",
        customer_id=r["customer_id"],
        time=_time(r["at"]),
        data={
            "actor": r["actor"],
            "action": r["action"],
            "target": r["target"],
            "detail": r.get("detail") or {},
            "summary": f"{r['actor']} {r['action']} {r['target']}".strip(),
        },
    )


@jobs.handler("integrations.publish")
def _publish(conn: psycopg.Connection, job: dict) -> None:
    src, row = job["payload"]["source"], job["payload"]["row"]
    if src == "events":
        from_event_row(conn, row)
    elif src == "decisions":
        from_decision_row(conn, row)
    elif src == "audit":
        from_audit_row(conn, row)


# ---- the tick: what no table records --------------------------------------------


def event_row(conn, customer_id: Any, node_id: Any, kind: str, detail: dict, at: dt.datetime | None = None) -> None:
    conn.execute(
        "INSERT INTO events (time, customer_id, node_id, kind, detail) VALUES (%s, %s, %s, %s, %s)",
        (at or dt.datetime.now(dt.UTC), customer_id, node_id, kind, Jsonb(detail)),
    )


def tick(conn: psycopg.Connection, now: dt.datetime | None = None) -> dict[str, int]:
    now = now or dt.datetime.now(dt.UTC)
    out = {"offline": 0, "online": 0, "started": 0, "ended": 0}
    for n in conn.execute(
        """UPDATE nodes SET offline_since = %s
           WHERE offline_since IS NULL AND last_seen IS NOT NULL AND last_seen < %s - make_interval(secs => %s)
             AND cert_serial NOT LIKE 'revoked:%%'
           RETURNING id, name, customer_id, last_seen""",
        (now, now, OFFLINE_S),
    ).fetchall():
        event_row(
            conn, n["customer_id"], n["id"], "node_offline", {"node": n["name"], "last_seen": _iso(n["last_seen"])}, now
        )
        out["offline"] += 1
    for n in conn.execute(
        """WITH back AS (SELECT id, offline_since FROM nodes
                         WHERE offline_since IS NOT NULL AND last_seen > offline_since FOR UPDATE)
           UPDATE nodes SET offline_since = NULL FROM back WHERE nodes.id = back.id
           RETURNING nodes.id, nodes.name, nodes.customer_id, back.offline_since""",
    ).fetchall():
        event_row(
            conn,
            n["customer_id"],
            n["id"],
            "node_online",
            {"node": n["name"], "offline_s": int((now - n["offline_since"]).total_seconds())},
            now,
        )
        out["online"] += 1
    from . import notices

    s, e = notices.windows(conn, now)
    out["started"], out["ended"] = s, e
    return out
