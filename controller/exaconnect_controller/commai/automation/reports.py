"""Outcome reports and cost controls (ADR 0020).

Reports count recorded rows, never estimates: commai_events for outcomes,
conversations for response and resolution times, usage_records for usage.
Each figure says where it comes from, so it can be checked against the
event log (acceptance test 10).

Definitions (also in ADR 0020):
- AI kept: a conversation with at least one ai.replied event in the period
  that never went to a person: no conversation.handed_over event and no
  conversation.handler_changed event to "human", up to the end of the period.
- AI verifiably resolved: an AI-kept conversation with a
  conversation.state_changed event to "resolved" in the period, made by a
  person, by the customer, or by the AI with a verified action (an
  action.succeeded, not a test, on that conversation), and not reopened
  afterwards within the period. A resolve by the system (inactivity,
  auto-close) is not counted: a customer who leaves a chat is not a
  resolution.

Prices are EXAMPLES until ExaCarib's price list is set, and are labelled so.
"""

from __future__ import annotations

import datetime as dt
import statistics
from typing import Any

import psycopg

# Example prices (USD per unit). Placeholders until Dudley's price list.
EXAMPLE_PRICES = {
    "ai_reply": 0.02,
    "ai_tokens": 0.000002,
    "copilot": 0.01,
    "message_out:whatsapp": 0.015,
    "message_out:sms": 0.03,
    "message_out:email": 0.001,
    "message_out:web": 0.0,
    "voice_minute": 0.02,
    "ai_voice_minute": 0.06,
    "workflow_run": 0.002,
}
PRICES_LABEL = "Example prices until ExaCarib's price list is set"


def period(start: dt.datetime | None, end: dt.datetime | None) -> tuple[dt.datetime, dt.datetime]:
    now = dt.datetime.now(dt.UTC)
    end = end or now
    start = start or end - dt.timedelta(days=30)
    if start.tzinfo is None:
        start = start.replace(tzinfo=dt.UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=dt.UTC)
    if start >= end:
        raise ValueError("The start of the period must be before its end.")
    if end - start > dt.timedelta(days=400):
        raise ValueError("Choose a period of at most 400 days.")
    return start, end


def _count(conn, cid, start, end, type_: str, where: str = "", args: tuple = ()) -> int:
    return conn.execute(
        f"SELECT count(*) AS n FROM commai_events WHERE customer_id = %s AND type = %s"
        f" AND at >= %s AND at < %s {where}",
        (cid, type_, start, end, *args),
    ).fetchone()["n"]


def _stats(seconds: list[float]) -> dict:
    if not seconds:
        return {"count": 0, "average_s": None, "median_s": None, "p90_s": None}
    s = sorted(seconds)
    return {
        "count": len(s),
        "average_s": round(sum(s) / len(s), 1),
        "median_s": round(statistics.median(s), 1),
        "p90_s": round(s[min(len(s) - 1, int(0.9 * len(s)))], 1),
    }


def ai_outcomes(conn, cid: Any, start: dt.datetime, end: dt.datetime) -> dict:
    convs = [
        r["c"]
        for r in conn.execute(
            """SELECT DISTINCT coalesce(data->>'conversation_id', subject) AS c FROM commai_events
           WHERE customer_id = %s AND type = 'ai.replied' AND at >= %s AND at < %s""",
            (cid, start, end),
        ).fetchall()
    ]
    if not convs:
        return {"ai_handled": 0, "ai_kept": 0, "ai_resolved": 0, "kept_ids": [], "resolved_ids": []}
    went_to_person = {
        r["c"]
        for r in conn.execute(
            """SELECT DISTINCT coalesce(data->>'conversation_id', subject) AS c FROM commai_events
           WHERE customer_id = %s AND at < %s AND coalesce(data->>'conversation_id', subject) = ANY(%s)
             AND (type = 'conversation.handed_over'
                  OR (type = 'conversation.handler_changed' AND data->>'to' = 'human'))""",
            (cid, end, convs),
        ).fetchall()
    }
    kept = [c for c in convs if c not in went_to_person]
    resolved = []
    for c in kept:
        res = conn.execute(
            """SELECT at, coalesce(data->>'by', '') AS by FROM commai_events WHERE customer_id = %s
               AND type = 'conversation.state_changed' AND data->>'to' = 'resolved'
               AND coalesce(data->>'conversation_id', subject) = %s AND at >= %s AND at < %s
               ORDER BY seq DESC LIMIT 1""",
            (cid, c, start, end),
        ).fetchone()
        if res is None or res["by"].startswith("system") or res["by"] == "":
            continue
        reopened = conn.execute(
            """SELECT 1 FROM commai_events WHERE customer_id = %s AND type = 'conversation.state_changed'
               AND data->>'to' = 'reopened' AND coalesce(data->>'conversation_id', subject) = %s AND at > %s
               AND at < %s LIMIT 1""",
            (cid, c, res["at"], end),
        ).fetchone()
        if reopened:
            continue
        by = res["by"]
        verified = by.startswith(("user:", "contact"))
        if not verified:  # resolved by the AI: only with a verified action on this conversation
            verified = bool(
                conn.execute(
                    """SELECT 1 FROM commai_events e JOIN action_runs r ON r.id::text = e.data->>'run_id'
                   WHERE e.customer_id = %s AND e.type = 'action.succeeded' AND coalesce(e.data->>'test', 'false')
                   <> 'true' AND r.conversation_id::text = %s AND e.at < %s LIMIT 1""",
                    (cid, c, end),
                ).fetchone()
            )
        if verified:
            resolved.append(c)
    return {
        "ai_handled": len(convs),
        "ai_kept": len(kept),
        "ai_resolved": len(resolved),
        "kept_ids": kept,
        "resolved_ids": resolved,
    }


