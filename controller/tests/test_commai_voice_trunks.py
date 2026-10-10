"""The organisation's own SIP trunks (ADR 0044): a primary and secondary per
location, the organisation default, ExaCarib's carriers last, failover on
refusal and on a trunk that is down, numbers on a trunk, billing, the PBX
answer and isolation between organisations."""

from __future__ import annotations

import pytest

from exaconnect_controller import db
from exaconnect_controller.commai.voice import pbx
from exaconnect_controller.commai.voice import provider as providers
from exaconnect_controller.commai.voice.common import domain

from .commai_helpers import base
from .test_commai_voice import setup_voice
from .test_commai_voice_global import sim_call, switch_on


@pytest.fixture(autouse=True)
def _clean():
    providers.FAULTS.clear()
    yield
    providers.FAULTS.clear()


def trunk(client, b, name, **kw):
    r = client.post(
        f"{base(b)}/voice/trunks", json={"name": name, "adapter": "simulated", **kw}, headers=b["boss"]["h"]
    )
    assert r.status_code == 201, r.text
    return r.json()


def locs(client, b):
    out = client.get(f"{base(b)}/voice/trunks", headers=b["boss"]["h"])
    assert out.status_code == 200, out.text
    return {x["name"]: x for x in out.json()["locations"]}


def tried(call):
    return [(a["carrier"], a["ok"]) for a in call["route"]]


