"""Enterprise administration (ADR 0024): business calendars, roles and
per-team permissions, security settings, unusual-use protection."""

from __future__ import annotations

import datetime as dt
import time

from exaconnect_controller import db
from exaconnect_controller.commai import inbox
from exaconnect_controller.commai.enterprise import protect, security
from exaconnect_controller.identity import totp

from .commai_helpers import PASSWORD, api_key, base, business, run_jobs
from .test_commai_channels import _account, _conv_for, _wa_inbound


def _code(secret: str) -> str:
    return totp.totp(totp.decode(secret), time.time())


def _enrol(client, h) -> str:
    secret = client.post("/api/v1/auth/two-step/start", headers=h).json()["secret"]
    r = client.post("/api/v1/auth/two-step/confirm", json={"code": _code(secret)}, headers=h)
    assert r.status_code == 200, r.text
    return secret


def _login(client, email, ip=None):
    headers = {"X-Forwarded-For": ip} if ip else {}
    return client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}, headers=headers)


def _inbound(b, body="Hello", address="+18685550101"):
    with db.tx() as conn:
        return inbox.receive(conn, b["id"], "web", address, body, name="Ana")["conversation"]


# ---- organisation and business calendars ---------------------------------------------------


def test_service_targets_count_business_time_only(client):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    monday_1630 = "2026-10-05T16:30:00+00:00"  # a Monday
    # No locations: targets run on the clock, as before.
    r = client.get(f"{u}/organisation/due", params={"minutes": 60, "start": monday_1630}, headers=h)
    assert r.json()["due"] == r.json()["clock_due"]

    loc = client.post(
        f"{u}/organisation/locations", json={"name": "Port of Spain", "country": "tt", "timezone": "UTC"}, headers=h
    )
    assert loc.status_code == 201, loc.text
    assert loc.json()["is_primary"] and loc.json()["country"] == "TT"
    lid = loc.json()["id"]
    hours = [{"weekday": d, "opens": "09:00", "closes": "17:00"} for d in range(5)]
    assert client.put(f"{u}/organisation/locations/{lid}/hours", json=hours, headers=h).status_code == 200
    due = lambda: client.get(f"{u}/organisation/due", params={"minutes": 60, "start": monday_1630}, headers=h).json()  # noqa: E731
    # 30 minutes on Monday, the other 30 when Tuesday opens.
    assert due()["due"].startswith("2026-10-06T09:30")
    # A public holiday in that location's country moves it to Wednesday...
    r = client.post(
        f"{u}/organisation/holidays", json={"country": "TT", "day": "2026-10-06", "name": "Example holiday"}, headers=h
    )
    assert r.status_code == 201, r.text
    # ...a holiday in another country does not.
    client.post(f"{u}/organisation/holidays", json={"country": "JM", "day": "2026-10-07"}, headers=h)
    assert due()["due"].startswith("2026-10-07T09:30")
    # A closure on Wednesday morning pushes it past the closure.
    r = client.post(
        f"{u}/organisation/closures",
        json={"starts_at": "2026-10-07T09:00:00Z", "ends_at": "2026-10-07T12:00:00Z", "reason": "Storm"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert due()["due"].startswith("2026-10-07T12:30")
    # Bad time zones are refused; carriers of data are audited.
    r = client.post(f"{u}/organisation/locations", json={"name": "X", "timezone": "Mars/Base"}, headers=h)
    assert r.status_code == 422
    org = client.get(f"{u}/organisation", headers=h).json()
    assert len(org["locations"]) == 1 and len(org["locations"][0]["hours"]) == 5 and len(org["holidays"]) == 2
    with db.tx() as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM audit_log WHERE customer_id = %s AND action LIKE 'commai.org.%%'", (b["id"],)
        ).fetchone()["n"]
    assert n >= 5


def test_routing_uses_the_calendar_after_hours(client):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    day = client.post(f"{u}/teams", json={"name": "Day", "members": [b["agent"]["id"]]}, headers=h).json()
    night = client.post(f"{u}/teams", json={"name": "Night", "members": [b["agent2"]["id"]]}, headers=h).json()
    client.post(
        f"{u}/routing-rules", json={"name": "All web", "match": {"channel": "web"}, "team_id": day["id"]}, headers=h
    )
    office = client.post(f"{u}/organisation/locations", json={"name": "Office", "timezone": "UTC"}, headers=h).json()
    remote = client.post(f"{u}/organisation/locations", json={"name": "Remote", "timezone": "UTC"}, headers=h).json()
    assert client.put(f"{u}/organisation/teams/{day['id']}", json={"location_id": office["id"]}, headers=h).is_success
    assert client.put(f"{u}/organisation/teams/{night['id']}", json={"location_id": remote["id"]}, headers=h).is_success
    now = dt.datetime.now(dt.UTC)
    client.post(
        f"{u}/organisation/closures",
        json={
            "location_id": office["id"],
            "starts_at": (now - dt.timedelta(hours=1)).isoformat(),
            "ends_at": (now + dt.timedelta(hours=2)).isoformat(),
            "reason": "Closed for the storm",
        },
        headers=h,
    )
    # Closed, no after-hours team: it waits in the team's queue, nobody assigned, target paused.
    conv = _inbound(b, address="+18685550111")
    assert str(conv["team_id"]) == day["id"] and conv["assignee_id"] is None
    assert conv["first_reply_due"] > now + dt.timedelta(hours=2)
    # With an after-hours team, it goes there and someone there gets it.
    r = client.put(
        f"{u}/organisation/locations/{office['id']}",
        json={"name": "Office", "timezone": "UTC", "is_primary": True, "after_hours_team_id": night["id"]},
        headers=h,
    )
    assert r.status_code == 200, r.text
    conv = _inbound(b, address="+18685550112")
    assert str(conv["team_id"]) == night["id"] and str(conv["assignee_id"]) == b["agent2"]["id"]
    with db.tx() as conn:
        log = conn.execute(
            "SELECT reason FROM conversation_log WHERE conversation_id = %s AND kind = 'assign'", (conv["id"],)
        ).fetchone()
    assert "outside hours at Office" in log["reason"]


# ---- roles ------------------------------------------------------------------------------------


def _role(client, b, name, perms, h=None):
    r = client.post(f"{base(b)}/roles", json={"name": name, "permissions": perms}, headers=h or b["agent"]["h"])
    assert r.status_code == 201, r.text
    return r.json()


def _assign(client, b, who, role, team_id=None, h=None):
    return client.post(
        f"{base(b)}/roles/assignments",
        json={"user_id": b[who]["id"], "role_id": role["id"], "team_id": team_id},
        headers=h or b["agent"]["h"],
    )


def test_custom_role_without_notes_cannot_read_notes(client):
    b = business(client)
    u = base(b)
    conv = _inbound(b)
    with db.tx() as conn:
        inbox.add_note(conn, b["id"], conv["id"], author="user:x", body="Private: card was stolen")
    listing = client.get(f"{u}/roles", headers=b["agent"]["h"]).json()
    assert {r["key"] for r in listing["roles"] if r["builtin"]} >= {"business_admin", "agent", "internal", "viewer"}
    assert len(listing["permissions"]) == 11
    r = client.post(f"{u}/roles", json={"name": "Bad", "permissions": ["fly"]}, headers=b["agent"]["h"])
    assert r.status_code == 422

    reader = _role(client, b, "Front desk", ["read_inbox", "reply"])
    assert _assign(client, b, "agent2", reader).status_code == 201
    h2 = b["agent2"]["h"]
    assert client.get(f"{u}/conversations/{conv['id']}", headers=h2).status_code == 200
    r = client.get(f"{u}/conversations/{conv['id']}/notes", headers=h2)
    assert r.status_code == 403 and "notes" in r.json()["detail"]
    assert client.post(f"{u}/conversations/{conv['id']}/notes", json={"body": "x"}, headers=h2).status_code == 403
    # A role narrows settings too: no manage_teams, no teams changes; no manage_security, no roles screen.
    assert client.post(f"{u}/teams", json={"name": "T"}, headers=h2).status_code == 403
    assert client.get(f"{u}/roles", headers=h2).status_code == 403
    me = client.get(f"{u}/roles/me", headers=h2).json()
    assert me["has_roles"] and me["permissions"] == ["read_inbox", "reply"]
    # Someone with no roles keeps their rights exactly as before.
    assert client.get(f"{u}/conversations/{conv['id']}/notes", headers=b["agent"]["h"]).status_code == 200


def test_permissions_can_be_held_for_one_team_only(client):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    sales = client.post(f"{u}/teams", json={"name": "Sales"}, headers=h).json()
    claims = client.post(f"{u}/teams", json={"name": "Claims"}, headers=h).json()
    c1, c2 = _inbound(b, address="+18685550121"), _inbound(b, address="+18685550122")
    for c, t in ((c1, sales), (c2, claims)):
        client.post(f"{u}/conversations/{c['id']}/assign", json={"team_id": t["id"]}, headers=h)
    _assign(client, b, "agent2", _role(client, b, "Reader", ["read_inbox"]))
    assert _assign(client, b, "agent2", _role(client, b, "Sales notes", ["notes"]), team_id=sales["id"]).is_success
    h2 = b["agent2"]["h"]
    assert client.get(f"{u}/conversations/{c1['id']}/notes", headers=h2).status_code == 200
    r = client.get(f"{u}/conversations/{c2['id']}/notes", headers=h2)
    assert r.status_code == 403 and "team" in r.json()["detail"]


def test_roles_cannot_lock_the_organisation_out(client):
    b = business(client)
    reader = _role(client, b, "Reader", ["read_inbox"])
    assert _assign(client, b, "agent2", reader).status_code == 201
    # 'internal' can't manage settings; giving the last admin a reader role is refused.
    r = _assign(client, b, "agent", reader)
    assert r.status_code == 422 and "nobody" in r.json()["detail"]
    other = business(client, "Other Bank", people=("agent",))
    assert client.get(f"{base(b)}/roles", headers=other["agent"]["h"]).status_code == 403


# ---- security settings ---------------------------------------------------------------------------


def test_require_two_step_blocks_sign_in_until_enrolled(client):
    b = business(client)
    u = base(b)
    r = client.put(f"{u}/security", json={"require_two_step": True}, headers=b["agent"]["h"])
    assert r.status_code == 409  # the admin must have it first
    _enrol(client, b["agent"]["h"])
    r = client.put(f"{u}/security", json={"require_two_step": True}, headers=b["agent"]["h"])
    assert r.status_code == 200, r.text
    # An existing session without two-step is held at set-up.
    assert client.get(f"{u}/conversations", headers=b["agent2"]["h"]).status_code == 403
    r = _login(client, b["agent2"]["email"])
    assert r.status_code == 200 and r.json()["user"]["two_step_required"] is True
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    r = client.get(f"{u}/conversations", headers=h)
    assert r.status_code == 403 and "two-step" in r.json()["detail"]
    assert client.get("/api/v1/auth/me", headers=h).json()["two_step_required"] is True
    _enrol(client, h)
    assert client.get(f"{u}/conversations", headers=h).status_code == 200
    assert client.get("/api/v1/auth/me", headers=h).json()["two_step_required"] is False
    # The setting change is in the audit view.
    rows = client.get(f"{u}/security/audit", params={"kind": "settings"}, headers=b["agent"]["h"]).json()
    assert any(r["action"] == "commai.security.update" for r in rows)


def test_ip_allow_list_blocks_keys_from_outside_and_cannot_lock_out_the_admin(client, admin_headers):
    b = business(client)
    u = base(b)
    key = api_key(client, b["agent2"]["h"], ["commai:read"])
    inside = {**b["agent"]["h"], "X-Forwarded-For": "203.0.113.5"}
    r = client.put(
        f"{u}/security",
        json={"ip_allowlist": ["203.0.113.0/24"]},
        headers={**inside, "X-Forwarded-For": "198.51.100.7"},
    )
    assert r.status_code == 409 and "lock you out" in r.json()["detail"]
    assert client.put(f"{u}/security", json={"ip_allowlist": ["0.0.0.0/0"]}, headers=inside).status_code == 422
    r = client.put(f"{u}/security", json={"ip_allowlist": ["203.0.113.0/24", " 2001:db8::/32 "]}, headers=inside)
    assert r.status_code == 200 and r.json()["ip_allowlist"] == ["203.0.113.0/24", "2001:db8::/32"]

    r = client.get(f"{u}/conversations", headers={**key, "X-Forwarded-For": "198.51.100.9"})
    assert r.status_code == 403 and "network addresses" in r.json()["detail"]
    assert client.get(f"{u}/conversations", headers={**key, "X-Forwarded-For": "203.0.113.9"}).status_code == 200
    assert client.get(f"{u}/conversations", headers=inside).status_code == 200
    # Signing in from outside is refused and recorded as a failed sign-in.
    r = _login(client, b["agent2"]["email"], ip="198.51.100.9")
    assert r.status_code == 403
    assert _login(client, b["agent2"]["email"], ip="203.0.113.10").status_code == 200
    failed = client.get(f"{u}/security/audit", params={"kind": "failed"}, headers=inside).json()
    assert any(f["detail"].get("reason") == "ip_not_allowed" for f in failed)
    # ExaCarib admins are not held to a business's list.
    assert client.get(f"{u}/conversations", headers={**admin_headers, "X-Forwarded-For": "198.51.100.9"}).is_success


def test_session_lifetime_is_enforced(client):
    b = business(client)
    u = base(b)
    assert client.put(f"{u}/security", json={"session_hours": 2}, headers=b["agent"]["h"]).status_code == 200
    r = _login(client, b["agent2"]["email"])
    exp = dt.datetime.fromisoformat(r.json()["expires_at"])
    assert exp < dt.datetime.now(dt.UTC) + dt.timedelta(hours=2, minutes=1)
    with db.tx() as conn:
        conn.execute(
            "UPDATE sessions SET created_at = now() - interval '3 hours' FROM users"
            " WHERE users.id = sessions.user_id AND users.email = %s",
            (b["agent2"]["email"],),
        )
    assert client.get(f"{u}/conversations", headers=b["agent2"]["h"]).status_code == 401


def test_sso_logout_reaches_the_provider(client, monkeypatch):
    from types import SimpleNamespace

    from exaconnect_controller.identity import oidc

    b = business(client, people=("agent",))
    monkeypatch.setattr(
        oidc, "discovery", lambda issuer: {"end_session_endpoint": f"{issuer}/protocol/openid-connect/logout"}
    )
    settings = SimpleNamespace(
        oidc_issuer="https://id.example.test/realms/exacarib",
        oidc_client_id="exacarib-connect",
        public_url="https://connect.example.test",
    )
    from exaconnect_controller.security import token_hash

    token = b["agent"]["h"]["Authorization"].removeprefix("Bearer ")
    security.remember_id_token(token_hash(token), "header.body.sig")
    with db.tx() as conn:
        url = security.logout_url(conn, settings, token_hash(token))
        assert url.startswith("https://id.example.test/realms/exacarib/protocol/openid-connect/logout?")
        assert "id_token_hint=header.body.sig" in url and "post_logout_redirect_uri=" in url
    client.put(f"{base(b)}/security", json={"sso_logout": False}, headers=b["agent"]["h"])
    with db.tx() as conn:
        assert security.logout_url(conn, settings, token_hash(token)) is None
    saml = (
        '<EntityDescriptor xmlns="urn:oasis:names:tc:SAML:2.0:metadata" entityID="x"><IDPSSODescriptor>'
        '<SingleLogoutService Location="https://idp/slo"/></IDPSSODescriptor></EntityDescriptor>'
    )
    assert security.saml_logout_supported(saml)
    assert not security.saml_logout_supported(saml.replace("SingleLogoutService", "SingleSignOnService"))


# ---- abuse and account protection -----------------------------------------------------------------


def test_api_key_new_address_alert_lock_and_unlock(client, admin_headers):
    b = business(client)
    u = base(b)
    key = api_key(client, b["agent"]["h"], ["commai:read"])
    assert client.get(f"{u}/conversations", headers={**key, "X-Forwarded-For": "198.51.100.1"}).is_success
    assert client.get(f"{u}/conversations", headers={**key, "X-Forwarded-For": "198.51.100.2"}).is_success
    alerts = client.get(f"{u}/security/alerts", headers=b["agent"]["h"]).json()
    assert [a["kind"] for a in alerts] == ["api_key.new_address"]
    for i in range(3, 7):
        client.get(f"{u}/conversations", headers={**key, "X-Forwarded-For": f"198.51.100.{i}"})
    r = client.get(f"{u}/conversations", headers={**key, "X-Forwarded-For": "198.51.100.1"})
    assert r.status_code == 403 and "locked" in r.json()["detail"]
    keys = client.get(f"{u}/security/keys", headers=b["agent"]["h"]).json()
    assert keys[0]["locked"] and keys[0]["addresses"] >= 5
    kinds = [a["kind"] for a in client.get("/api/v1/commai/enterprise/alerts", headers=admin_headers).json()]
    assert "api_key.locked" in kinds  # ExaCarib sees it too
    with db.tx() as conn:
        assert conn.execute(
            "SELECT 1 FROM commai_events WHERE customer_id = %s AND type = 'security.alert'", (b["id"],)
        ).fetchone()
    r = client.post(f"{u}/security/keys/{keys[0]['id']}/unlock", headers=b["agent"]["h"])
    assert r.status_code == 200
    assert client.get(f"{u}/conversations", headers={**key, "X-Forwarded-For": "198.51.100.1"}).is_success
    other = business(client, "Other Bank", people=("agent",))
    assert client.post(f"{u}/security/keys/{keys[0]['id']}/unlock", headers=other["agent"]["h"]).status_code == 403


def test_watch_raises_failed_signin_send_spike_and_usage_jump_alerts(client):
    b = business(client)
    for _ in range(10):
        client.post("/api/v1/auth/login", json={"email": b["agent2"]["email"], "password": "wrong"})
    conv = _inbound(b)
    with db.tx() as conn:
        for i in range(60):
            conn.execute(
                """INSERT INTO messages (customer_id, conversation_id, direction, author_kind, body, status)
                   VALUES (%s, %s, 'out', 'workflow', %s, 'sent')""",
                (b["id"], conv["id"], f"Offer {i}"),
            )
        conn.execute(
            "INSERT INTO usage_records (customer_id, meter, quantity, at) VALUES (%s, 'ai_reply', 500, now())",
            (b["id"],),
        )
        conn.execute(
            "INSERT INTO usage_records (customer_id, meter, quantity, at)"
            " VALUES (%s, 'ai_reply', 70, now() - interval '3 days')",
            (b["id"],),
        )
    raised = client.post(f"{base(b)}/security/watch", headers=b["agent"]["h"]).json()
    assert {a["kind"] for a in raised} == {"signin.failures", "send.spike", "usage.jump"}
    # Raised once: a second look finds nothing new.
    assert client.post(f"{base(b)}/security/watch", headers=b["agent"]["h"]).json() == []
    a = raised[0]
    r = client.post(f"{base(b)}/security/alerts/{a['id']}/acknowledge", headers=b["agent"]["h"])
    assert r.json()["status"] == "acknowledged"
    # The periodic job runs the same checks for every business.
    with db.tx() as conn:
        protect.ensure_tick(conn)
    assert run_jobs() >= 1


def test_hard_spend_cap_stops_sending(client):
    b = business(client)
    acct = _account(client, b, "whatsapp", "+1 868 555 0100")
    _wa_inbound(client, acct, "Hi", "wamid.cap1")
    conv = _conv_for(b, "whatsapp")
    u, h = base(b), b["agent"]["h"]
    assert client.put(f"{u}/usage-limits/message_out", json={"monthly_hard": 0}, headers=h).status_code == 200
    r = client.post(f"{u}/conversations/{conv['id']}/messages", json={"body": "Hello"}, headers=h)
    assert r.status_code == 422 and "monthly limit" in r.json()["detail"]
    client.delete(f"{u}/usage-limits/message_out", headers=h)
    client.put(f"{u}/usage-limits/message_out:whatsapp", json={"monthly_hard": 1}, headers=h)
    assert client.post(f"{u}/conversations/{conv['id']}/messages", json={"body": "One"}, headers=h).status_code == 201
    run_jobs()
    r = client.post(f"{u}/conversations/{conv['id']}/messages", json={"body": "Two"}, headers=h)
    assert r.status_code == 422 and "WhatsApp" in r.json()["detail"]
