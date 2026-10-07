"""Partners and white-label (ADR 0025): links the business accepts and revokes,
switching with granted scopes only, tenant isolation, statements, branding
validation and custom-domain verification."""

from __future__ import annotations

import struct
import zlib

from exaconnect_controller import db
from exaconnect_controller.commai import branding

from .commai_helpers import base, business

P = "/api/v1/commai/partners"


def _png(w: int = 64, h: int = 32) -> bytes:
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + chunk + struct.pack(">I", zlib.crc32(chunk)) + b"\x00" * 20


def _setup(client, admin_headers, markup: float = 20) -> dict:
    """A reseller with an admin and a member, and three businesses."""
    reseller = business(client, "Island Reseller", people=("boss", "staff"))
    r = client.post(P, json={"name": "Island Reseller", "kind": "reseller", "markup_pct": markup}, headers=admin_headers)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    for who, role in (("boss", "admin"), ("staff", "member")):
        r = client.post(
            f"{P}/{pid}/members", json={"email": reseller[who]["email"], "role": role}, headers=admin_headers
        )
        assert r.status_code == 201, r.text
    a = business(client, "Alpha Bakery", people=("owner", "clerk"))
    b = business(client, "Beta Garage", people=("owner",))
    c = business(client, "Gamma Hotel", people=("owner",))
    return {"pid": pid, "reseller": reseller, "a": a, "b": b, "c": c}


