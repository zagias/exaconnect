"""Shared accounts (ADR 0023), enforcement: tenant isolation across a broad
sample of Connect and CommAI endpoints, viewers are read-only everywhere,
switching organisation changes what you see, product plans, and people
provisioned by a business's directory (SCIM)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from exaconnect_controller import db
from exaconnect_controller.security import hash_password
from exaconnect_controller.seed import CUSTOMER, seed_lab

from .commai_helpers import PASSWORD

API = "/api/v1"
SCIM = "/api/v1/scim/v2"
USER = "urn:ietf:params:scim:schemas:core:2.0:User"
PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
NEW_PASSWORD = "a brand new password"


def _person(cid: str | None, email: str, role: str = "customer", carrier_id=None) -> str:
    with db.tx() as conn:
        return str(
            conn.execute(
                "INSERT INTO users (email, password_hash, role, customer_id, carrier_id) VALUES (%s, %s, %s, %s, %s)"
                " RETURNING id",
                (email, hash_password(PASSWORD), role, cid, carrier_id),
            ).fetchone()["id"]
        )


def _login(client, email: str, password: str = PASSWORD) -> dict:
    client.cookies.clear()
    r = client.post(f"{API}/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    client.cookies.clear()
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _join(client, inviter: dict, cid: str, email: str, role: str) -> dict:
    """A new person who accepted an invitation with this role. Returns their headers."""
    r = client.post(f"{API}/orgs/{cid}/invites", json={"email": email, "role": role}, headers=inviter)
    assert r.status_code == 201, r.text
    client.cookies.clear()
    r = client.post(f"{API}/invites/{r.json()['token']}/accept", json={"password": NEW_PASSWORD})
    assert r.status_code == 200, r.text
    client.cookies.clear()
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def world(client, admin_headers):
    """Org B is the lab's demo organisation (sites, links, classes). Org A is a
    second, empty organisation. Each has an owner; B's data must never reach A."""
    with db.tx() as conn:
        seeded = seed_lab(conn)
        b = seeded["customer_id"]
        sites = conn.execute("SELECT id, name FROM sites WHERE customer_id = %s", (b,)).fetchall()
        links = conn.execute(
            "SELECT l.id FROM links l JOIN sites s ON s.id = l.site_id WHERE s.customer_id = %s", (b,)
        ).fetchall()
    r = client.post(f"{API}/customers", json={"name": "Org A"}, headers=admin_headers)
    a = str(r.json()["id"])
    r = client.put(f"{API}/customers/{a}/classes/base", json={"sla": {"max_latency_ms": 250}}, headers=admin_headers)
    assert r.status_code == 200, r.text
    out = {
        "a": a,
        "b": b,
        "b_sites": [str(s["id"]) for s in sites],
        "b_site_names": [s["name"] for s in sites if len(s["name"]) > 5],
        "b_links": [str(lk["id"]) for lk in links],
        "owner_a": _person(a, "owner@a.example"),
        "owner_b": _person(b, "owner@b.example"),
    }
    out["h_a"] = _login(client, "owner@a.example")
    out["h_b"] = _login(client, "owner@b.example")
    # One CommAI contact in each, made through the API.
    for k, h, cid in (("contact_a", out["h_a"], a), ("contact_b", out["h_b"], b)):
        r = client.post(
            f"{API}/commai/customers/{cid}/contacts",
            json={"name": f"Contact {k}", "email": f"{k}@x.example"},
            headers=h,
        )
        assert r.status_code == 201, r.text
        out[k] = str(r.json()["id"])
    return out


def _openapi_paths(client) -> dict:
    app: FastAPI = client.app
    return app.openapi()["paths"]


def _customer_gets(client) -> list[str]:
    """Every GET under /api/v1 whose only path parameter is the organisation."""
    out = []
    for p, ops in _openapi_paths(client).items():
        if "get" in ops and "{customer_id}" in p and p.count("{") == 1:
            out.append(p)
    return out


# ---- tenant isolation -------------------------------------------------------------------