def test_location_primary_secondary_default_then_exacarib(client):
    b = setup_voice(client)
    u = base(b)
    h = b["boss"]["h"]
    # No trunks: calls route exactly as before (the single provider).
    first = sim_call(client, b, "+1 868 555 0100")
    assert first["allowed"] and first["route"] == []
    preview = client.get(f"{u}/voice/route", params={"extension": "201", "to": "+18685550100"}, headers=h).json()
    assert preview["own_trunks"] is False

    p = trunk(client, b, "TSTT Port of Spain")
    s = trunk(client, b, "Digicel Business")
    d = trunk(client, b, "Flow Default")
    assert p["key"].startswith("org-") and p["health"] == "unknown"
    pos = locs(client, b)["Port of Spain"]
    assert pos["has_phone_site"] and pos["people"] == 2

    # The same trunk twice, or a secondary without a primary, is refused.
    r = client.put(
        f"{u}/voice/locations/{pos['id']}/trunks",
        json={"primary_trunk_id": p["id"], "secondary_trunk_id": p["id"]},
        headers=h,
    )
    assert r.status_code == 422
    r = client.put(f"{u}/voice/locations/{pos['id']}/trunks", json={"secondary_trunk_id": s["id"]}, headers=h)
    assert r.status_code == 422
    r = client.put(
        f"{u}/voice/locations/{pos['id']}/trunks",
        json={"primary_trunk_id": p["id"], "secondary_trunk_id": s["id"]},
        headers=h,
    )
    assert r.status_code == 200 and r.json()["secondary_trunk_id"] == s["id"]
    r = client.put(f"{u}/voice/trunk-settings", json={"default_trunk_id": d["id"]}, headers=h)
    assert r.json() == {"default_trunk_id": d["id"], "exacarib_fallback": True}

    pv = client.get(f"{u}/voice/route", params={"extension": "201", "to": "+18685550100"}, headers=h).json()
    assert pv["location"] == "Port of Spain"
    assert [(x["name"], x["role"]) for x in pv["steps"]] == [
        ("TSTT Port of Spain", "Primary for Port of Spain"),
        ("Digicel Business", "Secondary for Port of Spain"),
        ("Flow Default", "Organisation default"),
    ]
    assert pv["route"] == [p["key"], s["key"], d["key"]]

    # The primary takes the call; the organisation's carrier bills it, not ExaCarib by the minute.
    ok = sim_call(client, b, "+1 868 555 0100")
    assert ok["allowed"] and tried(ok) == [(p["key"], True)] and ok["carrier"] == p["key"]
    assert not [c for c in ok["charges"] if c["kind"] == "call"]

    # The primary refuses: the secondary takes it.
    client.patch(f"{u}/voice/trunks/{p['id']}", json={"sim_fail": "refuse"}, headers=h)
    call = sim_call(client, b, "+1 868 555 0101")
    assert call["allowed"] and tried(call) == [(p["key"], False), (s["key"], True)]
    # Both refuse: the organisation default.
    client.patch(f"{u}/voice/trunks/{s['id']}", json={"sim_fail": "refuse"}, headers=h)
    call = sim_call(client, b, "+1 868 555 0102")
    assert tried(call) == [(p["key"], False), (s["key"], False), (d["key"], True)]

    # A trunk that is down is skipped without trying it.
    client.patch(f"{u}/voice/trunks/{s['id']}", json={"sim_fail": "down"}, headers=h)
    pv = client.get(f"{u}/voice/route", params={"extension": "201", "to": "+18685550100"}, headers=h).json()
    sec = next(x for x in pv["steps"] if x["key"] == s["key"])
    assert not sec["usable"] and "down" in sec["reason"]
    call = sim_call(client, b, "+1 868 555 0103")
    assert tried(call) == [(p["key"], False), (d["key"], True)]

    # With the default off, a refusal on the only usable trunk fails the call (no ExaCarib carrier is on).
    client.patch(f"{u}/voice/trunks/{d['id']}", json={"enabled": False}, headers=h)
    call = sim_call(client, b, "+1 868 555 0104")
    assert not call["allowed"] and tried(call) == [(p["key"], False)]
    # None of its own trunks usable at all: ExaCarib's standard provider takes the call.
    client.patch(f"{u}/voice/trunks/{p['id']}", json={"sim_fail": "down"}, headers=h)
    call = sim_call(client, b, "+1 868 555 0107")
    assert call["allowed"] and call["route"] == []
    client.patch(f"{u}/voice/trunks/{p['id']}", json={"sim_fail": "refuse"}, headers=h)

    # ExaCarib's carriers come after the organisation's own.
    client.patch(f"{u}/voice/trunks/{d['id']}", json={"enabled": True}, headers=h)
    switch_on("carrier", "sim-carrier-1")
    pv = client.get(f"{u}/voice/route", params={"extension": "201", "to": "+18685550100"}, headers=h).json()
    assert pv["route"] == [p["key"], d["key"], "sim-carrier-1"]
    client.patch(f"{u}/voice/trunks/{d['id']}", json={"sim_fail": "refuse"}, headers=h)
    call = sim_call(client, b, "+1 868 555 0105")
    assert tried(call) == [(p["key"], False), (d["key"], False), ("sim-carrier-1", True)]
    assert [c for c in call["charges"] if c["kind"] == "call"]  # ExaCarib's carrier: charged as usual
    # ... unless the organisation switches that fallback off.
    client.put(f"{u}/voice/trunk-settings", json={"exacarib_fallback": False}, headers=h)
    call = sim_call(client, b, "+1 868 555 0106")
    assert not call["allowed"] and call["status"] == "failed"
    assert tried(call) == [(p["key"], False), (d["key"], False)]

    # San Fernando has no trunks of its own: the organisation default only.
    client.put(f"{u}/voice/trunk-settings", json={"exacarib_fallback": True}, headers=h)
    from .test_commai_voice import save

    save(client, b, [{"op": "add_user", "name": "Cara Jones", "extension": "203", "site": "San Fernando"}])
    pv = client.get(f"{u}/voice/route", params={"extension": "203", "to": "+18685550100"}, headers=h).json()
    assert pv["location"] == "San Fernando" and [x["role"] for x in pv["steps"]][0] == "Organisation default"


