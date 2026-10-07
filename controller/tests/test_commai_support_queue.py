# ruff: noqa: F811  (pytest fixtures imported from test_commai_automation_helpers)
"""ExaCarib's support queue, replies and internal notes (ADR 0039)."""

from exaconnect_controller import db

from .commai_helpers import base, business
from .test_commai_automation_helpers import secrets_env  # noqa: F401

Q = "/api/v1/commai/exacarib/support/cases"


def _open(client, b, subject):
    r = client.post(f"{base(b)}/support-cases", json={"subject": subject}, headers=b["agent"]["h"])
    assert r.status_code == 201, r.text
    return r.json()


def test_exacarib_queue_replies_and_internal_notes(client, admin_headers, secrets_env):
    a, z = business(client, "Alpha Ltd"), business(client, "Zeta Ltd")
    ca = _open(client, a, "WhatsApp stopped")
    cz = _open(client, z, "Calls dropping")
    # Only ExaCarib sees the queue.
    assert client.get(Q, headers=a["agent"]["h"]).status_code == 403
    rows = client.get(Q, headers=admin_headers).json()
    assert {r["reference"] for r in rows} >= {ca["reference"], cz["reference"]}
    assert next(r for r in rows if r["reference"] == ca["reference"])["business"] == "Alpha Ltd"
    only = client.get(Q, params={"customer_id": a["id"]}, headers=admin_headers).json()
    assert [r["reference"] for r in only] == [ca["reference"]]
    # Assign and prioritise.
    p = client.patch(
        f"{Q}/{ca['id']}", json={"assignee": "support@exacarib.com", "priority": "high"}, headers=admin_headers
    )
    assert p.status_code == 200 and p.json()["priority"] == "high"
    assert client.patch(f"{Q}/{ca['id']}", json={"status": "lost"}, headers=admin_headers).status_code == 422
    mine = client.get(Q, params={"assignee": "support@exacarib.com"}, headers=admin_headers).json()
    assert [r["reference"] for r in mine] == [ca["reference"]]
    # An internal note and a reply that waits on the business.
    n = client.post(
        f"{Q}/{ca['id']}/replies",
        json={"body": "Meta shows the number flagged.", "internal": True},
        headers=admin_headers,
    )
    assert n.status_code == 201
    r = client.post(
        f"{Q}/{ca['id']}/replies",
        json={"body": "Please re-verify your business with Meta.", "status": "waiting_on_customer"},
        headers=admin_headers,
    )
    assert r.status_code == 201
    # The business sees ExaCarib's reply but not the internal note.
    t = client.get(f"{base(a)}/support-cases/{ca['id']}/thread", headers=a["agent"]["h"]).json()
    assert t["status"] == "waiting_on_customer"
    assert [x["body"] for x in t["replies"]] == ["Please re-verify your business with Meta."]
    full = client.get(f"{Q}/{ca['id']}", headers=admin_headers).json()
    assert len(full["replies"]) == 2 and full["replies"][0]["internal"]
    # The business answers: the case goes back to open.
    b = client.post(
        f"{base(a)}/support-cases/{ca['id']}/replies", json={"body": "Done, verified."}, headers=a["agent"]["h"]
    )
    assert b.status_code == 201 and not b.json()["from_exacarib"]
    assert client.get(f"{Q}/{ca['id']}", headers=admin_headers).json()["status"] == "open"
    # Another business can't read or reply to it.
    assert client.get(f"{base(z)}/support-cases/{ca['id']}/thread", headers=z["agent"]["h"]).status_code == 404
    assert (
        client.post(
            f"{base(z)}/support-cases/{ca['id']}/replies", json={"body": "hi"}, headers=z["agent"]["h"]
        ).status_code
        == 404
    )
    with db.tx() as conn:
        ev = conn.execute(
            "SELECT type, data FROM commai_events WHERE customer_id = %s AND type LIKE 'support_case.%%' ORDER BY id",
            (a["id"],),
        ).fetchall()
        assert sorted(e["type"] for e in ev) == [
            "support_case.opened",
            "support_case.replied",
            "support_case.replied",
            "support_case.updated",
        ]
        assert "flagged" not in str(ev)  # the internal note raised no event
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM audit_log WHERE action LIKE 'commai.support_case.%%' AND customer_id = %s",
                (a["id"],),
            ).fetchone()["n"]
            >= 4
        )


def test_queue_cursor_pages(client, admin_headers, secrets_env):
    b = business(client)
    refs = [_open(client, b, f"Case {i}")["reference"] for i in range(5)]
    seen, cursor = [], ""
    while True:
        r = client.get(Q, params={"cursor": cursor, "limit": 2, "customer_id": b["id"]}, headers=admin_headers).json()
        seen += [x["reference"] for x in r["items"]]
        if not r["next"]:
            break
        cursor = r["next"]
    assert sorted(seen) == sorted(refs) and len(seen) == 5
    assert client.get(Q, params={"cursor": "nonsense|x"}, headers=admin_headers).status_code == 422
