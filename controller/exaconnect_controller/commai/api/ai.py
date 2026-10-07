"""AI agents API (ADR 0019): the business's AI profile, which tools each role
may use, knowledge with sources and approval, knowledge gaps, the employee
copilot, customer memory and browser calls with the AI agent.

Every write is audited. Reads need commai:read; settings, knowledge and role
tools need commai:admin and a seat that may change settings.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from ... import audit, db
from ...api.deps import UserDep
from .. import access, inbox
from ..ai import agent, copilot, governance, knowledge, memory, runtime, voice
from ..ai.model import ModelError
from .common import errors

router = APIRouter(prefix="/customers/{customer_id}", tags=["commai: ai"])


def _admin(conn, user, customer_id: str) -> None:
    """Settings that change how the AI behaves: not for internal (notes-only) seats."""
    access.require_business_admin(user)
    if access.seat(conn, user, customer_id) == "internal":
        raise HTTPException(403, "Your seat can't change AI settings.")


# ---- status and profile -----------------------------------------------------------------


@router.get("/ai/status")
def ai_status(customer_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        prof = runtime.profile(conn, customer_id)
        mode = inbox.settings(conn, customer_id)["mode"]
    return {
        **runtime.model_status(),
        "ai_available": inbox.ai_available(),
        "enabled": prof["enabled"],
        "mode": mode,
        "speech": voice.speech_status(),
    }


class Escalation(BaseModel):
    keywords: list[str] = Field(default_factory=list, max_length=30)
    max_ai_replies: int = Field(default=8, ge=0, le=50)
    on_missing_knowledge: Literal["escalate", "answer"] = "escalate"


class ProfileIn(BaseModel):
    enabled: bool | None = None
    name: str | None = Field(default=None, min_length=1, max_length=60)
    tone: str | None = Field(default=None, max_length=200)
    instructions: str | None = Field(default=None, max_length=2000)
    greeting: str | None = Field(default=None, max_length=500)
    business_language: str | None = Field(default=None, min_length=2, max_length=5)
    languages: list[str] | None = Field(default=None, max_length=20)
    memory: bool | None = None
    holding_reply: str | None = Field(default=None, min_length=1, max_length=500)
    escalation: Escalation | None = None


@router.get("/ai/profile")
def get_profile(customer_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return runtime.profile(conn, customer_id)


@router.put("/ai/profile")
def put_profile(customer_id: str, body: ProfileIn, user: UserDep) -> dict:
    """Change the AI agent's name, tone, greeting, languages, memory and escalation rules."""
    access.check(user, customer_id, "commai:admin")
    fields = body.model_dump(exclude_none=True)
    if "escalation" in fields:
        fields["escalation"]["keywords"] = [k.strip()[:40] for k in fields["escalation"]["keywords"] if k.strip()]
    if "languages" in fields:
        fields["languages"] = sorted({x.strip().lower()[:5] for x in fields["languages"] if x.strip()})
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        # Tone and instructions go live only after the evaluation suite passes (ADR 0026).
        fields, later = governance.split_profile_change(conn, customer_id, fields)
        candidate = None
        if later:
            before = runtime.profile(conn, customer_id)
            candidate = governance.create_candidate(
                conn, customer_id, "instructions", later, {k: before.get(k) for k in later}, user.actor
            )
            audit.record(
                conn,
                user.actor,
                "commai.ai.candidate.create",
                str(candidate["id"]),
                customer_id,
                {"fields": sorted(later)},
            )
        current = (inbox.settings(conn, customer_id)["config"] or {}).get("ai") or {}
        new = {**current, **fields}
        conn.execute(
            """UPDATE commai_settings SET config = jsonb_set(config, '{ai}', %s), updated_at = now()
               WHERE customer_id = %s""",
            (Jsonb(new), customer_id),
        )
        audit.record(conn, user.actor, "commai.ai.profile.update", "", customer_id, {"fields": sorted(fields)})
        out = runtime.profile(conn, customer_id)
        out["candidate"] = candidate
        return out


# ---- role tools -------------------------------------------------------------------------


