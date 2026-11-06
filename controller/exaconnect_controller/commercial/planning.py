"""Deterministic planning intake and design attestation; no model or provider calls."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import uuid
from decimal import Decimal

from pydantic import ValidationError

from ..ai.brief import PlanningBrief
from ..billing.core import BillingError, dec
from . import catalogue, rating

# Keep the pure secure-entry predicate local: importing the Jibsy assistant
# bootstraps the API package and creates a commercial-module import cycle.
SECRET_HINT = re.compile(
    r"(?i)\b(password|passcode|api[_ -]?key|(?:client[_ -]?)?secret|(?:access[_ -]?|refresh[_ -]?)?token)"
    r"\b[\"']?\s*(is\b|=|:)\s*\S+|"
    r"\b(?:authorization\s*:\s*)?(?:bearer|basic)\s+[A-Za-z0-9_+/=.-]{8,}|"
    r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----|"
    r"[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@|"
    r"\b(sk|pk|pat|xox[bp])-[A-Za-z0-9-]{8,}|"
    r"\bEAA[A-Za-z0-9]{20,}|\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Z])(?=[A-Za-z0-9]*[a-z])[A-Za-z0-9]{32,}\b"
)


def clean_source(value) -> dict:
    try:
        brief = value if isinstance(value, PlanningBrief) else PlanningBrief.model_validate(value)
    except ValidationError as exc:
        raise BillingError("The planning brief is invalid. Import a reviewed planning brief.") from exc
    if any(
        SECRET_HINT.search(text)
        or re.search(r"(?i)\b(?:\d[ -]?){13,19}\b|\bprivate[_ -]?key\s*(?:is\b|[:=])\s*\S+", text)
        for text in brief.texts()
    ):
        raise BillingError("Remove passwords, keys, tokens and payment card details before importing the brief.")
    return brief.model_dump()


def _canonical(value):
    if isinstance(value, Decimal):
        return f"{value.normalize():f}"
    if isinstance(value, dt.date | dt.datetime | uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_canonical(v) for v in value]
    return value


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(_canonical(value), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def request_hash(values: dict, source: dict) -> str:
    """Hash intended input, never a new price, generated date, actor or quote ID."""
    raw = {k: v for k, v in values.items() if k not in ("creation_key", "planning_source")}
    raw["planning_source"] = source
    return digest(raw)


def explicit_line(raw: dict, item: dict) -> None:
    if "quantity" not in raw or raw["quantity"] in (None, "") or isinstance(raw["quantity"], bool):
        raise BillingError("Confirm a quantity for every planning quote line; no quantity is inferred.")
    if dec(raw["quantity"]) <= 0:
        raise BillingError("Confirm a quantity above zero for every planning quote line.")
    config = raw.get("config") or {}
    if not isinstance(config, dict):
        raise BillingError("Confirm the fulfilment details for every planning quote line.")
    if item["fulfilment"] == "link" and not config.get("underlay_type"):
        raise BillingError("Confirm the link type; no fibre connection is inferred from the brief.")


HEAD_FIELDS = ("customer_id", "title", "currency", "term_months", "valid_until", "notes")
LINE_FIELDS = (
    "position",
    "item_id",
    "price_id",
    "code",
    "name",
    "product",
    "charge_type",
    "unit",
    "quantity",
    "config",
    "description",
    "list_amount",
    "term_discount_pct",
    "discount_pct",
    "amount",
    "price",
    "rating",
    "fulfilment",
)


def review_state(q: dict, lines: list[dict]) -> dict:
    required = q.get("planning_source") is not None
    current = (
        digest(
            {
                "source": q.get("planning_source"),
                "head": {k: q.get(k) for k in HEAD_FIELDS},
                "lines": [{k: x.get(k) for k in LINE_FIELDS} for x in lines],
            }
        )
        if required
        else None
    )
    approved = bool(
        required
        and q.get("design_review_digest") == current
        and q.get("design_reviewed_by")
        and q.get("design_reviewed_at")
        and q.get("design_review_note")
    )
    return {
        "required": required,
        "status": "approved" if approved else "pending",
        "current_digest": current,
        "approved_digest": q.get("design_review_digest"),
        "reviewed_by": q.get("design_reviewed_by"),
        "reviewed_at": q.get("design_reviewed_at"),
        "note": q.get("design_review_note"),
    }


def reviewable(q: dict) -> None:
    if not q.get("lines"):
        raise BillingError("Pricing is pending. Add actual priced catalogue lines before design review.", 409)
    if q.get("example") or any(x.get("price", {}).get("example") for x in q["lines"]):
        raise BillingError("Example prices cannot receive a planning design review.", 409)
    for line in q["lines"]:
        if not line.get("price"):
            raise BillingError("Every line needs an actual price snapshot before design review.", 409)
        config = line["config"]
        if line["fulfilment"] == "site" and len(config.get("locations") or []) != dec(line["quantity"]):
            raise BillingError("Name every billed new site before design review.", 409)
        if line["fulfilment"] == "link" and not all(
            str(config.get(k) or "").strip() for k in ("location", "carrier", "underlay_type")
        ):
            raise BillingError("Confirm the link location, carrier and link type before design review.", 409)


def require_review(q: dict) -> None:
    if q.get("planning_source") is None:
        return
    reviewable(q)
    if q["design_review"]["status"] != "approved":
        raise BillingError("XaKore staff must review the current scope and prices before sending or accepting.", 409)


def options(conn, customer_id, source, currency: str) -> dict:
    source = clean_source(source)
    if conn.execute("SELECT id FROM customers WHERE id = %s", (customer_id,)).fetchone() is None:
        raise BillingError("Customer not found.", 404)
    currency = currency.upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise BillingError("Give the currency as three letters, such as USD.")
    text = " ".join([x["label"] + " " + x["value"] for x in source["facts"]] + source["gaps"]).lower()
    product = {"Connect": "connect", "Jibsy": "commai"}.get(source["offer"])
    telephone_only = product == "commai" and re.search(r"\b(phone|telephone|telephony|sip|pbx|calling|voice)\b", text)
    if telephone_only and not re.search(r"whatsapp|messag|chat|email|social|omnichannel|sms", text):
        product = None
    warnings = [
        "These are catalogue options, not confirmation of supplier availability, service readiness or a final design.",
        "All quantities and fulfilment details must be confirmed; planning notes do not authorise purchases.",
    ]
    questions = list(source["gaps"])
    items = []
    if product is None:
        warnings.append("This scope needs XaKore review; no catalogue product has been inferred.")
    else:
        for item in catalogue.list_items(conn, False, product, currency):
            if item["example"] or not item["active"]:
                continue
            price = item.get("price")
            blocked = None
            try:
                if item["price_source"] != "catalogue":
                    plan = catalogue.plan_for(conn, customer_id, item, dt.datetime.now(dt.UTC).date())
                    price = catalogue.plan_price(conn, customer_id, item, dt.datetime.now(dt.UTC).date())
                    if plan.get("example"):
                        blocked = "The plan is example data. XaKore must agree real pricing."
                if price is None:
                    blocked = f"No current price is available in {currency}."
                elif price.get("example"):
                    blocked = "This price is example data. XaKore must agree real pricing."
                elif price.get("currency") != currency:
                    blocked = f"The organisation price is in {price.get('currency')}, not {currency}."
                if price is not None:
                    rating.validate_price(price)
            except BillingError as exc:
                blocked = str(exc)
            price_text = (
                rating.describe(price, item["unit"], currency)
                if price is not None and not blocked
                else "Pricing pending"
            )
            items.append(
                {
                    **item,
                    "price": price if not blocked else None,
                    "price_text": price_text,
                    "planning_match": {
                        "reason": f"Catalogue option for {source['offer']}; selection requires review.",
                        "price_available": blocked is None,
                        "price_text": price_text,
                        **({"blocked_reason": blocked} if blocked else {}),
                    },
                }
            )
        if not any(x["planning_match"]["price_available"] for x in items):
            warnings.append(
                "No confirmed catalogue price matches this scope and currency. Save a scope draft for review."
            )
    questions.append("Which items, additional quantities and fulfilment details should this draft include?")
    return {
        "customer_id": customer_id,
        "source": source,
        "product": product,
        "items": items,
        "warnings": warnings,
        "questions": questions,
    }
