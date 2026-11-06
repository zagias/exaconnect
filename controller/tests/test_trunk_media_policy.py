"""Carrier policy, signed PBX API and XML checks; no database/provider/network."""

import re
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from xml.etree import ElementTree as XML

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from exaconnect_controller.api.deps import User, current_user
from exaconnect_controller.commai import entitlements
from exaconnect_controller.commai.api import voice_pbx, voice_trunks
from exaconnect_controller.commai.voice import (
    carriers,
    freeswitch,
    pbx,
    platforms,
    protected_ingress,
    trunk_media,
    trunks,
)
from exaconnect_controller.commai.voice.common import VoiceError, domain

from .test_voice_sbc_robustness import _phone_data

CID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
CALL = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TENANT = domain(CID).split(".")[0]


def carrier(key="own", *, required=True, transport="tls", cid=CID, health="up"):
    return {
        "id": key,
        "key": key,
        "name": key,
        "customer_id": cid,
        "platform": None,
        "adapter": "sip",
        "outbound": {"host": "carrier.example", "transport": transport, "srtp_required": required},
        "inbound": {},
        "enabled": True,
        "health": health,
        "last_options_at": None,
        "last_rtt_ms": None,
    }


@pytest.mark.parametrize("value", [None, "true", "false", 0, 1, [], {}])
def test_direct_helper_rejects_non_boolean_media_policy(value):
    with pytest.raises(VoiceError):
        trunks._clean({"name": "Carrier", "host": "carrier.example", "transport": "tls", "srtp_required": value}, None)


@pytest.mark.parametrize("transport", ["udp", "tcp"])
def test_encryption_cannot_be_silently_removed_by_transport_patch(transport):
    with pytest.raises(VoiceError, match="TLS"):
        trunks._clean({"transport": transport}, carrier())


def test_omitted_policy_preserves_patch_and_legacy_default():
    assert trunks._clean({"name": "Changed"}, carrier())["outbound"]["srtp_required"] is True
    old = carrier(required=False, transport="udp")
    del old["outbound"]["srtp_required"]
    assert trunks._out(old)["srtp_required"] is False
    assert trunks._clean({"name": "Changed"}, old)["outbound"]["srtp_required"] is False
    assert trunks._clean({"srtp_required": False, "transport": "udp"}, carrier())["outbound"]["srtp_required"] is False
    with pytest.raises(VoiceError):
        trunks._clean({"name": "Fake", "adapter": "simulated", "transport": "tls", "srtp_required": True}, None)


def test_saved_trunk_persists_typed_policy_and_renders_only_its_business(monkeypatch):
    old = carrier(required=False)
    old.update(id="33333333-3333-4333-8333-333333333333", consecutive_failures=0)
    writes, renders = [], []

    class Connection:
        def execute(self, sql, params):
            if sql.startswith("SELECT *"):
                row = deepcopy(old)
            elif sql.startswith("SELECT 1"):
                row = None
            else:
                assert sql.startswith("UPDATE voice_carriers") and params[-1] == old["id"]
                writes.append(deepcopy(params[2].obj))
                row = {**old, "outbound": params[2].obj, "inbound": params[3].obj}
            return SimpleNamespace(fetchone=lambda: row)

    monkeypatch.setattr(trunks.audit, "record", lambda *_: None)
    monkeypatch.setattr(carriers, "ensure_health_loop", lambda *_: None)
    monkeypatch.setattr(carriers, "render_kamailio", lambda *_: None)
    monkeypatch.setattr(freeswitch, "render_business", lambda _conn, cid: renders.append(cid))
    result = trunks.save_trunk(Connection(), CID, {"srtp_required": True}, "staff", old["id"])
    assert result["srtp_required"] is True and result["secure_ingress_ready"] is False
    assert writes[0]["srtp_required"] is True and writes[0]["transport"] == "tls"
    assert renders == [CID]


@pytest.mark.parametrize("value", ["true", "false", 0, 1, None])
def test_trunk_api_rejects_untyped_or_null_security_value_before_save(monkeypatch, value):
    app = FastAPI()
    app.include_router(voice_trunks.router)
    app.dependency_overrides[current_user] = lambda: User("u", "staff@example.org", "admin", None, None)
    monkeypatch.setattr(voice_trunks.db, "tx", lambda: nullcontext(object()))
    monkeypatch.setattr(voice_trunks, "_admin", lambda *_: None)
    called = []

    def save(_conn, cid, body, _actor, *_):
        called.append(cid)
        return trunks._clean(body, carrier())

    monkeypatch.setattr(voice_trunks.trunks, "save_trunk", save)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.patch(f"/customers/{CID}/voice/trunks/owned", json={"srtp_required": value})
    assert response.status_code == 422
    assert called == []


