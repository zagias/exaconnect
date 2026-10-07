"""Helpers (no tests) for the ADR 0029 connector tests: the kit's fixtures, plus
a business with a conversation, an app hook and a webhook check."""

from __future__ import annotations

from exaconnect_controller import db
from exaconnect_controller.commai import connectors
from exaconnect_controller.commai.connectors import more_common

from .commai_connector_kit import (  # noqa: F401 - fixtures re-exported for the tests
    connect_real,
    connect_simulated,
    execute_direct,
    fake,
    go_live,
    propose_and_run,
    real_env,
)


def conversation(customer_id: str, name: str = "Ana Lopez", subject: str = "Card blocked") -> str:
    with db.tx() as conn:
        ct = conn.execute(
            "INSERT INTO contacts (customer_id, name, email, phone)"
            " VALUES (%s, %s, 'ana@example.com', '+1 246 555 0100')"
            " RETURNING id",
            (customer_id, name),
        ).fetchone()
        cv = conn.execute(
            "INSERT INTO conversations (customer_id, contact_id, channel, subject) VALUES (%s, %s, 'chat', %s)"
            " RETURNING id",
            (customer_id, ct["id"], subject),
        ).fetchone()
    return str(cv["id"])


def hook(customer_id: str, app: str) -> dict:
    with db.tx() as conn:
        return more_common.app_hook(conn, customer_id, app)


def deliver(customer_id: str, app: str, headers: dict, body: bytes, query: dict | None = None) -> list[dict] | None:
    """What the generic receiver does: verify, then apply each event. None when refused."""
    c = connectors.get(app)
    with db.tx() as conn:
        row = connectors.connection(conn, customer_id, app)
        h = more_common.app_hook(conn, customer_id, app)
        if not c.verify_webhook(conn, row, h, headers, body, query or {}):
            return None
        evs = c.webhook_events(body, headers)
        for ev in evs:
            c.on_webhook_event(conn, row, ev)
        return evs


def connection(customer_id: str, app: str) -> dict:
    with db.tx() as conn:
        return connectors.connection(conn, customer_id, app)


def q(sql: str, *args) -> list[dict]:
    with db.tx() as conn:
        cur = conn.execute(sql, args)
        return cur.fetchall() if cur.description else []
