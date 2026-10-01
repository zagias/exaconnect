"""Steering end to end against Postgres: maps for sites and the PoP, the
routing pass reacting to telemetry, decisions in the API, shadow mode."""

import datetime as dt
import random

from exaconnect_controller import db
from exaconnect_controller.routing import runner
from exaconnect_controller.security import hash_password

from .test_flow import _enrol, _seed


def _windows(now, tunnel, loss_fn, rtt, n=30, sent=200, rng=None):
    rng = rng or random.Random(1)
    out = []
    for i in range(n):
        end = now - dt.timedelta(seconds=10 * (n - 1 - i))
        loss = loss_fn(i)
        lost = sum(1 for _ in range(sent) if rng.random() < loss / 100)
        out.append(
            {
                "path": tunnel,
                "start": (end - dt.timedelta(seconds=10)).isoformat(),
                "end": end.isoformat(),
                "sent": sent,
                "received": sent - lost,
                "loss_pct": 100 * lost / sent,
                "rtt_avg_ms": rtt,
                "rtt_min_ms": rtt,
                "rtt_max_ms": rtt,
                "jitter_ms": 3.0,
            }
        )
    return out


def _site(client, admin_headers, name):
    return next(s for s in client.get("/api/v1/sites", headers=admin_headers).json() if s["name"] == name)


def test_steering_maps_and_brownout_decision(client, admin_headers):
    tokens = _seed()["tokens"]
    _, pop_h = _enrol(client, tokens, "pop-miami")
    _, a_h = _enrol(client, tokens, "site-a")
    _, b_h = _enrol(client, tokens, "site-b")

    # Site map: three classes, CS1-only bulk, no satellite outside Storm Mode.
    m = client.get("/api/v1/agent/steering", headers=a_h).json()
    assert [c["name"] for c in m["classes"]] == ["voice", "business", "bulk"]
    assert [c["mark"] for c in m["classes"]] == [0x101, 0x102, 0x103]
    assert m["classes"][0]["ports"][0] == {"proto": "udp", "from": 5060, "to": 5060}
    assert m["classes"][2]["dscp"] == [8]
    assert {p["name"]: p["table"] for p in m["paths"]} == {"carrier-a": 101, "carrier-b": 102, "sat": 103}
    rules = {r["class"]: r for r in m["rules"]}
    assert rules["voice"]["paths"] == ["carrier-a", "carrier-b"]
    assert rules["voice"]["pause_if_none"] is False
    assert rules["bulk"]["pause_if_none"] is True
    assert m["local_prefixes"] == ["192.168.10.0/24"] and m["storm"] is False
    assert client.get(f"/api/v1/agent/steering?have={m['version']}", headers=a_h).status_code == 204

    # The PoP has one rule per class and site LAN.
    pm = client.get("/api/v1/agent/steering", headers=pop_h).json()
    assert {(r["class"], r["dst"]) for r in pm["rules"]} == {
        (c, d) for c in ("voice", "business", "bulk") for d in ("192.168.10.0/24", "192.168.20.0/24")
    }

    # Probe telemetry: carrier A loss ramping, carrier B clean. Desired state asks for 20 probes/s.
    ds = client.get("/api/v1/agent/desired-state", headers=a_h).json()
    assert next(t for t in ds["tunnels"] if t["name"] == "wg-a")["probe"]["interval_ms"] == 50
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    ramp = lambda i: max(0.0, (i - 12) * 0.12)  # noqa: E731  0 -> ~2% over the last 3 minutes
    body = {
        "at": now.isoformat(),
        "probes": _windows(now, "wg-a", ramp, 25.0) + _windows(now, "wg-b", lambda i: 0.0, 35.0),
        "tunnels": [
            {"name": "wg-a", "path": "carrier-a", "handshake_age_s": 3, "bfd": "up"},
            {"name": "wg-b", "path": "carrier-b", "handshake_age_s": 3, "bfd": "up"},
        ],
        "steering": [{"class": "voice", "path": "carrier-a"}, {"class": "bulk", "path": "carrier-a"}],
        "steering_version": m["version"],
    }
    assert client.post("/api/v1/agent/telemetry", headers=a_h, json=body).status_code == 204
    client.post("/api/v1/agent/telemetry", headers=b_h, json={"at": now.isoformat()})

    assert runner.run_once(now) == []  # first pass: breach seen once, persistence needs two
    made = runner.run_once(now + dt.timedelta(seconds=1))
    assert [(d["site"], d["class"], d["kind"], d["to_path"]) for d in made] == [
        ("site-a", "voice", "move", "carrier-b")
    ]
    assert made[0]["reason"].startswith("Moved voice from Carrier A to Carrier B: loss on Carrier A")

    # The new map puts voice on B first; bulk (5% threshold) stays on A.
    m2 = client.get(f"/api/v1/agent/steering?have={m['version']}", headers=a_h).json()
    rules = {r["class"]: r for r in m2["rules"]}
    assert rules["voice"]["paths"] == ["carrier-b", "carrier-a"]
    assert rules["bulk"]["paths"][0] == "carrier-a"
    pm2 = client.get("/api/v1/agent/steering", headers=pop_h).json()
    pop_voice_a = next(r for r in pm2["rules"] if r["class"] == "voice" and r["dst"] == "192.168.10.0/24")
    assert pop_voice_a["paths"][0] == "carrier-b"

    # Decisions and the site's steering view.
    dec = client.get("/api/v1/decisions", headers=admin_headers).json()
    assert dec[0]["class_name"] == "voice" and dec[0]["to_label"] == "Carrier B"
    assert "carrier-a" in dec[0]["inputs"]["paths"]
    site_a = _site(client, admin_headers, "site-a")
    detail = client.get(f"/api/v1/sites/{site_a['id']}", headers=admin_headers).json()
    voice = next(s for s in detail["steering"] if s["class_name"] == "voice")
    assert voice["intended"] == "carrier-b" and voice["actual"] == "carrier-a"
    assert voice["last_reason"].startswith("Moved voice")


