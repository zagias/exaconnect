"""Metering: hand-worked 95th percentile, 5-minute rollups and settlement."""

import datetime as dt
from decimal import Decimal

import pytest

from exaconnect_controller.metering.core import (
    Counter,
    Sample,
    discarded,
    five_minute_samples,
    month_bounds,
    percentile95,
    settle,
)

T0 = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.UTC)


def test_percentile95_hand_worked_20_samples():
    # 20 samples 1..20 Mbps. 5% of 20 = 1 discarded (the 20); the highest remaining is 19.
    assert discarded(20) == 1
    assert percentile95([float(i) for i in range(20, 0, -1)]) == 19.0


def test_percentile95_hand_worked_month():
    # A 30-day month has 8,640 five-minute samples. 5% = 432 discarded.
    # 8,000 samples at 10 Mbps, 208 at 40, 432 at 90 (the busiest 36 hours):
    # all 432 at 90 are discarded, so the billable rate is 40.
    vals = [10.0] * 8000 + [40.0] * 208 + [90.0] * 432
    assert len(vals) == 8640 and discarded(8640) == 432
    assert percentile95(vals) == 40.0
    # One more sample at 90 and the 95th percentile becomes 90.
    assert percentile95([10.0] * 8000 + [40.0] * 207 + [90.0] * 433) == 90.0


def test_percentile95_small_and_empty():
    assert discarded(19) == 0  # 0.95 rounds down: nothing discarded
    assert percentile95([5.0, 1.0, 3.0]) == 5.0
    assert percentile95([]) is None


def test_five_minute_samples_from_counters():
    # 60 s readings; 75 MB received and 37.5 MB sent per minute = 10 Mbps in, 5 Mbps out.
    cs = [Counter(T0 + dt.timedelta(minutes=m), m * 75_000_000, m * 37_500_000) for m in range(11)]
    s = five_minute_samples(cs)
    assert [x.bucket for x in s] == [T0, T0 + dt.timedelta(minutes=5)]
    assert all(x.in_mbps == pytest.approx(10.0) and x.out_mbps == pytest.approx(5.0) for x in s)
    assert s[0].seconds == 300


def test_samples_split_across_buckets_and_skip_resets():
    # One reading straddles a bucket boundary; a counter reset is dropped.
    cs = [
        Counter(T0 + dt.timedelta(minutes=4), 0, 0),
        Counter(T0 + dt.timedelta(minutes=6), 150_000_000, 0),  # 10 Mbps over 2 min, half in each bucket
        Counter(T0 + dt.timedelta(minutes=7), 10, 0),  # reset
        Counter(T0 + dt.timedelta(minutes=8), 75_000_010, 0),
    ]
    s = {x.bucket: x for x in five_minute_samples(cs)}
    assert s[T0].in_mbps == pytest.approx(10.0) and s[T0].seconds == 60
    later = s[T0 + dt.timedelta(minutes=5)]
    assert later.in_mbps == pytest.approx(10.0) and later.seconds == 120


def test_pop_side_swaps_direction():
    cs = [Counter(T0, 0, 0), Counter(T0 + dt.timedelta(minutes=1), 75_000_000, 0)]
    s = five_minute_samples(cs, rx_is_in=False)
    assert s[0].in_mbps == 0 and s[0].out_mbps == pytest.approx(10.0)


def test_settlement_hand_worked():
    # Carrier A: commit 100 Mbps at 4.00 per Mbps, burst at 6.00 per Mbps.
    # 20 samples, in 50..145 step 5, out lower. Discard 1 (145): billable 140.
    samples = [Sample(T0 + dt.timedelta(minutes=5 * i), 50.0 + 5 * i, 20.0, 300) for i in range(20)]
    st = settle(samples, 100, 4.0, 6.0)
    assert st.p95_in == 140.0 and st.p95_out == 20.0 and st.billable_mbps == 140.0
    assert st.commit_charge == Decimal("400.00")
    assert st.burst_mbps == Decimal("40.000")
    assert st.burst_charge == Decimal("240.00")
    assert st.total == Decimal("640.00")


def test_settlement_within_commit_uses_higher_direction():
    samples = [Sample(T0, 10.0, 30.0, 300), Sample(T0, 12.0, 35.0, 300)]
    st = settle(samples, 50, 2.5, 4.0)
    assert st.billable_mbps == 35.0 and st.burst_charge == Decimal("0.00") and st.total == Decimal("125.00")


def test_month_bounds():
    s, e = month_bounds(dt.datetime(2026, 12, 15, tzinfo=dt.UTC))
    assert s == dt.datetime(2026, 12, 1, tzinfo=dt.UTC) and e == dt.datetime(2027, 1, 1, tzinfo=dt.UTC)
