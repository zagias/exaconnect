"""Internet breakout, NAT gateway and firewall (ADR 0010)."""

import datetime as dt

from .test_fabric import _sites
from .test_flow import _enrol, _seed


def _desired(client, h):
    return client.get("/api/v1/agent/desired-state", headers=h).json()


def test_sites_go_through_the_pop_by_default(client, admin_headers):
    seed = _seed()
    _, pop_h = _enrol(client, seed["tokens"], "pop-miami")
    _, a_h = _enrol(client, seed["tokens"], "site-a")
    a = _desired(client, a_h)["internet"]
    assert a == {"mode": "pop", "lan_prefixes": ["192.168.10.0/24"], "tunnels": ["wg-a", "wg-b", "wg-sat"]}
    pop = _desired(client, pop_h)["internet"]
    assert pop["mode"] == "gateway" and pop["public_address"] == "100.64.0.2"
    assert pop["uplinks"] == [{"interface": "eth5", "gateway": "100.64.0.1", "path": "", "tunnel": ""}]
    # Every pop-mode site's LAN, plus the PoP's own.
    assert pop["lan_prefixes"] == ["10.200.0.0/24", "192.168.10.0/24", "192.168.20.0/24"]
    assert pop["firewall"] == [] and pop["port_forwards"] == []
    # Steering is unchanged for a site going through the PoP.
    assert "corporate_prefixes" not in client.get("/api/v1/agent/steering", headers=a_h).json()