def test_every_organisation_scoped_read_refuses_another_organisation(client, world):
    """A member of org A asks for org B on every organisation-scoped GET: each
    one refuses, and none of B's records come back."""
    paths = _customer_gets(client)
    assert len(paths) >= 50  # a broad sample: Connect, sign-in, people and CommAI
    assert any(p.startswith("/api/v1/commai/") for p in paths)
    assert any(p.startswith("/api/v1/customers/") for p in paths)
    allowed_own = 0
    for p in paths:
        r = client.get(p.replace("{customer_id}", world["b"]), headers=world["h_a"])
        assert r.status_code in (403, 404, 422), (p, r.status_code, r.text[:200])
        assert world["b"] not in r.text, p
        assert world["contact_b"] not in r.text, p
        if r.status_code == 422:
            # A required query parameter is missing; validation ran before the
            # organisation check. Its own organisation gets the same 422, so
            # nothing was read either way.
            assert client.get(p.replace("{customer_id}", world["a"]), headers=world["h_a"]).status_code == 422, p
            continue
        mine = client.get(p.replace("{customer_id}", world["a"]), headers=world["h_a"])
        if mine.status_code == 200:
            allowed_own += 1
    # The refusals are about the organisation, not the endpoint: most of the same
    # reads work for A's own organisation.
    assert allowed_own >= len(paths) * 0.7, allowed_own


def test_lists_never_include_another_organisation(client, world):
    """Lists scoped by the caller, even when asked for org B by query."""
    paths = [p for p, ops in _openapi_paths(client).items() if "get" in ops and "{" not in p]
    # The PBX's own call check signs with a shared secret, not a person (ADR 0033).
    skip = {
        "/api/v1/auth/oidc/start",
        "/api/v1/auth/oidc/callback",
        "/api/v1/ca.pem",
        "/api/v1/commai/internal/voice/authorise",
    }
    checked = 0
    for p in paths:
        if p in skip or p.startswith(SCIM):
            continue
        r = client.get(p, params={"customer_id": world["b"]}, headers=world["h_a"])
        assert r.status_code < 500, (p, r.text[:200])
        if r.status_code == 200:
            checked += 1
            assert world["b"] not in r.text, p
            assert CUSTOMER not in r.text, p
            for name in world["b_site_names"]:
                assert name not in r.text, (p, name)
    assert checked >= 10


def test_another_organisations_objects_are_out_of_reach(client, world):
    h, b = world["h_a"], world["b"]
    for sid in world["b_sites"]:
        assert client.get(f"{API}/sites/{sid}", headers=h).status_code in (403, 404)
        assert client.get(f"{API}/sites/{sid}/metrics", headers=h).status_code in (403, 404)
        assert client.post(f"{API}/sites/{sid}/storm", json={"on": True}, headers=h).status_code in (403, 404)
    for lid in world["b_links"][:3]:
        r = client.get(f"{API}/metering/links/{lid}/samples", headers=h)
        assert r.status_code in (403, 404), r.text
    r = client.get(f"{API}/inventory", params={"customer_id": b}, headers=h)
    assert r.status_code in (200, 403) and b not in r.text and CUSTOMER not in r.text
    # CommAI records: by B's path refused; by A's path, B's contact isn't there.
    assert client.get(f"{API}/commai/customers/{b}/contacts/{world['contact_b']}", headers=h).status_code == 403
    assert (
        client.get(f"{API}/commai/customers/{world['a']}/contacts/{world['contact_b']}", headers=h).status_code == 404
    )
    # Writes into B, as A's owner.
    sla = {"sla": {"max_latency_ms": 200}}
    writes = [
        ("put", f"{API}/customers/{b}/classes/x", sla),
        ("delete", f"{API}/customers/{b}/classes/voice", None),
        ("patch", f"{API}/customers/{b}/settings", {"shadow_mode": True}),
        ("post", f"{API}/customers/{b}/storm", {"on": True}),
        ("post", f"{API}/orgs/{b}/invites", {"email": "spy@a.example"}),
        ("post", f"{API}/orgs/{b}/transfer-ownership", {"user_id": world["owner_a"]}),
        ("patch", f"{API}/orgs/{b}/members/{world['owner_b']}", {"role": "viewer"}),
        ("delete", f"{API}/orgs/{b}/members/{world['owner_b']}", None),
        ("post", f"{API}/commai/customers/{b}/contacts", {"name": "Spy"}),
        ("post", f"{API}/commai/customers/{b}/teams", {"name": "Spies"}),
        ("post", f"{API}/customers/{b}/scim-tokens", {"name": "x"}),
        ("put", f"{API}/customers/{b}/products", {"products": ["connect"]}),
    ]
    for method, path, body in writes:
        r = client.request(method, path, json=body, headers=h)
        assert r.status_code in (403, 404), (method, path, r.status_code, r.text[:200])
    with db.tx() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM org_memberships WHERE customer_id = %s", (b,)).fetchone()["n"] == 1
        )
        assert conn.execute("SELECT shadow_mode FROM customers WHERE id = %s", (b,)).fetchone()["shadow_mode"] is False


