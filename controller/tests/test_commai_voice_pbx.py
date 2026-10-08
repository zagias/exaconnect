"""The PBX's check before each outside call (ADR 0033, voice/pbx.py): the
internal endpoint FreeSWITCH calls with mod_curl, its keyed digest, its plain
text answers, tenant isolation, and the rendered dial plan that uses it."""

from __future__ import annotations

import re
import uuid

import pytest

from exaconnect_controller import db
from exaconnect_controller.commai.voice import carriers, freeswitch, pbx
from exaconnect_controller.commai.voice.common import domain

from .commai_helpers import base, run_jobs
from .test_commai_voice import save, setup_voice
from .test_commai_voice_global import switch_on

URL = "/api/v1/commai/internal/voice/authorise"


@pytest.fixture
def secret(monkeypatch):
    value = "pbx-" + uuid.uuid4().hex  # made at run time, never a real key
    monkeypatch.setenv(pbx.SECRET_ENV, value)
    return value


def tenant(b) -> str:
    return domain(b["id"]).split(".")[0]


def ask(client, secret, b, to, ext="201", fwd="", call=None, sig=None, tenant_=None):
    t = tenant_ or tenant(b)
    call = call or str(uuid.uuid4())
    q = {"tenant": t, "ext": ext, "fwd": fwd, "to": to, "call": call}
    headers = {} if sig is False else {pbx.HEADER: sig or pbx.sign(secret, t, ext, fwd, to, call)}
    return client.get(URL, params=q, headers=headers)


def answer(client, secret, b, to, **kw) -> str:
    r = ask(client, secret, b, to, **kw)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/plain")
    return r.text


def test_only_the_pbx_may_ask(client, monkeypatch, secret):
    b = setup_voice(client, "PBX Bank")
    assert ask(client, secret, b, "18685550100", sig=False).status_code == 401  # no digest
    assert ask(client, secret, b, "18685550100", sig="0" * 32).status_code == 401  # wrong digest
    other = pbx.sign("not-" + secret, tenant(b), "201", "", "18685550100", "c" * 36)
    assert ask(client, secret, b, "18685550100", call="c" * 36, sig=other).status_code == 401  # wrong secret
    # A digest is for one call: another destination with the same digest is refused.
    good = pbx.sign(secret, tenant(b), "201", "", "18685550100", "c" * 36)
    assert ask(client, secret, b, "18685550199", call="c" * 36, sig=good).status_code == 401
    assert ask(client, secret, b, "18685550100", call="c" * 36, sig=good).status_code == 200
    # No secret configured: always refused, whatever is sent.
    monkeypatch.delenv(pbx.SECRET_ENV)
    assert ask(client, secret, b, "18685550100", sig=good, call="c" * 36).status_code == 503
    # People's sign-ins don't open it either.
    assert client.get(URL, params={"tenant": tenant(b)}, headers=b["boss"]["h"]).status_code == 503


def test_ok_gives_kamailio_sets_in_plan_order_and_the_caller_id(client, admin_headers, secret):
    b = setup_voice(client, "PBX Route Bank")
    # With no carrier on, the single provider takes the call: no sets.
    assert answer(client, secret, b, "18765550100") == "OK none none"
    save(client, b, [{"op": "add_number", "target_type": "user", "target": "201"}])
    run_jobs()
    with db.tx() as conn:
        n = conn.execute("SELECT e164 FROM voice_numbers WHERE customer_id = %s", (b["id"],)).fetchone()
    assert answer(client, secret, b, "18765550100") == f"OK none {n['e164']}"

    switch_on("carrier", "sim-carrier-1")
    switch_on("carrier", "sim-carrier-2")
    with db.tx() as conn:
        sets = carriers.carrier_sets(conn)
        route = carriers.plan(conn, b["id"], "18765550100")["route"]
    assert route == ["sim-carrier-2", "sim-carrier-1"]  # least cost for Jamaica
    call = str(uuid.uuid4())
    text = answer(client, secret, b, "18765550100", call=call)
    assert text == f"OK {sets['sim-carrier-2']},{sets['sim-carrier-1']} {n['e164']}"
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM voice_pbx_authorisations WHERE call_id = %s", (call,)).fetchone()
    assert row["allowed"] and row["route"] == route and row["customer_id"] == uuid.UUID(b["id"])
    # The other order for Trinidad.
    assert answer(client, secret, b, "+18685550100").startswith(f"OK {sets['sim-carrier-1']},{sets['sim-carrier-2']} ")


