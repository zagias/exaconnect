"""Tenant isolation sweep over every CommAI route (acceptance test 7, and the
"every record carries customer_id" rule of ADR 0016).

The routes come from the FastAPI app's own route table, so a route added later
is covered without anyone editing this file:

- every route under /api/v1/commai/customers/{customer_id}/... is called by
  business B's user (and B's API key) with business A's customer_id and A's
  real object ids: the answer must be 403 or 404, never A's data;
- every GET that takes an object id is called again under B's OWN customer_id
  with A's object ids: it must refuse (403/404) or answer without any of A's
  data;
- every route outside a business (ExaCarib administration: go-live, partners,
  support queue, carriers, SMS routes, regions...) is called by a customer
  user and a carrier user: only the routes in the commented allow-list below
  may answer them.
"""

from __future__ import annotations

import json
import re
import uuid

import pytest

from exaconnect_controller import db
from exaconnect_controller.security import hash_password

from .commai_helpers import api_key, base, business

COMMAI = "/api/v1/commai"
CUSTOMER_ROUTE = f"{COMMAI}/customers/{{customer_id}}"
MARK = "ZQXALPHA"  # appears in A's data only; must never show up in an answer to B

# Routes under {customer_id} that are intentionally shared between businesses.
# None today: every per-business route must refuse another business.
SHARED_CUSTOMER_ROUTES: set[tuple[str, str]] = set()

# Routes outside a business that a signed-in customer user may call: each
# answers with published reference material or the caller's own view only.
_DOCS = {
    ("GET", f"{COMMAI}/changelog"),  # the published API changelog
    ("GET", f"{COMMAI}/api-policy"),  # the published API policy
    ("GET", f"{COMMAI}/openapi.json"),  # the published API documents
    ("GET", f"{COMMAI}/asyncapi.json"),
    ("GET", f"{COMMAI}/i18n/catalogues/{{locale}}"),  # interface text in a language
}
_OWN_VIEW = {
    ("GET", f"{COMMAI}/branding/current"),  # the brand the caller's portal shows
    ("GET", f"{COMMAI}/partners"),  # admins: every partner; anyone else: their own membership only
    ("GET", f"{COMMAI}/partners/me"),  # the caller's own partner membership
    ("POST", f"{COMMAI}/partners/switch-back"),  # back to the caller's own account (409 when not switched)
}
SIGNED_IN_OK = (
    _DOCS
    | _OWN_VIEW
    | {
        ("GET", f"{COMMAI}/golive"),  # what is available to the caller's business, not ExaCarib's checks
        ("GET", f"{COMMAI}/regions"),  # hosting regions a business can choose
    }
)
# Carrier accounts never reach CommAI (access.py): only the published
# documents and the caller's own (empty) partner and brand view answer them.
CARRIER_OK = _DOCS | _OWN_VIEW
# Public endpoints: no sign-in at all, guarded by a token, key, signature or
# slug in the path (website widget, provider webhooks, help centre, CSAT
# links, calendar feeds...). Their own tests cover their guards; the
# webhook ones are in test_commai_webhook_security.py.
PUBLIC_PREFIXES = (
    f"{COMMAI}/widget/",
    f"{COMMAI}/channels/hooks/",
    f"{COMMAI}/channels/email/",
    f"{COMMAI}/channels/meta/",
    f"{COMMAI}/channels/telegram/",
    f"{COMMAI}/channels/whatsapp-cloud/",
    f"{COMMAI}/channels/media/",
    f"{COMMAI}/oauth/{{app}}/callback",
    f"{COMMAI}/oauth/token",
    f"{COMMAI}/oauth/revoke",
    f"{COMMAI}/voice/provision/",
    f"{COMMAI}/voice/livekit/",
    f"{COMMAI}/help/",
    f"{COMMAI}/csat/",
    f"{COMMAI}/i18n/widget/",
    f"{COMMAI}/hooks/",
    f"{COMMAI}/integration-hooks/",
    f"{COMMAI}/ical/",
    f"{COMMAI}/branding/host",
    f"{COMMAI}/branding/logo/",
    f"{COMMAI}/whitelabel/tls-ask",
    f"{COMMAI}/slack/interactions",
    f"{COMMAI}/teams/messages",
)


