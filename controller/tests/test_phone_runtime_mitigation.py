"""CI runtime proof must reject an overwritten or incomplete PBX mitigation."""

import importlib.util
from pathlib import Path

import pytest

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
        "FreeSWITCH Version 1.10.12-release+git~abc\n", "1.10.12-release+git~abc\nfalse\n", STATUS, profiles()
    )
    assert result["running_version"] == "1.10.12-release+git~abc"
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
