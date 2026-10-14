"""The product catalogue (ADR 0055): what ExaCarib sells, and its versioned prices.

Plan fees, sites and link commits are priced from the organisation's plan price
list (ADR 0022), so a quote shows exactly what the invoice will charge; every
other item has its own prices here. A price is never changed: a new version
takes effect from a date, and quotes keep a copy of the one they used.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import audit
from ..billing import plans as plans_mod
from ..billing.core import BillingError, dec
from . import rating

FAMILIES = ("plan", "site", "link", "hardware", "service", "seat", "support", "usage")
CHARGE_TYPES = ("recurring", "one_off", "usage")
FULFILMENTS = ("immediate", "manual", "subscription", "site", "link")
PRICE_SOURCES = ("catalogue", "plan_fee", "site_monthly", "commit_per_mbps")
# Items whose price and billing come from the plan price list, and what fulfils them.
PLAN_PRICED = {"plan_fee": "subscription", "site_monthly": "site", "commit_per_mbps": "link"}
EDITABLE = ("name", "description", "unit", "max_discount_pct", "active", "position", "example")


def _today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def _is_uuid(v: Any) -> bool:
    try:
        uuid.UUID(str(v))
    except ValueError:
        return False
    return True


def _price_out(row: dict | None) -> dict | None:
    if row is None:
        return None
    p = {
        "id": row["id"],
        "version": row["version"],
        "currency": row["currency"],
        "effective_from": row["effective_from"],
        "model": row["model"],
        "unit_price": f"{row['unit_price'].normalize():f}",
        "tiers": row["tiers"],
        "included_qty": f"{row['included_qty'].normalize():f}",
        "minimum": f"{row['minimum'].normalize():f}",
        "term_discounts": row["term_discounts"],
        "example": row["example"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }
    return p


def get_item(conn: psycopg.Connection, ref: Any) -> dict:
    """An item by id or code."""
    if _is_uuid(ref):
        row = conn.execute("SELECT * FROM catalogue_items WHERE id = %s", (ref,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM catalogue_items WHERE code = %s", (str(ref),)).fetchone()
    if row is None:
        raise BillingError(f"There is no catalogue item {ref}.", 404)
    return row


def current_price(conn: psycopg.Connection, item_id: Any, currency: str, on: dt.date | None = None) -> dict | None:
    """The newest price version in a currency that has started by a day."""
    return conn.execute(
        """SELECT * FROM catalogue_prices WHERE item_id = %s AND currency = %s AND effective_from <= %s
           ORDER BY effective_from DESC, version DESC LIMIT 1""",
        (item_id, currency, on or _today()),
    ).fetchone()


def list_items(
    conn: psycopg.Connection, include_inactive: bool = False, product: str | None = None, currency: str = "USD"
) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM catalogue_items WHERE (%(all)s OR active) AND (%(p)s::text IS NULL OR product = %(p)s)
           ORDER BY position, name""",
        {"all": include_inactive, "p": product},
    ).fetchall()
    out = []
    for r in rows:
        price = current_price(conn, r["id"], currency) if r["price_source"] == "catalogue" else None
        out.append(_item_out(r, price, currency))
    return out


def _item_out(r: dict, price: dict | None, currency: str) -> dict:
    p = _price_out(price)
    return {
        **r,
        "max_discount_pct": f"{r['max_discount_pct'].normalize():f}",
        "plan_priced": r["price_source"] != "catalogue",
        "price": p,
        "price_text": rating.describe(p, r["unit"], currency) if p else _plan_price_text(r["price_source"]),
    }


def _plan_price_text(source: str) -> str:
    return {
        "plan_fee": "Monthly fee from the plan price list",
        "site_monthly": "Per site per month from the plan price list",
        "commit_per_mbps": "Per Mbps per month from the plan price list",
    }.get(source, "No price yet")


def item_detail(conn: psycopg.Connection, ref: Any) -> dict:
    item = get_item(conn, ref)
    prices = conn.execute(
        "SELECT * FROM catalogue_prices WHERE item_id = %s ORDER BY currency, effective_from DESC, version DESC",
        (item["id"],),
    ).fetchall()
    current = current_price(conn, item["id"], "USD")
    return {**_item_out(item, current, "USD"), "prices": [_price_out(p) for p in prices]}


