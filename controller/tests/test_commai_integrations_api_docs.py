"""Jibsy's published OpenAPI and AsyncAPI documents (ADR 0034): served without
sign-in, structurally valid, complete, and consistent with what is sent."""

from __future__ import annotations

from exaconnect_controller.commai import events
from exaconnect_controller.commai.standards import api_docs, openapi_import


def test_openapi_document_is_valid_and_covers_commai(client):
    r = client.get("/api/v1/commai/openapi.json")
    assert r.status_code == 200, r.text
    doc = r.json()
    assert api_docs.check_openapi(doc) == []
    assert doc["info"]["title"] == "ExaCarib Connect: Jibsy API"
    paths = doc["paths"]
    assert paths and all(p.startswith("/api/v1/commai") for p in paths)
    for p in (
        "/api/v1/commai/customers/{customer_id}/integration-catalogue",
        "/api/v1/commai/customers/{customer_id}/rest-apps",
        "/api/v1/commai/hooks/{token}",
        "/api/v1/commai/openapi.json",
    ):
        assert p in paths, p
    # Everything Jibsy serves is described: compare with the app's own routes.
    full = client.get("/api/v1/openapi.json").json()
    assert {p for p in full["paths"] if p.startswith("/api/v1/commai")} == set(paths)
    # Our own importer reads it: the document is usable by others' tools too.
    ops = openapi_import.operations(openapi_import.parse(doc))
    assert len(ops) >= len(paths)


def test_check_openapi_finds_problems():
    bad = {
        "openapi": "3.1.0",
        "info": {"title": "x"},
        "paths": {
            "/a/{id}": {"get": {"operationId": "a", "responses": {"200": {"$ref": "#/components/schemas/Nope"}}}}
        },
    }
    problems = api_docs.check_openapi(bad)
    assert any("title and version" in p for p in problems)
    assert any("missing schema Nope" in p for p in problems)
    assert any("not declared" in p for p in problems)


def test_asyncapi_document_is_valid_and_lists_every_event(client):
    r = client.get("/api/v1/commai/asyncapi.json")
    assert r.status_code == 200, r.text
    doc = r.json()
    assert api_docs.check_asyncapi(doc) == []
    msgs = doc["components"]["messages"]
    for t in events.TYPES:
        name = t.replace(".", "_")
        assert name in msgs and "ce_" + name in msgs, t
    assert set(doc["components"]["schemas"]["ExaCaribEvent"]["properties"]["type"]["enum"]) == events.TYPES
    assert "webhook-signature" in doc["components"]["schemas"]["DeliveryHeaders"]["properties"]
    assert doc["operations"]["receiveInbound"]["action"] == "receive"
    broken = {**doc, "operations": {"x": {"action": "publish", "channel": {"$ref": "#/channels/nope"}}}}
    assert len(api_docs.check_asyncapi(broken)) == 2
