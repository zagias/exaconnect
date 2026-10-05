"""Traffic rules, priority queuing and application detection (ADR 0007)."""

import datetime as dt

import pytest

from exaconnect_controller import db, traffic
from exaconnect_controller.ai import apps, detect
from exaconnect_controller.security import hash_password

from .test_flow import _enrol, _seed


def _rule(**kw):
    base = {
        "name": "r",
        "class_name": "voice",
        "site_ids": [],
        "apps": [],
        "ports": "",
        "dst_subnets": [],
        "src_subnets": [],
        "vlans": [],
        "domains": [],
        "dscp": [],
        "enabled": True,
    }
    return {**base, **kw}


def test_rule_compiles_apps_and_own_fields():
    r = traffic.validate(
        _rule(apps=["zoom"], vlans=[20], src_subnets=["10.0.0.5/24"], domains=["https://Meet.Example.org/x"]),
        {"voice"},
    )
    assert r["src_subnets"] == ["10.0.0.0/24"] and r["domains"] == ["meet.example.org"]
    ms = traffic.rule_matches(r)
    # One match for Zoom's signature, one for the rule's own website; both keep the VLAN and source.
    assert ms[0] == {
        "class": "voice",
        "vlans": [20],
        "src": ["10.0.0.0/24"],
        "ports": [{"proto": "udp", "from": 8801, "to": 8810}, {"proto": "tcp", "from": 8801, "to": 8802}],
    }
    assert ms[1] == {"class": "voice", "vlans": [20], "src": ["10.0.0.0/24"], "domains": ["meet.example.org"]}


def test_rule_validation():
    with pytest.raises(traffic.RuleError, match="no class"):
        traffic.validate(_rule(class_name="nope", ports="udp:1"), {"voice"})
    with pytest.raises(traffic.RuleError, match="Say which traffic"):
        traffic.validate(_rule(), {"voice"})
    with pytest.raises(traffic.RuleError, match="VLAN"):
        traffic.validate(_rule(vlans=[4095]), {"voice"})
    with pytest.raises(traffic.RuleError, match="website"):
        traffic.validate(_rule(domains=["not a domain"]), {"voice"})
    with pytest.raises(traffic.RuleError, match="Unknown application"):
        traffic.validate(_rule(apps=["myspace"]), {"voice"})
    with pytest.raises(traffic.RuleError, match="Ports"):
        traffic.validate(_rule(ports="5060"), {"voice"})
    with pytest.raises(traffic.RuleError, match="IPv4"):
        traffic.validate(_rule(dst_subnets=["2001:db8::/32"]), {"voice"})
    # A rule can be about where traffic comes from alone: a VLAN.
    assert traffic.validate(_rule(vlans=[30]), {"voice"})["vlans"] == [30]


def test_limits_and_fit():
    many = [{"class": "voice", "domains": [f"d{i}.example.org"]} for i in range(150)]
    with pytest.raises(traffic.RuleError, match="websites"):
        traffic.check_limits(many)
    assert len(traffic.fit(many)) == traffic.MAX_DOMAINS


def test_qos_from_priorities_and_speeds():
    q = traffic.qos(
        [{"name": "voice", "priority": "realtime"}, {"name": "x", "priority": "normal"}],
        [{"tunnel": "wg-a", "shape_mbps": 200}, {"tunnel": "wg-b", "shape_mbps": None}],
    )
    assert q == {"classes": [{"name": "voice", "dscp": 46}], "paths": [{"tunnel": "wg-a", "shape_kbit": 190000}]}


def test_catalogue_recognises():
    assert apps.recognise("udp", 3479, "52.113.1.1").id == "teams"
    assert apps.recognise("udp", 8805, "1.2.3.4").id == "zoom"
    assert apps.recognise("tcp", 443, "1.2.3.4") is None


def _customer_user(cid):
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO users (email, password_hash, role, customer_id) VALUES ('it@example.org', %s, 'customer', %s)",
            (hash_password("a long customer password"), cid),
        )


