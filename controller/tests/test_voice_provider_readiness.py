"""Production refuses fictional voice provisioning, activation and bill collection."""

import datetime as dt
import sqlite3
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from exaconnect_controller import db
from exaconnect_controller.commai import bill, usage
from exaconnect_controller.commai.api import voice_global
from exaconnect_controller.commai.voice import billing, config, emergency, freeswitch, pbx, porting, provisioning
from exaconnect_controller.commai.voice import provider as providers

from .commai_helpers import base
from .test_commai_voice import overview, save, setup_voice


@pytest.fixture
def production(monkeypatch):
    monkeypatch.delenv("EXA_VOICE_TEST_MODE", raising=False)
    monkeypatch.delenv("EXA_SIP_PROVIDER", raising=False)
    monkeypatch.setenv("EXA_ENV", "lab")  # The deployed environment label grants no simulation permission.


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("search_numbers", (None, "TT")),
        ("order_number", (None, "customer", "key")),
        ("release_number", (None, "+18685550100")),
        ("submit_port", (None, "customer", "+18685550100", "Carrier", "key")),
        ("port_status", (None, "simport:key")),
        ("send_port_documents", (None, "simport:key", [])),
        ("reschedule_port", (None, "simport:key", dt.date(2026, 12, 1))),
        ("cancel_port", (None, "simport:key")),
        ("activate_port", (None, "simport:key")),
        ("rollback_port", (None, "simport:key")),
        ("test_call", (None, "+18685550100")),
        ("register_emergency_address", (None, "+18685550100", {})),
        ("validate_emergency_address", (None, {}, "key")),
        ("emergency_address_status", (None, "simaddr:key")),
        ("options_ping", (None, {"key": "primary"})),
        ("place_call", (None, {"key": "primary"}, "+18685550100")),
        ("fetch_cdrs", (None, "primary", dt.date(2026, 11, 1), dt.date(2026, 12, 1))),
    ],
)
def test_simulation_cannot_access_data_or_claim_service_in_production(production, method, args):
    # None is deliberate: the policy must fail before any provider/database operation.
    with pytest.raises(providers.NotConfigured, match="explicit voice test mode") as err:
        getattr(providers.SimulatedProvider(), method)(*args)
    assert err.value.code == 503


def test_readiness_requires_explicit_test_mode_and_stays_non_live(production, monkeypatch):
    assert providers.status() == {
        "name": "simulated",
        "live": False,
        "configured": False,
        "test_mode": False,
        "number_ordering_ready": False,
        "reason": "Number ordering is unavailable. A live telephone provider has not been connected.",
    }
    for value in ("true", "yes", "0"):
        monkeypatch.setenv("EXA_VOICE_TEST_MODE", value)
        assert not providers.status()["number_ordering_ready"]
    monkeypatch.setenv("EXA_VOICE_TEST_MODE", "1")
    assert providers.status()["number_ordering_ready"] and not providers.status()["live"]
    assert providers.get().test_call(None, "+18685550100")["ok"]
    monkeypatch.setenv("EXA_SIP_PROVIDER", "typo")
    assert not providers.status()["number_ordering_ready"]
    with pytest.raises(providers.NotConfigured, match="setting is invalid"):
        providers.get()


def test_credentials_do_not_enable_placeholder_supplier_requests(production, monkeypatch):
    monkeypatch.setenv("EXA_SIP_PROVIDER", "sip")
    monkeypatch.setenv("EXA_VOICE_TEST_MODE", "1")
    for key in providers.HttpSipProvider.SETTINGS:
        monkeypatch.setenv(key, "placeholder")
    status = providers.status()
    assert status["configured"] and not status["number_ordering_ready"] and not status["live"]
    assert not providers.get().live
    with pytest.raises(providers.NotConfigured, match="not implemented or validated"):
        providers.get().order_number(None, "customer", "key")


