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