@router.get("/ai/roles")
def list_roles(customer_id: str, user: UserDep) -> dict:
    """Each AI role, what it may read and do, the tools listed for it and the
    ones usable now (listed and switched on in a live integration)."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        roles = []
        for r in runtime.ROLES:
            p = runtime.PROFILES[r]
            roles.append(
                {
                    "role": r,
                    "label": p.label,
                    "serves": p.serves,
                    "may_read": p.may_read,
                    "may_do": p.may_do,
                    "tool_kinds": list(p.tool_kinds),
                    "tools": runtime.listed_tools(conn, customer_id, r),
                    "usable": [t["tool"] for t in runtime.usable_tools(conn, customer_id, r)],
                }
            )
    return {"roles": roles, "catalogue": runtime.catalogue_tools()}


class ToolsIn(BaseModel):
    tools: list[str] = Field(default_factory=list, max_length=100)


@router.put("/ai/roles/{role}/tools")
def set_role_tools(customer_id: str, role: str, body: ToolsIn, user: UserDep) -> dict:
    """Replace the list of tools a role may use. Anything not listed is refused
    by the action service, whatever the AI asks for."""
    access.check(user, customer_id, "commai:admin")
    if role not in runtime.ROLES:
        raise HTTPException(404, f"Unknown role {role}.")
    cat = {t["tool"]: t for t in runtime.catalogue_tools()}
    apps = {t["app"] for t in cat.values()}
    kinds = runtime.PROFILES[role].tool_kinds
    clean: list[str] = []
    for t in dict.fromkeys(x.strip() for x in body.tools if x.strip()):
        if t.endswith(".*") and t[:-2] in apps:
            clean.append(t)
            continue
        if t not in cat:
            raise HTTPException(422, f"There is no tool called {t}.")
        if cat[t]["kind"] not in kinds:
            raise HTTPException(
                422, f"The {runtime.PROFILES[role].label.lower()} can't be given {t} ({cat[t]['kind']} actions)."
            )
        clean.append(t)
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        before = runtime.listed_tools(conn, customer_id, role)
        conn.execute("DELETE FROM commai_role_tools WHERE customer_id = %s AND role = %s", (customer_id, role))
        for t in clean:
            conn.execute(
                "INSERT INTO commai_role_tools (customer_id, role, tool) VALUES (%s, %s, %s)", (customer_id, role, t)
            )
        audit.record(
            conn, user.actor, "commai.ai.role_tools.set", role, customer_id, {"before": before, "after": clean}
        )
        return {
            "role": role,
            "tools": clean,
            "usable": [t["tool"] for t in runtime.usable_tools(conn, customer_id, role)],
        }


# ---- knowledge --------------------------------------------------------------------------


class SourceIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=200_000)
    source_url: str = Field(default="", max_length=500)
    approved: bool = False


class SourcePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, min_length=1, max_length=200_000)
    source_url: str | None = Field(default=None, max_length=500)


class ApproveIn(BaseModel):
    approved: bool = True


@router.get("/ai/knowledge")
def list_knowledge(customer_id: str, user: UserDep) -> list[dict]:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return knowledge.list_sources(conn, customer_id)


@router.post("/ai/knowledge", status_code=201)
def add_knowledge(customer_id: str, body: SourceIn, user: UserDep) -> dict:
    """Add business content. The AI uses it only once it is approved."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        try:
            row = knowledge.add_source(
                conn,
                customer_id,
                title=body.title,
                body=body.body,
                source_url=body.source_url,
                approved=body.approved,
                created_by=user.actor,
            )
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        audit.record(
            conn,
            user.actor,
            "commai.ai.knowledge.create",
            str(row["id"]),
            customer_id,
            {"title": row["title"], "approved": row["approved"]},
        )
    return row