def _link(client, s: dict, cust: dict, scopes: list[str]) -> str:
    r = client.post(
        f"{P}/{s['pid']}/links",
        json={"customer_id": cust["id"], "scopes": scopes, "note": "We look after your chat."},
        headers=s["reseller"]["boss"]["h"],
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _switch(client, s: dict, cust: dict, who: str = "boss"):
    return client.post(f"{P}/switch", json={"customer_id": cust["id"]}, headers=s["reseller"][who]["h"])


def test_link_accept_switch_isolation_and_revoke(client, admin_headers):
    s = _setup(client, admin_headers)
    a, b, c = s["a"], s["b"], s["c"]

    # Only the partner's admins request links; members cannot.
    r = client.post(
        f"{P}/{s['pid']}/links",
        json={"customer_id": a["id"], "scopes": ["commai:read"]},
        headers=s["reseller"]["staff"]["h"],
    )
    assert r.status_code == 403
    la = _link(client, s, a, ["commai:read", "commai:write", "commai:notes"])
    lb = _link(client, s, b, ["commai:read"])
    assert client.post(  # a second open request to the same business is refused
        f"{P}/{s['pid']}/links", json={"customer_id": a["id"], "scopes": ["commai:read"]},
        headers=s["reseller"]["boss"]["h"],
    ).status_code == 409  # fmt: skip

    # Nothing works before the business accepts.
    assert _switch(client, s, a).status_code == 403

    # The business sees its own request only, and accepts with fewer scopes.
    mine = client.get(f"{base(a)}/partner-links", headers=a["owner"]["h"]).json()
    assert [x["partner_name"] for x in mine] == ["Island Reseller"]
    assert client.get(f"{base(b)}/partner-links", headers=a["owner"]["h"]).status_code == 403
    r = client.post(
        f"{base(a)}/partner-links/{la}/accept", json={"scopes": ["commai:read", "commai:admin"]}, headers=a["owner"]["h"]
    )
    assert r.status_code == 422  # can't grant what wasn't asked for
    r = client.post(
        f"{base(a)}/partner-links/{la}/accept", json={"scopes": ["commai:read", "commai:write"]}, headers=a["owner"]["h"]
    )
    assert r.status_code == 200 and r.json()["status"] == "active", r.text
    assert client.post(f"{base(b)}/partner-links/{lb}/decline", headers=b["owner"]["h"]).json()["status"] == "declined"

    # Consolidated view: the linked business with health and usage; the declined one without; never Gamma.
    view = client.get(f"{P}/{s['pid']}/customers", headers=s["reseller"]["staff"]["h"]).json()
    names = {v["customer_name"]: v for v in view}
    assert set(names) == {"Alpha Bakery", "Beta Garage"}
    assert names["Alpha Bakery"]["health"]["state"] in ("ok", "warn", "bad")
    assert names["Alpha Bakery"]["usage"]["markup_pct"] == 20
    assert names["Beta Garage"]["health"] is None and names["Beta Garage"]["usage"] is None
    # A business never sees the partner's view; another partner's people can't either.
    assert client.get(f"{P}/{s['pid']}/customers", headers=a["owner"]["h"]).status_code == 403

    # Switch into Alpha: only Alpha, only the granted scopes.
    r = _switch(client, s, a, "staff")
    assert r.status_code == 200, r.text
    assert sorted(r.json()["scopes"]) == ["commai:read", "commai:write"]
    dh = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.get(f"{base(a)}/conversations", headers=dh).status_code == 200
    for other in (b, c, s["reseller"]):
        assert client.get(f"{base(other)}/conversations", headers=dh).status_code == 403
    assert client.get("/api/v1/customers/mine", headers=dh).status_code == 403  # no Connect scope granted
    r = client.post(f"{base(a)}/conversations", json={"channel": "api", "body": "x"}, headers=dh)
    conv = client.get(f"{base(a)}/conversations", headers=a["owner"]["h"]).json()
    # Notes were not granted.
    if conv["items"]:
        cid = conv["items"][0]["id"]
        assert client.get(f"{base(a)}/conversations/{cid}/notes", headers=dh).status_code == 403
    me = client.get(f"{P}/me", headers=dh).json()
    assert me["acting_for"]["customer_name"] == "Alpha Bakery" and me["partner"] is None

    # A delegate can't change the link or approve anything for the business.
    assert client.delete(f"{base(a)}/partner-links/{la}", headers=dh).status_code == 403
    assert client.put(
        f"{base(a)}/partner-links/{la}/scopes", json={"scopes": ["commai:read"]}, headers=dh
    ).status_code == 403  # fmt: skip

    # Can't switch to a declined or unlinked business.
    assert _switch(client, s, b).status_code == 403
    assert _switch(client, s, c).status_code == 403
    # A business's own people are not partner people.
    assert client.post(f"{P}/switch", json={"customer_id": a["id"]}, headers=c["owner"]["h"]).status_code == 403

    # An API key made by the delegate can't switch back to the partner account.
    r = client.post("/api/v1/auth/api-keys", json={"name": "d", "scopes": ["commai:read"]}, headers=dh)
    assert r.status_code == 201, r.text
    kh = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.post(f"{P}/switch-back", headers=kh).status_code == 403
    r = client.post(f"{P}/switch-back", headers=dh)
    assert r.status_code == 200 and r.json()["email"] == s["reseller"]["staff"]["email"]
    back = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.get(f"{P}/me", headers=back).json()["partner"]["name"] == "Island Reseller"
    assert client.get(f"{base(a)}/conversations", headers=dh).status_code == 401  # that session ended

    # The business narrows the scopes: it applies at once.
    dh = {"Authorization": f"Bearer {_switch(client, s, a, 'staff').json()['token']}"}
    r = client.put(f"{base(a)}/partner-links/{la}/scopes", json={"scopes": ["commai:read"]}, headers=a["owner"]["h"])
    assert r.status_code == 200
    r = client.post(f"{base(a)}/contacts", json={"name": "Someone"}, headers=dh)
    assert r.status_code == 403

    # The business revokes: delegate sessions and keys stop at once.
    r = client.delete(f"{base(a)}/partner-links/{la}", headers=a["owner"]["h"])
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    assert client.get(f"{base(a)}/conversations", headers=dh).status_code == 401
    assert client.get(f"{base(a)}/conversations", headers=kh).status_code == 401
    assert _switch(client, s, a).status_code == 403
    view = client.get(f"{P}/{s['pid']}/customers", headers=s["reseller"]["boss"]["h"]).json()
    assert all(v["usage"] is None for v in view)

    with db.tx() as conn:
        acts = {
            r["action"]
            for r in conn.execute("SELECT action FROM audit_log WHERE action LIKE 'commai.partner.%'").fetchall()
        }
    assert {"commai.partner.switch", "commai.partner.link.accept", "commai.partner.link.revoke"} <= acts


def test_markup_statements(client, admin_headers):
    s = _setup(client, admin_headers, markup=25)
    a = s["a"]
    la = _link(client, s, a, ["commai:read"])
    client.post(f"{base(a)}/partner-links/{la}/accept", json={}, headers=a["owner"]["h"])
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO usage_records (customer_id, meter, quantity, ref) VALUES (%s, 'ai_reply', 100, 't1')",
            (a["id"],),
        )
    month = client.get(f"{P}/{s['pid']}/customers", headers=s["reseller"]["boss"]["h"]).json()[0]["usage"]
    assert month["example_total"] == 2.0 and month["example_total_with_markup"] == 2.5
    import datetime as dt

    ym = dt.datetime.now(dt.UTC).strftime("%Y-%m")
    r = client.post(f"{P}/{s['pid']}/statements", json={"month": ym}, headers=s["reseller"]["staff"]["h"])
    assert r.status_code == 403  # admins of the partner only
    r = client.post(f"{P}/{s['pid']}/statements", json={"month": ym}, headers=s["reseller"]["boss"]["h"])
    assert r.status_code == 201, r.text
    row = r.json()[0]
    assert float(row["base_amount"]) == 2.0 and float(row["total_amount"]) == 2.5 and float(row["markup_pct"]) == 25
    # A per-business markup overrides the partner's.
    client.patch(f"{P}/{s['pid']}/links/{la}", json={"markup_pct": 10}, headers=s["reseller"]["boss"]["h"])
    rows = client.post(f"{P}/{s['pid']}/statements", json={"month": ym}, headers=s["reseller"]["boss"]["h"]).json()
    assert float(rows[0]["total_amount"]) == 2.2
    assert len(client.get(f"{P}/{s['pid']}/statements", headers=s["reseller"]["staff"]["h"]).json()) == 1