def test_switching_organisation_changes_what_you_see(client, world):
    # The owner of A is invited into B as a member.
    r = client.post(f"{API}/orgs/{world['b']}/invites", json={"email": "owner@a.example"}, headers=world["h_b"])
    tok = r.json()["token"]
    h = _login(client, "owner@a.example")
    assert client.post(f"{API}/invites/{tok}/accept", json={}, headers=h).status_code == 200
    h = _login(client, "owner@a.example")  # a new session starts in the primary organisation

    def sites() -> set[str]:
        return {s["name"] for s in client.get(f"{API}/sites", headers=h).json()}

    def contacts(cid) -> int:
        r = client.get(f"{API}/commai/customers/{cid}/contacts", headers=h)
        return r.status_code

    assert sites() == set()
    assert contacts(world["a"]) == 200 and contacts(world["b"]) == 403
    me = client.get(f"{API}/auth/me", headers=h).json()
    assert me["customer_id"] == world["a"] and me["org_role"] == "owner"
    assert {m["name"]: m["role"] for m in me["memberships"]} == {"Org A": "owner", CUSTOMER: "member"}
    assert all(m["products"] == ["connect", "commai"] for m in me["memberships"])

    r = client.post(f"{API}/auth/organisation", json={"customer_id": world["b"]}, headers=h)
    assert r.status_code == 200 and r.json() == {"customer_id": world["b"], "name": CUSTOMER, "role": "member"}
    assert sites() and sites() <= set(n["name"] for n in client.get(f"{API}/sites", headers=world["h_b"]).json())
    assert contacts(world["b"]) == 200 and contacts(world["a"]) == 403
    me = client.get(f"{API}/auth/me", headers=h).json()
    assert me["customer_id"] == world["b"] and me["org_role"] == "member" and me["organisation"] == CUSTOMER
    # As a member in B they make changes but don't manage people or settings.
    assert client.post(f"{API}/customers/{world['b']}/storm", json={"on": False}, headers=h).status_code == 200
    assert (
        client.patch(f"{API}/customers/{world['b']}/settings", json={"shadow_mode": True}, headers=h).status_code == 403
    )
    assert client.get(f"{API}/orgs/{world['b']}/invites", headers=h).status_code == 403
    # Another session for the same person is unaffected by this switch.
    h2 = _login(client, "owner@a.example")
    assert client.get(f"{API}/auth/me", headers=h2).json()["customer_id"] == world["a"]


def test_cookie_session_switch_needs_the_portal_header(client, world):
    client.cookies.clear()
    r = client.post(f"{API}/auth/login", json={"email": "owner@a.example", "password": PASSWORD})
    assert r.status_code == 200
    # The test server is plain HTTP, so the Secure cookie is set by hand.
    client.cookies.clear()
    client.cookies.set("exa_session", r.json()["token"])
    # A cross-site form can't switch someone's organisation.
    r = client.post(f"{API}/auth/organisation", json={"customer_id": world["a"]})
    assert r.status_code == 403
    r = client.post(
        f"{API}/auth/organisation", json={"customer_id": world["a"]}, headers={"X-Requested-With": "exa-portal"}
    )
    assert r.status_code == 200
    client.cookies.clear()


