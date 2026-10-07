"""Metered usage, budgets and hard limits (ADR 0016, ADR 0039).

Every billable unit (an AI reply, an outbound WhatsApp message, a call
minute) is recorded once, keyed by a reference so a retry never counts
twice. Reports read these rows directly, so they match the recorded events.

Meters in use: ai_reply, ai_tokens, copilot, message_out:<channel>,
voice_minute, ai_voice_minute, workflow_run.

Two kinds of control:
- Quantity limits per meter (usage_limits): an alert level and a hard
  monthly limit that stops the metered work.
- Money budgets (commai_budgets), priced on the business's rate cards
  (bill.py, voice/billing.py): for the whole bill, per channel, for AI, and
  per workflow. Each has an alert level (an event, once, when it is crossed)
  and a hard limit that stops the work: AI replies, outbound messages on
  every channel, and workflow runs and their messages.

`allowed()` answers both before work starts; `record()` rates the usage and
raises the alerts after it happened.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import events

events.register("usage.alert", "usage.limit_reached", "usage.budget_alert", "usage.budget_reached")

BUDGET_SCOPES = ("total", "channel", "ai", "workflow")


def _month_start() -> dt.datetime:
    now = dt.datetime.now(dt.UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def used(conn: psycopg.Connection, customer_id: Any, meter: str) -> float:
    row = conn.execute(
        "SELECT coalesce(sum(quantity), 0) AS q FROM usage_records WHERE customer_id = %s AND meter = %s AND at >= %s",
        (customer_id, meter, _month_start()),
    ).fetchone()
    return float(row["q"])


def _scopes(meter: str, workflow_id: Any = None, channel: str | None = None) -> list[tuple[str, str]]:
    """The budgets a unit of this meter counts against."""
    from . import bill

    out = [("total", "")]
    fam = bill.family(meter)
    if fam == "ai":
        out.append(("ai", ""))
    if meter.startswith("message_out:"):
        out.append(("channel", meter.split(":", 1)[1]))
    elif fam == "voice" or meter == "ai_voice_minute":
        out.append(("channel", "voice"))
    if channel and ("channel", channel) not in out:
        out.append(("channel", channel))
    if workflow_id:
        out.append(("workflow", str(workflow_id)))
    return out


def _budgets(conn, customer_id: Any, scopes: list[tuple[str, str]]) -> list[dict]:
    if not scopes:
        return []
    rows = conn.execute("SELECT * FROM commai_budgets WHERE customer_id = %s", (customer_id,)).fetchall()
    want = set(scopes)
    return [r for r in rows if (r["scope"], r["key"]) in want]


def blocked_by(
    conn: psycopg.Connection,
    customer_id: Any,
    meter: str,
    quantity: float = 1,
    *,
    workflow_id: Any = None,
    channel: str | None = None,
) -> str | None:
    """Why this work may not start now, or None. Checks the quantity limit for
    the meter and every money budget the work counts against."""
    lim = conn.execute(
        "SELECT monthly_hard FROM usage_limits WHERE customer_id = %s AND meter = %s", (customer_id, meter)
    ).fetchone()
    if (
        lim
        and lim["monthly_hard"] is not None
        and used(conn, customer_id, meter) + quantity > float(lim["monthly_hard"])
    ):
        return f"The monthly limit for {meter.replace('_', ' ')} has been reached."
    budgets = [
        b for b in _budgets(conn, customer_id, _scopes(meter, workflow_id, channel)) if b["monthly_hard"] is not None
    ]
    if not budgets:
        return None
    from . import bill

    cost = bill.price_now(conn, customer_id, meter) * Decimal(str(quantity))
    for b in budgets:
        hard = Decimal(b["monthly_hard"])
        spent = bill.spend(conn, customer_id, b["scope"], b["key"])
        if spent >= hard or spent + cost > hard:
            return f"The monthly budget for {describe(conn, b)} ({hard} {_currency(conn, customer_id)}) is used up."
    return None


def allowed(
    conn: psycopg.Connection,
    customer_id: Any,
    meter: str,
    quantity: float = 1,
    *,
    workflow_id: Any = None,
    channel: str | None = None,
) -> bool:
    """False when this would pass the business's hard monthly limit or a money budget."""
    return blocked_by(conn, customer_id, meter, quantity, workflow_id=workflow_id, channel=channel) is None


def check_send(conn: psycopg.Connection, conversation: dict) -> None:
    """Before an outbound message: refuse it (SendBlocked) when its channel's
    limit or a money budget is used up. Called from inbox.send."""
    from . import channels

    why = blocked_by(conn, conversation["customer_id"], f"message_out:{conversation['channel']}")
    if why:
        raise channels.SendBlocked(why)


def record(
    conn: psycopg.Connection,
    customer_id: Any,
    meter: str,
    quantity: float = 1,
    ref: str = "",
    detail: dict | None = None,
) -> bool:
    """Record usage once per (meter, ref). Returns False if it was already recorded."""
    row = conn.execute(
        """INSERT INTO usage_records (customer_id, meter, quantity, ref, detail) VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (customer_id, meter, ref) WHERE ref <> '' DO NOTHING RETURNING *""",
        (customer_id, meter, quantity, ref, Jsonb(detail or {})),
    ).fetchone()
    if row is None:
        return False
    lim = conn.execute(
        "SELECT monthly_alert, monthly_hard FROM usage_limits WHERE customer_id = %s AND meter = %s",
        (customer_id, meter),
    ).fetchone()
    if lim:
        total = used(conn, customer_id, meter)
        before = total - float(quantity)
        for level, kind in ((lim["monthly_alert"], "usage.alert"), (lim["monthly_hard"], "usage.limit_reached")):
            if level is not None and before < float(level) <= total:
                events.emit(conn, customer_id, kind, {"meter": meter, "used": total, "level": float(level)}, meter)
    _rate_and_alert(conn, row)
    return True


