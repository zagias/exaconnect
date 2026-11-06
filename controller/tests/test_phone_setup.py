"""Tenant-scoped setup evidence, with no provider calls or settings writes."""

from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from exaconnect_controller import db
from exaconnect_controller.api.deps import User, current_user
from exaconnect_controller.commai.ai import voice as ai_voice
from exaconnect_controller.commai.api import voice
from exaconnect_controller.commai.voice import livekit, phone_setup, phone_worker
from exaconnect_controller.commai.voice.common import VoiceError, domain

CID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def setup(monkeypatch):
    state = SimpleNamespace(
        profile={"config": {"ai": {"enabled": True}}},
        model=True,
        entitled=True,
        voice_entitled=True,
        routing={"active_numbers": 2, "routed_numbers": 1},
        pbx={
            "version": "1.11.3",
            "transfer_enabled": True,
            "fresh": True,
            "checked_at": "2026-11-02T00:00:00Z",
            "expires_at": "2026-11-02T00:05:00Z",
        },
        runtime={
            "lab_only": False,
            "fresh": True,
            "speech_fresh": True,
            "transfer_fresh": True,
            "speech_verified_at": "recent",
            "transfer_verified_at": "recent",
            "expires_at": "soon",
        },
        admission=True,
        queries=[],
    )

    class Result:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return deepcopy(self.row)

    class Connection:
        def execute(self, sql, params=()):
            state.queries.append((sql, params))
            assert sql.lstrip().startswith("SELECT"), "Diagnostics must never change configuration or evidence"
            if "voice_ai_phone_pbx_runtime" in sql:
                return Result(state.pbx)
            assert params[0] in (CID, OTHER)
            if "commai_entitlements" in sql:
                assert params[1] in ("voice", "ai_agents")
                return Result({"enabled": state.voice_entitled if params[1] == "voice" else state.entitled})
            if "commai_settings" in sql:
                return Result(state.profile if params[0] == CID else None)
            if "voice_numbers" in sql:
                assert "r.customer_id = n.customer_id" in sql
                assert "n.customer_id = %s" in sql and "n.provider <> 'simulated'" in sql
                return Result(state.routing if params[0] == CID else {"active_numbers": 0, "routed_numbers": 0})
            if "voice_ai_phone_runtime" in sql:
                assert "WHERE customer_id = %s" in sql
                return Result(state.runtime if params[0] == CID else None)
            raise AssertionError("Unexpected diagnostic query")

    conn = Connection()
    for name in livekit.SETTINGS:
        monkeypatch.setenv(name, "NEVER-EXPOSE-THIS-CREDENTIAL")
    monkeypatch.setenv("EXA_LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("EXA_PHONE_HANDOVER_HOST", "freeswitch:5080")
    monkeypatch.setattr(phone_setup.inbox, "ai_available", lambda: state.model)

    def admission(cid, *, conn):
        assert cid == CID and isinstance(conn, Connection)
        return state.admission

    monkeypatch.setattr(livekit, "phone_ready", admission)
    return state, conn


def test_complete_setup_is_scoped_and_never_claims_a_human_answered(setup):
    state, conn = setup
    out = phone_setup.snapshot(conn, CID, can_manage_agent=True)
    assert out["ready"] and out["status"] == "ready" and not out["human_answered"]
    assert out["routing"] == {"active_numbers": 2, "routed_numbers": 1}
    assert "call rules still decide" in out["summary"]
    assert out["blockers"] == [] and "NEVER-EXPOSE" not in str(out)
    assert state.queries  # Every result came from reads, not a configured flag.


def test_revoked_voice_access_cannot_borrow_fresh_speech_evidence(setup):
    state, conn = setup
    state.voice_entitled = False
    out = phone_setup.snapshot(conn, CID, can_manage_agent=True)
    assert not out["ready"] and out["blockers"] == ["voice_access"]
    components = {component["id"]: component for component in out["components"]}
    assert components["voice_access"]["status"] == "blocked"
    assert components["voice_access"]["setup_action"] == "apps_plans"
    assert "disabled" in components["voice_access"]["detail"]
    assert components["assistant"]["status"] == "configured"
    assert components["speech_transfer"]["status"] == "verified"
    assert out["can_manage_agent"]  # Voice revocation does not revoke the separate AI permission.
    assert any(params == (CID, "voice") for _, params in state.queries)


@pytest.mark.parametrize("fresh", [True, False])
def test_patched_pbx_with_transfers_disabled_requests_policy_review(setup, fresh):
    state, conn = setup
    state.pbx.update(transfer_enabled=False, fresh=fresh)
    out = phone_setup.snapshot(conn, CID)
    pbx = next(component for component in out["components"] if component["id"] == "pbx")
    assert not out["ready"] and pbx["status"] == "blocked"
    assert "patched" in pbx["detail"] and "transfers disabled" in pbx["detail"]
    assert "review the transfer policy" in pbx["next_action"]
    assert "keep the current restriction" in pbx["next_action"]
    assert "upgrade" not in pbx["next_action"].lower()


def test_expired_patched_pbx_receipt_requests_probe_refresh(setup):
    state, conn = setup
    state.pbx["fresh"] = False
    out = phone_setup.snapshot(conn, CID)
    pbx = next(component for component in out["components"] if component["id"] == "pbx")
    assert not out["ready"] and pbx["status"] == "expired"
    assert "expired" in pbx["detail"] and "unverified" in pbx["detail"]
    assert "refresh the signed PBX runtime probe" in pbx["next_action"]
    assert "upgrade" not in pbx["next_action"].lower()


@pytest.mark.parametrize("version", ["1.10.12", "1.11.2", "unknown"])
def test_unsafe_pbx_receipt_requests_upgrade(setup, version):
    state, conn = setup
    state.pbx["version"] = version
    out = phone_setup.snapshot(conn, CID)
    pbx = next(component for component in out["components"] if component["id"] == "pbx")
    assert not out["ready"] and pbx["status"] == "blocked"
    assert "upgrade FreeSWITCH" in pbx["next_action"]


@pytest.mark.parametrize("host", ["pbx..internal", "256.1.2.3", "127.1", "pbx:65536", "pbx:0", "pbx@other"])
def test_invalid_handover_host_is_an_honest_setup_blocker(setup, monkeypatch, host):
    _, conn = setup
    monkeypatch.setenv("EXA_PHONE_HANDOVER_HOST", host)
    out = phone_setup.snapshot(conn, CID)
    assert not out["ready"] and out["blockers"] == ["handover"]
    handover = next(component for component in out["components"] if component["id"] == "handover")
    assert handover["status"] == "missing" and "invalid" in handover["detail"]


def test_setup_validates_handover_host_before_its_diagnostic_reads(setup, monkeypatch):
    state, conn = setup
    monkeypatch.setenv("EXA_PHONE_HANDOVER_HOST", "pbx:65536")
    validate = phone_setup.valid_handover_host
    validated = []

    def checked(host):
        assert not state.queries
        validated.append(host)
        return validate(host)

    monkeypatch.setattr(phone_setup, "valid_handover_host", checked)
    out = phone_setup.snapshot(conn, CID)
    assert validated == ["pbx:65536"] and out["blockers"] == ["handover"]
    assert state.queries  # Other components still report their own read-only diagnostics.


@pytest.mark.parametrize(
    "failure",
    [
        "no_livekit",
        "invalid_livekit",
        "profile_off",
        "no_model",
        "no_module",
        "no_voice_module",
        "no_did",
        "no_rule",
        "no_handover",
        "no_pbx",
        "old_pbx",
        "refer_only_patch",
        "stale_pbx",
        "transfers_disabled",
        "no_runtime",
        "lab_runtime",
        "expired_runtime",
        "stale_speech",
        "stale_transfer",
        "admission_denied",
    ],
)
def test_configuration_and_lab_flags_cannot_promote_readiness(setup, monkeypatch, failure):
    state, conn = setup
    if failure == "no_livekit":
        monkeypatch.delenv("EXA_LIVEKIT_AGENT_SECRET")
    elif failure == "invalid_livekit":
        monkeypatch.setenv("EXA_LIVEKIT_URL", "https://user:NEVER-EXPOSE@livekit/")
    elif failure == "profile_off":
        state.profile["config"]["ai"]["enabled"] = False
    elif failure == "no_model":
        state.model = False
    elif failure == "no_module":
        state.entitled = False
    elif failure == "no_voice_module":
        state.voice_entitled = False
    elif failure == "no_did":
        state.routing = {"active_numbers": 0, "routed_numbers": 0}
    elif failure == "no_rule":
        state.routing["routed_numbers"] = 0
    elif failure == "no_handover":
        monkeypatch.delenv("EXA_PHONE_HANDOVER_HOST")
    elif failure == "no_pbx":
        state.pbx = None
    elif failure == "old_pbx":
        state.pbx["version"] = "1.10.12"
    elif failure == "refer_only_patch":
        state.pbx["version"] = "1.11.2"
    elif failure == "stale_pbx":
        state.pbx["fresh"] = False
    elif failure == "transfers_disabled":
        state.pbx["transfer_enabled"] = False
    elif failure == "no_runtime":
        state.runtime = None
    elif failure == "lab_runtime":
        state.runtime["lab_only"] = True
    elif failure == "expired_runtime":
        state.runtime["fresh"] = False
    elif failure == "stale_speech":
        state.runtime["speech_fresh"] = False
    elif failure == "stale_transfer":
        state.runtime["transfer_fresh"] = False
    elif failure == "admission_denied":
        state.admission = False
    out = phone_setup.snapshot(conn, CID)
    assert not out["ready"] and out["status"] == "setup_required"
    assert "NEVER-EXPOSE" not in str(out)
    if failure == "no_module":
        assert not out["can_manage_agent"]
        assert next(c for c in out["components"] if c["id"] == "assistant")["setup_action"] == "apps_plans"


def test_another_tenant_cannot_borrow_configuration_or_evidence(setup):
    _, conn = setup
    out = phone_setup.snapshot(conn, OTHER)
    assert not out["ready"]
    assert out["routing"] == {"active_numbers": 0, "routed_numbers": 0}
    assert "speech_transfer" in out["blockers"]


def test_route_requires_signin_and_voice_admin_before_diagnostics(setup, monkeypatch):
    _, conn = setup
    monkeypatch.setattr(voice.db, "tx", lambda: nullcontext(conn))
    app = FastAPI()
    app.include_router(voice.router, prefix="/api/v1/commai")
    path = f"/api/v1/commai/customers/{CID}/voice/phone/setup"
    with TestClient(app) as client:
        assert client.get(path).status_code == 401
        app.dependency_overrides[current_user] = lambda: User("user", "owner@example.org", "admin", None, None)

        def denied(*_):
            raise HTTPException(403, "Voice admin required")

        monkeypatch.setattr(voice, "_admin", denied)
        assert client.get(path).status_code == 403
        monkeypatch.setattr(voice, "_admin", lambda *_: None)
        monkeypatch.setattr(voice.access, "seat", lambda *_: "internal")
        out = client.get(path)
        assert out.status_code == 200 and out.json()["can_manage_agent"] is False


@pytest.mark.parametrize("integration_permission", [False, True])
def test_setup_ai_action_checks_the_ai_routes_custom_role_permission(setup, monkeypatch, integration_permission):
    _, conn = setup
    monkeypatch.setattr(voice.db, "tx", lambda: nullcontext(conn))
    monkeypatch.setattr(voice, "_admin", lambda *_: None)  # Delegated voice administration was already checked.
    monkeypatch.setattr(voice.access, "seat", lambda *_: "agent")
    path = f"/api/v1/commai/customers/{CID}/voice/phone/setup"
    permissions = {"read_inbox", "voice_admin"}
    if integration_permission:
        permissions.add("manage_integrations")
    user = User(
        "user",
        "owner@example.org",
        "customer",
        CID,
        None,
        org_role="admin",
        permissions=frozenset(permissions),
        path=path,
    )
    app = FastAPI()
    app.include_router(voice.router, prefix="/api/v1/commai")
    app.dependency_overrides[current_user] = lambda: user
    with TestClient(app) as client:
        result = client.get(path)
    assert result.status_code == 200
    out = result.json()
    assert out["can_manage_agent"] is integration_permission
    component = next(c for c in out["components"] if c["id"] == "assistant")
    assert component["setup_action"] == ("agent_profile" if integration_permission else None)
    assert user.path == path  # Permission calculation cannot change the request's own role context.


def test_postgres_setup_counts_only_owned_real_numbers_and_does_not_initialise_settings(client, admin_headers):
    """Run real aggregate/lease SQL in isolated PostgreSQL, without any media service."""
    with db.tx() as conn:
        a = conn.execute("INSERT INTO customers (name) VALUES ('Phone setup A') RETURNING id").fetchone()["id"]
        b = conn.execute("INSERT INTO customers (name) VALUES ('Phone setup B') RETURNING id").fetchone()["id"]
        conn.execute("INSERT INTO commai_settings (customer_id) VALUES (%s)", (a,))
        conn.execute(
            "INSERT INTO voice_numbers (customer_id, e164, provider, status) "
            "VALUES (%s, '+18685550110', 'byo', 'active')",
            (a,),
        )
        conn.execute(
            "INSERT INTO voice_numbers (customer_id, e164, provider, status) "
            "VALUES (%s, '+18685550111', 'simulated', 'active')",
            (b,),
        )
        conn.execute(
            "INSERT INTO voice_ai_rules (customer_id, name, condition) VALUES (%s, 'Prepared', 'always')", (a,)
        )
    path = "/api/v1/commai/customers/{}/voice/phone/setup"
    first = client.get(path.format(a), headers=admin_headers)
    second = client.get(path.format(b), headers=admin_headers)
    assert first.status_code == second.status_code == 200
    assert first.json()["routing"] == {"active_numbers": 1, "routed_numbers": 1}
    assert second.json()["routing"] == {"active_numbers": 0, "routed_numbers": 0}
    assert not first.json()["ready"] and not second.json()["ready"]
    with db.tx() as conn:
        assert conn.execute("SELECT 1 FROM commai_settings WHERE customer_id = %s", (b,)).fetchone() is None
        assert conn.execute("SELECT count(*) AS n FROM voice_ai_phone_runtime").fetchone()["n"] == 0


@pytest.mark.parametrize("revoked", ["voice", "ai_agents", None])
def test_acceptance_call_cannot_bypass_module_revocation(monkeypatch, revoked):
    class Connection:
        def execute(self, sql, params):
            assert "FROM commai_entitlements" in sql and params[0] == CID
            self.module = params[1]
            return self

        def fetchone(self):
            return {"enabled": False} if self.module == revoked else None

    tenant = domain(CID).split(".")[0]
    monkeypatch.setattr(phone_worker, "require_pbx", lambda _: None)
    monkeypatch.setattr(
        livekit,
        "sip_participant",
        lambda *_: {
            "sip.trunkPhoneNumber": "+18685550112",
            "exa.customer_id": CID,
            "exa.tenant": tenant,
        },
    )
    monkeypatch.setattr(livekit, "business_for", lambda *_: {"customer_id": CID})

    def fallback(*_):
        raise VoiceError("Reached fallback validation", 503)

    monkeypatch.setattr(livekit, "handover_target", fallback)
    with pytest.raises(VoiceError) as error:
        phone_worker.start(
            Connection(),
            {
                "room": f"call-{tenant}-fixture",
                "participant_identity": "sip",
                "sip_call_id": "fixture",
                "acceptance_evidence_id": str(uuid4()),
            },
        )
    assert error.value.code == (403 if revoked else 503)
    assert ("modules enabled" if revoked else "fallback validation") in str(error.value)


def test_postgres_fresh_lease_loses_admission_on_own_module_revocation(client, monkeypatch):
    """Fictional rows in an isolated DB test SQL only; no live acceptance receipt."""
    for name in livekit.SETTINGS:
        monkeypatch.setenv(name, "fixture-only")
    monkeypatch.setenv("EXA_LIVEKIT_URL", "ws://livekit:7880")
    with db.tx() as conn:
        a = conn.execute("INSERT INTO customers (name) VALUES ('Lease fixture A') RETURNING id").fetchone()["id"]
        b = conn.execute("INSERT INTO customers (name) VALUES ('Lease fixture B') RETURNING id").fetchone()["id"]
        call = ai_voice.start_call(conn, a, started_by="livekit")
        session = conn.execute(
            """INSERT INTO voice_ai_phone_sessions (customer_id, conversation_id, token_hash, worker_id,
               provider_fingerprint, room_name, participant_identity, sip_call_id, transfer_target)
               VALUES (%s, %s, %s, 'fixture', %s, 'fixture-room', 'fixture-person', 'fixture-call', 'fixture-target')
               RETURNING id""",
            (a, call["conversation_id"], "a" * 64, "b" * 64),
        ).fetchone()["id"]
        conn.execute(
            """INSERT INTO voice_ai_phone_runtime (worker_id, customer_id, provider_fingerprint,
               speech_verified_at, transfer_verified_at, transfer_session_id, lab_only, expires_at)
               VALUES ('fixture', %s, %s, now(), now(), %s, false, now() + interval '75 seconds')""",
            (a, "b" * 64, session),
        )
        conn.execute(
            """INSERT INTO voice_ai_phone_pbx_runtime (instance_id, version, version_secure, transfer_enabled,
               checked_at, nonce, expires_at) VALUES ('main', '1.11.3', true, true, now(), %s,
               now() + interval '5 minutes')""",
            (uuid4(),),
        )
        conn.execute(
            """INSERT INTO voice_ai_phone_runtime (worker_id, customer_id, provider_fingerprint,
               speech_verified_at, transfer_verified_at, transfer_session_id, lab_only, expires_at)
               VALUES ('stale-fixture', %s, %s, now() - interval '2 minutes', now(), %s, false,
               now() + interval '5 minutes')""",
            (a, "c" * 64, session),
        )
        # A longer-expiry but stale worker cannot hide another worker's valid
        # operational proof in the setup view. This remains fixture evidence.
        setup_status = phone_setup.snapshot(conn, a)
        measured = next(c for c in setup_status["components"] if c["id"] == "speech_transfer")
        assert measured["status"] == "verified"
        assert livekit.phone_ready(a, conn=conn)  # No entitlement rows retains existing access.
        for module in ("voice", "ai_agents"):
            conn.execute(
                "INSERT INTO commai_entitlements (customer_id, module, enabled, updated_by) "
                "VALUES (%s, %s, false, 'fixture')",
                (b, module),
            )
            assert livekit.phone_ready(a, conn=conn)  # Another business's revocation cannot affect A.
            conn.execute(
                "INSERT INTO commai_entitlements (customer_id, module, enabled, updated_by) "
                "VALUES (%s, %s, false, 'fixture')",
                (a, module),
            )
            assert not livekit.phone_ready(a, conn=conn)
            conn.execute("DELETE FROM commai_entitlements WHERE customer_id = %s AND module = %s", (a, module))
        conn.execute("UPDATE voice_ai_phone_pbx_runtime SET version = '1.11.2', version_secure = true")
        assert not livekit.phone_ready(a, conn=conn)  # A legacy secure flag cannot survive the new floor.
