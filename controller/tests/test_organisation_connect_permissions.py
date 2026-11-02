"""Organisation address access must not bypass Connect provisioning permissions."""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from exaconnect_controller import db
from exaconnect_controller.api import organisation as org_api
from exaconnect_controller.api.deps import User, current_user

ORG = "11111111-1111-1111-1111-111111111111"
LOCATION = "22222222-2222-2222-2222-222222222222"
SITE = "33333333-3333-3333-3333-333333333333"
CONNECT = f"/api/v1/orgs/{ORG}/locations/{LOCATION}/connect"
INSTALL = f"/api/v1/orgs/{ORG}/sites/{SITE}/install-code"


@pytest.fixture
def portal(monkeypatch):
    state = SimpleNamespace(
        user=User(
            "person",
            "owner@example.invalid",
            "customer",
            ORG,
            None,
            org_role="owner",
            products=("connect", "commai"),
            apps=("connect", "commai"),
        ),
        provisioned=[],
        tokens=[],
        addresses=[],
    )

    class Connection:
        def execute(self, *_):
            return self

        def fetchone(self):
            return {"id": SITE, "name": "Example site"}

    def connect(_conn, _cid, _actor, location, _links, _prefixes):
        state.provisioned.append(location)
        return {"site_id": SITE, "name": "Example site"}

    def token(_conn, site, _actor, **_):
        state.tokens.append(site)
        return "example-install-code", None

    def address(_conn, _cid, _actor, values, location=None):
        state.addresses.append(values)
        return {"id": location or LOCATION, **values}

    monkeypatch.setattr(org_api.db, "tx", lambda: nullcontext(Connection()))
    monkeypatch.setattr(org_api.organisation, "connect_location", connect)
    monkeypatch.setattr(org_api.organisation, "has_hub", lambda *_: False)
    monkeypatch.setattr(org_api.organisation, "save_location", address)
    monkeypatch.setattr(org_api.inventory, "issue_token", token)
    app = FastAPI()
    app.state.settings = SimpleNamespace(agent_url="https://agent.example.invalid", agent_public_url="")
    app.state.ca = SimpleNamespace(fingerprint="example-fingerprint")
    app.dependency_overrides[current_user] = lambda: state.user
    app.include_router(org_api.router, prefix="/api/v1")
    with TestClient(app) as client:
        state.client = client
        yield state


@pytest.mark.parametrize("path", [CONNECT, INSTALL])
@pytest.mark.parametrize("org_role", ["owner", "admin"])
@pytest.mark.parametrize("via", ["cookie", "key"])
def test_read_only_connect_managers_cannot_provision_or_issue_install_codes(portal, path, org_role, via):
    portal.user.org_role = org_role
    portal.user.scopes = ("connect:read",)
    portal.user.via = via

    response = portal.client.post(path, json={"links": [{"carrier": "Example carrier"}]})

    assert response.status_code == 403
    assert "Connect write access" in response.json()["detail"]
    assert portal.provisioned == []
    assert portal.tokens == []


@pytest.mark.parametrize("scopes", [None, ("connect",)])
@pytest.mark.parametrize("path", [CONNECT, INSTALL])
def test_connect_managers_keep_provisioning_access(portal, scopes, path):
    portal.user.scopes = scopes

    response = portal.client.post(path, json={"links": [{"carrier": "Example carrier"}]})

    assert response.status_code == 201
    assert len(portal.tokens) == 1


@pytest.mark.parametrize("path", [CONNECT, INSTALL])
@pytest.mark.parametrize("org_role", ["member", "viewer"])
def test_connect_access_does_not_replace_the_organisation_manager_role(portal, path, org_role):
    portal.user.org_role = org_role
    portal.user.scopes = ("connect",)

    assert portal.client.post(path, json={"links": [{"carrier": "Example carrier"}]}).status_code == 403
    assert portal.provisioned == portal.tokens == []


@pytest.mark.parametrize("path", [CONNECT, INSTALL])
def test_an_owner_without_the_connect_app_cannot_provision(portal, path):
    portal.user.apps = ("commai",)

    assert portal.client.post(path, json={"links": [{"carrier": "Example carrier"}]}).status_code == 403
    assert portal.provisioned == portal.tokens == []


def test_read_only_connect_scope_does_not_remove_shared_address_management(portal):
    portal.user.scopes = ("connect:read",)

    response = portal.client.put(
        f"/api/v1/orgs/{ORG}/locations/{LOCATION}", json={"name": "Head office", "city": "Port of Spain"}
    )

    assert response.status_code == 200
    assert portal.addresses[0]["city"] == "Port of Spain"
    assert portal.provisioned == portal.tokens == []


def test_monitoring_owner_cannot_provision_through_real_authentication(client, admin_headers):
    """The /orgs exception for directory sessions must still allow address edits only."""
    created = client.post(
        "/api/v1/admin/organisations",
        json={"name": "Monitoring-only owner", "products": ["connect"], "owner_email": "monitor@example.invalid"},
        headers=admin_headers,
    )
    assert created.status_code == 201
    org = created.json()
    invite = org["invite"]["url"].rsplit("/", 1)[-1]
    client.cookies.clear()
    joined = client.post(
        f"/api/v1/invites/{invite}/accept", json={"name": "Monitoring owner", "password": "a long test password"}
    )
    assert joined.status_code == 200
    headers = {"Authorization": f"Bearer {joined.json()['token']}"}
    client.cookies.clear()
    with db.tx() as conn:
        conn.execute(
            "UPDATE users SET access_scopes = %s WHERE email = %s", (["connect:read"], "monitor@example.invalid")
        )

    location = client.post(f"/api/v1/orgs/{org['id']}/locations", json={"name": "Office address"}, headers=headers)
    assert location.status_code == 201
    response = client.post(
        f"/api/v1/orgs/{org['id']}/locations/{location.json()['id']}/connect",
        json={"links": [{"carrier": "Example carrier"}]},
        headers=headers,
    )
    assert response.status_code == 403
    assert "Connect write access" in response.json()["detail"]
    install = client.post(f"/api/v1/orgs/{org['id']}/sites/{SITE}/install-code", headers=headers)
    assert install.status_code == 403
    assert "Connect write access" in install.json()["detail"]
