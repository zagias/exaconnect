"""Judging a conversation against written criteria (ADR 0026).

Used by quality review (the business's own criteria) and by the evaluation
suite (expected and forbidden behaviour). The model does the judging; this
module holds the prompt, the simulated judge used without an AI service, and
the check that every quote offered as evidence really is in the conversation.

The simulated judge is deliberately simple and explainable:

- "greets ... by name": the first reply from the business contains the
  contact's first name.
- "escalates", "hands over", "a person": whether the AI handed over.
- "never X" / "must not X" / "don't X": fails when a reply from the business
  has a sentence containing X's key words without a negation ("not", "can't").
- anything else: passes when a reply from the business has a sentence with
  most of the criterion's key words.
"""

from __future__ import annotations

import re

from .model import ModelOutput, _stem, terms

JUDGE_SYSTEM = """You review a customer-service conversation against the business's written criteria.
For each criterion decide "pass", "fail" or "unclear". Quote the exact words from the conversation that show it
(copy them character for character; leave the quote empty when nothing shows it) and say why in one short line.
Judge only the business's replies (from "business" or "ai"), not the customer's words.
Put {"results": [{"id": criterion id, "verdict": "pass|fail|unclear", "quote": "...", "why": "..."}]} in "data"."""

ARTICLE_SYSTEM = """You draft a knowledge article for the business from questions customers asked that the AI
could not answer. Never invent facts, prices, times or policies: where an answer is needed, write a placeholder of
the form [Check: what the business must fill in]. A person reviews, completes and approves the draft before the AI
uses it. Put {"title": "...", "body": "..."} in "data"."""

NEGATIVE = re.compile(r"^\s*(never|do not|don't|must not|mustn't|should not|shouldn't|no)\b", re.I)
NEGATION = re.compile(r"\b(not|no|never|can't|cannot|won't|unable|don't|isn't|aren't)\b", re.I)
ESCALATION = re.compile(r"\b(escalat\w*|hand(?:s|ed)? (?:it )?over|handover|a person|human|staff member)\b", re.I)
GREETING = re.compile(r"\bgreet", re.I)
FILLER = set(
    "never always must should not don't do does customer customers business agent reply replies answer answers "
    "say says said mention mentions promise promises promised offer offers ever any every conversation".split()
)
FILLER_STEMS = {_stem(w) for w in FILLER}
PLACEHOLDER = re.compile(r"\[(?:Check|Write)[^\]]*\]", re.I)


def _sentences(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", text or "") if p.strip()]


def _key_terms(criterion: str) -> set[str]:
    body = NEGATIVE.sub("", criterion)
    return {t for t in terms(body) if t not in FILLER_STEMS}


def simulated_judge(ctx: dict) -> ModelOutput:
    conv = ctx.get("conversation") or []
    ours = [m["text"] for m in conv if m.get("from") in ("business", "ai") and m.get("text")]
    name = (ctx.get("contact_name") or "").strip().split(" ")[0]
    results = []
    for c in ctx.get("criteria") or []:
        cid, text = str(c.get("id")), str(c.get("text") or "")
        negative = bool(NEGATIVE.search(text))
        if GREETING.search(text) and "name" in text.lower():
            if not ours:
                results.append({"id": cid, "verdict": "unclear", "quote": "", "why": "The business never replied."})
            elif not name:
                results.append(
                    {"id": cid, "verdict": "unclear", "quote": "", "why": "The customer's name isn't known."}
                )
            else:
                first = _sentences(ours[0])[0] if _sentences(ours[0]) else ours[0]
                ok = re.search(rf"\b{re.escape(name)}\b", ours[0], re.I) is not None
                results.append(
                    {
                        "id": cid,
                        "verdict": "pass" if ok else "fail",
                        "quote": first,
                        "why": f"The first reply {'uses' if ok else 'does not use'} the name {name}.",
                    }
                )
            continue
        if ESCALATION.search(text) and ctx.get("escalated") is not None:
            happened = bool(ctx.get("escalated"))
            ok = happened != negative
            results.append(
                {
                    "id": cid,
                    "verdict": "pass" if ok else "fail",
                    "quote": "",
                    "why": "The AI handed the conversation to a person."
                    if happened
                    else "The AI answered without handing over.",
                }
            )
            continue
        keys = _key_terms(text)
        if not keys:
            results.append(
                {"id": cid, "verdict": "unclear", "quote": "", "why": "The criterion has no words to check."}
            )
            continue
        need = 1 if negative else max(1, (len(keys) + 1) // 2)
        found = ""
        for reply in ours:
            for s in _sentences(reply):
                if len(keys & terms(s)) >= need and not (negative and NEGATION.search(s)):
                    found = s
                    break
            if found:
                break
        if negative:
            results.append(
                {
                    "id": cid,
                    "verdict": "fail" if found else "pass",
                    "quote": found,
                    "why": "A reply does what the criterion forbids." if found else "No reply does this.",
                }
            )
        else:
            results.append(
                {
                    "id": cid,
                    "verdict": "pass" if found else "fail",
                    "quote": found,
                    "why": "A reply does this." if found else "No reply does this.",
                }
            )
    return ModelOutput(data={"results": results}, reason="Simulated judge: word matching against each criterion.")


def simulated_article(ctx: dict) -> ModelOutput:
    questions = [q for q in ctx.get("questions") or [] if q][:20]
    common: dict[str, int] = {}
    for q in questions:
        for t in terms(q):
            common[t] = common.get(t, 0) + 1
    top = [t for t, _ in sorted(common.items(), key=lambda x: (-x[1], x[0]))[:3]]
    title = ("About " + ", ".join(top)) if top else "Answers customers asked for"
    lines = ["Customers asked:"] + [f"- {q}" for q in questions]
    lines += ["", "[Check: write the answer here, from the business's own policy or price list.]"]
    return ModelOutput(
        data={"title": title[:200], "body": "\n".join(lines)},
        reason="Simulated draft: the questions, with a placeholder for the answer. It invents nothing.",
    )


def _norm(s: str) -> str:
    return " ".join((s or "").split()).lower()


def verify_quotes(results: list[dict], conversation: list[dict]) -> list[dict]:
    """Keep a quote only when it is really in the conversation; mark the rest."""
    corpus = [_norm(m.get("text", "")) for m in conversation]
    out = []
    for r in results:
        q = str(r.get("quote") or "").strip()[:500]
        verified = bool(q) and any(_norm(q) in c for c in corpus)
        out.append(
            {
                "id": str(r.get("id")),
                "verdict": r.get("verdict") if r.get("verdict") in ("pass", "fail", "unclear") else "unclear",
                "quote": q if verified else "",
                "quote_dropped": bool(q) and not verified,
                "why": str(r.get("why") or "")[:300],
            }
        )
    return out


def has_placeholders(text: str) -> bool:
    return PLACEHOLDER.search(text or "") is not None
