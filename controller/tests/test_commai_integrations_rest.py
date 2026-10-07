"""A business's own REST API from its OpenAPI document (ADR 0034).

The document is read, never run; actions can only name its operations; a
person approves; the app runs on a stand-in built from the document until
it is switched on and signed in; then real calls (faked HTTP here) carry the
sign-in, an Idempotency-Key, and never create twice."""

from __future__ import annotations

import json

import pytest

from exaconnect_controller.commai import connectors
from exaconnect_controller.commai.standards import openapi_import as oai

from .commai_connector_kit import (  # noqa: F401
    connect_real,
    connect_simulated,
    execute_direct,
    fake,
    go_live,
    propose_and_run,
    real_env,
)
from .commai_helpers import base, business

DOC = {
    "openapi": "3.1.0",
    "info": {"title": "Example Orders API", "version": "2.0"},
    "servers": [{"url": "https://api.orders.example/{v}", "variables": {"v": {"default": "v2"}}}],
    "security": [{"key": []}],
    "components": {
        "securitySchemes": {
            "key": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
            "cc": {
                "type": "oauth2",
                "flows": {
                    "clientCredentials": {
                        "tokenUrl": "https://auth.orders.example/token",
                        "scopes": {"orders.read": "", "orders.write": ""},
                    }
                },
            },
        },
        "schemas": {
            "Order": {
                "type": "object",
                "required": ["customer_email", "amount"],
                "properties": {
                    "id": {"type": "string", "readOnly": True, "example": "ord_1"},
                    "customer_email": {"type": "string", "format": "email"},
                    "amount": {"type": "number", "example": 25},
                    "note": {"type": ["string", "null"], "description": "Free text"},
                },
            }
        },
        "parameters": {"OrderId": {"name": "orderId", "in": "path", "required": True, "schema": {"type": "string"}}},
    },
    "paths": {
        "/orders": {
            "get": {
                "operationId": "listOrders",
                "summary": "List orders",
                "parameters": [{"name": "status", "in": "query", "schema": {"type": "string"}}],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"type": "array", "items": {"$ref": "#/components/schemas/Order"}}
                            }
                        }
                    }
                },
            },
            "post": {
                "operationId": "createOrder",
                "summary": "Create an order",
                "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Order"}}}},
                "responses": {
                    "201": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Order"}}}}
                },
            },
        },
        "/orders/{orderId}": {
            "parameters": [{"$ref": "#/components/parameters/OrderId"}],
            "get": {
                "operationId": "getOrder",
                "summary": "Get an order",
                "responses": {"200": {"content": {"application/json": {"example": {"id": "ord_1", "amount": 25}}}}},
            },
            "delete": {"operationId": "deleteOrder", "summary": "Cancel an order", "responses": {"204": {}}},
        },
    },
}

ACTIONS = [
    {"operation": "list_orders", "name": "list_orders"},
    {"operation": "get_order", "name": "get_order"},
    {"operation": "create_order", "name": "create_order", "mapping": {"email": "customer_email", "notes": "note"}},
    {"operation": "delete_order", "name": "cancel_order"},
]


def test_reading_documents():
    doc = oai.parse(json.dumps(DOC))
    ops = {o["id"]: o for o in oai.operations(doc)}
    assert set(ops) == {"list_orders", "create_order", "get_order", "delete_order"}
    assert ops["create_order"]["kind"] == "create" and ops["delete_order"]["kind"] == "delete"
    assert [b["name"] for b in ops["create_order"]["body"]] == ["customer_email", "amount", "note"]  # id is readOnly
    assert ops["create_order"]["body"][0]["type"] == "email" and ops["create_order"]["body"][1]["required"]
    assert ops["get_order"]["params"][0] == {
        "name": "orderId",
        "in": "path",
        "required": True,
        "type": "string",
        "label": "orderId",
    }
    assert oai.servers(doc) == ["https://api.orders.example/v2"]
    assert {s["type"] for s in oai.security_schemes(doc)} == {"api_key", "oauth2_client_credentials"}
    assert oai.suggest_mapping(ops["create_order"]) == {"email": "customer_email", "amount": "amount", "notes": "note"}
    for bad, why in (
        ({"swagger": "2.0", "info": {"title": "x"}, "paths": {}}, "Swagger 2.0"),
        ({"openapi": "4.0.0", "info": {"title": "x"}, "paths": {"/": {}}}, "3.0 and 3.1"),
        ({"openapi": "3.0.3", "info": {"title": "x"}, "paths": {}}, "no paths"),
    ):
        with pytest.raises(oai.OpenAPIError, match=why):
            oai.parse(bad)
    remote = json.loads(json.dumps(DOC))
    remote["paths"]["/orders"]["post"]["requestBody"] = {"$ref": "https://evil.example/body.json"}
    with pytest.raises(oai.OpenAPIError, match="inside the document"):
        oai.operations(oai.parse(remote))


