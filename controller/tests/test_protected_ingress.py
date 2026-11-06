"""Offline policy/render/activation checks. These do not qualify native SIP/TLS."""

from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as XML

import pytest

from exaconnect_controller.commai.voice import freeswitch
from exaconnect_controller.commai.voice import protected_ingress as ingress
from exaconnect_controller.commai.voice.common import domain

SOURCE = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "protected_policy_loader", SOURCE / "deploy/kamailio/protected_policy_loader.py"
)
loader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(loader)
AUTH_SPEC = importlib.util.spec_from_file_location(
    "provision_origin_auth", SOURCE / "deploy/kamailio/provision_origin_auth.py"
)
origin_auth = importlib.util.module_from_spec(AUTH_SPEC)
AUTH_SPEC.loader.exec_module(origin_auth)
CID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
TENANT = domain(CID).split(".")[0]
NUMBER = "+18685550100"


def carrier(**updates):
    value = {
        "id": "carrier-id",
        "key": "owned",
        "customer_id": CID,
        "platform": None,
        "adapter": "sip",
        "enabled": True,
        "set_id": 7,
        "outbound": {"host": "carrier.example", "transport": "tls", "srtp_required": True},
        "inbound": {"allow_ips": ["192.0.2.0/25", "2001:db8::1/128"]},
    }
    value.update(updates)
    return value


def number(**updates):
    value = {
        "id": "number-id",
        "customer_id": CID,
        "e164": NUMBER,
        "source": "trunk",
        "carrier": "owned",
        "target_type": "user",
        "target_id": "staff-user",
    }
    value.update(updates)
    return value


def row(n=None, c=None):
    n, c = n or number(), c or carrier()
    return {**n, **{"carrier_" + key: value for key, value in c.items()}}


def connection(rows):
    return SimpleNamespace(execute=lambda *_: SimpleNamespace(fetchall=lambda: deepcopy(rows)))


@pytest.mark.parametrize(
    "change",
    [
        {"customer_id": OTHER},
        {"platform": "teams"},
        {"adapter": "simulated"},
        {"enabled": False},
        {"set_id": None},
        {"inbound": {"allow_ips": []}},
        {"inbound": {"allow_ips": ["0.0.0.0/0"]}},
        {"outbound": {"transport": "udp", "srtp_required": True}},
        {"outbound": {"transport": "tls", "srtp_required": "false"}},
    ],
)
def test_invalid_or_other_owner_policy_cannot_become_legacy(change):
    assert ingress.describe(number(), carrier(**change))["state"] == "blocked"


def test_owned_orphan_and_legacy_are_distinct():
    assert ingress.describe(number(), None)["state"] == "blocked"
    assert ingress.describe(number(source="ordered"), None) is None
    assert ingress.describe(number(), carrier(outbound={"transport": "udp"})) is None


def test_revision_is_canonical_and_binds_target_and_carrier_set():
    a = ingress.describe(number(), carrier())
    b = ingress.describe(number(), carrier(inbound={"allow_ips": ["2001:db8::1/128", "192.0.2.1/25"]}))
    assert a == b
    assert a["group"] not in (1, 2)
    assert ingress.describe(number(target_id="someone-else"), carrier())["revision"] != a["revision"]
    assert ingress.describe(number(), carrier(set_id=8))["revision"] != a["revision"]
    assert (
        ingress.describe(
            number(), carrier(outbound={"host": "different.example", "transport": "tls", "srtp_required": True})
        )["revision"]
        != a["revision"]
    )


