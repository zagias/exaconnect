"""Bill warnings must use the same per-direction discard rule as settlement."""

import datetime as dt

from exaconnect_controller.ai import billshock
from exaconnect_controller.metering.core import Sample, settle

START = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)
END = dt.datetime(2026, 12, 1, tzinfo=dt.UTC)
PERIOD = (START, END)


def samples(values):
    return [
        Sample(START + dt.timedelta(minutes=5 * i), incoming, outgoing, 300)
        for i, (incoming, outgoing) in enumerate(values)
    ]


def test_alternating_direction_bursts_do_not_create_a_locked_in_bill():
    # Each direction has fewer than the month's 432 discarded peaks, but the
    # union has 800. Treating that union as one series produces a false floor.
    rows = samples([(100, 10)] * 400 + [(10, 100)] * 400 + [(10, 10)] * (8640 - 800))
    billed = settle(rows, 20, 1, 1)
    forecast = billshock.forecast_link(rows, 20, PERIOD)

    assert billed.billable_mbps == 10
    assert forecast is not None
    assert forecast.observed_p95 == billed.billable_mbps
    assert forecast.floor_mbps == billed.billable_mbps
    assert forecast.forecast_mbps == 10
    assert billshock.assess(forecast, "Example link", 20, 1) is not None
    assert billshock.assess(forecast, "Example link", 20, 1)["severity"] == "info"


def test_real_directional_burst_still_warns_and_matches_the_bill():
    rows = samples([(10, 100)] * 500 + [(10, 10)] * (8640 - 500))
    billed = settle(rows, 20, 1, 1)
    forecast = billshock.forecast_link(rows, 20, PERIOD)

    assert forecast is not None
    assert forecast.floor_mbps == billed.billable_mbps == 100
    assert forecast.forecast_mbps >= forecast.floor_mbps
    assert forecast.direction == "outbound"
    alert = billshock.assess(forecast, "Example link", 20, 1)
    assert alert["severity"] == "critical"
    assert alert["data"]["burst_charge"] == "80.00"


def test_observed_percentile_uses_settlement_discard_boundary():
    # Exactly one of 20 peaks is discarded. Interpolation would incorrectly
    # report 14.5 Mbps instead of settlement's 10 Mbps.
    rows = samples([(100, 10)] + [(10, 10)] * 19)
    billed = settle(rows, 20, 1, 1)
    forecast = billshock.forecast_link(rows, 20, PERIOD)

    assert forecast is not None
    assert forecast.observed_p95 == billed.billable_mbps == 10
    assert forecast.forecast_mbps == 10
    assert forecast.floor_mbps == 0


def test_link_forecast_waits_for_a_full_hour_of_samples():
    assert billshock.forecast_link(samples([(10, 10)] * 11), 20, PERIOD) is None
