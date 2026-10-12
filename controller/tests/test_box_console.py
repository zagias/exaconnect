"""The box console's report (ADR 0046): the controller keeps what was set up on
the box itself, shows it in the portal, and follows its choice of internet ports."""

import datetime as dt

from exaconnect_controller import db

from .test_flow import _enrol, _seed


def test_box_report(client, admin_headers):
    tokens = _seed()["tokens"]
    _, h = _enrol(client, tokens, "site-a")
    now = dt.datetime.now(dt.UTC)
    box = {
        "console": True,
        "url": "https://192.168.10.1",
        "admin_set": True,
        "wan": [
            {"iface": "enp1s0", "path": "carrier-a", "mode": "dhcp"},
            {"iface": "eth2", "path": "carrier-b", "mode": "static"},
        ],
        "lan": {"iface": "enp3s0", "mode": "static", "address": "192.168.10.1/24"},
        "lan_dhcp": True,
        "last_change": {"at": now.isoformat(), "by": "admin", "what": "Set the ports"},
    }
    with db.tx() as conn:
        before = conn.execute("SELECT max(version) AS v FROM desired_states").fetchone()["v"]
    body = {
        "at": now.isoformat(),
        "box": box,
        "events": [
            {
                "at": now.isoformat(),
                "kind": "local_change",
                "detail": {"by": "box console (admin)", "what": "Set the ports"},
            }
        ],
    }
    r = client.post("/api/v1/agent/telemetry", headers=h, json=body)
    assert r.status_code == 204, r.text

    nodes = client.get("/api/v1/nodes", headers=admin_headers).json()
    a = next(n for n in nodes if n["name"] == "site-a")
    assert a["box"]["console"] is True and a["box"]["lan"]["iface"] == "enp3s0"
    assert a["box"]["last_change"]["what"] == "Set the ports"

    with db.tx() as conn:
        links = {
            r["path"]: r["underlay_interface"]
            for r in conn.execute(
                """SELECT l.path, l.underlay_interface FROM links l JOIN sites s ON s.id = l.site_id
                   WHERE s.name = 'site-a'"""
            ).fetchall()
        }
        after = conn.execute("SELECT max(version) AS v FROM desired_states").fetchone()["v"]
        audit = conn.execute("SELECT detail FROM audit_log WHERE action = 'box.ports'").fetchall()
    # The ports the box chose become the links' underlay interfaces, and agents get new state.
    assert links["carrier-a"] == "enp1s0" and links["carrier-b"] == "eth2" and links["sat"] == "eth3"
    assert after > before and len(audit) == 1

    # The same report again changes nothing more.
    r = client.post("/api/v1/agent/telemetry", headers=h, json={"at": now.isoformat(), "box": box})
    assert r.status_code == 204
    with db.tx() as conn:
        assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'box.ports'").fetchone()["n"] == 1

    with db.tx() as conn:
        site_id = conn.execute("SELECT id FROM sites WHERE name = 'site-a'").fetchone()["id"]
    detail = client.get(f"/api/v1/sites/{site_id}", headers=admin_headers).json()
    assert detail["box"]["url"] == "https://192.168.10.1"
    events = client.get(f"/api/v1/events?site_id={site_id}", headers=admin_headers).json()
    assert any(e["kind"] == "local_change" for e in events)

    # A bad report is refused whole.
    bad = dict(box, wan=[{"iface": "eth0; rm -rf /", "path": "carrier-a", "mode": "dhcp"}])
    r = client.post("/api/v1/agent/telemetry", headers=h, json={"at": now.isoformat(), "box": bad})
    assert r.status_code == 422


