"""Service-level tests for expiry (with an injected clock) and concurrency."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import func, select

from app.db import SessionLocal
from app.engine.eta import DeliverySample, TimeAwareEtaModel
from app.models import Allocation, Order, OrderStatus, Urgency, WarehouseStock
from app.seed import WAREHOUSES, load_delivery_history
from app.services import orders, stock
from app.services.errors import Conflict

KM = {wid: km for wid, _, km, *_ in WAREHOUSES}

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
TTL = timedelta(minutes=10)
ETA = TimeAwareEtaModel.fit(
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


def available(session, warehouse_id, product_id, now):
    levels = stock.stock_levels(session, now, [product_id])
    return next(lv.available for lv in levels if lv.warehouse_id == warehouse_id)


def test_expired_reservation_frees_stock_without_any_write(session):
    orders.create_order(session, [("P002", 45)], Urgency.CITO, NOW, TTL, ETA)

    assert available(session, "W04", "P002", NOW + timedelta(minutes=9)) == 0
    assert available(session, "W04", "P002", NOW + timedelta(minutes=10)) == 40


def test_reported_status_is_expired_before_the_sweeper_runs(session):
    order = orders.create_order(session, [("P001", 1)], Urgency.CITO, NOW, TTL, ETA)

    assert orders.effective_status(order, NOW + TTL) is OrderStatus.EXPIRED
    assert order.status is OrderStatus.PENDING


def test_sweeper_marks_only_lapsed_pending_orders(session):
    old = orders.create_order(session, [("P001", 1)], Urgency.CITO, NOW, TTL, ETA)
    fresh = orders.create_order(session, [("P003", 1)], Urgency.CITO, NOW + TTL, TTL, ETA)

    assert orders.expire_stale_orders(session, NOW + TTL) == 1
    assert orders.get_order(session, old.id).status is OrderStatus.EXPIRED
    assert orders.get_order(session, fresh.id).status is OrderStatus.PENDING


def test_confirm_after_expiry_succeeds_when_stock_is_still_free(session):
    order = orders.create_order(session, [("P002", 45)], Urgency.CITO, NOW, TTL, ETA)

    confirmed = orders.confirm_order(session, order.id, NOW + timedelta(minutes=30), ETA)

    assert confirmed.status is OrderStatus.CONFIRMED
    on_hand = session.scalar(
        select(WarehouseStock.on_hand).where(
            WarehouseStock.warehouse_id == "W04", WarehouseStock.product_id == "P002"
        )
    )
    assert on_hand == 0


def test_confirm_after_expiry_fails_with_fresh_plan_when_stock_was_taken(session):
    order = orders.create_order(session, [("P002", 45)], Urgency.CITO, NOW, TTL, ETA)
    later = NOW + timedelta(minutes=15)
    orders.create_order(session, [("P002", 40)], Urgency.CITO, later, TTL, ETA)  # takes W04

    with pytest.raises(Conflict) as error:
        orders.confirm_order(session, order.id, later, ETA)

    assert "reservation expired" in error.value.message
    assert error.value.plan is not None
    assert [s.warehouse_id for s in error.value.plan.shipments] == ["W08"]  # W04 now taken
    assert orders.get_order(session, order.id).status is OrderStatus.EXPIRED


def test_lapsed_order_behaves_the_same_whether_or_not_the_sweeper_ran(session):
    swept = orders.create_order(session, [("P001", 1)], Urgency.CITO, NOW, TTL, ETA)
    swept_too = orders.create_order(session, [("P003", 1)], Urgency.CITO, NOW, TTL, ETA)
    later = NOW + timedelta(minutes=30)
    assert orders.expire_stale_orders(session, later) == 2

    assert orders.confirm_order(session, swept.id, later, ETA).status is OrderStatus.CONFIRMED
    assert orders.cancel_order(session, swept_too.id, later).status is OrderStatus.CANCELLED


def test_cancel_after_expiry_is_allowed(session):
    order = orders.create_order(session, [("P001", 1)], Urgency.CITO, NOW, TTL, ETA)

    cancelled = orders.cancel_order(session, order.id, NOW + timedelta(hours=1))

    assert cancelled.status is OrderStatus.CANCELLED


def test_concurrent_orders_never_oversell(session):
    """8 orders race for 100 Hep B each; only 108 units are eligible in total.
    Without row locks they'd all see 108 free and reserve far more than exists."""
    workers = 8
    barrier = Barrier(workers)

    def place_order(_):
        with SessionLocal() as s:
            barrier.wait()
            try:
                orders.create_order(s, [("P002", 100)], Urgency.REGULAR, NOW, TTL, ETA)
            except Exception as exc:  # 422 once nothing is left
                return type(exc).__name__
            return "created"

    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(place_order, range(workers)))

    reserved = dict(
        session.execute(
            select(Allocation.warehouse_id, func.sum(Allocation.quantity))
            .join(Order)
            .where(Order.status == OrderStatus.PENDING)
            .group_by(Allocation.warehouse_id)
        ).all()
    )
    assert reserved == {"W04": 40, "W07": 8, "W08": 60}  # exactly what exists, never more
    assert results.count("created") == 2  # 100, then the remaining 8
    assert results.count("Unprocessable") == workers - 2
