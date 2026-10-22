from datetime import datetime, time

import pytest

from app.engine.eta import (
    WIB,
    DeliverySample,
    EtaSource,
    Period,
    RouteType,
    TimeAwareEtaModel,
    local_period,
    period_of,
)
from app.seed import WAREHOUSES, load_delivery_history

KM = {wid: km for wid, _, km, *_ in WAREHOUSES}
MODEL = TimeAwareEtaModel.fit(
    [
        DeliverySample(
            r["warehouse_id"],
            KM[r["warehouse_id"]],
            r["day_of_week"],
            r["dispatch_time"],
            r["duration_minutes"],
        )
        for r in load_delivery_history()
    ]
)
MON, FRI, SAT = 0, 4, 5


@pytest.mark.parametrize(
    "day, clock, expected",
    [
        (MON, time(6, 59), Period.WEEKDAY_OFF_PEAK),
        (MON, time(7, 0), Period.WEEKDAY_PEAK),
        (MON, time(8, 59), Period.WEEKDAY_PEAK),
        (MON, time(9, 0), Period.WEEKDAY_OFF_PEAK),
        (MON, time(16, 0), Period.WEEKDAY_PEAK),
        (MON, time(19, 0), Period.WEEKDAY_OFF_PEAK),
        (FRI, time(17, 30), Period.FRIDAY_PEAK),
        (FRI, time(14, 0), Period.FRIDAY_OFF_PEAK),
        (SAT, time(8, 0), Period.WEEKEND),
    ],
)
def test_periods(day, clock, expected):
    assert period_of(day, clock) is expected


def test_period_uses_wib_wall_clock():
    # 10:30 UTC on a Friday is 17:30 in Surabaya.
    assert local_period(datetime.fromisoformat("2026-10-23T10:30:00+00:00")) is Period.FRIDAY_PEAK


def test_route_types_split_at_city_radius():
    assert {w for w, km in KM.items() if RouteType.of(km) is RouteType.CITY} == {
        "W01",
        "W02",
        "W03",
        "W04",
    }


def test_own_history_reproduces_observed_durations():
    eta = MODEL.estimate("W02", KM["W02"], datetime(2026, 10, 23, 17, 30, tzinfo=WIB))

    assert round(eta.minutes) == 100  # the one Friday-rush W02 delivery took 100 min
    assert eta.samples == 1
    assert eta.basis == "Friday rush hour, from 1 past W02 delivery"


def test_city_rush_hour_is_several_times_slower_than_off_peak():
    rush = MODEL.estimate_for_period("W01", KM["W01"], Period.WEEKDAY_PEAK)
    calm = MODEL.estimate_for_period("W01", KM["W01"], Period.WEEKDAY_OFF_PEAK)

    assert rush.minutes > 3 * calm.minutes


def test_malang_is_slower_at_weekends():
    weekend = MODEL.estimate_for_period("W05", KM["W05"], Period.WEEKEND)
    weekday = MODEL.estimate_for_period("W05", KM["W05"], Period.WEEKDAY_OFF_PEAK)

    assert weekend.minutes > 1.3 * weekday.minutes


def test_missing_history_borrows_only_from_the_same_route_type():
    # W08 has no weekend history; it must borrow the (slower) intercity weekend
    # pattern, never the light city weekend.
    weekend = MODEL.estimate_for_period("W08", KM["W08"], Period.WEEKEND)
    weekday = MODEL.estimate_for_period("W08", KM["W08"], Period.WEEKDAY_OFF_PEAK)

    assert "typical for intercity routes" in weekend.basis
    assert weekend.minutes > weekday.minutes


def test_no_history_for_route_type_falls_back_to_distance_baseline():
    # No intercity delivery was ever dispatched in weekday rush hour.
    eta = MODEL.estimate_for_period("W08", KM["W08"], Period.WEEKDAY_PEAK)

    assert eta.source is EtaSource.DISTANCE
    assert eta.samples == 0
    assert eta.minutes == MODEL.baseline.estimate(KM["W08"])
    assert eta.basis == "weekday rush hour, no history: distance only"