def test_actual_gather_selects_and_maps_dispatcher_set_into_protected_descriptor(monkeypatch):
    c, n = carrier(), number()
    joined = {**n, **{"trunk_" + key: value for key, value in c.items()}}
    seen = []

    class Connection:
        def execute(self, sql, _params):
            seen.append(sql)
            rows = []
            if "FROM voice_numbers n LEFT JOIN voice_carriers c" in sql:
                assert "c.set_id AS trunk_set_id" in sql
                rows = [deepcopy(joined)]
            elif "SELECT adapter, outbound FROM voice_carriers" in sql:
                rows = [c]
            return SimpleNamespace(fetchall=lambda: rows, fetchone=lambda: None)

    monkeypatch.setattr(freeswitch, "fraud_limits", lambda *_: {"blocked_prefixes": []})
    monkeypatch.setattr(freeswitch.pbx, "emergency_sets", lambda *_: [])
    monkeypatch.setattr(freeswitch.livekit, "phone_ready", lambda *_a, **_kw: False)
    monkeypatch.setattr(freeswitch.platforms, "dial_data", lambda *_: {})
    data = freeswitch.gather(Connection(), CID)
    policy = data["numbers"][0]["protected_ingress"]
    assert policy["state"] == "protected" and policy["carrier_set"] == 7
    assert policy == ingress.describe(n, c)
    assert seen and "trunk_set_id" not in data["numbers"][0]


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    # Native orchestration is simulated; these user-owned test directories
    # intentionally cannot pass the real private operator ownership boundary.
    monkeypatch.setattr(loader, "operator_root", lambda *_: None)
    policy_dir, fs_dir, approved = (tmp_path / name for name in ("policy", "freeswitch", "approved"))
    policy_dir.mkdir()
    directory = fs_dir / TENANT
    directory.mkdir(parents=True)
    n, c = number(), carrier()
    n["protected_ingress"] = ingress.describe(n, c)
    data = {
        "numbers": [n],
        "tenant": TENANT,
        "domain": domain(CID),
        "ai_rules": [],
    }
    files = ingress.edge_files(connection([row()]), "# group ip mask port tag\n1 192.0.2.0 25 0 carrier\n")
    for name, content in files.items():
        (policy_dir / name).write_text(content)
    for name, content in (
        ("owned-public.xml", ingress.render_owned_public(data)),
        ("protected.xml", ingress.render_private(data)),
        (
            "dialplan.xml",
            f'<include><context name="{TENANT}"><extension name="inbound">'
            f'<condition><action application="transfer" data="200 XML {TENANT}"/>'
            f'<action application="bridge" data="loopback/+18685550199/{TENANT}"/>'
            f'<action application="set" data="force_transfer_context={TENANT}"/>'
            f'<action application="ivr" data="{TENANT}_menu_300"/>'
            f'<action application="set" data="signed_url=controller?tenant={TENANT}"/>'
            "</condition></extension></context></include>",
        ),
        (
            "ivr.xml",
            f'<include><menu name="{TENANT}_menu_300"><entry param="transfer 200 XML {TENANT}"/></menu></include>',
        ),
    ):
        (directory / name).write_text(content)
    peers = tmp_path / "peers.json"
    peers.write_text(json.dumps({"sbc_ip": "172.30.1.2", "pbx_ip": "172.30.1.3"}))
    revision = json.loads(files["protected-ingress.json"])["revision"]
    bundle = loader.prepare(policy_dir, fs_dir, peers, approved, revision)
    return SimpleNamespace(
        root=approved,
        bundle=bundle,
        policy_dir=policy_dir,
        fs_dir=fs_dir,
        peers=peers,
        revision=revision,
        number=n,
        data=data,
    )


def test_exact_owner_snapshot_yields_distinct_protected_source_group(prepared):
    manifest = loader.verify_bundle(prepared.bundle, prepared.bundle.name)
    entry = manifest["entries"][0]
    assert entry["tenant"] == TENANT
    assert entry["sources"] == ["192.0.2.0/25", "2001:db8::1/128"]
    assert entry["carrier_set"] == 7
    assert not (prepared.root / "current").exists()  # preparing never activates
    assert (
        loader.prepare(prepared.policy_dir, prepared.fs_dir, prepared.peers, prepared.root, prepared.revision)
        == prepared.bundle
    )


def test_approved_generation_freezes_routing_not_signed_tenant_fields(prepared):
    context = (prepared.bundle / "contexts/routes.xml").read_text()
    root = XML.fromstring(context)
    frozen = root.find("context").get("name")
    assert frozen.startswith(TENANT + "_g")
    assert f"200 XML {frozen}" in context
    assert f"loopback/+18685550199/{frozen}" in context
    assert f"force_transfer_context={frozen}" in context
    assert f"{frozen}_menu_300" in context
    assert f"signed_url=controller?tenant={TENANT}" in context
    assert f"XML {frozen}" in (prepared.bundle / "ivr/routes.xml").read_text()
    assert f"exa-in-{NUMBER[1:]} XML {frozen}" in (prepared.bundle / "protected/routes.xml").read_text()


