"""Authenticated, call-scoped telephone worker protocol.

The bootstrap credential cannot operate arbitrary browser conversations.
Every mutating call request needs a random capability issued for a server-
verified SIP participant. Operational readiness is a short evidence lease.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import uuid
from typing import Any

from psycopg.types.json import Jsonb

from .. import inbox, jobs
from ..ai import voice as ai_voice
from . import livekit
from . import provider as providers
from .common import VoiceError, domain

WORDS = "amber blue cedar cloud coral dawn fern field green lake maple ocean rain river silver sky stone sun".split()


def pbx_receipt(conn, body: dict, authorization: str | None) -> dict:
    secret = os.environ.get("EXA_PBX_SECRET", "")
    if not secret:
        raise VoiceError("The PBX runtime link is not configured.", 503)
    signature = hmac.new(
        secret.encode(), json.dumps(body, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, authorization or ""):
        raise VoiceError("Not allowed.", 401)
    try:
        checked = dt.datetime.fromisoformat(body["checked_at"].replace("Z", "+00:00"))
        if checked.tzinfo is None or abs((dt.datetime.now(dt.UTC) - checked).total_seconds()) > 30:
            raise ValueError("Stale receipt")
    except ValueError:
        raise VoiceError("The PBX runtime receipt is stale or invalid.", 409) from None
    conn.execute("DELETE FROM voice_ai_phone_pbx_receipts WHERE seen_at < now() - interval '1 minute'")
    claimed = conn.execute(
        "INSERT INTO voice_ai_phone_pbx_receipts (nonce) VALUES (%s) ON CONFLICT DO NOTHING RETURNING nonce",
        (body["nonce"],),
    ).fetchone()
    if claimed is None:
        raise VoiceError("The PBX runtime receipt has already been used.", 409)
    # CVE/GHSA-xjxm-9qx6-w3fj: REFER variable injection is fixed in 1.11.2.
    # Unknown versions fail closed; the running binary's version is checked
    # here independently of the probe's transfer-enabled observation.
    match = re.fullmatch(r"(?:FreeSWITCH Version )?(\d+)\.(\d+)\.(\d+)(?:-release)?(?:[+ ].*)?", body["version"])
    prerelease = re.search(r"(?i)(dev|rc|alpha|beta|snapshot)", body["version"])
    secure = bool(match and not prerelease and tuple(map(int, match.groups())) >= (1, 11, 2))
    conn.execute(
        """INSERT INTO voice_ai_phone_pbx_runtime
           (instance_id, version, version_secure, transfer_enabled, checked_at, nonce, expires_at)
           VALUES (%s, %s, %s, %s, %s, %s, now() + interval '5 minutes')
           ON CONFLICT (instance_id) DO UPDATE SET version = excluded.version,
           version_secure = excluded.version_secure, transfer_enabled = excluded.transfer_enabled,
           checked_at = excluded.checked_at, nonce = excluded.nonce, expires_at = excluded.expires_at""",
        (body["instance_id"], body["version"], secure, body["transfer_enabled"], checked, body["nonce"]),
    )
    return {"pbx_transfer_ready": secure and body["transfer_enabled"], "expires_in_s": 300}


def require_pbx(conn) -> None:
    row = conn.execute(
        """SELECT 1 FROM voice_ai_phone_pbx_runtime WHERE instance_id = 'main'
           AND version_secure AND transfer_enabled AND expires_at > now()"""
    ).fetchone()
    if row is None:
        raise VoiceError("A fresh patched PBX transfer runtime check is required.", 503)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _phrase(text: str) -> str:
    return " ".join(re.findall(r"[a-z]+", text.lower()))


def challenge(conn, worker_id: str, fingerprint: str) -> dict:
    phrase = " ".join(secrets.choice(WORDS) for _ in range(6))
    row = conn.execute(
        """INSERT INTO voice_ai_phone_evidence (worker_id, provider_fingerprint, phrase)
           VALUES (%s, %s, %s) RETURNING id, expires_at""",
        (worker_id, fingerprint, phrase),
    ).fetchone()
    return {"id": str(row["id"]), "phrase": phrase, "expires_at": row["expires_at"]}


def prove(conn, evidence_id: str, transcript: str, worker_id: str, fingerprint: str) -> dict:
    evidence = conn.execute(
        """SELECT * FROM voice_ai_phone_evidence WHERE id = %s AND worker_id = %s
           AND provider_fingerprint = %s AND expires_at > now() AND proved_at IS NULL FOR UPDATE""",
        (evidence_id, worker_id, fingerprint),
    ).fetchone()
    if evidence is None or not hmac.compare_digest(_phrase(transcript), evidence["phrase"]):
        raise VoiceError("Speech loopback was not verified, or its challenge expired.", 409)
    conn.execute("UPDATE voice_ai_phone_evidence SET proved_at = now() WHERE id = %s", (evidence_id,))
    # Refresh only an already proven transfer, never manufacture one from a
    # speech check or readiness boolean. A real transfer expires after a day.
    conn.execute(
        """UPDATE voice_ai_phone_runtime SET speech_verified_at = now(),
                  expires_at = least(now() + interval '75 seconds', transfer_verified_at + interval '24 hours')
           WHERE worker_id = %s AND provider_fingerprint = %s
             AND transfer_verified_at > now() - interval '24 hours'""",
        (worker_id, fingerprint),
    )
    return {"speech_verified": True, "evidence_id": evidence_id}


def _speech_evidence(conn, evidence_id: str, worker_id: str, fingerprint: str) -> dict:
    row = conn.execute(
        """SELECT * FROM voice_ai_phone_evidence WHERE id = %s AND worker_id = %s
           AND provider_fingerprint = %s AND proved_at > now() - interval '60 seconds' FOR UPDATE""",
        (evidence_id, worker_id, fingerprint),
    ).fetchone()
    if row is None:
        raise VoiceError("A fresh measured speech loopback is required.", 503)
    return row


def media_proof(conn, evidence_id: str, body: dict) -> dict:
    evidence = _speech_evidence(conn, evidence_id, body["worker_id"], body["provider_fingerprint"])
    if evidence["media_proved_at"] is not None or not hmac.compare_digest(
        _phrase(body["transcript"]), evidence["phrase"]
    ):
        raise VoiceError("The SIP media challenge was not verified or was already used.", 409)
    livekit.sip_participant(body["room"], body["participant_identity"], body["sip_call_id"])
    conn.execute(
        """UPDATE voice_ai_phone_evidence SET media_proved_at = now(), proved_at = now(), room_name = %s,
                  participant_identity = %s, sip_call_id = %s WHERE id = %s""",
        (body["room"], body["participant_identity"], body["sip_call_id"], evidence_id),
    )
    return {"media_verified": True}


def start(conn, body: dict) -> dict:
    require_pbx(conn)
    attrs = livekit.sip_participant(body["room"], body["participant_identity"], body["sip_call_id"])
    # The dialled number is verified against the server's participant metadata;
    # the worker cannot select a tenant by posting an arbitrary telephone number.
    num = livekit.business_for(conn, attrs.get("sip.trunkPhoneNumber", ""))
    cid = num["customer_id"]
    tenant = domain(cid).split(".")[0]
    if (
        attrs.get("exa.customer_id") != str(cid)
        or attrs.get("exa.tenant") != tenant
        or not body["room"].startswith(f"call-{tenant}-")
    ):
        raise VoiceError("The managed SIP dispatch does not match the business that owns this number.", 403)
    target = livekit.handover_target(conn, cid)
    evidence_id = body.get("acceptance_evidence_id")
    if evidence_id:
        proof = _speech_evidence(conn, evidence_id, body["worker_id"], body["provider_fingerprint"])
        if proof["media_proved_at"] is None or any(
            proof[key] != body[field]
            for key, field in (
                ("room_name", "room"),
                ("participant_identity", "participant_identity"),
                ("sip_call_id", "sip_call_id"),
            )
        ):
            raise VoiceError("The speech media proof does not belong to this actual SIP call.", 503)
    else:
        ready = conn.execute(
            """SELECT 1 FROM voice_ai_phone_runtime WHERE worker_id = %s AND provider_fingerprint = %s
               AND customer_id = %s
               AND NOT lab_only AND expires_at > now() AND speech_verified_at > now() - interval '90 seconds'
               AND transfer_verified_at > now() - interval '24 hours'""",
            (body["worker_id"], body["provider_fingerprint"], cid),
        ).fetchone()
        if ready is None:
            raise VoiceError("Telephone AI is not ready. Use the configured call fallback.", 503)
    exists = conn.execute(
        """SELECT 1 FROM voice_ai_phone_sessions
           WHERE room_name = %s AND participant_identity = %s AND sip_call_id = %s""",
        (body["room"], body["participant_identity"], body["sip_call_id"]),
    ).fetchone()
    if exists:
        raise VoiceError("That SIP call already has a worker session.", 409)
    out = ai_voice.start_call(conn, cid, caller=attrs.get("sip.phoneNumber", ""), started_by="livekit")
    capability = secrets.token_urlsafe(32)
    session = conn.execute(
        """INSERT INTO voice_ai_phone_sessions
           (customer_id, conversation_id, token_hash, worker_id, provider_fingerprint, room_name,
            participant_identity, sip_call_id, transfer_target, acceptance)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id, expires_at""",
        (
            cid,
            out["conversation_id"],
            _digest(capability),
            body["worker_id"],
            body["provider_fingerprint"],
            body["room"],
            body["participant_identity"],
            body["sip_call_id"],
            target,
            bool(evidence_id),
        ),
    ).fetchone()
    jobs.enqueue(
        conn,
        "ai.phone_watchdog",
        {"session_id": str(session["id"]), "tick": 0},
        customer_id=cid,
        dedupe_key=f"phone-watchdog:{session['id']}:0",
        delay_s=30,
    )
    return {
        "session_id": str(session["id"]),
        "customer_id": str(cid),
        "conversation_id": out["conversation_id"],
        "room": body["room"],
        "greeting": out["greeting"],
        "handler": out["handler"],
        "session_token": capability,
        "expires_at": session["expires_at"],
        "max_duration_s": 1800,
    }


def session(conn, cid: str, conversation_id: str, authorization: str | None, *, active: bool = True) -> dict:
    scheme, _, value = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise VoiceError("A call-scoped worker capability is required.", 401)
    row = conn.execute(
        """SELECT *, expires_at > now() AS valid FROM voice_ai_phone_sessions
           WHERE customer_id = %s AND conversation_id = %s FOR UPDATE""",
        (cid, conversation_id),
    ).fetchone()
    if row is None or not hmac.compare_digest(row["token_hash"], _digest(value.strip())):
        raise VoiceError("Call not found.", 404)
    if active and (row["ended_at"] is not None or not row["valid"]):
        raise VoiceError("This phone worker session has ended.", 409)
    return row


def state(conn, row: dict) -> dict:
    if row["ended_at"] is None and row["valid"]:
        conn.execute("UPDATE voice_ai_phone_sessions SET heartbeat_at = now() WHERE id = %s", (row["id"],))
    conv = inbox.get(conn, row["customer_id"], row["conversation_id"])
    return {
        "handed_over": conv["handler"] != "ai",
        "handover_state": row["handover_state"],
        "ended": row["ended_at"] is not None or not row["valid"],
    }


def begin_turn(conn, row: dict, text: str, request_id: Any) -> tuple[dict | None, dict | None]:
    existing = conn.execute(
        "SELECT result FROM voice_ai_phone_turns WHERE session_id = %s AND request_id = %s",
        (row["id"], request_id),
    ).fetchone()
    if existing:
        if existing["result"] is None:
            raise VoiceError("This caller turn is still being processed.", 409)
        current = state(conn, row)
        cached = {**existing["result"], **current}
        if current["handed_over"] or current["ended"]:
            cached["reply"] = ""
        return None, cached
    if state(conn, row)["handed_over"]:
        return None, {"reply": "", "handed_over": True, "handover_state": row["handover_state"]}
    inflight = conn.execute(
        "SELECT 1 FROM voice_ai_phone_turns WHERE session_id = %s AND result IS NULL LIMIT 1", (row["id"],)
    ).fetchone()
    if inflight:
        raise VoiceError("Another caller turn is still being processed.", 409)
    msg = ai_voice.caller_turn(conn, row["customer_id"], row["conversation_id"], text)
    conn.execute(
        "INSERT INTO voice_ai_phone_turns (session_id, request_id, message_id) VALUES (%s, %s, %s)",
        (row["id"], request_id, msg["id"]),
    )
    return msg, None


def complete_turn(conn, row: dict, request_id: Any, message: dict, outcome: dict) -> dict:
    replies = ai_voice.replies_after(conn, row["customer_id"], row["conversation_id"], message)
    out = {"outcome": outcome.get("outcome"), "reply": " ".join(r["body"] for r in replies), **state(conn, row)}
    if out["handed_over"] or out["ended"]:
        out["reply"] = ""  # Never speak a stale AI reply after human takeover/termination.
    conn.execute(
        "UPDATE voice_ai_phone_turns SET result = %s WHERE session_id = %s AND request_id = %s",
        (Jsonb(out), row["id"], request_id),
    )
    return out


def end(conn, row: dict, reason: str) -> dict:
    call = ai_voice.end_call(conn, row["customer_id"], row["conversation_id"], "livekit")
    conn.execute(
        """UPDATE voice_ai_phone_sessions SET ended_at = coalesce(ended_at, now()),
                  end_reason = CASE WHEN ended_at IS NULL THEN %s ELSE end_reason END WHERE id = %s""",
        (reason, row["id"]),
    )
    if reason in ("provider_failure", "service_failure", "startup_failure", "input_overflow"):
        conn.execute(
            """UPDATE voice_ai_phone_runtime SET expires_at = now()
               WHERE worker_id = %s AND provider_fingerprint = %s""",
            (row["worker_id"], row["provider_fingerprint"]),
        )
    return {"ended_at": call["ended_at"], "handover_state": row["handover_state"]}


def claim_transfer(conn, row: dict) -> bool:
    attempt_id = uuid.uuid4()
    claimed = conn.execute(
        """UPDATE voice_ai_phone_sessions SET handover_state = 'requested', cleanup_pending = true,
                  transfer_attempt_id = %s, transfer_lease_until = now() + interval '75 seconds'
           WHERE id = %s AND handover_state <> 'accepted'
             AND (transfer_lease_until IS NULL OR transfer_lease_until <= now()) RETURNING id""",
        (attempt_id, row["id"]),
    ).fetchone()
    if claimed:
        row["transfer_attempt_id"] = attempt_id
    return claimed is not None


def record_transfer(conn, row: dict, accepted: bool, *, allow_evidence: bool = True) -> dict:
    status = "accepted" if accepted else "failed"
    recorded = conn.execute(
        """UPDATE voice_ai_phone_sessions SET handover_state = %s,
                  cleanup_pending = CASE WHEN %s THEN false ELSE cleanup_pending END,
                  transfer_lease_until = CASE WHEN %s THEN NULL ELSE transfer_lease_until END
           WHERE id = %s AND transfer_attempt_id = %s AND handover_state = 'requested' RETURNING id""",
        (status, accepted, accepted, row["id"], row["transfer_attempt_id"]),
    ).fetchone()
    if recorded is None:
        current = conn.execute(
            "SELECT handover_state FROM voice_ai_phone_sessions WHERE id = %s", (row["id"],)
        ).fetchone()
        return {"state": current["handover_state"], "human_answered": False}
    if not accepted:
        conn.execute(
            """UPDATE voice_ai_phone_runtime SET expires_at = now(),
                      transfer_verified_at = now() - interval '25 hours'
               WHERE worker_id = %s AND customer_id = %s""",
            (row["worker_id"], row["customer_id"]),
        )
    if accepted and row["acceptance"] and allow_evidence:
        # This receipt came from the controller's own successful LiveKit RPC,
        # never from a worker-provided 'success' field.
        proof = conn.execute(
            """SELECT proved_at FROM voice_ai_phone_evidence WHERE worker_id = %s
               AND provider_fingerprint = %s AND proved_at > now() - interval '60 seconds'
               ORDER BY proved_at DESC LIMIT 1""",
            (row["worker_id"], row["provider_fingerprint"]),
        ).fetchone()
        if proof:
            conn.execute(
                """INSERT INTO voice_ai_phone_runtime
                   (worker_id, customer_id, provider_fingerprint, speech_verified_at, transfer_verified_at,
                    transfer_session_id, lab_only, expires_at)
                   VALUES (%s, %s, %s, %s, now(), %s, %s, now() + interval '75 seconds')
                   ON CONFLICT (worker_id, customer_id) DO UPDATE
                   SET provider_fingerprint = excluded.provider_fingerprint,
                   speech_verified_at = excluded.speech_verified_at, transfer_verified_at = now(),
                   transfer_session_id = excluded.transfer_session_id, lab_only = excluded.lab_only,
                   expires_at = excluded.expires_at""",
                (
                    row["worker_id"],
                    row["customer_id"],
                    row["provider_fingerprint"],
                    proof["proved_at"],
                    row["id"],
                    providers.test_mode(),
                ),
            )
    return {
        "state": status,
        "human_answered": False,
        "evidence_scope": "lab" if providers.test_mode() else "operational",
    }


@jobs.handler("ai.phone_watchdog")
def watchdog(conn, job: dict) -> None:
    row = conn.execute(
        """SELECT *, expires_at > now() AS valid,
                  heartbeat_at < now() - interval '30 seconds' AS abandoned
           FROM voice_ai_phone_sessions WHERE id = %s FOR UPDATE""",
        (job["payload"]["session_id"],),
    ).fetchone()
    if row is None or (row["ended_at"] is not None and not row["cleanup_pending"]):
        return

    def again() -> None:
        tick = job["payload"]["tick"] + 1
        jobs.enqueue(
            conn,
            "ai.phone_watchdog",
            {"session_id": str(row["id"]), "tick": tick},
            customer_id=row["customer_id"],
            dedupe_key=f"phone-watchdog:{row['id']}:{tick}",
            delay_s=15,
        )

    if row["ended_at"] is None and row["valid"] and not row["abandoned"]:
        again()
        return
    accepted = row["handover_state"] == "accepted"
    cleaned = accepted
    if not accepted:
        # Endpoint and watchdog share this claim. In particular, a 35-second
        # REFER cannot be duplicated by the 30-second missed-heartbeat check.
        if not claim_transfer(conn, row):
            again()
            return
        try:
            require_pbx(conn)
            livekit.transfer(row["room_name"], row["participant_identity"], row["sip_call_id"], row["transfer_target"])
            accepted = True
            cleaned = True
        except VoiceError:
            try:
                livekit.hangup(row["room_name"], row["participant_identity"], row["sip_call_id"])
                cleaned = True
            except VoiceError:
                cleaned = False
        record_transfer(conn, row, accepted, allow_evidence=False)
    if cleaned:
        conn.execute("UPDATE voice_ai_phone_sessions SET cleanup_pending = false WHERE id = %s", (row["id"],))
    if row["ended_at"] is None:
        end(conn, row, "worker_lost" if row["abandoned"] else "duration_limit")
    if not cleaned:
        # Billing termination and media termination are separate. Each retry
        # has bounded network deadlines and a durable dedupe key; an outage
        # cannot erase the obligation to clean up the answered SIP leg.
        again()
