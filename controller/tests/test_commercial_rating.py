"""The rating engine against hand-worked examples (ADR 0055)."""

from decimal import Decimal

import pytest

from exaconnect_controller.billing.core import BillingError
from exaconnect_controller.commercial import rating

SEAT_BANDS = [
    {"up_to": 10, "unit_price": "25"},
    {"up_to": 50, "unit_price": "20"},
    {"up_to": None, "unit_price": "15"},
]


def price(**kw):
    return rating.validate_price(kw)


def test_per_unit_with_included_hours():
    # 5 engineer hours, 2 included, USD 85 an hour: 3 x 85 = 255.
    p = price(model="per_unit", unit_price="85", included_qty="2")
    r = rating.rate(p, 5)
    assert r["amount"] == Decimal("255.0000")
    assert r["included"] == "2" and r["billable"] == "3"
    assert r["bands"] == [{"from": "0", "to": None, "quantity": "3", "unit_price": "85", "amount": "255"}]
    # Inside the allowance costs nothing.
    assert rating.rate(p, 1)["amount"] == 0
    assert rating.rate(p, 2)["billable"] == "0"


def test_volume_prices_every_unit_at_the_band_reached():
    p = price(model="volume", tiers=SEAT_BANDS)
    assert rating.rate(p, 10)["amount"] == Decimal("250.0000")  # 10 x 25
    assert rating.rate(p, 12)["amount"] == Decimal("240.0000")  # 12 x 20: more seats, cheaper each
    assert rating.rate(p, 60)["amount"] == Decimal("900.0000")  # 60 x 15
    assert rating.rate(p, 12)["effective_unit_price"] == Decimal("20.0000")


def test_tiered_prices_each_band_at_its_own_price():
    p = price(model="tiered", tiers=SEAT_BANDS)
    # 12: 10 x 25 + 2 x 20 = 290.
    r = rating.rate(p, 12)
    assert r["amount"] == Decimal("290.0000")
    assert [(b["quantity"], b["unit_price"], b["amount"]) for b in r["bands"]] == [
        ("10", "25", "250"),
        ("2", "20", "40"),
    ]
    # 60: 10 x 25 + 40 x 20 + 10 x 15 = 250 + 800 + 150 = 1,200.
    assert rating.rate(p, 60)["amount"] == Decimal("1200.0000")
    # Fractions: 10.5 = 250 + 0.5 x 20 = 260.
    assert rating.rate(p, "10.5")["amount"] == Decimal("260.0000")


def test_flat_and_minimum():
    flat = price(model="flat", unit_price="500")
    assert rating.rate(flat, 3)["amount"] == Decimal("500.0000")
    assert rating.rate(flat, 0)["amount"] == 0
    # 2 x 1.50 = 3.00, below the 10.00 minimum.
    p = price(model="per_unit", unit_price="1.5", minimum="10")
    r = rating.rate(p, 2)
    assert r["amount"] == Decimal("10.0000") and r["minimum_applied"] is True
    # No usage, no minimum.
    assert rating.rate(p, 0)["amount"] == 0
    assert rating.rate(p, 10)["amount"] == Decimal("15.0000")


def test_rounds_to_four_places_half_up():
    p = price(model="per_unit", unit_price="0.333333")
    assert rating.rate(p, "1.5")["amount"] == Decimal("0.5000")  # 0.4999995
    assert rating.rate(price(model="per_unit", unit_price="0.00005"), 1)["amount"] == Decimal("0.0001")


def test_term_and_manual_discounts_combine():
    p = price(
        model="per_unit", unit_price="120", term_discounts=[{"min_months": 36, "pct": 10}, {"min_months": 12, "pct": 5}]
    )
    assert p["term_discounts"] == [{"min_months": 12, "pct": "5"}, {"min_months": 36, "pct": "10"}]
    assert rating.term_discount_pct(p, 6) == 0
    assert rating.term_discount_pct(p, 12) == 5
    assert rating.term_discount_pct(p, 24) == 5
    assert rating.term_discount_pct(p, 36) == 10
    # 5% then 10% leaves 0.95 x 0.90 = 85.5% of the price: 14.5% off.
    assert rating.combined_pct(5, 10) == Decimal("14.5000")
    assert rating.discounted(Decimal(1200), 5, 10) == Decimal("1026.0000")
    assert rating.discounted(Decimal(100)) == Decimal("100.0000")
    assert rating.needs_approval(15, 10) and not rating.needs_approval(10, 10)


@pytest.mark.parametrize(
    "bad",
    [
        {"model": "sometimes"},
        {"model": "per_unit", "unit_price": "-1"},
        {"model": "tiered", "tiers": []},
        {"model": "tiered", "tiers": [{"up_to": 10, "unit_price": 1}]},  # last band must be open
        {"model": "tiered", "tiers": [{"up_to": None, "unit_price": 1}, {"up_to": 10, "unit_price": 1}]},
        {
            "model": "volume",
            "tiers": [{"up_to": 10, "unit_price": 1}, {"up_to": 5, "unit_price": 1}, {"up_to": None, "unit_price": 1}],
        },
        {"model": "per_unit", "tiers": [{"up_to": None, "unit_price": 1}]},
        {"model": "per_unit", "term_discounts": [{"min_months": 12, "pct": 10}, {"min_months": 24, "pct": 5}]},
        {"model": "per_unit", "term_discounts": [{"min_months": 12, "pct": 110}]},
        {"model": "per_unit", "unit_price": "abc"},
    ],
)
def test_bad_prices_are_refused(bad):
    with pytest.raises(BillingError):
        rating.validate_price(bad)


def test_describe():
    assert rating.describe(price(model="per_unit", unit_price="85", included_qty="2"), "hour") == (
        "USD 85 per hour, first 2 included"
    )
    assert rating.describe(price(model="volume", tiers=SEAT_BANDS), "seat") == (
        "per seat, all at the band reached (1-10: USD 25; 11-50: USD 20; 51+: USD 15)"
    )
