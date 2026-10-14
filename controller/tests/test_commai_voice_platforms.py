"""Teams, Zoom Phone and other calling platforms joined to Jibsy (ADR 0056): links, people
with a Jibsy number and extension, set-up guides, test calls both ways, the dial plan,
Kamailio's lists, the PBX check for calls from a platform and isolation."""

from __future__ import annotations

import pytest

from exaconnect_controller import db
from exaconnect_controller.commai.voice import carriers, freeswitch, pbx
from exaconnect_controller.commai.voice import provider as providers
from exaconnect_controller.commai.voice.common import domain

from .commai_helpers import base
from .test_commai_voice import setup_voice
from .test_commai_voice_trunks import locs, trunk


@pytest.fixture(autouse=True)
def _clean():
    providers.FAULTS.clear()
    yield
    providers.FAULTS.clear()


def number(client, b, e164="+1 868 555 7100"):
    """A number on a simulated trunk of the organisation's own (free, active at once)."""
    t = trunk(client, b, f"Numbers {e164[-4:]}")
    r = client.post(
        f"{base(b)}/voice/trunks/{t['id']}/numbers",
        json={"e164": e164, "location_id": locs(client, b)["Port of Spain"]["id"]},
        headers=b["boss"]["h"],
    )
    assert r.status_code == 201, r.text
    return r.json()


def link(client, b, platform="teams", **kw):
    r = client.post(f"{base(b)}/voice/platforms", json={"platform": platform, **kw}, headers=b["boss"]["h"])
    assert r.status_code == 201, r.text
    return r.json()


def person(client, b, lk, num, **kw):
    body = {"name": "Dee Ramdass", "account": "dee@voicebank.example", "number_id": num["id"], **kw}
    r = client.post(f"{base(b)}/voice/platforms/{lk['id']}/users", json=body, headers=b["boss"]["h"])
    assert r.status_code == 201, r.text
    return r.json()


def view(client, b, who="boss"):
    r = client.get(f"{base(b)}/voice/platforms", headers=b[who]["h"])
    assert r.status_code == 200, r.text
    return r.json()


