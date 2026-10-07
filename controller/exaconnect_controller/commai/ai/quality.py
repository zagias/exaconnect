"""AI quality review against the business's own criteria (ADR 0032).

The business writes criteria in plain words ("greets the customer by name",
"never promises refunds"). A review is a durable job ("quality.review") that
takes a sample of recent conversations, some handled by the AI and some by
people, and asks the model to judge each one against every criterion with a
quote as evidence. A quote the model offers that is not in the conversation is
dropped. Any conversation that fails a criterion is flagged for a person to
review.

The judge reads customer-facing messages only (inbox.messages), never notes.
Each judged conversation is metered as "ai_quality".
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import events, inbox, jobs, usage
from . import judging, runtime
from .model import ModelError, ModelInput

log = logging.getLogger("exaconnect.commai.ai.quality")

events.register("quality.review_finished", "quality.flagged")

METER = "ai_quality"
MAX_CRITERIA = 20
MAX_SAMPLE = 100


class QualityError(Exception):
    def __init__(self, message: str, code: int = 422):
        super().__init__(message)
        self.code = code


# ---- criteria ---------------------------------------------------------------------------


def criteria(conn: psycopg.Connection, customer_id: Any, enabled_only: bool = False) -> list[dict]:
    return conn.execute(
        """SELECT id, text, enabled, created_by, created_at FROM quality_criteria
           WHERE customer_id = %s AND (NOT %s OR enabled) ORDER BY created_at, id""",
        (customer_id, enabled_only),
    ).fetchall()


def add_criterion(conn: psycopg.Connection, customer_id: Any, text: str, actor: str) -> dict:
    text = " ".join((text or "").split())
    if not 3 <= len(text) <= 300:
        raise QualityError("Write the criterion in 3 to 300 characters.")
    n = conn.execute("SELECT count(*) AS n FROM quality_criteria WHERE customer_id = %s", (customer_id,)).fetchone()
    if n["n"] >= MAX_CRITERIA:
        raise QualityError(f"A business can have up to {MAX_CRITERIA} criteria.")
    return conn.execute(
        "INSERT INTO quality_criteria (customer_id, text, created_by) VALUES (%s, %s, %s) RETURNING *",
        (customer_id, text, actor),
    ).fetchone()


def update_criterion(conn, customer_id: Any, criterion_id: Any, *, text: str | None, enabled: bool | None) -> dict:
    if text is not None:
        text = " ".join(text.split())
        if not 3 <= len(text) <= 300:
            raise QualityError("Write the criterion in 3 to 300 characters.")
    row = conn.execute(
        """UPDATE quality_criteria SET text = COALESCE(%s, text), enabled = COALESCE(%s, enabled)
           WHERE id = %s AND customer_id = %s RETURNING *""",
        (text, enabled, criterion_id, customer_id),
    ).fetchone()
    if row is None:
        raise QualityError("Criterion not found.", 404)
    return row


def delete_criterion(conn, customer_id: Any, criterion_id: Any) -> None:
    cur = conn.execute("DELETE FROM quality_criteria WHERE id = %s AND customer_id = %s", (criterion_id, customer_id))
    if cur.rowcount == 0:
        raise QualityError("Criterion not found.", 404)


# ---- reviews ----------------------------------------------------------------------------


def start_review(conn: psycopg.Connection, customer_id: Any, *, sample_size: int, days: int, actor: str) -> dict:
    crit = criteria(conn, customer_id, enabled_only=True)
    if not crit:
        raise QualityError("Write at least one criterion first.")
    if not 1 <= sample_size <= MAX_SAMPLE:
        raise QualityError(f"Review between 1 and {MAX_SAMPLE} conversations at a time.")
    review = conn.execute(
        """INSERT INTO quality_reviews (customer_id, sample_size, days, criteria, requested_by)
           VALUES (%s, %s, %s, %s, %s) RETURNING *""",
        (
            customer_id,
            sample_size,
            days,
            Jsonb([{"id": str(c["id"]), "text": c["text"]} for c in crit]),
            actor,
        ),
    ).fetchone()
    jobs.enqueue(
        conn,
        "quality.review",
        {"review_id": str(review["id"])},
        customer_id=customer_id,
        dedupe_key=f"quality:{review['id']}",
        max_attempts=3,
    )
    return review


def sample(conn: psycopg.Connection, customer_id: Any, review_id: Any, size: int, days: int) -> list[dict]:
    """Up to `size` conversations from the last `days` days with a reply from the
    business, half handled by the AI and half by people where both exist. The
    order is fixed by the review id, so a retried job judges the same ones."""
    rows = conn.execute(
        """SELECT c.id, c.contact_id,
                  CASE WHEN EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id AND m.author_kind = 'ai')
                       THEN 'ai' ELSE 'human' END AS handled_by
           FROM conversations c
           WHERE c.customer_id = %s AND c.created_at >= now() - make_interval(days => %s)
             AND EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id AND m.direction = 'out'
                         AND m.author_kind IN ('ai', 'user'))
           ORDER BY md5(c.id::text || %s::text)""",
        (customer_id, days, str(review_id)),
    ).fetchall()
    ai = [r for r in rows if r["handled_by"] == "ai"]
    human = [r for r in rows if r["handled_by"] == "human"]
    half = (size + 1) // 2
    pick = ai[:half] + human[: size - min(half, len(ai))]
    if len(pick) < size:
        pick += [r for r in ai[half:]][: size - len(pick)]
    return pick[:size]


def transcript(conn: psycopg.Connection, customer_id: Any, conversation_id: Any) -> list[dict]:
    """Customer-facing messages only: notes are never judged or quoted."""
    out = []
    for m in inbox.messages(conn, customer_id, conversation_id):
        if not m["body"]:
            continue
        who = "customer" if m["direction"] == "in" else ("ai" if m["author_kind"] == "ai" else "business")
        out.append({"from": who, "text": m["body"][:2000]})
    return out[-40:]


def judge(conn: psycopg.Connection, customer_id: Any, ctx: dict, ref: str) -> list[dict]:
    """Ask the model to judge; returns verified results (one per criterion)."""
    if not usage.allowed(conn, customer_id, METER):
        raise runtime.UsageLimit("The monthly limit for quality review has been reached.")
    out = runtime.get_model().complete(
        ModelInput(
            role="quality_reviewer",
            task="judge",
            system=judging.JUDGE_SYSTEM,
            context=ctx,
            max_tokens=900,
            temperature=0,
        )
    )
    raw = out.data.get("results") if isinstance(out.data, dict) else None
    if not isinstance(raw, list):
        raise ModelError("The AI service sent an answer in an unexpected shape.")
    by_id = {str(r.get("id")): r for r in raw if isinstance(r, dict)}
    results = [
        by_id.get(str(c["id"])) or {"id": c["id"], "verdict": "unclear", "why": "Not judged."} for c in ctx["criteria"]
    ]
    usage.record(conn, customer_id, METER, 1, ref=ref, detail={"model": out.model})
    return judging.verify_quotes(results, ctx["conversation"])


@jobs.handler("quality.review")
def _review_job(conn: psycopg.Connection, job: dict):
    run_review(conn, job["customer_id"], job["payload"]["review_id"])
    return None


def run_review(conn: psycopg.Connection, customer_id: Any, review_id: Any) -> dict:
    review = conn.execute(
        "SELECT * FROM quality_reviews WHERE id = %s AND customer_id = %s FOR UPDATE", (review_id, customer_id)
    ).fetchone()
    if review is None or review["status"] != "queued":
        return {"status": "skipped"}
    crit = review["criteria"]
    texts = {c["id"]: c["text"] for c in crit}
    judged = flagged = 0
    totals = {c["id"]: {"pass": 0, "fail": 0, "unclear": 0} for c in crit}
    by_handler = {"ai": [], "human": []}
    error = ""
    for conv in sample(conn, customer_id, review_id, review["sample_size"], review["days"]):
        name = ""
        if conv["contact_id"]:
            row = conn.execute("SELECT name FROM contacts WHERE id = %s", (conv["contact_id"],)).fetchone()
            name = row["name"] if row else ""
        ctx = {
            "conversation": transcript(conn, customer_id, conv["id"]),
            "criteria": crit,
            "contact_name": name,
            "escalated": None,
        }
        try:
            results = judge(conn, customer_id, ctx, ref=f"quality:{review_id}:{conv['id']}")
        except (ModelError, runtime.UsageLimit) as e:
            error = str(e)
            break
        for r in results:
            r["criterion"] = texts.get(r["id"], "")
            totals.setdefault(r["id"], {"pass": 0, "fail": 0, "unclear": 0})[r["verdict"]] += 1
        decided = [r for r in results if r["verdict"] != "unclear"]
        score = round(sum(r["verdict"] == "pass" for r in decided) / len(decided), 3) if decided else 1.0
        failed = [r for r in results if r["verdict"] == "fail"]
        conn.execute(
            """INSERT INTO quality_results (review_id, customer_id, conversation_id, handled_by, score, results,
                                            flagged, flag_status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (review_id, conversation_id) DO NOTHING""",
            (
                review_id,
                customer_id,
                conv["id"],
                conv["handled_by"],
                score,
                Jsonb(results),
                bool(failed),
                "open" if failed else "none",
            ),
        )
        judged += 1
        by_handler[conv["handled_by"]].append(score)
        if failed:
            flagged += 1
            events.emit(
                conn,
                customer_id,
                "quality.flagged",
                {"review_id": str(review_id), "conversation_id": str(conv["id"]), "failed": len(failed)},
                conv["id"],
            )
    summary = {
        "judged": judged,
        "flagged": flagged,
        "average": {k: (round(sum(v) / len(v), 3) if v else None) for k, v in by_handler.items()},
        "counted": {k: len(v) for k, v in by_handler.items()},
        "criteria": [{"id": c["id"], "text": c["text"], **totals.get(c["id"], {})} for c in crit],
    }
    status = "failed" if error and not judged else "done"
    conn.execute(
        "UPDATE quality_reviews SET status = %s, summary = %s, error = %s, finished_at = now() WHERE id = %s",
        (status, Jsonb(summary), error[:500], review_id),
    )
    events.emit(conn, customer_id, "quality.review_finished", {"review_id": str(review_id), **summary}, review_id)
    return {"status": status, **summary}


def reviews(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    return conn.execute(
        """SELECT id, status, sample_size, days, summary, error, requested_by, created_at, finished_at
           FROM quality_reviews WHERE customer_id = %s ORDER BY created_at DESC LIMIT 50""",
        (customer_id,),
    ).fetchall()


def results(conn: psycopg.Connection, customer_id: Any, review_id: Any) -> list[dict]:
    return conn.execute(
        """SELECT r.*, c.subject, c.channel, ct.name AS contact_name FROM quality_results r
           JOIN conversations c ON c.id = r.conversation_id LEFT JOIN contacts ct ON ct.id = c.contact_id
           WHERE r.review_id = %s AND r.customer_id = %s ORDER BY r.flagged DESC, r.score, r.created_at""",
        (review_id, customer_id),
    ).fetchall()


def flags(conn: psycopg.Connection, customer_id: Any, status: str = "open") -> list[dict]:
    return conn.execute(
        """SELECT r.id, r.review_id, r.conversation_id, r.handled_by, r.score, r.results, r.flag_status,
                  r.reviewed_by, r.review_note, r.reviewed_at, r.created_at, c.subject, c.channel
           FROM quality_results r JOIN conversations c ON c.id = r.conversation_id
           WHERE r.customer_id = %s AND r.flagged AND (%s = 'all' OR r.flag_status = %s)
           ORDER BY r.created_at DESC LIMIT 200""",
        (customer_id, status, status),
    ).fetchall()


def review_flag(conn: psycopg.Connection, customer_id: Any, result_id: Any, actor: str, note: str) -> dict:
    row = conn.execute(
        """UPDATE quality_results SET flag_status = 'reviewed', reviewed_by = %s, review_note = %s,
                  reviewed_at = now()
           WHERE id = %s AND customer_id = %s AND flagged RETURNING *""",
        (actor, note.strip()[:1000], result_id, customer_id),
    ).fetchone()
    if row is None:
        raise QualityError("Flag not found.", 404)
    return row