@router.get("/ai/knowledge/search")
def search_knowledge(customer_id: str, user: UserDep, q: str = Query(min_length=1, max_length=500)) -> dict:
    """What the AI would find for a question (approved sources only)."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return knowledge.lookup(conn, customer_id, q, 5)


@router.get("/ai/knowledge/{source_id}")
def get_knowledge(customer_id: str, source_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        row = knowledge.get_source(conn, customer_id, source_id)
        if row is None:
            raise HTTPException(404, "Source not found.")
        row["chunks"] = conn.execute(
            "SELECT id, ordinal, text FROM knowledge_chunks WHERE source_id = %s ORDER BY ordinal", (source_id,)
        ).fetchall()
    return row


@router.patch("/ai/knowledge/{source_id}")
def update_knowledge(customer_id: str, source_id: str, body: SourcePatch, user: UserDep) -> dict:
    """Edit a source. An edited source needs approving again."""
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        try:
            row = knowledge.update_source(
                conn, customer_id, source_id, title=body.title, body=body.body, source_url=body.source_url
            )
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        if row is None:
            raise HTTPException(404, "Source not found.")
        audit.record(conn, user.actor, "commai.ai.knowledge.update", source_id, customer_id)
    return row


@router.post("/ai/knowledge/{source_id}/approve")
def approve_knowledge(customer_id: str, source_id: str, body: ApproveIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        row = knowledge.set_approved(conn, customer_id, source_id, body.approved, user.actor)
        if row is None:
            raise HTTPException(404, "Source not found.")
        audit.record(
            conn,
            user.actor,
            "commai.ai.knowledge.approve" if body.approved else "commai.ai.knowledge.withdraw",
            source_id,
            customer_id,
        )
    return row


@router.delete("/ai/knowledge/{source_id}", status_code=204)
def delete_knowledge(customer_id: str, source_id: str, user: UserDep) -> None:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        row = knowledge.delete_source(conn, customer_id, source_id)
        if row is None:
            raise HTTPException(404, "Source not found.")
        audit.record(conn, user.actor, "commai.ai.knowledge.delete", row["title"], customer_id)


@router.get("/ai/gaps")
def list_gaps(customer_id: str, user: UserDep, status: Literal["open", "resolved", "all"] = "open") -> list[dict]:
    """Questions the AI could not answer from approved knowledge."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        return knowledge.list_gaps(conn, customer_id, status)