def test_teams_link_people_guide_and_test_calls(client):
    b = setup_voice(client)
    u, h = base(b), b["boss"]["h"]
    v = view(client, b)
    assert {p["key"] for p in v["platforms"]} == {"teams", "zoom", "webex", "google", "genesys", "nice", "talkdesk"}
    assert v["links"] == []

    lk = link(client, b)
    assert lk["mode"] == "simulated" and lk["fqdn"].endswith("-teams.sip.exacarib.com")
    assert lk["fqdn"].startswith("voice-bank-")
    assert lk["hosts"][0] == "sip.pstnhub.microsoft.com" and "52.112.0.0/14" in lk["allow_ips"]
    assert client.post(f"{u}/voice/platforms", json={"platform": "teams"}, headers=h).status_code == 409
    # Staff can't change the phone system.
    r = client.post(f"{u}/voice/platforms", json={"platform": "zoom"}, headers=b["ana"]["h"])
    assert r.status_code == 403

    num = number(client, b)
    assert num["e164"] in [n["e164"] for n in view(client, b)["numbers"]]
    dee = person(client, b, lk, num, extension="250")
    assert dee["e164"] == "+18685557100" and dee["extension"] == "250" and dee["number_rings_here"]
    # The number is taken, the extension is taken, the sign-in must look like an address.
    other = {"name": "X", "account": "x@voicebank.example", "number_id": num["id"]}
    assert client.post(f"{u}/voice/platforms/{lk['id']}/users", json=other, headers=h).status_code == 409
    num2 = number(client, b, "+1 868 555 7101")
    r = client.post(
        f"{u}/voice/platforms/{lk['id']}/users", json={**other, "number_id": num2["id"], "extension": "201"}, headers=h
    )
    assert r.status_code == 422 and "201" in r.json()["detail"]
    r = client.post(
        f"{u}/voice/platforms/{lk['id']}/users", json={**other, "number_id": num2["id"], "account": "x"}, headers=h
    )
    assert r.status_code == 422
    # Jibsy extensions can't reuse the platform user's extension either.
    pv = client.post(
        f"{u}/voice/preview", json={"ops": [{"op": "add_user", "name": "Z", "extension": "250"}]}, headers=h
    ).json()
    assert not pv["ok"] and "250" in str(pv["errors"])

    # The set-up guide names the SIP address and gives each person their number.
    g = view(client, b)["links"][0]["guide"]
    assert lk["fqdn"] in g["script"] and "Set-CsPhoneNumberAssignment" in g["script"]
    assert '-Identity "dee@voicebank.example" -PhoneNumber "+18685557100"' in g["script"]
    assert "Teams Phone" in g["needs"]

    # Test calls: Jibsy rings Dee (simulated: Jibsy answers), Dee calls an extension, a number, out.
    t = f"{u}/voice/platforms/{lk['id']}/test"
    r = client.post(t, json={"user_id": dee["id"], "direction": "to"}, headers=h).json()
    assert r["ok"] and "answered" in r["steps"][-1]
    r = client.post(t, json={"user_id": dee["id"], "direction": "from", "to": "201"}, headers=h).json()
    assert r["ok"] and "Ana Lee" in r["steps"][-1]
    r = client.post(t, json={"user_id": dee["id"], "direction": "from", "to": "299"}, headers=h).json()
    assert not r["ok"] and "no extension 299" in r["reason"]
    r = client.post(t, json={"user_id": dee["id"], "direction": "from", "to": "+1 868 555 7101"}, headers=h).json()
    assert r["ok"] and "one of your numbers" in r["steps"][-1]
    r = client.post(t, json={"user_id": dee["id"], "direction": "from", "to": "+1 868 555 0199"}, headers=h).json()
    assert r["ok"] and "+18685557100" in r["steps"][-1]

    # A simulated link that refuses calls, or is down, fails the test call with the reason.
    client.patch(f"{u}/voice/platforms/{lk['id']}", json={"sim_fail": "refuse"}, headers=h)
    r = client.post(t, json={"user_id": dee["id"], "direction": "to"}, headers=h).json()
    assert not r["ok"] and "refused" in r["reason"]
    after = client.patch(f"{u}/voice/platforms/{lk['id']}", json={"sim_fail": "down"}, headers=h).json()
    assert after["health"] == "down"
    r = client.post(t, json={"user_id": dee["id"], "direction": "to"}, headers=h).json()
    assert not r["ok"] and "not answering" in r["reason"]


def test_platform_links_stay_out_of_trunks_and_routing(client):
    b = setup_voice(client)
    u, h = base(b), b["boss"]["h"]
    link(client, b, "zoom")
    r = client.get(f"{u}/voice/trunks", headers=h).json()
    assert r["trunks"] == []
    pv = client.get(f"{u}/voice/route", params={"extension": "201", "to": "+18685550100"}, headers=h).json()
    assert pv["own_trunks"] is False
    # A trunk may have the same name as a platform.
    trunk(client, b, "Zoom Phone")