def _login(client, email, password):
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_customer_rules_reach_the_maps(client, admin_headers):
    seed = _seed()
    tokens, cid = seed["tokens"], seed["customer_id"]
    _, p_h = _enrol(client, tokens, "pop-miami")
    _, a_h = _enrol(client, tokens, "site-a")
    _, b_h = _enrol(client, tokens, "site-b")
    _customer_user(cid)
    c_h = _login(client, "it@example.org", "a long customer password")
    sites = {s["name"]: s["id"] for s in client.get("/api/v1/sites", headers=admin_headers).json()}

    # Seeded priorities and link speeds give every map its queues.
    m = client.get("/api/v1/agent/steering", headers=a_h).json()
    assert m["qos"]["classes"] == [
        {"name": "voice", "dscp": 46},
        {"name": "business", "dscp": 26},
        {"name": "bulk", "dscp": 8},
    ]
    assert {p["tunnel"]: p["shape_kbit"] for p in m["qos"]["paths"]} == {
        "wg-a": 190000,
        "wg-b": 95000,
        "wg-sat": 47500,
    }
    assert "matches" not in m

    # A customer creates a class of their own that prefers Carrier B.
    r = client.put(
        f"/api/v1/customers/{cid}/classes/erp",
        headers=c_h,
        json={"priority": "interactive", "preferred_path": "carrier-b", "sla": {"max_latency_ms": 200}},
    )
    assert r.status_code == 200, r.text
    m = client.get("/api/v1/agent/steering", headers=a_h).json()
    assert next(x for x in m["rules"] if x["class"] == "erp")["paths"] == ["carrier-b", "carrier-a"]
    assert {"name": "erp", "dscp": 26} in m["qos"]["classes"]
    bad = client.put(f"/api/v1/customers/{cid}/classes/erp", headers=c_h, json={"preferred_path": "nowhere", "sla": {}})
    assert bad.status_code == 400

    # ...and a rule for it: the ERP subnet on VLAN 20, at site A only.
    r = client.post(
        f"/api/v1/customers/{cid}/rules",
        headers=c_h,
        json={
            "name": "ERP",
            "class_name": "erp",
            "site_ids": [sites["site-a"]],
            "dst_subnets": ["10.50.0.0/16"],
            "ports": "tcp:443",
            "vlans": [20],
        },
    )
    assert r.status_code == 201, r.text
    rule = r.json()
    a = client.get("/api/v1/agent/steering", headers=a_h).json()
    assert a["matches"] == [
        {
            "class": "erp",
            "vlans": [20],
            "dst": ["10.50.0.0/16"],
            "ports": [{"proto": "tcp", "from": 443, "to": 443}],
        }
    ]
    assert "matches" not in client.get("/api/v1/agent/steering", headers=b_h).json()
    assert client.get("/api/v1/agent/steering", headers=p_h).json()["matches"] == a["matches"]

    # Rules are validated, scoped to the customer, and protect their class.
    assert (
        client.post(f"/api/v1/customers/{cid}/rules", headers=c_h, json={"name": "x", "class_name": "erp"}).status_code
        == 400
    )
    assert client.delete(f"/api/v1/customers/{cid}/classes/erp", headers=c_h).status_code == 400
    assert client.delete(f"/api/v1/customers/{cid}/classes/voice", headers=c_h).status_code == 400
    other = client.post("/api/v1/customers", headers=admin_headers, json={"name": "Other Org"}).json()
    assert client.get(f"/api/v1/customers/{other['id']}/rules", headers=c_h).status_code == 403

    # Disable, then delete: the match goes away.
    upd = client.put(
        f"/api/v1/customers/{cid}/rules/{rule['id']}",
        headers=c_h,
        json={**{k: rule[k] for k in ("name", "class_name", "ports", "dst_subnets", "vlans")}, "enabled": False},
    )
    assert upd.status_code == 200, upd.text
    assert "matches" not in client.get("/api/v1/agent/steering", headers=a_h).json()
    assert client.delete(f"/api/v1/customers/{cid}/rules/{rule['id']}", headers=c_h).status_code == 204
    assert client.delete(f"/api/v1/customers/{cid}/classes/erp", headers=c_h).status_code == 204
    actions = [a["action"] for a in client.get("/api/v1/audit", headers=admin_headers).json()]
    assert {"rule.create", "rule.update", "rule.delete", "class.upsert", "class.delete"} <= set(actions)


def _flows(now, minutes, **f):
    return [{**f, "at": (now - dt.timedelta(minutes=m)).isoformat()} for m in range(minutes)]