def _desired(client, h) -> dict:
    r = client.get("/api/v1/agent/desired-state", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_box_managed_from(client, admin_headers):
    """Where a box is managed from (ADR 0049): switched in the portal or on the
    box, carried both ways with its time, and the newest wins."""
    from .test_organisation_setup import _join, _new_org

    tokens = _seed()["tokens"]
    _, h = _enrol(client, tokens, "site-a")
    with db.tx() as conn:
        site_id = str(conn.execute("SELECT id FROM sites WHERE name = 'site-a'").fetchone()["id"])
    url = f"/api/v1/sites/{site_id}/box/managed"

    # Not a box console yet: nothing to switch, and no box block for the agent.
    assert client.post(url, json={"mode": "cloud"}, headers=admin_headers).status_code == 409
    assert "box" not in _desired(client, h)

    now = dt.datetime.now(dt.UTC)
    box = {"console": True, "admin_set": True, "wan": [{"iface": "eth0", "path": "carrier-a", "mode": "dhcp"}]}
    r = client.post("/api/v1/agent/telemetry", headers=h, json={"at": now.isoformat(), "box": box})
    assert r.status_code == 204
    got = client.get(url, headers=admin_headers).json()
    assert got == {"console": True, "managed": {"mode": "both", "at": None, "by": None}}

    # Switched in the portal: audited, an event, and down to the box in desired state.
    r = client.post(url, json={"mode": "cloud"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    portal_at = dt.datetime.fromisoformat(r.json()["managed"]["at"])
    mf = _desired(client, h)["box"]["managed_from"]
    assert mf["mode"] == "cloud" and mf["by"] == "Connect portal (admin@example.org)"
    detail = client.get(f"/api/v1/sites/{site_id}", headers=admin_headers).json()
    assert detail["box_managed"]["mode"] == "cloud"
    events = client.get(f"/api/v1/events?site_id={site_id}", headers=admin_headers).json()
    assert any(e["kind"] == "box_managed" for e in events)
    with db.tx() as conn:
        assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'box.managed'").fetchone()["n"] == 1
        version = conn.execute("SELECT max(version) AS v FROM desired_states").fetchone()["v"]

    # The box reporting the same setting back changes nothing.
    same = dict(box, managed={"mode": "cloud", "at": mf["at"], "by": mf["by"]})
    client.post("/api/v1/agent/telemetry", headers=h, json={"at": now.isoformat(), "box": same})
    # An older setting from the box loses.
    old = dict(box, managed={"mode": "site", "at": (portal_at - dt.timedelta(minutes=1)).isoformat(), "by": "x"})
    client.post("/api/v1/agent/telemetry", headers=h, json={"at": now.isoformat(), "box": old})
    with db.tx() as conn:
        assert conn.execute("SELECT max(version) AS v FROM desired_states").fetchone()["v"] == version
    assert client.get(url, headers=admin_headers).json()["managed"]["mode"] == "cloud"

    # Taken back on the box (an emergency): newer, so the portal follows.
    later = (portal_at + dt.timedelta(seconds=30)).isoformat()
    back = dict(box, managed={"mode": "site", "at": later, "by": "box console (admin)"})
    client.post("/api/v1/agent/telemetry", headers=h, json={"at": now.isoformat(), "box": back})
    got = client.get(url, headers=admin_headers).json()["managed"]
    assert got["mode"] == "site" and got["by"] == "box console (admin)"
    assert _desired(client, h)["box"]["managed_from"]["mode"] == "site"
    # Repeating it changes nothing more.
    client.post("/api/v1/agent/telemetry", headers=h, json={"at": now.isoformat(), "box": back})
    with db.tx() as conn:
        n = conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'box.managed'").fetchone()["n"]
        v2 = conn.execute("SELECT max(version) AS v FROM desired_states").fetchone()["v"]
    assert n == 2
    client.post("/api/v1/agent/telemetry", headers=h, json={"at": now.isoformat(), "box": back})
    with db.tx() as conn:
        assert conn.execute("SELECT max(version) AS v FROM desired_states").fetchone()["v"] == v2

    # Another organisation cannot see or switch this box; nonsense is refused.
    other = _new_org(client, admin_headers, "Reef Insurance")
    _, stranger = _join(client, other["invite"]["url"], "Stranger")
    assert client.post(url, json={"mode": "cloud"}, headers=stranger).status_code == 403
    assert client.get(url, headers=stranger).status_code == 403
    assert client.post(url, json={"mode": "elsewhere"}, headers=admin_headers).status_code == 422
    r = client.post("/api/v1/sites/nope/box/managed", json={"mode": "cloud"}, headers=admin_headers)
    assert r.status_code == 404


def test_box_ports_from_portal(client, admin_headers):
    """Ports set in the portal (ADR 0051): checked like the box checks them, sent
    down in desired state while the box is managed from the cloud or both, and a
    typed PPPoE password forgotten once the box has handled the set-up."""
    tokens = _seed()["tokens"]
    _, h = _enrol(client, tokens, "site-a")
    with db.tx() as conn:
        site_id = str(conn.execute("SELECT id FROM sites WHERE name = 'site-a'").fetchone()["id"])
    url = f"/api/v1/sites/{site_id}/box/network"
    good = {"wan": [{"iface": "eth0", "path": "carrier-a", "mode": "dhcp"}], "lan": None, "lan_dhcp": False}
    assert client.post(url, json=good, headers=admin_headers).status_code == 409  # no box console yet

    now = dt.datetime.now(dt.UTC)
    box = {
        "console": True,
        "admin_set": True,
        "wan": [{"iface": "eth0", "path": "carrier-a", "mode": "dhcp"}],
        "lan": {"iface": "eth1", "mode": "static", "address": "192.168.10.1/24"},
        "ports": [{"name": "eth0"}, {"name": "eth1"}, {"name": "eth2"}],
        "network_at": now.isoformat(),
    }
    r = client.post("/api/v1/agent/telemetry", headers=h, json={"at": now.isoformat(), "box": box})
    assert r.status_code == 204

    # Mistakes show at once, in the box's words.
    for bad, words in [
        ({"wan": [{"iface": "eth9", "mode": "dhcp"}]}, "no port called"),
        ({"wan": [{"iface": "eth0", "mode": "static", "address": "10.0.0.2/24"}]}, "needs a gateway"),
        ({"wan": [{"iface": "eth0", "mode": "dhcp"}, {"iface": "eth0", "mode": "dhcp"}]}, "chosen twice"),
        ({"wan": [{"iface": "eth0", "mode": "pppoe", "pppoe_user": "u", "pppoe_password": "set"}]}, "PPPoE needs"),
        ({"wan": [{"iface": "eth0", "mode": "dhcp"}], "lan_dhcp": True}, "office port"),
    ]:
        r = client.post(url, json=bad, headers=admin_headers)
        assert r.status_code == 400 and words in r.json()["detail"], (bad, r.text)

    # Managed on site: the portal does not set the ports.
    client.post(f"/api/v1/sites/{site_id}/box/managed", json={"mode": "site"}, headers=admin_headers)
    assert client.post(url, json=good, headers=admin_headers).status_code == 409
    client.post(f"/api/v1/sites/{site_id}/box/managed", json={"mode": "cloud"}, headers=admin_headers)

    ports = {
        "wan": [
            {"iface": "eth0", "path": "carrier-a", "mode": "pppoe", "pppoe_user": "branch@isp"}
            | {"pppoe_password": "s3cret-pw"},
            {"iface": "eth2", "path": "carrier-b", "mode": "dhcp", "dns": ["1.1.1.1"]},
        ],
        "lan": {"iface": "eth1", "mode": "static", "address": "192.168.20.1/24"},
        "lan_dhcp": True,
    }
    r = client.post(url, json=ports, headers=admin_headers)
    assert r.status_code == 200, r.text
    shown = r.json()["portal"]
    assert shown["network"]["wan"][0]["pppoe_password"] == "set"  # never shown back
    at = shown["at"]
    down = _desired(client, h)["box"]["network"]
    assert down["at"] == at and down["by"] == "Connect portal (admin@example.org)"
    assert down["network"]["wan"][0]["pppoe_password"] == "s3cret-pw"
    assert down["network"]["lan"]["address"] == "192.168.20.1/24" and down["network"]["lan_dhcp"] is True
    detail = client.get(f"/api/v1/sites/{site_id}", headers=admin_headers).json()
    assert detail["box_network"]["network"]["wan"][0]["pppoe_password"] == "set"
    with db.tx() as conn:
        a = conn.execute("SELECT detail FROM audit_log WHERE action = 'box.network'").fetchone()["detail"]
    assert "s3cret-pw" not in str(a)

    # The box is still trying it: the password stays. Once kept, it goes.
    trying = dict(box, portal={"at": at, "by": down["by"], "state": "trying"})
    client.post("/api/v1/agent/telemetry", headers=h, json={"at": now.isoformat(), "box": trying})
    assert _desired(client, h)["box"]["network"]["network"]["wan"][0]["pppoe_password"] == "s3cret-pw"
    kept = dict(box, network_at=at, portal={"at": at, "by": down["by"], "state": "kept"})
    client.post("/api/v1/agent/telemetry", headers=h, json={"at": now.isoformat(), "box": kept})
    after = _desired(client, h)["box"]["network"]
    assert after["at"] == at and after["network"]["wan"][0]["pppoe_password"] == "set"
    with db.tx() as conn:
        left = conn.execute("SELECT count(*) AS n FROM desired_states WHERE body::text LIKE '%s3cret-pw%'").fetchone()
    assert left["n"] == 0

    # A later portal change is newer than the box's ports, even from a box clock running fast.
    fast = dict(kept, network_at=(dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)).isoformat())
    client.post("/api/v1/agent/telemetry", headers=h, json={"at": now.isoformat(), "box": fast})
    r = client.post(url, json=good, headers=admin_headers)
    assert r.status_code == 200
    assert dt.datetime.fromisoformat(r.json()["portal"]["at"]) > dt.datetime.fromisoformat(fast["network_at"])


def test_box_templates(client, admin_headers):
    """Box templates (ADR 0053): one port set-up for many boxes; boxes that follow
    it get its ports now and on every change, each office port on its own
    location's network; setting a box's ports directly takes it off the template."""
    tokens = _seed()["tokens"]
    _, ha = _enrol(client, tokens, "site-a")
    _, hb = _enrol(client, tokens, "site-b")
    with db.tx() as conn:
        sites = {r["name"]: r for r in conn.execute("SELECT id, name, customer_id, lan_prefixes FROM sites").fetchall()}
    a, b = str(sites["site-a"]["id"]), str(sites["site-b"]["id"])
    cid = str(sites["site-a"]["customer_id"])
    now = dt.datetime.now(dt.UTC).isoformat()
    box = {
        "console": True,
        "admin_set": True,
        "wan": [{"iface": "eth0", "path": "carrier-a", "mode": "dhcp"}],
        "lan": {"iface": "eth1", "mode": "static", "address": "192.168.99.1/24"},
        "ports": [{"name": "eth0"}, {"name": "eth1"}, {"name": "eth2"}],
    }
    for h in (ha, hb):
        assert client.post("/api/v1/agent/telemetry", headers=h, json={"at": now, "box": box}).status_code == 204
    url = f"/api/v1/box-templates?customer_id={cid}"

    # PPPoE has no place in a template; a template from a box copies its ports.
    pppoe = {"wan": [{"iface": "eth0", "mode": "pppoe", "pppoe_user": "u", "pppoe_password": "p"}]}
    r = client.post(url, json={"name": "Branch", "network": pppoe}, headers=admin_headers)
    assert r.status_code == 400 and "PPPoE" in r.json()["detail"]
    r = client.post(url, json={"name": "Copy", "from_site_id": a}, headers=admin_headers)
    assert r.status_code == 201 and r.json()["network"]["lan"]["address"] == "192.168.99.1/24"
    net = {
        "wan": [
            {"iface": "eth0", "path": "carrier-a", "mode": "dhcp"},
            {"iface": "eth2", "path": "carrier-b", "mode": "dhcp"},
        ],
        "lan": {"iface": "eth1", "mode": "static", "address": "auto"},
        "lan_dhcp": True,
    }
    r = client.post(url, json={"name": "Branch", "network": net}, headers=admin_headers)
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    assert client.post(url, json={"name": "Branch", "network": net}, headers=admin_headers).status_code == 409

    # Following it: each box gets the template, with its own location's office address.
    r = client.post(f"/api/v1/box-templates/{tid}/sites", json={"site_ids": [a, b]}, headers=admin_headers)
    assert r.status_code == 200 and all(x["ok"] for x in r.json()["results"]), r.text
    for h, s in ((ha, "site-a"), (hb, "site-b")):
        down = _desired(client, h)["box"]["network"]
        prefix = sites[s]["lan_prefixes"][0]
        assert down["network"]["lan"]["address"] == f"{next(prefix.hosts())}/{prefix.prefixlen}"
        assert down["by"].startswith("Template Branch")
    listed = client.get(url, headers=admin_headers).json()
    assert {x["site"]: x["template_id"] for x in listed["boxes"]} == {"site-a": tid, "site-b": tid}

    # Changing the template changes every box that follows it.
    net2 = dict(net, lan_dhcp=False)
    r = client.put(f"/api/v1/box-templates/{tid}", json={"name": "Branch", "network": net2}, headers=admin_headers)
    assert r.status_code == 200 and len(r.json()["results"]) == 2
    assert _desired(client, hb)["box"]["network"]["network"]["lan_dhcp"] is False

    # A box with no such port, or managed on site, is reported, not changed.
    bad = dict(net2, wan=[{"iface": "eth7", "path": "carrier-a", "mode": "dhcp"}])
    client.put(f"/api/v1/box-templates/{tid}", json={"name": "Odd", "network": bad}, headers=admin_headers)
    client.post(f"/api/v1/sites/{b}/box/managed", json={"mode": "site"}, headers=admin_headers)
    r = client.put(f"/api/v1/box-templates/{tid}", json={"name": "Branch", "network": net}, headers=admin_headers)
    res = {x["site"]: x for x in r.json()["results"]}
    assert res["site-a"]["ok"] and not res["site-b"]["ok"] and "managed on site" in res["site-b"]["error"]
    r = client.post(f"/api/v1/box-templates/{tid}", json={}, headers=admin_headers)
    assert r.status_code == 405

    # Ports set directly on a box take it off the template; deleting the template keeps the ports.
    own = {"wan": [{"iface": "eth0", "path": "carrier-a", "mode": "dhcp"}]}
    assert client.post(f"/api/v1/sites/{a}/box/network", json=own, headers=admin_headers).status_code == 200
    listed = client.get(url, headers=admin_headers).json()
    assert {x["site"]: x["template_id"] for x in listed["boxes"]}["site-a"] is None
    assert client.delete(f"/api/v1/box-templates/{tid}", headers=admin_headers).status_code == 200
    assert "network" in _desired(client, ha)["box"]


def test_box_software(client, admin_headers, monkeypatch):
    """Fleet software updates (ADR 0054): the portal asks boxes for a version, it
    goes down in desired state, and each box's report of what became of it shows."""
    tokens = _seed()["tokens"]
    _, ha = _enrol(client, tokens, "site-a")
    _, hb = _enrol(client, tokens, "site-b")
    with db.tx() as conn:
        sites = {r["name"]: r for r in conn.execute("SELECT id, name, customer_id FROM sites").fetchall()}
    a, b = str(sites["site-a"]["id"]), str(sites["site-b"]["id"])
    cid = str(sites["site-a"]["customer_id"])
    now = dt.datetime.now(dt.UTC).isoformat()
    image = {"console": True, "software": {"ab": True, "slot": "A", "version": "1.0.20"}}
    apt = {"console": True}
    assert client.post("/api/v1/agent/telemetry", headers=ha, json={"at": now, "box": image}).status_code == 204
    assert client.post("/api/v1/agent/telemetry", headers=hb, json={"at": now, "box": apt}).status_code == 204

    from exaconnect_controller import box as boxes

    boxes._releases.update(at=0.0, versions=None)
    fake = [
        {"tag_name": "box-v1.0.21", "assets": [{"name": "exacarib-box-1.0.21-amd64.root.tar.zst"}]},
        {"tag_name": "box-v1.0.20", "assets": [{"name": "exacarib-box-1.0.20-amd64.iso"}]},
        {"tag_name": "v9", "assets": [{"name": "x-amd64.root.tar.zst"}]},
    ]
    monkeypatch.setattr(boxes, "releases", lambda fetch=None, _r=boxes.releases: _r(lambda u: fake))
    r = client.get("/api/v1/box-software/versions", headers=admin_headers)
    assert r.json() == {"versions": ["1.0.21"], "latest": "1.0.21"}

    url = f"/api/v1/box-software?customer_id={cid}"
    listed = {x["site"]: x for x in client.get(url, headers=admin_headers).json()["boxes"]}
    assert listed["site-a"]["ab"] and listed["site-a"]["version"] == "1.0.20" and not listed["site-b"]["ab"]

    assert client.post(url, json={"site_ids": [a], "version": "1.0;rm"}, headers=admin_headers).status_code == 400
    client.post(f"/api/v1/sites/{b}/box/managed", json={"mode": "site"}, headers=admin_headers)
    r = client.post(url, json={"site_ids": [a, b], "version": "1.0.21"}, headers=admin_headers)
    res = {x.get("site"): x for x in r.json()["results"]}
    assert res["site-a"]["ok"] and "managed on site" in res["site-b"]["error"]
    down = _desired(client, ha)["box"]["software"]
    assert down["version"] == "1.0.21" and down["by"].startswith("Connect portal")
    assert "software" not in (_desired(client, hb).get("box") or {})
    assert {x["site"]: x["state"] for x in client.get(url, headers=admin_headers).json()["boxes"]}["site-a"] == "sent"

    # The box reports it installed and came back on the new version.
    rep = {"version": "1.0.21", "at": down["at"], "state": "done"}
    image = {**image, "software": {**image["software"], "slot": "B", "version": "1.0.21"}, "software_portal": rep}
    assert client.post("/api/v1/agent/telemetry", headers=ha, json={"at": now, "box": image}).status_code == 204
    a_row = {x["site"]: x for x in client.get(url, headers=admin_headers).json()["boxes"]}["site-a"]
    assert a_row["state"] == "done" and a_row["version"] == "1.0.21" and a_row["asked"]["version"] == "1.0.21"