def test_candidate_change_and_controller_outage_do_not_change_approved_routes(prepared):
    before = {name: (prepared.bundle / name).read_bytes() for name in loader.MEMBERS}
    # The approved route is local XML. No new remote admission dependency exists.
    for name in ("public/routes.xml", "protected/routes.xml"):
        assert "curl(" not in before[name].decode()
    (prepared.policy_dir / "protected-ingress.json").write_text("{}")
    (prepared.fs_dir / TENANT / "dialplan.xml").write_text("<include/>")
    assert loader.verify_bundle(prepared.bundle, prepared.bundle.name)
    assert before == {name: (prepared.bundle / name).read_bytes() for name in loader.MEMBERS}


@pytest.mark.parametrize("filename", ["owned-public.xml", "protected.xml"])
def test_partial_or_revision_mismatched_fs_render_refuses_prepare(prepared, filename):
    path = prepared.fs_dir / TENANT / filename
    path.write_text("<include/>")
    with pytest.raises(ValueError):
        loader.prepare(prepared.policy_dir, prepared.fs_dir, prepared.peers, prepared.root, prepared.revision)


def test_stale_legacy_render_cannot_unlock_new_protected_policy(prepared):
    n = {**prepared.number, "protected_ingress": None}
    (prepared.fs_dir / TENANT / "owned-public.xml").write_text(
        ingress.render_owned_public({**prepared.data, "numbers": [n]})
    )
    with pytest.raises(ValueError):
        loader.prepare(prepared.policy_dir, prepared.fs_dir, prepared.peers, prepared.root, prepared.revision)


def test_private_unconditional_transfer_cannot_hide_alongside_valid_conditions(prepared):
    path = prepared.fs_dir / TENANT / "protected.xml"
    root = XML.fromstring(path.read_text())
    root.find("extension").insert(0, XML.Element("action", {"application": "transfer", "data": "200 XML default"}))
    path.write_text(XML.tostring(root, encoding="unicode"))
    with pytest.raises(ValueError):
        loader.prepare(prepared.policy_dir, prepared.fs_dir, prepared.peers, prepared.root, prepared.revision)


@pytest.mark.parametrize("member", ["policy.json", "public/routes.xml", "protected/routes.xml", "contexts/routes.xml"])
def test_corrupt_approved_member_refuses_verification(prepared, member):
    target = prepared.bundle / member
    target.chmod(0o644)
    target.write_text("corrupt")
    with pytest.raises(ValueError):
        loader.verify_bundle(prepared.bundle, prepared.bundle.name)


class FakeNative:
    container, pbx = "edge", "pbx"

    def __init__(self, bundle, *, fail_at=None):
        self.bundle, self.fail_at, self.calls = bundle, fail_at, []

    def rpc(self, *args):
        self.calls.append(("rpc", *args))
        return ""

    def command(self, container, *args):
        self.calls.append(("command", container, *args))
        if args[0] == self.fail_at:
            raise RuntimeError("simulated native failure")
        if args[0] == "sha256sum":
            path = self.bundle / args[1].split(self.bundle.name + "/", 1)[1]
            return loader.sha(path.read_bytes()) + "  " + args[1] + "\n"
        if args[0] == "fs_cli":
            if args[-1].startswith("xml_locate"):
                name = args[-1].split()[-1]
                if name in ("public", "exacarib-protected"):
                    filename = "public/routes.xml" if name == "public" else "protected/routes.xml"
                    context = XML.Element("context", {"name": name})
                    context.extend(XML.parse(self.bundle / filename).getroot())
                    return XML.tostring(context, encoding="unicode")
                for context in XML.parse(self.bundle / "contexts/routes.xml").getroot():
                    if context.get("name") == name:
                        return XML.tostring(context, encoding="unicode")
                return "<include/>"
            return "+OK [Success]\n"
        if args[0] == "readlink":
            return "generations/" + self.bundle.name + "\n"
        if args[0] == "stat":
            return "0 755\n"
        return ""


