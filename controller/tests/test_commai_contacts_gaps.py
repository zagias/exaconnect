"""Contacts (ADR 0038): channel identities added, verified and removed through
the API; customer history (calls, open requests, linked records, bookings);
cursor paging on the inbox lists."""

from psycopg.types.json import Jsonb

from exaconnect_controller import db
from exaconnect_controller.commai import inbox

from .commai_helpers import api_key, base, business


def _contact(client, b, **body):
    r = client.post(f"{base(b)}/contacts", json={"name": "Ana", **body}, headers=b["agent"]["h"])
    assert r.status_code == 201, r.text
    return r.json()


def test_identities_add_verify_remove(client):
    b = business(client)
    c = _contact(client, b, email="ana@example.org")
    u = f"{base(b)}/contacts/{c['id']}/identities"
    r = client.post(u, json={"channel": "whatsapp", "address": "+1 (868) 555-0101"}, headers=b["agent"]["h"])
    assert r.status_code == 201, r.text
    wa = r.json()
    assert wa["address"] == "+18685550101" and wa["verified"] is False

    # Verified needs evidence.
    r = client.post(u, json={"channel": "sms", "address": "+18685550102", "verified": True}, headers=b["agent"]["h"])
    assert r.status_code == 422 and r.json()["code"] == "invalid"
    r = client.post(f"{u}/{wa['id']}/verify", json={"evidence": "Code sent by our app"}, headers=b["agent"]["h"])
    assert r.status_code == 200 and r.json()["verified"] is True
    with db.tx() as conn:
        a = conn.execute(
            "SELECT * FROM audit_log WHERE action = 'commai.contact.identity.verify' AND target = %s", (wa["id"],)
        ).fetchone()
        assert a and a["detail"]["evidence"] == "Code sent by our app"
        assert conn.execute(
            "SELECT 1 FROM commai_events WHERE type = 'contact.identity_verified' AND customer_id = %s", (b["id"],)
        ).fetchone()

    # The same address can't belong to two contacts.
    other = _contact(client, b, name="Ben")
    r = client.post(
        f"{base(b)}/contacts/{other['id']}/identities",
        json={"channel": "whatsapp", "address": "+18685550101"},
        headers=b["agent"]["h"],
    )
    assert r.status_code == 409

    listed = client.get(u, headers=b["agent"]["h"]).json()
    assert {i["channel"] for i in listed} == {"email", "whatsapp"}
    # An inbound message from the verified address joins this contact, verified.
    with db.tx() as conn:
        got = inbox.receive(conn, b["id"], "whatsapp", "+18685550101", "Hello")
    assert str(got["conversation"]["contact_id"]) == c["id"]

    # Internal seats can't remove; agents can. Read-only keys can't write.
    assert client.delete(f"{u}/{wa['id']}", headers=b["internal"]["h"]).status_code == 403
    ro = api_key(client, b["agent"]["h"], ["commai:read"])
    assert client.delete(f"{u}/{wa['id']}", headers=ro).status_code == 403
    assert client.delete(f"{u}/{wa['id']}", headers=b["agent"]["h"]).status_code == 204
    assert {i["channel"] for i in client.get(u, headers=b["agent"]["h"]).json()} == {"email"}

    # Another business can't see or touch them.
    x = business(client, "Other Bank")
    assert client.get(u, headers=x["agent"]["h"]).status_code == 403
    assert client.get(f"{base(x)}/contacts/{c['id']}/identities", headers=x["agent"]["h"]).status_code == 404


