"""The employee copilot (ADR 0019): helps staff with a conversation. It never
sends anything: a person reviews and sends.

It reads what the staff member may read: customer-facing messages, the
contact's record and memory, actions and handovers, approved knowledge, and
private notes when the person may read notes.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import inbox
from . import knowledge, language, memory, runtime
from .agent import history

ROLE = "copilot"
TASKS = ("summary", "draft", "translate", "missing", "next_steps")


def staff_context(
    conn: psycopg.Connection, customer_id: Any, conversation_id: Any, *, include_notes: bool, prof: dict
) -> dict:
    rp = runtime.PROFILES[ROLE]
    conv = inbox.get(conn, customer_id, conversation_id)
    msgs = history(conn, customer_id, conversation_id, rp.max_history)
    last_customer = next((m["text"] for m in reversed(msgs) if m["from"] == "customer"), "")
    contact: dict = {}
    facts: list[str] = []
    if conv["contact_id"]:
        row = conn.execute("SELECT name, email, phone FROM contacts WHERE id = %s", (conv["contact_id"],)).fetchone()
        contact = dict(row) if row else {}
        facts = [f["fact"] for f in memory.facts(conn, customer_id, conv["contact_id"])]
    ctx = {
        "business": runtime.business_name(conn, customer_id),
        "business_language": prof["business_language"],
        "customer_language": conv["language"] or language.detect(last_customer, prof["business_language"]),
        "channel": conv["channel"],
        "state": conv["state"],
        "conversation": msgs,
        "contact": contact,
        "verified": memory.identity_verified(conn, conv),
        "memory": facts,
        "knowledge": knowledge.lookup(conn, customer_id, last_customer, rp.max_knowledge),
        "handovers": conn.execute(
            "SELECT reason, at::text AS at FROM handovers WHERE conversation_id = %s ORDER BY id", (conv["id"],)
        ).fetchall(),
        "actions": conn.execute(
            """SELECT app, action, status, error FROM action_runs WHERE conversation_id = %s
               ORDER BY created_at""",
            (conv["id"],),
        ).fetchall(),
    }
    if include_notes:
        ctx["notes"] = [
            {"text": n["body"], "author": n["author"]}
            for n in conn.execute(
                "SELECT body, author FROM commai_notes WHERE conversation_id = %s ORDER BY created_at", (conv["id"],)
            ).fetchall()
        ]
    return ctx


def run(
    conn: psycopg.Connection,
    customer_id: Any,
    conversation_id: Any,
    task: str,
    *,
    actor: str,
    include_notes: bool,
    text: str = "",
    message_id: Any = None,
    to: str = "",
    instruction: str = "",
) -> dict:
    """Run one copilot task. Raises runtime.UsageLimit or model.ModelError."""
    if task not in TASKS:
        raise ValueError(f"Unknown copilot task {task}.")
    prof = runtime.profile(conn, customer_id)
    ctx = staff_context(conn, customer_id, conversation_id, include_notes=include_notes, prof=prof)
    if task == "translate":
        if message_id:
            m = conn.execute(
                "SELECT body FROM messages WHERE id = %s AND conversation_id = %s AND customer_id = %s",
                (message_id, conversation_id, customer_id),
            ).fetchone()
            if m is None:
                raise ValueError("Message not found in this conversation.")
            text = m["body"]
        if not text.strip():
            raise ValueError("Give the text or the message to translate.")
        ctx = {
            "text": text[:5000],
            "from_language": language.detect(text, ""),
            "to_language": to or prof["business_language"],
        }
    if instruction:
        ctx["staff_instruction"] = instruction[:500]
    out = runtime.complete(conn, customer_id, ROLE, task, ctx, prof=prof)
    hits = {h["chunk_id"]: h for h in (ctx.get("knowledge") or {}).get("hits", [])}
    sources = [
        {k: hits[s][k] for k in ("chunk_id", "source_id", "title", "source_url")} for s in out.sources if s in hits
    ]
    conn.execute(
        """INSERT INTO ai_runs (customer_id, conversation_id, role, task, model, outcome, reason, language, sources,
                                tokens_in, tokens_out, actor)
           VALUES (%s, %s, %s, %s, %s, 'done', %s, %s, %s, %s, %s, %s)""",
        (
            customer_id,
            conversation_id,
            ROLE,
            task,
            out.model,
            out.reason[:500],
            out.language,
            Jsonb(sources),
            out.tokens_in,
            out.tokens_out,
            actor,
        ),
    )
    base = {"task": task, "model": out.model, "reason": out.reason}
    if task == "summary":
        return {**base, "summary": out.answer}
    if task == "draft":
        draft = out.answer
        # Notes stay internal: a draft that quotes a note is refused.
        for n in ctx.get("notes") or []:
            snippet = n["text"].strip()[:40]
            if len(snippet) >= 12 and snippet.lower() in draft.lower():
                draft = "The draft quoted a private note, so it was withheld. Ask again or write the reply yourself."
                sources = []
                break
        return {**base, "draft": draft, "sources": sources, "language": out.language or prof["business_language"]}
    if task == "translate":
        translated = not out.escalate and bool(out.answer)
        return {
            **base,
            "text": out.answer if translated else ctx["text"],
            "language": out.language if translated else ctx["from_language"],
            "to": ctx["to_language"],
            "translated": translated,
            "original": ctx["text"],
        }
    return {**base, "items": out.items}
