"""Carrier changes must not expose another carrier's anomaly measurements."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import nullcontext

import pytest
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from exaconnect_controller import db
from exaconnect_controller.ai import insights
from exaconnect_controller.api import ai, items
from exaconnect_controller.api.deps import User
from exaconnect_controller.security import hash_password

from .test_flow import _seed


@pytest.fixture
def store(monkeypatch):
    """Run the actual insight SQL in memory; no Postgres service is needed."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.create_function("now", 0, lambda: "2026-11-01 12:00:00")
    conn.executescript("""
        CREATE TABLE sites (id TEXT, name TEXT);
        CREATE TABLE links (id TEXT, path TEXT, carrier_id TEXT);
        CREATE TABLE carriers (id TEXT, name TEXT);
        CREATE TABLE events (time TEXT, customer_id TEXT, kind TEXT, detail TEXT);
        CREATE TABLE insights (
            id INTEGER PRIMARY KEY, customer_id TEXT, kind TEXT, key TEXT, severity TEXT,
            title TEXT, detail TEXT, data TEXT, example BOOLEAN, site_id TEXT, link_id TEXT,
            carrier_id TEXT, first_seen TEXT, last_seen TEXT, resolved_at TEXT,
            acknowledged_by TEXT, acknowledged_at TEXT
        );
        INSERT INTO sites VALUES ('site', 'Office');
        INSERT INTO links VALUES ('link', 'carrier-a', 'a');
        INSERT INTO carriers VALUES ('a', 'Carrier A'), ('b', 'Carrier B');
    """)

    class Connection:
        def execute(self, query, params):
            query = query.replace("now() - interval '24 hours'", "datetime(now(), '-24 hours')")
            query = re.sub(r"::(?:uuid|text)", "", query)
            if isinstance(params, dict):
                query = re.sub(r"%\((\w+)\)s", r":\1", query)
            else:
                query = query.replace("%s", "?")
                params = [json.dumps(p.obj) if isinstance(p, Jsonb) else p for p in params]
            return conn.execute(query, params)

    adapter = Connection()
    monkeypatch.setattr(ai.db, "tx", lambda: nullcontext(adapter))
    yield conn, adapter
    conn.close()


def _raise(conn, carrier, *, link="link"):
    return insights.raise_insight(
        conn,
        "org",
        "anomaly",
        "anomaly:link:latency",
        "warning",
        f"Carrier {carrier.upper()} latency",
        "Current measurements",
        {"carrier": carrier},
        site_id="site",
        link_id=link,
        carrier_id=carrier,
    )


def _visible(carrier, *, include_resolved=False):
    user = User("user", "carrier@example.invalid", "carrier", None, carrier)
    return ai.list_insights(user, include_resolved=include_resolved)


def _detail(carrier, insight_id):
    user = User("user", "carrier@example.invalid", "carrier", None, carrier)
    return items.get_insight(insight_id, user)


def _detail_not_found(carrier, insight_id):
    with pytest.raises(HTTPException) as error:
        _detail(carrier, insight_id)
    assert error.value.status_code == 404


@pytest.mark.parametrize("include_resolved", [False, True])
def test_reassigned_link_hides_stale_insights_until_refreshed(store, include_resolved):
    conn, adapter = store
    insight_id, _ = _raise(adapter, "a")
    if include_resolved:
        conn.execute("UPDATE insights SET resolved_at = now()")
    assert [r["id"] for r in _visible("a", include_resolved=include_resolved)] == [insight_id]
    assert _visible("b", include_resolved=include_resolved) == []
    assert _detail("a", insight_id)["id"] == insight_id
    _detail_not_found("b", insight_id)

    conn.execute("UPDATE links SET carrier_id = 'b' WHERE id = 'link'")
    # Neither the previous owner nor the new owner sees stale measurements.
    assert _visible("a", include_resolved=include_resolved) == []
    assert _visible("b", include_resolved=include_resolved) == []
    _detail_not_found("a", insight_id)
    _detail_not_found("b", insight_id)

    refreshed_id, notified = _raise(adapter, "b")
    assert refreshed_id == insight_id and not notified
    assert _visible("a", include_resolved=include_resolved) == []
    current = _visible("b", include_resolved=include_resolved)
    assert len(current) == 1 and current[0]["title"] == "Carrier B latency"
    assert json.loads(current[0]["data"])["carrier"] == "b"
    _detail_not_found("a", insight_id)
    assert _detail("b", insight_id)["title"] == "Carrier B latency"


