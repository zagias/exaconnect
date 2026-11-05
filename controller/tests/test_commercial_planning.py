"""Offline planning quote gates; optional isolated PostgreSQL guard exercise.

No client fixture: that fixture resets a database. The optional PostgreSQL test
uses only an explicitly supplied disposable database and its own random schema.
"""

import datetime as dt
import os
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from exaconnect_controller.api import commercial as api
from exaconnect_controller.billing.core import BillingError
from exaconnect_controller.commercial import planning, quotes

CID = uuid.UUID("00000000-0000-0000-0000-000000000001")
KEY = uuid.UUID("00000000-0000-0000-0000-000000000002")



@pytest.mark.parametrize(
    "module",
    [
        "exaconnect_controller.commercial.quotes",
        "exaconnect_controller.commercial.planning",
        "exaconnect_controller.api.commercial",
    ],
)
def test_commercial_modules_import_in_fresh_process_without_application_bootstrap(module):
    # Pytest's normal API-first collection masked a quotes-first import cycle.
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "EXA_ROUTING_INTERVAL_S": "0"}
    result = subprocess.run(
        [sys.executable, "-B", "-c", f"import importlib; importlib.import_module({module!r})"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def brief(**changes):
    return {
        "format": "xakore-planning-brief",
        "version": 1,
        "offer": "Connect",
        "facts": [{"label": "Sites", "value": "Twenty shops; additions still to confirm"}],
        "gaps": [],
        **changes,
    }


def quote(**changes):
    return {
        "id": KEY,
        "customer_id": CID,
        "number": "Q-2026-0001",
        "status": "draft",
        "title": "Scope",
        "currency": "USD",
        "term_months": 12,
        "valid_until": dt.date.today() + dt.timedelta(days=30),
        "notes": "",
        "planning_source": brief(),
        "example": False,
        "lines": [],
        **changes,
    }


def line(**changes):
    return {
        "position": 1,
        "item_id": KEY,
        "price_id": KEY,
        "code": "real-service",
        "name": "Real service",
        "product": "connect",
        "charge_type": "recurring",
        "unit": "each",
        "quantity": Decimal("2.000000"),
        "config": {},
        "description": "Confirmed service",
        "list_amount": Decimal("10.0000"),
        "term_discount_pct": Decimal(0),
        "discount_pct": Decimal(0),
        "amount": Decimal("10.0000"),
        "price": {"model": "per_unit", "unit_price": "5", "example": False},
        "rating": {},
        "fulfilment": "manual",
        **changes,
    }


def test_full_24_fact_source_survives_separately_from_notes_and_authority_is_rejected():
    source = brief(facts=[{"label": f"Requirement {i}", "value": "x" * 500} for i in range(24)])
    body = api.QuoteIn(planning_source=source, creation_key=KEY, notes="A reviewed summary")
    values = api._values(body)
    assert values["planning_source"] == source
    assert len(str(values["planning_source"])) > 4000
    assert values["notes"] == "A reviewed summary"
    with pytest.raises(ValidationError):
        api.QuoteIn(planning_source={**source, "permissions": ["order"]}, creation_key=KEY)


@pytest.mark.parametrize(
    "fact",
    [
        {"label": "Password", "value": "never-store"},
        {"label": "Payment", "value": "4111 1111 1111 1111"},
        {"label": "Private key", "value": "private_key=never-store"},
    ],
)
def test_sensitive_source_is_rejected_before_storage_or_options_lookup(fact):
    class Forbidden:
        def execute(self, *_):
            pytest.fail("Sensitive source reached storage")

    with pytest.raises(BillingError):
        planning.options(Forbidden(), CID, brief(facts=[fact]), "USD")
    with pytest.raises(ValidationError):
        api.DesignOptionsIn(brief=brief(facts=[fact]), currency="USD")


def test_api_preserves_missing_quantity_and_planning_identity():
    with pytest.raises(ValidationError, match="creation key"):
        api.QuoteIn(planning_source=brief())
    body = api.QuoteIn(planning_source=brief(), creation_key=KEY, lines=[{"item": "real-service"}])
    raw = api._values(body)["lines"][0]
    assert "quantity" not in raw
    with pytest.raises(BillingError, match="quantity"):
        planning.explicit_line(raw, {"fulfilment": "manual"})
    with pytest.raises(BillingError, match="link type"):
        planning.explicit_line({"quantity": "20", "config": {}}, {"fulfilment": "link"})
    assert "planning_source" in api._values(api.QuotePatch(planning_source=None))
    with pytest.raises(ValidationError, match="true or false"):
        api.QuoteIn(planning_source=brief(), creation_key=KEY, lines=[{"item": "real-service", "quantity": True}])


def test_preview_allows_source_without_creation_identity_but_create_stays_strict():
    preview = api.QuotePreviewIn(planning_source=brief())
    assert preview.creation_key is None
    assert api._values(preview)["planning_source"] == brief()
    with pytest.raises(ValidationError, match="creation key"):
        api.QuoteIn(planning_source=brief())
    for model in (api.QuotePreviewIn, api.QuoteIn):
        with pytest.raises(ValidationError, match="only used with a planning brief"):
            model(creation_key=KEY)


def test_options_use_manager_and_existing_customer_gates():
    member = SimpleNamespace(role="customer", customer_id=CID, org_role="member")
    with pytest.raises(HTTPException) as err:
        api._quote_customer(member, CID)
    assert err.value.status_code == 403
    with pytest.raises(HTTPException) as err:
        api._quote_customer(SimpleNamespace(role="admin"), None)
    assert err.value.status_code == 422

    class Missing:
        def execute(self, *_):
            return SimpleNamespace(fetchone=lambda: None)

    with pytest.raises(BillingError, match="Customer not found"):
        planning.options(Missing(), CID, brief(), "USD")


def test_options_resolve_tenant_price_and_never_invent_quantity(monkeypatch):
    conn = SimpleNamespace(execute=lambda *_: SimpleNamespace(fetchone=lambda: {"id": CID}))
    base = {
        "id": KEY,
        "active": True,
        "example": False,
        "product": "connect",
        "unit": "site",
        "price_source": "site_monthly",
        "price": None,
        "plan_id": None,
    }
    monkeypatch.setattr(planning.catalogue, "list_items", lambda *_: [base, {**base, "example": True}])
    monkeypatch.setattr(planning.catalogue, "plan_for", lambda *_: {"example": False})
    calls = []

    def price(_conn, cid, *_):
        calls.append(cid)
        return {"model": "per_unit", "unit_price": "17", "currency": "USD", "example": False}

    monkeypatch.setattr(planning.catalogue, "plan_price", price)
    out = planning.options(conn, CID, brief(), "USD")
    assert calls == [CID] and len(out["items"]) == 1
    assert out["items"][0]["planning_match"]["price_available"] is True
    assert "quantity" not in out["items"][0]
    monkeypatch.setattr(planning.catalogue, "plan_for", lambda *_: {"example": True})
    assert not planning.options(conn, CID, brief(), "USD")["items"][0]["planning_match"]["price_available"]
    monkeypatch.setattr(planning.catalogue, "plan_for", lambda *_: {"example": False})
    monkeypatch.setattr(planning.catalogue, "plan_price", lambda *_: {**price(None, CID), "currency": "TTD"})
    assert not planning.options(conn, CID, brief(), "USD")["items"][0]["planning_match"]["price_available"]
    monkeypatch.setattr(planning.catalogue, "list_items", lambda *_: [{**base, "price_source": "catalogue"}])
    missing = planning.options(conn, CID, brief(), "USD")
    assert not missing["items"][0]["planning_match"]["price_available"]
    assert missing["items"][0]["price"] is None


@pytest.mark.parametrize(
    "source",
    [
        brief(offer="Discovery"),
        brief(offer="Network operations"),
        brief(offer="Connected devices"),
        brief(offer="Jibsy", facts=[{"label": "Need", "value": "Telephone PBX"}]),
    ],
)
def test_unsupported_and_telephone_only_scopes_have_no_inferred_product(source, monkeypatch):
    conn = SimpleNamespace(execute=lambda *_: SimpleNamespace(fetchone=lambda: {"id": CID}))
    monkeypatch.setattr(planning.catalogue, "list_items", lambda *_: pytest.fail("Invented product mapping"))
    out = planning.options(conn, CID, source, "USD")
    assert out["product"] is None and out["items"] == [] and out["warnings"]


def test_review_digest_covers_stored_scope_prices_and_config_but_not_discount_approval():
    q = quote(lines=[line()])
    current = planning.review_state(q, q["lines"])["current_digest"]
    approved = {
        **q,
        "design_review_digest": current,
        "design_reviewed_by": "user:staff@example.org",
        "design_reviewed_at": dt.datetime.now(dt.UTC),
        "design_review_note": "Commercial scope checked",
    }
    assert planning.review_state(approved, q["lines"])["status"] == "approved"
    assert planning.review_state({**approved, "approved_by": "other"}, q["lines"])["status"] == "approved"
    assert planning.review_state({**approved, "notes": "Changed scope"}, q["lines"])["status"] == "pending"
    assert planning.review_state(approved, [line(price={"unit_price": "9"})])["status"] == "pending"
    assert planning.review_state(approved, [line(config={"note": "Changed location"})])["status"] == "pending"


def test_planning_lines_use_canonical_prices_while_manual_defaults_remain(monkeypatch):
    item = {
        "id": KEY,
        "name": "Real service",
        "code": "real-service",
        "active": True,
        "example": False,
        "product": "connect",
        "charge_type": "one_off",
        "fulfilment": "manual",
        "unit": "each",
        "price_source": "catalogue",
        "max_discount_pct": 10,
    }
    price = {
        "id": KEY,
        "version": 1,
        "model": "per_unit",
        "unit_price": "75",
        "tiers": [],
        "included_qty": "0",
        "minimum": "0",
        "term_discounts": [],
        "example": False,
    }
    monkeypatch.setattr(quotes.catalogue, "get_item", lambda *_: item)
    monkeypatch.setattr(quotes.catalogue, "current_price", lambda *_: price)
    assert quotes.price_line(None, CID, "USD", 12, {"item": "real-service"}, False)["quantity"] == 1
    with pytest.raises(BillingError, match="quantity"):
        quotes.price_line(None, CID, "USD", 12, {"item": "real-service"}, False, planning_origin=True)
    priced = quotes.price_line(
        None,
        CID,
        "USD",
        12,
        {"item": "real-service", "quantity": "2", "price": {"unit_price": "0.01"}},
        False,
        planning_origin=True,
    )
    assert priced["amount"] == Decimal("150")
    price["example"] = True
    with pytest.raises(BillingError, match="Example"):
        quotes.price_line(None, CID, "USD", 12, {"item": "real-service", "quantity": "2"}, False, planning_origin=True)


def test_list_review_uses_current_stored_lines_in_one_batch(monkeypatch):
    q = quote(line_count=1)
    stored = {**line(), "quote_id": KEY}
    q.update(
        design_review_digest=planning.review_state(q, [stored])["current_digest"],
        design_reviewed_by="user:staff@example.org",
        design_reviewed_at=dt.datetime.now(dt.UTC),
        design_review_note="Commercial scope checked",
    )
    queries = []

    class Read:
        def execute(self, sql, *_):
            queries.append(sql)
            return SimpleNamespace(fetchall=lambda: [stored] if "SELECT l.*" in sql else [q])

    monkeypatch.setattr(quotes, "visible", lambda *_: True)
    out = quotes.list_quotes(Read(), CID, False)
    assert out[0]["design_review"]["status"] == "approved" and len(queries) == 2


def test_incomplete_scope_cannot_be_reviewed_or_order_ready():
    with pytest.raises(BillingError, match="Pricing is pending"):
        planning.require_review(quote(design_review={"status": "approved"}))
    for item, message in [
        (line(fulfilment="site", config={"locations": []}), "Name every"),
        (line(fulfilment="link", config={"underlay_type": "lte"}), "location, carrier"),
    ]:
        with pytest.raises(BillingError, match=message):
            planning.reviewable(quote(lines=[item]))
    with pytest.raises(BillingError, match="review the current"):
        planning.require_review(quote(lines=[line()], design_review={"status": "pending"}))


def test_replayed_create_is_returned_before_price_lookup_or_audit(monkeypatch):
    values = api._values(
        api.QuoteIn(planning_source=brief(), creation_key=KEY, lines=[{"item": "gone", "quantity": "2"}])
    )
    expected_hash = planning.request_hash(values, brief())
    statements = []

    class Replay:
        def execute(self, sql, *_):
            statements.append(sql)
            assert "INSERT" not in sql and "set_config" not in sql
            return SimpleNamespace(fetchone=lambda: {"id": KEY, "creation_request_hash": expected_hash})

    monkeypatch.setattr(quotes, "get", lambda *_: {"id": KEY, "created_by": "user:original@example.org"})
    monkeypatch.setattr(quotes, "price_line", lambda *_a, **_k: pytest.fail("Replay repriced an item"))
    monkeypatch.setattr(quotes.audit, "record", lambda *_: pytest.fail("Replay duplicated audit"))
    replayed = quotes.create(Replay(), CID, values, "user:retry@example.org", False)
    assert replayed["created_by"] == "user:original@example.org"
    assert "pg_advisory_xact_lock" in statements[0]
    with pytest.raises(BillingError) as err:
        quotes.create(Replay(), CID, {**values, "title": "Changed request"}, "user:retry@example.org", False)
    assert err.value.status == 409


def test_empty_scope_creation_records_source_and_actor_but_never_orders(monkeypatch):
    saved, audits, lines = [], [], []

    class Store:
        def execute(self, sql, params=()):
            if "INSERT INTO quotes" in sql:
                saved.append(params)
                return SimpleNamespace(fetchone=lambda: {"id": KEY, "number": "Q-NEW"})
            return SimpleNamespace(fetchone=lambda: None)

    monkeypatch.setattr(quotes, "_customer", lambda *_: {"name": "Example organisation"})
    monkeypatch.setattr(quotes, "_number", lambda *_: "Q-NEW")
    monkeypatch.setattr(quotes, "_write_lines", lambda _c, _id, rows: lines.extend(rows))
    monkeypatch.setattr(quotes, "get", lambda *_: {"id": KEY})
    monkeypatch.setattr(quotes.audit, "record", lambda *args: audits.append(args))
    from exaconnect_controller.commercial import orders

    monkeypatch.setattr(orders, "create_from_quote", lambda *_: pytest.fail("Draft creation ordered a service"))
    values = api._values(api.QuoteIn(planning_source=brief(), creation_key=KEY))
    assert quotes.create(Store(), CID, values, "user:owner@example.org", False) == {"id": KEY}
    assert saved[0][12] == "user:owner@example.org" and saved[0][13].obj == brief()
    assert saved[0][14] == KEY and lines == [] and len(audits) == 1


def test_staff_design_attestation_is_current_and_independent_of_discount_approval(monkeypatch):
    q = quote(status="pending_approval", needs_approval=True, lines=[line()])
    monkeypatch.setattr(quotes, "_row", lambda *_a, **_k: q)
    monkeypatch.setattr(quotes, "get", lambda *_: {**q, "design_review": planning.review_state(q, q["lines"])})
    audits = []
    monkeypatch.setattr(quotes.audit, "record", lambda *args: audits.append(args))

    class Store:
        def execute(self, sql, params):
            assert "status =" not in sql
            q.update(
                design_review_digest=params[0],
                design_reviewed_by=params[1],
                design_reviewed_at=dt.datetime.now(dt.UTC),
                design_review_note=params[2],
            )

    current = planning.review_state(q, q["lines"])["current_digest"]
    with pytest.raises(BillingError, match="changed"):
        quotes.design_review(Store(), KEY, "0" * 64, "Commercial scope checked", "user:staff@example.org")
    out = quotes.design_review(Store(), KEY, current, "Commercial scope checked", "user:staff@example.org")
    assert out["design_review"]["status"] == "approved"
    assert out["status"] == "pending_approval" and out["needs_approval"] is True and len(audits) == 1


def test_send_and_accept_recheck_review_before_any_write_or_order(monkeypatch):
    q = quote(status="sent")
    monkeypatch.setattr(quotes, "_row", lambda *_a, **_k: q)
    monkeypatch.setattr(quotes, "get", lambda *_: {**q, "lines": [line()], "design_review": {"status": "pending"}})

    class NoWrite:
        def execute(self, *_):
            pytest.fail("Unreviewed scope reached a write or order")

    with pytest.raises(BillingError, match="review the current"):
        quotes.accept(NoWrite(), KEY, "Fictional customer", "user:customer@example.org", CID)
    q["status"] = "draft"
    with pytest.raises(BillingError, match="review the current"):
        quotes.send(NoWrite(), KEY, "user:staff@example.org", True)


def test_copy_retains_source_and_allocates_new_identity_and_source_cannot_be_changed(monkeypatch):
    q = quote(created_by="user:customer@example.org")
    monkeypatch.setattr(quotes, "_row", lambda *_a, **_k: q)
    monkeypatch.setattr(quotes, "_stored_raw", lambda *_: [])
    monkeypatch.setattr(quotes, "create", lambda _c, _cid, values, *_: values)
    copied = quotes.copy(None, KEY, "user:staff@example.org", True)
    assert copied["planning_source"] == q["planning_source"] and copied["creation_key"] != KEY
    for value in (None, brief(offer="Discovery")):
        with pytest.raises(BillingError, match="original brief"):
            quotes.update(None, KEY, {"planning_source": value}, "user:staff@example.org", True)


def test_unpriced_printout_has_no_zero_priced_offer_and_escapes_requirements():
    q = quote(
        customer="Example organisation",
        state_label="Draft",
        number="Q-TEST",
        one_off_total=0,
        monthly_total=0,
        contract_total=0,
        usage_estimate=0,
        planning_source=brief(facts=[{"label": "Need", "value": "<script>unsafe</script>"}]),
    )
    rendered = quotes.document_html(q)
    assert "Pricing pending — no price agreed" in rendered and "USD 0.00" not in rendered
    assert "&lt;script&gt;unsafe&lt;/script&gt;" in rendered and "Design review: pending" in rendered


def test_planning_printout_exposes_actual_stored_quantities_and_fulfilment_details():
    stored = line(
        description="Customer description says one item",
        config={"note": "<script>quoted</script>"},
        price_text="USD 5 each",
    )
    q = quote(
        customer="Fictional organisation",
        state_label="Draft",
        number="Q-TEST",
        lines=[stored],
        one_off_total=10,
        monthly_total=0,
        contract_total=10,
        usage_estimate=0,
    )
    rendered = quotes.document_html(q)
    assert "Quantity: 2 each" in rendered and "Catalogue item: real-service" in rendered
    assert "Fulfilment note: &lt;script&gt;quoted&lt;/script&gt;" in rendered
    assert "Customer description says one item" in rendered


@pytest.mark.skipif(
    not os.environ.get("EXA_PLANNING_GUARD_TEST_DATABASE_URL"),
    reason="explicit disposable PostgreSQL database required; never uses the reset fixture",
)
def test_postgres_migration_and_legacy_write_guards():
    schema = "planning_guard_" + uuid.uuid4().hex
    sql = Path(quotes.__file__).with_name("schema.sql").read_text().split("CREATE TABLE IF NOT EXISTS sales_orders")[0]
    with psycopg.connect(os.environ["EXA_PLANNING_GUARD_TEST_DATABASE_URL"], autocommit=True) as conn:
        try:
            conn.execute(f'CREATE SCHEMA "{schema}"')
            conn.execute(f'SET search_path TO "{schema}", public')
            conn.execute("CREATE TABLE customers (id uuid PRIMARY KEY)")
            conn.execute("CREATE TABLE plans (id uuid PRIMARY KEY)")
            conn.execute(sql)
            conn.execute(sql)  # migration is repeatable
            conn.execute("INSERT INTO customers VALUES (%s)", (CID,))
            insert = (
                "INSERT INTO quotes (number, customer_id, valid_until, created_by) "
                "VALUES ('Q-LEGACY', %s, CURRENT_DATE, 'test')"
            )
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(insert, (CID,))
            with conn.transaction():
                conn.execute("SELECT set_config('xakore.quote_write_protocol', 'design_review_v1', true)")
                conn.execute(insert, (CID,))
            # A new number distinguishes protocol refusal from duplicate-number refusal.
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(insert.replace("'Q-LEGACY'", "'Q-AFTER-COMMIT'"), (CID,))
            with conn.transaction():
                conn.execute("SELECT set_config('xakore.quote_write_protocol', 'design_review_v1', true)")
                conn.execute(
                    """INSERT INTO quotes (number, customer_id, valid_until, created_by, planning_source,
                    creation_key, creation_request_hash)
                    VALUES ('Q-PLAN', %s, CURRENT_DATE, 'test', %s::jsonb, %s, %s)""",
                    (CID, __import__("json").dumps(brief()), KEY, "a" * 64),
                )
            for statement in (
                "UPDATE quotes SET status = 'sent' WHERE number = 'Q-PLAN'",
                "UPDATE quotes SET planning_source = NULL WHERE number = 'Q-PLAN'",
                "UPDATE quotes SET status = 'accepted' WHERE number = 'Q-PLAN'",
            ):
                with pytest.raises(psycopg.errors.CheckViolation):
                    conn.execute(statement)
            quote_id = conn.execute("SELECT id FROM quotes WHERE number = 'Q-PLAN'").fetchone()[0]
            item_id = conn.execute("""INSERT INTO catalogue_items (code, name, product, charge_type, created_by)
                VALUES ('real-service', 'Real service', 'connect', 'recurring', 'test') RETURNING id""").fetchone()[0]
            conn.execute(
                """INSERT INTO quote_lines (quote_id, position, item_id, code, name, product,
                charge_type, unit, quantity, list_amount, amount, price)
                VALUES (%s, 1, %s, 'real-service', 'Real service', 'connect', 'recurring', 'each', 2, 10, 10,
                        '{"model":"per_unit","unit_price":"5","example":false}')""",
                (quote_id, item_id),
            )
            attest = """UPDATE quotes SET design_review_digest = %s, design_reviewed_by = 'user:staff@example.org',
                design_reviewed_at = now(), design_review_note = 'Commercial scope checked' WHERE id = %s"""
            conn.execute(attest, ("a" * 64, quote_id))
            conn.execute("UPDATE quotes SET title = 'Changed scope' WHERE id = %s", (quote_id,))
            review_sql = "SELECT design_review_digest FROM quotes WHERE id = %s"
            assert conn.execute(review_sql, (quote_id,)).fetchone()[0] is None
            conn.execute(attest, ("a" * 64, quote_id))
            conn.execute("UPDATE quote_lines SET quantity = 3 WHERE quote_id = %s", (quote_id,))
            assert conn.execute(review_sql, (quote_id,)).fetchone()[0] is None
            conn.execute(attest, ("a" * 64, quote_id))
            conn.execute("UPDATE quotes SET approved_by = 'discount-reviewer' WHERE id = %s", (quote_id,))
            assert conn.execute("SELECT design_review_digest FROM quotes WHERE id = %s", (quote_id,)).fetchone()[0]
            # A competing legacy line edit must take the same parent lock as review/send.
            with conn.transaction():
                conn.execute("SELECT id FROM quotes WHERE id = %s FOR UPDATE", (quote_id,))
                with psycopg.connect(os.environ["EXA_PLANNING_GUARD_TEST_DATABASE_URL"], autocommit=True) as other:
                    other.execute(f'SET search_path TO "{schema}", public')
                    other.execute("SET lock_timeout = '100ms'")
                    with pytest.raises(psycopg.errors.LockNotAvailable):
                        other.execute("UPDATE quote_lines SET quantity = 4 WHERE quote_id = %s", (quote_id,))
            conn.execute("UPDATE quotes SET status = 'sent' WHERE id = %s", (quote_id,))
            for statement in (
                "UPDATE quote_lines SET quantity = 4 WHERE quote_id = %s",
                "DELETE FROM quote_lines WHERE quote_id = %s",
                "UPDATE quotes SET notes = 'Changed after send' WHERE id = %s",
            ):
                with pytest.raises(psycopg.errors.CheckViolation):
                    conn.execute(statement, (quote_id,))
            conn.execute("DELETE FROM customers WHERE id = %s", (CID,))  # parent cascade remains possible
        finally:
            conn.execute("SET search_path TO public")
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