def commai_routes(app) -> list[tuple[str, str]]:
    """(method, path) for every CommAI route in the app's route table."""
    try:  # FastAPI 0.13x+: included routers are resolved lazily
        from fastapi.routing import iter_route_contexts

        routes = list(iter_route_contexts(app.routes))
    except ImportError:  # older FastAPI: a flat list
        routes = list(app.routes)
    out = set()
    for r in routes:
        path = getattr(r, "path", "")
        if not path.startswith(COMMAI):
            continue
        for m in getattr(r, "methods", None) or ():
            if m not in ("HEAD", "OPTIONS"):
                out.add((m, path))
    return sorted(out, key=lambda x: (x[1], x[0]))


def test_route_table_is_read(tmp_path):
    """Guard the sweep itself: if the route table can't be read, it would pass on nothing."""
    from exaconnect_controller.main import create_app
    from exaconnect_controller.settings import Settings

    app = create_app(Settings(database_url="", data_dir=str(tmp_path), proxy_secret="x"))
    routes = commai_routes(app)
    assert len([r for r in routes if r[1].startswith(CUSTOMER_ROUTE)]) > 300
    assert ("GET", f"{CUSTOMER_ROUTE}/conversations/{{conversation_id}}") in routes


# ---- business A's data -----------------------------------------------------------------------------


def _seed_a(client, a: dict) -> dict:
    """Make one of as many kinds of record as possible in business A, each
    carrying MARK. Returns {path parameter name: A's id}."""
    from exaconnect_controller.commai import inbox

    u, h = base(a), a["agent"]["h"]
    ids: dict[str, str] = {}

    def made(param, r, key="id"):
        if r.status_code in (200, 201):
            body = r.json()
            for k in key.split("."):
                body = body[k]
            ids[param] = str(body)

    with db.tx() as conn:
        out = inbox.receive(conn, a["id"], "web", "+18685550199", f"{MARK} card enquiry", name=f"{MARK} Ana")
        conv = out["conversation"]
        ids["conversation_id"] = str(conv["id"])
        ids["contact_id"] = str(conv["contact_id"])
        note = inbox.add_note(conn, a["id"], conv["id"], author="x", body=f"{MARK} private note")
        ids["note_id"] = str(note["id"])
    made("team_id", client.post(f"{u}/teams", json={"name": f"{MARK} team", "members": [a["agent"]["id"]]}, headers=h))
    made(
        "rule_id",
        client.post(
            f"{u}/routing-rules",
            json={"name": f"{MARK} rule", "match": {"keywords": ["card"]}, "team_id": ids.get("team_id")},
            headers=h,
        ),
    )
    made(
        "key_id",
        client.post(
            f"{u}/widget-keys", json={"name": f"{MARK} site", "allowed_origins": ["https://a.example"]}, headers=h
        ),
    )
    made(
        "account_id",
        client.post(
            f"{u}/channel-accounts",
            json={"channel": "sms", "provider": "simulated", "address": "+18685550188", "name": f"{MARK} SMS"},
            headers=h,
        ),
    )
    made(
        "template_id",
        client.post(
            f"{u}/whatsapp-templates",
            json={"name": "zqx_reminder", "category": "utility", "body": f"{MARK} hello {{{{1}}}}"},
            headers=h,
        ),
    )
    made("source_id", client.post(f"{u}/ai/knowledge", json={"title": f"{MARK} kb", "body": f"{MARK} body"}, headers=h))
    made(
        "view_id",
        client.post(f"{u}/saved-views", json={"name": f"{MARK} view", "filters": {"view": "mine"}}, headers=h),
    )
    made("criterion_id", client.post(f"{u}/quality/criteria", json={"text": f"{MARK} greets the customer"}, headers=h))
    made("role_id", client.post(f"{u}/roles", json={"name": f"{MARK} role", "permissions": ["reply"]}, headers=h))
    made(
        "location_id",
        client.post(
            f"{u}/organisation/locations",
            json={"name": f"{MARK} office", "country": "tt", "timezone": "UTC"},
            headers=h,
        ),
    )
    made(
        "holiday_id",
        client.post(
            f"{u}/organisation/holidays", json={"country": "TT", "day": "2026-12-25", "name": f"{MARK} day"}, headers=h
        ),
    )
    made(
        "hook_id",
        client.post(f"{u}/inbound-hooks", json={"name": f"{MARK} hook", "event_type": "order.created"}, headers=h),
    )
    made("case_id", client.post(f"{u}/support-cases", json={"subject": f"{MARK} case"}, headers=h))
    made(
        "chat_id",
        client.post(f"{u}/staff-chat", json={"kind": "direct", "members": [a["agent2"]["email"]]}, headers=h),
    )
    made("endpoint_id", client.post(f"{u}/webhooks", json={"url": "https://hooks.example/zqx"}, headers=h))
    made(
        "identity_id",
        client.post(
            f"{u}/contacts/{ids['contact_id']}/identities",
            json={"kind": "email", "value": "zqx@a.example"},
            headers=h,
        ),
    )
    return ids