def test_native_activation_closes_first_publishes_pointer_after_fs_reload_then_opens(prepared):
    native = FakeNative(prepared.bundle)
    result = loader.activate(prepared.bundle, prepared.root, prepared.bundle.name, native)
    assert result["runtime_qualified"] is False
    assert native.calls[0] == ("rpc", "htable.sets", "protectedin", "gate", "closed")
    opened = ("rpc", "htable.sets", "protectedin", "gate", "open")
    assert native.calls[-1] == opened
    reload_index = next(i for i, call in enumerate(native.calls) if "reloadxml" in call)
    active_index = native.calls.index(("rpc", "htable.sets", "protectedin", "active", prepared.bundle.name))
    assert reload_index < active_index < len(native.calls) - 1
    assert (prepared.root / "current").resolve() == prepared.bundle
    assert (prepared.root / "active-revision").read_text().strip() == prepared.bundle.name


@pytest.mark.parametrize("failure", ["kamailio", "sha256sum", "fs_cli", "readlink"])
def test_any_native_activation_failure_stays_closed(prepared, failure):
    native = FakeNative(prepared.bundle, fail_at=failure)
    with pytest.raises(RuntimeError):
        loader.activate(prepared.bundle, prepared.root, prepared.bundle.name, native)
    assert native.calls[-1] == ("rpc", "htable.sets", "protectedin", "gate", "closed")
    assert ("rpc", "htable.sets", "protectedin", "gate", "open") not in native.calls
    assert not (prepared.root / "active-revision").exists()


def test_private_profile_retains_refer_and_message_restrictions_and_is_unpublished():
    profile = XML.parse(SOURCE / "deploy/freeswitch/sip_profiles/protected.xml.example")
    params = {node.get("name"): node.get("value") for node in profile.findall(".//settings/param")}
    assert params["disable-transfer"] == "true" and params["enable-chat"] == "false"
    assert params["enable-chat-api-proto"] == "false"
    assert params["tls-only"] == "true" and params["tls-verify-in-subjects"] == "jibsy-sbc.internal"
    ports = (SOURCE / "deploy/voice/ports.yml").read_text()
    assert "5091:" not in ports and "5092:" not in ports


def test_operator_approval_directory_requires_root_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(loader.os, "geteuid", lambda: 501)
    with pytest.raises(PermissionError):
        loader.operator_root(tmp_path)


@pytest.mark.parametrize(
    "values,tls,source_allowed,accepted",
    [
        (["1" * 64], True, True, True),
        ([], True, True, False),
        (["1" * 64, "1" * 64], True, True, False),
        (["2" * 64], True, True, False),
        (["1" * 64], False, True, False),
        (["1" * 64], True, False, False),
        (["short"], True, True, False),
    ],
)
def test_trunk_origin_claim_contract(values, tls, source_allowed, accepted):
    stored = origin_auth.verifier(7, "1" * 64)  # public synthetic test value, never a real token
    assert origin_auth.accepts(stored, 7, values, tls=tls, source_allowed=source_allowed) is accepted


def test_shared_twilio_source_and_verifier_reuse_do_not_authenticate_another_trunk():
    stored = origin_auth.verifier(7, "1" * 64)
    assert not origin_auth.accepts(stored, 8, ["1" * 64], tls=True, source_allowed=True)
    assert not origin_auth.accepts(stored, 7, [stored], tls=True, source_allowed=True)
    assert not origin_auth.accepts(None, 7, ["1" * 64], tls=True, source_allowed=True)


def test_user_private_provisioner_outputs_no_material_to_stdout_and_uses_private_modes(tmp_path, monkeypatch, capsys):
    target = tmp_path / "private"
    monkeypatch.setattr(origin_auth, "private_directory", lambda path: path.mkdir(mode=0o700))
    monkeypatch.setattr(origin_auth.secrets, "token_hex", lambda size: "1" * (size * 2))
    origin_auth.provision(7, "sbc.example.org", 5061, target)
    assert capsys.readouterr().out == ""
    assert (target.stat().st_mode & 0o777) == 0o700
    for path in target.iterdir():
        assert (path.stat().st_mode & 0o777) == 0o600
    assert (target / "origination-url.txt").read_text().startswith("sip:sbc.example.org:5061;transport=tls?")
    assert "1" * 64 not in (target / "private-origination-auth.cfg").read_text()
