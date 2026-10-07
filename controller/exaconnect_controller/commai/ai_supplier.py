"""The supplier side of AI (ADR 0033): what the model and speech provider
(DeepInfra) charges ExaCarib, reconciled against what CommAI recorded and
billed, with the margin per business.

The supplier reports usage per day and model, for all of ExaCarib, not per
business. So reconciliation works in two steps:

1. For each day, model and kind (tokens, speech seconds), compare the
   supplier's quantity with the total CommAI recorded for every business.
   A difference is listed as an issue (the supplier counted usage we did not
   record, or the other way round).
2. Share the supplier's cost for that day and model between businesses by
   their share of the recorded quantity. The margin is what the business was
   billed for AI (bill.py and voice AI minutes) less its share of the cost.

The usage comes from a `UsageSource`. The simulated source builds the
supplier's view from CommAI's own records at example supplier prices, so the
whole path runs without an account. The DeepInfra source refuses to run
until EXA_DEEPINFRA_API_KEY is set (Dudley's account) and its usage endpoint
has been confirmed against DeepInfra's documentation.
"""

from __future__ import annotations

import datetime as dt
import os
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import psycopg

from . import bill

# Example supplier prices (USD): per token by model, per second of speech.
SIMULATED_TOKEN_PRICE = {"default": Decimal("0.0000006")}
SIMULATED_SPEECH_PER_SECOND = Decimal("0.0002")
SPEECH_MODEL = "speech"


class SupplierError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def _period(period: dt.date) -> tuple[dt.datetime, dt.datetime]:
    start = bill.month_start(period)
    return (
        dt.datetime.combine(start, dt.time(), dt.UTC),
        dt.datetime.combine(bill.next_month(start), dt.time(), dt.UTC),
    )


def _recorded(conn, start: dt.datetime, end: dt.datetime, customer_id: Any = None) -> list[dict]:
    """CommAI's own AI usage per day, model and kind (one business, or all)."""
    return conn.execute(
        """SELECT (at AT TIME ZONE 'UTC')::date AS day,
                  CASE WHEN meter = 'ai_tokens' THEN coalesce(nullif(detail->>'model', ''), 'unknown')
                       ELSE %s END AS model,
                  CASE WHEN meter = 'ai_tokens' THEN 'tokens' ELSE 'speech_seconds' END AS kind,
                  sum(CASE WHEN meter = 'ai_tokens' THEN quantity ELSE quantity * 60 END) AS quantity
           FROM usage_records WHERE meter IN ('ai_tokens', 'ai_voice_minute') AND at >= %s AND at < %s
             AND (%s::uuid IS NULL OR customer_id = %s::uuid)
           GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""",
        (SPEECH_MODEL, start, end, customer_id, customer_id),
    ).fetchall()


class UsageSource:
    name = ""
    simulated = False

    def fetch(self, conn: psycopg.Connection, start: dt.datetime, end: dt.datetime) -> list[dict]:
        """[{"day": date, "model": str, "kind": "tokens" | "speech_seconds", "quantity": Decimal, "cost": Decimal}]"""
        raise NotImplementedError


class SimulatedDeepInfra(UsageSource):
    """The supplier's view built from CommAI's records at example prices."""

    name = "deepinfra"
    simulated = True

    def fetch(self, conn, start, end):
        out = []
        for r in _recorded(conn, start, end):
            q = Decimal(r["quantity"])
            if r["kind"] == "tokens":
                unit = SIMULATED_TOKEN_PRICE.get(r["model"], SIMULATED_TOKEN_PRICE["default"])
            else:
                unit = SIMULATED_SPEECH_PER_SECOND
            out.append({**r, "quantity": q, "cost": (q * unit).quantize(Decimal("0.000001"), ROUND_HALF_UP)})
        return out


class DeepInfraUsage(UsageSource):
    """DeepInfra's account usage. Not live: needs Dudley's account key, and the
    usage endpoint and its fields must be confirmed before first use."""

    name = "deepinfra"

    def fetch(self, conn, start, end):
        key = os.environ.get("EXA_DEEPINFRA_API_KEY", "")
        url = os.environ.get("EXA_DEEPINFRA_USAGE_URL", "")
        if not key or not url:
            raise SupplierError(
                "DeepInfra usage is not connected: set EXA_DEEPINFRA_API_KEY and EXA_DEEPINFRA_USAGE_URL.", 409
            )
        from .automation import http

        r = http.request("GET", url, token=key, params={"from": start.date().isoformat(), "to": end.date().isoformat()})
        if r.status != 200 or not isinstance(r.body, dict):
            raise SupplierError(f"DeepInfra answered {r.status}.", 502)
        out = []
        for row in r.body.get("usage") or []:
            try:
                out.append(
                    {
                        "day": dt.date.fromisoformat(str(row["date"])[:10]),
                        "model": str(row.get("model") or "unknown")[:120],
                        "kind": "speech_seconds" if row.get("seconds") is not None else "tokens",
                        "quantity": Decimal(
                            str(row.get("seconds") if row.get("seconds") is not None else row["tokens"])
                        ),
                        "cost": Decimal(str(row["cost"])),
                    }
                )
            except (KeyError, ValueError, ArithmeticError):
                continue
        return out