def test_creation_and_activation_guards_precede_database_or_billing(production):
    items = {"numbers": {"new": 1}}
    with pytest.raises(providers.NotConfigured):
        provisioning.create(None, "customer", items, "actor")
    with pytest.raises(providers.NotConfigured):
        provisioning._billing(None, {"items": items}, None, [])
    with pytest.raises(config.OpError, match="unavailable"):
        config._Run(None, "customer", "actor", 1, True).add_number({})
    with pytest.raises(providers.NotConfigured):
        porting.create(None, "customer", {}, "actor")
    with pytest.raises(providers.NotConfigured):
        porting.go_live(None, {}, "actor")
    # Internal extensions do not depend on this supplier adapter.
    provisioning._require_provider({"users": [{"name": "User", "number": False}]})
    assert emergency.outbound_verdict(None, {"provider": "simulated"}) == (
        False,
        "This is a fictional test number, not a live telephone service.",
    )


def test_unavailable_search_is_empty_and_explains_readiness(production, monkeypatch):
    monkeypatch.setattr(voice_global.access, "check", lambda *args: None)
    monkeypatch.setattr(voice_global, "_admin", lambda *args: None)
    monkeypatch.setattr(voice_global.db, "tx", lambda: nullcontext(None))
    out = voice_global.search_numbers("customer", SimpleNamespace(), "TT", "868", "", 10)
    assert out["numbers"] == [] and not out["example"]
    assert not out["provider"]["number_ordering_ready"]


def test_caller_id_excludes_persisted_fictional_numbers_but_preserves_own_trunks(production, monkeypatch):
    sql = sqlite3.connect(":memory:")
    sql.row_factory = sqlite3.Row
    sql.executescript("""
        CREATE TABLE voice_numbers (customer_id TEXT, e164 TEXT, status TEXT, provider TEXT,
                                    target_type TEXT, target_id TEXT);
        INSERT INTO voice_numbers VALUES ('a', '+18685550100', 'active', 'simulated', 'user', 'u');
        INSERT INTO voice_numbers VALUES ('a', '+18686251234', 'active', 'own-trunk', 'none', NULL);
        INSERT INTO voice_numbers VALUES ('other', '+18680000001', 'active', 'own-trunk', 'user', 'u');
    """)
    conn = SimpleNamespace(execute=lambda query, params: sql.execute(query.replace("%s", "?"), params))
    assert pbx.caller_id(conn, "a", "u") == "+18686251234"
    sql.execute("DELETE FROM voice_numbers WHERE customer_id = 'a' AND provider = 'own-trunk'")
    assert pbx.caller_id(conn, "a", "u") == ""
    monkeypatch.setenv("EXA_VOICE_TEST_MODE", "1")
    assert pbx.caller_id(conn, "a", "u") == "+18685550100"


class ChargesDB:
    """Execute the source-selection SQL, translating only PostgreSQL array/cast syntax."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE voice_charges (id TEXT, customer_id TEXT, cdr_id TEXT, ref TEXT,
                                        rate_card_id TEXT DEFAULT 'agreed');
            CREATE TABLE voice_cdrs (id TEXT, customer_id TEXT, call_id TEXT);
            CREATE TABLE voice_numbers (id TEXT, customer_id TEXT, provider TEXT);
            CREATE TABLE voice_port_orders (id TEXT, number_id TEXT, customer_id TEXT);
            CREATE TABLE voice_rate_cards (id TEXT, customer_id TEXT, example INTEGER);
            INSERT INTO voice_rate_cards VALUES ('agreed', 'a', 0), ('agreed', 'other', 0);
        """)

    def execute(self, sql, params):
        cid, ids = params
        sql = sql.replace("c.id = ANY(%s)", "c.id IN (" + ",".join("?" for _ in ids) + ")")
        sql = sql.replace("%s", "?").replace("%%", "%").replace("n.id::text", "CAST(n.id AS TEXT)")
        sql = sql.replace("p.id::text", "CAST(p.id AS TEXT)")
        return self.conn.execute(sql, (cid, *ids))


