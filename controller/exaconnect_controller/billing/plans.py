"""Products, plans and subscriptions (ADR 0022).

Connect and Jibsy are sold as separate plans. An organisation (a customer
record) holds a product while one of its subscriptions to a plan for that
product covers the day. Subscriptions are the source of truth; the
``customers.products`` column used for access gating is kept in step with
them when it exists (it is added by the organisations work and may not be
there yet, so every use of it is guarded).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import psycopg

from .. import audit
from .core import BillingError

PRODUCTS = {"connect": "Connect", "commai": "Jibsy"}


def _today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def _is_uuid(v: Any) -> bool:
    try:
        uuid.UUID(str(v))
    except ValueError:
        return False
    return True


def _date(v: Any, what: str) -> dt.date:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v))
    except ValueError as e:
        raise BillingError(f"Give the {what} as YYYY-MM-DD.") from e


# ---- products ---------------------------------------------------------------------------


def products(conn: psycopg.Connection, customer_id: Any, on: dt.date | None = None) -> list[str]:
    """The products an organisation holds on a day (today by default), in a fixed order."""
    on = on or _today()
    rows = conn.execute(
        """SELECT DISTINCT product FROM subscriptions WHERE customer_id = %s AND starts_on <= %s
           AND (ends_on IS NULL OR ends_on > %s)""",
        (customer_id, on, on),
    ).fetchall()
    held = {r["product"] for r in rows}
    return [p for p in PRODUCTS if p in held]


def holds(conn: psycopg.Connection, customer_id: Any, product: str, on: dt.date | None = None) -> bool:
    return product in products(conn, customer_id, on)


def has_products_column(conn: psycopg.Connection) -> bool:
    return (
        conn.execute(
            """SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()
               AND table_name = 'customers' AND column_name = 'products'"""
        ).fetchone()
        is not None
    )


def sync_products(conn: psycopg.Connection, customer_id: Any) -> list[str] | None:
    """Copy what the subscriptions say into customers.products, if that column
    exists and the organisation has ever had a subscription (one that never had
    any is left as it is, so access set by hand before plans is not taken away).
    Returns what was written, or None when nothing was."""
    if not has_products_column(conn):
        return None
    if not conn.execute("SELECT 1 FROM subscriptions WHERE customer_id = %s LIMIT 1", (customer_id,)).fetchone():
        return None
    held = products(conn, customer_id)
    conn.execute("UPDATE customers SET products = %s WHERE id = %s", (held, customer_id))
    # A request to add an app (ADR 0041) is answered once the plan holds it.
    if conn.execute("SELECT to_regclass('customer_app_requests') AS t").fetchone()["t"]:
        conn.execute(
            "DELETE FROM customer_app_requests WHERE customer_id = %s AND product = ANY(%s)", (customer_id, held)
        )
    return held


def sync_all(conn: psycopg.Connection) -> int:
    """Bring every organisation's products column in step (a plan that ended
    yesterday stops counting today). Returns how many were written."""
    if not has_products_column(conn):
        return 0
    n = 0
    for r in conn.execute("SELECT DISTINCT customer_id FROM subscriptions").fetchall():
        if sync_products(conn, r["customer_id"]) is not None:
            n += 1
    return n


# ---- plans ------------------------------------------------------------------------------


def list_plans(conn: psycopg.Connection, include_inactive: bool = True) -> list[dict]:
    return conn.execute(
        """SELECT p.*, (SELECT count(*) FROM subscriptions s WHERE s.plan_id = p.id
                        AND (s.ends_on IS NULL OR s.ends_on > current_date))::int AS subscribers
           FROM plans p WHERE %s OR p.active ORDER BY p.product, p.name""",
        (include_inactive,),
    ).fetchall()


def get_plan(conn: psycopg.Connection, plan_id: Any) -> dict:
    if not _is_uuid(plan_id):
        raise BillingError("Plan not found.", 404)
    row = conn.execute("SELECT * FROM plans WHERE id = %s", (str(plan_id),)).fetchone()
    if row is None:
        raise BillingError("Plan not found.", 404)
    return row


def create_plan(conn: psycopg.Connection, values: dict, actor: str) -> dict:
    product = values.get("product")
    if product not in PRODUCTS:
        raise BillingError("A plan is for Connect or Jibsy.")
    name = str(values.get("name") or "").strip()
    if not name or len(name) > 80:
        raise BillingError("Give the plan a name of up to 80 characters.")
    if conn.execute("SELECT 1 FROM plans WHERE product = %s AND name = %s", (product, name)).fetchone():
        raise BillingError(f"There is already a {PRODUCTS[product]} plan called {name}.", 409)
    row = conn.execute(
        """INSERT INTO plans (product, name, description, created_by) VALUES (%s, %s, %s, %s) RETURNING *""",
        (product, name, str(values.get("description") or "")[:300], actor),
    ).fetchone()
    audit.record(conn, actor, "billing.plan.create", name, None, {"plan_id": str(row["id"]), "product": product})
    return row


def update_plan(conn: psycopg.Connection, plan_id: Any, values: dict, actor: str) -> dict:
    plan = get_plan(conn, plan_id)
    name = str(values["name"]).strip() if values.get("name") is not None else plan["name"]
    if not name or len(name) > 80:
        raise BillingError("Give the plan a name of up to 80 characters.")
    if (
        name != plan["name"]
        and conn.execute("SELECT 1 FROM plans WHERE product = %s AND name = %s", (plan["product"], name)).fetchone()
    ):
        raise BillingError(f"There is already a {PRODUCTS[plan['product']]} plan called {name}.", 409)
    desc = values["description"] if values.get("description") is not None else plan["description"]
    active = values["active"] if values.get("active") is not None else plan["active"]
    row = conn.execute(
        "UPDATE plans SET name = %s, description = %s, active = %s WHERE id = %s RETURNING *",
        (name, str(desc)[:300], bool(active), plan["id"]),
    ).fetchone()
    audit.record(
        conn,
        actor,
        "billing.plan.update",
        name,
        None,
        {"plan_id": str(plan["id"]), "name": name, "active": bool(active)},
    )
    return row


# ---- subscriptions ----------------------------------------------------------------------


def _status(row: dict, today: dt.date) -> str:
    if row["starts_on"] > today:
        return "scheduled"
    if row["ends_on"] is not None and row["ends_on"] <= today:
        return "ended"
    return "active"


def subscriptions(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    """Every subscription an organisation has had, newest first, with its plan."""
    today = _today()
    rows = conn.execute(
        """SELECT s.*, p.name AS plan, p.description AS plan_description, p.example AS plan_example
           FROM subscriptions s JOIN plans p ON p.id = s.plan_id
           WHERE s.customer_id = %s ORDER BY s.starts_on DESC, s.created_at DESC""",
        (customer_id,),
    ).fetchall()
    for r in rows:
        r["state"] = _status(r, today)
    return rows


def overlapping(
    conn: psycopg.Connection, customer_id: Any, start: dt.date, end: dt.date, product: str | None = None
) -> list[dict]:
    """Subscriptions that cover any day in [start, end), with their plan's name."""
    return conn.execute(
        """SELECT s.*, p.name AS plan FROM subscriptions s JOIN plans p ON p.id = s.plan_id
           WHERE s.customer_id = %(c)s AND s.starts_on < %(e)s AND (s.ends_on IS NULL OR s.ends_on > %(s)s)
             AND (%(p)s::text IS NULL OR s.product = %(p)s)
           ORDER BY s.product = 'commai', s.starts_on""",
        {"c": customer_id, "s": start, "e": end, "p": product},
    ).fetchall()


