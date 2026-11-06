"""Controller-owned protected ingress policy; configuration is not runtime proof.

The edge checks the real external transport and a DID-specific source group.
Only the separate private mTLS Sofia context consumes the revision assertion.
Candidates remain pending until the matching immutable local bundle is approved.
The approved bundle continues working during a controller outage.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from typing import Any
from xml.sax.saxutils import quoteattr

from . import provider as providers
from . import signalling, trunk_media
from .common import VoiceError, domain

SCHEMA = "jibsy-protected-ingress-v1"
HEADER = "X-Exa-Ingress-Policy"
CONTEXT = "exacarib-protected"
REVISION = re.compile(r"[0-9a-f]{64}")
DID = re.compile(r"\+[1-9][0-9]{6,14}")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _fs(value: str) -> str:
    return "$" + "{" + value + "}"


def describe(number: dict, carrier: dict | None) -> dict | None:
    """None means legacy; invalid organisation-owned policy remains blocked."""
    owned = number.get("source") == "trunk"
    if carrier is None:
        return {"state": "blocked", "revision": "", "reason": "missing_owned_carrier"} if owned else None
    try:
        policy = trunk_media.mode(carrier.get("outbound") or {}, carrier.get("adapter", ""))
        if policy == trunk_media.LEGACY and not owned:
            return None
        if (
            str(number.get("customer_id")) != str(carrier.get("customer_id"))
            or carrier.get("platform") is not None
            or number.get("carrier") != carrier.get("key")
        ):
            raise ValueError("carrier_owner")
        if policy == trunk_media.LEGACY:
            return None
        if carrier.get("enabled") is not True or carrier.get("adapter") != "sip":
            raise ValueError("carrier_disabled")
        if type(carrier.get("set_id")) is not int or carrier["set_id"] < 1:
            raise ValueError("carrier_set")
        outbound = carrier["outbound"]
        port = outbound.get("port", 5061)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("carrier_port")
        destination = signalling.destination(outbound["host"], port, "tls")
        e164 = number["e164"]
        if not isinstance(e164, str) or not DID.fullmatch(e164):
            raise ValueError("did")
        networks = (carrier.get("inbound") or {}).get("allow_ips")
        if not isinstance(networks, list) or not 1 <= len(networks) <= 32:
            raise ValueError("source_acl")
        parsed = {ipaddress.ip_network(net, strict=False) for net in networks}
        if any(net.num_addresses > 256 for net in parsed):
            raise ValueError("source_acl")
        sources = [
            str(net) for net in sorted(parsed, key=lambda net: (net.version, int(net.network_address), net.prefixlen))
        ]
        body = {
            "schema": SCHEMA,
            "customer_id": str(number["customer_id"]),
            "number_id": str(number["id"]),
            "did": e164,
            "target_type": number.get("target_type"),
            "target_id": str(number.get("target_id") or ""),
            "carrier_id": str(carrier["id"]),
            "carrier_key": carrier["key"],
            "carrier_set": carrier["set_id"],
            "destination": destination,
            "origin_auth": "private_header_sha256_v1",
            "sources": sources,
            "media": trunk_media.SRTP_REQUIRED,
            "transport": "tls",
        }
        revision = digest(body)
        # Separate from groups 1/2; reject collisions across the full snapshot.
        group = 1000 + int(revision[:7], 16)
        return {"state": "protected", "revision": revision, "group": group, **body}
    except (KeyError, TypeError, ValueError, VoiceError):
        return {"state": "blocked", "revision": "", "reason": "invalid_owned_carrier_policy"}


def _number_rows(conn, *, customer_id=None, e164=None) -> list[dict]:
    # One statement supplies all owner/policy/target attributes.
    where, params = ["(%s OR n.provider <> 'simulated')"], [providers.test_mode()]
    if customer_id is not None:
        where.append("n.customer_id = %s")
        params.append(customer_id)
    if e164 is not None:
        where.append("n.e164 = %s")
        params.append(e164)
    extra = " AND " + " AND ".join(where) if where else ""
    return conn.execute(
        """SELECT n.id::text AS id, n.customer_id::text AS customer_id, n.e164, n.source,
                  n.carrier, n.target_type, n.target_id::text AS target_id,
                  c.id::text AS carrier_id, c.key AS carrier_key,
                  c.customer_id::text AS carrier_customer_id, c.platform AS carrier_platform,
                  c.adapter AS carrier_adapter, c.enabled AS carrier_enabled,
                  c.set_id AS carrier_set_id,
                  c.outbound AS carrier_outbound, c.inbound AS carrier_inbound
           FROM voice_numbers n LEFT JOIN voice_carriers c
             ON c.key = n.carrier AND c.customer_id = n.customer_id AND c.platform IS NULL
           WHERE n.status = 'active'"""
        + extra
        + " ORDER BY n.e164",
        tuple(params),
    ).fetchall()


def descriptor(row: dict) -> dict | None:
    names = ("id", "key", "customer_id", "platform", "adapter", "enabled", "outbound", "inbound", "set_id")
    carrier = None if row.get("carrier_id") is None else {name: row.get("carrier_" + name) for name in names}
    return describe(row, carrier)


def edge_files(conn, legacy_addresses: str) -> dict[str, str]:
    """Render a candidate only. The operator loader activates one exact digest."""
    entries, groups, seen = [], {}, set()
    addresses = legacy_addresses
    for row in _number_rows(conn):
        policy = descriptor(row)
        if policy is None and row.get("source") != "trunk":
            continue
        did = row["e164"]
        if not DID.fullmatch(did) or did in seen:
            raise ValueError("Duplicate or invalid protected DID in controller snapshot.")
        seen.add(did)
        if policy and policy["state"] == "protected":
            group = policy["group"]
            if group in groups and groups[group] != policy["revision"]:
                raise ValueError("Protected source group collision; refuse this generation.")
            groups[group] = policy["revision"]
            for source in policy["sources"]:
                net = ipaddress.ip_network(source)
                addresses += f"{group} {net.network_address} {net.prefixlen} 0 protected\n"
        entries.append(
            {
                "did": did,
                "tenant": domain(row["customer_id"]).split(".")[0],
                "state": policy["state"] if policy else "legacy",
                "revision": policy["revision"] if policy else "LEGACY",
                "group": policy.get("group", 0) if policy else 0,
                "sources": policy.get("sources", []) if policy else [],
                "carrier_set": policy.get("carrier_set", 0) if policy else 0,
                "destination": policy.get("destination", "") if policy else "",
            }
        )
    body = {
        "schema": SCHEMA,
        "entries": entries,
        "address_sha256": hashlib.sha256(addresses.encode()).hexdigest(),
    }
    manifest = {**body, "revision": digest(body)}
    return {"address.list": addresses, "protected-ingress.json": canonical(manifest) + "\n"}


def _route_actions(data: dict, number: dict) -> list[str]:
    from . import freeswitch

    q, did = quoteattr, number["e164"]
    rule = next((r for r in data.get("ai_rules", ()) if r.get("number_id") in (None, number["id"])), None)
    fallback = freeswitch._fallback_target(data, rule["fallback"]) if rule else "exa-voicemail-main"
    return [
        f'      <action application="set" data={q("domain_name=" + data["domain"])}/>',
        f'      <action application="export" data={q("exa_ai_dialled=" + did)}/>',
        f'      <action application="export" data={q("exa_ai_fallback=" + fallback)}/>',
        f'      <action application="transfer" data={q("exa-in-" + did[1:] + " XML " + data["tenant"])}/>',
    ]


def render_owned_public(data: dict) -> str:
    """Candidate owned routes. Only the approved local bundle includes them."""
    q, out = quoteattr, ["<include>"]
    for number in data["numbers"]:
        if number.get("source") != "trunk":
            continue
        did, policy = number["e164"], number.get("protected_ingress")
        if not DID.fullmatch(did):
            raise ValueError("Invalid owned DID.")
        state = "deny" if policy else "in"
        expression = r"^\+?" + did[1:] + "$"
        out += [
            f'  <extension name="owned-{state}-{did[1:]}">',
            f'    <condition field="destination_number" expression={q(expression)}>',
        ]
        out += ['      <action application="hangup" data="CALL_REJECTED"/>'] if policy else _route_actions(data, number)
        out += ["    </condition>", "  </extension>"]
    return "\n".join([*out, "</include>", ""])


def render_private(data: dict) -> str:
    """Fragments included ONLY by the mTLS context; never include in public."""
    q, out = quoteattr, ["<include>"]
    for number in data["numbers"]:
        policy = number.get("protected_ingress")
        if not policy or policy["state"] != "protected":
            continue
        revision, did = policy["revision"], number["e164"]
        if not REVISION.fullmatch(revision) or not DID.fullmatch(did):
            raise ValueError("Malformed private ingress descriptor.")
        digits = did[1:]
        expression = r"^\+?" + digits + "$"
        out += [
            f'  <extension name="protected-in-{digits}">',
            f'    <condition field="destination_number" expression={q(expression)}/>',
            f"    <condition field={q(_fs('sip_h_' + HEADER))} expression={q('^' + revision + '$')}>",
            f'      <action application="set" data={q("rtp_secure_media_inbound=mandatory:" + trunk_media.SUITE)}/>',
            *_route_actions(data, number),
            "    </condition>",
            "  </extension>",
        ]
    out += ["</include>", ""]
    return "\n".join(out)
