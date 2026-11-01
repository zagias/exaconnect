"""Review/create self-hosted SIP resources. Dry run is default and offline.

Input is an operator-reviewed JSON list of {customer_id, e164} owned DIDs.
--apply creates missing resources only; conflicting existing resources are
never changed or deleted. This command does not originate telephone calls.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import re
import uuid
from pathlib import Path


def plan(numbers: list[dict], freeswitch_address: str) -> list[dict]:
    network = ipaddress.ip_network(freeswitch_address, strict=False)
    if (
        network.prefixlen != network.max_prefixlen
        or network.network_address.is_multicast
        or network.network_address.is_unspecified
    ):
        raise ValueError(
            "Use the exact FreeSWITCH source IP (/32 or /128), not a shared network"
        )
    if not numbers or len(numbers) > 1000:
        raise ValueError("Supply between one and 1000 operator-reviewed owned DIDs")
    grouped: dict[str, list[str]] = {}
    seen = set()
    tenants: dict[str, str] = {}
    for number in numbers:
        customer_id = str(uuid.UUID(number["customer_id"]))
        tenant = customer_id.replace("-", "")[:12]
        if tenant in tenants and tenants[tenant] != customer_id:
            raise ValueError(
                "Customer tenant prefixes collide; operator review is required"
            )
        tenants[tenant] = customer_id
        did = number["e164"]
        if not re.fullmatch(r"\+[1-9][0-9]{6,14}", did) or did in seen:
            raise ValueError("Owned DIDs must be unique, canonical E.164 numbers")
        seen.add(did)
        grouped.setdefault(customer_id, []).append(did)
    return [
        {
            "name": "jibsy-phone-" + "c" + cid.replace("-", "")[:12],
            "numbers": sorted(dids),
            "allowed_addresses": [str(network)],
            "room_prefix": "call-c" + cid.replace("-", "")[:12] + "-",
            "agent_name": "exacarib-phone",
            "attributes": {
                "exa.customer_id": cid,
                "exa.tenant": "c" + cid.replace("-", "")[:12],
            },
            "ringing_timeout_s": 8,
            "max_call_duration_s": 1800,
        }
        for cid, dids in sorted(grouped.items())
    ]


def requests_for(item: dict, trunk_id: str):
    from google.protobuf.duration_pb2 import Duration
    from livekit import api

    trunk = api.SIPInboundTrunkInfo(
        name=item["name"],
        numbers=item["numbers"],
        allowed_addresses=item["allowed_addresses"],
        ringing_timeout=Duration(seconds=item["ringing_timeout_s"]),
        max_call_duration=Duration(seconds=item["max_call_duration_s"]),
    )
    dispatch = api.SIPDispatchRuleInfo(
        name=item["name"],
        trunk_ids=[trunk_id],
        attributes=item["attributes"],
        rule=api.SIPDispatchRule(
            dispatch_rule_individual=api.SIPDispatchRuleIndividual(
                room_prefix=item["room_prefix"]
            )
        ),
        room_config=api.RoomConfiguration(
            agents=[api.RoomAgentDispatch(agent_name=item["agent_name"])]
        ),
    )
    return api.CreateSIPInboundTrunkRequest(
        trunk=trunk
    ), api.CreateSIPDispatchRuleRequest(dispatch_rule=dispatch)


def same_trunk(existing, desired) -> bool:
    return same_resource(
        existing, desired, ("sip_trunk_id", "created_at", "updated_at")
    )


def same_dispatch(existing, desired) -> bool:
    return same_resource(
        existing, desired, ("sip_dispatch_rule_id", "created_at", "updated_at")
    )


def same_resource(existing, desired, server_fields) -> bool:
    clone = type(existing)()
    clone.CopyFrom(existing)
    for name in server_fields:
        if name in clone.DESCRIPTOR.fields_by_name:
            clone.ClearField(name)
    return clone == desired


async def apply(items: list[dict]) -> None:
    from livekit import api

    async with api.LiveKitAPI() as client:
        trunks = list(
            (
                await asyncio.wait_for(
                    client.sip.list_sip_inbound_trunk(api.ListSIPInboundTrunkRequest()),
                    10,
                )
            ).items
        )
        rules = list(
            (
                await asyncio.wait_for(
                    client.sip.list_sip_dispatch_rule(api.ListSIPDispatchRuleRequest()),
                    10,
                )
            ).items
        )
        # Review all known conflicts before creating the first resource.
        for item in items:
            matching = [trunk for trunk in trunks if trunk.name == item["name"]]
            named_rules = [rule for rule in rules if rule.name == item["name"]]
            if len(matching) > 1 or len(named_rules) > 1:
                raise ValueError(
                    "Duplicate managed resource names require operator review"
                )
            inbound, desired_rule = requests_for(
                item, matching[0].sip_trunk_id if matching else "pending"
            )
            if matching and not same_trunk(matching[0], inbound.trunk):
                raise ValueError("An existing trunk differs from the reviewed plan")
            if named_rules and (
                not matching
                or not same_dispatch(named_rules[0], desired_rule.dispatch_rule)
            ):
                raise ValueError("An existing dispatch differs from the reviewed plan")
            if any(
                set(item["numbers"]) & set(trunk.numbers) and trunk not in matching
                for trunk in trunks
            ):
                raise ValueError("A selected DID is already assigned to another trunk")
            if any(
                not trunk.numbers
                and (
                    not trunk.allowed_addresses
                    or any(
                        ipaddress.ip_network(
                            item["allowed_addresses"][0]
                        ).network_address
                        in ipaddress.ip_network(address)
                        for address in trunk.allowed_addresses
                    )
                )
                for trunk in trunks
                if trunk not in matching
            ):
                raise ValueError(
                    "A broad existing inbound trunk requires operator review"
                )
            if any(
                not rule.trunk_ids
                or (matching and matching[0].sip_trunk_id in rule.trunk_ids)
                for rule in rules
                if rule not in named_rules
            ):
                raise ValueError(
                    "A broad or overlapping existing dispatch requires operator review"
                )
        for item in items:
            matching = [trunk for trunk in trunks if trunk.name == item["name"]]
            if len(matching) > 1:
                raise ValueError(
                    "Duplicate managed trunk names require operator review"
                )
            if any(
                set(item["numbers"]) & set(trunk.numbers) and trunk not in matching
                for trunk in trunks
            ):
                raise ValueError(
                    "A selected DID is already assigned to another trunk; refusing to change it"
                )
            inbound, _ = requests_for(item, "pending")
            if matching:
                trunk = matching[0]
                if not same_trunk(trunk, inbound.trunk):
                    raise ValueError(
                        "Existing trunk differs from the reviewed plan; refusing to overwrite it"
                    )
            else:
                trunk = await asyncio.wait_for(
                    client.sip.create_sip_inbound_trunk(inbound), 10
                )
                trunks.append(trunk)
            _, dispatch = requests_for(item, trunk.sip_trunk_id)
            matching_rules = [rule for rule in rules if rule.name == item["name"]]
            if len(matching_rules) > 1:
                raise ValueError(
                    "Duplicate managed dispatch names require operator review"
                )
            if matching_rules:
                if not same_dispatch(matching_rules[0], dispatch.dispatch_rule):
                    raise ValueError(
                        "Existing dispatch differs from the reviewed plan; refusing to overwrite it"
                    )
            else:
                rule = await asyncio.wait_for(
                    client.sip.create_sip_dispatch_rule(dispatch), 10
                )
                rules.append(rule)
    print("Reviewed SIP resources created or matched. No call was originated.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--numbers-file", required=True, type=Path)
    parser.add_argument("--freeswitch-address", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        items = plan(json.loads(args.numbers_file.read_text()), args.freeswitch_address)
        if args.apply:
            asyncio.run(apply(items))
        else:
            print(json.dumps({"mode": "review_only", "resources": items}, indent=2))
    except (ValueError, KeyError, TypeError) as error:
        raise SystemExit(str(error)) from None
    except Exception:  # noqa: BLE001 - SDK errors can contain caller data or credentials.
        # SDK failures may include request metadata; never print provider bodies.
        raise SystemExit(
            "SIP configuration was not confirmed. Review existing resources before retrying."
        ) from None


if __name__ == "__main__":
    main()