def test_customer_history(client):
    b = business(client)
    c = _contact(client, b, phone="+18685550101")
    with db.tx() as conn:
        conv = inbox.receive(conn, b["id"], "sms", "+18685550101", "Can I book for Friday?")["conversation"]
        conn.execute(
            """INSERT INTO voice_cdrs (customer_id, call_id, direction, from_number, to_number, started_at, ended_at,
                                       seconds) VALUES (%s, 'c1', 'inbound', '18685550101', '+18685550000',
                                       now() - interval '1 hour', now() - interval '55 minutes', 300),
                                      (%s, 'c2', 'inbound', '+18685559999', '+18685550000', now(), now(), 10)""",
            (b["id"], b["id"]),
        )
        for key, status, inputs in (
            ("k-book", "succeeded", {"start": "2026-10-09T10:00:00Z", "end": "2026-10-09T10:30:00Z"}),
            ("k-refund", "awaiting_approval", {"amount": 20}),
            ("k-test", "succeeded", {"start": "2026-10-10T10:00:00Z"}),
        ):
            conn.execute(
                """INSERT INTO action_runs (customer_id, conversation_id, role, app, action, inputs, idempotency_key,
                                            status, proposed_by, test)
                   VALUES (%s, %s, 'customer_agent', 'simulated', %s, %s, %s, %s, 'ai', %s)""",
                (
                    b["id"],
                    conv["id"],
                    "book" if "book" in key else "refund",
                    Jsonb(inputs),
                    key,
                    status,
                    key == "k-test",
                ),
            )
        conn.execute(
            """INSERT INTO commai_connector_objects (customer_id, app, idempotency_key, object_type, object_id)
               VALUES (%s, 'hubspot', 'k-book', 'deal', 'D-42')""",
            (b["id"],),
        )
        conn.execute(
            """INSERT INTO sim_records (customer_id, app, kind, idempotency_key, data)
               VALUES (%s, 'simulated', 'booking', 'k-book', '{}')""",
            (b["id"],),
        )
    h = client.get(f"{base(b)}/contacts/{c['id']}/history", headers=b["agent"]["h"])
    assert h.status_code == 200, h.text
    h = h.json()
    assert [x["id"] for x in h["conversations"]] == [str(conv["id"])]
    assert [x["call_id"] for x in h["calls"] if x["kind"] == "phone"] == ["c1"]  # not someone else's call
    kinds = {(x["kind"], x["state"]) for x in h["open_requests"]}
    assert ("conversation", "open") in kinds and ("action", "awaiting_approval") in kinds
    assert {(r["app"], r["object_id"]) for r in h["linked_records"] if not r["simulated"]} == {("hubspot", "D-42")}
    assert any(r["simulated"] for r in h["linked_records"])
    assert [x["start"] for x in h["bookings"]] == ["2026-10-09T10:00:00Z"]  # test runs are not bookings
    # Another business gets nothing.
    x = business(client, "Other Bank")
    assert client.get(f"{base(x)}/contacts/{c['id']}/history", headers=x["agent"]["h"]).status_code == 404


def test_cursor_paging_keeps_old_lists(client):
    b = business(client)
    for name in ("A team", "B team", "C team"):
        client.post(f"{base(b)}/teams", json={"name": name}, headers=b["agent"]["h"])
    old = client.get(f"{base(b)}/teams", headers=b["agent"]["h"]).json()
    assert isinstance(old, list) and len(old) == 3
    p1 = client.get(f"{base(b)}/teams?cursor=&limit=2", headers=b["agent"]["h"]).json()
    assert [t["name"] for t in p1["items"]] == ["A team", "B team"] and p1["next"]
    p2 = client.get(f"{base(b)}/teams?cursor={p1['next']}&limit=2", headers=b["agent"]["h"]).json()
    assert [t["name"] for t in p2["items"]] == ["C team"] and p2["next"] is None
    bad = client.get(f"{base(b)}/teams?cursor=nope", headers=b["agent"]["h"])
    assert bad.status_code == 400

    for text in ("one", "two", "three"):  # one transaction each, so each has its own time
        with db.tx() as conn:
            conv = inbox.receive(conn, b["id"], "web", "visitor:1", text)["conversation"]
    u = f"{base(b)}/conversations/{conv['id']}/messages"
    assert [m["body"] for m in client.get(u, headers=b["agent"]["h"]).json()] == ["one", "two", "three"]
    p1 = client.get(f"{u}?cursor=&limit=2", headers=b["agent"]["h"]).json()
    assert [m["body"] for m in p1["items"]] == ["one", "two"]
    p2 = client.get(u, params={"cursor": p1["next"], "limit": 2}, headers=b["agent"]["h"]).json()
    assert [m["body"] for m in p2["items"]] == ["three"] and p2["next"] is None
    for path in ("members", "routing-rules", f"conversations/{conv['id']}/notes"):
        r = client.get(f"{base(b)}/{path}?cursor=", headers=b["agent"]["h"])
        assert r.status_code == 200 and "items" in r.json(), path
