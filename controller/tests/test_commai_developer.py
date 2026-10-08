"""Developer platform (ADR 0031): OAuth 2.0 with PKCE for partner apps, sandbox
keys that never send for real, deprecation headers and the changelog."""

from __future__ import annotations

import base64
import hashlib
import secrets
import urllib.parse

import pytest

from exaconnect_controller import db
from exaconnect_controller.commai.channels import providers

from .commai_helpers import base, business, run_jobs, switch_on

P = "/api/v1/commai/partners"
OA = "/api/v1/commai/oauth"
REDIRECT = "https://app.partner.example/callback"


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _partner_app(client, admin_headers, scopes: list[str], confidential: bool = False) -> dict:
    reseller = business(client, f"App Maker {secrets.token_hex(2)}", people=("dev",))
    r = client.post(P, json={"name": f"Apps {secrets.token_hex(3)}", "kind": "msp"}, headers=admin_headers)
    pid = r.json()["id"]
    client.post(f"{P}/{pid}/members", json={"email": reseller["dev"]["email"], "role": "admin"}, headers=admin_headers)
    r = client.post(
        f"{P}/{pid}/oauth-clients",
        json={"name": "Bookings app", "redirect_uris": [REDIRECT], "scopes": scopes, "confidential": confidential},
        headers=reseller["dev"]["h"],
    )
    assert r.status_code == 201, r.text
    return {**r.json(), "pid": pid, "dev": reseller["dev"]}


def _consent(client, headers, app: dict, scope: str, challenge: str, state: str = "s1") -> str:
    body = {
        "client_id": app["client_id"],
        "redirect_uri": REDIRECT,
        "scope": scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "approve": True,
    }
    r = client.post(f"{OA}/authorize", json=body, headers=headers)
    assert r.status_code == 200, r.text
    q = urllib.parse.parse_qs(urllib.parse.urlparse(r.json()["redirect"]).query)
    assert q["state"] == [state]
    return q["code"][0]


def _token(client, app: dict, **form) -> object:
    return client.post(f"{OA}/token", data={"client_id": app["client_id"], **form})