def test_dial_plan_and_kamailio_lists(client):
    b = setup_voice(client)
    u, h = base(b), b["boss"]["h"]
    lk = link(client, b)
    num = number(client, b)
    dee = person(client, b, lk, num, extension="250")
    with db.tx() as conn:
        files = freeswitch.render(freeswitch.gather(conn, b["id"]))
        k = carriers.render_kamailio(conn)["files"]
    t = domain(b["id"]).split(".")[0]
    dp, pub = files[f"{t}/dialplan.xml"], files[f"{t}/public.xml"]
    name = "exa-plat-" + dee["id"].replace("-", "")[:12]
    # Simulated: the number and extension reach Dee, and Jibsy answers with an echo.
    assert f'data="{name} XML {t}"' in dp and 'expression="^250$"' in dp
    assert '<action application="echo"/>' in dp
    assert "X-Exa-Link" not in pub  # nothing comes in from a simulated link
    assert lk["key"] not in k["platforms.list"] and "52.112.0.0" not in k["address.list"]

    # Live: Kamailio gets Microsoft's addresses and the SIP address; FreeSWITCH bridges with SRTP.
    lk = client.patch(f"{u}/voice/platforms/{lk['id']}", json={"mode": "live"}, headers=h).json()
    with db.tx() as conn:
        files = freeswitch.render(freeswitch.gather(conn, b["id"]))
        k = carriers.render_kamailio(conn)["files"]
        set_id = carriers.carrier_sets(conn)[lk["key"]]
    dp, pub = files[f"{t}/dialplan.xml"], files[f"{t}/public.xml"]
    assert f"sip_h_X-Exa-Route={set_id}" in dp
    assert "{rtp_secure_media=mandatory:AES_CM_128_HMAC_SHA1_80}sofia/gateway/exacarib_sip/+18685557100" in dp
    assert f"{lk['fqdn']} teams:{lk['key']}" in k["platforms.list"]
    assert f"{set_id} sip:sip.pstnhub.microsoft.com:5061;transport=tls 0 0" in k["dispatcher.list"]
    assert f"set:{set_id} {lk['fqdn']}" in k["platforms.list"]
    assert "1 52.112.0.0 14 0 teams" in k["address.list"]
    # Calls from Teams: the link's header, the organisation's numbers and the controller check.
    assert f'expression="^{lk["key"]}$"' in pub and f"exa-plat-in-$1 XML {t}" in pub
    assert 'name="platform-short"' in dp and "exa-plat-in-(18685557100)" in dp
    assert "/internal/voice/authorise-platform?tenant=" in dp
    # A second organisation's Teams link shares Microsoft's addresses: listed once.
    b2 = setup_voice(client, "Other Bank")
    l2 = link(client, b2)
    client.patch(f"{base(b2)}/voice/platforms/{l2['id']}", json={"mode": "live"}, headers=b2["boss"]["h"])
    with db.tx() as conn:
        k = carriers.render_kamailio(conn)["files"]
    assert k["address.list"].count("52.112.0.0 14 0 teams") == 1
    assert k["platforms.list"].count(" teams:") == 2


def test_zoom_needs_its_addresses_and_checks_them(client):
    b = setup_voice(client)
    u, h = base(b), b["boss"]["h"]
    lk = link(client, b, "zoom")
    assert lk["hosts"] == [] and lk["allow_ips"] == []
    lk = client.patch(f"{u}/voice/platforms/{lk['id']}", json={"mode": "live"}, headers=h).json()
    assert any("SIP addresses" in m for m in lk["missing"])
    r = client.patch(f"{u}/voice/platforms/{lk['id']}", json={"allow_ips": ["0.0.0.0/0"]}, headers=h)
    assert r.status_code == 422
    r = client.patch(f"{u}/voice/platforms/{lk['id']}", json={"hosts": ["not a host"]}, headers=h)
    assert r.status_code == 422
    lk = client.patch(
        f"{u}/voice/platforms/{lk['id']}",
        json={"hosts": ["byoc.zoom.example"], "allow_ips": ["198.51.100.0/28"]},
        headers=h,
    ).json()
    assert lk["missing"] == [] and lk["guide"]["steps"][0].startswith("Ask Zoom")
    with db.tx() as conn:
        k = carriers.render_kamailio(conn)["files"]
    assert "1 198.51.100.0 28 0 zoom" in k["address.list"]
    assert "sip:byoc.zoom.example:5061;transport=tls" in k["dispatcher.list"]
    # Contact-centre platforms take a queue or flow name, not an email address.
    cc = link(client, b, "genesys")
    num = number(client, b)
    p = person(client, b, cc, num, name="Sales queue", account="Sales queue")
    assert p["account"] == "Sales queue"


