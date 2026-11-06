"""Fresh PBX call admission is signed, tenant-bound and safe with stale XML.

Exercise the real API, PBX signature/tenant resolution, owned DID lookup and
readiness query against an explicit fake database. No SIP, model or HTTP call.
"""

from __future__ import annotations

import uuid
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from exaconnect_controller.commai.api import voice_pbx
from exaconnect_controller.commai.voice import livekit, pbx
from exaconnect_controller.commai.voice.common import domain

SECRET = "fictional-pbx-call-admission-signing-secret"
CUSTOMER = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER = uuid.UUID("22222222-2222-4222-8222-222222222222")
NUMBER_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
DID = "18685550100"
CALL = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PATH = "/api/v1/commai/internal/voice/phone-authorise"


@pytest.fixture
def gate(monkeypatch):
    state = SimpleNamespace(
        number_customer=CUSTOMER,
        number_active=True,
        rule_enabled=True,
        rule_number=NUMBER_ID,
        evidence_customer=CUSTOMER,
        lease_fresh=True,
        speech_fresh=True,
        transfer_fresh=True,
        pbx_fresh=True,
        pbx_version="1.11.3",
        lab_only=False,
        queries=[],
        transactions=0,
        profile_present=True,
        profile={"ai": {"enabled": True}},
        model_available=True,
        revoked_module=None,
    )

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

        def fetchall(self):
            return self.rows

    class Connection:
        def execute(self, sql, params=()):
            state.queries.append((sql, params))
            if "FROM customers WHERE replace" in sql:
                return Result(
                    [
                        {"id": cid}
                        for cid in (CUSTOMER, OTHER)
                        if str(cid).replace("-", "").startswith(params[0].rstrip("%"))
                    ]
                )
            if "FROM voice_numbers WHERE status = 'active'" in sql:
                return Result(
                    [{"id": NUMBER_ID, "customer_id": state.number_customer, "e164": "+" + DID}]
                    if state.number_active and params == (DID,)
                    else []
                )
            if sql == "SELECT config FROM commai_settings WHERE customer_id = %s":
                assert params == (CUSTOMER,)
                return Result([{"config": state.profile}] if state.profile_present else [])
            if "FROM voice_ai_rules" in sql:
                # Match both current tenant and this owned DID (or an explicit
                # all-number rule); a rule from a different DID is insufficient.
                enabled = state.rule_enabled and params == (CUSTOMER, NUMBER_ID)
                enabled = enabled and state.rule_number in (None, NUMBER_ID)
                return Result([{"ok": 1}] if enabled else [])
            if "FROM voice_ai_phone_runtime" in sql:
                assert "NOT lab_only" in sql and "expires_at > now()" in sql
                assert "speech_verified_at >" in sql and "transfer_verified_at >" in sql
                assert "voice_ai_phone_pbx_runtime" in sql
                assert "e.customer_id = voice_ai_phone_runtime.customer_id" in sql
                assert "e.module IN ('voice', 'ai_agents') AND NOT e.enabled" in sql
                assert params == (CUSTOMER, CUSTOMER)  # no global readiness query
                ready = all((state.lease_fresh, state.speech_fresh, state.transfer_fresh, state.pbx_fresh))
                ready = ready and not state.lab_only and state.evidence_customer == CUSTOMER
                ready = ready and state.revoked_module is None
                return Result([{"version": state.pbx_version}] if ready else [])
            raise AssertionError("Unexpected SQL in the read-only call gate")

    connection = Connection()

    def transaction():
        state.transactions += 1
        return nullcontext(connection)

    monkeypatch.setattr(voice_pbx.db, "tx", transaction)
    monkeypatch.setattr(voice_pbx.inbox, "ai_available", lambda: state.model_available)

    def no_initialisation(*_args, **_kwargs):
        raise AssertionError("The read-only call gate must not initialise settings")

    monkeypatch.setattr(voice_pbx.inbox, "settings", no_initialisation)
    monkeypatch.setenv(pbx.SECRET_ENV, SECRET)
    for name in livekit.SETTINGS:
        monkeypatch.setenv(name, "configured-but-never-contacted-for-this-test")
    monkeypatch.setenv("EXA_LIVEKIT_URL", "ws://livekit:7880")
    app = FastAPI()
    app.include_router(voice_pbx.public, prefix="/api/v1/commai")
    with TestClient(app) as client:
        state.client = client
        yield state


def _request(gate, *, params=None, signed=True, extra_headers=None):
    fields = {"tenant": domain(CUSTOMER).split(".")[0], "to": DID, "call": CALL}
    fields.update(params or {})
    headers = dict(extra_headers or {})
    if signed:
        headers[pbx.HEADER] = pbx.sign(SECRET, fields["tenant"], "", "", fields["to"], fields["call"])
    return gate.client.get(PATH, params=fields, headers=headers)


