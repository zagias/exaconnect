"""HubSpot, extended for ADR 0028: update_contact (PATCH) through the action
service, and app webhooks checked with X-HubSpot-Signature-v3. HTTP is faked;
the signature is computed here from HubSpot's published recipe, not our code."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse

from exaconnect_controller import db
from exaconnect_controller.commai import connectors

from .commai_connector_kit import connect_real, fake, propose_and_run, real_env  # noqa: F401
from .commai_helpers import base, business


def hubspot_sign(secret: str, url: str, body: bytes, ts_ms: int) -> dict:
    mac = hmac.new(secret.encode(), f"POST{url}".encode() + body + str(ts_ms).encode(), hashlib.sha256)
    return {
        "X-HubSpot-Signature-v3": base64.b64encode(mac.digest()).decode(),
        "X-HubSpot-Request-Timestamp": str(ts_ms),
    }


def test_update_contact_patch_and_errors(client, real_env, fake):  # noqa: F811
    b = business(client, "HubSpot Update", people=("agent",))
    connect_real(b["id"], "hubspot", ["update_contact"], token="tok-" + "x" * 8)
    fake.on("PATCH", r"/crm/v3/objects/contacts/101$", (200, {"id": "101", "properties": {"phone": "+1246555"}}))
    run = propose_and_run(
        b["id"], "hubspot", "update_contact", {"contact_id": "101", "phone": "+1246555"}, "k-upd", "user:approver"
    )
    assert run["status"] == "succeeded", run
    assert run["result"]["updated"] and run["result"]["contact_id"] == "101"
    sent = fake.last("PATCH", r"contacts/101")
    assert sent["body"] == {"properties": {"phone": "+1246555"}}
    assert sent["headers"]["Authorization"].startswith("Bearer tok-")

    # A contact HubSpot doesn't have is an input problem; an expired sign-in is named.
    fake.on("PATCH", r"/contacts/999$", (404, {"message": "Object not found", "category": "OBJECT_NOT_FOUND"}))
    run = propose_and_run(b["id"], "hubspot", "update_contact", {"contact_id": "999"}, "k-404", "user:approver")
    assert run["status"] == "failed" and "404" in (run["error"] or "")
    fake.on("PATCH", r"/contacts/102$", (401, {"message": "expired", "category": "EXPIRED_AUTHENTICATION"}))
    run = propose_and_run(b["id"], "hubspot", "update_contact", {"contact_id": "102"}, "k-401", "user:approver")
    assert run["status"] == "failed"
    with db.tx() as conn:
        row = conn.execute(
            "SELECT status, last_error FROM integration_connections WHERE customer_id = %s AND app = 'hubspot'",
            (b["id"],),
        ).fetchone()
    assert row["status"] == "broken"
    assert "expired_signin" in run["error"]


def test_hubspot_webhook_v3_good_bad_stale_and_isolated(client, real_env, monkeypatch):  # noqa: F811
    secret = "hs-secret-" + "y" * 12
    monkeypatch.setenv("EXA_HUBSPOT_CLIENT_SECRET", secret)
    seen: list[dict] = []
    monkeypatch.setattr(
        connectors.get("hubspot"), "on_webhook_event", lambda conn, c, ev: seen.append(ev), raising=False
    )
    b = business(client, "HubSpot Hooks", people=("agent",))
    other = business(client, "HubSpot Other", people=("agent",))
    connect_real(b["id"], "hubspot", ["find_contact"])
    hook = client.post(f"{base(b)}/integrations/hubspot/hook", headers=b["agent"]["h"])
    assert hook.status_code == 200, hook.text
    path = urllib.parse.urlsplit(hook.json()["url"]).path
    public = "https://connect.example.org" + path  # what HubSpot called (EXA_PUBLIC_URL)
    body = json.dumps(
        [{"eventId": 77, "subscriptionType": "contact.propertyChange", "objectId": 101, "propertyName": "phone"}]
    ).encode()
    now = int(time.time() * 1000)

    ok = client.post(
        path, content=body, headers={"Content-Type": "application/json", **hubspot_sign(secret, public, body, now)}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["events"] == 1
    again = client.post(
        path, content=body, headers={"Content-Type": "application/json", **hubspot_sign(secret, public, body, now)}
    )
    assert again.json()["events"] == 0
    assert [e["id"] for e in seen] == ["77"]  # the connector acts once per verified event

    bad = client.post(path, content=body, headers=hubspot_sign("wrong-secret", public, body, now))
    stale = client.post(path, content=body, headers=hubspot_sign(secret, public, body, now - 600_000))
    other_url = client.post(path, content=body, headers=hubspot_sign(secret, public + "x", body, now))
    assert bad.status_code == stale.status_code == other_url.status_code == 401

    with db.tx() as conn:
        mine = conn.execute(
            "SELECT data FROM commai_events WHERE customer_id = %s AND type = 'integration.event'", (b["id"],)
        ).fetchall()
        theirs = conn.execute(
            "SELECT count(*) AS n FROM commai_events WHERE customer_id = %s AND type = 'integration.event'",
            (other["id"],),
        ).fetchone()["n"]
    assert len(mine) == 1 and mine[0]["data"]["type"] == "hubspot.contact.propertyChange"
    assert theirs == 0