# ---- viewers -------------------------------------------------------------------------------


def test_viewer_cannot_write_anywhere(client, world):
    b = world["b"]
    h = _join(client, world["h_b"], b, "viewer@b.example", "viewer")
    reads = [
        f"{API}/sites",
        f"{API}/classes",
        f"{API}/overview",
        f"{API}/decisions",
        f"{API}/customers/{b}/settings",
        f"{API}/orgs/{b}/members",
        f"{API}/commai/customers/{b}/contacts",
        f"{API}/commai/customers/{b}/conversations",
    ]
    for p in reads:
        assert client.get(p, headers=h).status_code == 200, p
    sid = world["b_sites"][0]
    writes = [
        ("put", f"{API}/customers/{b}/classes/x", {"sla": {"max_latency_ms": 200}}),
        ("delete", f"{API}/customers/{b}/classes/voice", None),
        ("patch", f"{API}/customers/{b}/settings", {"shadow_mode": True}),
        ("post", f"{API}/customers/{b}/storm", {"on": True}),
        ("post", f"{API}/sites/{sid}/storm", {"on": True}),
        ("post", f"{API}/customers/{b}/rules", {"name": "x"}),
        ("post", f"{API}/ai/plans/00000000-0000-0000-0000-000000000000/apply", None),
        ("post", f"{API}/commai/customers/{b}/contacts", {"name": "New"}),
        ("patch", f"{API}/commai/customers/{b}/contacts/{world['contact_b']}", {"name": "Changed"}),
        ("post", f"{API}/commai/customers/{b}/teams", {"name": "Desk"}),
        ("post", f"{API}/orgs/{b}/invites", {"email": "x@b.example"}),
        ("post", f"{API}/customers/{b}/scim-tokens", {"name": "x"}),
    ]
    for method, path, body in writes:
        r = client.request(method, path, json=body, headers=h)
        assert r.status_code == 403, (method, path, r.status_code, r.text[:200])
        assert "view-only" in r.json()["detail"], path
    # Their own sign-in is still theirs: password, keys, switching and leaving.
    assert client.post(f"{API}/auth/api-keys", json={"name": "mine"}, headers=h).status_code == 201
    r = client.post(f"{API}/auth/password", json={"current": NEW_PASSWORD, "new": "another long password"}, headers=h)
    assert r.status_code == 204
    assert client.post(f"{API}/orgs/{b}/leave", headers=h).status_code == 204


def test_promoting_a_viewer_takes_effect_at_once(client, world):
    b = world["b"]
    h = _join(client, world["h_b"], b, "v2@b.example", "viewer")
    sla = {"sla": {"max_latency_ms": 200}}
    assert client.put(f"{API}/customers/{b}/classes/promo", json=sla, headers=h).status_code == 403
    with db.tx() as conn:
        uid = conn.execute("SELECT id FROM users WHERE email = 'v2@b.example'").fetchone()["id"]
    r = client.patch(f"{API}/orgs/{b}/members/{uid}", json={"role": "member"}, headers=world["h_b"])
    assert r.status_code == 200
    assert client.put(f"{API}/customers/{b}/classes/promo", json=sla, headers=h).status_code == 200
    # And demoting back stops writes on the very next request.
    client.patch(f"{API}/orgs/{b}/members/{uid}", json={"role": "viewer"}, headers=world["h_b"])
    assert client.delete(f"{API}/customers/{b}/classes/promo", headers=h).status_code == 403


# ---- product plans ---------------------------------------------------------------------------


def _set_products(client, admin_headers, cid: str, products: list[str]) -> None:
    r = client.put(f"{API}/customers/{cid}/products", json={"products": products}, headers=admin_headers)
    assert r.status_code == 200, r.text


