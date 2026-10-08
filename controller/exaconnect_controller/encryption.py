"""The encryption report (ADR 0012): which of a customer's traffic is
encrypted, how, and since when."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import psycopg

from . import fabric

FRESH_S = 120  # telemetry older than this says nothing about now
HANDSHAKE_S = 180  # WireGuard re-keys every 2 minutes while traffic flows
WEAK = [
    (re.compile(r"SHA1\b|SHA1_"), "Uses SHA-1"),
    (re.compile(r"MD5"), "Uses MD5"),
    (re.compile(r"(?<![A-Z])3?DES(?![A-Z])"), "Uses DES"),
    (re.compile(r"MODP_(768|1024|1536)\b"), "Weak key exchange"),
]
WIREGUARD = "ChaCha20-Poly1305, Curve25519 key exchange"


def notes(*ciphers: str) -> list[str]:
    out: list[str] = []
    for text in ciphers:
        for pattern, note in WEAK:
            if text and pattern.search(text) and note not in out:
                out.append(note)
    return out


def _path_status(row: dict, now: dt.datetime) -> str:
    if (now - row["updated_at"]).total_seconds() > FRESH_S:
        return "down"
    age = row["handshake_age_s"]
    if age is not None and 0 <= age < HANDSHAKE_S:
        return "encrypted"
    return "idle" if str(row["bfd"] or "").lower() == "up" else "down"


def report(conn: psycopg.Connection, customer_id: Any) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    paths = conn.execute(
        """SELECT s.name AS site, p.name AS path, p.label, ts.handshake_age_s, ts.bfd, ts.updated_at
           FROM sites s JOIN nodes n ON n.site_id = s.id JOIN tunnel_state ts ON ts.node_id = n.id
           JOIN paths p ON p.tunnel = ts.tunnel
           WHERE s.customer_id = %s AND s.kind = 'site' ORDER BY s.overlay_host, p.ordinal""",
        (customer_id,),
    ).fetchall()
    path_rows = [
        {
            "site": r["site"],
            "path": r["path"],
            "label": r["label"],
            "protocol": "WireGuard",
            "cipher": WIREGUARD,
            "handshake_age_s": r["handshake_age_s"],
            "status": _path_status(r, now),
        }
        for r in paths
    ]
    circuits = conn.execute(
        "SELECT * FROM circuits WHERE customer_id = %s AND deleted_at IS NULL AND enabled ORDER BY id",
        (customer_id,),
    ).fetchall()
    states: dict[tuple[int, int], dict] = {}
    for s in conn.execute(
        "SELECT * FROM circuit_state WHERE circuit_id = ANY(%s) ORDER BY updated_at",
        ([c["id"] for c in circuits],),
    ):
        states[(s["circuit_id"], s["tunnel"])] = s
    circuit_rows: list[dict[str, Any]] = []
    layer2: list[dict[str, Any]] = []
    for c in circuits:
        if c["kind"] == "site":
            layer2.append(
                {
                    "id": c["id"],
                    "name": c["name"],
                    "protocol": "VXLAN inside WireGuard",
                    "status": "encrypted",
                }
            )
            continue
        tunnels = [("primary", 1)] + ([("secondary", 2)] if c["secondary_peer_address"] else [])
        for which, n in tunnels:
            s = states.get((c["id"], n))
            fresh = s is not None and (now - s["updated_at"]).total_seconds() <= FRESH_S
            up = fresh and s["ike"] == "up"
            circuit_rows.append(
                {
                    "id": c["id"],
                    "name": c["name"],
                    "kind": c["kind"],
                    "tunnel": which,
                    "protocol": "IPsec (IKEv2)",
                    "ike_cipher": s["ike_cipher"] if up else "",
                    "esp_cipher": s["esp_cipher"] if up else "",
                    "established_s": s["established_s"] if up else -1,
                    "status": "encrypted" if up else "down",
                    "notes": notes(s["ike_cipher"], s["esp_cipher"]) if up else [],
                }
            )
    counted = [p["status"] for p in path_rows] + [c["status"] for c in circuit_rows]
    return {
        "summary": {"encrypted": sum(st in ("encrypted", "idle") for st in counted), "total": len(counted)},
        "paths": path_rows,
        "circuits": circuit_rows,
        "layer2": layer2,
        "control": {"protocol": "TLS with client certificates (mutual TLS)"},
        "proposals": {"ike": fabric.IKE_PROPOSALS, "esp": fabric.ESP_PROPOSALS},
        "internet": "Internet traffic is encrypted between your sites and the PoP. "
        "Beyond the PoP it travels as your applications send it.",
    }
