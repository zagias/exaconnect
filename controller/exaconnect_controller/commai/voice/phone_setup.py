"""Read-only telephone setup evidence, scoped to one business.

This endpoint does not probe providers, initialise settings, provision SIP or
refresh readiness. Configuration and an accepted transfer are reported
separately from fresh operational proof and a person answering.
"""

from __future__ import annotations

import os
import re
from typing import Any

from .. import entitlements, inbox
from . import livekit, pbx_runtime


def snapshot(conn, customer_id: Any, *, can_manage_agent: bool = False) -> dict:
    components = []

    def add(id, status, title, detail, action, responsibility, setup_action=None, **evidence):
        components.append(
            dict(
                id=id,
                status=status,
                title=title,
                detail=detail,
                next_action=action,
                responsibility=responsibility,
                setup_action=setup_action,
                **evidence,
            )
        )

    missing = livekit.missing()
    media_configured = not missing and livekit.valid_url()
    add(
        "livekit",
        "missing" if missing else "configured" if media_configured else "blocked",
        "Phone media service",
        "The controller connection is incomplete."
        if not media_configured
        else "Connection settings exist; this does not prove a working media service.",
        "Ask the operator to configure the private LiveKit server and worker credentials.",
        "operator",
        missing=missing,
    )

    row = conn.execute("SELECT config FROM commai_settings WHERE customer_id = %s", (customer_id,)).fetchone()
    enabled = row is not None and (((row["config"] or {}).get("ai") or {}).get("enabled", True) is True)
    model = inbox.ai_available()
    entitled = entitlements.enabled(conn, customer_id, "ai_agents")
    assistant_ok = enabled and model and entitled
    add(
        "assistant",
        "configured" if assistant_ok else "blocked",
        "Business assistant",
        "The business assistant and model are enabled."
        if assistant_ok
        else "The business needs AI agent access, an enabled assistant and a connected model.",
        "Enable the business assistant in AI settings; ask the operator to connect its model if unavailable.",
        "business",
        setup_action="apps_plans" if not entitled else "agent_profile" if can_manage_agent else None,
    )

    routing = conn.execute(
        """SELECT count(*) AS active_numbers,
                  count(*) FILTER (WHERE EXISTS (
                    SELECT 1 FROM voice_ai_rules r WHERE r.customer_id = n.customer_id AND r.enabled
                    AND (r.number_id IS NULL OR r.number_id = n.id))) AS routed_numbers
           FROM voice_numbers n WHERE n.customer_id = %s AND n.status = 'active'
             AND n.provider <> 'simulated'""",
        (customer_id,),
    ).fetchone()
    has_routing = routing["routed_numbers"] > 0
    add(
        "number_routing",
        "configured" if has_routing else "missing",
        "Number and call fallback",
        "An active number has an enabled AI rule. Its condition and fallback still control routing."
        if has_routing
        else "An active, non-simulated number needs an AI rule and an existing human or voicemail fallback.",
        "Review your own trunk, numbers and AI rules in Call routing. Preserve the intended fallback.",
        "business",
        setup_action="call_routing" if routing["active_numbers"] else "numbers",
    )

    host = os.environ.get("EXA_PHONE_HANDOVER_HOST", "")
    handover = bool(re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?", host))
    add(
        "handover",
        "configured" if handover else "missing",
        "Private call handover",
        "An internal handover address is configured; SIP provisioning still requires operational proof."
        if handover
        else "The private PBX handover address is missing or invalid.",
        "Ask the operator to provision the restricted SIP trunk, owned-number dispatch and tenant handover route.",
        "operator",
    )

    pbx = conn.execute(
        """SELECT version, transfer_enabled, checked_at, expires_at, expires_at > now() AS fresh
           FROM voice_ai_phone_pbx_runtime WHERE instance_id = 'main'"""
    ).fetchone()
    secure = bool(pbx and pbx_runtime.version(pbx["version"])[1])
    pbx_ok = bool(pbx and secure and pbx["transfer_enabled"] and pbx["fresh"])
    pbx_status = (
        "not_verified"
        if not pbx
        else "blocked"
        if not secure or not pbx["transfer_enabled"]
        else "verified"
        if pbx["fresh"]
        else "expired"
    )
    add(
        "pbx",
        pbx_status,
        "Patched phone system",
        "A fresh trusted PBX receipt permits transfers."
        if pbx_ok
        else "A fresh trusted receipt must show a supported patched PBX with transfers available. "
        "Disabling transfers on an affected version is mitigation only.",
        "Ask the operator to upgrade FreeSWITCH to a supported patched release and restore the signed runtime probe.",
        "operator",
        **({k: pbx[k] for k in ("version", "checked_at", "expires_at")} if pbx else {}),
    )

    runtime = conn.execute(
        """SELECT lab_only, expires_at, speech_verified_at, transfer_verified_at,
                  expires_at > now() AS fresh,
                  speech_verified_at > now() - interval '90 seconds' AS speech_fresh,
                  transfer_verified_at > now() - interval '24 hours' AS transfer_fresh
           FROM voice_ai_phone_runtime WHERE customer_id = %s
           ORDER BY lab_only, (expires_at > now()
               AND speech_verified_at > now() - interval '90 seconds'
               AND transfer_verified_at > now() - interval '24 hours') DESC, expires_at DESC LIMIT 1""",
        (customer_id,),
    ).fetchone()
    measured = bool(
        runtime
        and not runtime["lab_only"]
        and runtime["fresh"]
        and runtime["speech_fresh"]
        and runtime["transfer_fresh"]
    )
    status = "verified" if measured else "expired" if runtime and not runtime["lab_only"] else "not_verified"
    add(
        "speech_transfer",
        status,
        "Speech and transfer evidence",
        "Fresh real speech and accepted SIP transfer evidence exists for this business. "
        "This does not confirm a person answered."
        if measured
        else "Real speech and SIP transfer have no fresh operational evidence for this business. "
        "Isolated lab evidence cannot enable live calls.",
        "Ask the operator to configure the worker's explicit speech providers, "
        "then obtain separately authorised speech and SIP acceptance evidence.",
        "operator",
        **({k: runtime[k] for k in ("speech_verified_at", "transfer_verified_at", "expires_at")} if runtime else {}),
    )

    # Keep the existing admission function authoritative; this view additionally
    # requires the business's own configuration to be usable. Never writes a lease.
    ready = (
        media_configured
        and assistant_ok
        and has_routing
        and handover
        and pbx_ok
        and measured
        and livekit.phone_ready(customer_id, conn=conn)
    )
    blockers = [c["id"] for c in components if c["status"] not in ("configured", "verified")]
    if not ready and not blockers:
        # A lease can expire between the explanatory reads and admission read.
        components[-1]["status"] = "expired"
        components[-1]["detail"] = "Current call admission did not confirm the evidence lease. Refresh this view."
        blockers.append("speech_transfer")
    return {
        "ready": bool(ready),
        "status": "ready" if ready else "setup_required",
        "summary": "Recent evidence permits telephone AI; your call rules still decide when it answers."
        if ready
        else "Telephone AI needs setup or fresh evidence. Keep the existing phone route or fallback.",
        "components": components,
        "blockers": blockers,
        "routing": dict(routing),
        "can_manage_agent": bool(can_manage_agent and entitled),
        "human_answered": False,
    }
