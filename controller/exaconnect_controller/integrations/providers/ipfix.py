"""IPFIX flow export (RFC 7011), configured here and carried out by the edge
agents through desired state (ADR 0026).

Unlike the other connectors this one takes no events: saving it adds an
`ipfix` block to each chosen site's desired state, and the site's agent
sends its flow aggregates (protocol, destination, port, bytes and packets
each way, flow count and the Connect class) to the collector every minute.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from . import Field, Provider, register

HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


@register
class IpfixExport(Provider):
    key = "ipfix"
    name = "IPFIX flow export"
    category = "flows"
    docs = "https://www.rfc-editor.org/rfc/rfc7011"
    api = "IPFIX (RFC 7011) over UDP from each site's agent; template 256, reverse counters per RFC 5103"
    live_needs = (
        "A collector reachable from your sites (nfdump/nfcapd, ntopng, pmacct, Elastic, Kentik...): its address "
        "and UDP port, usually 4739."
    )
    receives_events = False
    owners = ("customer",)
    fields = (
        Field("collector_host", "Collector address", required=True, help="A name or IP address your sites reach."),
        Field("collector_port", "UDP port", default=4739, kind="int"),
        Field("observation_domain", "Observation domain ID", default=0, kind="int", help="0: one per site."),
    )

    def validate(self, config: dict) -> dict:
        out = super().validate(config)
        host = str(out["collector_host"])
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not HOST.match(host):
                raise ValueError("Collector address is a name or IP address only.") from None
        if not 1 <= int(out.get("collector_port", 0)) <= 65535:
            raise ValueError("UDP port must be 1 to 65535.")
        if not 0 <= int(out.get("observation_domain", 0)) <= 4294967295:
            raise ValueError("Observation domain ID is 0 to 4294967295.")
        return out

    def credentials_present(self, config: dict, secrets: dict) -> bool:
        return bool(config.get("collector_host"))


def block(integrations: list[dict], site: dict) -> dict[str, Any] | None:
    """The desired-state `ipfix` block for a site, from the organisation's enabled IPFIX integrations
    (the first that covers the site wins)."""
    if site["kind"] != "site":
        return None
    for i in integrations:
        if i["site_ids"] and site["id"] not in i["site_ids"]:
            continue
        cfg = i["config"] or {}
        host = str(cfg.get("collector_host", ""))
        try:
            hostport = f"[{host}]" if ipaddress.ip_address(host).version == 6 else host
        except ValueError:
            hostport = host
        domain = int(cfg.get("observation_domain") or 0) or int(site["overlay_host"])
        return {"collector": f"{hostport}:{int(cfg.get('collector_port') or 4739)}", "observation_domain": domain}
    return None