@pytest.mark.parametrize("source", ["new_number", "monthly_number", "port", "call"])
def test_historical_simulated_charges_cannot_become_payable(production, monkeypatch, source):
    conn = ChargesDB()
    conn.conn.execute("INSERT INTO voice_numbers VALUES ('n', 'a', 'simulated')")
    conn.conn.execute("INSERT INTO voice_port_orders VALUES ('p', 'n', 'a')")
    conn.conn.execute("INSERT INTO voice_cdrs VALUES ('d', 'a', 'sim-old')")
    ref = {
        "new_number": "new_number:n",
        "monthly_number": "monthly_number:n:2026-11-01",
        "port": "port:p",
        "call": "call:old",
    }[source]
    conn.conn.execute(
        "INSERT INTO voice_charges (id, customer_id, cdr_id, ref) VALUES ('c', 'a', ?, ?)",
        ("d" if source == "call" else None, ref),
    )
    with pytest.raises(billing.VoiceError, match="simulated telephone charges"):
        billing.ensure_billable_charges(conn, "a", ["c"])
    # The exact same fixture remains available for an explicitly selected test run.
    monkeypatch.setenv("EXA_VOICE_TEST_MODE", "1")
    billing.ensure_billable_charges(conn, "a", ["c"])


def test_charge_selection_retains_tenant_and_document_scope(production):
    conn = ChargesDB()
    conn.conn.execute("INSERT INTO voice_numbers VALUES ('n', 'other', 'simulated')")
    conn.conn.execute("INSERT INTO voice_numbers VALUES ('live', 'a', 'customer_trunk')")
    for cid, ref, key in [
        ("a", "new_number:n", "own"),
        ("other", "new_number:n", "other"),
        ("a", "new_number:live", "live"),
    ]:
        conn.conn.execute(
            "INSERT INTO voice_charges (id, customer_id, cdr_id, ref) VALUES (?, ?, NULL, ?)", (key, cid, ref)
        )
    billing.ensure_billable_charges(conn, "a", ["own", "other", "live"])
    billing.ensure_billable_charges(conn, "other", ["own", "live"])
    with pytest.raises(billing.VoiceError):
        billing.ensure_billable_charges(conn, "other", ["other"])


def test_example_voice_prices_require_approved_sources_not_just_a_new_card(production, monkeypatch):
    conn = ChargesDB()
    conn.conn.execute("INSERT INTO voice_charges (id, customer_id, ref) VALUES ('c', 'a', 'user-setup')")
    billing.ensure_billable_charges(conn, "a", ["c"])
    conn.conn.execute("UPDATE voice_rate_cards SET example = 1 WHERE customer_id = 'a'")
    conn.conn.execute("INSERT INTO voice_rate_cards VALUES ('approved-v2', 'a', 0)")
    with pytest.raises(billing.VoiceError, match="example or unapproved voice prices"):
        billing.ensure_billable_charges(conn, "a", ["c"])
    monkeypatch.setenv("EXA_VOICE_TEST_MODE", "1")
    billing.ensure_billable_charges(conn, "a", ["c"])
    monkeypatch.delenv("EXA_VOICE_TEST_MODE")
    conn.conn.execute("UPDATE voice_charges SET rate_card_id = 'approved-v2'")
    billing.ensure_billable_charges(conn, "a", ["c"])
    conn.conn.execute("UPDATE voice_charges SET rate_card_id = NULL")
    with pytest.raises(billing.VoiceError, match="example or unapproved voice prices"):
        billing.ensure_billable_charges(conn, "a", ["c"])
    assert conn.conn.execute("SELECT count(*) FROM voice_charges").fetchone()[0] == 1