def test_connect_only_and_commai_only_organisations(client, world, admin_headers):
    a, b = world["a"], world["b"]
    _set_products(client, admin_headers, a, ["connect"])
    _set_products(client, admin_headers, b, ["commai"])

    # A holds Connect only.
    for p in (f"{API}/classes", f"{API}/sites", f"{API}/customers/{a}/settings", f"{API}/decisions"):
        assert client.get(p, headers=world["h_a"]).status_code == 200, p
    for p in (f"{API}/commai/customers/{a}/contacts", f"{API}/commai/customers/{a}/conversations"):
        r = client.get(p, headers=world["h_a"])
        assert r.status_code == 403 and "CommAI plan" in r.json()["detail"], p
    r = client.post(f"{API}/commai/customers/{a}/contacts", json={"name": "x"}, headers=world["h_a"])
    assert r.status_code == 403 and "CommAI plan" in r.json()["detail"]

    # B holds CommAI only.
    assert client.get(f"{API}/commai/customers/{b}/contacts", headers=world["h_b"]).status_code == 200
    for p in (
        f"{API}/classes",
        f"{API}/sites",
        f"{API}/overview",
        f"{API}/customers/{b}/settings",
        f"{API}/metering/links",
        f"{API}/customers/{b}/circuits",
    ):
        r = client.get(p, headers=world["h_b"])
        assert r.status_code == 403 and "Connect plan" in r.json()["detail"], p
    r = client.post(f"{API}/customers/{b}/storm", json={"on": True}, headers=world["h_b"])
    assert r.status_code == 403 and "Connect plan" in r.json()["detail"]

    # Shared by both plans: sign-in, people, and which organisations you act for.
    for h, cid in ((world["h_a"], a), (world["h_b"], b)):
        assert client.get(f"{API}/auth/me", headers=h).status_code == 200
        assert client.get(f"{API}/orgs/{cid}/members", headers=h).status_code == 200
        assert client.get(f"{API}/customers/mine", headers=h).status_code == 200
    assert client.get(f"{API}/auth/me", headers=world["h_a"]).json()["products"] == ["connect"]
    assert client.get(f"{API}/auth/me", headers=world["h_b"]).json()["products"] == ["commai"]

    # API keys are held to the plan of the organisation they act in.
    r = client.post(f"{API}/auth/api-keys", json={"name": "k"}, headers=world["h_a"])
    hk = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.get(f"{API}/classes", headers=hk).status_code == 200
    assert client.get(f"{API}/commai/customers/{a}/contacts", headers=hk).status_code == 403

    # ExaCarib admins aren't limited by plan.
    assert client.get(f"{API}/commai/customers/{a}/contacts", headers=admin_headers).status_code == 200
    assert client.get(f"{API}/customers/{b}/settings", headers=admin_headers).status_code == 200


def test_switching_between_plans(client, world, admin_headers):
    """A person in a Connect-only and a CommAI-only organisation gets each plan
    only while acting for the organisation that holds it."""
    a, b = world["a"], world["b"]
    _set_products(client, admin_headers, a, ["connect"])
    _set_products(client, admin_headers, b, ["commai"])
    r = client.post(f"{API}/orgs/{b}/invites", json={"email": "owner@a.example"}, headers=world["h_b"])
    h = world["h_a"]
    assert client.post(f"{API}/invites/{r.json()['token']}/accept", json={}, headers=h).status_code == 200
    me = client.get(f"{API}/auth/me", headers=h).json()
    assert {m["name"]: m["products"] for m in me["memberships"]} == {"Org A": ["connect"], CUSTOMER: ["commai"]}
    # Accepting moved the session into B: CommAI yes, Connect no.
    assert client.get(f"{API}/commai/customers/{b}/contacts", headers=h).status_code == 200
    assert client.get(f"{API}/classes", headers=h).status_code == 403
    client.post(f"{API}/auth/organisation", json={"customer_id": a}, headers=h)
    assert client.get(f"{API}/classes", headers=h).status_code == 200
    assert client.get(f"{API}/commai/customers/{a}/contacts", headers=h).status_code == 403