def test_detection_suggests_and_applies(client, admin_headers):
    seed = _seed()
    tokens, cid = seed["tokens"], seed["customer_id"]
    _, p_h = _enrol(client, tokens, "pop-miami")
    _, a_h = _enrol(client, tokens, "site-a")
    _, b_h = _enrol(client, tokens, "site-b")
    now = dt.datetime.now(dt.UTC)
    zoom = dict(proto="udp", dst="192.168.20.10", dport=8801, flows=1, bytes_out=300_000, bytes_in=300_000)
    zoom.update(pkts_out=3000, pkts_in=3000)
    zoom["class"] = ""
    # An unknown app on UDP 7777 that behaves like a call, and a big one-way backup on TCP 9000.
    call = {**zoom, "dport": 7777}
    backup = dict(proto="tcp", dst="192.168.20.10", dport=9000, flows=1, bytes_out=40_000_000, bytes_in=200_000)
    backup.update(pkts_out=27_000, pkts_in=4_000, **{"class": ""})
    # Ephemeral-port traffic says nothing and is ignored.
    noise = {**call, "dport": 50000}
    body = {"at": now.isoformat(), "flows": _flows(now, 5, **zoom) + _flows(now, 5, **call) + _flows(now, 5, **backup)}
    body["flows"] += _flows(now, 5, **noise)
    assert client.post("/api/v1/agent/telemetry", headers=a_h, json=body).status_code == 204

    r = client.post(f"/api/v1/customers/{cid}/applications/detect", headers=admin_headers)
    assert r.json() == {"detections": 3}
    dets = {d["key"]: d for d in client.get(f"/api/v1/customers/{cid}/applications", headers=admin_headers).json()}
    assert set(dets) == {"app:zoom", "udp:7777", "tcp:9000"}
    z = dets["app:zoom"]
    assert z["suggested_class"] == "voice" and z["confidence"] == 0.9 and z["site"] == "site-a"
    assert "Zoom meetings seen at site-a" in z["reason"] and "currently unclassified" in z["reason"]
    assert dets["udp:7777"]["suggested_class"] == "voice" and dets["udp:7777"]["confidence"] == 0.6
    assert dets["tcp:9000"]["suggested_class"] == "bulk"

    # Apply Zoom: a rule for site A naming the app; site A's map carries Zoom's ports.
    r = client.post(f"/api/v1/applications/{z['id']}/apply", headers=admin_headers)
    assert r.status_code == 200, r.text
    rules = client.get(f"/api/v1/customers/{cid}/rules", headers=admin_headers).json()
    assert rules[0]["apps"] == ["zoom"] and rules[0]["source"] == "detected"
    m = client.get("/api/v1/agent/steering", headers=a_h).json()
    assert m["matches"][0]["ports"][0] == {"proto": "udp", "from": 8801, "to": 8810}
    assert "matches" not in client.get("/api/v1/agent/steering", headers=b_h).json()
    # Dismiss the backup; both leave the open list.
    assert (
        client.post(f"/api/v1/applications/{dets['tcp:9000']['id']}/dismiss", headers=admin_headers).status_code == 204
    )
    open_keys = {d["key"] for d in client.get(f"/api/v1/customers/{cid}/applications", headers=admin_headers).json()}
    assert open_keys == {"udp:7777"}

    # Once flows arrive marked voice, Zoom is not suggested again.
    later = now + dt.timedelta(minutes=6)
    marked = {**zoom, "class": "voice"}
    client.post(
        "/api/v1/agent/telemetry", headers=a_h, json={"at": later.isoformat(), "flows": _flows(later, 30, **marked)}
    )
    with db.tx() as conn:
        changed = detect.detect(conn, {"id": cid, "auto_prioritise": False}, later)
    assert "app:zoom" not in {d["key"] for d in changed}

    # Deleting the rule puts the detection back as a suggestion.
    client.delete(f"/api/v1/customers/{cid}/rules/{rules[0]['id']}", headers=admin_headers)
    open_keys = {d["key"] for d in client.get(f"/api/v1/customers/{cid}/applications", headers=admin_headers).json()}
    assert "app:zoom" in open_keys