def test_combined_bill_rejects_example_nonvoice_prices_with_tenant_scope(production):
    sql = sqlite3.connect(":memory:")
    sql.row_factory = sqlite3.Row
    sql.executescript("""
        CREATE TABLE commai_bill_lines (bill_id TEXT, customer_id TEXT, family TEXT,
                                       rate_card_version INTEGER, voice_charge_id TEXT);
        CREATE TABLE commai_rate_cards (id TEXT, customer_id TEXT, version INTEGER, example INTEGER);
        INSERT INTO commai_bill_lines VALUES ('b', 'a', 'ai', 1, NULL);
        INSERT INTO commai_rate_cards VALUES ('other', 'other', 1, 0), ('old', 'a', 1, 1), ('new', 'a', 2, 0);
    """)
    conn = SimpleNamespace(execute=lambda query, params: sql.execute(query.replace("%s", "?"), params))
    with pytest.raises(bill.BillError, match="example or unapproved prices"):
        bill._check_charge_sources(conn, "a", "b")
    sql.execute("UPDATE commai_bill_lines SET rate_card_version = 2")
    bill._check_charge_sources(conn, "a", "b")
    sql.execute("DELETE FROM commai_rate_cards WHERE customer_id = 'a'")
    with pytest.raises(bill.BillError, match="example or unapproved prices"):
        bill._check_charge_sources(conn, "a", "b")


def test_customer_api_blocks_fake_numbers_but_allows_internal_setup(client, monkeypatch):
    b = setup_voice(client)
    u, h = base(b), b["boss"]["h"]
    monkeypatch.delenv("EXA_VOICE_TEST_MODE")
    assert not overview(client, b)["provider"]["number_ordering_ready"]
    result = client.get(f"{u}/voice/numbers/search", params={"country": "TT"}, headers=h)
    assert result.status_code == 200 and result.json()["numbers"] == []
    number = {"op": "add_number", "site": "Port of Spain"}
    result = client.post(f"{u}/voice/preview", json={"ops": [number]}, headers=h)
    assert result.status_code == 200 and not result.json()["ok"]
    for path, body in [
        ("orders", {"items": {"numbers": {"new": 1}}}),
        ("port-orders", {"e164": "+18685550155", "losing_carrier": "Other"}),
        ("calls/simulate", {"extension": "201", "to": "+18685550155", "seconds": 30}),
    ]:
        result = client.post(f"{u}/voice/{path}", json=body, headers=h)
        assert result.status_code == 503, (path, result.text)
    save(client, b, [{"op": "add_site", "name": "New office", "address_line1": "1 High St", "country": "TT"}])
    save(client, b, [{"op": "add_user", "name": "New colleague", "extension": "205", "site": "New office"}])
    out = overview(client, b)
    assert next(s for s in out["sites"] if s["name"] == "New office")["emergency_status"] == "not_registered"
    with db.tx() as conn:
        assert conn.execute("SELECT count(*) AS n FROM voice_sim_provider_numbers").fetchone()["n"] == 0


def test_approved_test_order_cannot_resume_after_test_mode_is_disabled(client, monkeypatch):
    b = setup_voice(client)
    u, h = base(b), b["boss"]["h"]
    created = client.post(f"{u}/voice/orders", json={"items": {"numbers": {"new": 1}}}, headers=h)
    assert created.status_code == 201, created.text
    order = created.json()
    price = {k: order["price"][k] for k in ("monthly_delta", "one_time")}
    result = client.post(f"{u}/voice/orders/{order['id']}/approve", json={"accepted_price": price}, headers=h)
    assert result.status_code == 200, result.text
    monkeypatch.delenv("EXA_VOICE_TEST_MODE")
    with pytest.raises(providers.NotConfigured), db.tx() as conn:
        provisioning.run(conn, {"payload": {"order_id": order["id"]}})
    with db.tx() as conn:
        assert (
            conn.execute("SELECT status FROM voice_orders WHERE id = %s", (order["id"],)).fetchone()["status"]
            == "approved"
        )
        assert conn.execute("SELECT count(*) AS n FROM voice_sim_provider_numbers").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM voice_numbers").fetchone()["n"] == 0