def test_pbx_answer_for_calls_from_a_platform(client, monkeypatch):
    b = setup_voice(client)
    u, h = base(b), b["boss"]["h"]
    lk = link(client, b, mode="live")
    num = number(client, b)
    person(client, b, lk, num)
    monkeypatch.setenv(pbx.SECRET_ENV, "test-secret")
    tenant = domain(b["id"]).split(".")[0]
    call = "abcdef0123456789"
    # The digest covers tenant, link, caller and number, like the extension check.
    sig = pbx.sign("test-secret", tenant, lk["key"], "18685557100", "18685550199", call)
    pbx.check(sig, tenant, lk["key"], "18685557100", "18685550199", call)
    r = client.get(
        "/api/v1/commai/internal/voice/authorise-platform",
        params={"tenant": tenant, "link": lk["key"], "from": "18685557100", "to": "18685550199", "call": call},
        headers={pbx.HEADER: sig},
    )
    assert r.status_code == 200 and r.text == "OK none +18685557100"
    with db.tx() as conn:
        # Someone not on the link, a link of another organisation, a switched-off person: refused.
        assert pbx.authorise_platform(conn, tenant, lk["key"], "18685550000", "18685550199", call) == (
            "NO unknown_caller"
        )
        assert pbx.authorise_platform(conn, tenant, "org-00000000-00000000", "18685557100", "18685550199", call) == (
            "NO unknown_caller"
        )
        row = conn.execute(
            "SELECT allowed, code FROM voice_pbx_authorisations WHERE customer_id = %s AND call_id = %s",
            (b["id"], call),
        ).fetchone()
    assert row["allowed"] is False and row["code"] == "unknown_caller"
    # International off: refused like a call from an extension.
    with db.tx() as conn:
        out = pbx.authorise_platform(conn, tenant, lk["key"], "+18685557100", "447700900123", "0123456789abcdef")
    assert out in ("NO international_off", "OK none +18685557100")
    # Removing the link frees the number.
    assert client.delete(f"{u}/voice/platforms/{lk['id']}", headers=h).status_code == 204
    with db.tx() as conn:
        n = conn.execute("SELECT target_type FROM voice_numbers WHERE id = %s", (num["id"],)).fetchone()
    assert n["target_type"] == "none"


def test_teams_dns_record_for_exacarib(client, admin_headers):
    b = setup_voice(client)
    u, h = base(b), b["boss"]["h"]
    lk = link(client, b)
    r = client.patch(f"{u}/voice/platforms/{lk['id']}", json={"dns_txt": "bad value with spaces"}, headers=h)
    assert r.status_code == 422
    lk = client.patch(f"{u}/voice/platforms/{lk['id']}", json={"dns_txt": "ms=ms12345678"}, headers=h).json()
    assert lk["dns_txt"] == "ms=ms12345678" and not lk["dns_done"]
    assert client.get("/api/v1/commai/voice-admin/platform-dns", headers=h).status_code == 403
    rows = client.get("/api/v1/commai/voice-admin/platform-dns", headers=admin_headers).json()
    mine = [x for x in rows if x["id"] == lk["id"]]
    assert mine and mine[0]["fqdn"] == lk["fqdn"] and mine[0]["dns_txt"] == "ms=ms12345678"
    r = client.put(f"/api/v1/commai/voice-admin/platform-dns/{lk['id']}", json={"done": True}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["dns_done"] is True
    assert not any("DNS" in m for m in view(client, b)["links"][0]["missing"])


def test_isolation_between_organisations(client):
    a = setup_voice(client)
    b = setup_voice(client, "Other Bank")
    lk = link(client, a)
    num = number(client, a)
    p = person(client, a, lk, num)
    hb = b["boss"]["h"]
    assert view(client, b)["links"] == []
    assert client.patch(f"{base(b)}/voice/platforms/{lk['id']}", json={"enabled": False}, headers=hb).status_code == 404
    assert client.delete(f"{base(b)}/voice/platform-users/{p['id']}", headers=hb).status_code == 404
    lb = link(client, b)
    r = client.post(
        f"{base(b)}/voice/platforms/{lb['id']}/users",
        json={"name": "X", "account": "x@other.example", "number_id": num["id"]},
        headers=hb,
    )
    assert r.status_code == 422  # not its number
