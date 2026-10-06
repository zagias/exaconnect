"""SCIM 2.0 provisioning: tokens, users, groups to teams, deactivation, admin mappings."""

import pytest

from exaconnect_controller import db

from .commai_helpers import api_key, business
from .identity_helpers import identity_settings

SCIM = "/api/v1/scim/v2"
USER = "urn:ietf:params:scim:schemas:core:2.0:User"
PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


@pytest.fixture
def settings(tmp_path):
    return identity_settings(tmp_path)


def _token(client, b, who="agent"):
    r = client.post(f"/api/v1/customers/{b['id']}/scim-tokens", json={"name": "Entra ID"}, headers=b[who]["h"])
    assert r.status_code == 201, r.text
    assert r.json()["token"].startswith("scim_")
    return {"Authorization": f"Bearer {r.json()['token']}", "Content-Type": "application/scim+json"}


def _new_user(client, h, email, **kw):
    body = {
        "schemas": [USER],
        "userName": email,
        "name": {"givenName": "Ana", "familyName": "Lee"},
        "emails": [{"value": email, "primary": True}],
        "active": True,
        **kw,
    }
    r = client.post(f"{SCIM}/Users", json=body, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def _sign_in_as(email):
    """A session for a provisioned person (they would arrive through SSO)."""
    from exaconnect_controller.identity import sessions

    with db.tx() as conn:
        uid = conn.execute("SELECT id FROM users WHERE email = %s", (email,)).fetchone()["id"]
        token, _ = sessions.start(conn, uid, 1, "sso")
    return {"Authorization": f"Bearer {token}"}


def test_token_required_and_revocable(client):
    b = business(client)
    assert client.get(f"{SCIM}/Users").status_code == 401
    assert client.get(f"{SCIM}/Users", headers={"Authorization": "Bearer scim_nope"}).json()["status"] == "401"
    h = _token(client, b)
    assert client.get(f"{SCIM}/ServiceProviderConfig", headers=h).json()["patch"]["supported"] is True
    assert client.get(f"{SCIM}/ResourceTypes", headers=h).json()["totalResults"] == 2
    assert client.get(f"{SCIM}/Schemas", headers=h).status_code == 200
    toks = client.get(f"/api/v1/customers/{b['id']}/scim-tokens", headers=b["agent"]["h"]).json()
    assert "token" not in toks["items"][0] and toks["scim_url"].endswith("/api/v1/scim/v2")
    tid = toks["items"][0]["id"]
    assert client.delete(f"/api/v1/customers/{b['id']}/scim-tokens/{tid}", headers=b["agent"]["h"]).status_code == 204
    assert client.get(f"{SCIM}/Users", headers=h).status_code == 401
    other = business(client, "Other Bank")
    assert (
        client.post(
            f"/api/v1/customers/{b['id']}/scim-tokens", json={"name": "x"}, headers=other["agent"]["h"]
        ).status_code
        == 403
    )


def test_users_lifecycle(client):
    b = business(client)
    h = _token(client, b)
    u = _new_user(client, h, "ana.lee@examplebank.example", externalId="e-1")
    assert u["active"] and u["name"]["givenName"] == "Ana" and u["externalId"] == "e-1"
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = %s", (u["id"],)).fetchone()
        seat = conn.execute("SELECT seat FROM commai_members WHERE user_id = %s", (u["id"],)).fetchone()
    assert row["role"] == "customer" and str(row["customer_id"]) == b["id"] and row["provisioned_by"] == "scim"
    assert row["password_hash"] == "!sso" and seat["seat"] == "agent"
    # Duplicate userName.
    r = client.post(f"{SCIM}/Users", json={"schemas": [USER], "userName": "ana.lee@examplebank.example"}, headers=h)
    assert r.status_code == 409 and r.json()["scimType"] == "uniqueness"
    # Filter and pagination.
    r = client.get(f'{SCIM}/Users?filter=userName eq "ANA.LEE@examplebank.example"', headers=h).json()
    assert r["totalResults"] == 1 and r["Resources"][0]["id"] == u["id"]
    assert client.get(f'{SCIM}/Users?filter=title co "x"', headers=h).status_code == 400
    page = client.get(f"{SCIM}/Users?startIndex=2&count=2", headers=h).json()
    assert page["totalResults"] == 4 and page["startIndex"] == 2 and page["itemsPerPage"] == 2  # 3 seeded + Ana
    # PATCH and PUT.
    r = client.patch(
        f"{SCIM}/Users/{u['id']}",
        headers=h,
        json={
            "schemas": [PATCH],
            "Operations": [
                {"op": "Replace", "path": "name.familyName", "value": "Lee-Smith"},
                {"op": "replace", "value": {"displayName": "Ana L."}},
            ],
        },
    )
    assert r.status_code == 200 and r.json()["name"]["familyName"] == "Lee-Smith"
    assert r.json()["displayName"] == "Ana L."
    r = client.put(
        f"{SCIM}/Users/{u['id']}",
        headers=h,
        json={"schemas": [USER], "userName": "ana.lee@examplebank.example", "name": {"givenName": "Ana"}},
    )
    assert r.status_code == 200 and r.json()["name"]["familyName"] == ""
    # Another business's token can't see her.
    other = business(client, "Other Bank")
    oh = _token(client, other)
    assert client.get(f"{SCIM}/Users/{u['id']}", headers=oh).status_code == 404
    assert client.get(f"{SCIM}/Users/not-a-uuid", headers=h).status_code == 404


def test_deactivation_ends_sessions_and_keys_at_once(client):
    b = business(client)
    h = _token(client, b)
    u = _new_user(client, h, "bo@examplebank.example")
    session = _sign_in_as("bo@examplebank.example")
    assert client.get("/api/v1/auth/me", headers=session).status_code == 200
    key = api_key(client, session, ["commai:read"])
    assert client.get("/api/v1/auth/me", headers=key).status_code == 200
    r = client.patch(
        f"{SCIM}/Users/{u['id']}",
        headers=h,
        json={"schemas": [PATCH], "Operations": [{"op": "Replace", "path": "active", "value": "False"}]},
    )
    assert r.status_code == 200 and r.json()["active"] is False
    assert client.get("/api/v1/auth/me", headers=session).status_code == 401
    assert client.get("/api/v1/auth/me", headers=key).status_code == 401
    with db.tx() as conn:
        assert conn.execute("SELECT revoked_at FROM api_keys").fetchone()["revoked_at"] is not None
    # Reactivating doesn't bring the key back.
    client.patch(
        f"{SCIM}/Users/{u['id']}",
        headers=h,
        json={"schemas": [PATCH], "Operations": [{"op": "replace", "path": "active", "value": True}]},
    )
    assert client.get("/api/v1/auth/me", headers=key).status_code == 401
    # Delete: gone from SCIM, signed out, account kept for history.
    session = _sign_in_as("bo@examplebank.example")
    assert client.delete(f"{SCIM}/Users/{u['id']}", headers=h).status_code == 204
    assert client.get("/api/v1/auth/me", headers=session).status_code == 401
    assert client.get(f"{SCIM}/Users/{u['id']}", headers=h).status_code == 404
    # Re-provisioning the same person brings the account back.
    again = _new_user(client, h, "bo@examplebank.example")
    assert again["id"] == u["id"] and again["active"]


def test_groups_map_to_teams_and_admin_needs_approval(client, admin_headers):
    b = business(client)
    h = _token(client, b)
    ana = _new_user(client, h, "ana@examplebank.example")
    bo = _new_user(client, h, "bo@examplebank.example")
    r = client.post(f"{SCIM}/Groups", headers=h, json={"displayName": "Support", "members": [{"value": ana["id"]}]})
    assert r.status_code == 201, r.text
    g = r.json()
    assert [m["value"] for m in g["members"]] == [ana["id"]]
    with db.tx() as conn:
        team = conn.execute(
            "SELECT id FROM commai_teams WHERE customer_id = %s AND name = 'Support'", (b["id"],)
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM commai_team_members WHERE team_id = %s AND user_id = %s", (team["id"], ana["id"])
        ).fetchone()
    # PATCH add / remove members (Entra ID style).
    r = client.patch(
        f"{SCIM}/Groups/{g['id']}",
        headers=h,
        json={"schemas": [PATCH], "Operations": [{"op": "Add", "path": "members", "value": [{"value": bo["id"]}]}]},
    )
    assert len(r.json()["members"]) == 2
    r = client.patch(
        f"{SCIM}/Groups/{g['id']}",
        headers=h,
        json={"schemas": [PATCH], "Operations": [{"op": "Remove", "path": f'members[value eq "{bo["id"]}"]'}]},
    )
    assert [m["value"] for m in r.json()["members"]] == [ana["id"]]
    with db.tx() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM commai_team_members WHERE team_id = %s AND user_id = %s", (team["id"], bo["id"])
            ).fetchone()
            is None
        )
    assert client.get(f'{SCIM}/Groups?filter=displayName eq "Support"', headers=h).json()["totalResults"] == 1

    ana_h = _sign_in_as("ana@examplebank.example")
    assert client.get("/api/v1/auth/me", headers=ana_h).json()["scopes"] == [
        "commai:read",
        "commai:write",
        "commai:notes",
    ]
    # Member rights: CommAI work only, no network API, no settings, no stronger API key.
    assert client.get("/api/v1/sites", headers=ana_h).status_code == 403
    assert client.get(f"/api/v1/customers/{b['id']}/directory-groups", headers=ana_h).status_code == 403
    assert (
        client.post("/api/v1/auth/api-keys", json={"name": "x", "scopes": ["connect"]}, headers=ana_h).status_code
        == 403
    )
    r = client.post("/api/v1/auth/api-keys", json={"name": "x"}, headers=ana_h)
    assert r.status_code == 201 and r.json()["scopes"] == ["commai:read", "commai:write", "commai:notes"]

    # Seat mapping.
    base = f"/api/v1/customers/{b['id']}/directory-groups/{g['id']}"
    r = client.put(base, json={"seat": "internal", "team_id": str(team["id"])}, headers=b["agent"]["h"])
    assert r.status_code == 200 and r.json()["seat"] == "internal"
    with db.tx() as conn:
        assert (
            conn.execute("SELECT seat FROM commai_members WHERE user_id = %s", (ana["id"],)).fetchone()["seat"]
            == "internal"
        )
    # Ask for admin: pending, no rights yet; the asker can't approve their own request.
    r = client.put(
        base, json={"seat": "agent", "team_id": str(team["id"]), "business_admin": True}, headers=b["agent"]["h"]
    )
    assert r.json()["business_admin"] == "pending"
    assert client.get("/api/v1/auth/me", headers=ana_h).json()["scopes"] is not None
    assert client.post(f"{base}/approve-admin", headers=b["agent"]["h"]).status_code == 403
    assert client.post(f"{base}/approve-admin", headers=ana_h).status_code == 403
    r = client.post(f"{base}/approve-admin", headers=b["agent2"]["h"])
    assert r.status_code == 200 and r.json()["business_admin"] == "approved"
    assert client.get("/api/v1/auth/me", headers=ana_h).json()["scopes"] is None
    # Leaving the group takes the rights away again.
    client.patch(
        f"{SCIM}/Groups/{g['id']}",
        headers=h,
        json={"schemas": [PATCH], "Operations": [{"op": "remove", "path": "members"}]},
    )
    assert client.get("/api/v1/auth/me", headers=ana_h).json()["scopes"] is not None
    with db.tx() as conn:
        actions = {r["action"] for r in conn.execute("SELECT action FROM audit_log").fetchall()}
    assert {
        "scim.user.create",
        "scim.group.create",
        "scim.group.patch",
        "directory_group.map",
        "directory_group.approve_admin",
        "scim_token.create",
    } <= actions
    assert client.delete(f"{SCIM}/Groups/{g['id']}", headers=h).status_code == 204


def test_existing_hand_made_account_is_linked_not_duplicated(client):
    b = business(client)
    h = _token(client, b)
    u = _new_user(client, h, b["agent2"]["email"])
    assert u["id"] == b["agent2"]["id"]
    with db.tx() as conn:
        row = conn.execute("SELECT provisioned_by, access_scopes FROM users WHERE id = %s", (u["id"],)).fetchone()
    assert row["provisioned_by"] is None and row["access_scopes"] is None  # rights unchanged
    # An ExaCarib admin's email can't be taken over by a business directory.
    r = client.post(f"{SCIM}/Users", json={"schemas": [USER], "userName": "admin@example.org"}, headers=h)
    assert r.status_code == 409