def test_refusals_say_no_with_a_code_and_are_kept_as_blocked_calls(client, secret):
    b = setup_voice(client, "PBX Fraud Bank")
    u = base(b)
    call = str(uuid.uuid4())
    assert answer(client, secret, b, "19005550100", call=call) == "NO blocked_prefix"
    calls = client.get(f"{u}/voice/calls", headers=b["boss"]["h"]).json()
    blocked = next(c for c in calls if c["call_id"] == call)
    assert blocked["status"] == "blocked" and "1900" in blocked["block_reason"] and blocked["extension"] == "201"

    r = client.put(f"{u}/voice/fraud-limits", json={"international": False}, headers=b["boss"]["h"])
    assert r.status_code == 200, r.text
    assert answer(client, secret, b, "442079460000") == "NO international_off"
    client.put(f"{u}/voice/fraud-limits", json={"international": True}, headers=b["boss"]["h"])
    assert answer(client, secret, b, "5375550100") == "NO high_risk"  # Cuba from Trinidad (fraud.py)

    # The daily spend cap (the business's credit for the day).
    client.put(f"{u}/voice/fraud-limits", json={"daily_cap": "0.01"}, headers=b["boss"]["h"])
    sim_call = {"extension": "201", "to": "+18685550100", "seconds": 120}
    sim = client.post(f"{u}/voice/calls/simulate", json=sim_call, headers=b["boss"]["h"])
    assert sim.status_code == 201 and sim.json()["allowed"]
    assert answer(client, secret, b, "18685550100") == "NO daily_cap"

    # Unknown or inactive callers, and malformed requests.
    assert answer(client, secret, b, "18685550100", ext="999") == "NO unknown_caller"
    assert answer(client, secret, b, "18685550100", ext="") == "NO unknown_caller"
    assert answer(client, secret, b, "12") == "NO bad_request"
    assert answer(client, secret, b, "18685550100", call="not a uuid") == "NO bad_request"
    assert re.fullmatch(r"NO \w+", answer(client, secret, b, "18685550100", tenant_="c000000000000"))


def test_a_tenant_cannot_spend_another_business_credit(client, secret):
    a = setup_voice(client, "PBX Tenant A")
    b = setup_voice(client, "PBX Tenant B")
    # A has hit its cap; B has not.
    client.put(f"{base(a)}/voice/fraud-limits", json={"daily_cap": "0"}, headers=a["boss"]["h"])
    assert answer(client, secret, a, "18685550100") == "NO daily_cap"
    assert answer(client, secret, b, "18685550100").startswith("OK ")
    # The business is the tenant the dial plan was rendered for: A's extension 201
    # asked under B's tenant is B's own 201, and a digest made for A's tenant
    # doesn't open B's.
    call = str(uuid.uuid4())
    sig_a = pbx.sign(secret, tenant(a), "201", "", "18685550100", call)
    assert ask(client, secret, b, "18685550100", call=call, sig=sig_a).status_code == 401
    # Forwarding by an extension the business doesn't have is refused.
    assert answer(client, secret, b, "18685550100", ext="", fwd="777") == "NO unknown_caller"
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT customer_id FROM voice_pbx_authorisations WHERE customer_id IN (%s, %s)", (a["id"], b["id"])
        ).fetchall()
    assert {str(r["customer_id"]) for r in rows} == {a["id"], b["id"]}


