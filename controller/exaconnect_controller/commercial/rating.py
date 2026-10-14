"""The rating engine (ADR 0055): turns a quantity and a price into money, and
shows its working. Pure functions, unit-tested with hand-worked examples.

A price has a model:

* ``per_unit``: every billable unit at ``unit_price``;
* ``flat``: ``unit_price`` once, whatever the quantity (zero usage costs nothing);
* ``tiered`` (graduated): each band's units at that band's price, like income tax;
* ``volume``: every billable unit at the price of the band the total falls in.

``included_qty`` units are free before any model applies, and ``minimum`` is the
least a charge can be once anything is billable. Term discounts reward longer
contracts; a person's discount is on top of that, so 5% and 10% make 14.5%,
never 15%. Amounts keep four decimal places here; invoices round each line to
the cent.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ..billing.core import BillingError, dec

MODELS = ("per_unit", "flat", "tiered", "volume")
MAX_TIERS = 10
FOUR = Decimal("0.0001")
HUNDRED = Decimal(100)


def q4(v: Decimal) -> Decimal:
    return v.quantize(FOUR, rounding=ROUND_HALF_UP)


def _text(v: Decimal) -> str:
    s = f"{v.normalize():f}"
    return s


def _pct(v: Any, what: str) -> Decimal:
    d = dec(v)
    if not (0 <= d <= 100):
        raise BillingError(f"The {what} must be between 0% and 100%.")
    return d


def validate_price(raw: dict) -> dict:
    """Check a price and return it in a standard shape (Decimals as strings,
    tiers ascending with the open band last)."""
    if not isinstance(raw, dict):
        raise BillingError("A price needs a model and amounts.")
    model = raw.get("model") or "per_unit"
    if model not in MODELS:
        raise BillingError("Choose a pricing model: per unit, flat, tiered or volume.")
    unit_price = dec(raw.get("unit_price") or 0)
    included = dec(raw.get("included_qty") or 0)
    minimum = dec(raw.get("minimum") or 0)
    for v, what in ((unit_price, "price"), (included, "included quantity"), (minimum, "minimum charge")):
        if v < 0:
            raise BillingError(f"The {what} can't be negative.")
    tiers: list[dict] = []
    if model in ("tiered", "volume"):
        rows = raw.get("tiers") or []
        if not isinstance(rows, list) or not rows:
            raise BillingError("A tiered or volume price needs at least one band.")
        if len(rows) > MAX_TIERS:
            raise BillingError(f"Keep a price to {MAX_TIERS} bands or fewer.")
        last: Decimal | None = Decimal(0)
        for i, r in enumerate(rows):
            if not isinstance(r, dict):
                raise BillingError("Each band needs up_to and unit_price.")
            price = dec(r.get("unit_price") if r.get("unit_price") is not None else 0)
            if price < 0:
                raise BillingError("A band's price can't be negative.")
            up_to = r.get("up_to")
            if up_to in (None, ""):
                if i != len(rows) - 1:
                    raise BillingError("Only the last band can be open-ended.")
                tiers.append({"up_to": None, "unit_price": _text(price)})
                continue
            top = dec(up_to)
            if last is None or top <= last:
                raise BillingError("Bands must go up: each band's 'up to' must be above the one before.")
            tiers.append({"up_to": _text(top), "unit_price": _text(price)})
            last = top
        if tiers[-1]["up_to"] is not None:
            raise BillingError("The last band must be open-ended (leave 'up to' empty).")
    elif raw.get("tiers"):
        raise BillingError("Only tiered and volume prices have bands.")
    discounts: list[dict] = []
    seen = set()
    for r in raw.get("term_discounts") or []:
        if not isinstance(r, dict):
            raise BillingError("Each term discount needs min_months and pct.")
        months = int(dec(r.get("min_months")))
        if not (1 <= months <= 120):
            raise BillingError("A term discount starts between 1 and 120 months.")
        if months in seen:
            raise BillingError(f"There are two term discounts for {months} months.")
        seen.add(months)
        discounts.append({"min_months": months, "pct": _text(_pct(r.get("pct"), "term discount"))})
    discounts.sort(key=lambda r: r["min_months"])
    for a, b in zip(discounts, discounts[1:], strict=False):
        if dec(b["pct"]) < dec(a["pct"]):
            raise BillingError("A longer term must never get a smaller discount.")
    return {
        "model": model,
        "unit_price": _text(unit_price),
        "tiers": tiers,
        "included_qty": _text(included),
        "minimum": _text(minimum),
        "term_discounts": discounts,
    }


def rate(price: dict, quantity: Any) -> dict:
    """The charge for a quantity, before discounts, with every band shown."""
    qty = dec(quantity)
    if qty < 0:
        raise BillingError("A quantity can't be negative.")
    model = price.get("model", "per_unit")
    included = dec(price.get("included_qty") or 0)
    billable = max(Decimal(0), qty - included)
    bands: list[dict] = []
    if model == "flat":
        amount = dec(price["unit_price"]) if billable > 0 else Decimal(0)
        if billable > 0:
            bands.append(
                {
                    "from": "0",
                    "to": None,
                    "quantity": _text(billable),
                    "unit_price": None,
                    "amount": _text(q4(amount)),
                    "flat": True,
                }
            )
    elif model == "per_unit":
        unit = dec(price["unit_price"])
        amount = billable * unit
        if billable > 0:
            bands.append(
                {
                    "from": "0",
                    "to": None,
                    "quantity": _text(billable),
                    "unit_price": _text(unit),
                    "amount": _text(q4(amount)),
                }
            )
    elif model == "tiered":
        amount, floor = Decimal(0), Decimal(0)
        for t in price["tiers"]:
            if billable <= floor:
                break
            top = dec(t["up_to"]) if t["up_to"] is not None else None
            in_band = (min(billable, top) if top is not None else billable) - floor
            unit = dec(t["unit_price"])
            amount += in_band * unit
            bands.append(
                {
                    "from": _text(floor),
                    "to": t["up_to"],
                    "quantity": _text(in_band),
                    "unit_price": _text(unit),
                    "amount": _text(q4(in_band * unit)),
                }
            )
            if top is None:
                break
            floor = top
    elif model == "volume":
        amount = Decimal(0)
        floor = Decimal(0)
        for t in price["tiers"]:
            top = dec(t["up_to"]) if t["up_to"] is not None else None
            if top is None or billable <= top:
                unit = dec(t["unit_price"])
                amount = billable * unit
                if billable > 0:
                    bands.append(
                        {
                            "from": _text(floor),
                            "to": t["up_to"],
                            "quantity": _text(billable),
                            "unit_price": _text(unit),
                            "amount": _text(q4(amount)),
                            "volume": True,
                        }
                    )
                break
            floor = top
    else:
        raise BillingError("Choose a pricing model: per unit, flat, tiered or volume.")
    minimum = dec(price.get("minimum") or 0)
    minimum_applied = billable > 0 and amount < minimum
    if minimum_applied:
        amount = minimum
    amount = q4(amount)
    return {
        "model": model,
        "quantity": _text(qty),
        "included": _text(min(qty, included)),
        "billable": _text(billable),
        "bands": bands,
        "minimum_applied": minimum_applied,
        "amount": amount,
        "effective_unit_price": q4(amount / billable) if billable > 0 else Decimal(0),
    }


def term_discount_pct(price: dict, term_months: int) -> Decimal:
    """The largest term discount the contract length earns."""
    best = Decimal(0)
    for r in price.get("term_discounts") or []:
        if term_months >= int(r["min_months"]):
            best = max(best, dec(r["pct"]))
    return best


def combined_pct(*pcts: Any) -> Decimal:
    """Discounts applied one after another: 5% then 10% is 14.5%."""
    keep = Decimal(1)
    for p in pcts:
        keep *= 1 - _pct(p, "discount") / HUNDRED
    return q4((1 - keep) * HUNDRED)


def discounted(amount: Decimal, *pcts: Any) -> Decimal:
    return q4(amount * (1 - combined_pct(*pcts) / HUNDRED))


def needs_approval(manual_pct: Any, max_pct: Any) -> bool:
    """A person's discount above what the item allows needs an ExaCarib admin."""
    return dec(manual_pct) > dec(max_pct)


def describe(price: dict, unit: str = "unit", currency: str = "USD") -> str:
    """A short plain-English summary of a price, for screens and quote documents."""
    model = price.get("model", "per_unit")
    inc = dec(price.get("included_qty") or 0)
    parts: list[str] = []
    if model == "per_unit":
        parts.append(f"{currency} {_text(dec(price['unit_price']))} per {unit}")
    elif model == "flat":
        parts.append(f"{currency} {_text(dec(price['unit_price']))} flat")
    else:
        bands, lo = [], Decimal(0)
        for t in price["tiers"]:
            hi = t["up_to"]
            rng = f"{_text(lo + 1) if lo else '1'}-{hi}" if hi is not None else f"{_text(lo + 1)}+"
            bands.append(f"{rng}: {currency} {t['unit_price']}")
            lo = dec(hi) if hi is not None else lo
        kind = "each band at its price" if model == "tiered" else "all at the band reached"
        parts.append(f"per {unit}, {kind} ({'; '.join(bands)})")
    if inc > 0:
        parts.append(f"first {_text(inc)} included")
    if dec(price.get("minimum") or 0) > 0:
        parts.append(f"minimum {currency} {_text(dec(price['minimum']))}")
    return ", ".join(parts)