def _fill(path: str, ids: dict, customer_id: str) -> str:
    defaults = {
        "app": "sim_calendar",
        "task": "summary",
        "role": "customer_agent",
        "meter": "messages",
        "scope": "business",
        "verb": "approve",
        "n": "1",
        "version": "1",
        "pack": "starter",
        "external_user": "someone",
        "line_id": "1",
    }

    def sub(m):
        name = m.group(1)
        if name == "customer_id":
            return customer_id
        return ids.get(name) or defaults.get(name) or str(uuid.uuid4())

    return re.sub(r"{([a-z_]+)(?::path)?}", sub, path)


def _leaks(text: str, ids: dict) -> list[str]:
    found = [MARK] if MARK in text else []
    found += [k for k, v in ids.items() if k != "customer" and v in text]
    return found


@pytest.fixture
def two_businesses(client, monkeypatch):
    monkeypatch.setenv("EXA_WEBHOOK_ALLOW_PRIVATE", "1")  # webhook hosts need not resolve here
    a = business(client, "Alpha Bank", ("agent", "agent2"))
    b = business(client, "Beta Shop", ("agent",))
    ids = _seed_a(client, a)
    ids["customer"] = a["id"]
    return a, b, ids


QUERY_DEFAULTS = {"q": "card", "country": "TT", "line_id": "1", "to": "+18685550100"}


def _call(client, method, path, headers, ids=None):
    """Call a route; a GET that answers "<param>: Field required" is called
    again with that query parameter filled in (A's id where there is one), so
    the refusal is tested, not the validation."""
    params: dict[str, str] = {}
    for _ in range(4):
        if method == "GET":
            r = client.get(path, params=params, headers=headers)
        elif method == "DELETE":
            r = client.delete(path, headers=headers)
        else:
            return client.request(method, path, json={}, headers=headers)
        m = re.match(r"(\w+): Field required", r.json().get("detail", "") if r.status_code == 422 else "")
        if not m or m.group(1) in params:
            return r
        name = m.group(1)
        params[name] = (ids or {}).get(name) or QUERY_DEFAULTS.get(name) or str(uuid.uuid4())
    return r


# ---- the sweep -----------------------------------------------------------------------------------------


def test_every_business_route_refuses_another_business(client, two_businesses):
    a, b, ids = two_businesses
    assert len(ids) >= 15, ids  # most kinds of record were made
    routes = [r for r in commai_routes(client.app) if r[1].startswith(CUSTOMER_ROUTE + "/")]
    b_key = api_key(client, b["agent"]["h"], None, "b full key")
    carrier = _carrier_user(client)
    bad = []
    writes_checked = 0
    for method, path in routes:
        if (method, path) in SHARED_CUSTOMER_ROUTES:
            continue
        url = _fill(path, ids, a["id"])
        for who, h in (("B user", b["agent"]["h"]), ("B key", b_key), ("carrier", carrier)):
            if method != "GET" and who != "B user":
                continue
            r = _call(client, method, url, h, ids)
            # Reads must be refused outright. A write may also fail body
            # validation (422) before the business is checked; it must never succeed.
            ok = {403, 404} if method == "GET" else {403, 404, 422}
            if r.status_code not in ok or _leaks(r.text, ids):
                bad.append(f"{who} {method} {path} -> {r.status_code} {r.text[:120]}")
            if method != "GET" and r.status_code in (403, 404):
                writes_checked += 1
    assert not bad, "\n".join(bad)
    assert writes_checked > 50  # most writes were refused on the business, not only on their body


