"""Helpers shared by the CommAI tests: a business with people in it."""

from __future__ import annotations

from exaconnect_controller import db
from exaconnect_controller.security import hash_password

PASSWORD = "commai test password"


def business(client, name: str = "Example Bank", people: tuple[str, ...] = ("agent", "agent2", "internal")) -> dict:
    """A customer with users. Returns {"id", "<who>": {"id", "email", "h": headers}}.
    A person called 'internal' gets the internal (notes-only) seat."""
    slug = name.lower().replace(" ", "")
    with db.tx() as conn:
        cid = conn.execute("INSERT INTO customers (name) VALUES (%s) RETURNING id", (name,)).fetchone()["id"]
        out: dict = {"id": str(cid)}
        for who in people:
            email = f"{who}@{slug}.example"
            uid = conn.execute(
                "INSERT INTO users (email, password_hash, role, customer_id) VALUES (%s, %s, 'customer', %s)"
                " RETURNING id",
                (email, hash_password(PASSWORD), cid),
            ).fetchone()["id"]
            seat = "internal" if who == "internal" else "agent"
            conn.execute(
                "INSERT INTO commai_members (customer_id, user_id, seat) VALUES (%s, %s, %s)", (cid, uid, seat)
            )
            out[who] = {"id": str(uid), "email": email}
    for who in people:
        r = client.post("/api/v1/auth/login", json={"email": out[who]["email"], "password": PASSWORD})
        assert r.status_code == 200, r.text
        out[who]["h"] = {"Authorization": f"Bearer {r.json()['token']}"}
    return out


def api_key(client, headers: dict, scopes: list[str] | None, name: str = "test") -> dict:
    r = client.post("/api/v1/auth/api-keys", json={"name": name, "scopes": scopes}, headers=headers)
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def base(b: dict) -> str:
    return f"/api/v1/commai/customers/{b['id']}"


def connect_app(customer_id: str, app: str, actions: list[str], *, status: str = "live", settings: dict | None = None):
    """Switch on a connector for a business directly (the setup flow is tested on its own)."""
    from psycopg.types.json import Jsonb

    with db.tx() as conn:
        conn.execute(
            """INSERT INTO integration_connections (customer_id, app, status, allowed_actions, settings, test_mode)
               VALUES (%s, %s, %s, %s, %s, false)
               ON CONFLICT (customer_id, app) DO UPDATE SET status = EXCLUDED.status,
                 allowed_actions = EXCLUDED.allowed_actions, settings = EXCLUDED.settings""",
            (customer_id, app, status, actions, Jsonb(settings or {})),
        )


def allow_tools(customer_id: str, role: str, tools: list[str]) -> None:
    with db.tx() as conn:
        for t in tools:
            conn.execute(
                "INSERT INTO commai_role_tools (customer_id, role, tool) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (customer_id, role, t),
            )


def run_jobs() -> int:
    from exaconnect_controller.commai import jobs

    with db.tx() as conn:  # make every queued job ready now
        conn.execute("UPDATE jobs SET run_after = now() WHERE status = 'queued'")
    return jobs.run_pending()