@router.post("/ai/gaps/{gap_id}/resolve")
def resolve_gap(customer_id: str, gap_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:admin")
    with db.tx() as conn:
        _admin(conn, user, customer_id)
        row = knowledge.resolve_gap(conn, customer_id, gap_id, user.actor)
        if row is None:
            raise HTTPException(404, "Gap not found.")
        audit.record(conn, user.actor, "commai.ai.gap.resolve", gap_id, customer_id)
    return row


# ---- what the AI did in a conversation --------------------------------------------------


@router.get("/ai/conversations/{conversation_id}/runs")
def conversation_runs(customer_id: str, conversation_id: str, user: UserDep) -> list[dict]:
    """Each AI answer in the conversation with the sources it used, what it
    proposed and how it ended. For staff."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, errors():
        inbox.get(conn, customer_id, conversation_id)
        return conn.execute(
            """SELECT id, role, task, model, outcome, intent, reason, language, sources, tool_calls, message_id,
                      reply_message_id, tokens_in, tokens_out, actor, created_at
               FROM ai_runs WHERE conversation_id = %s AND customer_id = %s ORDER BY created_at DESC LIMIT 100""",
            (conversation_id, customer_id),
        ).fetchall()


# ---- copilot ----------------------------------------------------------------------------


class CopilotIn(BaseModel):
    text: str = Field(default="", max_length=5000)
    message_id: str | None = None
    to: str = Field(default="", max_length=5)
    instruction: str = Field(default="", max_length=500)


@router.post("/ai/conversations/{conversation_id}/copilot/{task}")
def copilot_task(
    customer_id: str,
    conversation_id: str,
    task: Literal["summary", "draft", "translate", "missing", "next_steps"],
    body: CopilotIn,
    user: UserDep,
) -> dict:
    """Summarise, draft a grounded reply with sources, translate, flag missing
    details or suggest next steps. Nothing is sent: a person reviews and sends."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, errors():
        inbox.get(conn, customer_id, conversation_id)
        try:
            out = copilot.run(
                conn,
                customer_id,
                conversation_id,
                task,
                actor=user.actor,
                include_notes=access.can_read_notes(user),
                text=body.text,
                message_id=body.message_id,
                to=body.to,
                instruction=body.instruction,
            )
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        except runtime.UsageLimit as e:
            raise HTTPException(429, str(e)) from e
        except ModelError as e:
            raise HTTPException(502, f"The copilot couldn't answer: {e}") from e
        audit.record(conn, user.actor, f"commai.ai.copilot.{task}", conversation_id, customer_id)
    return out


# ---- memory -----------------------------------------------------------------------------


class FactIn(BaseModel):
    fact: str = Field(min_length=1, max_length=300)


def _contact(conn, customer_id: str, contact_id: str) -> dict:
    row = conn.execute(
        "SELECT id FROM contacts WHERE id = %s AND customer_id = %s", (contact_id, customer_id)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Contact not found.")
    return row


@router.get("/ai/contacts/{contact_id}/memory")
def list_memory(customer_id: str, contact_id: str, user: UserDep) -> dict:
    """What the AI remembers about a contact. Used only when their identity is verified."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn:
        _contact(conn, customer_id, contact_id)
        verified = conn.execute(
            "SELECT bool_or(verified) AS v FROM contact_identities WHERE contact_id = %s", (contact_id,)
        ).fetchone()["v"]
        return {"facts": memory.facts(conn, customer_id, contact_id), "verified": bool(verified)}


@router.post("/ai/contacts/{contact_id}/memory", status_code=201)
def add_memory(customer_id: str, contact_id: str, body: FactIn, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn:
        _contact(conn, customer_id, contact_id)
        row = memory.remember(conn, customer_id, contact_id, body.fact, source="staff", created_by=user.actor)
        if row is None:
            raise HTTPException(409, "That fact is already remembered.")
        audit.record(conn, user.actor, "commai.ai.memory.add", contact_id, customer_id)
    return row


@router.delete("/ai/contacts/{contact_id}/memory/{fact_id}", status_code=204)
def delete_memory(customer_id: str, contact_id: str, fact_id: str, user: UserDep) -> None:
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn:
        _contact(conn, customer_id, contact_id)
        row = memory.forget(conn, customer_id, fact_id)
        if row is None or str(row["contact_id"]) != contact_id:
            raise HTTPException(404, "Fact not found.")
        audit.record(conn, user.actor, "commai.ai.memory.delete", contact_id, customer_id, {"fact_id": fact_id})


@router.delete("/ai/contacts/{contact_id}/memory", status_code=204)
def clear_memory(customer_id: str, contact_id: str, user: UserDep) -> None:
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn:
        _contact(conn, customer_id, contact_id)
        n = memory.forget_all(conn, customer_id, contact_id)
        audit.record(conn, user.actor, "commai.ai.memory.clear", contact_id, customer_id, {"facts": n})


# ---- browser calls (voice stage 1) ------------------------------------------------------


class CallIn(BaseModel):
    caller: str = Field(default="", max_length=100)


class TurnIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@router.get("/ai/speech")
def speech(customer_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:read")
    return voice.speech_status()


@router.post("/ai/calls", status_code=201)
def start_call(customer_id: str, body: CallIn, user: UserDep) -> dict:
    """Start a browser call with the AI agent. Speech is recognised and spoken
    in the browser; this API carries text only."""
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn, errors():
        out = voice.start_call(conn, customer_id, caller=body.caller, started_by=user.actor)
        audit.record(conn, user.actor, "commai.ai.call.start", out["conversation_id"], customer_id)
    return out


@router.post("/ai/calls/{conversation_id}/turns")
def caller_turn(customer_id: str, conversation_id: str, body: TurnIn, user: UserDep) -> dict:
    """What the caller said; returns what the AI says back (to speak in the browser)."""
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn, errors():
        msg = voice.caller_turn(conn, customer_id, conversation_id, body.text)
        audit.record(conn, user.actor, "commai.ai.call.turn", conversation_id, customer_id)
    # A separate transaction, so the reply is stored after the caller's words.
    with db.tx() as conn, errors():
        result = agent.respond(conn, customer_id, conversation_id, msg["id"])
    with db.tx() as conn:
        replies = voice.replies_after(conn, customer_id, conversation_id, msg)
        conv = inbox.get(conn, customer_id, conversation_id)
    return {
        "outcome": result.get("outcome"),
        "reply": " ".join(r["body"] for r in replies),
        "messages": replies,
        "handler": conv["handler"],
        "handed_over": conv["handler"] != "ai",
    }


@router.get("/ai/calls/{conversation_id}")
def get_call(customer_id: str, conversation_id: str, user: UserDep) -> dict:
    """The call record and its transcript."""
    access.check(user, customer_id, "commai:read")
    with db.tx() as conn, errors():
        call = voice.get_call(conn, customer_id, conversation_id)
        conv = inbox.get(conn, customer_id, conversation_id)
        call["handler"] = conv["handler"]
        call["transcript"] = [
            {"from": agent._who(m), "text": m["body"], "at": m["created_at"]}
            for m in inbox.messages(conn, customer_id, conversation_id)
        ]
    return call


@router.post("/ai/calls/{conversation_id}/end")
def end_call(customer_id: str, conversation_id: str, user: UserDep) -> dict:
    access.check(user, customer_id, "commai:write")
    with db.tx() as conn, errors():
        call = voice.end_call(conn, customer_id, conversation_id, user.actor)
        audit.record(conn, user.actor, "commai.ai.call.end", conversation_id, customer_id)
    return call