def _import(client, b, monkeypatch) -> dict:
    monkeypatch.setenv("EXA_WEBHOOK_ALLOW_PRIVATE", "1")
    u, h = base(b), b["agent"]["h"]
    r = client.post(f"{u}/rest-apps", json={"name": "Orders", "document": json.dumps(DOC)}, headers=h)
    assert r.status_code == 201, r.text
    app = r.json()
    assert app["status"] == "draft" and app["base_url"] == "https://api.orders.example/v2"
    # The draft only ever names the document's operations.
    draft = client.post(f"{u}/rest-apps/{app['id']}/draft", headers=h).json()["actions"]
    assert {a["operation"] for a in draft} <= {o["id"] for o in app["operations"]}
    # Invented operations and fields are refused.
    for bad in (
        [{"operation": "drop_database"}],
        [{"operation": "create_order", "mapping": {"email": "nonexistent"}}],
        [{"operation": "get_order", "name": "x"}, {"operation": "list_orders", "name": "x"}],
    ):
        r = client.put(f"{u}/rest-apps/{app['id']}", json={"actions": bad}, headers=h)
        assert r.status_code == 422, r.text
    r = client.put(f"{u}/rest-apps/{app['id']}", json={"auth": {"type": "basic"}}, headers=h)
    assert r.status_code == 422 and "does not declare" in r.text
    r = client.put(
        f"{u}/rest-apps/{app['id']}",
        json={"actions": ACTIONS, "auth": {"type": "api_key", "scheme": "key"}},
        headers=h,
    )
    assert r.status_code == 200, r.text
    # Not in the catalogue until a person approves it.
    cat = client.get(f"{u}/integration-catalogue", headers=h).json()
    assert not [a for c in cat["categories"] for a in c["apps"] if a["app"] == app["app"]]
    r = client.post(f"{u}/rest-apps/{app['id']}/approve", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "approved", r.text
    return app


def test_import_approve_and_run_on_the_stand_in(client, monkeypatch):
    b = business(client, "Orders Ltd", people=("agent",))
    other = business(client, "Orders Other", people=("agent",))
    app = _import(client, b, monkeypatch)
    name = app["app"]

    cat = client.get(f"{base(b)}/integration-catalogue", headers=b["agent"]["h"]).json()
    custom = next(c for c in cat["categories"] if c["id"] == "custom")
    item = next(a for a in custom["apps"] if a["app"] == name)
    assert item["read_actions"] == ["list_orders", "get_order"]
    assert set(item["write_actions"]) == {"create_order", "cancel_order"}
    assert next(a for a in item["actions"] if a["name"] == "cancel_order")["sensitive"]
    other_cat = client.get(f"{base(other)}/integration-catalogue", headers=other["agent"]["h"]).json()
    assert not [a for c in other_cat["categories"] for a in c["apps"] if a["app"] == name]
    assert client.get(f"{base(other)}/rest-apps/{app['id']}", headers=other["agent"]["h"]).status_code == 404

    # Connecting before sign-in runs on the stand-in built from the document.
    r = client.post(f"{base(b)}/integrations/{name}/connect", headers=b["agent"]["h"])
    assert r.status_code == 200, r.text
    assert r.json()["connection"]["auth_method"] == "simulated"
    connect_simulated(b["id"], name, [a["name"] for a in ACTIONS])
    run = propose_and_run(
        b["id"], name, "create_order", {"email": "ann@example.com", "amount": "40", "notes": "Blue"}, "rest-k1"
    )
    assert run["status"] == "succeeded", run
    oid = run["result"]["id"]
    assert run["result"]["data"]["customer_email"] == "ann@example.com" and run["result"]["data"]["amount"] == 40
    again = execute_direct(b["id"], name, "create_order", {"email": "ann@example.com", "amount": 40}, "rest-k1")
    assert again["replayed"] and again["id"] == oid
    got = propose_and_run(b["id"], name, "get_order", {"orderId": oid}, "rest-k2")
    assert got["result"]["data"]["note"] == "Blue"
    # Deletes need a person's approval.
    cancel = propose_and_run(b["id"], name, "cancel_order", {"orderId": oid}, "rest-k3", "user:approver")
    assert cancel["status"] == "succeeded", cancel

    # Changing the actions sends it back to draft and out of the catalogue.
    client.put(f"{base(b)}/rest-apps/{app['id']}", json={"actions": ACTIONS[:1]}, headers=b["agent"]["h"])
    with pytest.raises(KeyError):
        connectors.get(name)


def test_real_calls_sign_in_idempotency_rate_limit_and_isolation(client, real_env, fake, monkeypatch):  # noqa: F811
    b = business(client, "Orders Live", people=("agent",))
    other = business(client, "Orders Spy", people=("agent",))
    app = _import(client, b, monkeypatch)
    name = app["app"]
    go_live(name, b["id"])  # the generic REST connector is switched on for this business
    key = "k-" + "a" * 20
    connect_real(b["id"], name, [a["name"] for a in ACTIONS], creds={"api_key": key})

    created = {"n": 0}

    def create(call):
        created["n"] += 1
        return 201, {"id": "ord_77", **call["body"]}

    fake.on("POST", r"^https://api\.orders\.example/v2/orders$", create)
    run = propose_and_run(b["id"], name, "create_order", {"email": "bo@example.com", "amount": 12}, "live-1")
    assert run["status"] == "succeeded", run
    call = fake.last("POST", r"/orders$")
    assert call["headers"]["X-API-Key"] == key and call["headers"]["Idempotency-Key"] == "live-1"
    assert call["body"] == {"customer_email": "bo@example.com", "amount": 12}
    execute_direct(b["id"], name, "create_order", {"email": "bo@example.com", "amount": 12}, "live-1")
    assert created["n"] == 1

    # Path values cannot leave the operation's path.
    fake.on("GET", r"/orders/", (200, {"id": "x"}))
    propose_and_run(b["id"], name, "get_order", {"orderId": "../admin?x=1"}, "live-2")
    assert fake.last("GET", r"/orders/")["url"] == "https://api.orders.example/v2/orders/..%2Fadmin%3Fx%3D1"

    # A short Retry-After is waited out; a long one is reported.
    answers = iter([(429, {"message": "slow"}, {"Retry-After": "1"}), (200, [])])
    fake.on("GET", r"/orders\?status=open$", lambda c: next(answers))
    assert propose_and_run(b["id"], name, "list_orders", {"status": "open"}, "live-3")["status"] == "succeeded"
    fake.on("GET", r"/orders\?status=late$", (429, {"message": "slow"}, {"Retry-After": "600"}))
    with pytest.raises(connectors.ConnectorError, match="limiting") as e:
        execute_direct(b["id"], name, "list_orders", {"status": "late"}, "live-4")
    assert e.value.cause == "provider"  # the action service retries it later

    # A rejected key names an expired sign-in and marks the connection broken.
    fake.on("GET", r"/orders\?status=x$", (401, {"message": "bad key"}))
    run = propose_and_run(b["id"], name, "list_orders", {"status": "x"}, "live-5")
    assert run["status"] == "failed" and "expired_signin" in run["error"]

    # Another business can't use this business's API, even with a connection row.
    connect_real(other["id"], name, ["list_orders"], creds={"api_key": key})
    with pytest.raises(connectors.ConnectorError, match="another business"):
        execute_direct(other["id"], name, "list_orders", {}, "spy-1")


def test_oauth_client_credentials(client, real_env, fake, monkeypatch):  # noqa: F811
    b = business(client, "Orders OAuth", people=("agent",))
    monkeypatch.setenv("EXA_WEBHOOK_ALLOW_PRIVATE", "1")
    u, h = base(b), b["agent"]["h"]
    app = client.post(f"{u}/rest-apps", json={"name": "Orders CC", "document": DOC}, headers=h).json()
    r = client.put(
        f"{u}/rest-apps/{app['id']}",
        json={"actions": ACTIONS[:1], "auth": {"type": "oauth2_client_credentials", "scopes": ["orders.read"]}},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert client.post(f"{u}/rest-apps/{app['id']}/approve", headers=h).status_code == 200
    name = app["app"]
    go_live(name, b["id"])
    cid, secret = "client-" + "1" * 6, "secret-" + "2" * 10
    connect_real(b["id"], name, ["list_orders"], creds={"client_id": cid, "client_secret": secret})
    fake.on("POST", r"auth\.orders\.example/token$", (200, {"access_token": "at-" + "3" * 8, "expires_in": 3600}))
    fake.on("GET", r"/orders", (200, [{"id": "o1"}]))
    for k in ("cc-1", "cc-2"):
        assert propose_and_run(b["id"], name, "list_orders", {}, k)["status"] == "succeeded"
    assert fake.count("POST", r"/token$") == 1  # cached
    tok = fake.last("POST", r"/token$")
    assert tok["body"] == {"grant_type": "client_credentials", "scope": "orders.read"}
    assert tok["headers"]["Authorization"].startswith("Basic ")
    assert fake.last("GET", r"/orders")["headers"]["Authorization"] == "Bearer at-" + "3" * 8
