"""Syslog: RFC 5424 messages over UDP (RFC 5426), TCP (RFC 6587 octet
counting) or TLS (RFC 5425), for events and, when subscribed to
``audit.recorded``, the audit log."""

from __future__ import annotations

import datetime as dt
import socket

from ..transport import tls_context
from . import Context, Field, Outcome, Provider, ProviderError, kind_name, register, summary

# RFC 5612 reserves enterprise number 32473 for documentation. ExaCarib should
# register its own Private Enterprise Number with IANA and set EXA_IANA_PEN.
DEFAULT_PEN = "32473"
FACILITIES = {
    "kern": 0,
    "user": 1,
    "daemon": 3,
    "auth": 4,
    "local0": 16,
    "local1": 17,
    "local2": 18,
    "local3": 19,
    "local4": 20,
    "local5": 21,
    "local6": 22,
    "local7": 23,
}
SEVERITY = {"critical": 2, "warning": 4, "info": 6}
BOM = "﻿"


def pen() -> str:
    import os

    return os.environ.get("EXA_IANA_PEN", DEFAULT_PEN)


def _sd_value(v: object) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("]", "\\]")


def _token(s: str, limit: int) -> str:
    """PRINTUSASCII without spaces, or '-' (RFC 5424 §6)."""
    out = "".join(c for c in s if 33 <= ord(c) <= 126)[:limit]
    return out or "-"


def format_5424(ev: dict, facility: str = "local0", hostname: str = "exacarib-connect", app: str = "connect") -> str:
    pri = FACILITIES.get(facility, 16) * 8 + SEVERITY.get(ev.get("severity", "info"), 6)
    ts = ev["time"]
    if isinstance(ts, dt.datetime):
        ts = ts.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
    data = ev.get("data") or {}
    params = {"id": ev["id"], "type": ev["type"], "action": ev.get("action", "notify")}
    if ev.get("dedupkey"):
        params["dedupkey"] = ev["dedupkey"]
    for k in ("site", "path", "class", "carrier", "node", "actor", "target"):
        if data.get(k):
            params[k] = data[k]
    sd = f"[exacarib@{pen()} " + " ".join(f'{k}="{_sd_value(v)}"' for k, v in params.items()) + "]"
    return (
        f"<{pri}>1 {ts} {_token(hostname, 255)} {_token(app, 48)} - {_token(kind_name(ev), 32)} {sd} {BOM}{summary(ev)}"
    )


def frame(msg: str, transport: str) -> bytes:
    raw = msg.encode("utf-8")
    if transport == "udp":
        return raw
    # Octet counting: MSG-LEN SP SYSLOG-MSG (RFC 6587 §3.4.1, RFC 5425 §4.3).
    return str(len(raw)).encode() + b" " + raw


@register
class Syslog(Provider):
    key = "syslog"
    name = "Syslog (RFC 5424)"
    category = "logs"
    docs = "https://www.rfc-editor.org/rfc/rfc5424"
    api = "RFC 5424 over UDP (RFC 5426), TCP with octet counting (RFC 6587) or TLS (RFC 5425)"
    live_needs = "Your collector's host and port, the transport, and for TLS its CA certificate if it is private."
    fields = (
        Field("host", "Host", required=True),
        Field("port", "Port", default=6514, kind="int"),
        Field("transport", "Transport", default="tls", kind="choice", choices=("udp", "tcp", "tls")),
        Field("facility", "Facility", default="local0", kind="choice", choices=tuple(FACILITIES)),
        Field("hostname", "Host name in messages", default="exacarib-connect"),
        Field("ca_pem", "CA certificate (PEM, for TLS)", help="Only for a collector with a private CA."),
    )
    owners = ("customer",)

    def validate(self, config: dict) -> dict:
        out = super().validate(config)
        if not 1 <= int(out.get("port", 0)) <= 65535:
            raise ValueError("Port must be 1 to 65535.")
        if any(c.isspace() or c in "/:@" for c in out["host"]):
            raise ValueError("Host is a name or address only.")
        return out

    def deliver(self, ctx: Context, ev: dict) -> Outcome:
        cfg = ctx.config
        transport = cfg.get("transport", "tls")
        payload = frame(
            format_5424(ev, cfg.get("facility", "local0"), cfg.get("hostname", "exacarib-connect")), transport
        )
        send_bytes(ctx, cfg["host"], int(cfg.get("port", 6514)), transport, payload, cfg.get("ca_pem", ""))
        return Outcome(None, {"transport": transport, "bytes": len(payload)})


def send_bytes(ctx: Context, host: str, port: int, transport: str, payload: bytes, ca_pem: str = "") -> None:
    def send() -> None:
        if transport == "udp":
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.sendto(payload, (host, port))
            return
        with socket.create_connection((host, port), timeout=10) as raw:
            if transport == "tls":
                with tls_context(ca_pem).wrap_socket(raw, server_hostname=host) as s:
                    s.sendall(payload)
            else:
                raw.sendall(payload)

    if transport not in ("udp", "tcp", "tls"):
        raise ProviderError(f"Unknown transport {transport}.", retry=False)
    ctx.http.raw(f"SYSLOG/{transport.upper()}", host, port, payload, send)
