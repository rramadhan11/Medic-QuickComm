from dataclasses import replace

import pytest

from app.engine.eta import DistanceEtaModel
from app.engine.selection import (
    MAX_EXACT_WAREHOUSES,
    LineRequest,
    WarehouseOption,
    select_warehouses,
)
from app.models import Urgency
from app.seed import PRODUCTS, WAREHOUSES, load_delivery_history

PRODUCT_INFO = {pid: (name, cond) for pid, name, _, cond in PRODUCTS}
KM = {wid: km for wid, _, km, *_ in WAREHOUSES}
ETA = DistanceEtaModel.fit(
    [(KM[r["warehouse_id"]], r["duration_minutes"]) for r in load_delivery_history()]
)
GLOVES, HEP_B, SYRINGE, TETANUS, PARACETAMOL = "P001", "P002", "P003", "P004", "P005"


def seed_warehouses() -> list[WarehouseOption]:
    return [
        WarehouseOption(wid, city, km, ETA.estimate(km), cold_storage, cold_fleet, dict(stock))
        for wid, city, km, cold_storage, cold_fleet, stock in WAREHOUSES
    ]


def order(**quantities: int) -> list[LineRequest]:
    ids = {"gloves": GLOVES, "hep_b": HEP_B, "syringe": SYRINGE, "tetanus": TETANUS}
    ids["paracetamol"] = PARACETAMOL
    return [
        LineRequest(ids[k], PRODUCT_INFO[ids[k]][0], qty, PRODUCT_INFO[ids[k]][1])
        for k, qty in quantities.items()
    ]


def allocation(plan) -> dict[str, dict[str, int]]:
    return {s.warehouse_id: {i.product_id: i.quantity for i in s.items} for s in plan.shipments}


def test_single_warehouse_nearest_with_stock():
    plan = select_warehouses(order(gloves=100), seed_warehouses(), Urgency.CITO)

    assert allocation(plan) == {"W01": {GLOVES: 100}}
    assert plan.fully_fulfilled


def test_vaccine_never_ships_from_warehouse_without_refrigerated_fleet():
    # W02 is the nearest warehouse holding Hep B, but its fleet is not refrigerated.
    plan = select_warehouses(order(hep_b=10), seed_warehouses(), Urgency.CITO)

    assert allocation(plan) == {"W04": {HEP_B: 10}}
    excluded = {(e.warehouse_id, e.product_id): e.reason for e in plan.exclusions}
    assert "no refrigerated delivery fleet" in excluded[("W02", HEP_B)]
    assert "no refrigerated delivery fleet" in excluded[("W06", HEP_B)]


def test_paracetamol_is_pharmaceutical_but_not_cold_chain():
    plan = select_warehouses(order(paracetamol=50), seed_warehouses(), Urgency.CITO)

    assert allocation(plan) == {"W01": {PARACETAMOL: 50}}
    assert plan.exclusions == ()


def test_cito_prefers_earlier_arrival_over_fewer_shipments():
    # 45 Hep B: W08 alone (150 km) vs W04 + W07 (30 + 45 km).
    plan = select_warehouses(order(hep_b=45), seed_warehouses(), Urgency.CITO)

    assert allocation(plan) == {"W04": {HEP_B: 40}, "W07": {HEP_B: 5}}
    assert plan.alternatives[0].warehouse_ids == ("W08",)
    assert plan.alternatives[0].comparison.startswith("uses 1 fewer shipment(s), but arrives")


def test_regular_prefers_fewer_shipments_over_earlier_arrival():
    plan = select_warehouses(order(hep_b=45), seed_warehouses(), Urgency.REGULAR)

    assert allocation(plan) == {"W08": {HEP_B: 45}}
    assert [a.warehouse_ids for a in plan.alternatives] == [("W04", "W07")]
    assert plan.alternatives[0].comparison.startswith("arrives")
    assert "but needs 1 more shipment(s)" in plan.alternatives[0].comparison


def test_mixed_order_urgency_changes_the_decision():
    # W07 alone can ship both (45 km) vs W01 + W04 (5 + 30 km, two shipments).
    lines = order(gloves=100, hep_b=5)

    cito = select_warehouses(lines, seed_warehouses(), Urgency.CITO)
    regular = select_warehouses(lines, seed_warehouses(), Urgency.REGULAR)

    assert allocation(cito) == {"W01": {GLOVES: 100}, "W04": {HEP_B: 5}}
    assert allocation(regular) == {"W07": {GLOVES: 100, HEP_B: 5}}


def test_split_single_product_across_minimum_number_of_warehouses():
    # 1000 gloves: only W05 + W03 + W01 + W07 (400 + 300 + 200 + 150) covers it in 4.
    plan = select_warehouses(order(gloves=1000), seed_warehouses(), Urgency.REGULAR)

    assert set(allocation(plan)) == {"W01", "W03", "W05", "W07"}
    assert sum(q[GLOVES] for q in allocation(plan).values()) == 1000
    assert plan.fully_fulfilled