def test_products_are_set_by_exacarib_admins_only(client, world, admin_headers):
    a = world["a"]
    r = client.put(f"{API}/customers/{a}/products", json={"products": ["connect"]}, headers=world["h_a"])
    assert r.status_code == 403
    r = client.put(f"{API}/customers/{a}/products", json={"products": ["fax"]}, headers=admin_headers)
    assert r.status_code == 422
    r = client.put(f"{API}/customers/not-a-uuid/products", json={"products": []}, headers=admin_headers)
    assert r.status_code == 404
    _set_products(client, admin_headers, a, ["commai", "connect", "commai"])
    with db.tx() as conn:
        assert conn.execute("SELECT products FROM customers WHERE id = %s", (a,)).fetchone()["products"] == [
            "commai",
            "connect",
        ]
        row = conn.execute("SELECT detail FROM audit_log WHERE action = 'customer.products'").fetchone()
    assert row["detail"]["to"] == ["commai", "connect"]
    # An organisation from before plans (NULL) holds both.
    with db.tx() as conn:
        conn.execute("UPDATE customers SET products = NULL WHERE id = %s", (a,))
    assert client.get(f"{API}/auth/me", headers=world["h_a"]).json()["products"] == ["connect", "commai"]
    assert client.get(f"{API}/commai/customers/{a}/contacts", headers=world["h_a"]).status_code == 200


def test_carrier_accounts_keep_their_view(client, world, admin_headers):
    with db.tx() as conn:
        carrier = conn.execute("SELECT id FROM carriers ORDER BY name LIMIT 1").fetchone()["id"]
    _person(None, "noc@carrier.example", "carrier", carrier)
    h = _login(client, "noc@carrier.example")
    _set_products(client, admin_headers, world["b"], ["commai"])
    r = client.get(f"{API}/metering/links", headers=h)
    assert r.status_code == 200 and len(r.json()) > 0
    me = client.get(f"{API}/auth/me", headers=h).json()
    assert me["memberships"] == [] and me["products"] is None and me["org_role"] is None
    # Carriers don't join organisations and can't switch into one.
    assert client.post(f"{API}/auth/organisation", json={"customer_id": world["b"]}, headers=h).status_code == 400
    r = client.post(f"{API}/orgs/{world['b']}/invites", json={"email": "noc@carrier.example"}, headers=world["h_b"])
    assert r.status_code == 409


def test_check_product_unit():
    from fastapi import HTTPException

    from exaconnect_controller.api.deps import User, check_product, products_of, require_product

    assert products_of(None) == ("connect", "commai")
    assert products_of(["commai"]) == ("commai",)
    cust = User("u", "e", "customer", "c", None, products=("connect",))
    check_product(cust, "connect")
    with pytest.raises(HTTPException) as e:
        check_product(cust, "commai")
    assert e.value.status_code == 403
    check_product(User("u", "e", "admin", None, None), "commai")
    check_product(User("u", "e", "carrier", None, "k"), "connect")
    with pytest.raises(ValueError):
        require_product("fax")


# ---- directory-provisioned people (SCIM) ------------------------------------------------------


def _scim_token(client, cid, h) -> dict:
    r = client.post(f"{API}/customers/{cid}/scim-tokens", json={"name": "Entra ID"}, headers=h)
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['token']}", "Content-Type": "application/scim+json"}


def _sign_in_as(email):
    from exaconnect_controller.identity import sessions

    with db.tx() as conn:
        uid = conn.execute("SELECT id FROM users WHERE email = %s", (email,)).fetchone()["id"]
        token, _ = sessions.start(conn, uid, 1, "sso")
    return {"Authorization": f"Bearer {token}"}


