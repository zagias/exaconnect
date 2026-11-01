"""Firewall scopes must shrink to their selected sites, never expand by overlap."""

import ipaddress

import pytest

from exaconnect_controller import internet


def rule(site_id=None, src=None):
    return {"id": 1, "site_id": site_id, "src": src or [], "dst": [], "action": "deny", "protocol": "any", "ports": ""}


def test_broad_source_is_intersected_with_the_selected_site():
    sites = [
        {"id": "a", "lan_prefixes": ["192.168.10.0/24"]},
        {"id": "b", "lan_prefixes": ["192.168.20.0/24"]},
    ]
    rendered = internet._rules_for([rule("a", ["0.0.0.0/0"])], sites)
    assert rendered[0]["src"] == ["192.168.10.0/24"]
    assert not any(ipaddress.ip_address("192.168.20.10") in ipaddress.ip_network(p) for p in rendered[0]["src"])


def test_narrow_sources_and_multiple_sites_preserve_only_the_actual_intersections():
    sites = [{"id": "a", "lan_prefixes": ["192.168.10.0/24", "10.20.0.0/24"]}]
    assert internet._rules_for([rule("a", ["192.168.10.128/25", "10.0.0.0/8", "203.0.113.0/24"])], sites)[0]["src"] == [
        "10.20.0.0/24",
        "192.168.10.128/25",
    ]


@pytest.mark.parametrize("sources", [[], ["0.0.0.0/0"]])
def test_empty_site_lan_never_becomes_a_wildcard_rule(sources):
    sites = [{"id": "a", "lan_prefixes": []}, {"id": "b", "lan_prefixes": ["192.168.20.0/24"]}]
    assert internet._rules_for([rule("a", sources)], sites) == []


def test_all_site_rule_does_not_cover_addresses_outside_any_site_lan():
    sites = [{"id": "a", "lan_prefixes": ["192.168.10.0/24"]}, {"id": "b", "lan_prefixes": ["192.168.20.0/24"]}]
    assert internet._rules_for([rule(src=["192.168.0.0/16"])], sites)[0]["src"] == [
        "192.168.10.0/24",
        "192.168.20.0/24",
    ]


def test_off_desired_block_carries_authoritative_corporate_destinations():
    site = {"kind": "site", "internet_mode": "off", "lan_prefixes": ["192.168.10.0/24"]}
    inv = {"internet": {}, "corporate": ["10.254.0.0/24", "192.168.10.0/24", "203.0.113.0/24"]}
    assert internet.block(inv, site, [], []) == {
        "mode": "off",
        "lan_prefixes": ["192.168.10.0/24"],
        "corporate_prefixes": inv["corporate"],
    }
    del inv["corporate"]
    assert internet.block(inv, site, [], [])["corporate_prefixes"] == []


class ForwardConnection:
    def __init__(self, vpn_ports=()):
        self.vpn_ports = vpn_ports
        self.row = None

    def execute(self, query, values):
        self.row = {"exists": 1} if "FROM paths" in query and values[0] in self.vpn_ports else None
        return self

    def fetchone(self):
        return self.row


@pytest.mark.parametrize("port", [500, 4500, 51820])
def test_udp_forward_cannot_steal_a_vpn_listener(monkeypatch, port):
    conn = ForwardConnection([51820])
    with pytest.raises(internet.InternetError, match="reserved.*VPN"):
        internet.validate_forward(conn, "org", {"protocol": "udp", "port": port})


@pytest.mark.parametrize("protocol,port", [("tcp", 443), ("tcp", 500), ("udp", 51821)])
def test_reserved_udp_port_protection_keeps_unrelated_forwards(monkeypatch, protocol, port):
    monkeypatch.setattr(
        internet,
        "_sites",
        lambda *_: {"site": {"id": "site", "kind": "site", "name": "Office", "lan": ["192.168.10.0/24"]}},
    )
    result = internet.validate_forward(
        ForwardConnection([51820]),
        "org",
        {"protocol": protocol, "port": port, "to_site_id": "site", "to_address": "192.168.10.10"},
    )
    assert result["port"] == port
