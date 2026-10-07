"""Several carriers at once (ADR 0033): routing, failover, trunk health and rates.

ExaCarib buys calls from carriers (SIP suppliers). Each carrier is a row in
`voice_carriers`, has a versioned per-minute rate sheet by destination prefix,
and is a capability of kind "carrier" in the go-live registry: it carries a
business's calls only once ExaCarib has recorded its written criteria as met
and switched it on (for everyone, or for named pilot businesses).

Routing, per destination prefix (`voice_route_rules`, the longest prefix wins):
- "lcr": cheapest carrier first, quality breaking ties;
- "quality": best trunk health first (OPTIONS success and round-trip time),
  cost breaking ties.
A carrier whose trunk is down, has no rate for the destination, or is not
switched on is skipped. A call tries each carrier in order until one takes it
(failover); every try is kept in `voice_route_attempts`.

Trunk health comes from SIP OPTIONS: Kamailio's dispatcher sends them in
production; here the simulated provider answers them. Three failures in a row
mark a trunk down; one success brings it back.

When no carrier is switched on for a business, calls use the single SIP
provider as in stage 4, so nothing changes until ExaCarib turns carriers on.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ... import audit
from .. import golive, jobs
from . import billing
from . import provider as providers
from .common import VoiceError, digits, money, month_start, next_month, q2, q4, s

CARRIER_CRITERIA = {
    "contract": "Carrier contract signed, with credit limits, fraud liability and support contacts written down.",
    "interconnect": "Trunk set up through Kamailio with TLS where offered; OPTIONS answered and test calls passed "
    "both ways.",
    "rates": "Rate sheet loaded and checked against the carrier's first invoice.",
    "emergency": "The carrier delivers emergency calls in every country it carries them for, tested.",
}
SIMULATED = (("sim-carrier-1", "Simulated carrier A"), ("sim-carrier-2", "Simulated carrier B"))
for _key, _name in SIMULATED:
    golive.declare("carrier", _key, _name, CARRIER_CRITERIA, {"simulated": True})

DOWN_AFTER = 3
SIM_RATES = {
    "sim-carrier-1": [
        ("1868", "Trinidad and Tobago", "0.0080"),
        ("1876", "Jamaica", "0.0260"),
        ("1246", "Barbados", "0.0250"),
        ("1", "North America", "0.0090"),
        ("44", "United Kingdom", "0.0150"),
        ("", "Rest of world", "0.1500"),
    ],
    "sim-carrier-2": [
        ("1868", "Trinidad and Tobago", "0.0095"),
        ("1876", "Jamaica", "0.0210"),
        ("1246", "Barbados", "0.0270"),
        ("1", "North America", "0.0085"),
        ("44", "United Kingdom", "0.0140"),
        ("", "Rest of world", "0.1400"),
    ],
}


def ensure_simulated(conn: psycopg.Connection) -> None:
    """The two simulated carriers, with example rates, exist from the start
    (switched off in the go-live registry like any other carrier)."""
    for i, (key, name) in enumerate(SIMULATED):
        row = conn.execute(
            """INSERT INTO voice_carriers (key, name, adapter, countries, outbound, inbound, created_by)
               VALUES (%s, %s, 'simulated', %s, %s, %s, 'system') ON CONFLICT (key) DO NOTHING RETURNING id""",
            (
                key,
                name,
                ["TT", "JM", "BB", "US", "GB"],
                Jsonb({"host": f"sip{i + 1}.carrier.example", "port": 5061, "transport": "tls", "max_channels": 30}),
                Jsonb({"allow_ips": [f"192.0.2.{10 + i}"]}),  # documentation range: never a real host
            ),
        ).fetchone()
        if row:
            _insert_rates(
                conn, row["id"], 1, [{"prefix": p, "name": n, "per_minute": r} for p, n, r in SIM_RATES[key]], "system"
            )


# ---- carriers -------------------------------------------------------------------------


def _out(conn, c: dict) -> dict:
    c = dict(c)
    c["rates"] = [
        {**r, "per_minute": s(r["per_minute"])}
        for r in conn.execute(
            "SELECT prefix, name, per_minute FROM voice_carrier_rates WHERE carrier_id = %s AND version = %s"
            " ORDER BY length(prefix) DESC, prefix",
            (c["id"], c["rate_version"]),
        ).fetchall()
    ]
    cap = golive.get(conn, "carrier", c["key"]) or {}
    c["golive"] = {"status": cap.get("status", "off"), "pilots": cap.get("pilots", [])}
    c["quality"] = quality(conn, c)
    return c


def list_carriers(conn: psycopg.Connection) -> list[dict]:
    ensure_simulated(conn)
    ensure_health_loop(conn)
    return [_out(conn, c) for c in conn.execute("SELECT * FROM voice_carriers ORDER BY name").fetchall()]


def get(conn, key: str) -> dict:
    c = conn.execute("SELECT * FROM voice_carriers WHERE key = %s", (key,)).fetchone()
    if c is None:
        raise VoiceError("Carrier not found.", 404)
    return c


def _check_ips(ips) -> list[str]:
    out = []
    for ip in ips or []:
        try:
            out.append(str(ipaddress.ip_network(str(ip).strip(), strict=False)))
        except ValueError as e:
            raise VoiceError(f"{ip} is not an IP address or network.", 422) from e
    return out


def save(conn: psycopg.Connection, body: dict, actor: str) -> dict:
    """Add or change a carrier. A new carrier starts switched off in the go-live registry."""
    key = str(body.get("key", "")).strip().lower()
    if not key or not all(ch.isalnum() or ch == "-" for ch in key) or len(key) > 40:
        raise VoiceError("A carrier key is up to 40 letters, digits and hyphens.", 422)
    name = str(body.get("name", "")).strip()[:120] or key
    outbound = dict(body.get("outbound") or {})
    if outbound:
        if outbound.get("transport", "tls") not in ("tls", "tcp", "udp"):
            raise VoiceError("Transport is tls, tcp or udp.", 422)
        port = int(outbound.get("port", 5061))
        if not 1 <= port <= 65535:
            raise VoiceError("The port is 1 to 65535.", 422)
        outbound = {
            "host": str(outbound.get("host", ""))[:200],
            "port": port,
            "transport": outbound.get("transport", "tls"),
            "max_channels": max(1, min(int(outbound.get("max_channels", 30)), 10000)),
            "prefix": digits(outbound.get("prefix", "")),
        }
    inbound = {"allow_ips": _check_ips((body.get("inbound") or {}).get("allow_ips"))}
    adapter = body.get("adapter", "simulated")
    if adapter not in ("simulated", "sip"):
        raise VoiceError("The adapter is simulated or sip.", 422)
    ctry = sorted({str(c).upper()[:2] for c in body.get("countries") or []})
    row = conn.execute(
        """INSERT INTO voice_carriers (key, name, adapter, countries, outbound, inbound, enabled, created_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (key) DO UPDATE SET name = EXCLUDED.name, adapter = EXCLUDED.adapter,
             countries = EXCLUDED.countries, outbound = EXCLUDED.outbound, inbound = EXCLUDED.inbound,
             enabled = EXCLUDED.enabled, updated_at = now()
           RETURNING *""",
        (key, name, adapter, ctry, Jsonb(outbound), Jsonb(inbound), bool(body.get("enabled", True)), actor),
    ).fetchone()
    golive.declare("carrier", key, name, CARRIER_CRITERIA, {"adapter": adapter})
    golive.sync(conn)
    ensure_health_loop(conn)
    render_kamailio(conn)  # Kamailio's trunk and allow-list files follow the carriers
    return _out(conn, row)


def _insert_rates(conn, carrier_id, version: int, lines: list[dict], actor: str) -> None:
    for ln in lines:
        conn.execute(
            """INSERT INTO voice_carrier_rates (carrier_id, version, prefix, name, per_minute, created_by)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                carrier_id,
                version,
                digits(ln.get("prefix", "")),
                str(ln.get("name", ""))[:120],
                money(ln["per_minute"]),
                actor,
            ),
        )
    conn.execute("UPDATE voice_carriers SET rate_version = %s, updated_at = now() WHERE id = %s", (version, carrier_id))


