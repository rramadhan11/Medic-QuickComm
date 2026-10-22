"""Travel-time estimates fitted on historical deliveries.

Two layers:
1. DistanceEtaModel: `minutes = a + b * km`, a least-squares line over all deliveries.
   Short urban trips carry a large fixed overhead, so a line beats minutes-per-km.
2. TimeAwareEtaModel: the baseline times a traffic multiplier for the dispatch period
   (e.g. Surabaya rush hour, Malang weekend traffic). The multiplier is the median of
   actual/baseline over past deliveries, taken from
     a) the same warehouse in the same period, else
     b) warehouses on the same route type (city vs intercity) in that period, else
     c) nothing: 1.0, the plain distance baseline.
   (b) never mixes route types: city rush hour slows only the urban leg, and city
   weekends are light while intercity weekends are heavy, so borrowing across route
   types gives wrong answers (e.g. a 150 km trip "faster at weekends").
   Every estimate states its source and sample count; the history is only 38 rows.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import StrEnum
from statistics import linear_regression, median

# Delivery history is recorded in local time; WIB has no daylight saving.
WIB = timezone(timedelta(hours=7), "WIB")
PEAK_HOURS = ((time(7), time(9)), (time(16), time(19)))
# Warehouses in the data sit at <= 30 km (Surabaya metro) or >= 45 km (intercity).
CITY_RADIUS_KM = 40


@dataclass(frozen=True)
class DistanceEtaModel:
    intercept_minutes: float
    minutes_per_km: float

    @classmethod
    def fit(cls, samples: Sequence[tuple[float, float]]) -> "DistanceEtaModel":
        """samples: (distance_km, duration_minutes) pairs; needs two distinct distances."""
        distances = [km for km, _ in samples]
        durations = [minutes for _, minutes in samples]
        slope, intercept = linear_regression(distances, durations)
        return cls(intercept_minutes=intercept, minutes_per_km=slope)

    def estimate(self, distance_km: float) -> float:
        return self.intercept_minutes + self.minutes_per_km * distance_km


class RouteType(StrEnum):
    CITY = "CITY"
    INTERCITY = "INTERCITY"

    @classmethod
    def of(cls, distance_km: float) -> "RouteType":
        return cls.CITY if distance_km <= CITY_RADIUS_KM else cls.INTERCITY


class Period(StrEnum):
    WEEKDAY_PEAK = "WEEKDAY_PEAK"  # Mon–Thu, 07–09 / 16–19
    WEEKDAY_OFF_PEAK = "WEEKDAY_OFF_PEAK"
    FRIDAY_PEAK = "FRIDAY_PEAK"  # worst city traffic, start of weekend travel
    FRIDAY_OFF_PEAK = "FRIDAY_OFF_PEAK"
    WEEKEND = "WEEKEND"  # no commute peak; out-of-town routes get busier

    @property
    def label(self) -> str:
        return {
            Period.WEEKDAY_PEAK: "weekday rush hour",
            Period.WEEKDAY_OFF_PEAK: "weekday off-peak",
            Period.FRIDAY_PEAK: "Friday rush hour",
            Period.FRIDAY_OFF_PEAK: "Friday off-peak",
            Period.WEEKEND: "weekend",
        }[self]


def period_of(day_of_week: int, clock: time) -> Period:
    """day_of_week: 0 = Monday, local (WIB) wall-clock time."""
    if day_of_week >= 5:
        return Period.WEEKEND
    peak = any(start <= clock < end for start, end in PEAK_HOURS)
    if day_of_week == 4:
        return Period.FRIDAY_PEAK if peak else Period.FRIDAY_OFF_PEAK
    return Period.WEEKDAY_PEAK if peak else Period.WEEKDAY_OFF_PEAK


def local_period(moment: datetime) -> Period:
    local = moment.astimezone(WIB)
    return period_of(local.weekday(), local.time())


@dataclass(frozen=True)
class DeliverySample:
    warehouse_id: str
    distance_km: float
    day_of_week: int
    dispatch_time: time
    duration_minutes: float


class EtaSource(StrEnum):
    WAREHOUSE = "WAREHOUSE"  # this warehouse's own deliveries in the period
    ROUTE_TYPE = "ROUTE_TYPE"  # borrowed from warehouses on the same route type
    DISTANCE = "DISTANCE"  # no history for the period: distance baseline only


@dataclass(frozen=True)
class EtaEstimate:
    minutes: float
    period: Period
    source: EtaSource
    samples: int  # past deliveries behind the multiplier; 0 for DISTANCE
    basis: str


@dataclass(frozen=True)
class TimeAwareEtaModel:
    baseline: DistanceEtaModel
    own: dict[tuple[str, Period], tuple[float, ...]]  # traffic ratios per warehouse+period
    pooled: dict[tuple[RouteType, Period], tuple[float, ...]]

    @classmethod
    def fit(cls, samples: Sequence[DeliverySample]) -> "TimeAwareEtaModel":
        baseline = DistanceEtaModel.fit([(s.distance_km, s.duration_minutes) for s in samples])
        own: dict[tuple[str, Period], list[float]] = defaultdict(list)
        pooled: dict[tuple[RouteType, Period], list[float]] = defaultdict(list)
        for s in samples:
            ratio = s.duration_minutes / baseline.estimate(s.distance_km)
            period = period_of(s.day_of_week, s.dispatch_time)
            own[(s.warehouse_id, period)].append(ratio)
            pooled[(RouteType.of(s.distance_km), period)].append(ratio)
        return cls(
            baseline=baseline,
            own={k: tuple(v) for k, v in own.items()},
            pooled={k: tuple(v) for k, v in pooled.items()},
        )

    def estimate(self, warehouse_id: str, distance_km: float, dispatch_at: datetime) -> EtaEstimate:
        return self.estimate_for_period(warehouse_id, distance_km, local_period(dispatch_at))

    def estimate_for_period(
        self, warehouse_id: str, distance_km: float, period: Period
    ) -> EtaEstimate:
        base = self.baseline.estimate(distance_km)
        if ratios := self.own.get((warehouse_id, period)):
            source, n = EtaSource.WAREHOUSE, len(ratios)
            basis = (
                f"{period.label}, from {n} past {warehouse_id} deliver{'y' if n == 1 else 'ies'}"
            )
        elif ratios := self.pooled.get((route := RouteType.of(distance_km), period)):
            source, n = EtaSource.ROUTE_TYPE, len(ratios)
            basis = (
                f"{period.label}, no {warehouse_id} history: "
                f"typical for {route.lower()} routes ({n} deliveries)"
            )
        else:
            return EtaEstimate(
                base, period, EtaSource.DISTANCE, 0, f"{period.label}, no history: distance only"
            )
        return EtaEstimate(base * median(ratios), period, source, n, basis)