def test_oauth_pkce_scopes_refresh_revoke(client, admin_headers):
    app = _partner_app(client, admin_headers, ["commai:read", "commai:write"])
    b = business(client, "Oauth Shop", people=("owner",))
    v, ch = _pkce()
    q = {"client_id": app["client_id"], "redirect_uri": REDIRECT, "scope": "commai:read"}

    # PKCE is required, S256 only; scopes and redirect URIs are limited to the registration.
    assert client.get(f"{OA}/authorize", params=q, headers=b["owner"]["h"]).status_code == 400
    r = client.get(
        f"{OA}/authorize", params={**q, "code_challenge": v, "code_challenge_method": "plain"}, headers=b["owner"]["h"]
    )
    assert r.status_code == 400 and "S256" in r.json()["detail"]
    full = {**q, "code_challenge": ch, "code_challenge_method": "S256"}
    assert (
        client.get(f"{OA}/authorize", params={**full, "scope": "commai:notes"}, headers=b["owner"]["h"]).status_code
        == 400
    )
    assert (
        client.get(
            f"{OA}/authorize", params={**full, "redirect_uri": "https://evil.example/cb"}, headers=b["owner"]["h"]
        ).status_code
        == 400
    )
    info = client.get(f"{OA}/authorize", params=full, headers=b["owner"]["h"]).json()
    assert info["client"]["name"] == "Bookings app" and info["granted"] == ["commai:read"]

    # Refusing sends the browser back with access_denied.
    r = client.post(f"{OA}/authorize", json={**full, "state": "x", "approve": False}, headers=b["owner"]["h"])
    assert "error=access_denied" in r.json()["redirect"]

    code = _consent(client, b["owner"]["h"], app, "commai:read", ch)
    r = _token(client, app, grant_type="authorization_code", code=code, redirect_uri=REDIRECT, code_verifier=_pkce()[0])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"  # wrong verifier
    r = _token(client, app, grant_type="authorization_code", code=code, redirect_uri=REDIRECT)
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"  # no verifier
    r = _token(client, app, grant_type="authorization_code", code=code, redirect_uri=REDIRECT, code_verifier=v)
    assert r.status_code == 200, r.text
    tok = r.json()
    assert tok["scope"] == "commai:read" and tok["expires_in"] == 3600 and r.headers["cache-control"] == "no-store"
    ah = {"Authorization": f"Bearer {tok['access_token']}"}

    # The token reaches only this business, only with the granted scope.
    assert client.get(f"{base(b)}/conversations", headers=ah).status_code == 200
    assert client.post(f"{base(b)}/contacts", json={"name": "New"}, headers=ah).status_code == 403
    other = business(client, "Not Theirs", people=("owner",))
    assert client.get(f"{base(other)}/conversations", headers=ah).status_code == 403
    assert client.get("/api/v1/customers/mine", headers=ah).status_code == 403

    # Refresh rotates both tokens; the old access token stops.
    r = _token(client, app, grant_type="refresh_token", refresh_token=tok["refresh_token"])
    assert r.status_code == 200, r.text
    tok2 = r.json()
    assert client.get(f"{base(b)}/conversations", headers=ah).status_code == 401
    ah2 = {"Authorization": f"Bearer {tok2['access_token']}"}
    assert client.get(f"{base(b)}/conversations", headers=ah2).status_code == 200
    # A refresh can't widen scopes.
    r = _token(client, app, grant_type="refresh_token", refresh_token=tok2["refresh_token"], scope="commai:write")
    assert r.json()["error"] == "invalid_scope"
    # Replaying the old refresh token ends the whole grant.
    r = _token(client, app, grant_type="refresh_token", refresh_token=tok["refresh_token"])
    assert r.json()["error"] == "invalid_grant"
    assert client.get(f"{base(b)}/conversations", headers=ah2).status_code == 401

    # A code used twice revokes what it produced.
    v3, ch3 = _pkce()
    code3 = _consent(client, b["owner"]["h"], app, "commai:read commai:write", ch3)
    t3 = _token(client, app, grant_type="authorization_code", code=code3, redirect_uri=REDIRECT, code_verifier=v3)
    h3 = {"Authorization": f"Bearer {t3.json()['access_token']}"}
    assert client.post(f"{base(b)}/contacts", json={"name": "Written by app"}, headers=h3).status_code == 201
    again = _token(client, app, grant_type="authorization_code", code=code3, redirect_uri=REDIRECT, code_verifier=v3)
    assert again.json()["error"] == "invalid_grant"
    assert client.get(f"{base(b)}/conversations", headers=h3).status_code == 401

    # RFC 7009 revoke, and the business's own list.
    v4, ch4 = _pkce()
    code4 = _consent(client, b["owner"]["h"], app, "commai:read", ch4)
    t4 = _token(
        client, app, grant_type="authorization_code", code=code4, redirect_uri=REDIRECT, code_verifier=v4
    ).json()
    grants = client.get(f"{base(b)}/oauth-grants", headers=b["owner"]["h"]).json()
    assert [g["app"] for g in grants] == ["Bookings app"]
    assert (
        client.post(f"{OA}/revoke", data={"client_id": app["client_id"], "token": t4["refresh_token"]}).status_code
        == 200
    )
    h4 = {"Authorization": f"Bearer {t4['access_token']}"}
    assert client.get(f"{base(b)}/conversations", headers=h4).status_code == 401
    assert client.get(f"{base(b)}/oauth-grants", headers=b["owner"]["h"]).json() == []

    # Consent needs a person of the business: not a key, not an ExaCarib admin.
    assert client.post(f"{OA}/authorize", json={**full, "approve": True}, headers=h4).status_code == 401
    assert client.post(f"{OA}/authorize", json={**full, "approve": True}, headers=admin_headers).status_code == 403


def test_confidential_client_needs_its_secret(client, admin_headers):
    app = _partner_app(client, admin_headers, ["commai:read"], confidential=True)
    assert app["client_secret"].startswith("ocs_")
    b = business(client, "Secret Shop", people=("owner",))
    v, ch = _pkce()
    code = _consent(client, b["owner"]["h"], app, "commai:read", ch)
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "code_verifier": v}
    r = _token(client, app, client_secret="wrong", **form)
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"
    basic = base64.b64encode(f"{app['client_id']}:{app['client_secret']}".encode()).decode()
    r = client.post(f"{OA}/token", data=form, headers={"Authorization": f"Basic {basic}"})
    assert r.status_code == 200, r.text