def _customer(conn, customer_id) -> dict:
    row = conn.execute("SELECT id, name FROM customers WHERE id = %s", (customer_id,)).fetchone()
    if row is None:
        raise BillingError("Customer not found.", 404)
    return row


def subscribe(
    conn: psycopg.Connection, customer_id: Any, plan_id: Any, starts_on: Any, actor: str, *, _audit: bool = True
) -> dict:
    """Start a plan for an organisation. It may not overlap another subscription
    to the same product (change the plan instead)."""
    cust = _customer(conn, customer_id)
    plan = get_plan(conn, plan_id)
    if not plan["active"]:
        raise BillingError(f"{plan['name']} is no longer offered.", 409)
    start = _date(starts_on or _today(), "start date")
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext('subscription:' || %s || ':' || %s))",
        (str(customer_id), plan["product"]),
    )
    clash = conn.execute(
        """SELECT s.*, p.name AS plan FROM subscriptions s JOIN plans p ON p.id = s.plan_id
           WHERE s.customer_id = %s AND s.product = %s AND (s.ends_on IS NULL OR s.ends_on > %s)
           ORDER BY s.starts_on LIMIT 1""",
        (customer_id, plan["product"], start),
    ).fetchone()
    if clash:
        raise BillingError(
            f"{cust['name']} already holds {clash['plan']} for {PRODUCTS[plan['product']]}"
            f" from {clash['starts_on']:%d %B %Y}. Change that plan instead.",
            409,
        )
    row = conn.execute(
        """INSERT INTO subscriptions (customer_id, plan_id, product, starts_on, created_by)
           VALUES (%s, %s, %s, %s, %s) RETURNING *""",
        (customer_id, plan["id"], plan["product"], start, actor),
    ).fetchone()
    if _audit:
        audit.record(
            conn,
            actor,
            "billing.subscription.start",
            f"{cust['name']}: {plan['name']}",
            customer_id,
            {"subscription_id": str(row["id"]), "plan": plan["name"], "starts_on": start.isoformat()},
        )
    sync_products(conn, customer_id)
    return {**row, "plan": plan["name"], "state": _status(row, _today())}