def test_scim_provisioned_people_are_unaffected(client, world):
    b = world["b"]
    sh = _scim_token(client, b, world["h_b"])
    body = {"schemas": [USER], "userName": "ana@b.example", "emails": [{"value": "ana@b.example"}], "active": True}
    r = client.post(f"{SCIM}/Users", json=body, headers=sh)
    assert r.status_code == 201, r.text
    ana = r.json()["id"]
    with db.tx() as conn:
        m = conn.execute(
            "SELECT role, managed_by FROM org_memberships WHERE customer_id = %s AND user_id = %s", (b, ana)
        ).fetchone()
    assert m == {"role": "member", "managed_by": "scim"}
    h = _sign_in_as("ana@b.example")
    me = client.get(f"{API}/auth/me", headers=h).json()
    # Same rights as before shared accounts: CommAI work, no network API.
    assert me["scopes"] == ["commai:read", "commai:write", "commai:notes"]
    assert me["memberships"][0]["managed_by"] == "scim" and me["org_role"] == "member"
    assert client.get(f"{API}/commai/customers/{b}/contacts", headers=h).status_code == 200
    assert client.get(f"{API}/sites", headers=h).status_code == 403
    assert client.get(f"{API}/orgs/{b}/members", headers=h).status_code == 200
    assert client.get(f"{API}/orgs/{b}/invites", headers=h).status_code == 403
    # The portal doesn't change or remove them, and they can't leave by hand.
    assert (
        client.patch(f"{API}/orgs/{b}/members/{ana}", json={"role": "viewer"}, headers=world["h_b"]).status_code == 409
    )
    assert client.delete(f"{API}/orgs/{b}/members/{ana}", headers=world["h_b"]).status_code == 409
    assert client.post(f"{API}/orgs/{b}/leave", headers=h).status_code == 409
    # SCIM still lists them, and switching them off still works.
    assert client.get(f"{SCIM}/Users/{ana}", headers=sh).status_code == 200
    r = client.patch(
        f"{SCIM}/Users/{ana}",
        json={"schemas": [PATCH], "Operations": [{"op": "replace", "path": "active", "value": False}]},
        headers=sh,
    )
    assert r.status_code == 200 and r.json()["active"] is False
    assert client.get(f"{API}/auth/me", headers=h).status_code == 401
    # SCIM sees the people whose primary organisation is B, as before; never A's.
    listed = {u["userName"] for u in client.get(f"{SCIM}/Users", headers=sh).json()["Resources"]}
    assert "ana@b.example" in listed and "owner@a.example" not in listed


def test_scim_admin_mapping_moves_the_membership_role(client, world):
    from exaconnect_controller.identity import scim

    b = world["b"]
    sh = _scim_token(client, b, world["h_b"])
    body = {"schemas": [USER], "userName": "bo@b.example", "active": True}
    bo = client.post(f"{SCIM}/Users", json=body, headers=sh).json()["id"]
    with db.tx() as conn:
        conn.execute("UPDATE users SET access_scopes = NULL WHERE id = %s", (bo,))
        scim.refresh_rights(conn, b, bo)
        role = conn.execute(
            "SELECT role FROM org_memberships WHERE customer_id = %s AND user_id = %s", (b, bo)
        ).fetchone()["role"]
    # Without an approved admin group mapping the directory makes them a member.
    assert role == "member"


def test_scim_cannot_take_over_a_member_of_another_organisation(client, world):
    """Someone who joined B by invitation keeps their account when A's directory
    sees the same email (A's directory owns only A's people)."""
    a = world["a"]
    _join(client, world["h_b"], world["b"], "shared@b.example", "member")
    sh = _scim_token(client, a, world["h_a"])
    r = client.post(f"{SCIM}/Users", json={"schemas": [USER], "userName": "shared@b.example"}, headers=sh)
    assert r.status_code == 409


