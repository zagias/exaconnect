"""Insights (storm warnings, disaster watch, bill-shock forecasts, carrier anomalies) and
"Ask your network"."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import audit, db
from ..ai import ask as ask_mod
from ..ai import hazards, runner, storms
from .deps import AdminDep, UserDep

router = APIRouter(tags=["ai"])


@router.get("/ai/status")
def status(request: Request, user: UserDep) -> dict:
    s = request.app.state.settings
    return {
        "ask_enabled": bool(s.llm_api_key) and user.role != "carrier",
        "model": s.llm_model if s.llm_api_key else None,
        "storm_watch": bool(s.nhc_url),
        "hazard_watch": runner.hazard_watch_on(s),
    }


@router.get("/insights")
def list_insights(
    user: UserDep,
    customer_id: str | None = None,
    kind: Literal["storm_warning", "hazard", "bill_shock", "anomaly"] | None = None,
    include_resolved: bool = False,
    limit: int = 100,
) -> list[dict]:
    """Open insights, newest first. Carrier users see anomalies on their own links only."""
    params = {
        "c": customer_id,
        "k": kind,
        "r": include_resolved,
        "carrier": None,
        "l": max(1, min(limit, 500)),
    }
    if user.role == "customer":
        params["c"] = user.customer_id
    elif user.role == "carrier":
        if not user.carrier_id:
            raise HTTPException(403, "Not available for this account.")
        params.update(c=None, k="anomaly", carrier=user.carrier_id)
    with db.tx() as conn:
        return conn.execute(
            """SELECT i.id, i.customer_id, i.kind, i.severity, i.title, i.detail, i.data, i.example,
                      i.first_seen, i.last_seen, i.resolved_at, i.acknowledged_by, i.acknowledged_at,
                      s.name AS site, l.path, ca.name AS carrier
               FROM insights i LEFT JOIN sites s ON s.id = i.site_id LEFT JOIN links l ON l.id = i.link_id
               LEFT JOIN carriers ca ON ca.id = i.carrier_id
               WHERE (%(c)s::uuid IS NULL OR i.customer_id = %(c)s)
                 AND (%(k)s::text IS NULL OR i.kind = %(k)s)
                 AND (%(carrier)s::uuid IS NULL OR i.carrier_id = %(carrier)s)
                 AND (%(r)s OR i.resolved_at IS NULL)
               ORDER BY i.resolved_at IS NOT NULL,
                        CASE i.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, i.last_seen DESC
               LIMIT %(l)s""",
            params,
        ).fetchall()


@router.post("/insights/{insight_id}/acknowledge")
def acknowledge(insight_id: int, user: UserDep) -> dict:
    if user.role == "carrier":
        raise HTTPException(403, "Not available for this account.")
    with db.tx() as conn:
        row = conn.execute(
            """UPDATE insights SET acknowledged_by = %s, acknowledged_at = now()
               WHERE id = %s AND (%s::uuid IS NULL OR customer_id = %s) RETURNING id, customer_id, title""",
            (
                user.actor,
                insight_id,
                user.customer_id if user.role == "customer" else None,
                user.customer_id if user.role == "customer" else None,
            ),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Insight not found.")
        audit.record(conn, user.actor, "insight.acknowledge", row["title"], row["customer_id"])
    return {"id": row["id"]}


@router.post("/ai/storm-watch/example")
def storm_example(user: AdminDep, request: Request, on: bool = True) -> dict:
    """Raise (or clear) warnings from a made-up hurricane near Jamaica and a
    made-up earthquake off Trinidad, labelled as example data, to show the
    hurricane and disaster watches without a real event. The earthquake is
    reported by three feeds and still raises one insight."""
    with db.tx() as conn:
        n = storms.run_once(conn, storms.example_feed() if on else {"activeStorms": []}, example=True)
        h = hazards.run_once(
            conn, hazards.example_reports() if on else [], example=True, nhc_on=bool(request.app.state.settings.nhc_url)
        )
        audit.record(conn, user.actor, "storm_watch.example", "on" if on else "off")
    return {"open_warnings": n, "open_hazards": h}


class AskIn(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    customer_id: str | None = None


@router.post("/ai/ask")
async def ask(body: AskIn, user: UserDep, request: Request) -> dict:
    s = request.app.state.settings
    if user.role == "carrier":
        raise HTTPException(403, "Not available for this account.")
    if not s.llm_api_key:
        raise HTTPException(503, "Ask your network is switched off: no AI service key is configured.")
    customer_id = user.customer_id if user.role == "customer" else body.customer_id
    if not customer_id:
        raise HTTPException(400, "Choose a customer first.")

    def prepare():
        with db.tx() as conn:
            if conn.execute("SELECT 1 FROM customers WHERE id = %s", (customer_id,)).fetchone() is None:
                raise HTTPException(404, "Customer not found.")
            used = conn.execute(
                "SELECT count(*) AS n FROM audit_log WHERE actor = %s AND action = 'ai.ask'"
                " AND at > now() - interval '1 hour'",
                (user.actor,),
            ).fetchone()["n"]
            if used >= s.llm_questions_per_hour:
                raise HTTPException(429, "That's the limit of questions for this hour. Try again later.")
            audit.record(conn, user.actor, "ai.ask", body.question[:200], customer_id)
            return ask_mod.build_context(conn, customer_id)

    context = await asyncio.to_thread(prepare)
    try:
        answer = await asyncio.to_thread(
            ask_mod.ask,
            body.question,
            context,
            api_key=s.llm_api_key,
            base_url=s.llm_base_url,
            model=s.llm_model,
        )
    except ask_mod.AskError as e:
        raise HTTPException(502, str(e)) from None
    return {
        "answer": answer,
        "model": s.llm_model,
        "based_on": {
            "decisions": len(context.get("routing_decisions_last_7_days_newest_first", [])),
            "events": len(context.get("events_last_24h_newest_first", [])),
            "insights": len(context.get("open_insights", [])),
            "sites": len(context.get("sites", [])),
        },
    }
