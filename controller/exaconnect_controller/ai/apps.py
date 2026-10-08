"""The application catalogue: well-known business applications and how to
recognise their traffic (ports, published address ranges, domain names).

Used two ways: a traffic rule can name applications instead of ports and
addresses, and application detection labels flows it recognises. Ranges are
the vendors' published ones for real-time media; domains are resolved by the
agents. The catalogue is deliberately small and conservative: a wrong label
is worse than "unrecognised", which the behaviour classifier then handles.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

from ..ports import parse_ports

PRIORITIES = ("realtime", "interactive", "normal", "bulk")
# DSCP each priority is marked with on the way into a tunnel; CAKE's diffserv4
# tins: EF -> Voice, AF31 -> Video (the interactive tin), CS1 -> Bulk.
PRIORITY_DSCP = {"realtime": 46, "interactive": 26, "bulk": 8}
# The built-in class for each priority, used when suggesting where traffic belongs.
PRIORITY_CLASS = {"realtime": "voice", "interactive": "business", "bulk": "bulk"}


@dataclass(frozen=True)
class Signature:
    """One way to recognise the app: every non-empty part must match."""

    ports: str = ""
    subnets: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class App:
    id: str
    name: str
    category: str
    priority: str
    signatures: tuple[Signature, ...] = field(default_factory=tuple)


CATALOGUE: tuple[App, ...] = (
    App(
        "teams",
        "Microsoft Teams calls",
        "Calls and meetings",
        "realtime",
        (
            Signature("udp:3478-3481", ("13.107.64.0/18", "52.112.0.0/14", "52.122.0.0/15")),
            Signature("udp:3478-3481"),
        ),
    ),
    App("zoom", "Zoom meetings", "Calls and meetings", "realtime", (Signature("udp:8801-8810 tcp:8801-8802"),)),
    App("webex", "Webex meetings", "Calls and meetings", "realtime", (Signature("udp:9000,5004"),)),
    App("google-meet", "Google Meet", "Calls and meetings", "realtime", (Signature("udp:19302-19309"),)),
    App(
        "sip",
        "SIP telephony and RTP",
        "Calls and meetings",
        "realtime",
        (
            Signature("udp:5060 tcp:5060-5061"),
            Signature("udp:10000-20000"),
        ),
    ),
    App("rdp", "Remote Desktop (RDP)", "Remote desktops", "interactive", (Signature("tcp:3389 udp:3389"),)),
    App(
        "citrix",
        "Citrix virtual apps and desktops",
        "Remote desktops",
        "interactive",
        (Signature("tcp:1494,2598 udp:1494,2598"),),
    ),
    App("horizon", "VMware Horizon", "Remote desktops", "interactive", (Signature("tcp:4172,22443 udp:4172,22443"),)),
    App("sap", "SAP GUI and RFC", "Business systems", "interactive", (Signature("tcp:3200-3299,3300-3399,3600-3699"),)),
    App("oracle-db", "Oracle Database", "Business systems", "interactive", (Signature("tcp:1521"),)),
    App("sql-server", "Microsoft SQL Server", "Business systems", "interactive", (Signature("tcp:1433"),)),
    App("ssh", "SSH", "Administration", "interactive", (Signature("tcp:22"),)),
    App("dns", "DNS", "Network services", "interactive", (Signature("udp:53 tcp:53"),)),
    App("rsync", "rsync backups", "Backups and updates", "bulk", (Signature("tcp:873"),)),
    App("veeam", "Veeam backups", "Backups and updates", "bulk", (Signature("tcp:6160-6162,10005-10006"),)),
    App(
        "windows-update",
        "Windows Update",
        "Backups and updates",
        "bulk",
        (Signature("tcp:80,443", domains=("download.windowsupdate.com", "dl.delivery.mp.microsoft.com")),),
    ),
)
BY_ID = {a.id: a for a in CATALOGUE}


def _ports(spec: str) -> list[dict]:
    return parse_ports(spec)


def recognise(proto: str, dport: int, dst: str) -> App | None:
    """The catalogue app a flow belongs to, from its protocol, port and
    destination. Signatures with domains can't be checked from a flow alone
    and are skipped here (the agents match those by resolved address)."""
    try:
        addr = ipaddress.ip_address(dst)
    except ValueError:
        return None
    for app in CATALOGUE:
        for sig in app.signatures:
            if sig.domains:
                continue
            ports = _ports(sig.ports)
            if ports and not any(p["proto"] == proto and p["from"] <= dport <= p["to"] for p in ports):
                continue
            if sig.subnets and not any(addr in ipaddress.ip_network(s) for s in sig.subnets):
                continue
            if ports or sig.subnets:
                return app
    return None


def catalogue_view() -> list[dict]:
    return [
        {
            "id": a.id,
            "name": a.name,
            "category": a.category,
            "priority": a.priority,
            "signatures": [
                {"ports": s.ports, "subnets": list(s.subnets), "domains": list(s.domains)} for s in a.signatures
            ],
        }
        for a in CATALOGUE
    ]
