"""One portal, two apps (ADR 0040): owners and admins choose which of the
organisation's apps each person may open, and may ask ExaCarib to add an app."""

from __future__ import annotations

from exaconnect_controller import db

from . import test_memberships_access as _shared
from .test_memberships_access import API, _join, _set_products

# The same two organisations the shared-accounts tests use.
world = _shared.world


def _me(client, h) -> dict:
    r = client.get(f"{API}/auth/me", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def _uid(email: str) -> str:
    with db.tx() as conn:
        return str(conn.execute("SELECT id FROM users WHERE email = %s", (email,)).fetchone()["id"])


def test_people_get_some_or_all_of_the_plan(client, world):
    a, h = world["a"], world["h_a"]
    sam = _join(client, h, a, "sam@a.example", "member")
    assert _me(client, sam)["apps"] == ["connect", "commai"]

    # Connect only: Jibsy is closed to Sam, with a message that says who to ask.
    r = client.put(f"{API}/orgs/{a}/members/{_uid('sam@a.example')}/apps", json={"apps": ["connect"]}, headers=h)
    assert r.status_code == 200, r.text
    me = _me(client, sam)
    assert me["apps"] == ["connect"] and me["products"] == ["connect", "commai"]
    assert client.get(f"{API}/sites", headers=sam).status_code == 200
    r = client.get(f"{API}/commai/customers/{a}/contacts", headers=sam)
    assert r.status_code == 403 and "don't have access to Jibsy" in r.json()["detail"]

    # Jibsy only: the other way round.
    client.put(f"{API}/orgs/{a}/members/{_uid('sam@a.example')}/apps", json={"apps": ["commai"]}, headers=h)
    assert client.get(f"{API}/commai/customers/{a}/contacts", headers=sam).status_code == 200
    r = client.get(f"{API}/sites", headers=sam)
    assert r.status_code == 403 and "don't have access to Connect" in r.json()["detail"]

    # The People page lists each person's apps; the owner, never narrowed, has both.
    r = client.get(f"{API}/orgs/{a}/members", headers=h).json()
    assert r["products"] == ["connect", "commai"]
    apps = {m["email"]: m["apps"] for m in r["members"]}
    assert apps == {"owner@a.example": ["connect", "commai"], "sam@a.example": ["commai"]}

    # Every change is audited.
    with db.tx() as conn:
        rows = conn.execute("SELECT detail FROM audit_log WHERE action = 'org.member.apps' ORDER BY id").fetchall()
    assert [r["detail"]["to"] for r in rows] == [["connect"], ["commai"]]

    # Both again is stored as "everything on the plan".
    client.put(f"{API}/orgs/{a}/members/{_uid('sam@a.example')}/apps", json={"apps": ["commai", "connect"]}, headers=h)
    with db.tx() as conn:
        m = conn.execute("SELECT apps FROM org_memberships WHERE user_id = %s", (_uid("sam@a.example"),)).fetchone()
    assert m["apps"] is None


def test_a_plan_added_later_reaches_everyone_not_narrowed(client, world, admin_headers):
    a, h = world["a"], world["h_a"]
    _set_products(client, admin_headers, a, ["connect"])
    kim = _join(client, h, a, "kim@a.example", "member")
    assert _me(client, kim)["apps"] == ["connect"]
    _set_products(client, admin_headers, a, ["connect", "commai"])
    assert _me(client, kim)["apps"] == ["connect", "commai"]


def test_who_may_change_access(client, world, admin_headers):
    a, h = world["a"], world["h_a"]
    _set_products(client, admin_headers, a, ["connect"])
    adm = _join(client, h, a, "adm@a.example", "admin")
    mem = _join(client, h, a, "mem@a.example", "member")
    owner, mem_id = world["owner_a"], _uid("mem@a.example")
    url = f"{API}/orgs/{a}/members/{{}}/apps"

    # Not yourself, and an admin can't narrow an owner.
    r = client.put(url.format(_uid("adm@a.example")), json={"apps": []}, headers=adm)
    assert r.status_code == 400
    r = client.put(url.format(owner), json={"apps": []}, headers=adm)
    assert r.status_code == 403
    # Members can't change anyone's access.
    r = client.put(url.format(_uid("adm@a.example")), json={"apps": []}, headers=mem)
    assert r.status_code == 403
    # An app that isn't on the plan can't be given.
    r = client.put(url.format(mem_id), json={"apps": ["commai"]}, headers=adm)
    assert r.status_code == 409 and "isn't on your organisation's plan" in r.json()["detail"]
    r = client.put(url.format(mem_id), json={"apps": ["fax"]}, headers=adm)
    assert r.status_code == 422
    # Another organisation's people are out of reach.
    r = client.put(f"{API}/orgs/{world['b']}/members/{world['owner_b']}/apps", json={"apps": []}, headers=h)
    assert r.status_code == 403
    r = client.put(url.format(world["owner_b"]), json={"apps": []}, headers=h)
    assert r.status_code == 404
    # ExaCarib admins may.
    r = client.put(url.format(owner), json={"apps": []}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["apps"] == []


def test_asking_exacarib_to_add_an_app(client, world, admin_headers):
    a, h = world["a"], world["h_a"]
    _set_products(client, admin_headers, a, ["connect"])
    mem = _join(client, h, a, "m2@a.example", "member")

    apps = client.get(f"{API}/orgs/{a}/apps", headers=mem).json()
    assert {x["id"]: x["status"] for x in apps["apps"]} == {"connect": "active", "commai": "off"}
    assert apps["can_manage"] is False
    assert client.post(f"{API}/orgs/{a}/apps/commai/request", headers=mem).status_code == 403
    assert client.post(f"{API}/orgs/{a}/apps/connect/request", headers=h).status_code == 409
    assert client.post(f"{API}/orgs/{a}/apps/fax/request", headers=h).status_code == 404

    r = client.post(f"{API}/orgs/{a}/apps/commai/request", headers=h)
    assert r.status_code == 201, r.text
    got = {x["id"]: x for x in r.json()["apps"]}
    assert got["commai"]["status"] == "requested" and got["commai"]["requested_by"] == "owner@a.example"
    # Asking twice is harmless.
    assert client.post(f"{API}/orgs/{a}/apps/commai/request", headers=h).status_code == 201

    # ExaCarib sees the request; adding the plan answers it.
    queue = client.get(f"{API}/admin/app-requests", headers=admin_headers).json()
    assert [(q["organisation"], q["product"]) for q in queue] == [("Org A", "commai")]
    assert client.get(f"{API}/admin/app-requests", headers=h).status_code == 403
    _set_products(client, admin_headers, a, ["connect", "commai"])
    assert client.get(f"{API}/admin/app-requests", headers=admin_headers).json() == []
    apps = client.get(f"{API}/orgs/{a}/apps", headers=h).json()["apps"]
    assert all(x["status"] == "active" for x in apps)
    # Another organisation's apps are private.
    assert client.get(f"{API}/orgs/{world['b']}/apps", headers=h).status_code == 403


def test_apps_of_unit():
    import pytest
    from fastapi import HTTPException

    from exaconnect_controller.api.deps import User, apps_of, check_product

    assert apps_of(None, None) == ("connect", "commai")
    assert apps_of(["connect"], None) == ("connect",)
    assert apps_of(None, ["commai"]) == ("commai",)
    assert apps_of(["connect"], ["commai"]) == ()
    u = User("u", "e", "customer", "c", None, products=("connect", "commai"), apps=("connect",))
    check_product(u, "connect")
    with pytest.raises(HTTPException) as e:
        check_product(u, "commai")
    assert "don't have access to Jibsy" in e.value.detail