def outcomes(conn: psycopg.Connection, customer_id: Any, start: dt.datetime, end: dt.datetime) -> dict:
    cid = customer_id
    now = dt.datetime.now(dt.UTC)
    created = conn.execute(
        """SELECT id, created_at, first_reply_at, first_reply_due FROM conversations
           WHERE customer_id = %s AND created_at >= %s AND created_at < %s""",
        (cid, start, end),
    ).fetchall()
    first = [(r["first_reply_at"] - r["created_at"]).total_seconds() for r in created if r["first_reply_at"]]
    missed = [
        r
        for r in created
        if r["first_reply_at"] is None and r["first_reply_due"] and r["first_reply_due"] < min(now, end)
    ]
    late = [
        r
        for r in created
        if r["first_reply_at"] and r["first_reply_due"] and r["first_reply_at"] > r["first_reply_due"]
    ]
    res_rows = conn.execute(
        """SELECT e.at, c.created_at FROM commai_events e
           JOIN conversations c ON c.id::text = e.data->>'conversation_id'
           WHERE e.customer_id = %s AND e.type = 'conversation.state_changed' AND e.data->>'to' = 'resolved'
           AND e.at >= %s AND e.at < %s""",
        (cid, start, end),
    ).fetchall()
    backlog = conn.execute(
        """SELECT count(*) FILTER (WHERE state <> 'resolved') AS open,
                  count(*) FILTER (WHERE state <> 'resolved' AND first_reply_at IS NULL) AS unanswered,
                  count(*) FILTER (WHERE state <> 'resolved' AND resolve_due < now()) AS overdue
           FROM conversations WHERE customer_id = %s""",
        (cid,),
    ).fetchone()
    failures = conn.execute(
        """SELECT data->>'app' AS app, coalesce(data->>'cause', '') AS cause, count(*) AS n FROM commai_events
           WHERE customer_id = %s AND type = 'action.failed' AND at >= %s AND at < %s GROUP BY 1, 2 ORDER BY 3 DESC""",
        (cid, start, end),
    ).fetchall()
    ai = ai_outcomes(conn, cid, start, end)
    return {
        "period": {"from": start, "to": end},
        "conversations": {
            "value": _count(conn, cid, start, end, "conversation.created"),
            "source": "conversation.created events",
        },
        "messages_received": {
            "value": _count(conn, cid, start, end, "message.received"),
            "source": "message.received events",
        },
        "first_response": {**_stats(first), "source": "conversations created in the period (first reply time)"},
        "missed_contacts": {
            "value": len(missed),
            "late_first_replies": len(late),
            "source": "conversations created in the period with no reply by the target time",
        },
        "backlog": {**backlog, "source": "conversations not resolved, now"},
        "resolution_time": {
            **_stats([(r["at"] - r["created_at"]).total_seconds() for r in res_rows]),
            "source": "conversation.state_changed to resolved events",
        },
        "resolved": {
            "value": _count(conn, cid, start, end, "conversation.state_changed", "AND data->>'to' = 'resolved'"),
            "source": "conversation.state_changed to resolved events",
        },
        "reopened": {
            "value": _count(conn, cid, start, end, "conversation.state_changed", "AND data->>'to' = 'reopened'"),
            "source": "conversation.state_changed to reopened events",
        },
        "confirmed_bookings": {
            "value": _count(conn, cid, start, end, "booking.confirmed"),
            "source": "booking.confirmed events",
        },
        "verified_actions": {
            "value": _count(
                conn, cid, start, end, "action.succeeded", "AND coalesce(data->>'test', 'false') <> 'true'"
            ),
            "source": "action.succeeded events (not tests)",
        },
        "integration_failures": {
            "value": sum(f["n"] for f in failures),
            "by_app": failures,
            "source": "action.failed events",
        },
        "ai": {
            "handled": ai["ai_handled"],
            "kept": ai["ai_kept"],
            "verifiably_resolved": ai["ai_resolved"],
            "source": "ai.replied, conversation.handed_over, handler_changed, state_changed and action.succeeded "
            "events",
            "definitions": {
                "kept": "The AI replied and the conversation never went to a person.",
                "verifiably_resolved": "Kept by the AI and resolved by a person, the customer, or the AI with a "
                "verified action, and not reopened. Inactivity is not a resolution.",
            },
        },
        "workflows": {
            "runs": _count(conn, cid, start, end, "workflow.run_started"),
            "failed": _count(conn, cid, start, end, "workflow.run_failed"),
            "source": "workflow.run_started and workflow.run_failed events",
        },
        "satisfaction": _csat_summary(conn, cid, start, end),
    }


