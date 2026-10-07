"""Seed the Phase A lab: one customer, two sites, the Miami PoP, three paths
per site, three application classes with SLAs, and fresh enrolment tokens.

    python -m exaconnect_controller.seed --lab [--sat leo|geo]

Prints JSON with the CA fingerprint, the agent URL and one token per node, for
lab/scripts/agents.sh. Safe to re-run: inventory is upserted and new tokens are
issued each time. The satellite link is LEO unless --sat geo or SAT_PROFILE=geo
(the same switch lab/netem uses for the satellite router's profile)."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import db, desired, inventory, pki
from .billing import plans
from .settings import get_settings

ACTOR = "system:seed"
CUSTOMER = "Demo Organisation"

# (name, kind, location, timezone, asn, lan, overlay host, site octet, latitude, longitude)
LAB_SITES = [
    ("pop-miami", "pop", "Miami", "America/New_York", 65000, ["10.200.0.0/24"], 1, "0", 25.76, -80.19),
    ("site-a", "site", "Kingston", "America/Jamaica", 65001, ["192.168.10.0/24"], 11, "1", 17.97, -76.79),
    ("site-b", "site", "Port of Spain", "America/Port_of_Spain", 65002, ["192.168.20.0/24"], 12, "2", 10.66, -61.51),
]
# (path, carrier, underlay type, interface, second octet, commit Mbps, cost per Mbps, burst price, speed Mbps)
LAB_LINKS = [
    ("carrier-a", "Carrier A", "fibre", "eth1", "11", 100, 4.0, 6.0, 200),
    ("carrier-b", "Carrier B", "broadband", "eth2", "12", 50, 2.5, 4.0, 100),
    ("sat", "Satellite", "leo", "eth3", "13", 20, 12.0, 20.0, 50),
]
# Bulk matches CS1 only: unmarked traffic is not classified and follows BGP.
# Bulk's 5% loss threshold is its "own threshold" in demo step 2.
# The three built-in classes carry the three queue priorities (ADR 0007).
CLASSES = [
    ("voice", "SIP/RTP, Teams and Zoom media", [46, 34], "udp:5060,10000-20000", 150, 30, 1, True, "realtime"),
    ("business", "ERP, core banking, VDI", [26, 18], "tcp:443,3389,1521", 250, None, 2, True, "interactive"),
    ("bulk", "Backups and updates", [8], "", None, None, 5, False, "bulk"),
]


SAT_TYPES = ("leo", "geo")


def seed_lab(conn, sat_type: str = "leo") -> dict:
    if sat_type not in SAT_TYPES:
        raise ValueError(f"satellite type must be one of {', '.join(SAT_TYPES)}, not {sat_type!r}")
    cid = inventory.ensure_customer(conn, CUSTOMER, ACTOR)
    conn.execute("UPDATE customers SET example = true WHERE id = %s", (cid,))
    for ordinal, (name, desc, dscp, ports, lat, jit, loss, sat, prio) in enumerate(CLASSES, 1):
        conn.execute(
            """INSERT INTO app_classes (customer_id, name, description, dscp, ports, ordinal, priority, builtin)
               VALUES (%s, %s, %s, %s, %s, %s, %s, true)
               ON CONFLICT (customer_id, name) DO UPDATE SET description = EXCLUDED.description,
                 dscp = EXCLUDED.dscp, ports = EXCLUDED.ports, ordinal = EXCLUDED.ordinal,
                 priority = EXCLUDED.priority, builtin = true""",
            (cid, name, desc, dscp, ports, ordinal, prio),
        )
        conn.execute(
            """INSERT INTO sla_policies (customer_id, class_name, max_latency_ms, max_jitter_ms, max_loss_pct,
                                        allow_satellite)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (customer_id, class_name) DO UPDATE SET max_latency_ms = EXCLUDED.max_latency_ms,
                 max_jitter_ms = EXCLUDED.max_jitter_ms, max_loss_pct = EXCLUDED.max_loss_pct,
                 allow_satellite = EXCLUDED.allow_satellite""",
            (cid, name, lat, jit, loss, sat),
        )
    tokens = {}
    for name, kind, loc, tz, asn, lan, host, octet, lat, lon in LAB_SITES:
        sid = inventory.upsert_site(
            conn,
            cid,
            ACTOR,
            name=name,
            kind=kind,
            location=loc,
            timezone=tz,
            asn=asn,
            lan_prefixes=lan,
            overlay_host=host,
            latitude=lat,
            longitude=lon,
            # The PoP reaches the lab's simulated clouds through the "ix" router.
            cloud_interface="eth5" if kind == "pop" else None,
            cloud_address="100.64.0.2/24" if kind == "pop" else None,
            # ...which also stands in for its internet upstream (ADR 0010).
            internet_interface="eth5" if kind == "pop" else None,
            internet_gateway="100.64.0.1" if kind == "pop" else None,
        )
        for path, carrier, utype, iface, second, commit, cost, burst, speed in LAB_LINKS:
            if path == "sat":
                utype = sat_type
            carrier_id = inventory.ensure_carrier(conn, carrier, ACTOR)
            inventory.upsert_link(
                conn,
                cid,
                sid,
                ACTOR,
                carrier_id=carrier_id,
                path=path,
                underlay_type=utype,
                underlay_interface=iface,
                underlay_ip=f"10.{second}.{octet}.2/24",
                underlay_gateway=f"10.{second}.{octet}.1",
                commit_mbps=commit,
                cost_per_mbps=cost,
                burst_price=burst,
                shape_mbps=speed,
            )
        tokens[name], _ = inventory.issue_token(conn, sid, ACTOR, ttl_hours=2)
    desired.refresh(conn, cid)
    # The lab organisation holds both plans (ADR 0022), so the CommAI screens work
    # in the demo too: the standard plans from the day it was added.
    plans.ensure_connect(conn, cid, ACTOR)
    plans.ensure_plan(conn, cid, "commai", ACTOR)
    return {"customer_id": str(cid), "tokens": tokens}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lab", action="store_true", help="seed the Phase A containerlab topology")
    ap.add_argument(
        "--sat",
        choices=SAT_TYPES,
        default=os.environ.get("SAT_PROFILE") or "leo",
        help="satellite underlay type (default: $SAT_PROFILE, else leo)",
    )
    args = ap.parse_args(argv)
    if not args.lab:
        ap.error("only --lab is supported")
    if args.sat not in SAT_TYPES:
        ap.error(f"SAT_PROFILE must be one of {', '.join(SAT_TYPES)}")
    return args


def main() -> None:
    args = parse_args()
    s = get_settings()
    db.init(s.database_url)
    ca = pki.load_or_create(s.data_dir, s.tls_names())
    with db.tx() as conn:
        out = seed_lab(conn, args.sat)
    db.close()
    json.dump({**out, "ca_fingerprint": ca.fingerprint, "agent_url": s.agent_url}, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