def test_shortfall_ships_everything_eligible_and_backorders_the_rest():
    # Eligible Hep B: W04 40 + W07 8 + W08 60 = 108. W02/W06 stock must not be counted.
    plan = select_warehouses(order(hep_b=200), seed_warehouses(), Urgency.CITO)

    assert allocation(plan) == {"W04": {HEP_B: 40}, "W07": {HEP_B: 8}, "W08": {HEP_B: 60}}
    assert not plan.fully_fulfilled
    [backorder] = plan.backorders
    assert (backorder.product_id, backorder.quantity) == (HEP_B, 92)
    assert "only 108 available" in backorder.reason
    assert plan.summary.startswith("Partially fulfilled")


def test_shortfall_on_one_line_does_not_block_the_others():
    # Tetanus needs every eligible unit (W04 25 + W07 10 + W08 50). W07 also holds
    # gloves, so they ride along on that shipment instead of adding a W01 trip.
    plan = select_warehouses(order(gloves=10, tetanus=500), seed_warehouses(), Urgency.CITO)

    assert allocation(plan) == {
        "W04": {TETANUS: 25},
        "W07": {GLOVES: 10, TETANUS: 10},
        "W08": {TETANUS: 50},
    }
    assert [(b.product_id, b.quantity) for b in plan.backorders] == [(TETANUS, 415)]


def test_cold_product_held_only_by_non_cold_warehouses_is_fully_backordered():
    warehouses = [w for w in seed_warehouses() if w.warehouse_id in {"W01", "W02", "W06"}]

    plan = select_warehouses(order(hep_b=5), warehouses, Urgency.CITO)

    assert plan.shipments == ()
    assert plan.metrics is None
    assert plan.backorders[0].reason == "no warehouse with a full cold chain has stock"
    assert plan.summary.startswith("Nothing can be shipped")


def test_reserved_stock_is_not_offered():
    # Availability already excludes reservations; zero means fully reserved.
    warehouses = [
        replace(w, available={**w.available, GLOVES: 0}) if w.warehouse_id == "W01" else w
        for w in seed_warehouses()
    ]

    plan = select_warehouses(order(gloves=100), warehouses, Urgency.CITO)

    assert "W01" not in allocation(plan)
    assert ("W01", GLOVES) in {(e.warehouse_id, e.product_id) for e in plan.exclusions}


def test_ties_are_broken_deterministically():
    twin = [
        WarehouseOption("WB", "X", 10, 20, False, False, {GLOVES: 50}),
        WarehouseOption("WA", "X", 10, 20, False, False, {GLOVES: 50}),
    ]

    plan = select_warehouses(order(gloves=10), twin, Urgency.CITO)

    assert allocation(plan) == {"WA": {GLOVES: 10}}
    assert plan.alternatives[0].comparison == "ties on every criterion; lost on warehouse id order"


def test_explanation_names_every_shipment_and_exclusion():
    plan = select_warehouses(order(hep_b=45), seed_warehouses(), Urgency.CITO)

    text = "\n".join(plan.reasons)
    assert plan.reasons[0].startswith("CITO order")
    assert "W04 (Gresik, 30 km" in text and "W07 (Pasuruan, 45 km" in text
    assert "Considered W08: uses 1 fewer shipment(s), but arrives" in text
    assert "W02 not used for Vaccine — Hepatitis B" in text


@pytest.mark.parametrize(
    "lines, message",
    [
        ([], "no lines"),
        (order(gloves=1) + order(gloves=2), "duplicate"),
        (order(gloves=0), "positive"),
    ],
)
def test_rejects_invalid_orders(lines, message):
    with pytest.raises(ValueError, match=message):
        select_warehouses(lines, seed_warehouses(), Urgency.CITO)


def test_alternatives_that_are_worse_on_every_criterion_are_not_reported():
    # W01 + W04 + W07 (winner plus a redundant W01) must not show up.
    plan = select_warehouses(order(gloves=100, hep_b=45), seed_warehouses(), Urgency.CITO)

    assert allocation(plan) == {"W04": {HEP_B: 40}, "W07": {GLOVES: 100, HEP_B: 5}}
    assert plan.alternatives == ()
    assert "No other plan is faster, uses fewer shipments, or travels less." in plan.reasons


def test_search_stays_fast_at_the_exact_search_limit():
    many = [
        WarehouseOption(f"W{i:02}", "X", 10 + i, 20 + i, False, False, {GLOVES: 10})
        for i in range(MAX_EXACT_WAREHOUSES)
    ]

    plan = select_warehouses(order(gloves=10 * MAX_EXACT_WAREHOUSES), many, Urgency.CITO)

    assert plan.metrics.shipments == MAX_EXACT_WAREHOUSES


def test_eta_model_has_fixed_overhead_and_increases_with_distance():
    assert ETA.intercept_minutes > 0
    assert ETA.estimate(5) < ETA.estimate(30) < ETA.estimate(150)