def _clean_item(values: dict, creating: bool) -> dict:
    out: dict[str, Any] = {}
    if "name" in values:
        name = str(values["name"] or "").strip()
        if not 1 <= len(name) <= 120:
            raise BillingError("An item needs a name of up to 120 characters.")
        out["name"] = name
    if "description" in values:
        out["description"] = str(values["description"] or "").strip()[:500]
    if "unit" in values:
        unit = str(values["unit"] or "").strip()
        if not 1 <= len(unit) <= 30:
            raise BillingError("Name the unit, such as site, seat or hour.")
        out["unit"] = unit
    if "max_discount_pct" in values:
        pct = dec(values["max_discount_pct"])
        if not 0 <= pct <= 100:
            raise BillingError("The most a person can discount must be between 0% and 100%.")
        out["max_discount_pct"] = pct
    for k in ("active", "example"):
        if k in values:
            out[k] = bool(values[k])
    if "position" in values:
        out["position"] = int(values["position"])
    if creating:
        code = str(values.get("code") or "").strip().lower()
        if not re.match(r"^[a-z0-9][a-z0-9._-]{1,47}$", code):
            raise BillingError("Give the item a short code: lower-case letters, digits, dots and dashes.")
        product = values.get("product")
        if product not in plans_mod.PRODUCTS:
            raise BillingError("Choose Connect or Jibsy.")
        charge = values.get("charge_type")
        if charge not in CHARGE_TYPES:
            raise BillingError("Choose how it is charged: every month, once, or by use.")
        family = values.get("family") or {"usage": "usage", "one_off": "service"}.get(charge, "service")
        if family not in FAMILIES or family in ("plan", "site", "link"):
            raise BillingError("Choose the kind of item: hardware, service, seat, support or usage.")
        fulfilment = values.get("fulfilment") or ("manual" if charge == "one_off" else "immediate")
        if fulfilment not in ("immediate", "manual"):
            raise BillingError("New items are either set up straight away or by ExaCarib staff.")
        meter = None
        if charge == "usage":
            meter = str(values.get("meter") or "").strip().lower()
            if not re.match(r"^[a-z0-9][a-z0-9._:-]{0,63}$", meter):
                raise BillingError("A usage item needs the meter its usage arrives on, such as engineer_hours.")
        out.update(
            code=code,
            product=product,
            charge_type=charge,
            family=family,
            fulfilment=fulfilment,
            meter=meter,
            price_source="catalogue",
        )
        out.setdefault("unit", "each")
        out.setdefault("description", "")
        if "name" not in out:
            raise BillingError("An item needs a name.")
    return out


def create_item(conn: psycopg.Connection, values: dict, actor: str) -> dict:
    """A new catalogue item, with its first price when one is given."""
    vals = _clean_item(values, creating=True)
    if conn.execute("SELECT 1 FROM catalogue_items WHERE code = %s", (vals["code"],)).fetchone():
        raise BillingError(f"There is already an item with the code {vals['code']}.", 409)
    cols = [*vals, "created_by"]
    row = conn.execute(
        f"INSERT INTO catalogue_items ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING *",
        (*vals.values(), actor),
    ).fetchone()
    audit.record(conn, actor, "catalogue.item.create", row["code"], None, {"name": row["name"]})
    if values.get("price"):
        add_price(conn, row["id"], values["price"], actor)
    return item_detail(conn, row["id"])


def update_item(conn: psycopg.Connection, ref: Any, values: dict, actor: str) -> dict:
    item = get_item(conn, ref)
    vals = _clean_item({k: v for k, v in values.items() if k in EDITABLE}, creating=False)
    if vals:
        sets = ", ".join(f"{k} = %s" for k in vals)
        conn.execute(
            f"UPDATE catalogue_items SET {sets}, updated_at = now() WHERE id = %s", (*vals.values(), item["id"])
        )
        audit.record(conn, actor, "catalogue.item.update", item["code"], None, {k: str(v) for k, v in vals.items()})
    return item_detail(conn, item["id"])