def test_sandbox_key_never_sends_for_real(client, monkeypatch):
    b = business(client, "Sandbox Co", people=("owner",))
    u = base(b)
    switch_on("country", "TT")  # SMS destinations start off (ADR 0029)
    assert client.post(f"{u}/sandbox/keys", json={}, headers=b["owner"]["h"]).status_code == 409
    sb = client.post(f"{u}/sandbox", headers=b["owner"]["h"])
    assert sb.status_code == 201, sb.text
    sid = sb.json()["id"]
    r = client.post(f"{u}/sandbox/keys", json={"name": "dev", "scopes": ["connect"]}, headers=b["owner"]["h"])
    assert r.status_code == 422
    r = client.post(f"{u}/sandbox/keys", json={"name": "dev"}, headers=b["owner"]["h"])
    assert r.status_code == 201, r.text
    assert r.json()["sandbox"] is True and r.json()["token"].startswith("exa_sbx_")
    kh = {"Authorization": f"Bearer {r.json()['token']}"}
    sbu = f"/api/v1/commai/customers/{sid}"

    # The key reaches the sandbox only, never the real business.
    assert client.get(f"{u}/conversations", headers=kh).status_code == 403
    assert client.get(f"{sbu}/conversations", headers=kh).status_code == 200

    # Asking for a real provider still gives a simulated account.
    r = client.post(
        f"{sbu}/channel-accounts",
        json={"channel": "sms", "provider": "twilio", "address": "+18685550100"},
        headers=kh,
    )
    assert r.status_code == 201, r.text
    acct = r.json()
    assert acct["provider"] == "simulated"
    with db.tx() as conn:  # even a direct change is held to simulated
        conn.execute("UPDATE channel_accounts SET provider = 'twilio' WHERE id = %s", (acct["id"],))
        assert (
            conn.execute("SELECT provider FROM channel_accounts WHERE id = %s", (acct["id"],)).fetchone()["provider"]
            == "simulated"
        )
    client.patch(f"{sbu}/channel-accounts/{acct['id']}", json={"status": "live"}, headers=kh)

    def boom(*a, **k):
        raise AssertionError("a real provider was called from a sandbox")

    for p in ("twilio", "360dialog"):
        monkeypatch.setattr(providers.get(p), "send_text", boom)
    r = client.post(
        f"{sbu}/channel-accounts/{acct['id']}/simulate-inbound", json={"from": "+18685550199", "body": "Hi"}, headers=kh
    )
    assert r.status_code == 200, r.text
    conv = client.get(f"{sbu}/conversations", headers=kh).json()["items"][0]
    r = client.post(f"{sbu}/conversations/{conv['id']}/messages", json={"body": "Hello from the sandbox"}, headers=kh)
    assert r.status_code == 201, r.text
    run_jobs()
    msgs = client.get(f"{sbu}/conversations/{conv['id']}/messages", headers=kh).json()
    out = [m for m in (msgs["items"] if isinstance(msgs, dict) else msgs) if m["direction"] == "out"]
    assert out and out[-1]["status"] in ("sent", "delivered")

    # No number orders in a sandbox, whatever path is used.
    with pytest.raises(Exception, match="sandbox"), db.tx() as conn:
        conn.execute("INSERT INTO voice_orders (customer_id, items) VALUES (%s, '[]')", (sid,))

    keys = client.get(f"{u}/sandbox", headers=b["owner"]["h"]).json()["keys"]
    assert len(keys) == 1
    assert client.delete(f"{u}/sandbox/keys/{keys[0]['id']}", headers=b["owner"]["h"]).status_code == 204
    assert client.get(f"{sbu}/conversations", headers=kh).status_code == 401


def test_deprecation_headers_and_changelog(client):
    b = business(client, "Dev Shop", people=("owner",))
    r = client.get(f"{base(b)}/event-types", headers=b["owner"]["h"])
    assert r.status_code == 200
    assert r.headers["deprecation"].startswith("@")
    assert r.headers["sunset"] == "Wed, 07 Apr 2027 00:00:00 GMT"
    assert 'rel="successor-version"' in r.headers["link"] and "event-catalogue" in r.headers["link"]
    r = client.get(f"{base(b)}/event-catalogue", headers=b["owner"]["h"])
    assert r.status_code == 200 and "deprecation" not in r.headers
    assert {"type": "webhook.test", "family": "webhook", "webhooks": True} in r.json()
    spec = client.get("/api/v1/openapi.json").json()
    assert spec["paths"]["/api/v1/commai/customers/{customer_id}/event-types"]["get"].get("deprecated") is True
    log = client.get("/api/v1/commai/changelog").json()
    assert any(e["kind"] == "deprecated" and "event-types" in e["change"] for e in log["entries"])
    pol = client.get("/api/v1/commai/api-policy").json()
    assert pol["deprecations"][0]["sunset"] == "2027-04-07"