def test_valid_signature_and_current_tenant_evidence_return_exact_ok(gate):
    response = _request(gate)
    assert response.status_code == 200 and response.text == "OK"
    assert response.headers["content-type"].startswith("text/plain")
    assert gate.transactions == 1  # reuse the gate connection for phone_ready
    assert len(gate.queries) == 5
    assert all(sql.lstrip().upper().startswith("SELECT ") for sql, _params in gate.queries)


@pytest.mark.parametrize("expired", ["lease_fresh", "speech_fresh", "transfer_fresh", "pbx_fresh"])
def test_any_expired_readiness_evidence_refuses_the_bridge(gate, expired):
    setattr(gate, expired, False)
    response = _request(gate)
    assert response.status_code == 200 and response.text == "NO"


@pytest.mark.parametrize("version", ["1.11.2", "1.10.12", "1.11.3-rc1", "unknown"])
def test_older_controllers_secure_boolean_cannot_bypass_the_current_binary_floor(gate, version):
    gate.pbx_version = version
    # Simulate a fresh receipt whose version_secure field was computed before
    # the stronger baseline. Current admission must parse the version again.
    assert _request(gate).text == "NO"


@pytest.mark.parametrize("module", ["voice", "ai_agents"])
def test_module_revocation_blocks_call_admission_despite_fresh_operational_evidence(gate, module):
    gate.revoked_module = module
    assert _request(gate).text == "NO"


@pytest.mark.parametrize("mismatch", ["owned_number", "evidence", "tenant"])
def test_another_tenants_did_or_readiness_never_authorises_this_call(gate, mismatch):
    params = {}
    if mismatch == "owned_number":
        gate.number_customer = OTHER
    elif mismatch == "evidence":
        gate.evidence_customer = OTHER
    else:
        params["tenant"] = domain(OTHER).split(".")[0]
    response = _request(gate, params=params)
    assert response.status_code == 200 and response.text == "NO"


@pytest.mark.parametrize("problem", ["disabled_rule", "other_number_rule", "inactive_number", "lab_receipt"])
def test_disabled_or_unrelated_rule_and_unusable_number_are_refused(gate, problem):
    if problem == "disabled_rule":
        gate.rule_enabled = False
    elif problem == "other_number_rule":
        gate.rule_number = uuid.uuid4()
    elif problem == "inactive_number":
        gate.number_active = False
    else:
        gate.lab_only = True
    response = _request(gate)
    assert response.status_code == 200 and response.text == "NO"


def test_enabled_all_number_rule_still_requires_owned_did_and_current_readiness(gate):
    gate.rule_number = None
    assert _request(gate).text == "OK"
    gate.lease_fresh = False
    assert _request(gate).text == "NO"


@pytest.mark.parametrize("unavailable", ["profile_off", "model_off", "profile_absent"])
def test_current_ai_availability_is_checked_without_initialising_a_profile(gate, unavailable):
    if unavailable == "profile_off":
        gate.profile["ai"]["enabled"] = False
    elif unavailable == "model_off":
        gate.model_available = False
    else:
        gate.profile_present = False
    response = _request(gate)
    assert response.status_code == 200 and response.text == "NO"
    assert len(gate.queries) == 3
    assert all(sql.lstrip().upper().startswith("SELECT ") for sql, _params in gate.queries)
    assert not any("FROM voice_ai_phone_runtime" in sql for sql, _params in gate.queries)


@pytest.mark.parametrize(
    "params",
    [
        {"tenant": "other"},
        {"tenant": "c111111111111\n"},
        {"to": "18685550100;execute=system"},
        {"to": ""},
        {"call": "not-a-call"},
        {"call": "aaaaaaaa\n"},
    ],
)
def test_signed_but_malformed_fields_return_no_without_database_reads(gate, params):
    response = _request(gate, params=params)
    assert response.status_code == 200 and response.text == "NO"
    assert gate.transactions == 0 and gate.queries == []


def test_wrong_digest_is_401_before_any_database_read(gate):
    response = _request(gate, signed=False, extra_headers={pbx.HEADER: "0" * 32})
    assert response.status_code == 401
    assert gate.transactions == 0 and gate.queries == []


def test_signed_fields_cannot_be_substituted_after_signing(gate):
    signature = pbx.sign(SECRET, domain(CUSTOMER).split(".")[0], "", "", DID, CALL)
    response = _request(gate, params={"to": "18685550999"}, signed=False, extra_headers={pbx.HEADER: signature})
    assert response.status_code == 401 and gate.queries == []


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer fictional-portal-session"},
        {"X-Role": "admin", "X-Exa-Role": "admin", "X-Requested-With": "exa-portal"},
        {"X-Forwarded-User": "admin@example.invalid", "X-Forwarded-Proto": "https"},
    ],
)
def test_portal_or_public_role_headers_cannot_replace_pbx_authentication(gate, headers):
    response = _request(gate, signed=False, extra_headers=headers)
    assert response.status_code == 401
    assert gate.transactions == 0 and gate.queries == []


def test_there_is_no_public_voice_alias_for_call_admission(gate):
    assert gate.client.get("/api/v1/commai/voice/phone-authorise").status_code == 404