def test_a_writes_with_valid_bodies_are_refused_for_another_business(client, two_businesses):
    """A selection of writes with bodies that would succeed for A's own user,
    so the refusal is the business check and not body validation."""
    a, b, ids = two_businesses
    u = base(a)
    conv = ids["conversation_id"]
    writes = [
        ("POST", f"{u}/conversations/{conv}/messages", {"body": "hijack"}),
        ("POST", f"{u}/conversations/{conv}/notes", {"body": "hijack"}),
        ("POST", f"{u}/conversations/{conv}/takeover", None),
        ("POST", f"{u}/conversations/{conv}/assign", {"assignee_id": b["agent"]["id"]}),
        ("POST", f"{u}/conversations/{conv}/state", {"state": "resolved"}),
        ("PATCH", f"{u}/contacts/{ids['contact_id']}", {"name": "hijack"}),
        ("POST", f"{u}/contacts", {"name": "hijack"}),
        ("POST", f"{u}/teams", {"name": "hijack", "members": []}),
        ("PATCH", f"{u}/settings", {"ai_enabled": False}),
        ("POST", f"{u}/webhooks", {"url": "https://hooks.example/b", "events": ["*"]}),
        ("POST", f"{u}/widget-keys", {"name": "x", "allowed_origins": ["https://b.example"]}),
        ("POST", f"{u}/widget-keys/{ids['key_id']}/rotate", None),
        ("POST", f"{u}/ai/knowledge", {"title": "x", "body": "y"}),
        ("POST", f"{u}/roles", {"name": "x", "permissions": ["reply"]}),
        ("POST", f"{u}/support-cases", {"subject": "Leads not created"}),
        ("POST", f"{u}/inbound-hooks", {"name": "x", "event_type": "x"}),
        ("POST", f"{u}/data/exports", {}),
        ("DELETE", f"{u}/teams/{ids['team_id']}", None),
        ("DELETE", f"{u}/saved-views/{ids['view_id']}", None),
    ]
    for method, path, body in writes:
        r = client.request(method, path, json=body, headers=b["agent"]["h"])
        assert r.status_code == 403, f"{method} {path} -> {r.status_code} {r.text[:200]}"
    # A's data is untouched.
    msgs = client.get(f"{u}/conversations/{conv}/messages", headers=a["agent"]["h"]).json()
    assert [m["body"] for m in msgs] == [f"{MARK} card enquiry"]
    assert client.get(f"{u}/saved-views", headers=a["agent"]["h"]).status_code == 200


def test_a_object_ids_under_b_own_business_find_nothing(client, two_businesses):
    a, b, ids = two_businesses
    routes = [
        r
        for r in commai_routes(client.app)
        if r[1].startswith(CUSTOMER_ROUTE + "/") and r[0] == "GET" and re.search(r"{(?!customer_id)[a-z_]+_id}", r[1])
    ]
    assert len(routes) > 30
    bad = []
    for method, path in routes:
        names = re.findall(r"{([a-z_]+)}", path)
        if not any(n in ids for n in names if n != "customer_id"):
            continue  # no A id of that kind to try
        url = _fill(path, ids, b["id"])
        r = client.get(url, headers=b["agent"]["h"])
        if r.status_code < 300 and _leaks(r.text, ids):
            bad.append(f"{method} {path} -> {r.status_code} {r.text[:160]}")
        elif r.status_code >= 500:
            bad.append(f"{method} {path} -> {r.status_code}")
    assert not bad, "\n".join(bad)
    # And a selection of writes with A's ids under B's business change nothing of A's.
    u = base(b)
    conv = ids["conversation_id"]
    for method, path, body in (
        ("POST", f"{u}/conversations/{conv}/messages", {"body": "hijack"}),
        ("POST", f"{u}/conversations/{conv}/notes", {"body": "hijack"}),
        ("POST", f"{u}/conversations/{conv}/takeover", None),
        ("POST", f"{u}/conversations/{conv}/state", {"state": "resolved"}),
        ("PATCH", f"{u}/contacts/{ids['contact_id']}", {"name": "hijack"}),
        ("PUT", f"{u}/teams/{ids['team_id']}", {"name": "hijack", "members": []}),
        ("DELETE", f"{u}/teams/{ids['team_id']}", None),
        ("DELETE", f"{u}/routing-rules/{ids['rule_id']}", None),
        ("POST", f"{u}/widget-keys/{ids['key_id']}/rotate", None),
        ("DELETE", f"{u}/channel-accounts/{ids['account_id']}", None),
        ("DELETE", f"{u}/ai/knowledge/{ids['source_id']}", None),
        ("DELETE", f"{u}/saved-views/{ids['view_id']}", None),
        ("DELETE", f"{u}/roles/{ids['role_id']}", None),
        ("DELETE", f"{u}/inbound-hooks/{ids['hook_id']}", None),
        ("DELETE", f"{u}/organisation/locations/{ids['location_id']}", None),
    ):
        r = client.request(method, path, json=body, headers=b["agent"]["h"])
        assert r.status_code in (403, 404, 422), f"{method} {path} -> {r.status_code} {r.text[:200]}"
    ah = a["agent"]["h"]
    ua = base(a)
    assert [m["body"] for m in client.get(f"{ua}/conversations/{conv}/messages", headers=ah).json()] == [
        f"{MARK} card enquiry"
    ]
    assert any(t["id"] == ids["team_id"] for t in _items(client.get(f"{ua}/teams", headers=ah).json()))
    assert any(v["id"] == ids["view_id"] for v in _items(client.get(f"{ua}/saved-views", headers=ah).json()))


