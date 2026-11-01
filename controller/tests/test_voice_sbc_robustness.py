"""Voice policy and failed/concurrent configuration publication, without a provider."""

from __future__ import annotations

import errno
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from exaconnect_controller import db
from exaconnect_controller.commai import golive
from exaconnect_controller.commai.voice import carriers, files, freeswitch, pbx, signalling, trunks
from exaconnect_controller.commai.voice.common import VoiceError


def test_failed_staging_preserves_the_entire_previous_configuration(tmp_path, monkeypatch):
    before = {"tenant/directory.xml": "old users", "tenant/dialplan.xml": "old routing"}
    files.write_files(before, str(tmp_path))
    stage = files._stage
    calls = 0

    def fail(path, content, mode):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError(errno.ENOSPC, "test disk full")
        return stage(path, content, mode)

    monkeypatch.setattr(files, "_stage", fail)
    with pytest.raises(OSError, match="disk full"):
        files.write_files({name: "new" for name in before}, str(tmp_path))
    assert {name: (tmp_path / name).read_text() for name in before} == before
    assert list(tmp_path.rglob("*.tmp")) == []


def test_failed_publication_restores_already_replaced_files(tmp_path, monkeypatch):
    before = {"address.list": "old permissions", "dispatcher.list": "old routes"}
    files.write_files(before, str(tmp_path))
    replace = files.os.replace
    calls = 0

    def fail(src, dst):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.EIO, "test rename failure")
        return replace(src, dst)

    monkeypatch.setattr(files.os, "replace", fail)
    with pytest.raises(OSError, match="rename failure"):
        files.write_files({name: "new" for name in before}, str(tmp_path))
    assert {name: (tmp_path / name).read_text() for name in before} == before
    assert list(tmp_path.glob("*.tmp")) == []


def test_concurrent_renderers_publish_complete_batches(tmp_path):
    start = threading.Barrier(2)

    def render(value):
        start.wait(timeout=5)
        files.write_files({"tenant/directory.xml": value, "tenant/dialplan.xml": value}, str(tmp_path))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(render, ["first render", "second render"]))
    assert (tmp_path / "tenant/directory.xml").read_text() == (tmp_path / "tenant/dialplan.xml").read_text()
    assert list(tmp_path.rglob("*.tmp")) == []


def test_invalid_xml_never_replaces_working_phone_configuration(tmp_path):
    before = {"tenant/directory.xml": "<include/>", "tenant/dialplan.xml": "<include/>"}
    freeswitch.write(before, str(tmp_path))
    with pytest.raises(ElementTree.ParseError):
        freeswitch.write(
            {"tenant/directory.xml": "<include><user/></include>", "tenant/dialplan.xml": "<bad"}, str(tmp_path)
        )
    assert {name: (tmp_path / name).read_text() for name in before} == before


@pytest.mark.parametrize("relative", ["../escape.xml", "/absolute.xml"])
def test_voice_files_cannot_escape_the_render_directory(tmp_path, relative):
    with pytest.raises(ValueError, match="inside"):
        files.write_files({relative: "no"}, str(tmp_path))


@pytest.mark.parametrize(
    "host",
    ["sip:carrier.example", "carrier.example;transport=udp", "a..example", "-a.example", "a\n101 sip:evil.example"],
)
def test_bad_carrier_addresses_are_rejected_before_persistence(host):
    conn = SimpleNamespace(execute=lambda *_: pytest.fail("invalid configuration reached the database"))
    with pytest.raises(VoiceError) as error:
        carriers.save(conn, {"key": "carrier", "outbound": {"host": host}}, "test")
    assert error.value.code == 422


def test_ipv6_trunk_is_validated_and_rendered_as_a_bracketed_sip_destination():
    cleaned = trunks._clean({"name": "IPv6 trunk", "host": "2001:db8::1", "transport": "tls"}, None)
    assert cleaned["outbound"]["host"] == "2001:db8::1"
    carrier = {"set_id": 2, "key": "org-trunk", "platform": None, "outbound": cleaned["outbound"]}
    conn = SimpleNamespace(execute=lambda *_: SimpleNamespace(fetchall=lambda: [carrier]))
    assert "2 sip:[2001:db8::1]:5061;transport=tls" in carriers.dispatcher_list(conn)
    assert signalling.host("SIP.Carrier.Example.") == "sip.carrier.example"


def test_platform_primary_host_keeps_the_highest_dispatcher_priority():
    carrier = {
        "set_id": 3,
        "key": "org-platform",
        "platform": "teams",
        "adapter": "sip",
        "outbound": {"hosts": ["primary.example", "secondary.example", "last.example"]},
    }
    conn = SimpleNamespace(execute=lambda *_: SimpleNamespace(fetchall=lambda: [carrier]))
    lines = [line.split() for line in carriers.dispatcher_list(conn).splitlines() if not line.startswith("#")]
    assert [(line[1].split(":")[1], int(line[3])) for line in lines] == [
        ("primary.example", 3),
        ("secondary.example", 2),
        ("last.example", 1),
    ]


@pytest.mark.parametrize("required,expected", [(True, "mandatory"), (False, "optional")])
def test_inbound_platform_media_enforces_the_links_security_policy(required, expected):
    xml = freeswitch._public(
        {
            "tenant": "tenant",
            "domain": "tenant.example",
            "numbers": [],
            "platform_links": [{"key": "org-link", "live": True, "enabled": True, "srtp": required}],
        }
    )
    actions = ElementTree.fromstring(xml).findall(".//action")
    assert f"rtp_secure_media={expected}" in [action.attrib.get("data") for action in actions]