def set_rates(conn: psycopg.Connection, key: str, lines: list[dict], actor: str) -> dict:
    """A new rate sheet version. Old versions stay for reconciliation."""
    c = get(conn, key)
    if not lines:
        raise VoiceError("A rate sheet needs at least one line.", 422)
    seen = set()
    for ln in lines:
        p = digits(ln.get("prefix", ""))
        if p in seen:
            raise VoiceError(f"Prefix {p or '(default)'} is in the sheet twice.", 422)
        seen.add(p)
        try:
            if money(ln.get("per_minute")) < 0:
                raise VoiceError("Rates can't be negative.", 422)
        except (TypeError, ArithmeticError, ValueError) as e:
            raise VoiceError('Each line needs a per_minute rate, as a string like "0.0120".', 422) from e
    _insert_rates(conn, c["id"], c["rate_version"] + 1, lines, actor)
    return _out(conn, get(conn, key))


def rate_for(conn, carrier: dict, number: str, version: int | None = None) -> dict | None:
    d = digits(number)
    row = conn.execute(
        """SELECT prefix, name, per_minute FROM voice_carrier_rates
           WHERE carrier_id = %s AND version = %s AND %s LIKE prefix || '%%'
           ORDER BY length(prefix) DESC LIMIT 1""",
        (carrier["id"], version or carrier["rate_version"], d),
    ).fetchone()
    return row