def source(name: str = "") -> UsageSource:
    name = name or ("deepinfra" if os.environ.get("EXA_DEEPINFRA_API_KEY") else "simulated")
    if name == "simulated":
        return SimulatedDeepInfra()
    if name == "deepinfra":
        return DeepInfraUsage()
    raise SupplierError(f"Unknown supplier source {name}.", 422)


def import_costs(conn: psycopg.Connection, period: dt.date, src: UsageSource, actor: str) -> dict:
    """Import a month of supplier usage. Importing again replaces the numbers
    for each day, model and kind (suppliers revise recent days)."""
    start, end = _period(period)
    rows = src.fetch(conn, start, end)
    imp = conn.execute(
        """INSERT INTO ai_supplier_imports (supplier, period, simulated, rows, created_by)
           VALUES (%s, %s, %s, %s, %s) RETURNING *""",
        (src.name, start.date(), src.simulated, len(rows), actor),
    ).fetchone()
    for r in rows:
        conn.execute(
            """INSERT INTO ai_supplier_costs (import_id, supplier, day, model, kind, quantity, cost)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (supplier, day, model, kind) DO UPDATE SET import_id = EXCLUDED.import_id,
                 quantity = EXCLUDED.quantity, cost = EXCLUDED.cost""",
            (imp["id"], src.name, r["day"], r["model"], r["kind"], r["quantity"], r["cost"]),
        )
    return {"id": str(imp["id"]), "supplier": src.name, "simulated": src.simulated, "rows": len(rows)}


def reconcile(conn: psycopg.Connection, customer_id: Any, period: dt.date, supplier: str = "deepinfra") -> dict:
    start, end = _period(period)
    ours = {(r["day"], r["model"], r["kind"]): Decimal(r["quantity"]) for r in _recorded(conn, start, end, customer_id)}
    everyone = {(r["day"], r["model"], r["kind"]): Decimal(r["quantity"]) for r in _recorded(conn, start, end)}
    costs = {
        (r["day"], r["model"], r["kind"]): r
        for r in conn.execute(
            "SELECT * FROM ai_supplier_costs WHERE supplier = %s AND day >= %s AND day < %s",
            (supplier, start.date(), end.date()),
        ).fetchall()
    }
    issues = []
    for k in sorted(set(everyone) | set(costs)):
        day, model, kind = k
        rec, sup = everyone.get(k, Decimal(0)), costs.get(k)
        unit = "tokens" if kind == "tokens" else "seconds of speech"
        if sup is None:
            issues.append({"day": day.isoformat(), "model": model, "issue": f"No supplier charge yet for {rec} {unit}"})
        elif Decimal(sup["quantity"]) != rec:
            issues.append(
                {
                    "day": day.isoformat(),
                    "model": model,
                    "issue": f"Supplier counted {Decimal(sup['quantity']).normalize()} {unit}, "
                    f"CommAI recorded {rec.normalize()}",
                }
            )
    lines, cost = [], Decimal(0)
    for k, mine in sorted(ours.items()):
        sup = costs.get(k)
        total = everyone.get(k) or Decimal(0)
        share = (Decimal(sup["cost"]) * mine / total) if sup and total else Decimal(0)
        cost += share
        lines.append(
            {
                "day": k[0].isoformat(),
                "model": k[1],
                "kind": k[2],
                "recorded": bill.s(mine.normalize()),
                "share_of_supplier_cost": bill.s(share.quantize(Decimal("0.000001"), ROUND_HALF_UP)),
                "matched": sup is not None,
            }
        )
    bill.rate_pending(conn, customer_id, end)
    billed = Decimal(
        conn.execute(
            """SELECT coalesce(sum(amount), 0) AS a FROM commai_charges WHERE customer_id = %s AND family = 'ai'
               AND at >= %s AND at < %s""",
            (customer_id, start, end),
        ).fetchone()["a"]
    ) + Decimal(
        conn.execute(
            """SELECT coalesce(sum(amount), 0) AS a FROM voice_charges WHERE customer_id = %s AND kind = 'ai_minutes'
               AND at >= %s AND at < %s""",
            (customer_id, start, end),
        ).fetchone()["a"]
    )
    margin = billed - cost
    simulated = conn.execute(
        "SELECT bool_or(simulated) AS s FROM ai_supplier_imports WHERE supplier = %s AND period = %s",
        (supplier, start.date()),
    ).fetchone()["s"]
    return {
        "period": start.date().isoformat(),
        "supplier": supplier,
        "simulated": bool(simulated),
        "billed_ai": bill.s(bill.q2(billed)),
        "supplier_cost": bill.s(bill.q2(cost)),
        "margin": bill.s(bill.q2(margin)),
        "margin_pct": bill.s(bill.q2(margin * 100 / billed)) if billed else None,
        "lines": lines,
        "issues": issues[:200],
    }