def test_refresh_updates_both_link_and_carrier_associations(store):
    conn, adapter = store
    insight_id, _ = _raise(adapter, "a")
    conn.execute("INSERT INTO links VALUES ('replacement', 'carrier-b', 'b')")
    assert _raise(adapter, "b", link="replacement")[0] == insight_id
    row = conn.execute("SELECT link_id, carrier_id FROM insights WHERE id = ?", (insight_id,)).fetchone()
    assert tuple(row) == ("replacement", "b")
    assert _visible("a") == []
    assert [r["id"] for r in _visible("b")] == [insight_id]


def test_carrier_cannot_read_an_insight_after_its_link_is_removed(store):
    conn, adapter = store
    insight_id, _ = _raise(adapter, "a")
    conn.execute("DELETE FROM links")
    assert _visible("a", include_resolved=True) == []
    _detail_not_found("a", insight_id)


def test_carrier_reassignment_with_authenticated_accounts(client, admin_headers):
    """Exercise Postgres joins and real carrier authentication when configured."""
    cid = _seed()["customer_id"]
    with db.tx() as conn:
        link = conn.execute(
            "SELECT id, site_id, carrier_id FROM links WHERE customer_id = %s AND path = 'carrier-a' LIMIT 1", (cid,)
        ).fetchone()
        new_carrier = conn.execute("SELECT id FROM carriers WHERE name = 'Carrier B'").fetchone()["id"]
        for name, carrier_id in (("a", link["carrier_id"]), ("b", new_carrier)):
            conn.execute(
                "INSERT INTO users (email, password_hash, role, carrier_id) VALUES (%s, %s, 'carrier', %s)",
                (f"carrier-{name}@example.invalid", hash_password("local carrier test password"), carrier_id),
            )

        def refresh(carrier_id, title):
            return insights.raise_insight(
                conn,
                cid,
                "anomaly",
                f"anomaly:{link['id']}:latency",
                "warning",
                title,
                "Current measurements",
                site_id=link["site_id"],
                link_id=link["id"],
                carrier_id=carrier_id,
            )[0]

        insight_id = refresh(link["carrier_id"], "Carrier A measurements")

    headers = {}
    for name in ("a", "b"):
        login = client.post(
            "/api/v1/auth/login",
            json={"email": f"carrier-{name}@example.invalid", "password": "local carrier test password"},
        )
        assert login.status_code == 200, login.text
        headers[name] = {"Authorization": f"Bearer {login.json()['token']}"}

    def visible(name):
        response = client.get("/api/v1/insights?include_resolved=true", headers=headers[name])
        assert response.status_code == 200, response.text
        return response.json()

    def detail(name):
        return client.get(f"/api/v1/insights/{insight_id}", headers=headers[name])

    assert [r["id"] for r in visible("a")] == [insight_id]
    assert detail("a").status_code == 200
    assert detail("b").status_code == 404
    with db.tx() as conn:
        conn.execute("UPDATE links SET carrier_id = %s WHERE id = %s", (new_carrier, link["id"]))
    assert visible("a") == visible("b") == []
    assert detail("a").status_code == detail("b").status_code == 404
    with db.tx() as conn:
        refresh(new_carrier, "Carrier B measurements")
    assert visible("a") == []
    assert [(r["id"], r["title"]) for r in visible("b")] == [(insight_id, "Carrier B measurements")]
    assert detail("a").status_code == 404
    assert detail("b").status_code == 200
    assert detail("b").json()["title"] == "Carrier B measurements"