def _items(body):
    return body["items"] if isinstance(body, dict) and "items" in body else body


# ---- ExaCarib administration ---------------------------------------------------------------------------


def _carrier_user(client) -> dict:
    with db.tx() as conn:
        cid = conn.execute("INSERT INTO carriers (name) VALUES ('Sweep Carrier') RETURNING id").fetchone()["id"]
        conn.execute(
            "INSERT INTO users (email, password_hash, role, carrier_id) VALUES (%s, %s, 'carrier', %s)",
            ("noc@sweep-carrier.example", hash_password("carrier password 1"), cid),
        )
    r = client.post("/api/v1/auth/login", json={"email": "noc@sweep-carrier.example", "password": "carrier password 1"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_customer_and_carrier_users_cannot_reach_exacarib_admin_routes(
    client, two_businesses, admin_headers, monkeypatch
):
    # The PBX link set up as on a voice host, so its route refuses on the signature, not for want of a secret.
    monkeypatch.setenv("EXA_PBX_SECRET", "sweep-" + "x" * 24)
    a, b, ids = two_businesses
    carrier = _carrier_user(client)
    routes = [
        r
        for r in commai_routes(client.app)
        if not r[1].startswith(CUSTOMER_ROUTE) and not r[1].startswith(PUBLIC_PREFIXES)
    ]
    assert len(routes) > 40
    bad = []
    for method, path in routes:
        url = _fill(path, {**ids, "kind": "channel", "key": "telegram", "locale": "fr"}, a["id"])
        for who, h, allowed in (("customer", b["agent"]["h"], SIGNED_IN_OK), ("carrier", carrier, CARRIER_OK)):
            if (method, path) in allowed:
                continue
            r = _call(client, method, url, h, ids)
            ok = {401, 403, 404} if method == "GET" else {401, 403, 404, 422}
            if r.status_code not in ok or _leaks(r.text, ids):
                bad.append(f"{who} {method} {path} -> {r.status_code} {r.text[:120]}")
    assert not bad, "\n".join(bad)

    # The named admin areas, explicitly, with valid bodies: refused for both, allowed for an admin.
    named = [
        ("GET", f"{COMMAI}/golive/channel/telegram"),
        ("PUT", f"{COMMAI}/golive/channel/telegram/status"),
        ("POST", f"{COMMAI}/partners"),
        ("GET", f"{COMMAI}/exacarib/support/cases"),
        ("GET", f"{COMMAI}/voice-admin/carriers"),
        ("GET", f"{COMMAI}/sms-carriers"),
        ("GET", f"{COMMAI}/sms-routes"),
        ("PUT", f"{base(a)}/entitlements"),
    ]
    bodies = {
        "PUT golive": {"status": "on"},
        "POST partners": {"name": "Rogue partner", "kind": "reseller"},
        "PUT entitlements": {"modules": {"voice": False}},
    }
    for method, path in named:
        body = next((v for k, v in bodies.items() if k.split()[0] == method and k.split()[1] in path), {})
        for h in (a["agent"]["h"], carrier):
            r = client.request(method, path, json=body, headers=h)
            assert r.status_code == 403, f"{method} {path} -> {r.status_code} {r.text[:200]}"
    for method, path in named:
        if method == "GET":
            assert client.get(path, headers=admin_headers).status_code == 200, path
    # Nothing changed for A.
    ents = client.get(f"{base(a)}/entitlements", headers=a["agent"]["h"])
    assert ents.status_code == 200 and json.dumps(ents.json()).count("false") == 0