def _sub(conn, customer_id, subscription_id) -> dict:
    if not _is_uuid(subscription_id):
        raise BillingError("Subscription not found.", 404)
    row = conn.execute(
        """SELECT s.*, p.name AS plan FROM subscriptions s JOIN plans p ON p.id = s.plan_id
           WHERE s.id = %s AND s.customer_id = %s FOR UPDATE OF s""",
        (str(subscription_id), customer_id),
    ).fetchone()
    if row is None:
        raise BillingError("Subscription not found.", 404)
    return row


def _end_row(conn, sub: dict, on: dt.date, actor: str) -> dict:
    if sub["ends_on"] is not None and sub["ends_on"] <= on:
        raise BillingError(f"This subscription already ends on {sub['ends_on']:%d %B %Y}.", 409)
    if on <= sub["starts_on"]:
        raise BillingError(f"The end date must be after the start, {sub['starts_on']:%d %B %Y}.")
    issued = conn.execute(
        """SELECT number, period_start FROM billing_invoices WHERE subscription_id = %s AND status = 'issued'
           AND covered_to > %s ORDER BY period_start DESC LIMIT 1""",
        (sub["id"], on),
    ).fetchone()
    if issued:
        raise BillingError(
            f"Invoice {issued['number']} already bills this plan past {on:%d %B %Y}. Void it first.", 409
        )
    return conn.execute(
        """UPDATE subscriptions SET ends_on = %s, status = 'ended', ended_by = %s, ended_at = now()
           WHERE id = %s RETURNING *""",
        (on, actor, sub["id"]),
    ).fetchone()


def end(conn: psycopg.Connection, customer_id: Any, subscription_id: Any, on: Any, actor: str) -> dict:
    """End a plan on a day (exclusive: the organisation holds it until the day before)."""
    cust = _customer(conn, customer_id)
    sub = _sub(conn, customer_id, subscription_id)
    day = _date(on or _today(), "end date")
    row = _end_row(conn, sub, day, actor)
    audit.record(
        conn,
        actor,
        "billing.subscription.end",
        f"{cust['name']}: {sub['plan']}",
        customer_id,
        {"subscription_id": str(sub["id"]), "ends_on": day.isoformat()},
    )
    sync_products(conn, customer_id)
    return {**row, "plan": sub["plan"], "state": _status(row, _today())}