def test_branding_validation_and_runtime(client, admin_headers):
    s = _setup(client, admin_headers)
    a, c = s["a"], s["c"]
    boss = s["reseller"]["boss"]["h"]
    url = f"{P}/{s['pid']}/branding"
    good = {"product_name": "Island Connect", "colour": "#0B5D3B", "support_email": "help@island.example"}

    assert branding.contrast("#155EEF", "#FFFFFF") >= 4.5  # ExaCarib blue passes its own rule
    for bad in (
        {**good, "colour": "#FFD400"},  # yellow on white: too little contrast
        {**good, "colour": "green"},
        {**good, "ink": "#A0A0A0"},
        {**good, "product_name": "ExaCarib Plus"},
        {**good, "support_email": "not-an-email"},
    ):
        r = client.put(url, json=bad, headers=boss)
        assert r.status_code == 422, (bad, r.text)
    assert client.put(url, json=good, headers=s["reseller"]["staff"]["h"]).status_code == 403  # admins only
    r = client.put(url, json=good, headers=boss)
    assert r.status_code == 200, r.text
    brand_id = r.json()["id"]

    # Logo: type, bytes and size are checked.
    logo = f"/api/v1/commai/brands/{brand_id}/logo"
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
    assert client.put(logo, content=svg, headers={**boss, "Content-Type": "image/svg+xml"}).status_code == 415
    assert client.put(logo, content=b"\xff\xd8\xff" + b"0" * 50, headers={**boss, "Content-Type": "image/png"}).status_code == 415  # fmt: skip
    big = _png() + b"\x00" * (300 * 1024)
    assert client.put(logo, content=big, headers={**boss, "Content-Type": "image/png"}).status_code == 413
    assert client.put(logo, content=_png(5000, 10), headers={**boss, "Content-Type": "image/png"}).status_code == 422
    r = client.put(logo, content=_png(), headers={**boss, "Content-Type": "image/png"})
    assert r.status_code == 200, r.text
    got = client.get(r.json()["logo_url"])
    assert got.status_code == 200 and got.content == _png() and got.headers["x-content-type-options"] == "nosniff"
    assert client.put(logo, content=_png(), headers={**a["owner"]["h"], "Content-Type": "image/png"}).status_code == 403

    # Runtime: partner people and linked businesses get the brand; others ExaCarib's.
    la = _link(client, s, a, ["commai:read", "commai:admin"])
    client.post(f"{base(a)}/partner-links/{la}/accept", json={}, headers=a["owner"]["h"])
    cur = "/api/v1/commai/branding/current"
    assert client.get(cur, headers=boss).json()["product_name"] == "Island Connect"
    assert client.get(cur, headers=a["clerk"]["h"]).json()["colour"] == "#0B5D3B"
    assert client.get(cur, headers=c["owner"]["h"]).json() is None
    assert client.get(cur, headers=admin_headers).json() is None

    # The widget uses the business's (here, its partner's) brand.
    r = client.post(
        f"{base(a)}/widget-keys", json={"allowed_origins": ["https://alpha.example"]}, headers=a["owner"]["h"]
    )
    assert r.status_code == 201, r.text
    pk = r.json()["public_key"]
    cfg = client.get(f"/api/v1/commai/widget/{pk}/config", headers={"Origin": "https://alpha.example"}).json()
    assert cfg["brand"]["product_name"] == "Island Connect" and cfg["colour"] == "#0B5D3B"

    # Per-business branding: only once a partner manages the business.
    own = {"product_name": "Alpha Chat", "colour": "#7A1F5C"}
    assert client.put(f"{base(c)}/branding", json=own, headers=c["owner"]["h"]).status_code == 403
    assert client.put(f"{base(a)}/branding", json=own, headers=a["owner"]["h"]).status_code == 200
    assert client.get(cur, headers=a["clerk"]["h"]).json()["product_name"] == "Alpha Chat"
    eff = client.get(f"{base(a)}/branding", headers=a["clerk"]["h"]).json()
    assert eff["effective"]["product_name"] == "Alpha Chat" and eff["partner_managed"] is True