def test_rendered_dial_plan_asks_first_and_never_holds_up_emergency_calls(client, monkeypatch, secret):
    monkeypatch.setenv("EXA_PBX_CONTROLLER_URL", "http://controller:8000/")
    b = setup_voice(client, "PBX Plan Bank")
    client.patch(f"{base(b)}/voice/me", json={"forward_to": "+18685550199"}, headers=b["ben"]["h"])
    with db.tx() as conn:
        files = freeswitch.render(freeswitch.gather(conn, b["id"]))
    t = tenant(b)
    plan = files[f"{t}/dialplan.xml"]
    assert secret not in "".join(files.values())  # only ${exa_pbx_secret}, FreeSWITCH's own variable
    # The curl step, inline so the extensions after it can match its answer.
    asks = plan.split('name="outbound-authorise"')[1].split("</extension>")[0]
    assert 'continue="true"' in plan.split('name="outbound-authorise"')[1][:30]
    assert f"${{curl(http://controller:8000{URL}?tenant={t}&amp;ext=${{user_name}}" in asks
    assert f"append_headers X-Exa-Pbx-Auth:${{md5(${{exa_pbx_secret}}:{t}:" in asks and 'inline="true"' in asks
    # NO and no answer both refuse; OK sets X-Exa-Route and bridges.
    refused = plan.split('name="outbound-refused"')[1].split("</extension>")[0]
    assert "^NO (\\S+)" in refused and 'data="CALL_REJECTED"' in refused
    down = plan.split('name="outbound-unavailable"')[1].split("</extension>")[0]
    assert "^(?!OK )" in down and 'data="SERVICE_UNAVAILABLE"' in down
    assert 'data="sip_h_X-Exa-Route=$1"' in plan.split('name="outbound-route"')[1].split("</extension>")[0]
    final = plan.split('<extension name="outbound">')[1].split("</extension>")[0]
    assert 'expression="^OK "' in final and "sofia/gateway/exacarib_sip/+${exa_dest}" in final
    assert plan.index("outbound-authorise") < plan.index("outbound-refused") < plan.index('name="outbound"')
    # Forwarding to an outside number goes back through the same check.
    ben = plan.split('name="user-202"')[1].split("</extension>")[0]
    assert "exa_forwarded_by=202" in ben and f"loopback/+18685550199/{t}" in ben and "sofia/gateway" not in ben
    # Emergency calls come first and don't ask; with no carrier on, no header.
    em = plan.split('name="emergency"')[1].split("</extension>")[0]
    assert "curl" not in em and "X-Exa-Route" not in em and "sofia/gateway/exacarib_sip/$1" in em
    assert plan.index('name="emergency"') < plan.index("outbound-authorise")

    # With carriers on, an emergency call carries every one of them, in set order.
    switch_on("carrier", "sim-carrier-2")
    switch_on("carrier", "sim-carrier-1")
    with db.tx() as conn:
        sets = carriers.carrier_sets(conn)
        plan = freeswitch.render(freeswitch.gather(conn, b["id"]))[f"{t}/dialplan.xml"]
    em = plan.split('name="emergency"')[1].split("</extension>")[0]
    want = ",".join(str(n) for n in sorted(sets.values()))
    assert f'data="sip_h_X-Exa-Route={want}"' in em and em.index("X-Exa-Route") < em.index("bridge")


def test_kamailio_allow_list_has_the_pbx_networks(client, monkeypatch):
    monkeypatch.setenv("EXA_KAMAILIO_PBX_NETS", "172.18.0.0/16, 10.9.8.7")
    with db.tx() as conn:
        text = carriers.address_list(conn)
    assert "\n2 172.18.0.0 16 0 freeswitch\n" in text and "\n2 10.9.8.7 32 0 freeswitch\n" in text
    monkeypatch.setenv("EXA_KAMAILIO_PBX_NETS", "nope")
    with db.tx() as conn, pytest.raises(ValueError):
        carriers.address_list(conn)