def test_shadow_mode_logs_but_does_not_steer(client, admin_headers):
    seed = _seed()
    tokens, cid = seed["tokens"], seed["customer_id"]
    _enrol(client, tokens, "pop-miami")
    _, a_h = _enrol(client, tokens, "site-a")
    r = client.patch(f"/api/v1/customers/{cid}/settings", headers=admin_headers, json={"shadow_mode": True})
    assert r.status_code == 200 and r.json()["shadow_mode"] is True

    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    body = {
        "at": now.isoformat(),
        "probes": _windows(now, "wg-a", lambda i: 3.0, 25.0) + _windows(now, "wg-b", lambda i: 0.0, 35.0),
    }
    assert client.post("/api/v1/agent/telemetry", headers=a_h, json=body).status_code == 204
    runner.run_once(now)
    made = runner.run_once(now + dt.timedelta(seconds=1))
    # 3% loss breaches voice (1%) and business (2%) but not bulk (5%).
    assert {(d["class"], d["to_path"]) for d in made} == {("voice", "carrier-b"), ("business", "carrier-b")}
    dec = client.get("/api/v1/decisions?include_holds=false", headers=admin_headers).json()
    assert all(d["shadow"] for d in dec)
    assert any(d["reason"].startswith("[Shadow] Moved voice from Carrier A to Carrier B") for d in dec)
    m = client.get("/api/v1/agent/steering", headers=a_h).json()
    assert next(r for r in m["rules"] if r["class"] == "voice")["paths"][0] == "carrier-a"

    # The switch is audited.
    actions = [a["action"] for a in client.get("/api/v1/audit", headers=admin_headers).json()]
    assert "settings.shadow_mode" in actions