def test_auto_prioritise_applies_known_apps_only(client, admin_headers):
    seed = _seed()
    tokens, cid = seed["tokens"], seed["customer_id"]
    _, a_h = _enrol(client, tokens, "site-a")
    r = client.patch(f"/api/v1/customers/{cid}/settings", headers=admin_headers, json={"auto_prioritise": True})
    assert r.json()["auto_prioritise"] is True
    now = dt.datetime.now(dt.UTC)
    teams = dict(proto="udp", dst="52.113.1.1", dport=3479, flows=2, bytes_out=200_000, bytes_in=200_000)
    teams.update(pkts_out=2000, pkts_in=2000, **{"class": ""})
    call = {**teams, "dst": "8.8.8.8", "dport": 7777}
    flows = _flows(now, 3, **teams) + _flows(now, 3, **call)
    client.post("/api/v1/agent/telemetry", headers=a_h, json={"at": now.isoformat(), "flows": flows})
    with db.tx() as conn:
        assert detect.run_once(conn, now) == 2
    dets = {
        d["key"]: d
        for d in client.get(f"/api/v1/customers/{cid}/applications?include_closed=true", headers=admin_headers).json()
    }
    assert dets["app:teams"]["status"] == "applied"
    assert dets["udp:7777"]["status"] == "suggested"
    rules = client.get(f"/api/v1/customers/{cid}/rules", headers=admin_headers).json()
    assert [r["created_by"] for r in rules] == ["system:auto-prioritise"]


def test_a_rule_that_covers_a_detection_closes_it(client, admin_headers):
    seed = _seed()
    tokens, cid = seed["tokens"], seed["customer_id"]
    _, a_h = _enrol(client, tokens, "site-a")
    now = dt.datetime.now(dt.UTC)
    zoom = dict(proto="udp", dst="192.168.20.10", dport=8801, flows=1, bytes_out=300_000, bytes_in=300_000)
    zoom.update(pkts_out=3000, pkts_in=3000, **{"class": ""})
    call = {**zoom, "dport": 7777}
    body = {"at": now.isoformat(), "flows": _flows(now, 5, **zoom) + _flows(now, 5, **call)}
    assert client.post("/api/v1/agent/telemetry", headers=a_h, json=body).status_code == 204
    client.post(f"/api/v1/customers/{cid}/applications/detect", headers=admin_headers)

    def open_keys():
        return {d["key"] for d in client.get(f"/api/v1/customers/{cid}/applications", headers=admin_headers).json()}

    assert open_keys() == {"app:zoom", "udp:7777"}

    # The customer's own rule for all sites names Zoom among other apps: the Zoom suggestion closes at once,
    # with no new traffic needed, and points at that rule.
    rule = client.post(
        f"/api/v1/customers/{cid}/rules",
        headers=admin_headers,
        json={"name": "all voice", "class_name": "voice", "apps": ["teams", "zoom"]},
    ).json()
    assert open_keys() == {"udp:7777"}
    closed = client.get(f"/api/v1/customers/{cid}/applications?include_closed=true", headers=admin_headers).json()
    z = next(d for d in closed if d["key"] == "app:zoom")
    assert z["status"] == "applied" and z["rule_id"] == rule["id"]

    # A plain port rule covers the unrecognised call; one narrowed to other addresses does not.
    narrowed = {"name": "narrow", "class_name": "voice", "ports": "udp:7777", "dst_subnets": ["10.9.9.0/24"]}
    client.post(f"/api/v1/customers/{cid}/rules", headers=admin_headers, json=narrowed)
    assert open_keys() == {"udp:7777"}
    client.post(f"/api/v1/customers/{cid}/rules", headers=admin_headers, json={**narrowed, "dst_subnets": []})
    assert open_keys() == set()

    # A later detection pass keeps them closed, and disabling the rule reopens Zoom.
    with db.tx() as conn:
        detect.detect(conn, {"id": cid, "auto_prioritise": False}, now)
    assert open_keys() == set()
    upd = client.put(
        f"/api/v1/customers/{cid}/rules/{rule['id']}",
        headers=admin_headers,
        json={"name": "all voice", "class_name": "voice", "apps": ["teams", "zoom"], "enabled": False},
    )
    assert upd.status_code == 200, upd.text
    assert open_keys() == {"app:zoom"}