def _csat_summary(conn, cid: Any, start: dt.datetime, end: dt.datetime) -> dict:
    from .. import csat  # satisfaction surveys (ADR 0026)

    return csat.summary(conn, cid, start, end)


def _price(meter: str) -> float | None:
    return EXAMPLE_PRICES.get(meter)


def usage_report(conn: psycopg.Connection, customer_id: Any, start: dt.datetime, end: dt.datetime) -> dict:
    rows = conn.execute(
        """SELECT meter, sum(quantity) AS quantity, count(*) AS records FROM usage_records
           WHERE customer_id = %s AND at >= %s AND at < %s GROUP BY meter ORDER BY meter""",
        (customer_id, start, end),
    ).fetchall()
    meters = []
    by_channel: dict[str, dict] = {}
    total = 0.0
    for r in rows:
        q = float(r["quantity"])
        p = _price(r["meter"])
        cost = round(q * p, 4) if p is not None else None
        total += cost or 0
        meters.append(
            {"meter": r["meter"], "quantity": q, "records": r["records"], "example_unit_price": p, "example_cost": cost}
        )
        channel = (
            r["meter"].split(":", 1)[1]
            if r["meter"].startswith("message_out:")
            else ("voice" if "voice" in r["meter"] else ("ai" if r["meter"].startswith(("ai", "copilot")) else "other"))
        )
        c = by_channel.setdefault(channel, {"channel": channel, "quantity": 0.0, "example_cost": 0.0})
        c["quantity"] += q
        c["example_cost"] = round(c["example_cost"] + (cost or 0), 4)
    wf = conn.execute(
        """SELECT u.detail->>'workflow_id' AS workflow_id, w.name, u.meter, sum(u.quantity) AS quantity
           FROM usage_records u LEFT JOIN commai_workflows w ON w.id::text = u.detail->>'workflow_id'
           WHERE u.customer_id = %s AND u.at >= %s AND u.at < %s AND u.detail ? 'workflow_id'
           GROUP BY 1, 2, 3 ORDER BY 2""",
        (customer_id, start, end),
    ).fetchall()
    workflows = [
        {
            "workflow_id": w["workflow_id"],
            "name": w["name"] or "(deleted)",
            "meter": w["meter"],
            "quantity": float(w["quantity"]),
            "example_cost": round(float(w["quantity"]) * (_price(w["meter"]) or 0), 4),
        }
        for w in wf
    ]
    return {
        "period": {"from": start, "to": end},
        "meters": meters,
        "by_channel": sorted(by_channel.values(), key=lambda x: x["channel"]),
        "by_workflow": workflows,
        "example_total": round(total, 4),
        "prices": PRICES_LABEL,
        "currency": "USD",
        "limits": limits(conn, customer_id),
    }


def limits(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    from .. import usage

    out = []
    for r in conn.execute("SELECT * FROM usage_limits WHERE customer_id = %s ORDER BY meter", (customer_id,)):
        used = usage.used(conn, customer_id, r["meter"])
        alert = float(r["monthly_alert"]) if r["monthly_alert"] is not None else None
        hard = float(r["monthly_hard"]) if r["monthly_hard"] is not None else None
        state = "ok"
        if hard is not None and used >= hard:
            state = "stopped"
        elif alert is not None and used >= alert:
            state = "alert"
        out.append(
            {"meter": r["meter"], "monthly_alert": alert, "monthly_hard": hard, "used_this_month": used, "state": state}
        )
    return out


def set_limit(conn, customer_id: Any, meter: str, monthly_alert: float | None, monthly_hard: float | None) -> dict:
    if monthly_alert is not None and monthly_hard is not None and monthly_alert > monthly_hard:
        raise ValueError("The alert level must not be above the hard limit.")
    for v in (monthly_alert, monthly_hard):
        if v is not None and v < 0:
            raise ValueError("Limits can't be negative.")
    return conn.execute(
        """INSERT INTO usage_limits (customer_id, meter, monthly_alert, monthly_hard) VALUES (%s, %s, %s, %s)
           ON CONFLICT (customer_id, meter) DO UPDATE SET monthly_alert = EXCLUDED.monthly_alert,
             monthly_hard = EXCLUDED.monthly_hard RETURNING *""",
        (customer_id, meter, monthly_alert, monthly_hard),
    ).fetchone()
