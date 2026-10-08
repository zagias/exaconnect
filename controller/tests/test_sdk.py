"""The Python SDK (sdk/python) against the real API (ADR 0013)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk" / "python"))

from exaconnect import ExaConnect, ExaConnectError  # noqa: E402

from .conftest import ADMIN  # noqa: E402
from .test_fabric import PSK  # noqa: E402
from .test_flow import _seed  # noqa: E402


def test_sdk_end_to_end(client, monkeypatch):
    seed = _seed()
    cid = str(seed["customer_id"])
    monkeypatch.delenv("EXACONNECT_API_KEY", raising=False)
    with pytest.raises(ValueError):
        ExaConnect(client=client)
    admin = ExaConnect(client=client, email=ADMIN[0], password=ADMIN[1])
    key = admin.api_keys.create("sdk test", days=1)
    exa = ExaConnect(client=client, api_key=key["token"])
    assert key["token"] not in repr(exa)
    assert exa.me()["role"] == "admin"

    sites = exa.sites.by_name(cid)
    assert {"site-a", "site-b", "pop-miami"} <= set(sites)
    c = exa.circuits.create(
        cid,
        name="AWS sdk",
        kind="cloud",
        provider="aws",
        region="us-east-1",
        a_site_id=sites["site-a"]["id"],
        peer_address="100.64.10.2",
        secondary_peer_address="100.64.10.3",
        peer_asn=64512,
        psk=PSK,
        cloud_prefixes=["10.100.0.0/16"],
        bandwidth_mbps=50,
    )
    assert c["resilient"] and exa.circuits.get(cid, c["id"])["bandwidth_mbps"] == 50
    assert exa.circuits.update(cid, c["id"], bandwidth_mbps=80)["bandwidth_mbps"] == 80
    with pytest.raises(ExaConnectError) as e:
        exa.circuits.update(cid, c["id"], bandwidth_mbps=5000)
    assert e.value.status == 422

    with pytest.raises(ExaConnectError) as e:
        exa.circuits.create(cid, name="bad", kind="cloud", provider="aws", peer_address="nope", bandwidth_mbps=5)
    assert e.value.status == 400 and "public address" in e.value.detail

    exa.internet.set_mode(cid, sites["site-b"]["id"], "local")
    rule = exa.internet.create_rule(cid, "deny", protocol="icmp", description="sdk")
    assert exa.internet.update_rule(cid, rule["id"], enabled=False)["enabled"] is False
    fwd = exa.internet.create_forward(cid, "tcp", 8443, sites["site-a"]["id"], "192.168.10.10")
    view = exa.internet.get(cid)
    assert [r["id"] for r in view["rules"]] == [rule["id"]] and view["forwards"][0]["id"] == fwd["id"]
    exa.internet.delete_forward(cid, fwd["id"])
    exa.internet.delete_rule(cid, rule["id"])

    order = exa.orders.draft(cid, "Send Kingston's internet straight out", engine="rules")
    assert order["problems"] == [] and exa.orders.confirm(cid, order["id"])["status"] == "done"
    assert exa.partners.get("aws")["kind"] == "cloud"
    assert exa.encryption.report(cid)["summary"]["total"] >= 2
    assert isinstance(exa.decisions.list(limit=5), list)
    assert "link" in exa.metering.settlement_csv(hours=1).splitlines()[0].lower()

    exa.circuits.delete(cid, c["id"])
    assert all(x["id"] != c["id"] for x in exa.circuits.list(cid))
    admin.api_keys.revoke(key["id"])
    with pytest.raises(ExaConnectError) as e:
        exa.me()
    assert e.value.status == 401 and "revoked" in e.value.detail