@pytest.mark.parametrize("secondary,fallback", [(True, False), (False, True)])
def test_actual_route_candidates_prevent_downgrade_even_when_secure_primary_is_down(monkeypatch, secondary, fallback):
    from exaconnect_controller import organisation

    monkeypatch.setattr(organisation, "join_up", lambda *_: None)
    monkeypatch.setattr(trunks, "settings", lambda *_: {"default_trunk_id": "secure", "exacarib_fallback": fallback})
    monkeypatch.setattr(carriers, "any_enabled", lambda *_: False)
    monkeypatch.setattr(trunks, "caller_location", lambda *_: {"id": "loc", "name": "Office"})
    rows = {"secure": carrier("secure", health="down"), "legacy": carrier("legacy", required=False)}

    class Connection:
        def execute(self, sql, params):
            if "SELECT 1 FROM voice_carriers" in sql:
                row = {"ok": 1}
            elif "voice_location_trunks" in sql:
                row = {"primary_trunk_id": "secure", "secondary_trunk_id": "legacy" if secondary else None}
            else:
                row = deepcopy(rows[params[0]])
            return SimpleNamespace(fetchone=lambda: row)

    result = trunks.route(Connection(), CID, "+18685550100")
    assert result["route"] == (["legacy"] if secondary else [])  # Preview explains refusal without hiding candidates.
    assert result["media_policy_error"]
    with pytest.raises(trunk_media.PolicyError):
        carriers.route_keys(Connection(), CID, "18685550100")


@pytest.mark.parametrize(
    "rows",
    [
        [carrier(), carrier("legacy", required=False)],
        [carrier(transport="udp")],
        [carrier(required="false")],
    ],
)
def test_invalid_or_mixed_candidate_modes_fail_shut(rows):
    with pytest.raises(trunk_media.PolicyError):
        trunk_media.candidates(rows)


@pytest.mark.parametrize("rows", [[carrier(cid=OTHER)], [], [carrier("different")]])
def test_carrier_media_lookup_refuses_tenant_or_missing_route_tampering(rows):
    conn = SimpleNamespace(execute=lambda *_: SimpleNamespace(fetchall=lambda: rows))
    with pytest.raises(trunk_media.PolicyError):
        carriers.media_policy(conn, CID, ["own"])


@pytest.fixture
def admission(monkeypatch):
    state = SimpleNamespace(rows=[carrier()], verdict={"allowed": True, "route": ["own"]}, records=[])

    class Connection:
        def execute(self, sql, params):
            if "FROM voice_users" in sql:
                return SimpleNamespace(fetchone=lambda: {"id": "voice-user"})
            assert "WHERE key = ANY" in sql and params == (state.verdict["route"],)
            return SimpleNamespace(fetchall=lambda: state.rows)

    monkeypatch.setattr(pbx, "business_for_tenant", lambda *_: CID)
    monkeypatch.setattr(pbx, "caller_id", lambda *_: "+18685550100")
    monkeypatch.setattr(entitlements, "enabled", lambda *_: True)
    monkeypatch.setattr(platforms, "caller", lambda *_: {"e164": "+18685550100"})
    monkeypatch.setattr(pbx.billing, "authorise", lambda *_a, **_kw: deepcopy(state.verdict))
    monkeypatch.setattr(carriers, "carrier_sets", lambda *_: {"own": 7, "legacy": 8})
    monkeypatch.setattr(pbx, "_record", lambda *_a: state.records.append(deepcopy(_a[5])))
    monkeypatch.setattr(pbx.billing, "record_call", lambda *_: None)
    monkeypatch.setattr(pbx.audit, "record", lambda *_: None)
    return state, Connection()


@pytest.mark.parametrize("platform", [False, True])
def test_signed_versioned_api_chooses_media_and_ignores_caller_claims(admission, monkeypatch, platform):
    _, conn = admission
    monkeypatch.setenv(pbx.SECRET_ENV, "fixture-secret")
    monkeypatch.setattr(voice_pbx.db, "tx", lambda: nullcontext(conn))
    app = FastAPI()
    app.include_router(voice_pbx.public)
    ext, fwd = ("link", "+18685550100") if platform else ("200", "")
    params = {"tenant": TENANT, "to": "18685550199", "call": CALL, "mode": "LEGACY"}
    params.update({"link": ext, "from": fwd} if platform else {"ext": ext})
    header = {pbx.HEADER: pbx.sign("fixture-secret", TENANT, ext, fwd, params["to"], CALL)}
    path = "/internal/voice/authorise" + ("-platform" if platform else "")
    with TestClient(app) as client:
        assert client.get(path + "-v2", params=params).status_code == 401
        response = client.get(path + "-v2", params=params, headers=header)
        assert response.text == "OK 7 +18685550100 SRTP_REQUIRED"
        assert client.get(path, params=params, headers=header).text == "NO media_policy_required"