def test_invite_into_a_single_sign_on_domain(client, world):
    """A business that requires single sign-on for its domain: an invited
    newcomer there can't set a password; they sign in through SSO first."""
    a = world["a"]
    with db.tx() as conn:
        c = conn.execute(
            """INSERT INTO sso_connections (customer_id, alias, protocol, display_name, status, require_sso)
               VALUES (%s, 'a-idp', 'oidc', 'Org A sign-in', 'enabled', true) RETURNING id""",
            (a,),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO sso_domains (connection_id, customer_id, domain, status)"
            " VALUES (%s, %s, 'sso-a.example', 'approved')",
            (c, a),
        )
    r = client.post(f"{API}/orgs/{a}/invites", json={"email": "new@sso-a.example"}, headers=world["h_a"])
    tok = r.json()["token"]
    client.cookies.clear()
    shown = client.get(f"{API}/invites/{tok}").json()
    assert shown["sso_required"] is True and shown["has_account"] is False
    r = client.post(f"{API}/invites/{tok}/accept", json={"password": NEW_PASSWORD})
    assert r.status_code == 403 and "single sign-on" in r.json()["detail"]
    with db.tx() as conn:
        assert conn.execute("SELECT 1 FROM users WHERE email = 'new@sso-a.example'").fetchone() is None
        # The invitation wasn't used up by the refusal.
        assert (
            conn.execute("SELECT accepted_at FROM org_invites WHERE email = 'new@sso-a.example'").fetchone()[
                "accepted_at"
            ]
            is None
        )


def test_invitation_notice_goes_to_the_simulated_outbox_without_the_link(client, world):
    r = client.post(f"{API}/orgs/{world['a']}/invites", json={"email": "n@a.example"}, headers=world["h_a"])
    inv = r.json()
    assert inv["emailed"] is False and inv["email_sender"] == "simulated"
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT to_address, body, headers FROM sim_channel_outbox WHERE customer_id = %s", (world["a"],)
        ).fetchall()
    assert len(rows) == 1 and rows[0]["to_address"] == "n@a.example"
    assert inv["token"] not in rows[0]["body"] and inv["token"] not in str(rows[0]["headers"])
    assert "Org A" in rows[0]["body"]


def test_migration_turns_legacy_accounts_into_memberships(client, world):
    """Accounts from before memberships: hand-made people become admins with the
    earliest the owner, directory people follow their scopes, keys keep their
    organisation, and running it again changes nothing."""
    from importlib import resources

    sql = resources.files("exaconnect_controller").joinpath("commai", "sql", "15_memberships.sql").read_text()
    a = world["a"]
    with db.tx() as conn:
        legacy = []
        for email, prov, scopes in (
            ("first@legacy.example", None, None),
            ("second@legacy.example", None, None),
            ("dir@legacy.example", "scim", ["commai:read"]),
        ):
            legacy.append(
                conn.execute(
                    """INSERT INTO users (email, password_hash, role, customer_id, provisioned_by, access_scopes)
                       VALUES (%s, '!', 'customer', %s, %s, %s) RETURNING id""",
                    (email, a, prov, scopes),
                ).fetchone()["id"]
            )
        conn.execute(
            "INSERT INTO api_keys (user_id, name, prefix, token_hash) VALUES (%s, 'old', 'exa_old', 'h-old')",
            (legacy[0],),
        )
        # Wipe the organisation's memberships, as if they predate the table.
        conn.execute("DELETE FROM org_memberships WHERE customer_id = %s", (a,))
        conn.execute("UPDATE api_keys SET customer_id = NULL")
        conn.execute(sql)
        first = {
            r["email"]: (r["role"], r["managed_by"])
            for r in conn.execute(
                """SELECT u.email, m.role, m.managed_by FROM org_memberships m JOIN users u ON u.id = m.user_id
                   WHERE m.customer_id = %s""",
                (a,),
            ).fetchall()
        }
        conn.execute(sql)
        again = conn.execute("SELECT count(*) AS n FROM org_memberships WHERE customer_id = %s", (a,)).fetchone()["n"]
        key_org = conn.execute("SELECT customer_id FROM api_keys WHERE name = 'old'").fetchone()["customer_id"]
    assert first == {
        "owner@a.example": ("owner", None),  # made first
        "first@legacy.example": ("admin", None),
        "second@legacy.example": ("admin", None),
        "dir@legacy.example": ("member", "scim"),
    }
    assert again == 4
    assert str(key_org) == a
