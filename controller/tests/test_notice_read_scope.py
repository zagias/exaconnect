"""Selected-organisation notices and link names share the same read boundary."""

import json
import re
import sqlite3
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from exaconnect_controller.api import items
from exaconnect_controller.api.deps import User, current_user
from exaconnect_controller.integrations import api as integrations_api

ORG_A, ORG_B = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
LINK_A, LINK_B, LINK_C = (
    f"{n}" * 8 + "-" + f"{n}" * 4 + "-" + f"{n}" * 4 + "-" + f"{n}" * 4 + "-" + f"{n}" * 12 for n in (3, 4, 5)
)


@pytest.fixture
def portal(monkeypatch):
    """Execute the real read SQL, adapting PostgreSQL arrays to an in-memory function."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.create_function("host", 1, lambda value: value.split("/")[0] if value else None)
    conn.create_function("masklen", 1, lambda value: int(value.split("/")[1]) if value else None)
    conn.create_function("overlaps", 2, lambda a, b: bool(set(json.loads(a)) & set(json.loads(b))))
    conn.executescript("""
        CREATE TABLE sites (id TEXT, name TEXT);
        CREATE TABLE carriers (id TEXT, name TEXT);
        CREATE TABLE paths (name TEXT, label TEXT, ordinal INTEGER);
        CREATE TABLE links (
            id TEXT, customer_id TEXT, site_id TEXT, path TEXT, carrier_id TEXT,
            underlay_type TEXT, underlay_interface TEXT, underlay_ip TEXT,
            underlay_gateway TEXT, commit_mbps REAL, cost_per_mbps REAL, burst_price REAL, shape_mbps REAL
        );
        CREATE TABLE connect_notices (
            id INTEGER, carrier_id TEXT, kind TEXT, status TEXT, severity TEXT,
            title TEXT, description TEXT, link_ids TEXT, starts_at TEXT, ends_at TEXT,
            move_traffic BOOLEAN, window_state TEXT, external_id TEXT, source TEXT,
            created_at TEXT, updated_at TEXT, resolved_at TEXT
        );
        INSERT INTO sites VALUES ('site-a', 'Office A'), ('site-b', 'Office B');
        INSERT INTO carriers VALUES ('carrier-a', 'Carrier A'), ('carrier-b', 'Carrier B');
        INSERT INTO paths VALUES ('carrier-a', 'Primary', 1);
    """)
    for lid, cid, site, carrier in (
        (LINK_A, ORG_A, "site-a", "carrier-a"),
        (LINK_B, ORG_B, "site-b", "carrier-a"),
        (LINK_C, ORG_B, "site-b", "carrier-b"),
    ):
        conn.execute(
            "INSERT INTO links VALUES (?, ?, ?, 'carrier-a', ?, 'fibre', 'eth1',"
            " '192.0.2.1/24', '192.0.2.2', 10, 1, 1, 10)",
            (lid, cid, site, carrier),
        )
    for nid, carrier, ids in (
        (1, "carrier-a", [LINK_A, LINK_B]),
        (2, "carrier-a", [LINK_B]),
        (3, "carrier-b", [LINK_C]),
    ):
        conn.execute(
            "INSERT INTO connect_notices VALUES (?, ?, 'fault', 'open', 'warning', 'Carrier fault', '', ?,"
            " NULL, NULL, 0, '', '', 'portal', NULL, NULL, NULL)",
            (nid, carrier, json.dumps(ids)),
        )

    class Result:
        def __init__(self, cursor):
            self.rows = [dict(row) for row in cursor.fetchall()]
            for row in self.rows:
                if "link_ids" in row:
                    row["link_ids"] = json.loads(row["link_ids"])

        def __iter__(self):
            return iter(self.rows)

        def fetchall(self):
            return self.rows

    class Connection:
        def execute(self, query, params):
            query = query.replace("n.link_ids && %(own)s::uuid[]", "overlaps(n.link_ids, %(own)s)")
            query = re.sub(r"::(?:uuid|bigint|text)", "", query)
            if isinstance(params, dict):
                query = re.sub(r"%\((\w+)\)s", r":\1", query)
                params = {
                    k: json.dumps(v) if isinstance(v, list) else str(v) if v is not None else None
                    for k, v in params.items()
                }
            else:
                query = query.replace("%s", "?")
                params = tuple(str(v) if v is not None else None for v in params)
            return Result(conn.execute(query, params))

    state = SimpleNamespace(user=User("staff", "staff@example.invalid", "admin", None, None))
    monkeypatch.setattr(items.db, "tx", lambda: nullcontext(Connection()))
    app = FastAPI()
    app.dependency_overrides[current_user] = lambda: state.user
    app.include_router(items.router, prefix="/api/v1")
    app.include_router(integrations_api.router, prefix="/api/v1")
    with TestClient(app) as client:
        state.client = client
        yield state
    conn.close()


def test_staff_selection_scopes_notices_and_their_link_names(portal):
    notices = portal.client.get(f"/api/v1/notices?customer_id={ORG_A}")
    assert notices.status_code == 200
    assert [(n["id"], n["link_ids"]) for n in notices.json()] == [(1, [LINK_A])]
    links = portal.client.get(f"/api/v1/links?customer_id={ORG_A}")
    assert links.status_code == 200
    assert [link["id"] for link in links.json()] == [LINK_A]
    assert len(portal.client.get("/api/v1/notices").json()) == 3
    assert len(portal.client.get("/api/v1/links").json()) == 3


def test_customer_query_cannot_switch_to_another_organisations_notices_or_links(portal):
    portal.user = User("customer", "customer@example.invalid", "customer", ORG_A, None)
    notices = portal.client.get(f"/api/v1/notices?customer_id={ORG_B}").json()
    assert [(n["id"], n["link_ids"]) for n in notices] == [(1, [LINK_A])]
    assert [link["id"] for link in portal.client.get(f"/api/v1/links?customer_id={ORG_B}").json()] == [LINK_A]


def test_carrier_query_cannot_widen_its_own_notices_or_links(portal):
    portal.user = User("carrier", "carrier@example.invalid", "carrier", None, "carrier-b")
    notices = portal.client.get(f"/api/v1/notices?customer_id={ORG_A}").json()
    assert [(n["id"], n["link_ids"]) for n in notices] == [(3, [LINK_C])]
    assert [link["id"] for link in portal.client.get(f"/api/v1/links?customer_id={ORG_A}").json()] == [LINK_C]


def test_empty_staff_selection_returns_no_notices_or_link_names(portal):
    empty = "99999999-9999-9999-9999-999999999999"
    assert portal.client.get(f"/api/v1/notices?customer_id={empty}").json() == []
    assert portal.client.get(f"/api/v1/links?customer_id={empty}").json() == []