def test_custom_domain_verification(client, admin_headers):
    s = _setup(client, admin_headers)
    boss = s["reseller"]["boss"]["h"]
    r = client.put(f"{P}/{s['pid']}/branding", json={"product_name": "Island Connect", "colour": "#0B5D3B"}, headers=boss)
    brand_id = r.json()["id"]
    durl = f"/api/v1/commai/brands/{brand_id}/domains"

    for bad in ("exacarib.com", "portal.exacarib.com", "localhost", "10.0.0.1", "bad_name.example", "x.invalid"):
        assert client.post(durl, json={"purpose": "portal", "domain": bad}, headers=boss).status_code == 422, bad
    r = client.post(durl, json={"purpose": "portal", "domain": "Portal.Island.example."}, headers=boss)
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["domain"] == "portal.island.example" and d["status"] == "pending"
    assert d["dns"]["name"] == "_exacarib-challenge.portal.island.example"
    assert client.post(durl, json={"purpose": "widget", "domain": "portal.island.example"}, headers=boss).status_code == 409

    ask = "/api/v1/commai/whitelabel/tls-ask"
    assert client.get(ask, params={"domain": "portal.island.example"}).status_code == 404

    # Not published yet, then a wrong value: still pending, with the reason.
    r = client.post(f"{durl}/{d['id']}/verify", headers=boss)
    assert r.json()["status"] == "pending" and "No TXT record" in r.json()["last_error"]
    sim = "/api/v1/commai/whitelabel/simulated-dns"
    assert client.put(sim, json={"name": d["dns"]["name"], "value": "x"}, headers=boss).status_code == 403
    client.put(sim, json={"name": d["dns"]["name"], "value": "exacarib-verify=wrong"}, headers=admin_headers)
    assert client.post(f"{durl}/{d['id']}/verify", headers=boss).json()["status"] == "pending"
    client.put(sim, json={"name": d["dns"]["name"], "value": d["dns"]["value"]}, headers=admin_headers)
    r = client.post(f"{durl}/{d['id']}/verify", headers=boss)
    assert r.json()["status"] == "verified", r.text

    # Only now does Caddy get a yes, and the sign-in page on that host gets the brand.
    assert client.get(ask, params={"domain": "portal.island.example"}).status_code == 200
    assert client.get(ask, params={"domain": "other.example"}).status_code == 404
    hb = client.get("/api/v1/commai/branding/host", headers={"X-Forwarded-Host": "portal.island.example"}).json()
    assert hb["product_name"] == "Island Connect"
    assert client.get("/api/v1/commai/branding/host", headers={"X-Forwarded-Host": "nope.example"}).json() is None

    # Nobody else can see or change this brand's domains.
    other = business(client, "Other Firm", people=("owner",))
    assert client.get(durl, headers=other["owner"]["h"]).status_code == 403
    assert client.post(f"{durl}/{d['id']}/verify", headers=other["owner"]["h"]).status_code == 403
