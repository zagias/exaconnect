"""The knowledge-gap report (ADR 0026), built on knowledge.record_gap (ADR 0019).

knowledge_gaps already holds each question the AI could not answer from
approved sources, counted by exact wording. The report groups similar
questions by the words they share, and "Draft an article" asks the model for a
draft that answers a group. The draft is an unapproved knowledge source, so
the AI never uses it until a person approves it; approval is refused while the
draft still has "[Check: ...]" placeholders. Approving it resolves the gaps it
was drafted from.
"""

from __future__ import annotations

from typing import Any

import psycopg

from .. import usage
from . import judging, knowledge, runtime
from .model import ModelError, ModelInput, terms

METER = "copilot"


class GapError(Exception):
    def __init__(self, message: str, code: int = 422):
        super().__init__(message)
        self.code = code


def _similar(a: set[str], b: set[str]) -> bool:
    if not a or not b:
        return False
    shared = len(a & b)
    return shared >= 2 or (shared == 1 and min(len(a), len(b)) <= 2)


def report(conn: psycopg.Connection, customer_id: Any) -> dict:
    """Open gaps grouped by shared words, biggest groups first."""
    gaps = knowledge.list_gaps(conn, customer_id, "open")
    drafts = {
        str(r["id"]): r["draft_source_id"]
        for r in conn.execute(
            "SELECT id, draft_source_id FROM knowledge_gaps WHERE customer_id = %s AND status = 'open'",
            (customer_id,),
        ).fetchall()
    }
    groups: list[dict] = []
    for g in gaps:
        t = terms(g["question"])
        home = next((grp for grp in groups if _similar(t, grp["_terms"])), None)
        if home is None:
            home = {"_terms": set(), "gaps": []}
            groups.append(home)
        home["_terms"] |= t
        home["gaps"].append({**g, "draft_source_id": drafts.get(str(g["id"]))})
    out = []
    for grp in groups:
        counts: dict[str, int] = {}
        for g in grp["gaps"]:
            for t in terms(g["question"]):
                counts[t] = counts.get(t, 0) + g["times"]
        topic = [t for t, _ in sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:3]]
        draft = next((g["draft_source_id"] for g in grp["gaps"] if g["draft_source_id"]), None)
        out.append(
            {
                "topic": ", ".join(topic),
                "times": sum(g["times"] for g in grp["gaps"]),
                "reasons": sorted({g["reason"] for g in grp["gaps"]}),
                "gap_ids": [str(g["id"]) for g in grp["gaps"]],
                "questions": [
                    {"id": str(g["id"]), "question": g["question"], "times": g["times"], "reason": g["reason"]}
                    for g in grp["gaps"]
                ],
                "draft_source_id": str(draft) if draft else None,
            }
        )
    out.sort(key=lambda x: -x["times"])
    return {"groups": out, "open": len(gaps)}


def draft_article(conn: psycopg.Connection, customer_id: Any, gap_ids: list[str], actor: str) -> dict:
    """Ask the model for a draft article answering these gaps. The draft is a
    knowledge source that is not approved."""
    rows = conn.execute(
        """SELECT id, question FROM knowledge_gaps WHERE customer_id = %s AND id = ANY(%s::uuid[])
           AND status = 'open'""",
        (customer_id, gap_ids),
    ).fetchall()
    if not rows:
        raise GapError("Those questions are not open gaps any more.", 404)
    if not usage.allowed(conn, customer_id, METER):
        raise GapError("The monthly limit for the copilot has been reached.", 429)
    questions = [r["question"] for r in rows]
    related = []
    for q in questions[:3]:
        related += [h["text"][:600] for h in knowledge.lookup(conn, customer_id, q, 2)["hits"]]
    out = runtime.get_model().complete(
        ModelInput(
            role="copilot",
            task="article",
            system=judging.ARTICLE_SYSTEM,
            context={"business": runtime.business_name(conn, customer_id), "questions": questions, "related": related},
            max_tokens=900,
        )
    )
    title = str(out.data.get("title") or "").strip()[:200] if isinstance(out.data, dict) else ""
    body = str(out.data.get("body") or "").strip() if isinstance(out.data, dict) else ""
    if not title or not body:
        raise ModelError("The AI service sent an answer in an unexpected shape.")
    src = knowledge.add_source(conn, customer_id, title=title, body=body, approved=False, created_by=actor)
    conn.execute("UPDATE knowledge_sources SET drafted_from_gaps = true WHERE id = %s", (src["id"],))
    conn.execute(
        "UPDATE knowledge_gaps SET draft_source_id = %s WHERE customer_id = %s AND id = ANY(%s::uuid[])",
        (src["id"], customer_id, [r["id"] for r in rows]),
    )
    usage.record(conn, customer_id, METER, 1, ref=f"article:{src['id']}", detail={"task": "article"})
    return {**src, "placeholders": judging.has_placeholders(body), "gap_ids": [str(r["id"]) for r in rows]}


def approve_draft(conn: psycopg.Connection, customer_id: Any, source_id: Any, actor: str) -> dict:
    src = knowledge.get_source(conn, customer_id, source_id)
    if src is None:
        raise GapError("Draft not found.", 404)
    if judging.has_placeholders(src["body"]):
        raise GapError("Fill in every [Check: ...] in the draft before approving it.", 409)
    row = knowledge.set_approved(conn, customer_id, source_id, True, actor)
    resolved = conn.execute(
        """UPDATE knowledge_gaps SET status = 'resolved', resolved_by = %s
           WHERE customer_id = %s AND draft_source_id = %s AND status = 'open' RETURNING id""",
        (actor, customer_id, source_id),
    ).fetchall()
    return {**row, "resolved_gaps": [str(r["id"]) for r in resolved]}
