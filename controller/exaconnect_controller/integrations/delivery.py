"""Delivering one event to one integration, with retries and a log.

A delivery is a row in connect_deliveries and a job on the durable queue.
A failure that could pass (a time-out, a 5xx, a 429) is recorded and tried
again with exponential backoff (10 s, 20 s, 40 s... six attempts); a
failure that can't (a 4xx, bad credentials) stops at once. Every attempt
updates the delivery log the portal shows.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ..commai import jobs
from ..commai.automation.vault import VaultError
from . import cloudevents, providers, transport
from .providers import Context, ProviderError

log = logging.getLogger("exaconnect.integrations")

RETRY_DELAY_S = 10

CARRIER_KEYS = (
    "link_id",
    "links",
    "path",
    "path_label",
    "carrier",
    "notice",
    "title",
    "status",
    "starts_at",
    "ends_at",
    "metric",
    "now",
    "limit",
)


def carrier_view(ce: dict) -> dict:
    """What a carrier sees: its own link and the measurement, not the customer's business."""
    data = ce.get("data") or {}
    kept = {k: data[k] for k in CARRIER_KEYS if k in data}
    title = providers.title(ce)
    kept["summary"] = f"{title} on your link {data.get('link_id') or ''}".strip() + "."
    out = {k: v for k, v in ce.items() if k not in ("organisationid", "subject")}
    out["source"] = cloudevents_source_for_carrier()
    out["data"] = kept
    return out


def cloudevents_source_for_carrier() -> str:
    from .catalogue import SOURCE

    return SOURCE + "/carriers"


def ce_for(ev: dict, integration: dict) -> dict:
    ce = cloudevents.event(ev)
    if integration.get("carrier_id") is not None:
        ce = carrier_view(ce)
    return ce


def mode_of(integration: dict, secrets: dict | None = None) -> str:
    p = providers.get(integration["provider"])
    if p is None or not transport.live_enabled():
        return "simulated"
    if secrets is None:
        try:
            secrets = transport.decrypt(integration["secret_ciphertext"])
        except VaultError:
            return "simulated"
    return "live" if p.credentials_present(integration["config"] or {}, secrets) else "simulated"


def attempt(conn: psycopg.Connection, delivery_id: int, final: bool = False) -> dict:
    """One attempt. Returns {"status", "error", "retry"}; the row is updated."""
    d = conn.execute(
        """SELECT d.*, e.type, e.source, e.subject, e.time, e.customer_id AS ev_customer, e.severity, e.dedup_key,
                  e.action, e.data, e.id AS ev_id, e.carrier_ids, e.site_id
           FROM connect_deliveries d JOIN connect_events e ON e.id = d.event_id
           WHERE d.id = %s FOR UPDATE OF d""",
        (delivery_id,),
    ).fetchone()
    if d is None or d["status"] in ("delivered", "simulated", "skipped"):
        return {"status": d["status"] if d else "missing", "error": "", "retry": False}
    integ = conn.execute("SELECT * FROM connect_integrations WHERE id = %s", (d["integration_id"],)).fetchone()
    if integ is None or (not integ["enabled"] and not d["test"]):
        _finish(conn, d, "skipped", None, "The integration is switched off.", {})
        return {"status": "skipped", "error": "", "retry": False}
    p = providers.get(integ["provider"])
    ev = {
        "id": d["ev_id"],
        "type": d["type"],
        "source": d["source"],
        "subject": d["subject"],
        "time": d["time"],
        "customer_id": d["ev_customer"],
        "severity": d["severity"],
        "dedup_key": d["dedup_key"],
        "action": d["action"],
        "data": d["data"],
    }
    if p is None or not p.receives_events:
        _finish(conn, d, "failed", None, f"{integ['provider']} does not take events.", {})
        return {"status": "failed", "error": "unknown provider", "retry": False}
    try:
        secrets = transport.decrypt(integ["secret_ciphertext"])
    except VaultError as e:
        _finish(conn, d, "failed", None, str(e), {})
        return {"status": "failed", "error": str(e), "retry": False}
    live = mode_of(integ, secrets) == "live"
    http = transport.Transport(conn, integ["id"], integ["provider"], live, secrets, p.simulate)
    ctx = Context(conn, integ, integ["config"] or {}, secrets, http, test=d["test"])
    ce = ce_for(ev, integ)
    try:
        out = p.deliver(ctx, ce)
    except (ProviderError, transport.Unreachable) as e:
        retry = getattr(e, "retry", True) and not final
        code = getattr(e, "code", None)
        msg = http.redact(str(e))[:500]
        status = "pending" if retry else "failed"
        _finish(conn, d, status, code, msg, {"mode": "live" if live else "simulated", "requests": http.sent[-3:]})
        return {"status": status, "error": msg, "retry": retry}
    except (KeyError, ValueError) as e:
        msg = f"The integration's settings are incomplete ({type(e).__name__}: {e})."[:500]
        _finish(conn, d, "failed", None, http.redact(msg), {"requests": http.sent[-3:]})
        return {"status": "failed", "error": msg, "retry": False}
    status = "skipped" if out.skipped else ("delivered" if live else "simulated")
    detail = {
        "mode": "live" if live else "simulated",
        "requests": http.sent[-3:],
        "outcome": out.detail,
    }
    if out.skipped:
        detail["skipped"] = out.skipped
    _finish(conn, d, status, out.code, "", detail)
    return {"status": status, "error": "", "retry": False}


def _finish(conn, d: dict, status: str, code: int | None, error: str, detail: dict) -> None:
    conn.execute(
        """UPDATE connect_deliveries SET attempts = attempts + 1, status = %s, response_code = %s, last_error = %s,
                  detail = %s, delivered_at = CASE WHEN %s IN ('delivered', 'simulated') THEN now() END
           WHERE id = %s""",
        (status, code, error, Jsonb(detail), status, d["id"]),
    )
    conn.execute(
        "UPDATE connect_integrations SET last_status = %s, last_delivery_at = now() WHERE id = %s",
        (status if not error else f"{status}: {error[:180]}", d["integration_id"]),
    )


@jobs.handler("integrations.deliver")
def _deliver(conn: psycopg.Connection, job: dict) -> Any:
    final = job["attempts"] >= job["max_attempts"]
    r = attempt(conn, job["payload"]["delivery_id"], final=final)
    if r["retry"]:
        # Commit the attempt record, then come back: 10 s, 20 s, 40 s, ... (jobs.run_job doubles it).
        return jobs.Later(r["error"] or "retry", delay_s=RETRY_DELAY_S)
    return None


@jobs.on_dead("integrations.deliver")
def _dead(job: dict, error: str) -> None:
    from .. import db

    with db.tx() as conn:
        conn.execute(
            "UPDATE connect_deliveries SET status = 'failed', last_error = %s"
            " WHERE id = %s AND status NOT IN ('delivered', 'simulated', 'skipped')",
            (error[:500], job["payload"]["delivery_id"]),
        )