def test_historical_test_documents_cannot_be_issued_as_live(client, monkeypatch):
    b = setup_voice(client)
    save(client, b, [{"op": "add_number", "site": "Port of Spain"}])
    period = dt.date.today().replace(day=1)
    with db.tx() as conn:
        invoice = billing.draft_invoice(conn, b["id"], period, "test")
        combined = bill.draft(conn, b["id"], period, "test")
        number = conn.execute("SELECT id FROM voice_numbers WHERE customer_id = %s", (b["id"],)).fetchone()["id"]
        # Actual PostgreSQL source selection, not just the portable fixture above.
        charge = billing.one_time_charge(conn, b["id"], f"new_number:{number}", "new_number", "Test number")
        ids = (
            [charge["id"]]
            if charge
            else [
                r["id"]
                for r in conn.execute("SELECT id FROM voice_charges WHERE customer_id = %s", (b["id"],)).fetchall()
            ]
        )
        conn.execute(
            """INSERT INTO voice_numbers (customer_id, e164, source, status, provider)
               VALUES (%s, '+18686251234', 'trunk', 'active', 'customer-owned-trunk')""",
            (b["id"],),
        )
    monkeypatch.delenv("EXA_VOICE_TEST_MODE")
    out = overview(client, b)
    assert any(n["simulated"] for n in out["numbers"])
    with db.tx() as conn:
        data = freeswitch.gather(conn, b["id"])
    assert [n["e164"] for n in data["numbers"]] == ["+18686251234"]
    assert all(u["emergency_callback"] == "+18686251234" for u in data["users"])
    with pytest.raises(billing.VoiceError, match="simulated telephone charges"), db.tx() as conn:
        billing.ensure_billable_charges(conn, b["id"], ids)
    with pytest.raises(billing.VoiceError, match="simulated telephone charges"), db.tx() as conn:
        billing.issue_invoice(conn, b["id"], invoice["id"], "test")
    with pytest.raises(bill.BillError, match="simulated telephone charges"), db.tx() as conn:
        bill.issue(conn, b["id"], combined["id"], "test")
    with pytest.raises(billing.VoiceError, match="fictional test numbers"), db.tx() as conn:
        billing.draft_invoice(conn, b["id"], period, "test")
    with pytest.raises(bill.BillError, match="fictional test numbers"), db.tx() as conn:
        bill.draft(conn, b["id"], period, "test")


def test_example_only_service_charges_cannot_be_drafted_or_issued_in_production(client, monkeypatch):
    b = setup_voice(client)  # Internal extensions only: no fictional number or simulated call.
    period = dt.date.today().replace(day=1)
    with db.tx() as conn:
        usage.record(conn, b["id"], "ai_reply", 1, ref="example-price")
        invoice = billing.draft_invoice(conn, b["id"], period, "test")
        combined = bill.draft(conn, b["id"], period, "test")
        assert invoice["lines"] and combined["lines"]
    monkeypatch.delenv("EXA_VOICE_TEST_MODE")
    for operation in (
        lambda conn: billing.issue_invoice(conn, b["id"], invoice["id"], "test"),
        lambda conn: billing.draft_invoice(conn, b["id"], period, "test"),
    ):
        with pytest.raises(billing.VoiceError, match="example or unapproved voice prices"), db.tx() as conn:
            operation(conn)
    for operation in (
        lambda conn: bill.issue(conn, b["id"], combined["id"], "test"),
        lambda conn: bill.draft(conn, b["id"], period, "test"),
    ):
        with pytest.raises(bill.BillError, match="example or unapproved voice prices"), db.tx() as conn:
            operation(conn)
    # A new card does not rewrite the historic charges on an existing draft.
    with db.tx() as conn:
        billing.new_card(conn, b["id"], {**billing.EXAMPLE_CARD, "example": False}, "test")
    with pytest.raises(billing.VoiceError, match="example or unapproved voice prices"), db.tx() as conn:
        billing.issue_invoice(conn, b["id"], invoice["id"], "test")
    with db.tx() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM voice_charges WHERE customer_id = %s", (b["id"],)).fetchone()["n"]
            > 0
        )