def test_pbx_answer_lists_trunk_sets_in_order(client, monkeypatch):
    b = setup_voice(client)
    u = base(b)
    h = b["boss"]["h"]
    p = trunk(client, b, "Primary SIP", adapter="sip", host="sip.primary.example")
    s = trunk(client, b, "Backup SIP", adapter="sip", host="203.0.113.10", allow_ips=["203.0.113.0/28"])
    pos = locs(client, b)["Port of Spain"]
    client.put(
        f"{u}/voice/locations/{pos['id']}/trunks",
        json={"primary_trunk_id": p["id"], "secondary_trunk_id": s["id"]},
        headers=h,
    )
    monkeypatch.setenv(pbx.SECRET_ENV, "test-secret")
    tenant = domain(b["id"]).split(".")[0]
    with db.tx() as conn:
        sets = pbx.carriers.carrier_sets(conn)
        answer = pbx.authorise(conn, tenant, "201", "", "18685550100", "abcdef0123456789")
        k = pbx.carriers.render_kamailio(conn)
    assert answer.startswith(f"OK {sets[p['key']]},{sets[s['key']]} ")
    # Set ids are kept for good: a new trunk, or one switched off, renumbers nothing.
    trunk(client, b, "Another")
    client.patch(f"{u}/voice/trunks/{p['id']}", json={"enabled": False}, headers=h)
    with db.tx() as conn:
        after = pbx.carriers.carrier_sets(conn)
        listed = pbx.carriers.render_kamailio(conn)["files"]["dispatcher.list"]
    assert p["key"] not in after and all(after[k] == n for k, n in sets.items() if k != p["key"])
    assert f"{sets[s['key']]} sip:203.0.113.10:5060" in listed
    assert "sip:sip.primary.example:5060" in k["files"]["dispatcher.list"]
    assert "203.0.113.0 28 0" in k["files"]["address.list"]
    r = client.post(f"{u}/voice/trunks", json={"name": "Bad", "adapter": "sip"}, headers=h)
    assert r.status_code == 422 and "host" in r.json()["detail"]
    r = client.post(
        f"{u}/voice/trunks",
        json={"name": "Wide", "adapter": "sip", "host": "a.example", "allow_ips": ["0.0.0.0/0"]},
        headers=h,
    )
    assert r.status_code == 422


def test_numbers_on_a_trunk_and_their_location(client):
    b = setup_voice(client)
    u = base(b)
    h = b["boss"]["h"]
    t = trunk(client, b, "Own carrier")
    ls = locs(client, b)
    r = client.post(
        f"{u}/voice/trunks/{t['id']}/numbers",
        json={
            "e164": "+1 868 555 7000",
            "location_id": ls["Port of Spain"]["id"],
            "target_type": "user",
            "target": "201",
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    n = r.json()
    assert n["source"] == "trunk" and n["status"] == "active" and n["location"] == "Port of Spain"
    assert n["trunk"] == "Own carrier" and n["target_type"] == "user"
    with db.tx() as conn:
        row = conn.execute("SELECT billing_from FROM voice_numbers WHERE id = %s", (n["id"],)).fetchone()
    assert row["billing_from"] is None  # not an ExaCarib number: no monthly number charge
    dup = client.post(
        f"{u}/voice/trunks/{t['id']}/numbers",
        json={"e164": "+18685557000", "location_id": ls["Port of Spain"]["id"]},
        headers=h,
    )
    assert dup.status_code == 409
    r = client.put(f"{u}/voice/numbers/{n['id']}/location", json={"location_id": ls["San Fernando"]["id"]}, headers=h)
    assert r.json()["location"] == "San Fernando"
    # A trunk with numbers on it isn't deleted by accident.
    assert client.delete(f"{u}/voice/trunks/{t['id']}", headers=h).status_code == 409


def test_trunks_belong_to_one_organisation_and_need_a_voice_admin(client, admin_headers):
    a = setup_voice(client, "Bank A")
    b = setup_voice(client, "Bank B")
    t = trunk(client, a, "A's trunk")
    # Staff can read but not change; another organisation can't see or use A's trunk.
    assert client.get(f"{base(a)}/voice/trunks", headers=a["ana"]["h"]).status_code == 200
    assert (
        client.post(
            f"{base(a)}/voice/trunks", json={"name": "X", "adapter": "simulated"}, headers=a["ana"]["h"]
        ).status_code
        == 403
    )
    assert client.get(f"{base(a)}/voice/trunks", headers=b["boss"]["h"]).status_code == 403
    assert client.get(f"{base(b)}/voice/trunks", headers=b["boss"]["h"]).json()["trunks"] == []
    r = client.put(f"{base(b)}/voice/trunk-settings", json={"default_trunk_id": t["id"]}, headers=b["boss"]["h"])
    assert r.status_code == 404
    pos_b = locs(client, b)["Port of Spain"]
    r = client.put(
        f"{base(b)}/voice/locations/{pos_b['id']}/trunks", json={"primary_trunk_id": t["id"]}, headers=b["boss"]["h"]
    )
    assert r.status_code == 404
    # B's calls never touch A's trunk; ExaCarib's carrier list leaves it out.
    assert sim_call(client, b, "+1 868 555 0100")["route"] == []
    cs = client.get("/api/v1/commai/voice-admin/carriers", headers=admin_headers).json()
    assert t["key"] not in {c["key"] for c in cs}