def add_price(conn: psycopg.Connection, ref: Any, values: dict, actor: str) -> dict:
    """A new price version for an item. It takes effect from its date (today by
    default); quotes and contracts made earlier keep the price they used."""
    item = get_item(conn, ref)
    if item["price_source"] != "catalogue":
        raise BillingError(f"{item['name']} is priced on the plan price list. Change it under Billing > Plans.", 409)
    price = rating.validate_price(values)
    if item["charge_type"] != "recurring" and price["term_discounts"]:
        raise BillingError("Only monthly items have term discounts.")
    currency = str(values.get("currency") or "USD").upper()
    if not re.match(r"^[A-Z]{3}$", currency):
        raise BillingError("Give the currency as three letters, such as USD or JMD.")
    try:
        start = dt.date.fromisoformat(str(values.get("effective_from") or _today()))
    except ValueError as e:
        raise BillingError("Give the start date as YYYY-MM-DD.") from e
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('catalogue_price:' || %s))", (str(item["id"]),))
    last = conn.execute(
        "SELECT coalesce(max(version), 0) AS v FROM catalogue_prices WHERE item_id = %s AND currency = %s",
        (item["id"], currency),
    ).fetchone()["v"]
    row = conn.execute(
        """INSERT INTO catalogue_prices (item_id, version, currency, effective_from, model, unit_price, tiers,
                                         included_qty, minimum, term_discounts, example, created_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (
            item["id"],
            last + 1,
            currency,
            start,
            price["model"],
            Decimal(price["unit_price"]),
            Jsonb(price["tiers"]),
            Decimal(price["included_qty"]),
            Decimal(price["minimum"]),
            Jsonb(price["term_discounts"]),
            bool(values.get("example", item["example"])),
            actor,
        ),
    ).fetchone()
    audit.record(
        conn,
        actor,
        "catalogue.price.create",
        item["code"],
        None,
        {"version": row["version"], "currency": currency, "effective_from": start.isoformat(), **price},
    )
    return _price_out(row)


def plan_for(conn: psycopg.Connection, customer_id: Any, item: dict, on: dt.date) -> dict:
    """The plan whose price list prices a plan-priced item for an organisation:
    the plan the item sells, else the plan it holds for the product, else the
    product's standard plan."""
    if item["plan_id"]:
        return plans_mod.get_plan(conn, item["plan_id"])
    sub = conn.execute(
        """SELECT plan_id FROM subscriptions WHERE customer_id = %s AND product = %s
             AND (ends_on IS NULL OR ends_on > %s) ORDER BY starts_on LIMIT 1""",
        (customer_id, item["product"], on),
    ).fetchone()
    if sub:
        return plans_mod.get_plan(conn, sub["plan_id"])
    name = {"connect": "Connect Standard", "commai": "Jibsy Standard"}[item["product"]]
    row = conn.execute("SELECT * FROM plans WHERE product = %s AND name = %s", (item["product"], name)).fetchone()
    if row is None:
        raise BillingError("There is no plan to price this item from.", 409)
    return row


def plan_price(conn: psycopg.Connection, customer_id: Any, item: dict, on: dt.date) -> dict:
    """A plan-priced item's price, as a per-unit price the rating engine can use,
    with the price list it came from."""
    from ..billing import service

    plan = plan_for(conn, customer_id, item, on)
    pl = service.price_list_for(conn, plan["id"], customer_id, on)
    field = {"plan_fee": "monthly_fee", "site_monthly": "site_monthly", "commit_per_mbps": "commit_per_mbps"}[
        item["price_source"]
    ]
    return {
        "model": "per_unit",
        "unit_price": f"{dec(pl[field]).normalize():f}",
        "tiers": [],
        "included_qty": "0",
        "minimum": "0",
        "term_discounts": [],
        "source": "plan_price_list",
        "plan": plan["name"],
        "plan_id": str(plan["id"]),
        "price_list_id": str(pl["id"]),
        "price_list_version": pl["version"],
        "currency": pl["currency"],
        "example": bool(pl["example"]),
    }
