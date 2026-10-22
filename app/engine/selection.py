"""Warehouse selection: decide which warehouse(s) ship which quantities for an order.

Pure functions over plain data, with no database access, so every rule here is
unit-testable in isolation.

Algorithm
1. Eligibility: a warehouse may ship a product only if it has available stock and,
   for cold-chain products, both refrigerated storage and a refrigerated fleet.
2. Target: each line ships min(requested, total eligible stock); the rest is
   backordered. Every candidate plan reaches the same target, so "fulfil as much
   as possible" is guaranteed before any other criterion is considered.
3. Search: try every subset of candidate warehouses (2^n, n <= MAX_EXACT_WAREHOUSES),
   allocate within it fastest-first, and keep the warehouses actually used.
4. Rank: CITO prefers the earliest last-parcel arrival, then fewer shipments;
   REGULAR prefers fewer shipments, then earliest arrival. Total distance and
   warehouse ids break remaining ties so the result is deterministic.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations

from app.models import StorageCondition, Urgency

# Exhaustive search is exact but exponential; 2^16 subsets is still fast.
# Beyond that, switch to a heuristic or an ILP solver.
MAX_EXACT_WAREHOUSES = 16
MAX_ALTERNATIVES = 3


@dataclass(frozen=True)
class LineRequest:
    product_id: str
    product_name: str
    quantity: int
    storage_condition: StorageCondition


@dataclass(frozen=True)
class WarehouseOption:
    warehouse_id: str
    city: str
    distance_km: float
    eta_minutes: float
    has_cold_storage: bool
    has_cold_fleet: bool
    # Unreserved stock per product; products the warehouse doesn't carry are absent.
    available: Mapping[str, int]
    eta_basis: str = "distance only"  # how eta_minutes was estimated, for the explanation


@dataclass(frozen=True)
class ShipmentItem:
    product_id: str
    quantity: int


@dataclass(frozen=True)
class Shipment:
    warehouse_id: str
    city: str
    distance_km: float
    eta_minutes: float
    eta_basis: str
    items: tuple[ShipmentItem, ...]


@dataclass(frozen=True)
class Backorder:
    product_id: str
    quantity: int
    reason: str


@dataclass(frozen=True)
class Exclusion:
    warehouse_id: str
    product_id: str
    reason: str


@dataclass(frozen=True)
class PlanMetrics:
    shipments: int
    last_arrival_minutes: float
    total_distance_km: float


@dataclass(frozen=True)
class Alternative:
    warehouse_ids: tuple[str, ...]
    metrics: PlanMetrics
    comparison: str  # what it would gain, and why it still lost


@dataclass(frozen=True)
class Plan:
    urgency: Urgency
    shipments: tuple[Shipment, ...]
    backorders: tuple[Backorder, ...]
    exclusions: tuple[Exclusion, ...]
    alternatives: tuple[Alternative, ...]
    metrics: PlanMetrics | None
    summary: str
    reasons: tuple[str, ...]

    @property
    def fully_fulfilled(self) -> bool:
        return not self.backorders


def select_warehouses(
    lines: Sequence[LineRequest],
    warehouses: Sequence[WarehouseOption],
    urgency: Urgency,
) -> Plan:
    if not lines:
        raise ValueError("order has no lines")
    if len({line.product_id for line in lines}) != len(lines):
        raise ValueError("order has duplicate products")
    if any(line.quantity <= 0 for line in lines):
        raise ValueError("quantities must be positive")

    usable, exclusions = _eligible_stock(lines, warehouses)
    targets, backorders = _targets(lines, usable)

    by_id = {w.warehouse_id: w for w in warehouses}
    candidates = sorted(
        (
            by_id[wid]
            for wid, stock in usable.items()
            if any(stock.get(pid, 0) > 0 for pid, qty in targets.items() if qty > 0)
        ),
        key=_speed_order,
    )
    if len(candidates) > MAX_EXACT_WAREHOUSES:
        raise ValueError(
            f"{len(candidates)} candidate warehouses exceeds exact-search limit "
            f"of {MAX_EXACT_WAREHOUSES}"
        )

    names = {line.product_id: line.product_name for line in lines}
    plans = _feasible_plans(candidates, usable, targets)
    if not plans:
        return Plan(
            urgency=urgency,
            shipments=(),
            backorders=backorders,
            exclusions=exclusions,
            alternatives=(),
            metrics=None,
            summary="Nothing can be shipped: no eligible warehouse has stock for this order.",
            reasons=tuple(f"{names[b.product_id]}: {b.reason}." for b in backorders),
        )

    best, *trade_offs = _pareto_front(sorted(plans, key=lambda p: _rank_key(p, urgency)))
    alternatives = tuple(
        Alternative(
            warehouse_ids=tuple(s.warehouse_id for s in alt.shipments),
            metrics=alt.metrics,
            comparison=_compare(best.metrics, alt.metrics, urgency),
        )
        for alt in trade_offs[:MAX_ALTERNATIVES]
    )
    return Plan(
        urgency=urgency,
        shipments=best.shipments,
        backorders=backorders,
        exclusions=exclusions,
        alternatives=alternatives,
        metrics=best.metrics,
        summary=_summary(best, backorders),
        reasons=_reasons(best, alternatives, backorders, exclusions, urgency, names),
    )


def cold_chain_gaps(warehouse: WarehouseOption) -> list[str]:
    gaps = []
    if not warehouse.has_cold_storage:
        gaps.append("refrigerated storage")
    if not warehouse.has_cold_fleet:
        gaps.append("refrigerated delivery fleet")
    return gaps


@dataclass(frozen=True)
class _DraftPlan:
    shipments: tuple[Shipment, ...]
    metrics: PlanMetrics


def _speed_order(w: WarehouseOption) -> tuple[float, float, str]:
    return (w.eta_minutes, w.distance_km, w.warehouse_id)


def _eligible_stock(
    lines: Sequence[LineRequest], warehouses: Sequence[WarehouseOption]
) -> tuple[dict[str, dict[str, int]], tuple[Exclusion, ...]]:
    usable: dict[str, dict[str, int]] = {}
    exclusions = []
    for w in warehouses:
        for line in lines:
            qty = w.available.get(line.product_id)
            if qty is None:
                continue  # doesn't carry it: not worth reporting
            if line.storage_condition is StorageCondition.COLD_2_8C and (
                gaps := cold_chain_gaps(w)
            ):
                exclusions.append(
                    Exclusion(
                        w.warehouse_id,
                        line.product_id,
                        f"cannot keep 2–8 °C to delivery: no {' and no '.join(gaps)}",
                    )
                )
            elif qty <= 0:
                exclusions.append(Exclusion(w.warehouse_id, line.product_id, "no unreserved stock"))
            else:
                usable.setdefault(w.warehouse_id, {})[line.product_id] = qty
    return usable, tuple(exclusions)


def _targets(
    lines: Sequence[LineRequest], usable: Mapping[str, Mapping[str, int]]
) -> tuple[dict[str, int], tuple[Backorder, ...]]:
    targets = {}
    backorders = []
    for line in lines:
        eligible_total = sum(stock.get(line.product_id, 0) for stock in usable.values())
        targets[line.product_id] = min(line.quantity, eligible_total)
        short = line.quantity - targets[line.product_id]
        if short == 0:
            continue
        if eligible_total == 0:
            reason = (
                "no warehouse with a full cold chain has stock"
                if line.storage_condition is StorageCondition.COLD_2_8C
                else "no warehouse has stock"
            )
        else:
            reason = f"only {eligible_total} available across all eligible warehouses"
        backorders.append(Backorder(line.product_id, short, reason))
    return targets, tuple(backorders)


def _feasible_plans(
    candidates: Sequence[WarehouseOption],
    usable: Mapping[str, Mapping[str, int]],
    targets: Mapping[str, int],
) -> list[_DraftPlan]:
    plans: dict[frozenset[str], _DraftPlan] = {}
    for size in range(1, len(candidates) + 1):
        for subset in combinations(candidates, size):  # already in speed order
            plan = _allocate(subset, usable, targets)
            if plan is not None:
                plans.setdefault(frozenset(s.warehouse_id for s in plan.shipments), plan)
    return list(plans.values())


def _allocate(
    subset: Sequence[WarehouseOption],
    usable: Mapping[str, Mapping[str, int]],
    targets: Mapping[str, int],
) -> _DraftPlan | None:
    """Fill each product's target from the fastest warehouses in the subset first.
    Returns None if the subset can't reach every target."""
    items: dict[str, list[ShipmentItem]] = {}
    for pid, target in targets.items():
        remaining = target
        for w in subset:
            if remaining == 0:
                break
            take = min(remaining, usable[w.warehouse_id].get(pid, 0))
            if take:
                items.setdefault(w.warehouse_id, []).append(ShipmentItem(pid, take))
                remaining -= take
        if remaining:
            return None

    shipments = tuple(
        Shipment(
            w.warehouse_id,
            w.city,
            w.distance_km,
            w.eta_minutes,
            w.eta_basis,
            tuple(items[w.warehouse_id]),
        )
        for w in subset
        if w.warehouse_id in items
    )
    return _DraftPlan(
        shipments=shipments,
        metrics=PlanMetrics(
            shipments=len(shipments),
            last_arrival_minutes=max(s.eta_minutes for s in shipments),
            total_distance_km=sum(s.distance_km for s in shipments),
        ),
    )


