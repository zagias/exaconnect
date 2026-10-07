"""Regional hosting (ADR 0025): a region switches on only when every dependency
has a provider in that region recorded; home region and data location."""

from __future__ import annotations

from .commai_helpers import base, business

G = "/api/v1/commai/golive"
R = "/api/v1/commai/regions"


def _base_criteria(client, admin_headers, key: str) -> None:
    for c in ("tested", "security-review", "operations"):
        r = client.put(f"{G}/region/{key}/criteria/{c}", json={"met": True, "evidence": "run 7"}, headers=admin_headers)
        assert r.status_code == 200, r.text


def test_region_needs_every_dependency(client, admin_headers):
    b = business(client, "Region Bank", people=("owner",))
    rows = {r["key"]: r for r in client.get(R, headers=admin_headers).json()}
    assert set(rows) >= {"primary", "caribbean", "us-east", "eu-west"}
    assert all(r["status"] == "off" for r in rows.values())
    assert set(rows["eu-west"]["missing"]) == {"database", "backups", "llm", "channels", "speech"}

    _base_criteria(client, admin_headers, "eu-west")
    for dep, prov in (
        ("database", "Managed Postgres"),
        ("backups", "Object store"),
        ("llm", "Model host"),
        ("channels", "Messaging provider"),
    ):
        r = client.put(
            f"{R}/eu-west/dependencies/{dep}",
            json={"provider": prov, "provider_region": "eu-west-1"},
            headers=admin_headers,
        )
        assert r.status_code == 200, r.text
    assert (
        client.put(  # must name the provider's region
            f"{R}/eu-west/dependencies/speech",
            json={"provider": "Speech", "provider_region": ""},
            headers=admin_headers,
        ).status_code
        == 422
    )

    # Marking the speech criterion met by hand doesn't count without a provider.
    r = client.put(
        f"{G}/region/eu-west/criteria/dependency-speech",
        json={"met": True, "evidence": "trust me"},
        headers=admin_headers,
    )
    crit = {c["criterion"]: c for c in r.json()["criteria"]}
    assert crit["dependency-speech"]["met"] is False and "Refused" in crit["dependency-speech"]["evidence"]
    r = client.put(f"{G}/region/eu-west/status", json={"status": "on"}, headers=admin_headers)
    assert r.status_code == 409 and "Speech" in r.json()["detail"]

    # A customer can't be homed there while it is off.
    r = client.put(f"{base(b)}/home-region", json={"region": "eu-west"}, headers=admin_headers)
    assert r.status_code == 409
    assert client.put(f"{base(b)}/home-region", json={"region": "primary"}, headers=b["owner"]["h"]).status_code == 403

    r = client.put(
        f"{R}/eu-west/dependencies/speech",
        json={"provider": "Speech host", "provider_region": "eu-west-1"},
        headers=admin_headers,
    )
    assert r.json()["missing"] == []
    r = client.put(f"{G}/region/eu-west/status", json={"status": "pilot", "pilots": [b["id"]]}, headers=admin_headers)
    assert r.status_code == 200, r.text
    avail = {x["key"]: x["available"] for x in client.get(R, headers=b["owner"]["h"]).json()}
    assert avail["eu-west"] is True and avail["us-east"] is False and avail["primary"] is True

    r = client.put(f"{base(b)}/home-region", json={"region": "eu-west"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    loc = client.get(f"{base(b)}/data-location", headers=b["owner"]["h"]).json()
    assert loc["home_region"] == "eu-west"
    assert {i["dependency"]: i["provider"] for i in loc["items"]}["database"] == "Managed Postgres"
    assert all(i["source"] == "recorded" for i in loc["items"])

    # Removing a provider switches the region off again.
    client.delete(f"{R}/eu-west/dependencies/backups", headers=admin_headers)
    assert client.get(f"{R}/eu-west", headers=admin_headers).json()["status"] == "off"


def test_default_home_region_reports_configuration(client):
    b = business(client, "Home Bank", people=("owner",))
    loc = client.get(f"{base(b)}/data-location", headers=b["owner"]["h"]).json()
    assert loc["home_region"] == "primary"
    assert {i["source"] for i in loc["items"]} == {"configuration"}
    other = business(client, "Other Bank", people=("owner",))
    assert client.get(f"{base(other)}/data-location", headers=b["owner"]["h"]).status_code == 403