# ---- health ---------------------------------------------------------------------------


def quality(conn, c: dict) -> dict:
    rows = conn.execute(
        "SELECT ok, rtt_ms FROM voice_carrier_options WHERE carrier_id = %s ORDER BY at DESC, id DESC LIMIT 20",
        (c["id"],),
    ).fetchall()
    if not rows:
        return {"score": 50.0, "success_pct": None, "avg_rtt_ms": None, "samples": 0}
    ok = [r for r in rows if r["ok"]]
    pct = 100.0 * len(ok) / len(rows)
    rtt = sum(r["rtt_ms"] or 0 for r in ok) / len(ok) if ok else None
    score = pct - (rtt or 500) / 10.0
    return {
        "score": round(score, 1),
        "success_pct": round(pct, 1),
        "avg_rtt_ms": round(rtt) if rtt else None,
        "samples": len(rows),
    }


def record_options(conn: psycopg.Connection, c: dict, res: dict) -> dict:
    """Store one OPTIONS answer and move the trunk's health."""
    conn.execute(
        "INSERT INTO voice_carrier_options (carrier_id, ok, rtt_ms, code) VALUES (%s, %s, %s, %s)",
        (c["id"], res["ok"], res.get("rtt_ms"), int(res.get("code") or 0)),
    )
    fails = 0 if res["ok"] else c["consecutive_failures"] + 1
    health = "up" if res["ok"] else ("down" if fails >= DOWN_AFTER else "degraded")
    row = conn.execute(
        """UPDATE voice_carriers SET consecutive_failures = %s, health = %s, last_options_at = now(),
             last_rtt_ms = %s, updated_at = now() WHERE id = %s RETURNING *""",
        (fails, health, res.get("rtt_ms"), c["id"]),
    ).fetchone()
    if health != c["health"] and "down" in (health, c["health"]):
        audit.record(
            conn, "system:voice", "commai.voice.trunk_health", c["key"], None, {"from": c["health"], "to": health}
        )
    conn.execute(
        "DELETE FROM voice_carrier_options WHERE carrier_id = %s AND at < now() - interval '7 days'", (c["id"],)
    )
    return row