def _phone_data():
    return {
        "domain": "tenant.example",
        "tenant": "tenant",
        "users": [],
        "numbers": [],
        "ring_groups": [],
        "queues": [
            {
                "id": "queue",
                "extension": "400",
                "name": "Support",
                "members": [],
                "strategy": "longest-idle-agent",
                "max_wait_s": 60,
            }
        ],
        "hours": [],
        "menus": [],
        "blocked_prefixes": [],
        "controller_url": "http://controller:8000",
        "ai_rules": [{"fallback": "queue"}],
    }


@pytest.mark.parametrize("ready", [None, False])
def test_unready_telephone_ai_goes_directly_to_the_configured_fallback(ready):
    data = _phone_data()
    if ready is not None:
        data["ai_phone_ready"] = ready
    xml = ElementTree.fromstring(freeswitch._dialplan(data))
    ai = xml.find(".//extension[@name='exa-ai']")
    assert ai.find(".//action[@application='bridge']") is None
    assert ai.find(".//action[@application='transfer']").attrib["data"] == "400 XML tenant"


@pytest.mark.parametrize("platform", [False, True])
@pytest.mark.parametrize(
    "reply",
    [
        "OK ",
        "OK none",
        "OK 1",
        "OK invalid none",
        "OK 0 none",
        "OK 1 spoofed",
        "OK none none trailing",
        "",
        "NO daily_cap",
    ],
)
def test_malformed_pbx_authorisation_never_bridges_an_outside_call(platform, reply):
    xml = ElementTree.fromstring("<include>" + "".join(freeswitch._outbound(_phone_data(), platform)) + "</include>")
    prefix = "platform-" if platform else ""
    bridge = xml.find(f".//extension[@name='{prefix}outbound']/condition[@field='${{exa_auth}}']")
    unavailable = xml.find(f".//extension[@name='{prefix}outbound-unavailable']/condition[@field='${{exa_auth}}']")
    assert not re.search(bridge.attrib["expression"], reply)
    assert re.search(unavailable.attrib["expression"], reply)


@pytest.mark.parametrize("reply", ["OK none none", "OK 1 +18685550100", "OK 2,1 none"])
def test_complete_pbx_authorisation_still_allows_the_outside_call(reply):
    xml = ElementTree.fromstring("<include>" + "".join(freeswitch._outbound(_phone_data())) + "</include>")
    condition = xml.find(".//extension[@name='outbound']/condition[@field='${exa_auth}']")
    assert re.search(condition.attrib["expression"], reply)


def test_emergency_calls_include_own_trunks_without_another_tenants_trunks(monkeypatch):
    rows = [
        {"key": "org-own", "set_id": 1, "customer_id": "org"},
        {"key": "org-other", "set_id": 2, "customer_id": "other"},
        {"key": "approved", "set_id": 3, "customer_id": None},
        {"key": "unapproved", "set_id": 4, "customer_id": None},
    ]
    conn = SimpleNamespace(execute=lambda *_: SimpleNamespace(fetchall=lambda: rows))
    monkeypatch.setattr(golive, "enabled", lambda _conn, _kind, key, _cid: key == "approved")
    assert pbx.emergency_sets(conn, "org") == [1, 3]
    assert pbx.emergency_sets(conn, "other") == [2, 3]


def test_registered_phone_directory_carries_an_emergency_callback_number():
    data = {
        **_phone_data(),
        "customer_id": "org",
        "users": [
            {
                "extension": "201",
                "name": "Test caller",
                "a1": "test-digest",
                "site": "Office",
                "email": "",
                "voicemail_to_email": False,
                "voicemail_greeting": "",
                "emergency_callback": "+18685550100",
            }
        ],
    }
    directory = ElementTree.fromstring(freeswitch._directory(data))
    callback = directory.find(".//user[@id='201']/variables/variable[@name='exa_emergency_callback']")
    assert callback.attrib["value"] == "+18685550100"


def test_emergency_render_uses_only_the_organisations_trunks_and_callback(client):
    """Real PostgreSQL relationships and the complete gather/render path."""
    with db.tx() as conn:
        first = conn.execute("INSERT INTO customers (name) VALUES ('Voice A') RETURNING id").fetchone()["id"]
        second = conn.execute("INSERT INTO customers (name) VALUES ('Voice B') RETURNING id").fetchone()["id"]
        for cid, suffix in ((first, "01"), (second, "02")):
            user = conn.execute(
                """INSERT INTO voice_users (customer_id, name, extension, sip_password)
                   VALUES (%s, 'Caller', '201', 'local-test-value') RETURNING id""",
                (cid,),
            ).fetchone()["id"]
            conn.execute(
                """INSERT INTO voice_numbers (customer_id, e164, status, target_type, target_id)
                   VALUES (%s, %s, 'active', 'user', %s)""",
                (cid, "+186855501" + suffix, user),
            )
            conn.execute(
                "INSERT INTO voice_carriers (key, name, customer_id) VALUES (%s, 'Own trunk', %s)",
                ("org-" + suffix, cid),
            )
        own_set = conn.execute(
            "SELECT set_id FROM voice_carriers WHERE customer_id = %s",
            (first,),
        ).fetchone()["set_id"]
        # A platform is not a PSTN emergency carrier, even for the same tenant.
        conn.execute(
            """INSERT INTO voice_carriers (key, name, customer_id, platform)
               VALUES ('org-platform', 'Calling platform', %s, 'teams')""",
            (first,),
        )
        data = freeswitch.gather(conn, first)
        assert data["emergency_sets"] == [own_set]
        assert data["users"][0]["emergency_callback"] == "+18685550101"
        rendered = freeswitch.render(data)
    directory = ElementTree.fromstring(rendered[f"{data['tenant']}/directory.xml"])
    callback = directory.find(".//user[@id='201']/variables/variable[@name='exa_emergency_callback']")
    assert callback.attrib["value"] == "+18685550101"
