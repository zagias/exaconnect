"""Port range specs shared by classes, rules and the application catalogue."""

from __future__ import annotations

from typing import Any


def parse_ports(spec: str) -> list[dict[str, Any]]:
    """'udp:5060,10000-20000 tcp:443' or 'udp:5060,tcp:443' -> port ranges."""
    out: list[dict[str, Any]] = []
    proto = None
    for tok in spec.replace(" ", ",").split(","):
        tok = tok.strip().lower()
        if not tok:
            continue
        if ":" in tok:
            proto, tok = tok.split(":", 1)
        if proto not in ("tcp", "udp"):
            raise ValueError(f"port '{tok}' needs a protocol, like udp:5060")
        lo, _, hi = tok.partition("-")
        a, b = int(lo), int(hi or lo)
        if not 1 <= a <= b <= 65535:
            raise ValueError(f"bad port range {tok}")
        out.append({"proto": proto, "from": a, "to": b})
    return out
