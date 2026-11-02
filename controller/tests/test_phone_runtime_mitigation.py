"""CI runtime proof must reject an overwritten or incomplete PBX mitigation."""

import datetime as dt
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from exaconnect_controller.commai.api.voice_pbx import PhoneRuntimeIn
from exaconnect_controller.commai.voice import pbx_runtime, phone_worker

PATH = Path(__file__).resolve().parents[2] / "deploy/voice/phone_runtime_check.py"
SPEC = importlib.util.spec_from_file_location("phone_runtime_mitigation", PATH)
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)
STATUS = (
    "internal profile sip:mod_sofia@10.0.0.2:5060 RUNNING (0)\nexternal profile sip:mod_sofia@10.0.0.2:5080 RUNNING (0)"
)


def profiles(param='<param name="disable-transfer" value="true"/>'):
    return {
        f"/etc/freeswitch/sip_profiles/{name}.xml": f'<profile name="{name}"><settings>{param}</settings></profile>'
        for name in ("internal", "external")
    }


def test_unpatched_running_binary_has_direct_disabled_profile_proof():
    result = check.validate(
        "FreeSWITCH Version 1.10.12-release+git~20240802T210227Z~a88d069d6f\n",
        "1.10.12-release+git~20240802T210227Z~a88d069d6f\nfalse\n",
        STATUS,
        profiles(),
    )
    assert result["running_version"] == "1.10.12-release+git~20240802T210227Z~a88d069d6f"
    assert not result["version_secure"] and not result["guard_transfer_enabled"]
    assert result["loaded_profiles"] == [
        {"name": "external", "disable_transfer": True},
        {"name": "internal", "disable_transfer": True},
    ]


@pytest.mark.parametrize(
    "param",
    [
        "",
        '<param name="disable-transfer" value="false"/>',
        '<!-- <param name="disable-transfer" value="true"/> -->',
        '<param name="disable-transfer" value="true"/><param name="disable-transfer" value="false"/>',
    ],
)
def test_stock_boot_overwrite_comments_and_duplicate_parameters_cannot_count_as_mitigation(param):
    with pytest.raises(check.CheckError, match="does not disable"):
        check.validate("1.10.12", "1.10.12\nfalse\n", STATUS, profiles(param))


def test_unknown_binary_stays_unsafe_and_must_disable_transfers():
    result = check.validate("unrecognised version", "unknown\nfalse\n", STATUS, profiles())
    assert result["running_version"] == "unknown" and not result["version_secure"]
    with pytest.raises(check.CheckError, match="reports SIP transfers enabled"):
        check.validate("unrecognised version", "unknown\ntrue\n", STATUS, profiles())


def test_guard_receipt_belongs_to_running_binary_and_all_loaded_profiles_are_present():
    with pytest.raises(check.CheckError, match="does not match"):
        check.validate("1.10.12", "1.11.2\nfalse\n", STATUS, profiles())
    missing = profiles()
    missing.pop("/etc/freeswitch/sip_profiles/external.xml")
    with pytest.raises(check.CheckError, match="missing from"):
        check.validate("1.10.12", "1.10.12\nfalse\n", STATUS, missing)


VERSIONS = json.loads((Path(__file__).parent / "phone_runtime_versions.json").read_text())


@pytest.mark.parametrize("case", VERSIONS)
def test_official_release_grammar_is_identical_in_controller_and_direct_checker(case):
    expected = (case["normalised"], case["secure"])
    assert pbx_runtime.version(case["value"]) == expected
    assert check.version(case["value"]) == expected
    # This also exercises matching guard receipts and the runtime profile gate,
    # not just the parser. No binary, container, provider or network is contacted.
    if "\n" not in case["value"] and "\r" not in case["value"]:
        result = check.validate(case["value"], case["normalised"] + "\nfalse\n", STATUS, profiles())
        assert result["version_secure"] is case["secure"]


@pytest.mark.parametrize("case", VERSIONS)
def test_signed_runtime_receipt_independently_applies_release_grammar(monkeypatch, case):
    class Connection:
        def execute(self, sql, params=None):
            if "INSERT INTO voice_ai_phone_pbx_runtime" in sql:
                self.record = params
            return self

        def fetchone(self):
            return {"nonce": "new"}

    monkeypatch.setenv("EXA_PBX_SECRET", "fictional-pbx-version-test-secret")
    body = PhoneRuntimeIn(
        instance_id="main",
        version=case["value"],
        transfer_enabled=True,
        checked_at=dt.datetime.now(dt.UTC).isoformat(),
        nonce=uuid4(),
    ).model_dump(mode="json")
    signature = hmac.new(
        b"fictional-pbx-version-test-secret",
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode(),
        hashlib.sha256,
    ).hexdigest()
    conn = Connection()
    result = phone_worker.pbx_receipt(conn, body, signature)
    assert result["pbx_transfer_ready"] is case["secure"]
    assert conn.record[2] is case["secure"]


def test_oversized_runtime_version_is_rejected_at_both_adapter_and_api_boundaries():
    value = "1.11.2" + " " * 321
    assert pbx_runtime.version(value) == check.version(value) == ("unknown", False)
    with pytest.raises(ValueError):
        PhoneRuntimeIn(
            instance_id="main",
            version=value,
            transfer_enabled=True,
            checked_at=dt.datetime.now(dt.UTC).isoformat(),
            nonce=uuid4(),
        )


CLI_VERSIONS = json.loads((Path(__file__).parent / "phone_runtime_cli_versions.json").read_text())


@pytest.mark.parametrize("case", CLI_VERSIONS)
def test_actual_cli_framing_is_normalised_only_at_its_command_boundary(case):
    record = check.cli_record(case["stdout"], fs_cli=case["command"] == "fs_cli")
    assert check.version(record) == (case["normalised"], case["secure"])
    assert pbx_runtime.version(case["stdout"]) == ("unknown", False)
    if case["command"] == "fs_cli":
        result = check.validate(case["stdout"], case["normalised"] + "\nfalse\n", STATUS, profiles())
        assert result["version_secure"] is case["secure"]


def test_raw_version_diagnostics_are_bounded_escaped_and_never_print_unexpected_output():
    raw = CLI_VERSIONS[0]["stdout"]
    assert check.version_diagnostic(raw)["stdout"] == raw
    for raw in ("unexpected secret body", "<xml>private</xml>", "FreeSWITCH Version 1.11.2\nprivate", "x" * 1000):
        assert check.version_diagnostic(raw)["stdout"] == "suppressed: unexpected version output"


def test_failed_version_command_keeps_safe_diagnostics_but_cannot_establish_a_version(monkeypatch):
    def run(command, **kwargs):
        assert command == ["docker", "exec", "disposable", "fs_cli", "-x", "version"]
        assert kwargs["timeout"] == 10 and kwargs["check"] is False
        return SimpleNamespace(returncode=1, stdout=CLI_VERSIONS[6]["stdout"], stderr="fictional private error body")

    monkeypatch.setattr(check.subprocess, "run", run)
    raw, evidence = check.version_command("disposable", "fs_cli", "-x", "version")
    assert raw is None and evidence["exit_code"] == 1
    assert evidence["stdout"] == CLI_VERSIONS[6]["stdout"]
    assert "fictional private error body" not in json.dumps(evidence)