def check_trunks(conn: psycopg.Connection, key: str | None = None) -> list[dict]:
    prov = providers.get()
    out = []
    rows = conn.execute(
        "SELECT * FROM voice_carriers WHERE enabled AND (%s::text IS NULL OR key = %s) ORDER BY key", (key, key)
    ).fetchall()
    for c in rows:
        try:
            res = prov.options_ping(conn, c)
        except providers.NotConfigured:
            continue
        r = record_options(conn, c, res)
        out.append({"key": r["key"], "health": r["health"], "ok": res["ok"], "rtt_ms": res.get("rtt_ms")})
    return out


def ensure_health_loop(conn: psycopg.Connection) -> None:
    bucket = int(dt.datetime.now(dt.UTC).timestamp() // 60)
    jobs.enqueue(conn, "voice.trunk_options", {}, dedupe_key=f"voice.trunk_options:{bucket}", delay_s=60)


@jobs.handler("voice.trunk_options")
def _options_job(conn: psycopg.Connection, job: dict):
    check_trunks(conn)
    bucket = int(dt.datetime.now(dt.UTC).timestamp() // 60) + 1
    jobs.enqueue(conn, "voice.trunk_options", {}, dedupe_key=f"voice.trunk_options:{bucket}", delay_s=60)
    return None


# ---- routing --------------------------------------------------------------------------


def set_rule(conn: psycopg.Connection, prefix: str, mode: str, actor: str) -> None:
    if mode not in ("lcr", "quality"):
        raise VoiceError("Routing is lcr (least cost) or quality.", 422)
    conn.execute(
        """INSERT INTO voice_route_rules (prefix, mode, updated_by) VALUES (%s, %s, %s)
           ON CONFLICT (prefix) DO UPDATE SET mode = EXCLUDED.mode, updated_by = EXCLUDED.updated_by,
             updated_at = now()""",
        (digits(prefix), mode, actor),
    )


def delete_rule(conn, prefix: str) -> None:
    if not digits(prefix):
        raise VoiceError("The default rule stays; change its mode instead.", 422)
    conn.execute("DELETE FROM voice_route_rules WHERE prefix = %s", (digits(prefix),))


def rules(conn) -> list[dict]:
    return conn.execute("SELECT * FROM voice_route_rules ORDER BY length(prefix), prefix").fetchall()


def mode_for(conn, number: str) -> str:
    row = conn.execute(
        "SELECT mode FROM voice_route_rules WHERE %s LIKE prefix || '%%' ORDER BY length(prefix) DESC LIMIT 1",
        (digits(number),),
    ).fetchone()
    return row["mode"] if row else "lcr"


def any_enabled(conn, cid: Any) -> bool:
    if not conn.execute("SELECT 1 FROM voice_carriers LIMIT 1").fetchone():
        ensure_simulated(conn)
    return any(
        golive.enabled(conn, "carrier", r["key"], cid)
        for r in conn.execute("SELECT key FROM voice_carriers WHERE enabled").fetchall()
    )


def plan(conn: psycopg.Connection, cid: Any, number: str) -> dict:
    """Every carrier with whether it can take this call and why, in try order."""
    mode = mode_for(conn, number)
    rows = []
    for c in conn.execute("SELECT * FROM voice_carriers ORDER BY key").fetchall():
        rate = rate_for(conn, c, number)
        q = quality(conn, c)
        reason = ""
        if not c["enabled"]:
            reason = "Turned off by ExaCarib."
        elif not golive.enabled(conn, "carrier", c["key"], cid):
            reason = "Not switched on (go-live checks)."
        elif c["health"] == "down":
            reason = "Trunk is down (no answer to OPTIONS)."
        elif rate is None:
            reason = "No rate for this destination."
        rows.append(
            {
                "key": c["key"],
                "name": c["name"],
                "health": c["health"],
                "per_minute": s(rate["per_minute"]) if rate else None,
                "rate_name": rate["name"] if rate else "",
                "quality": q["score"],
                "usable": not reason,
                "reason": reason,
                "_cost": money(rate["per_minute"]) if rate else Decimal("1e9"),
            }
        )
    usable = [r for r in rows if r["usable"]]
    if mode == "lcr":
        usable.sort(key=lambda r: (r["_cost"], -r["quality"], r["key"]))
    else:
        usable.sort(key=lambda r: (-r["quality"], r["_cost"], r["key"]))
    order = usable + [r for r in rows if not r["usable"]]
    for r in order:
        r.pop("_cost")
    return {"mode": mode, "carriers": order, "route": [r["key"] for r in usable]}


def route_keys(conn, cid: Any, number: str) -> list[str] | None:
    """The carriers to try, in order. None: no carrier is switched on for this
    business, so the single provider takes the call (stage 4 behaviour)."""
    if not any_enabled(conn, cid):
        return None
    return plan(conn, cid, number)["route"]


def connect(conn: psycopg.Connection, cid: Any, call_id: str, number: str, route: list[str]) -> dict:
    """Hand the call to each carrier in turn until one takes it."""
    prov = providers.get()
    tried = []
    for pos, key in enumerate(route):
        c = get(conn, key)
        res = prov.place_call(conn, c, number)
        conn.execute(
            """INSERT INTO voice_route_attempts (customer_id, call_id, position, carrier, ok, detail)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (cid, call_id, pos, key, res["ok"], res["detail"][:300]),
        )
        tried.append({"carrier": key, "ok": res["ok"], "detail": res["detail"]})
        if res["ok"]:
            return {"ok": True, "carrier": key, "attempts": tried}
    return {
        "ok": False,
        "carrier": "",
        "attempts": tried,
        "reason": "No carrier could take the call: " + "; ".join(t["detail"] for t in tried),
    }


def attach(conn: psycopg.Connection, cdr: dict, carrier_key: str) -> None:
    """Record which carrier carried a call and what it costs ExaCarib."""
    c = get(conn, carrier_key)
    rate = rate_for(conn, c, cdr["to_number"])
    cost = q4(Decimal(cdr["seconds"]) / 60 * money(rate["per_minute"])) if rate else None
    conn.execute("UPDATE voice_cdrs SET carrier = %s, carrier_cost = %s WHERE id = %s", (carrier_key, cost, cdr["id"]))


# ---- supplier side, per carrier ---------------------------------------------------------


def import_from_provider(conn: psycopg.Connection, key: str, period: dt.date, actor: str) -> dict:
    """Fetch the carrier's call records for the month and import them (each call once)."""
    get(conn, key)
    start = month_start(period)
    text = providers.get().fetch_cdrs(conn, key, start, next_month(start))
    return billing.import_supplier_csv(conn, key, text, f"{key}-{start:%Y-%m}.csv", actor)


def reconcile(conn: psycopg.Connection, key: str, period: dt.date) -> dict:
    """What the carrier charged, against its own rate sheet and what ExaCarib billed."""
    c = get(conn, key)
    start = month_start(period)
    end = next_month(start)
    rows = conn.execute(
        """SELECT sc.call_ref, sc.destination, sc.seconds, sc.cost, sc.cdr_id, d.seconds AS our_seconds,
                  d.customer_id, coalesce((SELECT sum(amount) FROM voice_charges x WHERE x.cdr_id = d.id), 0) AS billed
           FROM voice_supplier_charges sc LEFT JOIN voice_cdrs d ON d.id = sc.cdr_id
           WHERE sc.supplier = %s AND (sc.started_at IS NULL OR (sc.started_at >= %s AND sc.started_at < %s))
           ORDER BY sc.started_at""",
        (key, start, end),
    ).fetchall()
    charged = expected = billed = Decimal(0)
    issues = []
    for r in rows:
        cost = money(r["cost"])
        charged += cost
        rate = rate_for(conn, c, r["destination"])
        exp = q4(Decimal(r["seconds"]) / 60 * money(rate["per_minute"])) if rate else None
        if exp is not None:
            expected += exp
        billed += money(r["billed"])
        if r["cdr_id"] is None:
            issues.append({"call_ref": r["call_ref"], "issue": "No matching call on our side"})
        elif exp is None:
            issues.append({"call_ref": r["call_ref"], "issue": "No rate on the sheet for this destination"})
        elif abs(cost - exp) > Decimal("0.0001"):
            issues.append({"call_ref": r["call_ref"], "issue": f"Charged {s(cost)}, the rate sheet gives {s(exp)}"})
        elif r["our_seconds"] is not None and r["our_seconds"] != r["seconds"]:
            issues.append(
                {
                    "call_ref": r["call_ref"],
                    "issue": f"Duration differs: we have {r['our_seconds']} s, carrier {r['seconds']} s",
                }
            )
    missing = conn.execute(
        """SELECT count(*) AS n FROM voice_cdrs d WHERE d.carrier = %s AND d.status = 'completed'
           AND d.ended_at >= %s AND d.ended_at < %s
           AND NOT EXISTS (SELECT 1 FROM voice_supplier_charges sc WHERE sc.cdr_id = d.id AND sc.supplier = %s)""",
        (key, start, end, key),
    ).fetchone()["n"]
    return {
        "carrier": key,
        "period": start.isoformat(),
        "rows": len(rows),
        "charged": s(q2(charged)),
        "expected_from_rates": s(q2(expected)),
        "difference": s(q2(charged - expected)),
        "billed_to_customers": s(q2(billed)),
        "margin": s(q2(billed - charged)),
        "calls_not_on_carrier_records": missing,
        "issues": issues[:200],
    }


# ---- Kamailio configuration -----------------------------------------------------------------


def dispatcher_list(conn: psycopg.Connection) -> str:
    """Kamailio dispatcher.list: one set per carrier (set id = position + 1).
    FreeSWITCH puts the carrier order from the controller in X-Exa-Route."""
    lines = [
        "# Rendered by the ExaCarib controller (voice carriers). Do not edit by hand.",
        "# setid destination flags priority attrs",
    ]
    for i, c in enumerate(conn.execute("SELECT * FROM voice_carriers WHERE enabled ORDER BY key").fetchall(), start=1):
        o = c["outbound"] or {}
        if not o.get("host"):
            continue
        lines.append(
            f"{i} sip:{o['host']}:{o.get('port', 5061)};transport={o.get('transport', 'tls')} 0 0 "
            f"carrier={c['key']};maxload={o.get('max_channels', 30)}"
        )
    return "\n".join(lines) + "\n"


def address_list(conn: psycopg.Connection) -> str:
    """Kamailio permissions address.list: carrier signalling addresses allowed in
    (group 1), and FreeSWITCH (group 2): the networks in EXA_KAMAILIO_PBX_NETS,
    which `make voice-up` sets to the compose network both run on."""
    lines = ["# Rendered by the ExaCarib controller. group ip mask port tag"]
    for c in conn.execute("SELECT * FROM voice_carriers WHERE enabled ORDER BY key").fetchall():
        for net in (c["inbound"] or {}).get("allow_ips", []):
            n = ipaddress.ip_network(net, strict=False)
            lines.append(f"1 {n.network_address} {n.prefixlen} 0 {c['key']}")
    for net in os.environ.get("EXA_KAMAILIO_PBX_NETS", "").split(","):
        if net.strip():
            n = ipaddress.ip_network(net.strip(), strict=False)
            lines.append(f"2 {n.network_address} {n.prefixlen} 0 freeswitch")
    return "\n".join(lines) + "\n"


def carrier_sets(conn: psycopg.Connection) -> dict[str, int]:
    return {
        c["key"]: i
        for i, c in enumerate(
            conn.execute("SELECT key FROM voice_carriers WHERE enabled ORDER BY key").fetchall(), start=1
        )
    }


def render_kamailio(conn: psycopg.Connection) -> dict:
    files = {"dispatcher.list": dispatcher_list(conn), "address.list": address_list(conn)}
    root = os.environ.get("EXA_KAMAILIO_DIR", "")
    if root:
        Path(root).mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            tmp = Path(root) / f".{name}.tmp"
            tmp.write_text(text)
            tmp.replace(Path(root) / name)
    return {"files": files, "written_to": root, "sets": carrier_sets(conn)}
