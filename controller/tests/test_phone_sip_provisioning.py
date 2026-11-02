"""Offline provisioning review cannot connect or broaden the SIP trust boundary."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[2] / "deploy/livekit/agent/configure_sip.py"
SPEC = importlib.util.spec_from_file_location("phone_sip_config", PATH)
config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(config)
CID = "11111111-1111-1111-1111-111111111111"
NUMBERS = [{"customer_id": CID, "e164": "+18685550100"}]


@pytest.mark.parametrize("address", ["0.0.0.0/0", "10.0.0.0/24", "::/0", "224.0.0.1", "0.0.0.0"])
def test_sip_provisioning_requires_exact_unicast_freeswitch_address(address):
    with pytest.raises(ValueError):
        config.plan(NUMBERS, address)


def test_duplicate_did_and_tenant_prefix_collision_fail_before_any_sdk_use():
    with pytest.raises(ValueError, match="unique"):
        config.plan(NUMBERS + NUMBERS, "10.0.0.2")
    with pytest.raises(ValueError, match="prefixes collide"):
        config.plan(
            NUMBERS + [{"customer_id": "11111111-1111-2222-2222-222222222222", "e164": "+18685550101"}], "10.0.0.2"
        )


def test_dry_run_works_without_livekit_sdk_credentials_or_network(tmp_path):
    numbers = tmp_path / "owned-numbers.json"
    numbers.write_text(json.dumps(NUMBERS))
    result = subprocess.run(
        [sys.executable, str(PATH), "--numbers-file", str(numbers), "--freeswitch-address", "10.0.0.2"],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    review = json.loads(result.stdout)
    assert review["mode"] == "review_only"
    trunk = review["resources"][0]
    assert trunk["allowed_addresses"] == ["10.0.0.2/32"] and trunk["numbers"] == ["+18685550100"]
    assert trunk["attributes"]["exa.customer_id"] == CID
    assert trunk["agent_name"] == "exacarib-phone" and trunk["max_call_duration_s"] == 1800
