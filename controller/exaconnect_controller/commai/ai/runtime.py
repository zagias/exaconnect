"""One AI runtime, three role profiles (ADR 0019).

The roles share the runtime and the model, never their permissions:

- customer_agent: serves the business's customers. Reads customer-facing
  messages, approved knowledge, and the contact's own records and memory only
  once their identity is verified. Proposes actions from its tool list.
- copilot: serves staff. Reads what the staff member may read (notes
  included when they may read notes). Drafts; a person sends.
- platform_assistant: serves the business's admins. Explains and proposes;
  changes apply only when an admin approves.

Each role's tools are listed per business in commai_role_tools. The prompt
names them, but the list is enforced by commai.actions.propose, which refuses
anything not listed, whatever the model asks for.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import psycopg

from ...settings import get_settings
from .. import connectors, inbox, usage
from .model import Model, ModelError, ModelInput, ModelOutput, OpenAICompatibleModel, SimulatedModel

ROLES = ("customer_agent", "copilot", "platform_assistant")


class UsageLimit(Exception):
    """The business has reached its hard monthly limit for this AI work."""


@dataclass(frozen=True)
class RoleProfile:
    role: str
    label: str
    serves: str
    may_read: str
    may_do: str
    meter: str
    max_tokens: int
    temperature: float
    max_history: int  # messages of the conversation given to the model
    max_knowledge: int  # knowledge chunks given to the model
    tool_kinds: tuple[str, ...]  # action kinds this role may ever be given


PROFILES: dict[str, RoleProfile] = {
    "customer_agent": RoleProfile(
        "customer_agent",
        "Customer AI agent",
        "The business's customers, on chat, WhatsApp and calls",
        "Approved knowledge; the customer's own records and memory once their identity is verified",
        "Answer, qualify leads, book, create tickets and other actions the business switched on",
        "ai_reply",
        600,
        0.2,
        20,
        4,
        ("read", "create", "update", "cancel"),
    ),
    "copilot": RoleProfile(
        "copilot",
        "Employee copilot",
        "Staff in the inbox and on calls",
        "What the staff member may read, private notes included when they may read notes",
        "Summarise, draft grounded replies, translate, flag missing details, suggest next steps. A person sends",
        "copilot",
        700,
        0.2,
        40,
        4,
        ("read",),
    ),
    "platform_assistant": RoleProfile(
        "platform_assistant",
        "Platform assistant",
        "The business's admins",
        "Configuration, health, diagnostics and usage",
        "Explain, diagnose, propose setup and fixes. Changes apply only when an admin approves",
        "copilot",
        900,
        0.1,
        10,
        0,
        ("read",),
    ),
}


# ---- the business's AI profile (commai_settings.config["ai"]) -------------------------

DEFAULT_PROFILE: dict = {
    "enabled": True,
    "name": "Assistant",
    "tone": "friendly, plain and brief",
    "greeting": "",
    "business_language": "en",
    "languages": ["en", "es", "fr", "pt", "nl", "ht", "pap"],
    "memory": True,
    "holding_reply": "Thanks for your message. A member of our team will reply here shortly.",
    "escalation": {
        "keywords": ["human", "person", "someone", "agent", "manager", "complaint", "complain"],
        "max_ai_replies": 8,
        "on_missing_knowledge": "escalate",
    },
}


def profile(conn: psycopg.Connection, customer_id: Any) -> dict:
    cfg = (inbox.settings(conn, customer_id)["config"] or {}).get("ai") or {}
    out = {**DEFAULT_PROFILE, **{k: v for k, v in cfg.items() if k in DEFAULT_PROFILE}}
    out["escalation"] = {**DEFAULT_PROFILE["escalation"], **(cfg.get("escalation") or {})}
    return out


def business_name(conn: psycopg.Connection, customer_id: Any) -> str:
    row = conn.execute("SELECT name FROM customers WHERE id = %s", (customer_id,)).fetchone()
    return row["name"] if row else "the business"


def system_prompt(role: str, prof: dict, business: str) -> str:
    if role == "customer_agent":
        return f"""You are {prof["name"]}, the AI agent answering customers for {business}. Tone: {prof["tone"]}.
- Answer only from the approved knowledge in the context and cite the chunk ids you used in "sources".
- If the knowledge does not cover the question, or two sources disagree, set "escalate" to true and say why \
in "reason". Never guess prices, times, policies or availability.
- To act (book, create a lead or ticket) put a tool call in "tool_calls" using only the tools listed in the \
context. Never say an action is done or a booking is confirmed: the system confirms it to the customer only \
after the business system reports success.
- Use the contact's records and memory only if they appear in the context (their identity is verified).
- Reply in the customer's language ("customer_language"). When it differs from the business language, also give \
the same reply in the business language in "answer_original".
- Escalate when the customer asks for a person, is upset, or anything sensitive comes up.
- Keep replies under 80 words. Plain British English when replying in English."""
    if role == "copilot":
        return f"""You are the copilot helping staff at {business} handle a conversation. You never send anything: \