def change(conn: psycopg.Connection, customer_id: Any, subscription_id: Any, plan_id: Any, on: Any, actor: str) -> dict:
    """Move an organisation to another plan of the same product from a day: the
    old subscription ends that day and the new one starts on it."""
    cust = _customer(conn, customer_id)
    sub = _sub(conn, customer_id, subscription_id)
    plan = get_plan(conn, plan_id)
    if plan["product"] != sub["product"]:
        raise BillingError(f"{plan['name']} is not a {PRODUCTS[sub['product']]} plan.")
    if str(plan["id"]) == str(sub["plan_id"]):
        raise BillingError(f"{cust['name']} is already on {plan['name']}.", 409)
    if not plan["active"]:
        raise BillingError(f"{plan['name']} is no longer offered.", 409)
    day = _date(on or _today(), "change date")
    later = conn.execute(
        "SELECT 1 FROM subscriptions WHERE customer_id = %s AND product = %s AND starts_on > %s AND id <> %s",
        (customer_id, sub["product"], sub["starts_on"], sub["id"]),
    ).fetchone()
    if later:
        raise BillingError("A later subscription to this product exists. End or change that one.", 409)
    old = _end_row(conn, sub, day, actor)
    new = conn.execute(
        """INSERT INTO subscriptions (customer_id, plan_id, product, starts_on, ends_on, status, created_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (
            customer_id,
            plan["id"],
            plan["product"],
            day,
            sub["ends_on"],
            "active" if sub["ends_on"] is None else "ended",
            actor,
        ),
    ).fetchone()
    audit.record(
        conn,
        actor,
        "billing.subscription.change",
        f"{cust['name']}: {sub['plan']} to {plan['name']}",
        customer_id,
        {
            "from_subscription_id": str(sub["id"]),
            "to_subscription_id": str(new["id"]),
            "from_plan": sub["plan"],
            "to_plan": plan["name"],
            "on": day.isoformat(),
        },
    )
    sync_products(conn, customer_id)
    today = _today()
    return {
        "ended": {**old, "plan": sub["plan"], "state": _status(old, today)},
        "started": {**new, "plan": plan["name"], "state": _status(new, today)},
    }


def ensure_plan(
    conn: psycopg.Connection, customer_id: Any, product: str, actor: str, starts_on: dt.date | None = None
) -> None:
    """Give an organisation the standard plan for a product if it never had a
    subscription for it (the lab and demo seed; existing deployments are covered
    by the schema and commai/sql/16_plans.sql)."""
    if conn.execute(
        "SELECT 1 FROM subscriptions WHERE customer_id = %s AND product = %s", (customer_id, product)
    ).fetchone():
        return
    name = {"connect": "Connect Standard", "commai": "Jibsy Standard"}[product]
    plan = conn.execute("SELECT id FROM plans WHERE product = %s AND name = %s", (product, name)).fetchone()
    if plan is None:
        return
    if starts_on is None:
        created = conn.execute("SELECT created_at FROM customers WHERE id = %s", (customer_id,)).fetchone()
        starts_on = created["created_at"].astimezone(dt.UTC).date() if created else _today()
    subscribe(conn, customer_id, plan["id"], starts_on, actor, _audit=False)


def ensure_connect(conn: psycopg.Connection, customer_id: Any, actor: str, starts_on: dt.date | None = None) -> None:
    ensure_plan(conn, customer_id, "connect", actor, starts_on)


def summary(conn: psycopg.Connection, customer_id: Any) -> dict:
    """What GET /customers/{id}/plans returns."""
    cust = _customer(conn, customer_id)
    return {
        "customer_id": cust["id"],
        "customer": cust["name"],
        "products": products(conn, customer_id),
        "subscriptions": subscriptions(conn, customer_id),
    }