def _rank_key(plan: _DraftPlan, urgency: Urgency) -> tuple:
    m = plan.metrics
    ids = tuple(sorted(s.warehouse_id for s in plan.shipments))
    if urgency is Urgency.CITO:
        return (m.last_arrival_minutes, m.shipments, m.total_distance_km, ids)
    return (m.shipments, m.last_arrival_minutes, m.total_distance_km, ids)


def _deltas(best: PlanMetrics, alt: PlanMetrics) -> dict[str, float]:
    """Positive = alt is worse than best on that criterion."""
    return {
        "arrival": alt.last_arrival_minutes - best.last_arrival_minutes,
        "shipments": alt.shipments - best.shipments,
        "distance": alt.total_distance_km - best.total_distance_km,
    }


def _improvements(best: PlanMetrics, alt: PlanMetrics) -> list[str]:
    return [k for k, delta in _deltas(best, alt).items() if delta < 0]


def _dominates(a: PlanMetrics, b: PlanMetrics) -> bool:
    """a is at least as good as b on every criterion and strictly better on one."""
    deltas = _deltas(a, b).values()
    return all(d >= 0 for d in deltas) and any(d > 0 for d in deltas)


def _pareto_front(ranked: Sequence[_DraftPlan]) -> list[_DraftPlan]:
    """Drop plans that another plan beats on every criterion (e.g. the winner plus a
    redundant warehouse): they explain nothing. `ranked` is sorted lexicographically
    on the metrics, so any dominating plan comes earlier and is already in the front."""
    front: list[_DraftPlan] = []
    for plan in ranked:
        if not any(_dominates(kept.metrics, plan.metrics) for kept in front):
            front.append(plan)
    return front


