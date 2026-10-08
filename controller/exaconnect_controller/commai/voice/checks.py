"""Voice diagnostics for the platform assistant (ADR 0039).

Read-only checks over the phone system: orders that failed, PBX files that
are out of date, calls blocked by the fraud limits, number transfers
(porting) that were rejected or are slow, and calls that failed. Each
finding says how sure it is; fixes come from the assistant's fixed list
and run only when an admin approves.
"""

from __future__ import annotations

from typing import Any

import psycopg

from .. import diagnostics
from ..automation.assistant import register_fix
from ..automation.redact import redact

PORT_SLOW_DAYS = 10


def _f(area: str, status: str, confidence: str, summary: str, evidence=(), fix=None, ids=()) -> dict:
    out = {
        "area": f"voice:{area}",
        "status": status,
        "confidence": confidence,
        "summary": summary,
        "evidence": [redact(str(e)) for e in evidence],
        "fix": fix,
    }
    if ids:
        out["correlation_ids"] = list(ids)
    return out


@register_fix("retry_voice_order", "Retry a failed phone order (each step is safe to repeat)")
def _fix_retry_order(conn, customer_id, params, actor):
    from . import provisioning

    o = provisioning.retry(conn, customer_id, params["order_id"], actor)
    return {"ok": True, "detail": f"Order queued again (attempt {o.get('retries', 0) + 1})."}


@register_fix("render_pbx", "Rebuild the phone system's PBX files from the current set-up")
def _fix_render(conn, customer_id, params, actor):
    from . import freeswitch

    out = freeswitch.render_business(conn, customer_id)
    return {"ok": True, "detail": f"PBX files rebuilt ({len(out['files'])} files)."}


def _orders(conn, cid) -> list[dict]:
    out = []
    for o in conn.execute(
        """SELECT id, failed_step, error, retries, updated_at FROM voice_orders
           WHERE customer_id = %s AND status = 'failed' ORDER BY updated_at DESC LIMIT 5""",
        (cid,),
    ).fetchall():
        out.append(
            _f(
                "orders",
                "problem",
                "confirmed",
                f"A phone order failed at the {o['failed_step'] or 'unknown'} step, so those numbers or phones "
                "are not live.",
                [f"Error: {o['error'] or 'none recorded'}", f"Retried {o['retries']} time(s)"],
                {"id": "retry_voice_order", "label": "Retry the failed order", "params": {"order_id": str(o["id"])}},
                [f"voice_order:{o['id']}"],
            )
        )
    return out


def _pbx(conn, cid) -> list[dict]:
    from . import freeswitch

    has_phones = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM voice_users WHERE customer_id = %s) AS u", (cid,)
    ).fetchone()["u"]
    if not has_phones:
        return []
    stored = conn.execute("SELECT digest, rendered_at FROM voice_pbx_renders WHERE customer_id = %s", (cid,)).fetchone()
    try:
        current = freeswitch.digest(freeswitch.render(freeswitch.gather(conn, cid)))
    except Exception as e:  # noqa: BLE001 - a set-up the PBX can't express is itself the finding
        return [
            _f(
                "pbx",
                "problem",
                "confirmed",
                "The phone set-up can't be turned into PBX files, so recent changes are not live.",
                [f"{type(e).__name__}: {e}"],
            )
        ]
    fix = {"id": "render_pbx", "label": "Rebuild the PBX files", "params": {}}
    if stored is None:
        return [_f("pbx", "problem", "likely", "The PBX files have never been built for this business.", [], fix)]
    if stored["digest"] != current:
        return [
            _f(
                "pbx",
                "problem",
                "likely",
                "The PBX files are older than the phone set-up, so the latest changes may not be live.",
                [f"Last built {stored['rendered_at']:%d %b %H:%M} UTC"],
                fix,
            )
        ]
    return [_f("pbx", "ok", "confirmed", "The PBX files match the phone set-up.")]


def _fraud(conn, cid) -> list[dict]:
    rows = conn.execute(
        """SELECT block_reason, count(*) AS n, max(ended_at) AS last,
                  (array_agg(call_id ORDER BY ended_at DESC))[1:3] AS ids
           FROM voice_cdrs WHERE customer_id = %s AND status = 'blocked' AND ended_at > now() - interval '24 hours'
           GROUP BY block_reason ORDER BY n DESC""",
        (cid,),
    ).fetchall()
    if not rows:
        return []
    total = sum(r["n"] for r in rows)
    return [
        _f(
            "fraud",
            "problem",
            "confirmed",
            f"{total} call(s) were blocked by the call safety limits in the last 24 hours.",
            [f"{r['n']} blocked: {r['block_reason'] or 'no reason recorded'}" for r in rows[:4]]
            + ["Change the limits on the Phone system screen (Call safety) if these calls were wanted."],
            None,
            [f"call:{i}" for r in rows for i in (r["ids"] or [])][:5],
        )
    ]


def _porting(conn, cid) -> list[dict]:
    out = []
    for p in conn.execute(
        """SELECT id, e164, status, note, created_at, updated_at FROM voice_port_orders
           WHERE customer_id = %s AND (status = 'rejected'
              OR (status IN ('submitted', 'accepted') AND created_at < now() - make_interval(days => %s)))
           ORDER BY updated_at DESC LIMIT 5""",
        (cid, PORT_SLOW_DAYS),
    ).fetchall():
        if p["status"] == "rejected":
            out.append(
                _f(
                    "porting",
                    "problem",
                    "confirmed",
                    f"The transfer of {p['e164']} was rejected by the losing provider.",
                    [f"Reason given: {p['note'] or 'none'}", "Check the account number and the name on the account."],
                    None,
                    [f"port:{p['id']}"],
                )
            )
        else:
            out.append(
                _f(
                    "porting",
                    "problem",
                    "likely",
                    f"The transfer of {p['e164']} has been {p['status']} for more than {PORT_SLOW_DAYS} days.",
                    [f"Submitted {p['created_at']:%d %b}"],
                    None,
                    [f"port:{p['id']}"],
                )
            )
    return out


def _calls(conn, cid) -> list[dict]:
    r = conn.execute(
        """SELECT count(*) AS n, count(*) FILTER (WHERE status = 'failed') AS failed
           FROM voice_cdrs WHERE customer_id = %s AND ended_at > now() - interval '24 hours'""",
        (cid,),
    ).fetchone()
    if r["n"] >= 5 and r["failed"] / r["n"] >= 0.2:
        return [
            _f(
                "calls",
                "problem",
                "likely",
                f"{r['failed']} of {r['n']} calls failed in the last 24 hours.",
                ["Check the provider status and the phones' registration on the Phone system screen."],
            )
        ]
    return []


@diagnostics.register("voice")
def check(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    return [
        *_orders(conn, customer_id),
        *_pbx(conn, customer_id),
        *_fraud(conn, customer_id),
        *_porting(conn, customer_id),
        *_calls(conn, customer_id),
    ]
