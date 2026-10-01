"""Seed the Phase A lab: one customer, two sites, the Miami PoP, three paths
per site, three application classes with SLAs, and fresh enrolment tokens.

    python -m exaconnect_controller.seed --lab

Prints JSON with the CA fingerprint, the agent URL and one token per node, for
lab/scripts/agents.sh. Safe to re-run: inventory is upserted and new tokens are
issued each time."""

from __future__ import annotations

import argparse
import json
import sys

from . import db, desired, inventory, pki
from .settings import get_settings

ACTOR = "system:seed"
CUSTOMER = "Demo Organisation"

# (name, kind, location, timezone, asn, lan, overlay host, site octet)
LAB_SITES = [
    ("pop-miami", "pop", "Miami", "America/New_York", 65000, ["10.200.0.0/24"], 1, "0"),
    ("site-a", "site", "Kingston", "America/Jamaica", 65001, ["192.168.10.0/24"], 11, "1"),
    ("site-b", "site", "Port of Spain", "America/Port_of_Spain", 65002, ["192.168.20.0/24"], 12, "2"),
]
# (path, carrier, underlay type, interface, second octet, commit Mbps, cost per Mbps, burst price)
LAB_LINKS = [
    ("carrier-a", "Carrier A", "fibre", "eth1", "11", 100, 4.0, 6.0),
    ("carrier-b", "Carrier B", "broadband", "eth2", "12", 50, 2.5, 4.0),
    ("sat", "Satellite", "leo", "eth3", "13", 20, 12.0, 20.0),
]
# Bulk matches CS1 only: unmarked traffic is not classified and follows BGP.
# Bulk's 5% loss threshold is its "own threshold" in demo step 2.
CLASSES = [
    ("voice", "SIP/RTP, Teams and Zoom media", [46, 34], "udp:5060,10000-20000", 150, 30, 1, True),
    ("business", "ERP, core banking, VDI", [26, 18], "tcp:443,3389,1521", 250, None, 2, True),
    ("bulk", "Backups and updates", [8], "", None, None, 5, False),
]


def seed_lab(conn) -> dict:
    cid = inventory.ensure_customer(conn, CUSTOMER, ACTOR)
    for ordinal, (name, desc, dscp, ports, lat, jit, loss, sat) in enumerate(CLASSES, 1):
        conn.execute(
            """INSERT INTO app_classes (customer_id, name, description, dscp, ports, ordinal)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (customer_id, name) DO UPDATE SET description = EXCLUDED.description,
                 dscp = EXCLUDED.dscp, ports = EXCLUDED.ports, ordinal = EXCLUDED.ordinal""",
            (cid, name, desc, dscp, ports, ordinal),
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
    for name, kind, loc, tz, asn, lan, host, octet in LAB_SITES:
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
        )
        for path, carrier, utype, iface, second, commit, cost, burst in LAB_LINKS:
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
                commit_mbps=commit,
                cost_per_mbps=cost,
                burst_price=burst,
            )
        tokens[name], _ = inventory.issue_token(conn, sid, ACTOR, ttl_hours=2)
    desired.refresh(conn, cid)
    return {"customer_id": str(cid), "tokens": tokens}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lab", action="store_true", help="seed the Phase A containerlab topology")
    args = ap.parse_args()
    if not args.lab:
        ap.error("only --lab is supported")
    s = get_settings()
    db.init(s.database_url)
    ca = pki.load_or_create(s.data_dir, [x.strip() for x in s.tls_sans.split(",") if x.strip()])
    with db.tx() as conn:
        out = seed_lab(conn)
    db.close()
    json.dump({**out, "ca_fingerprint": ca.fingerprint, "agent_url": s.agent_url}, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