def _rate_and_alert(conn, rec: dict) -> None:
    from . import bill

    charge = bill.rate_record(conn, rec)
    if charge is not None:
        amount, wf = Decimal(charge["amount"]), charge["workflow_id"]
    else:
        # Voice is rated by voice billing, which passes the amount it charged.
        try:
            amount = Decimal(str((rec["detail"] or {}).get("amount") or "0"))
        except ArithmeticError:
            amount = Decimal(0)
        wf = None
    if amount <= 0:
        return
    for b in _budgets(conn, rec["customer_id"], _scopes(rec["meter"], wf)):
        after = bill.spend(conn, rec["customer_id"], b["scope"], b["key"])
        before = after - amount
        for level, kind in ((b["monthly_alert"], "usage.budget_alert"), (b["monthly_hard"], "usage.budget_reached")):
            if level is not None and before < Decimal(level) <= after:
                events.emit(
                    conn,
                    rec["customer_id"],
                    kind,
                    {
                        "scope": b["scope"],
                        "key": b["key"],
                        "budget": describe(conn, b),
                        "spent": bill.s(bill.q2(after)),
                        "level": bill.s(Decimal(level)),
                    },
                    f"budget:{b['scope']}:{b['key']}",
                )


# ---- money budgets ------------------------------------------------------------------


def _currency(conn, customer_id: Any) -> str:
    from . import bill

    return bill.card_at(conn, customer_id)["currency"]


def describe(conn, b: dict) -> str:
    if b["scope"] == "total":
        return "the whole bill"
    if b["scope"] == "ai":
        return "AI"
    if b["scope"] == "channel":
        from . import channels

        return channels.label(b["key"]) if b["key"] != "voice" else "voice"
    row = conn.execute("SELECT name FROM commai_workflows WHERE id::text = %s", (b["key"],)).fetchone()
    return f'the workflow "{row["name"]}"' if row else "a removed workflow"


def budgets(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    from . import bill

    out = []
    for b in conn.execute(
        "SELECT * FROM commai_budgets WHERE customer_id = %s ORDER BY scope, key", (customer_id,)
    ).fetchall():
        spent = bill.spend(conn, customer_id, b["scope"], b["key"])
        alert = Decimal(b["monthly_alert"]) if b["monthly_alert"] is not None else None
        hard = Decimal(b["monthly_hard"]) if b["monthly_hard"] is not None else None
        state = "ok"
        if hard is not None and spent >= hard:
            state = "stopped"
        elif alert is not None and spent >= alert:
            state = "alert"
        out.append(
            {
                "scope": b["scope"],
                "key": b["key"],
                "label": describe(conn, b),
                "monthly_alert": bill.s(alert) if alert is not None else None,
                "monthly_hard": bill.s(hard) if hard is not None else None,
                "spent_this_month": bill.s(bill.q2(spent)),
                "state": state,
                "updated_by": b["updated_by"],
                "updated_at": b["updated_at"],
            }
        )
    return out


def set_budget(
    conn: psycopg.Connection,
    customer_id: Any,
    scope: str,
    key: str,
    monthly_alert: Decimal | None,
    monthly_hard: Decimal | None,
    actor: str,
) -> dict:
    from . import channels

    if scope not in BUDGET_SCOPES:
        raise ValueError(f"A budget is for {', '.join(BUDGET_SCOPES)}.")
    key = (key or "").strip()
    if scope in ("total", "ai"):
        key = ""
    elif scope == "channel":
        if key != "voice" and not channels.any_channel(key):
            raise ValueError(f"There is no channel called {key}.")
    elif not conn.execute(
        "SELECT 1 FROM commai_workflows WHERE id::text = %s AND customer_id = %s", (key, customer_id)
    ).fetchone():
        raise ValueError("There is no such workflow.")
    if monthly_alert is None and monthly_hard is None:
        raise ValueError("Set an alert level, a hard limit or both.")
    for v in (monthly_alert, monthly_hard):
        if v is not None and v < 0:
            raise ValueError("Budgets can't be negative.")
    if monthly_alert is not None and monthly_hard is not None and monthly_alert > monthly_hard:
        raise ValueError("The alert level must not be above the hard limit.")
    conn.execute(
        """INSERT INTO commai_budgets (customer_id, scope, key, monthly_alert, monthly_hard, updated_by)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (customer_id, scope, key) DO UPDATE SET monthly_alert = EXCLUDED.monthly_alert,
             monthly_hard = EXCLUDED.monthly_hard, updated_by = EXCLUDED.updated_by, updated_at = now()""",
        (customer_id, scope, key, monthly_alert, monthly_hard, actor),
    )
    return next(b for b in budgets(conn, customer_id) if b["scope"] == scope and b["key"] == key)


def delete_budget(conn: psycopg.Connection, customer_id: Any, scope: str, key: str) -> bool:
    row = conn.execute(
        "DELETE FROM commai_budgets WHERE customer_id = %s AND scope = %s AND key = %s RETURNING scope",
        (customer_id, scope, key or ""),
    ).fetchone()
    return row is not None