@pytest.mark.parametrize("platform", [False, True])
def test_legacy_call_contract_remains_exact_and_v2_modes_are_explicit(admission, platform):
    state, conn = admission
    state.rows = [carrier(required=False)]
    function = pbx.authorise_platform if platform else pbx.authorise
    args = (conn, TENANT, "link" if platform else "200", "+18685550100" if platform else "", "18685550199", CALL)
    assert function(*args) == "OK 7 +18685550100"
    assert function(*args, media_v2=True) == "OK 7 +18685550100 LEGACY"
    state.verdict["route"].append("legacy")
    state.rows.append(carrier("legacy"))
    assert function(*args, media_v2=True) == "NO media_policy_mismatch"


@pytest.mark.parametrize("platform", [False, True])
def test_v2_rendered_offer_has_only_carrier_b_leg_encryption(platform):
    d = {**_phone_data(), "outbound_media_v2": True}
    xml = XML.fromstring("<include>" + "".join(freeswitch._outbound(d, platform)) + "</include>")
    prefix = "platform-" if platform else ""
    encrypted = xml.find(f".//extension[@name='{prefix}outbound-srtp']")
    assert encrypted.find(".//action[@application='bridge']").attrib["data"] == (
        "{rtp_secure_media_outbound=mandatory:AES_CM_128_HMAC_SHA1_80,"
        "sip_h_X-Exa-Media-Policy=SRTP_REQUIRED}sofia/gateway/exacarib_sip_protected/+${exa_dest}"
    )
    assert not xml.findall(".//action[@application='export']")
    conditions = xml.findall(".//extension/condition[@field='${exa_auth}']")
    bridge = encrypted.find("condition[@field='${exa_auth}']")
    assert re.fullmatch(bridge.attrib["expression"], "OK 7 +18685550100 SRTP_REQUIRED")
    for bad in ["OK 7 +18685550100", "OK 7 +18685550100 LEGACY", "OK 7 none SRTP_REQUIRED injected"]:
        assert not re.fullmatch(bridge.attrib["expression"], bad)
    assert any(
        "authorise" + ("-platform" if platform else "") + "-v2" in a.attrib["data"]
        for a in xml.findall(".//action[@application='set']")
    )
    assert conditions


@pytest.mark.parametrize("policy", ["SRTP_REQUIRED", "BLOCKED", "spoofed"])
def test_protected_inbound_did_is_rejected_before_platform_headers_or_tenant_transfer(policy):
    d = {
        **_phone_data(),
        "numbers": [{"e164": "+16474927669", "carrier_media_policy": policy}],
        "platform_links": [{"key": "org-link", "live": True, "enabled": True, "srtp": True}],
    }
    xml = XML.fromstring(freeswitch._public(d))
    first = xml.find("extension")
    assert first.attrib["name"] == "in-16474927669"
    assert first.find("condition").attrib["field"] == "destination_number"
    assert first.find(".//action[@application='hangup']").attrib["data"] == "SERVICE_UNAVAILABLE"
    assert first.find(".//action[@application='transfer']") is None
    assert first.find(".//action[@application='bridge']") is None
    assert "sip_via_protocol" not in freeswitch._public(d)
    assert "sip_h_X-Exa-TLS" not in freeswitch._public(d)


def test_legacy_did_and_internal_ai_media_are_unchanged():
    d = {
        **_phone_data(),
        "ai_rules": [],
        "numbers": [{"id": "number", "e164": "+16474927669", "target_type": "user", "target_id": None}],
    }
    legacy = freeswitch._public(d)
    d["numbers"][0]["carrier_media_policy"] = "LEGACY"
    assert freeswitch._public(d) == legacy
    assert "rtp_secure_media" not in legacy and "exa-in-16474927669 XML tenant" in legacy
    assert "rtp_secure_media_outbound" not in freeswitch._dialplan(d)


def test_old_and_new_policy_responses_cannot_cross_render_protocols():
    assert re.fullmatch(freeswitch.AUTH_OK, "OK 7 none")
    assert not re.fullmatch(freeswitch.AUTH_OK, "OK 7 none SRTP_REQUIRED")
    assert re.fullmatch(freeswitch.AUTH_OK_V2, "OK 7 none SRTP_REQUIRED")
    for answer in ["OK 7 none", "OK 7 none unknown", "OK 7 none SRTP_REQUIRED injected"]:
        assert not re.fullmatch(freeswitch.AUTH_OK_V2, answer)


def test_owned_legacy_did_has_no_new_controller_dependency():
    d = {
        **_phone_data(),
        "tenant": TENANT,
        "numbers": [
            {"id": "owned", "source": "trunk", "e164": "+16474927669", "target_type": "user", "target_id": None}
        ],
    }
    candidate = freeswitch._public(d)
    approved = protected_ingress.render_owned_public(d)
    assert "curl(" not in candidate and "curl(" not in approved
    assert "SERVICE_UNAVAILABLE" in candidate
    assert "exa-in-16474927669 XML " + TENANT not in candidate
    assert "exa-in-16474927669 XML " + TENANT in approved
