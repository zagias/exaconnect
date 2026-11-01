"""The AI bridge keeps owned DID/rule identity and cannot open public tenant routes."""

import re
from xml.etree import ElementTree as XML

import pytest

from exaconnect_controller.commai.voice import freeswitch

from .test_voice_sbc_robustness import _phone_data


def data():
    return {
        **_phone_data(),
        "ai_phone_ready": True,
        "numbers": [
            {"id": "first", "e164": "+18685550100", "target_type": "ai", "target_id": None},
            {"id": "second", "e164": "+18685550200", "target_type": "ai", "target_id": None},
        ],
        "ai_rules": [
            {"number_id": "first", "fallback": "queue", "condition": "always"},
            {"number_id": "second", "fallback": "ring_group", "condition": "always"},
        ],
        "ring_groups": [
            {
                "id": "sales",
                "extension": "300",
                "members": [],
                "strategy": "simultaneous",
                "ring_seconds": 15,
                "name": "Sales",
            }
        ],
    }


@pytest.mark.parametrize("ready", [True, False])
def test_owned_dids_keep_individual_rule_fallbacks(ready):
    d = data()
    d["ai_phone_ready"] = ready
    xml = XML.fromstring(freeswitch._dialplan(d))
    for number, fallback in (("18685550100", "400"), ("18685550200", "300")):
        extension = xml.find(f".//extension[@name='exa-ai-number-{number}']")
        condition = extension.find("condition")
        assert re.fullmatch(condition.attrib["expression"], "exa-ai:+" + number + (":OK" if ready else ""))
        assert not re.fullmatch(condition.attrib["expression"], "exa-ai:tenant")
        assert not re.fullmatch(condition.attrib["expression"], "exa-ai:+18685550999")
        assert extension.find(".//action[@application='transfer']").attrib["data"] == fallback + " XML tenant"
        bridge = extension.find(".//action[@application='bridge']")
        if ready:
            assert bridge.attrib["data"] == "sofia/gateway/exacarib_ai/+" + number
            exports = [x.attrib["data"] for x in extension.findall(".//action[@application='export']")]
            assert "force_transfer_context=tenant" in exports
            assert "exa_ai_handover_allowed=true" in exports
            assert "exa_ai_fallback=" + fallback in exports
            check = xml.find(f".//extension[@name='exa-ai-check-{number}']")
            assert check.attrib["continue"] == "true"
            ask = check.find(".//action[@application='set']")
            assert ask.attrib["inline"] == "true" and "phone-authorise" in ask.attrib["data"]
            assert "${md5(${exa_pbx_secret}:tenant:::" + number in ask.attrib["data"]
            refused = xml.find(f".//extension[@name='exa-ai-unavailable-{number}']")
            assert refused.find(".//action[@application='bridge']") is None
            assert refused.find(".//action[@application='transfer']").attrib["data"] == fallback + " XML tenant"
        else:
            assert bridge is None
    assert xml.find(".//extension[@name='exa-ai']//action[@application='bridge']") is None


def test_public_did_exports_owned_identity_but_no_public_handover_route():
    xml = XML.fromstring(freeswitch._public(data()))
    assert "exa-ai-handover" not in XML.tostring(xml, encoding="unicode")
    first = xml.find(".//extension[@name='in-18685550100']")
    exports = [x.attrib["data"] for x in first.findall(".//action[@application='export']")]
    assert exports == ["exa_ai_dialled=+18685550100", "exa_ai_fallback=400"]
    assert "exa_ai_handover_allowed=true" not in exports


def test_handover_is_bounded_to_established_tenant_and_allowed_fallbacks():
    xml = XML.fromstring(freeswitch._dialplan(data()))
    routes = xml.findall(
        ".//extension/condition[@field='${destination_number}:${exa_ai_handover_allowed}:${exa_ai_fallback}']"
    )
    assert len(routes) == 3
    accepted = "exa-ai-handover-tenant:true:400"
    route = next(x for x in routes if re.fullmatch(x.attrib["expression"], accepted))
    assert route.find("action[@application='transfer']").attrib["data"] == "400 XML tenant"
    assert route.find("action[@application='set']").attrib["data"] == "exa_ai_handover_allowed=false"
    for refused in (
        "exa-ai-handover-other:true:400",
        "exa-ai-handover-tenant:false:400",
        "exa-ai-handover-tenant:true:+19005550100",
        "exa-ai-handover-tenant:true:exa-ai",
    ):
        assert not any(re.fullmatch(x.attrib["expression"], refused) for x in routes)
