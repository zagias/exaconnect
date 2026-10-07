"""Shared accounts (ADR 0023): memberships, roles, invitations, switching
organisation and enforcement of the current organisation and viewer role."""

from __future__ import annotations

import pytest

from exaconnect_controller import db
from exaconnect_controller.security import hash_password, token_hash

from .commai_helpers import PASSWORD

API = "/api/v1"
NEW_PASSWORD = "a brand new password"


def _org(client, admin_headers, name: str) -> str:
    r = client.post(f"{API}/customers", json={"name": name}, headers=admin_headers)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _person(cid: str | None, email: str) -> str:
    """A customer account made the old way (users.customer_id), like the seed and SCIM do."""
    with db.tx() as conn:
        return str(
            conn.execute(
                "INSERT INTO users (email, password_hash, role, customer_id) VALUES (%s, %s, 'customer', %s)"
                " RETURNING id",
                (email, hash_password(PASSWORD), cid),
            ).fetchone()["id"]
        )


def _login(client, email: str, password: str = PASSWORD) -> dict:
    client.cookies.clear()
    r = client.post(f"{API}/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    client.cookies.clear()
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _invite(client, headers, cid: str, email: str, role: str = "member") -> dict:
    r = client.post(f"{API}/orgs/{cid}/invites", json={"email": email, "role": role}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _role(cid: str, uid: str) -> str | None:
    with db.tx() as conn:
        row = conn.execute(
            "SELECT role FROM org_memberships WHERE customer_id = %s AND user_id = %s", (cid, uid)
        ).fetchone()
    return row["role"] if row else None


@pytest.fixture
def two_orgs(client, admin_headers):
    """Org A with an owner and an admin; org B with an owner."""
    a = _org(client, admin_headers, "Org A")
    b = _org(client, admin_headers, "Org B")
    for cid in (a, b):
        r = client.put(
            f"{API}/customers/{cid}/classes/base", json={"sla": {"max_latency_ms": 250}}, headers=admin_headers
        )
        assert r.status_code == 200, r.text
    out = {
        "a": a,
        "b": b,
        "owner_a": _person(a, "owner@a.example"),
        "admin_a": _person(a, "admin@a.example"),
        "owner_b": _person(b, "owner@b.example"),
    }
    out["h_owner_a"] = _login(client, "owner@a.example")
    out["h_admin_a"] = _login(client, "admin@a.example")
    out["h_owner_b"] = _login(client, "owner@b.example")
    return out


# ---- back-compat -----------------------------------------------------------------------


def test_existing_customer_accounts_become_members(client, two_orgs):
    o = two_orgs
    assert _role(o["a"], o["owner_a"]) == "owner"  # the first person in an organisation owns it
    assert _role(o["a"], o["admin_a"]) == "admin"  # later hand-made people keep full rights
    me = client.get(f"{API}/auth/me", headers=o["h_admin_a"]).json()
    assert me["customer_id"] == o["a"]
    assert me["org_role"] == "admin"
    assert me["organisation"] == "Org A"
    assert [m["name"] for m in me["memberships"]] == ["Org A"]


def test_schema_backfill_is_idempotent(client, two_orgs):
    from importlib import resources

    sql = resources.files("exaconnect_controller").joinpath("commai", "sql", "15_memberships.sql").read_text()
    with db.tx() as conn:
        conn.execute("DELETE FROM org_memberships WHERE user_id = %s", (two_orgs["admin_a"],))
        conn.execute(sql)
        conn.execute(sql)
        n = conn.execute("SELECT count(*) AS n FROM org_memberships").fetchone()["n"]
        owners = conn.execute(
            "SELECT count(*) AS n FROM org_memberships WHERE customer_id = %s AND role = 'owner'", (two_orgs["a"],)
        ).fetchone()["n"]
    assert n == 3
    assert owners == 1
    assert _role(two_orgs["a"], two_orgs["admin_a"]) == "admin"


# ---- invitations ------------------------------------------------------------------------


def test_invite_new_person_sets_password_and_joins(client, two_orgs):
    o = two_orgs
    inv = _invite(client, o["h_admin_a"], o["a"], "New.Person@a.example", "member")
    assert inv["emailed"] is False and inv["path"].startswith("/invite/")
    token = inv["token"]
    # Only the hash is stored.
    with db.tx() as conn:
        row = conn.execute("SELECT token_hash FROM org_invites WHERE id = %s", (inv["id"],)).fetchone()
        assert row["token_hash"] == token_hash(token)
        assert conn.execute("SELECT 1 FROM org_invites WHERE token_hash = %s", (token,)).fetchone() is None

    shown = client.get(f"{API}/invites/{token}").json()
    assert shown == {
        "organisation": "Org A",
        "email": "new.person@a.example",
        "role": "member",
        "expires_at": shown["expires_at"],
        "has_account": False,
        "sso_required": False,
    }
    # The pending list shows it to managers, without the token.
    pending = client.get(f"{API}/orgs/{o['a']}/invites", headers=o["h_owner_a"]).json()
    assert [p["email"] for p in pending] == ["new.person@a.example"] and "token" not in pending[0]

    r = client.post(f"{API}/invites/{token}/accept", json={"password": "short"})
    assert r.status_code == 422
    r = client.post(f"{API}/invites/{token}/accept", json={"password": NEW_PASSWORD, "name": "New Person"})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "member"
    assert "exa_session" in r.cookies or client.cookies.get("exa_session")
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    me = client.get(f"{API}/auth/me", headers=h).json()
    assert me["role"] == "customer" and me["customer_id"] == o["a"] and me["org_role"] == "member"
    # The new password works for signing in.
    _login(client, "new.person@a.example", NEW_PASSWORD)
    # The link is single use.
    client.cookies.clear()
    r = client.post(f"{API}/invites/{token}/accept", json={"password": NEW_PASSWORD})
    assert r.status_code == 410
    assert client.get(f"{API}/invites/{token}").status_code == 410
    with db.tx() as conn:
        actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log ORDER BY id").fetchall()]
    assert "org.invite.create" in actions and "org.invite.accept" in actions


def test_invite_existing_person_signs_in_and_accepts(client, two_orgs):
    o = two_orgs
    inv = _invite(client, o["h_owner_b"], o["b"], "admin@a.example", "viewer")
    token = inv["token"]
    assert client.get(f"{API}/invites/{token}").json()["has_account"] is True
    # Signed out, an existing account can't take the invitation by setting a password.
    client.cookies.clear()
    r = client.post(f"{API}/invites/{token}/accept", json={"password": NEW_PASSWORD})
    assert r.status_code == 401
    # Someone else's account can't accept it.
    r = client.post(f"{API}/invites/{token}/accept", json={}, headers=o["h_owner_a"])
    assert r.status_code == 403
    r = client.post(f"{API}/invites/{token}/accept", json={}, headers=o["h_admin_a"])
    assert r.status_code == 200, r.text
    assert _role(o["b"], o["admin_a"]) == "viewer"
    # Accepting switches this session to the new organisation; the primary stays.
    me = client.get(f"{API}/auth/me", headers=o["h_admin_a"]).json()
    assert me["customer_id"] == o["b"] and me["org_role"] == "viewer"
    assert sorted(m["name"] for m in me["memberships"]) == ["Org A", "Org B"]
    with db.tx() as conn:
        assert (
            str(conn.execute("SELECT customer_id FROM users WHERE id = %s", (o["admin_a"],)).fetchone()["customer_id"])
            == o["a"]
        )
    # Reused token fails.
    r = client.post(f"{API}/invites/{token}/accept", json={}, headers=o["h_admin_a"])
    assert r.status_code == 410


def test_expired_and_revoked_invites_fail(client, two_orgs):
    o = two_orgs
    inv = _invite(client, o["h_owner_a"], o["a"], "late@a.example")
    with db.tx() as conn:
        conn.execute("UPDATE org_invites SET expires_at = now() - interval '1 second' WHERE id = %s", (inv["id"],))
    client.cookies.clear()
    assert client.get(f"{API}/invites/{inv['token']}").status_code == 410
    r = client.post(f"{API}/invites/{inv['token']}/accept", json={"password": NEW_PASSWORD})
    assert r.status_code == 410
    with db.tx() as conn:
        assert conn.execute("SELECT 1 FROM users WHERE email = 'late@a.example'").fetchone() is None

    inv2 = _invite(client, o["h_owner_a"], o["a"], "gone@a.example")
    r = client.delete(f"{API}/orgs/{o['a']}/invites/{inv2['id']}", headers=o["h_admin_a"])
    assert r.status_code == 204
    client.cookies.clear()
    assert client.post(f"{API}/invites/{inv2['token']}/accept", json={"password": NEW_PASSWORD}).status_code == 404
    assert client.get(f"{API}/invites/not-a-real-token").status_code == 404

    # A fresh invitation replaces an earlier one for the same address.
    first = _invite(client, o["h_owner_a"], o["a"], "twice@a.example")
    _invite(client, o["h_owner_a"], o["a"], "twice@a.example")
    assert client.get(f"{API}/invites/{first['token']}").status_code == 404


def test_invite_rules(client, two_orgs, admin_headers):
    o = two_orgs
    # Owner role is handed on, not invited.
    r = client.post(
        f"{API}/orgs/{o['a']}/invites", json={"email": "x@a.example", "role": "owner"}, headers=o["h_owner_a"]
    )
    assert r.status_code == 422
    # Existing members can't be invited again.
    r = client.post(f"{API}/orgs/{o['a']}/invites", json={"email": "admin@a.example"}, headers=o["h_owner_a"])
    assert r.status_code == 409
    # ExaCarib and carrier accounts don't join organisations.
    r = client.post(f"{API}/orgs/{o['a']}/invites", json={"email": "admin@example.org"}, headers=o["h_owner_a"])
    assert r.status_code == 409
    # Another organisation's owner can't invite into A.
    r = client.post(f"{API}/orgs/{o['a']}/invites", json={"email": "y@a.example"}, headers=o["h_owner_b"])
    assert r.status_code == 403
    # ExaCarib admins can.
    r = client.post(f"{API}/orgs/{o['a']}/invites", json={"email": "y@a.example"}, headers=admin_headers)
    assert r.status_code == 201


# ---- roles -------------------------------------------------------------------------------


def _join(client, inviter_headers, cid: str, email: str, role: str) -> tuple[str, dict]:
    """A new person who accepted an invitation with this role. Returns (user id, headers)."""
    inv = _invite(client, inviter_headers, cid, email, role)
    client.cookies.clear()
    r = client.post(f"{API}/invites/{inv['token']}/accept", json={"password": NEW_PASSWORD})
    assert r.status_code == 200, r.text
    client.cookies.clear()
    with db.tx() as conn:
        uid = str(conn.execute("SELECT id FROM users WHERE email = %s", (email,)).fetchone()["id"])
    return uid, {"Authorization": f"Bearer {r.json()['token']}"}


def test_member_and_viewer_rights(client, two_orgs):
    o = two_orgs
    member, h_member = _join(client, o["h_owner_a"], o["a"], "member@a.example", "member")
    viewer, h_viewer = _join(client, o["h_owner_a"], o["a"], "viewer@a.example", "viewer")

    # Everyone in the organisation sees its people.
    for h in (h_member, h_viewer):
        r = client.get(f"{API}/orgs/{o['a']}/members", headers=h)
        assert r.status_code == 200
        assert {m["email"] for m in r.json()["members"]} >= {"owner@a.example", "member@a.example"}
        assert r.json()["can_manage"] is False
    # Members and viewers don't manage people.
    assert (
        client.post(f"{API}/orgs/{o['a']}/invites", json={"email": "z@a.example"}, headers=h_member).status_code == 403
    )
    assert client.get(f"{API}/orgs/{o['a']}/invites", headers=h_member).status_code == 403
    assert (
        client.patch(f"{API}/orgs/{o['a']}/members/{viewer}", json={"role": "admin"}, headers=h_member).status_code
        == 403
    )
    assert client.delete(f"{API}/orgs/{o['a']}/members/{viewer}", headers=h_member).status_code == 403
    # Members change things; viewers can't write anything, but can still read.
    sla = {"sla": {"max_latency_ms": 200}, "description": "Video"}
    r = client.put(f"{API}/customers/{o['a']}/classes/video", json=sla, headers=h_member)
    assert r.status_code == 200, r.text
    r = client.put(f"{API}/customers/{o['a']}/classes/video2", json=sla, headers=h_viewer)
    assert r.status_code == 403 and "view-only" in r.json()["detail"]
    assert client.delete(f"{API}/customers/{o['a']}/classes/video", headers=h_viewer).status_code == 403
    assert client.get(f"{API}/classes", headers=h_viewer).status_code == 200
    # Settings (commai:admin, SSO, SCIM) are for owners and admins.
    r = client.post(f"{API}/commai/customers/{o['a']}/teams", json={"name": "Desk"}, headers=h_member)
    assert r.status_code == 403
    r = client.post(f"{API}/commai/customers/{o['a']}/teams", json={"name": "Desk"}, headers=o["h_admin_a"])
    assert r.status_code == 201, r.text
    # A viewer's API key is read-only too; their own sign-in is still theirs to manage.
    r = client.post(f"{API}/auth/api-keys", json={"name": "read"}, headers=h_viewer)
    assert r.status_code == 201
    hk = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.get(f"{API}/classes", headers=hk).status_code == 200
    assert client.put(f"{API}/customers/{o['a']}/classes/video3", json=sla, headers=hk).status_code == 403
    r = client.post(
        f"{API}/auth/password", json={"current": NEW_PASSWORD, "new": "another long password"}, headers=h_viewer
    )
    assert r.status_code == 204


def test_change_role_and_last_owner_protection(client, two_orgs):
    o = two_orgs
    member, h_member = _join(client, o["h_owner_a"], o["a"], "m2@a.example", "member")
    # Admins change roles of non-owners.
    r = client.patch(f"{API}/orgs/{o['a']}/members/{member}", json={"role": "viewer"}, headers=o["h_admin_a"])
    assert r.status_code == 200 and _role(o["a"], member) == "viewer"
    # ...but can't touch owners or make owners.
    r = client.patch(f"{API}/orgs/{o['a']}/members/{o['owner_a']}", json={"role": "member"}, headers=o["h_admin_a"])
    assert r.status_code == 403
    r = client.patch(f"{API}/orgs/{o['a']}/members/{member}", json={"role": "owner"}, headers=o["h_admin_a"])
    assert r.status_code == 403
    assert client.delete(f"{API}/orgs/{o['a']}/members/{o['owner_a']}", headers=o["h_admin_a"]).status_code == 403
    # The only owner can't step down, be removed or leave.
    r = client.patch(f"{API}/orgs/{o['a']}/members/{o['owner_a']}", json={"role": "admin"}, headers=o["h_owner_a"])
    assert r.status_code == 409
    assert client.post(f"{API}/orgs/{o['a']}/leave", headers=o["h_owner_a"]).status_code == 409
    assert client.delete(f"{API}/orgs/{o['a']}/members/{o['owner_a']}", headers=o["h_owner_a"]).status_code == 409
    assert _role(o["a"], o["owner_a"]) == "owner"
    # With a second owner, the first can step down.
    r = client.patch(f"{API}/orgs/{o['a']}/members/{o['admin_a']}", json={"role": "owner"}, headers=o["h_owner_a"])
    assert r.status_code == 200
    r = client.patch(f"{API}/orgs/{o['a']}/members/{o['owner_a']}", json={"role": "member"}, headers=o["h_owner_a"])
    assert r.status_code == 200 and _role(o["a"], o["owner_a"]) == "member"
    assert (
        client.patch(
            f"{API}/orgs/{o['a']}/members/{o['admin_a']}", json={"role": "admin"}, headers=o["h_admin_a"]
        ).status_code
        == 409
    )
    with db.tx() as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM audit_log WHERE action = 'org.member.role' AND customer_id = %s", (o["a"],)
        ).fetchone()["n"]
    assert n == 3


def test_transfer_ownership(client, two_orgs):
    o = two_orgs
    r = client.post(f"{API}/orgs/{o['a']}/transfer-ownership", json={"user_id": o["admin_a"]}, headers=o["h_admin_a"])
    assert r.status_code == 403  # only owners
    r = client.post(f"{API}/orgs/{o['a']}/transfer-ownership", json={"user_id": o["owner_b"]}, headers=o["h_owner_a"])
    assert r.status_code == 404  # not a member
    r = client.post(f"{API}/orgs/{o['a']}/transfer-ownership", json={"user_id": o["admin_a"]}, headers=o["h_owner_a"])
    assert r.status_code == 200, r.text
    assert _role(o["a"], o["admin_a"]) == "owner" and _role(o["a"], o["owner_a"]) == "admin"
    # Now the old owner may leave.
    assert client.post(f"{API}/orgs/{o['a']}/leave", headers=o["h_owner_a"]).status_code == 204


def test_remove_and_leave(client, two_orgs):
    o = two_orgs
    # admin@a also joins B; they're removed from A (their primary): B becomes primary.
    inv = _invite(client, o["h_owner_b"], o["b"], "admin@a.example", "member")
    assert client.post(f"{API}/invites/{inv['token']}/accept", json={}, headers=o["h_admin_a"]).status_code == 200
    client.post(f"{API}/auth/organisation", json={"customer_id": o["a"]}, headers=o["h_admin_a"])
    key = client.post(f"{API}/auth/api-keys", json={"name": "in A"}, headers=o["h_admin_a"]).json()
    hk = {"Authorization": f"Bearer {key['token']}"}
    assert client.get(f"{API}/classes", headers=hk).status_code == 200
    r = client.delete(f"{API}/orgs/{o['a']}/members/{o['admin_a']}", headers=o["h_owner_a"])
    assert r.status_code == 204
    assert _role(o["a"], o["admin_a"]) is None
    # The key made in A is revoked; the session falls back to B.
    assert client.get(f"{API}/classes", headers=hk).status_code == 401
    me = client.get(f"{API}/auth/me", headers=o["h_admin_a"]).json()
    assert me["customer_id"] == o["b"] and [m["name"] for m in me["memberships"]] == ["Org B"]
    with db.tx() as conn:
        assert (
            str(conn.execute("SELECT customer_id FROM users WHERE id = %s", (o["admin_a"],)).fetchone()["customer_id"])
            == o["b"]
        )
    # They leave B too: no organisation left, so nothing but their own sign-in.
    assert client.post(f"{API}/orgs/{o['b']}/leave", headers=o["h_admin_a"]).status_code == 204
    me = client.get(f"{API}/auth/me", headers=o["h_admin_a"])
    assert me.status_code == 200 and me.json()["customer_id"] is None and me.json()["memberships"] == []
    for path in ("/classes", "/customers/mine", "/sites", "/nodes"):
        assert client.get(f"{API}{path}", headers=o["h_admin_a"]).status_code == 403, path
    # Leaving an organisation you're not in.
    assert client.post(f"{API}/orgs/{o['a']}/leave", headers=o["h_admin_a"]).status_code == 404


# ---- switching and isolation ------------------------------------------------------------


def test_switching_changes_what_you_see(client, two_orgs):
    o = two_orgs
    inv = _invite(client, o["h_owner_b"], o["b"], "owner@a.example", "member")
    assert client.post(f"{API}/invites/{inv['token']}/accept", json={}, headers=o["h_owner_a"]).status_code == 200
    h = _login(client, "owner@a.example")  # a fresh session starts in the primary organisation

    def seen() -> set[str]:
        return {c["customer_id"] for c in client.get(f"{API}/classes", headers=h).json()}

    assert seen() == {o["a"]}
    assert [c["name"] for c in client.get(f"{API}/customers/mine", headers=h).json()] == ["Org A"]
    r = client.post(f"{API}/auth/organisation", json={"customer_id": o["b"]}, headers=h)
    assert r.status_code == 200 and r.json()["role"] == "member"
    assert seen() == {o["b"]}
    assert [c["name"] for c in client.get(f"{API}/customers/mine", headers=h).json()] == ["Org B"]
    # Acting for B, A's records are out of reach, even though they own A.
    sla = {"sla": {"max_latency_ms": 200}}
    assert client.put(f"{API}/customers/{o['a']}/classes/x", json=sla, headers=h).status_code == 403
    assert client.put(f"{API}/customers/{o['b']}/classes/x", json=sla, headers=h).status_code == 200
    # As a member of B they can't manage B's people.
    assert client.get(f"{API}/orgs/{o['b']}/invites", headers=h).status_code == 403
    # Switching back.
    assert client.post(f"{API}/auth/organisation", json={"customer_id": o["a"]}, headers=h).status_code == 200
    assert seen() == {o["a"]}
    with db.tx() as conn:
        assert conn.execute("SELECT 1 FROM audit_log WHERE action = 'org.switch'").fetchone()


def test_member_of_a_cannot_read_b(client, two_orgs):
    o = two_orgs
    h = o["h_admin_a"]
    # Can't switch into an organisation you don't belong to.
    assert client.post(f"{API}/auth/organisation", json={"customer_id": o["b"]}, headers=h).status_code == 403
    assert client.post(f"{API}/auth/organisation", json={"customer_id": "nonsense"}, headers=h).status_code == 403
    assert client.get(f"{API}/orgs/{o['b']}/members", headers=h).status_code == 403
    assert client.get(f"{API}/customers/{o['b']}/settings", headers=h).status_code == 403
    assert client.get(f"{API}/commai/customers/{o['b']}/members", headers=h).status_code == 403
    assert o["b"] not in {c["customer_id"] for c in client.get(f"{API}/classes", headers=h).json()}
    assert client.get(f"{API}/classes", params={"customer_id": o["b"]}, headers=h).json()[0]["customer_id"] == o["a"]


def test_api_key_stays_in_its_organisation(client, two_orgs):
    o = two_orgs
    inv = _invite(client, o["h_owner_b"], o["b"], "owner@a.example", "viewer")
    assert client.post(f"{API}/invites/{inv['token']}/accept", json={}, headers=o["h_owner_a"]).status_code == 200
    # The session is now in B (as a viewer). A key made now acts in B only, read-only.
    r = client.post(f"{API}/auth/api-keys", json={"name": "b"}, headers=o["h_owner_a"])
    assert r.status_code == 201
    hk = {"Authorization": f"Bearer {r.json()['token']}"}
    assert {c["customer_id"] for c in client.get(f"{API}/classes", headers=hk).json()} == {o["b"]}
    assert client.post(f"{API}/auth/organisation", json={"customer_id": o["a"]}, headers=hk).status_code == 403
    sla = {"sla": {"max_latency_ms": 200}}
    assert client.put(f"{API}/customers/{o['b']}/classes/k", json=sla, headers=hk).status_code == 403


def test_commai_people_follow_memberships(client, two_orgs):
    o = two_orgs
    inv = _invite(client, o["h_owner_b"], o["b"], "admin@a.example", "member")
    assert client.post(f"{API}/invites/{inv['token']}/accept", json={}, headers=o["h_admin_a"]).status_code == 200
    people = client.get(f"{API}/commai/customers/{o['b']}/members", headers=o["h_owner_b"]).json()
    assert {p["email"]: p["org_role"] for p in people} == {"owner@b.example": "owner", "admin@a.example": "member"}


def test_directory_managed_members_are_not_edited_by_hand(client, two_orgs):
    o = two_orgs
    with db.tx() as conn:
        uid = str(
            conn.execute(
                """INSERT INTO users (email, password_hash, role, customer_id, access_scopes, provisioned_by)
                   VALUES ('dir@a.example', '!sso', 'customer', %s, %s, 'scim') RETURNING id""",
                (o["a"], ["commai:read", "commai:write", "commai:notes"]),
            ).fetchone()["id"]
        )
    assert _role(o["a"], uid) == "member"
    r = client.patch(f"{API}/orgs/{o['a']}/members/{uid}", json={"role": "admin"}, headers=o["h_owner_a"])
    assert r.status_code == 409
    assert client.delete(f"{API}/orgs/{o['a']}/members/{uid}", headers=o["h_owner_a"]).status_code == 409
    r = client.post(f"{API}/orgs/{o['a']}/transfer-ownership", json={"user_id": uid}, headers=o["h_owner_a"])
    assert r.status_code == 409
    members = client.get(f"{API}/orgs/{o['a']}/members", headers=o["h_owner_a"]).json()["members"]
    assert {m["email"]: m["managed_by"] for m in members}["dir@a.example"] == "scim"


def test_deleting_the_only_owner_promotes_an_admin(client, two_orgs, admin_headers):
    o = two_orgs
    r = client.delete(f"{API}/users/{o['owner_a']}", headers=admin_headers)
    assert r.status_code == 204
    assert _role(o["a"], o["admin_a"]) == "owner"