def test_storm_mode(client, admin_headers):
    seed = _seed()
    tokens, cid = seed["tokens"], seed["customer_id"]
    _enrol(client, tokens, "pop-miami")
    _, a_h = _enrol(client, tokens, "site-a")
    m0 = client.get("/api/v1/agent/steering", headers=a_h).json()

    r = client.post(f"/api/v1/customers/{cid}/storm", headers=admin_headers, json={"on": True})
    assert r.status_code == 200 and r.json()["storm_mode"] is True
    assert r.json()["storm_by"] == "user:admin@example.org"

    # Satellite joins voice and business, last; bulk stays off it and pauses.
    m = client.get(f"/api/v1/agent/steering?have={m0['version']}", headers=a_h).json()
    rules = {r["class"]: r for r in m["rules"]}
    assert m["storm"] is True
    assert rules["voice"]["paths"] == ["carrier-a", "carrier-b", "sat"]
    assert rules["business"]["paths"] == ["carrier-a", "carrier-b", "sat"]
    assert rules["bulk"]["paths"] == ["carrier-a", "carrier-b"] and rules["bulk"]["pause_if_none"] is True
    ds = client.get("/api/v1/agent/desired-state", headers=a_h).json()
    assert next(t for t in ds["tunnels"] if t["name"] == "wg-sat")["probe"]["interval_ms"] == 200

    # Both terrestrial paths down: voice fails over to satellite with the Storm Mode hold time.
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    body = {
        "at": now.isoformat(),
        "probes": _windows(now, "wg-a", lambda i: 0.0, 25.0)
        + _windows(now, "wg-b", lambda i: 0.0, 35.0)
        + _windows(now, "wg-sat", lambda i: 0.5, 45.0),
        "tunnels": [
            {"name": "wg-a", "path": "carrier-a", "handshake_age_s": 3, "bfd": "down"},
            {"name": "wg-b", "path": "carrier-b", "handshake_age_s": 3, "bfd": "down"},
            {"name": "wg-sat", "path": "sat", "handshake_age_s": 3, "bfd": "up"},
        ],
    }
    assert client.post("/api/v1/agent/telemetry", headers=a_h, json=body).status_code == 204
    made = runner.run_once(now)
    assert {(d["class"], d["kind"], d["to_path"]) for d in made} == {
        ("voice", "failover", "sat"),
        ("business", "failover", "sat"),
        ("bulk", "hold", "carrier-a"),
    }
    dec = client.get("/api/v1/decisions", headers=admin_headers).json()
    assert next(d for d in dec if d["class_name"] == "voice")["inputs"]["policy"]["hold_s"] == 60

    # A customer user can switch it off; an admin-only option is refused.
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO users (email, password_hash, role, customer_id)"
            " VALUES ('ops@example.org', %s, 'customer', %s)",
            (hash_password("another long password"), cid),
        )
    tok = client.post("/api/v1/auth/login", json={"email": "ops@example.org", "password": "another long password"})
    c_h = {"Authorization": f"Bearer {tok.json()['token']}"}
    bad = client.post(f"/api/v1/customers/{cid}/storm", headers=c_h, json={"on": False, "allow_bulk_satellite": True})
    assert bad.status_code == 403
    r = client.post(f"/api/v1/customers/{cid}/storm", headers=c_h, json={"on": False})
    assert r.json()["storm_mode"] is False and r.json()["storm_by"] == "user:ops@example.org"

    # Off: satellite leaves the lists and the engine moves voice off it.
    m2 = client.get("/api/v1/agent/steering", headers=a_h).json()
    assert "sat" not in {p for r in m2["rules"] for p in r["paths"]}
    made = runner.run_once(now + dt.timedelta(seconds=10))
    voice = next(d for d in made if d["class"] == "voice")
    assert voice["from_path"] == "sat" and "no longer allowed" in voice["reason"]
    kinds = [e["kind"] for e in client.get("/api/v1/events", headers=admin_headers).json()]
    assert "storm_on" in kinds and "storm_off" in kinds


def test_storm_mode_is_per_site(client, admin_headers):
    seed = _seed()
    tokens, cid = seed["tokens"], seed["customer_id"]
    _, p_h = _enrol(client, tokens, "pop-miami")
    _, a_h = _enrol(client, tokens, "site-a")
    _, b_h = _enrol(client, tokens, "site-b")
    sites = {
        s["name"]: s for s in client.get(f"/api/v1/customers/{cid}/settings", headers=admin_headers).json()["sites"]
    }
    assert set(sites) == {"site-a", "site-b"} and not any(s["storm_mode"] for s in sites.values())

    # A storm heading for Kingston: only site-a switches over.
    r = client.post(f"/api/v1/sites/{sites['site-a']['id']}/storm", headers=admin_headers, json={"on": True})
    assert r.status_code == 200 and r.json()["storm_mode"] is True
    state = {s["name"]: s["storm_mode"] for s in r.json()["sites"]}
    assert state == {"site-a": True, "site-b": False}

    a = client.get("/api/v1/agent/steering", headers=a_h).json()
    b = client.get("/api/v1/agent/steering", headers=b_h).json()
    assert a["storm"] is True and "sat" in next(r for r in a["rules"] if r["class"] == "voice")["paths"]
    assert b["storm"] is False and "sat" not in {p for r in b["rules"] for p in r["paths"]}
    ds_b = client.get("/api/v1/agent/desired-state", headers=b_h).json()
    assert next(t for t in ds_b["tunnels"] if t["name"] == "wg-sat")["probe"]["interval_ms"] == 1000

    # The PoP sends return traffic to site-a over satellite too, but not to site-b.
    pop = client.get("/api/v1/agent/steering", headers=p_h).json()
    voice = {r["dst"]: r["paths"] for r in pop["rules"] if r["class"] == "voice"}
    assert "sat" in voice["192.168.10.0/24"] and "sat" not in voice["192.168.20.0/24"]

    r = client.post(f"/api/v1/sites/{sites['site-a']['id']}/storm", headers=admin_headers, json={"on": False})
    assert r.json()["storm_mode"] is False