def _compare(best: PlanMetrics, alt: PlanMetrics, urgency: Urgency) -> str:
    deltas = _deltas(best, alt)
    better = {
        "arrival": "arrives {:.0f} min sooner",
        "shipments": "uses {:g} fewer shipment(s)",
        "distance": "travels {:g} km less",
    }
    worse = {
        "arrival": "arrives {:.0f} min later",
        "shipments": "needs {:g} more shipment(s)",
        "distance": "travels {:g} km more",
    }
    priority = ["arrival", "shipments"] if urgency is Urgency.CITO else ["shipments", "arrival"]
    gains = [better[k].format(-deltas[k]) for k in _improvements(best, alt)]
    if not any(deltas.values()):
        return "ties on every criterion; lost on warehouse id order"
    loss = next(worse[k].format(deltas[k]) for k in [*priority, "distance"] if deltas[k] > 0)
    return f"{' and '.join(gains)}, but {loss}"


def _summary(best: _DraftPlan, backorders: Sequence[Backorder]) -> str:
    ids = " + ".join(s.warehouse_id for s in best.shipments)
    n = best.metrics.shipments
    shipments = f"{n} shipment{'s' if n > 1 else ''}"
    arrival = f"~{best.metrics.last_arrival_minutes:.0f} min"
    if backorders:
        short = sum(b.quantity for b in backorders)
        return (
            f"Partially fulfilled from {ids} ({shipments}, last arrival {arrival}); "
            f"{short} unit(s) backordered."
        )
    return f"Fully fulfilled from {ids} ({shipments}, last arrival {arrival})."


def _reasons(
    best: _DraftPlan,
    alternatives: Sequence[Alternative],
    backorders: Sequence[Backorder],
    exclusions: Sequence[Exclusion],
    urgency: Urgency,
    names: Mapping[str, str],
) -> tuple[str, ...]:
    reasons = [
        "CITO order: the plan whose last parcel arrives earliest wins; fewer shipments breaks ties."
        if urgency is Urgency.CITO
        else "REGULAR order: the plan with the fewest shipments wins; earliest arrival breaks ties."
    ]
    for s in best.shipments:
        goods = ", ".join(f"{names[i.product_id]} ×{i.quantity}" for i in s.items)
        reasons.append(
            f"{s.warehouse_id} ({s.city}, {s.distance_km:g} km, ~{s.eta_minutes:.0f} min "
            f"[{s.eta_basis}]): {goods}"
        )
    for alt in alternatives:
        reasons.append(f"Considered {' + '.join(alt.warehouse_ids)}: {alt.comparison}.")
    if not alternatives:
        reasons.append("No other plan is faster, uses fewer shipments, or travels less.")
    for b in backorders:
        reasons.append(f"Backordered {names[b.product_id]} ×{b.quantity}: {b.reason}.")
    for e in exclusions:
        reasons.append(f"{e.warehouse_id} not used for {names[e.product_id]}: {e.reason}.")
    return tuple(reasons)
