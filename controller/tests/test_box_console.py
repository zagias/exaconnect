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
    assert client.post("/api/v1/agent/telemetry", headers=h, json={"at": now.isoformat(), "box": box}).status_code == 204
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
    assert client.post("/api/v1/sites/nope/box/managed", json={"mode": "cloud"}, headers=admin_headers).status_code == 404
