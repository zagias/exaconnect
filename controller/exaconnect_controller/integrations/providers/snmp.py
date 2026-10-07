"""SNMP: v2c traps for path and link health (RFC 3416 SNMPv2-Trap-PDU).

The encoder is a small, self-contained BER writer: a trap is a handful of
fixed fields, so no SNMP library is needed. SNMPv3 (USM authentication and
privacy) and a read-only SNMP agent are left as seams: see
docs/integrations.md. The varbinds live under the enterprise arc
1.3.6.1.4.1.<PEN>.1 (see syslog.pen(); 32473 is the documentation number
until ExaCarib registers its own).
"""

from __future__ import annotations

import socket
import time

from .syslog import pen
from . import Context, Field, Outcome, Provider, kind_name, register, summary

START = time.monotonic()
SEV = {"info": 1, "warning": 2, "critical": 3}
ACTION = {"notify": 1, "trigger": 2, "resolve": 3}


def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _len(len(value)) + value


def integer(n: int, tag: int = 0x02) -> bytes:
    b = n.to_bytes(max(1, (n.bit_length() + 8) // 8), "big", signed=True)
    return tlv(tag, b)


def unsigned(n: int, tag: int) -> bytes:
    b = n.to_bytes(max(1, (n.bit_length() + 8) // 8), "big", signed=False)
    return tlv(tag, b)


def octets(s: str | bytes) -> bytes:
    return tlv(0x04, s.encode() if isinstance(s, str) else s)


def oid(dotted: str) -> bytes:
    parts = [int(p) for p in dotted.strip(".").split(".")]
    out = bytearray([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.append(0x80 | (p & 0x7F))
            p >>= 7
        out += bytes(reversed(chunk))
    return tlv(0x06, bytes(out))


def seq(*items: bytes, tag: int = 0x30) -> bytes:
    return tlv(tag, b"".join(items))


SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
TRAP_OID = "1.3.6.1.6.3.1.1.4.1.0"


def base() -> str:
    return f"1.3.6.1.4.1.{pen()}.1"


TRAPS = {
    "path.down": 1,
    "path.up": 2,
    "node.offline": 3,
    "node.online": 4,
    "sla.breach": 5,
    "carrier.fault": 6,
    "carrier.notice_resolved": 7,
    "maintenance.started": 8,
    "maintenance.ended": 9,
}


def trap_v2c(community: str, ev: dict, request_id: int = 1, uptime_cs: int | None = None) -> bytes:
    name = kind_name(ev)
    data = ev.get("data") or {}
    b = base()
    uptime = int((time.monotonic() - START) * 100) if uptime_cs is None else uptime_cs
    varbinds = [
        seq(oid(SYS_UPTIME), unsigned(uptime & 0xFFFFFFFF, 0x43)),
        seq(oid(TRAP_OID), oid(f"{b}.0.{TRAPS.get(name, 99)}")),
        seq(oid(f"{b}.2.1"), octets(ev["id"])),
        seq(oid(f"{b}.2.2"), octets(name)),
        seq(oid(f"{b}.2.3"), integer(SEV.get(ev.get("severity", "info"), 1))),
        seq(oid(f"{b}.2.4"), octets(str(data.get("site") or ""))),
        seq(oid(f"{b}.2.5"), octets(str(data.get("path") or ""))),
        seq(oid(f"{b}.2.6"), octets(str(data.get("carrier") or ""))),
        seq(oid(f"{b}.2.7"), octets(summary(ev)[:255])),
        seq(oid(f"{b}.2.8"), integer(ACTION.get(ev.get("action", "notify"), 1))),
    ]
    pdu = seq(integer(request_id), integer(0), integer(0), seq(*varbinds), tag=0xA7)
    return seq(integer(1), octets(community), pdu)  # version 1 = SNMPv2c


# ---- a reader, for tests and for anyone checking what a trap carries ---------


def _read(buf: bytes, i: int) -> tuple[int, bytes, int]:
    tag = buf[i]
    n = buf[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(buf[i : i + k], "big")
        i += k
    return tag, buf[i : i + n], i + n


def decode(buf: bytes) -> object:
    """BER to Python: sequences become lists, OIDs dotted strings, ints ints."""
    tag, value, _ = _read(buf, 0)
    return _value(tag, value)


def _value(tag: int, value: bytes) -> object:
    if tag in (0x30, 0xA7):
        out, i = [], 0
        while i < len(value):
            t, v, i = _read(value, i)
            out.append(_value(t, v))
        return out
    if tag == 0x02:
        return int.from_bytes(value, "big", signed=True)
    if tag == 0x43:
        return int.from_bytes(value, "big")
    if tag == 0x04:
        return value.decode("utf-8", "replace")
    if tag == 0x06:
        parts = [value[0] // 40, value[0] % 40]
        n = 0
        for byte in value[1:]:
            n = (n << 7) | (byte & 0x7F)
            if not byte & 0x80:
                parts.append(n)
                n = 0
        return ".".join(map(str, parts))
    return value


@register
class SnmpTrap(Provider):
    key = "snmp"
    name = "SNMP traps (v2c)"
    category = "monitoring"
    docs = "https://www.rfc-editor.org/rfc/rfc3416"
    api = "SNMPv2-Trap-PDU over UDP (RFC 3416, RFC 3417)"
    live_needs = "Your trap receiver's host and port and the community string. SNMPv3 is not built yet."
    fields = (
        Field("host", "Trap receiver", required=True),
        Field("port", "Port", default=162, kind="int"),
        Field("community", "Community", secret=True, required=True),
    )
    owners = ("customer",)

    def deliver(self, ctx: Context, ev: dict) -> Outcome:
        if kind_name(ev) not in TRAPS:
            return Outcome(skipped="only path, node, SLA breach, carrier and maintenance events become traps")
        payload = trap_v2c(ctx.secrets["community"], ev, request_id=abs(hash(ev["id"])) % 2_000_000_000)
        host, port = ctx.config["host"], int(ctx.config.get("port", 162))

        def send() -> None:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.sendto(payload, (host, port))

        # What the outbox keeps: the varbinds in words, never the community.
        shown = f"SNMPv2-Trap {kind_name(ev)} ({len(payload)} bytes): {summary(ev)}".encode()
        ctx.http.raw("SNMP/V2C-TRAP", host, port, shown, send)
        return Outcome(None, {"bytes": len(payload)})
