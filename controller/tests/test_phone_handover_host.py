"""Pure handover address grammar and fail-before-query behaviour; no SIP or DNS."""

from types import SimpleNamespace

import pytest

from exaconnect_controller.commai.voice import livekit
from exaconnect_controller.commai.voice.common import VoiceError, domain, valid_handover_host

CID = "11111111-1111-4111-8111-111111111111"
VALID_HOSTS = [
    "freeswitch:5080",
    "freesswitch:5080",
    "freeswitch",
    "PBX-1.internal",
    "pbx.example.com:1",
    "pbx.example.com:65535",
    "pbx.example.com:00080",
    "192.0.2.1",
    "192.0.2.1:5080",
    "0.0.0.0:1",
    "255.255.255.255:65535",
    ".".join(["a" * 63] * 3 + ["b" * 61]),
]
INVALID_HOSTS = [
    "",
    " ",
    "pbx internal",
    "pbx\tinternal",
    "pbx\r\nother",
    "pbx:0",
    "pbx:65536",
    "pbx:99999",
    "pbx:",
    "pbx:+5080",
    "pbx:-1",
    "pbx:5.0",
    "pbx:１２",
    "pbx:5080:5060",
    "pbx:5080/route",
    "pbx/route",
    "sip:pbx",
    "user@pbx",
    "pbx;transport=tcp",
    "pbx?header=value",
    "pbx#fragment",
    "pbx$(id)",
    "pbx`id`",
    "-pbx",
    "pbx-",
    "pbx..internal",
    ".pbx",
    "pbx.",
    "pbx_internal",
    "pбx.internal",
    "a" * 64,
    ".".join(["a" * 63] * 4),
    "256.1.2.3",
    "192.0.2.999",
    "127.1",
    "1234",
    "192.00.2.1",
    "192.0.2.1.",
    "192.0.2.1.2",
    "[::1]",
    "::1",
]


@pytest.mark.parametrize("host", VALID_HOSTS)
def test_valid_handover_host_is_used_without_resolving_it(monkeypatch, host):
    assert valid_handover_host(host)
    monkeypatch.setenv("EXA_PHONE_HANDOVER_HOST", host)
    queries = []

    class Connection:
        def execute(self, sql, params):
            queries.append((sql, params))
            return SimpleNamespace(fetchone=lambda: {"exists": 1})

    tenant = domain(CID).split(".")[0]
    assert livekit.handover_target(Connection(), CID) == f"sip:exa-ai-handover-{tenant}@{host}"
    assert len(queries) == 1 and queries[0][1] == (CID,)
    assert "voice_ai_rules" in queries[0][0]


@pytest.mark.parametrize("host", INVALID_HOSTS)
def test_invalid_handover_host_is_refused_before_any_database_query(monkeypatch, host):
    assert not valid_handover_host(host)
    monkeypatch.setenv("EXA_PHONE_HANDOVER_HOST", host)

    class NoQueries:
        def execute(self, *_):
            raise AssertionError("Invalid handover host must be rejected before querying")

    with pytest.raises(VoiceError) as error:
        livekit.handover_target(NoQueries(), CID)
    assert error.value.code == 503
    assert str(error.value) == "The internal phone handover route is not configured."


@pytest.mark.parametrize("value", [None, 5080, b"freeswitch", "pbx\x00internal"])
def test_handover_host_validator_refuses_values_unavailable_in_environment(value):
    assert not valid_handover_host(value)
