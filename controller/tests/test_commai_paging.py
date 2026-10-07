# ruff: noqa: F811  (pytest fixtures imported from test_commai_automation_helpers)
"""Cursor pagination on automation lists; the old plain list without a cursor (ADR 0033)."""

from exaconnect_controller import db

from .commai_helpers import base, business
from .test_commai_automation_helpers import secrets_env  # noqa: F401


def _all_pages(client, url, h, limit=2, key="items"):
    seen, cursor = [], ""
    for _ in range(20):
        r = client.get(url, params={"cursor": cursor, "limit": limit}, headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        seen += body[key] if key else body
        if not body["next"]:
            return seen
        cursor = body["next"]
    raise AssertionError("too many pages")


def test_lists_page_with_a_cursor_and_stay_lists_without(client, secrets_env):
    b = business(client)
    u, h = base(b), b["agent"]["h"]
    for i in range(5):
        r = client.post(
            f"{u}/workflows",
            json={"definition": {"name": f"Flow {i}", "trigger": {"type": "manual"}, "steps": []}},
            headers=h,
        )
        assert r.status_code == 201, r.text
        client.post(f"{u}/support-cases", json={"subject": f"Case {i}"}, headers=h)
        client.post(f"{u}/assistant/ask", json={"question": f"Why is the queue stuck {i}?"}, headers=h)
    # Same timestamps must not lose or repeat rows.
    with db.tx() as conn:
        conn.execute("UPDATE commai_workflows SET created_at = now() WHERE customer_id = %s", (b["id"],))
    old = client.get(f"{u}/workflows", headers=h).json()
    assert isinstance(old, list) and len(old) == 5
    paged = _all_pages(client, f"{u}/workflows", h)
    assert sorted(w["name"] for w in paged) == sorted(w["name"] for w in old)
    assert len({w["id"] for w in paged}) == 5

    cases = client.get(f"{u}/support-cases", headers=h).json()
    assert isinstance(cases, list) and len(cases) == 5
    assert len({c["id"] for c in _all_pages(client, f"{u}/support-cases", h)}) == 5

    hist = client.get(f"{u}/assistant/history", headers=h).json()
    assert isinstance(hist, list) and len(hist) == 5
    assert len({x["id"] for x in _all_pages(client, f"{u}/assistant/history", h)}) == 5

    approvals = client.get(f"{u}/approvals", headers=h).json()
    assert approvals["next"] is None and approvals["actions"] == []
    assert client.get(f"{u}/workflows", params={"cursor": "not-a-time|x"}, headers=h).status_code == 422