def test_local_breakout_uses_the_sites_links_and_its_rules(client, admin_headers):
    seed = _seed()
    cid, sites = seed["customer_id"], _sites(seed)
    _, pop_h = _enrol(client, seed["tokens"], "pop-miami")
    _, a_h = _enrol(client, seed["tokens"], "site-a")
    url = f"/api/v1/customers/{cid}"
    rule = {"action": "deny", "dst": ["198.51.100.0/24"], "protocol": "icmp", "description": "No pings out"}
    r = client.post(f"{url}/firewall/rules", json=rule, headers=admin_headers)
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    # Going through the PoP, the rule is applied there, for both sites' LANs.
    (fw,) = _desired(client, pop_h)["internet"]["firewall"]
    assert fw == {
        "id": rid,
        "action": "deny",
        "src": ["192.168.10.0/24", "192.168.20.0/24"],
        "dst": ["198.51.100.0/24"],
        "protocol": "icmp",
        "ports": "",
    }

    r = client.patch(f"{url}/internet/sites/{sites['site-a']}", json={"mode": "local"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    a = _desired(client, a_h)["internet"]
    assert a["mode"] == "local" and "tunnels" not in a
    assert a["uplinks"][0] == {"interface": "eth1", "gateway": "10.11.1.1", "path": "carrier-a", "tunnel": "wg-a"}
    assert [u["interface"] for u in a["uplinks"]] == ["eth1", "eth2", "eth3"]
    assert [f["src"] for f in a["firewall"]] == [["192.168.10.0/24"]]
    # The PoP now only carries site B.
    pop = _desired(client, pop_h)["internet"]
    assert "192.168.10.0/24" not in pop["lan_prefixes"] and pop["firewall"][0]["src"] == ["192.168.20.0/24"]
    # Steering at site A classifies only the customer's own destinations.
    corp = client.get("/api/v1/agent/steering", headers=a_h).json()["corporate_prefixes"]
    assert "192.168.20.0/24" in corp and "10.254.0.0/24" in corp and "198.51.100.0/24" not in corp
    assert any(c.startswith("100.64.") for c in corp)

    # A rule for site B only is not applied at site A.
    client.post(f"{url}/firewall/rules", json={"action": "deny", "site_id": sites["site-b"]}, headers=admin_headers)
    assert len(_desired(client, a_h)["internet"]["firewall"]) == 1

    client.patch(f"{url}/internet/sites/{sites['site-a']}", json={"mode": "off"}, headers=admin_headers)
    assert _desired(client, a_h)["internet"] == {"mode": "off", "lan_prefixes": ["192.168.10.0/24"]}
    audit = client.get("/api/v1/audit", headers=admin_headers).text
    assert "internet.mode" in audit and "firewall.create" in audit


def test_rule_validation_and_order(client, admin_headers):
    seed = _seed()
    cid, sites = seed["customer_id"], _sites(seed)
    url = f"/api/v1/customers/{cid}/firewall/rules"
    for body, words in [
        ({"action": "deny", "ports": "443"}, "TCP or UDP"),
        ({"action": "deny", "protocol": "tcp", "ports": "443-80"}, "1 to 65535"),
        ({"action": "deny", "protocol": "tcp", "ports": "web"}, "commas"),
        ({"action": "deny", "dst": ["example.com"]}, "not an address"),
        ({"action": "deny", "site_id": sites["pop-miami"]}, "this organisation's sites"),
    ]:
        r = client.post(url, json=body, headers=admin_headers)
        assert r.status_code == 400 and words in r.json()["detail"], (body, r.text)

    ids = [
        client.post(url, json={"action": "allow", "description": d}, headers=admin_headers).json()["id"]
        for d in ("one", "two", "three")
    ]
    # Inserted at a position; then reordered; then deleted, renumbering the rest.
    first = client.post(url, json={"action": "deny", "position": 1}, headers=admin_headers).json()["id"]
    view = client.get(f"/api/v1/customers/{cid}/internet", headers=admin_headers).json()
    assert [r["id"] for r in view["rules"]] == [first, *ids]
    order = [ids[2], ids[0], ids[1], first]
    r = client.post(f"/api/v1/customers/{cid}/firewall/order", json={"ids": order}, headers=admin_headers)
    assert [x["id"] for x in r.json()] == order
    assert (
        client.post(
            f"/api/v1/customers/{cid}/firewall/order", json={"ids": order[:2]}, headers=admin_headers
        ).status_code
        == 400
    )
    assert client.delete(f"{url}/{ids[0]}", headers=admin_headers).status_code == 204
    view = client.get(f"/api/v1/customers/{cid}/internet", headers=admin_headers).json()
    assert [(r["id"], r["position"]) for r in view["rules"]] == [(ids[2], 1), (ids[1], 2), (first, 3)]
    # Edit: switch protocol and ports together; clear the site.
    r = client.patch(f"{url}/{first}", json={"protocol": "tcp", "ports": "443, 8000-8100"}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["ports"] == "443,8000-8100"


def test_port_forwards_reach_sites_through_the_pop(client, admin_headers):
    seed = _seed()
    cid, sites = seed["customer_id"], _sites(seed)
    _, pop_h = _enrol(client, seed["tokens"], "pop-miami")
    url = f"/api/v1/customers/{cid}/port-forwards"
    good = {"protocol": "tcp", "port": 8080, "to_site_id": sites["site-a"], "to_address": "192.168.10.10"}
    for body, words in [
        ({**good, "to_address": "192.168.20.10"}, "not inside site-a"),
        ({**good, "to_address": "nope"}, "IPv4 address"),
        ({**good, "to_site_id": sites["pop-miami"]}, "Choose the site"),
    ]:
        r = client.post(url, json=body, headers=admin_headers)
        assert r.status_code == 400 and words in r.json()["detail"], (body, r.text)
    r = client.post(url, json={**good, "allow_from": ["203.0.113.0/24"]}, headers=admin_headers)
    assert r.status_code == 201, r.text
    fwd = r.json()
    assert fwd["to_port"] == 8080 and fwd["to_site"] == "site-a" and fwd["active"] is True
    # One public address for everyone: the port is taken.
    r = client.post(url, json=good, headers=admin_headers)
    assert r.status_code == 400 and "already forwarded" in r.json()["detail"]
    (pf,) = _desired(client, pop_h)["internet"]["port_forwards"]
    assert pf == {
        "id": fwd["id"],
        "protocol": "tcp",
        "port": 8080,
        "to_address": "192.168.10.10",
        "to_port": 8080,
        "allow_from": ["203.0.113.0/24"],
    }
    # A site going straight out can't be reached through the PoP: left out.
    client.patch(
        f"/api/v1/customers/{cid}/internet/sites/{sites['site-a']}", json={"mode": "local"}, headers=admin_headers
    )
    assert _desired(client, pop_h)["internet"]["port_forwards"] == []
    view = client.get(f"/api/v1/customers/{cid}/internet", headers=admin_headers).json()
    assert view["forwards"][0]["active"] is False
    # Clearing the inside port makes it the public port again.
    r = client.patch(f"{url}/{fwd['id']}", json={"port": 8443, "to_port": None}, headers=admin_headers)
    assert r.status_code == 200 and (r.json()["port"], r.json()["to_port"]) == (8443, 8443)
    assert client.delete(f"{url}/{fwd['id']}", headers=admin_headers).status_code == 204


def test_telemetry_shows_where_traffic_leaves_and_rule_hits(client, admin_headers):
    seed = _seed()
    cid, sites = seed["customer_id"], _sites(seed)
    _, pop_h = _enrol(client, seed["tokens"], "pop-miami")
    _, a_h = _enrol(client, seed["tokens"], "site-a")
    rid = client.post(f"/api/v1/customers/{cid}/firewall/rules", json={"action": "deny"}, headers=admin_headers).json()[
        "id"
    ]
    now = dt.datetime.now(dt.UTC).isoformat()
    tel = {"at": now, "internet": {"mode": "pop", "via": "wg-b"}}
    assert client.post("/api/v1/agent/telemetry", json=tel, headers=a_h).status_code == 204
    counters = [
        {"kind": "rule", "id": rid, "packets": 40, "bytes": 3360},
        {"kind": "inbound", "id": 0, "packets": 2, "bytes": 120},
    ]
    tel = {"at": now, "internet": {"mode": "gateway", "via": "eth5", "counters": counters}}
    assert client.post("/api/v1/agent/telemetry", json=tel, headers=pop_h).status_code == 204
    view = client.get(f"/api/v1/customers/{cid}/internet", headers=admin_headers).json()
    a = next(s for s in view["sites"] if s["id"] == sites["site-a"])
    assert (a["mode"], a["via"], a["via_label"]) == ("pop", "wg-b", "ExaCarib PoP over Carrier B")
    assert [s["name"] for s in view["sites"]] == ["site-a", "site-b"]
    assert (view["rules"][0]["packets"], view["rules"][0]["bytes"]) == (40, 3360)
    assert view["inbound_dropped"] == 2 and view["public_address"] == "100.64.0.2"


def test_needs_sign_in(client):
    seed = _seed()
    r = client.get(f"/api/v1/customers/{seed['customer_id']}/internet")
    assert r.status_code == 401
