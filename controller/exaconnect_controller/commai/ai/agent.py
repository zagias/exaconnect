"""The customer AI agent (ADR 0019).

Runs as the durable job "ai.respond", queued by the inbox when a customer
writes to a conversation the AI is handling.

What it reads: the conversation's customer-facing messages (never
commai_notes), approved knowledge, and the contact's own records and memory
only when their identity is verified.

What it does: replies through inbox.send(author_kind="ai"), which refuses the
moment a person has taken over (inbox.NotHandler); the whole run is then
rolled back and nothing is written. It proposes actions through
actions.propose with role customer_agent; it never says a booking is
confirmed: actions confirms on success. When it is unsure it hands the
conversation to a person with a handover packet. When the model or anything
it depends on fails, the conversation goes to a person with a holding reply.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import actions, events, inbox, jobs
from . import attachments, knowledge, language, memory, runtime
from .model import EMAIL, NAME, PHONE, ModelError, ModelOutput, parse_when

log = logging.getLogger("exaconnect.commai.ai.agent")

events.register("ai.escalated", "ai.call_ended")

ROLE = "customer_agent"
ON_SUCCESS = {
    "book": "Your booking is confirmed for {start}. Reference: {booking_id}.",
    "create_lead": "Thanks, {name}. Your details are with our team and someone will be in touch.",
    "create_ticket": "Your request has been logged. Reference: {ticket_id}.",
}
PENDING_APPROVAL = "A member of our team needs to approve this first. You'll hear back here."
# Words that would tell a customer something is done before the business system said so.
FALSE_CONFIRMATION = re.compile(
    r"\b(confirmed|is booked|are booked|have booked|i've booked|booking is set|all set|you're booked|reserved)\b",
    re.I,
)
SAFE_BOOKING_LINE = "I've asked the calendar for that time. You'll get a message here as soon as it is booked."


class _Duplicate(Exception):
    """Another run already answered this message."""


# ---- context ----------------------------------------------------------------------


def _who(m: dict) -> str:
    if m["direction"] == "in":
        return "customer"
    return "ai" if m["author_kind"] == "ai" else "business"


def history(conn: psycopg.Connection, customer_id: Any, conversation_id: Any, limit: int) -> list[dict]:
    """Customer-facing messages only (inbox.messages never reads notes). A voice
    note's transcript counts as the customer's words (ADR 0026)."""
    msgs = inbox.messages(conn, customer_id, conversation_id)[-limit:]
    spoken = attachments.transcripts(conn, customer_id, msgs)
    out = []
    for m in msgs:
        text = " ".join(x for x in (m["body"], spoken.get(str(m["id"]), "")) if x).strip()
        if text:
            out.append({"from": _who(m), "text": text, "at": m["created_at"].isoformat()})
    return out


def build_context(
    conn: psycopg.Connection, customer_id: Any, conv: dict, prof: dict, last_text: str, customer_language: str
) -> dict:
    rp = runtime.PROFILES[ROLE]
    verified = memory.identity_verified(conn, conv)
    contact: dict = {}
    remembered: list[str] = []
    if verified and conv["contact_id"]:
        row = conn.execute("SELECT name, email, phone FROM contacts WHERE id = %s", (conv["contact_id"],)).fetchone()
        contact = dict(row) if row else {}
        if prof.get("memory", True):
            remembered = [f["fact"] for f in memory.facts(conn, customer_id, conv["contact_id"])]
        contact["previous_conversations"] = conn.execute(
            """SELECT subject, channel, state, created_at::date::text AS opened FROM conversations
               WHERE contact_id = %s AND id <> %s ORDER BY created_at DESC LIMIT 5""",
            (conv["contact_id"], conv["id"]),
        ).fetchall()
    elif conv["channel"] in ("whatsapp", "sms", "email") and conv["identity_id"]:
        # Not a record: the address this conversation is already happening on.
        ident = conn.execute("SELECT address FROM contact_identities WHERE id = %s", (conv["identity_id"],)).fetchone()
        if ident:
            contact = {"email" if conv["channel"] == "email" else "phone": ident["address"]}
    kb = knowledge.lookup(conn, customer_id, last_text, rp.max_knowledge)
    tools = runtime.usable_tools(conn, customer_id, ROLE)
    last = conn.execute(
        "SELECT intent FROM ai_runs WHERE conversation_id = %s AND role = %s ORDER BY created_at DESC LIMIT 1",
        (conv["id"], ROLE),
    ).fetchone()
    pending = conn.execute(
        """SELECT app, action, status, inputs->>'start' AS start FROM action_runs
           WHERE conversation_id = %s ORDER BY created_at DESC LIMIT 5""",
        (conv["id"],),
    ).fetchall()
    return {
        "business": runtime.business_name(conn, customer_id),
        "business_language": prof["business_language"],
        "customer_language": customer_language,
        "channel": conv["channel"],
        "conversation": history(conn, customer_id, conv["id"], rp.max_history),
        "knowledge": kb,
        "tools": [t["tool"] for t in tools],
        "tool_details": tools,
        "verified": verified,
        "contact": contact,
        "memory": remembered,
        "actions_in_this_conversation": pending,
        "last_ai_intent": last["intent"] if last else "",
    }


def collected(ctx: dict) -> dict:
    """What the customer has told us so far, for the person taking over."""
    texts = [m["text"] for m in ctx.get("conversation", []) if m["from"] == "customer"]
    joined = "\n".join(texts)
    out: dict = {"language": language.name(ctx.get("customer_language", ""))}
    if (n := NAME.search(joined)) is not None:
        out["name"] = n.group(1)
    if (e := EMAIL.search(joined)) is not None:
        out["email"] = e.group(0)
    if (p := PHONE.search(joined)) is not None:
        out["phone"] = p.group(0).strip()
    when = next((t for t in (parse_when(x) for x in reversed(texts)) if t), None)
    if when:
        out["requested_time"] = when.isoformat()
    out["identity_verified"] = bool(ctx.get("verified"))
    return out


def summary(ctx: dict) -> str:
    texts = [m["text"] for m in ctx.get("conversation", []) if m["from"] == "customer"]
    if not texts:
        return "No customer messages yet."
    first = " ".join(texts[0].split())[:200]
    s = f'{len(texts)} message(s) from the customer. First: "{first}".'
    if len(texts) > 1:
        s += f' Latest: "{" ".join(texts[-1].split())[:200]}".'
    return s


# ---- the run ------------------------------------------------------------------------


@jobs.handler("ai.respond")
def _respond_job(conn: psycopg.Connection, job: dict):
    p = job["payload"]
    respond(conn, job["customer_id"], p["conversation_id"], p.get("message_id"))
    return None


def respond(conn: psycopg.Connection, customer_id: Any, conversation_id: Any, message_id: Any = None) -> dict:
    """Answer the latest customer message. Returns {"outcome": ...}:
    replied | proposed | escalated | failed | stopped | duplicate | superseded."""
    conv = conn.execute(
        "SELECT * FROM conversations WHERE id = %s AND customer_id = %s", (conversation_id, customer_id)
    ).fetchone()
    if conv is None or conv["handler"] != "ai":
        return {"outcome": "stopped"}
    if message_id:
        if conn.execute("SELECT 1 FROM ai_runs WHERE message_id = %s AND role = %s", (message_id, ROLE)).fetchone():
            return {"outcome": "duplicate"}
        newer = conn.execute(
            """SELECT 1 FROM messages m WHERE m.conversation_id = %s AND m.direction = 'in'
               AND m.created_at > (SELECT created_at FROM messages WHERE id = %s) LIMIT 1""",
            (conversation_id, message_id),
        ).fetchone()
        if newer:
            return {"outcome": "superseded"}  # the newer message's own run answers both
    prof = runtime.profile(conn, customer_id)
    try:
        with conn.transaction():  # a savepoint: a takeover mid-run leaves nothing written
            return _run(conn, customer_id, conv, message_id, prof)
    except _Duplicate:
        return {"outcome": "duplicate"}
    except inbox.NotHandler:
        return {"outcome": "stopped"}
    except Exception as e:  # noqa: BLE001 - the model or a dependency failed: a person takes it
        log.warning("AI agent failed on conversation %s: %s", conversation_id, type(e).__name__)
        return _fail(conn, customer_id, conversation_id, message_id, prof, e)


def _claim(conn, customer_id: Any, conv: dict, message_id: Any) -> Any:
    row = conn.execute(
        """INSERT INTO ai_runs (customer_id, conversation_id, message_id, role, task, outcome, actor)
           VALUES (%s, %s, %s, %s, 'reply', 'failed', 'ai') ON CONFLICT DO NOTHING RETURNING id""",
        (customer_id, conv["id"], message_id, ROLE),
    ).fetchone()
    if row is None:
        raise _Duplicate()
    return row["id"]


def _finish_run(conn, run_id: Any, **fields: Any) -> None:
    for k in ("sources", "tool_calls"):
        if k in fields:
            fields[k] = Jsonb(fields[k])
    sets = ", ".join(f"{k} = %({k})s" for k in fields)
    conn.execute(f"UPDATE ai_runs SET {sets} WHERE id = %(id)s", {**fields, "id": run_id})


def _run(conn: psycopg.Connection, customer_id: Any, conv: dict, message_id: Any, prof: dict) -> dict:
    # The run is claimed (an ai_runs row) only after the model call: the row
    # references the conversation, and holding that reference during the call
    # would block a person taking over.
    run_id = None
    last_msg = conn.execute(
        """SELECT id, body, attachments FROM messages WHERE conversation_id = %s AND direction = 'in'
           ORDER BY created_at DESC LIMIT 1""",
        (conv["id"],),
    ).fetchone()
    last_text = last_msg["body"] if last_msg else ""
    # Attachments and voice notes (ADR 0026): a voice note's words are the
    # customer's words; other files go to the model as data.
    files = attachments.for_message(conn, customer_id, last_msg) if last_msg else {"files": [], "unreadable": []}
    if files.get("transcript"):
        last_text = f"{last_text} {files['transcript']}".strip()
    business_lang = prof["business_language"]
    detected = language.detect(last_text, default=conv["language"] or business_lang)
    ctx = build_context(conn, customer_id, conv, prof, last_text, detected)
    ctx["attachments"] = files["files"]
    esc = prof["escalation"]

    def escalate(reason: str, *, out: ModelOutput | None = None, tried: list[str] | None = None, gap: str = ""):
        return _escalate(
            conn,
            customer_id,
            conv,
            run_id or _claim(conn, customer_id, conv, message_id),
            prof,
            ctx,
            reason,
            out=out,
            tried=tried or [],
            gap=gap,
            question=last_text,
        )

    if not prof.get("enabled", True):
        return escalate("The AI agent is switched off for this business.")
    if not last_text.strip() and not files["files"]:
        why = "; ".join(files["unreadable"]) or "the message has nothing the AI can read"
        return escalate(f"The AI could not read what the customer sent: {why}.")
    low = last_text.lower()
    hit = next((k for k in esc.get("keywords") or [] if k and re.search(rf"\b{re.escape(k.lower())}\b", low)), None)
    if hit:
        return escalate(f'The customer\'s message matches the escalation rule "{hit}".')
    replies = conn.execute(
        """SELECT count(*) AS n FROM ai_runs WHERE conversation_id = %s AND role = %s
           AND outcome IN ('replied', 'proposed')""",
        (conv["id"], ROLE),
    ).fetchone()["n"]
    if esc.get("max_ai_replies") and replies >= int(esc["max_ai_replies"]):
        return escalate(f"The AI has replied {replies} times; the business asks for a person after that.")
    allowed_langs = prof.get("languages") or [business_lang]
    if detected not in allowed_langs and detected != business_lang:
        return escalate(f"The customer writes in {language.name(detected)}, which the AI agent is not set to answer.")

    try:
        out = runtime.complete(conn, customer_id, ROLE, "reply", ctx, ref=str(message_id or ""), prof=prof)
    except runtime.UsageLimit as e:
        return escalate(str(e) + " A person takes over.")

    run_id = _claim(conn, customer_id, conv, message_id)
    kb = ctx["knowledge"]
    hit_ids = {h["chunk_id"] for h in kb["hits"]}
    out.sources = [s for s in out.sources if s in hit_ids]
    tried = [f"Searched approved knowledge: {len(kb['hits'])} relevant passage(s)."]
    if kb["contradictory"]:
        tried.append("Sources disagree: " + kb["contradiction"])
    question_like = out.intent in ("", "question")
    if out.escalate:
        gap = ""
        if question_like and kb["contradictory"]:
            gap = "contradictory"
        elif question_like and not kb["hits"]:
            gap = "missing"
        return escalate(out.reason or "The AI was not sure.", out=out, tried=tried, gap=gap)
    if question_like and not out.tool_calls:
        if kb["contradictory"]:
            return escalate(
                "The approved sources disagree: " + kb["contradiction"], out=out, tried=tried, gap="contradictory"
            )
        if not out.sources and not kb["hits"] and esc.get("on_missing_knowledge", "escalate") == "escalate":
            return escalate("No approved knowledge covers this question.", out=out, tried=tried, gap="missing")

    # Proposals: checked and executed by commai.actions, never by the model.
    proposed: list[dict] = []
    extra: list[str] = []
    for call in out.tool_calls:
        tool = call.get("tool", "")
        app, _, action = tool.partition(".")
        template = ON_SUCCESS.get(action)
        try:
            run = actions.propose(
                conn,
                customer_id,
                role=ROLE,
                app=app,
                action=action,
                inputs=call.get("inputs") or {},
                actor="ai:customer_agent",
                conversation_id=conv["id"],
                on_success={"reply": template} if template else None,
            )
        except actions.ActionRefused as e:
            tried.append(f"Proposed {tool}: refused ({e}).")
            return escalate(f"The AI could not {action.replace('_', ' ')}: {e}", out=out, tried=tried)
        proposed.append({"tool": tool, "run_id": str(run["id"]), "status": run["status"]})
        tried.append(f"Proposed {tool}: {run['status'].replace('_', ' ')}.")
        if run["status"] == "awaiting_approval":
            extra.append(PENDING_APPROVAL)

    answer = (out.answer or "").strip()
    if proposed or out.intent.startswith("booking"):
        # Only the calendar's success confirms a booking, never the AI's words.
        if FALSE_CONFIRMATION.search(answer):
            answer = SAFE_BOOKING_LINE
    if not answer and not extra:
        return escalate("The AI had nothing to say.", out=out, tried=tried)
    body = " ".join([answer, *extra]).strip()

    first_ai = not conn.execute(
        "SELECT 1 FROM messages WHERE conversation_id = %s AND author_kind = 'ai' LIMIT 1", (conv["id"],)
    ).fetchone()
    greeting = (prof.get("greeting") or "").strip()
    original_body, original_language = "", ""
    reply_lang = out.language or business_lang
    if reply_lang != business_lang and out.answer_original:
        original_body = " ".join([out.answer_original, *extra]).strip()
        original_language = business_lang
    elif detected != business_lang and reply_lang == business_lang and language.fallback_note(detected):
        # No translation available: say so in their language, then answer in ours.
        body = language.fallback_note(detected) + "\n\n" + body
        reply_lang = business_lang
    if first_ai and greeting and reply_lang == business_lang:
        body = greeting + "\n\n" + body

    if detected != (conv["language"] or ""):
        # Written only now: holding the conversation row during the model call
        # would block a person taking over.
        conn.execute("UPDATE conversations SET language = %s WHERE id = %s", (detected, conv["id"]))
    msg = inbox.send(
        conn,
        customer_id,
        conv["id"],
        body,
        author_kind="ai",
        author=prof["name"],
        original_body=original_body,
        original_language=original_language,
    )
    if ctx["verified"] and prof.get("memory", True) and conv["contact_id"]:
        for fact in out.facts:
            memory.remember(conn, customer_id, conv["contact_id"], fact, conversation_id=conv["id"])
    sources = [
        {k: h[k] for k in ("chunk_id", "source_id", "title", "source_url")}
        for h in kb["hits"]
        if h["chunk_id"] in out.sources
    ]
    outcome = "proposed" if proposed else "replied"
    _finish_run(
        conn,
        run_id,
        reply_message_id=msg["id"],
        model=out.model,
        outcome=outcome,
        intent=out.intent,
        reason=out.reason,
        language=reply_lang,
        sources=sources,
        tool_calls=proposed,
        tokens_in=out.tokens_in,
        tokens_out=out.tokens_out,
    )
    events.emit(
        conn,
        customer_id,
        "ai.replied",
        {
            "conversation_id": str(conv["id"]),
            "message_id": str(msg["id"]),
            "sources": len(sources),
            "proposed": [p["tool"] for p in proposed],
        },
        conv["id"],
    )
    return {"outcome": outcome, "message_id": str(msg["id"]), "proposed": proposed, "sources": sources}


def _packet(ctx: dict, reason: str, tried: list[str], out: ModelOutput | None) -> dict:
    return {
        "summary": summary(ctx),
        "collected": collected(ctx),
        "tried": tried,
        "why": reason,
        "intent": out.intent if out else "",
        "history": [{"from": m["from"], "text": m["text"][:500], "at": m["at"]} for m in ctx["conversation"][-10:]],
    }


def _escalate(
    conn,
    customer_id: Any,
    conv: dict,
    run_id: Any,
    prof: dict,
    ctx: dict,
    reason: str,
    *,
    out: ModelOutput | None,
    tried: list[str],
    gap: str,
    question: str,
) -> dict:
    locked = inbox.get(conn, customer_id, conv["id"], lock=True)
    if locked["handler"] != "ai":
        raise inbox.NotHandler()
    inbox.hand_over(
        conn,
        customer_id,
        conv["id"],
        reason=reason,
        packet=_packet(ctx, reason, tried, out),
        holding_reply=prof.get("holding_reply") or runtime.DEFAULT_PROFILE["holding_reply"],
    )
    if gap:
        knowledge.record_gap(
            conn,
            customer_id,
            question,
            gap,
            detail=reason if gap == "contradictory" else "",
            conversation_id=conv["id"],
        )
    _finish_run(
        conn,
        run_id,
        outcome="escalated",
        reason=reason[:500],
        model=out.model if out else "",
        intent=out.intent if out else "",
        tokens_in=out.tokens_in if out else 0,
        tokens_out=out.tokens_out if out else 0,
    )
    events.emit(
        conn, customer_id, "ai.escalated", {"conversation_id": str(conv["id"]), "reason": reason[:300]}, conv["id"]
    )
    return {"outcome": "escalated", "reason": reason}


def _fail(conn, customer_id: Any, conversation_id: Any, message_id: Any, prof: dict, err: Exception) -> dict:
    """The model or a dependency failed: a person takes the conversation, with a
    holding reply to the customer, so it is never lost and never silent."""
    conv = conn.execute(
        "SELECT * FROM conversations WHERE id = %s AND customer_id = %s FOR UPDATE", (conversation_id, customer_id)
    ).fetchone()
    if conv is None or conv["handler"] != "ai":
        return {"outcome": "stopped"}
    what = str(err) if isinstance(err, ModelError) else f"{type(err).__name__} in the AI agent"
    reason = f"The AI could not answer: {what}"
    try:
        hist = history(conn, customer_id, conversation_id, 10)
    except Exception:  # noqa: BLE001
        hist = []
    ctx = {"conversation": hist, "customer_language": conv["language"], "verified": False}
    conn.execute(
        """INSERT INTO ai_runs (customer_id, conversation_id, message_id, role, task, outcome, reason, actor)
           VALUES (%s, %s, %s, %s, 'reply', 'failed', %s, 'ai') ON CONFLICT DO NOTHING""",
        (customer_id, conversation_id, message_id or None, ROLE, reason[:500]),
    )
    inbox.hand_over(
        conn,
        customer_id,
        conversation_id,
        reason=reason,
        packet=_packet(ctx, reason, ["Called the AI model: it failed."], None),
        holding_reply=prof.get("holding_reply") or runtime.DEFAULT_PROFILE["holding_reply"],
    )
    events.emit(
        conn, customer_id, "ai.failed", {"conversation_id": str(conversation_id), "error": what[:300]}, conversation_id
    )
    return {"outcome": "failed", "reason": reason}