a person reviews and sends. Ground every draft in the approved knowledge given and cite chunk ids in "sources". \
Private notes in the context are for staff only: never copy them into a draft for the customer. \
Plain British English unless asked to translate."""
    return f"""You are the platform assistant for {business}'s CommAI admins. Explain settings, health, \
diagnostics and usage from the context only. Propose changes; never claim one was made. Plain British English."""


# ---- tools --------------------------------------------------------------------------


def listed_tools(conn: psycopg.Connection, customer_id: Any, role: str) -> list[str]:
    rows = conn.execute(
        "SELECT tool FROM commai_role_tools WHERE customer_id = %s AND role = %s ORDER BY tool", (customer_id, role)
    ).fetchall()
    return [r["tool"] for r in rows]


def usable_tools(conn: psycopg.Connection, customer_id: Any, role: str) -> list[dict]:
    """Tools the role may use right now: listed for the role, of a kind the role
    may have, and switched on for a live (or testing) connection."""
    listed = set(listed_tools(conn, customer_id, role))
    kinds = PROFILES[role].tool_kinds
    out = []
    for app in connectors.catalogue():
        c = connectors.connection(conn, customer_id, app["app"])
        if c is None or c["status"] not in ("live", "testing", "authorised"):
            continue
        for a in app["actions"]:
            name = f"{app['app']}.{a['name']}"
            if a["kind"] not in kinds or a["name"] not in c["allowed_actions"]:
                continue
            if name in listed or f"{app['app']}.*" in listed:
                out.append(
                    {
                        "tool": name,
                        "label": a["label"],
                        "sensitive": a["sensitive"],
                        "inputs": [f["name"] for f in a["fields"]],
                    }
                )
    return out


def catalogue_tools() -> list[dict]:
    """Every tool any role could be given, from the connectors catalogue."""
    out = []
    for app in connectors.catalogue():
        for a in app["actions"]:
            out.append(
                {
                    "tool": f"{app['app']}.{a['name']}",
                    "app": app["app"],
                    "app_label": app["label"],
                    "label": a["label"],
                    "kind": a["kind"],
                    "sensitive": a["sensitive"],
                    "roles": [r for r in ROLES if a["kind"] in PROFILES[r].tool_kinds],
                }
            )
    return out


# ---- the model ----------------------------------------------------------------------

_override: Model | None = None


def set_model(model: Model | None) -> None:
    """Use this model for every role (tests and the lab). None restores the default."""
    global _override
    _override = model


def get_model() -> Model:
    if _override is not None:
        return _override
    s = get_settings()
    if s.llm_api_key:
        return OpenAICompatibleModel(api_key=s.llm_api_key, base_url=s.llm_base_url, model=s.llm_model)
    return SimulatedModel()


SIMULATED_NOTE = (
    "No AI service key is set (EXA_LLM_API_KEY), so the built-in simulated model answers: it answers from"
    " approved knowledge, recognises booking requests and hands anything else to a person. It cannot translate."
)


def model_status() -> dict:
    m = get_model()
    live = isinstance(m, OpenAICompatibleModel)
    return {
        "model": m.name,
        "live": live,
        "note": "" if live else SIMULATED_NOTE,
    }


def complete(
    conn: psycopg.Connection,
    customer_id: Any,
    role: str,
    task: str,
    context: dict,
    *,
    ref: str = "",
    business: str | None = None,
    prof: dict | None = None,
) -> ModelOutput:
    """Run one model call for a role, within the business's usage limits.
    Raises UsageLimit before calling when the hard limit is reached, and
    ModelError when the model fails."""
    rp = PROFILES[role]
    if not usage.allowed(conn, customer_id, rp.meter) or not usage.allowed(conn, customer_id, "ai_tokens", 0):
        raise UsageLimit(f"The monthly limit for {rp.meter.replace('_', ' ')} has been reached.")
    prof = prof or profile(conn, customer_id)
    business = business or business_name(conn, customer_id)
    inp = ModelInput(
        role=role,
        task=task,
        system=system_prompt(role, prof, business),
        context=context,
        max_tokens=rp.max_tokens,
        temperature=rp.temperature,
    )
    out = get_model().complete(inp)
    if not isinstance(out, ModelOutput):
        raise ModelError("The AI service sent an answer in an unexpected shape.")
    ref = ref or f"{role}:{uuid.uuid4()}"
    usage.record(conn, customer_id, rp.meter, 1, ref=ref, detail={"role": role, "task": task, "model": out.model})
    tokens = out.tokens_in + out.tokens_out
    if tokens:
        usage.record(
            conn,
            customer_id,
            "ai_tokens",
            tokens,
            ref=ref,
            detail={"in": out.tokens_in, "out": out.tokens_out, "model": out.model},
        )
    return out
