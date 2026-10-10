"""Organisation set-up (ADR 0043): a new organisation in one step, its owner,
company details, the shared locations list kept in step with Connect, phones
and Jibsy, connecting a location, the checklist, and who may do what."""

from __future__ import annotations

from exaconnect_controller import db

API = "/api/v1"
PASSWORD = "a long enough password"


def _new_org(client, admin_headers, name="Coral Credit Union", products=("connect", "commai")) -> dict:
    r = client.post(
        f"{API}/admin/organisations",
        json={"name": name, "products": list(products), "owner_email": f"owner@{name.split()[0].lower()}.example"},
        headers=admin_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _join(client, invite_url: str, name="Marlene") -> tuple[dict, dict]:
    token = invite_url.rsplit("/", 1)[-1]
    client.cookies.clear()
    r = client.post(f"{API}/invites/{token}/accept", json={"password": PASSWORD, "name": name})
    assert r.status_code == 200, r.text
    client.cookies.clear()
    return r.json(), {"Authorization": f"Bearer {r.json()['token']}"}


def test_new_organisation_owner_and_checklist(client, admin_headers):
    org = _new_org(client, admin_headers)
    cid = org["id"]
    token = org["invite"]["url"].rsplit("/", 1)[-1]
    # The invitation says what the person becomes: the owner.
    assert client.get(f"{API}/invites/{token}").json()["role"] == "owner"
    joined, owner = _join(client, org["invite"]["url"])
    assert joined["role"] == "owner"

    setup = client.get(f"{API}/orgs/{cid}/setup", headers=owner).json()
    assert [s["id"] for s in setup["steps"]] == ["company", "locations", "people", "connect", "jibsy"]
    assert setup["done"] == 0 and not setup["complete"]

    r = client.patch(
        f"{API}/orgs/{cid}/company",
        json={"country": "tt", "timezone": "America/Port_of_Spain", "address": "22 Frederick Street"},
        headers=owner,
    )
    assert r.status_code == 200, r.text
    assert r.json()["country"] == "TT"
    bad = client.patch(f"{API}/orgs/{cid}/company", json={"timezone": "Mars/Olympus"}, headers=owner)
    assert bad.status_code == 422

    r = client.post(
        f"{API}/orgs/{cid}/locations",
        json={"name": "Head office", "address_line1": "22 Frederick Street", "city": "Port of Spain", "country": "TT"},
        headers=owner,
    )
    assert r.status_code == 201, r.text
    lid = r.json()["id"]
    dup = client.post(f"{API}/orgs/{cid}/locations", json={"name": "head office"}, headers=owner)
    assert dup.status_code == 409

    # The same location stands behind Jibsy's opening hours and the phone site.
    loc = client.get(f"{API}/orgs/{cid}/locations", headers=owner).json()["locations"][0]
    assert loc["hours"] is not None and loc["phone"] is not None and loc["connect"] is None
    with db.tx() as conn:
        assert (
            conn.execute("SELECT address_line1 FROM voice_sites WHERE org_location_id = %s", (lid,)).fetchone()[
                "address_line1"
            ]
            == "22 Frederick Street"
        )

    # Renaming and moving it changes the others too.
    r = client.put(
        f"{API}/orgs/{cid}/locations/{lid}",
        json={
            "name": "Head Office",
            "address_line1": "1 Independence Square",
            "city": "Port of Spain",
            "country": "TT",
        },
        headers=owner,
    )
    assert r.status_code == 200, r.text
    with db.tx() as conn:
        v = conn.execute("SELECT name, address_line1 FROM voice_sites WHERE org_location_id = %s", (lid,)).fetchone()
        c = conn.execute("SELECT name, address FROM commai_locations WHERE org_location_id = %s", (lid,)).fetchone()
    assert v == {"name": "Head Office", "address_line1": "1 Independence Square"}
    assert c["name"] == "Head Office" and c["address"].startswith("1 Independence Square")

    # Connecting it makes the Connect site with the network numbers chosen here.
    r = client.post(
        f"{API}/orgs/{cid}/locations/{lid}/connect",
        json={
            "links": [
                {"carrier": "Digicel", "underlay_type": "fibre", "commit_mbps": 200},
                {"carrier": "Flow", "underlay_type": "broadband", "commit_mbps": 100},
                {"carrier": "Starlink", "underlay_type": "leo", "commit_mbps": 50},
            ],
            "lan_prefixes": ["192.168.50.0/24"],
        },
        headers=owner,
    )
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["name"] == "head-office" and out["asn"] == 65001 and out["overlay_host"] == 11
    assert out["install"]["token"] and out["hub_ready"] is False
    with db.tx() as conn:
        links = conn.execute(
            "SELECT path, underlay_interface FROM links WHERE site_id = %s ORDER BY path", (out["site_id"],)
        ).fetchall()
    assert [(lk["path"], lk["underlay_interface"]) for lk in links] == [
        ("carrier-a", "eth1"),
        ("carrier-b", "eth2"),
        ("sat", "eth3"),
    ]
    again = client.post(
        f"{API}/orgs/{cid}/locations/{lid}/connect",
        json={"links": [{"carrier": "Digicel"}]},
        headers=owner,
    )
    assert again.status_code == 409
    # A location with a Connect site isn't deleted by accident.
    assert client.delete(f"{API}/orgs/{cid}/locations/{lid}", headers=owner).status_code == 409
    code = client.post(f"{API}/orgs/{cid}/sites/{out['site_id']}/install-code", headers=owner)
    assert code.status_code == 201 and code.json()["token"] != out["install"]["token"]

    client.post(f"{API}/orgs/{cid}/invites", json={"email": "agent@coral.example"}, headers=owner)
    setup = client.get(f"{API}/orgs/{cid}/setup", headers=owner).json()
    done = {s["id"]: s["done"] for s in setup["steps"]}
    assert done == {"company": True, "locations": True, "people": True, "connect": False, "jibsy": False}

    # Hiding the reminder marks set-up finished; it can come back.
    assert client.post(f"{API}/orgs/{cid}/setup", json={"done": True}, headers=owner).json()["complete"] is True
    assert client.post(f"{API}/orgs/{cid}/setup", json={"done": False}, headers=owner).json()["complete"] is False

    # ExaCarib sees the organisation with its owner and progress.
    rows = client.get(f"{API}/admin/organisations", headers=admin_headers).json()
    row = next(r for r in rows if r["id"] == cid)
    assert row["owners"] == "owner@coral.example" and row["setup"]["done"] == 3 and row["sites"] == 1


def test_members_read_and_strangers_cannot(client, admin_headers):
    org = _new_org(client, admin_headers)
    cid = org["id"]
    _, owner = _join(client, org["invite"]["url"])
    inv = client.post(f"{API}/orgs/{cid}/invites", json={"email": "m@coral.example"}, headers=owner).json()
    _, member = _join(client, inv["url"], "Member")
    assert client.get(f"{API}/orgs/{cid}/locations", headers=member).status_code == 200
    assert client.get(f"{API}/orgs/{cid}/setup", headers=member).status_code == 200
    assert client.post(f"{API}/orgs/{cid}/locations", json={"name": "X"}, headers=member).status_code == 403
    assert client.patch(f"{API}/orgs/{cid}/company", json={"name": "Y"}, headers=member).status_code == 403
    # Another organisation's owner can't read or change this one.
    other = _new_org(client, admin_headers, "Reef Insurance")
    _, stranger = _join(client, other["invite"]["url"], "Stranger")
    assert client.get(f"{API}/orgs/{cid}/locations", headers=stranger).status_code == 403
    assert client.post(f"{API}/orgs/{cid}/locations", json={"name": "Z"}, headers=stranger).status_code == 403
    # Only ExaCarib lists or creates organisations.
    assert client.get(f"{API}/admin/organisations", headers=owner).status_code == 403


def test_existing_records_join_the_shared_list(client, admin_headers):
    """Sites, phone sites and Jibsy locations made before (or elsewhere) are joined to one location by name."""
    org = _new_org(client, admin_headers, "Old Bank", products=("connect", "commai"))
    cid = org["id"]
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO sites (customer_id, name, kind, location, timezone, asn, overlay_host)
               VALUES (%s, 'site-a', 'site', 'Kingston', 'America/Jamaica', 65001, 11),
                      (%s, 'pop-x', 'pop', 'Miami', 'America/New_York', 65000, 1)""",
            (cid, cid),
        )
        conn.execute(
            "INSERT INTO commai_locations (customer_id, name, country, address) VALUES (%s, 'Kingston', 'JM', '')",
            (cid,),
        )
    rows = client.get(f"{API}/orgs/{cid}/locations", headers=admin_headers).json()
    assert [r["name"] for r in rows["locations"]] == ["Kingston"]
    k = rows["locations"][0]
    assert k["connect"]["name"] == "site-a" and k["hours"] is not None and k["timezone"] == "America/Jamaica"
    assert rows["hub_ready"] is True
    # A Jibsy-only organisation's checklist has no Connect step.
    j = _new_org(client, admin_headers, "Jibsy Only", products=("commai",))
    ids = [s["id"] for s in client.get(f"{API}/orgs/{j['id']}/setup", headers=admin_headers).json()["steps"]]
    assert "connect" not in ids and "jibsy" in ids
