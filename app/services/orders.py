"""Order lifecycle: PENDING (stock reserved) → CONFIRMED | CANCELLED | EXPIRED.

Every function takes `now` explicitly so expiry is deterministic in tests.
Functions raise without rolling back: the request-scoped session rolls back, and
releases its row locks, when it closes.
"""

import uuid
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.engine.eta import TimeAwareEtaModel
from app.engine.selection import LineRequest, Plan, select_warehouses
from app.models import Allocation, Order, OrderLine, OrderStatus, Product, Urgency, WarehouseStock
from app.services.errors import Conflict, NotFound, Unprocessable
from app.services.stock import lock_stock, stock_levels, warehouse_options


def effective_status(order: Order, now: datetime) -> OrderStatus:
    """A PENDING order past its expiry is EXPIRED even before the sweeper marks it."""
    if order.status is OrderStatus.PENDING and order.expires_at <= now:
        return OrderStatus.EXPIRED
    return order.status


def create_order(
    session: Session,
    lines: Sequence[tuple[str, int]],
    urgency: Urgency,
    now: datetime,
    ttl: timedelta,
    eta_model: TimeAwareEtaModel,
    dispatch_at: datetime | None = None,
) -> Order:
    dispatch_at = dispatch_at or now
    product_ids = [pid for pid, _ in lines]
    products = {
        p.id: p for p in session.scalars(select(Product).where(Product.id.in_(product_ids)))
    }
    if unknown := [pid for pid in product_ids if pid not in products]:
        raise Unprocessable(f"Unknown product(s): {', '.join(unknown)}")
    requests = [
        LineRequest(pid, products[pid].name, qty, products[pid].storage_condition)
        for pid, qty in lines
    ]

    lock_stock(session, product_ids)
    plan = select_warehouses(
        requests, warehouse_options(session, now, product_ids, eta_model, dispatch_at), urgency
    )
    if not plan.shipments:
        raise Unprocessable("Nothing in this order can be fulfilled right now.", plan)

    backordered = {b.product_id: b.quantity for b in plan.backorders}
    order = Order(
        id=uuid.uuid4(),
        urgency=urgency,
        status=OrderStatus.PENDING,
        created_at=now,
        dispatch_at=dispatch_at,
        expires_at=now + ttl,
        decision=plan_snapshot(plan),
        lines=[
            OrderLine(product_id=pid, quantity=qty, backordered_quantity=backordered.get(pid, 0))
            for pid, qty in lines
        ],
        allocations=[
            Allocation(warehouse_id=s.warehouse_id, product_id=i.product_id, quantity=i.quantity)
            for s in plan.shipments
            for i in s.items
        ],
    )
    session.add(order)
    session.commit()
    return get_order(session, order.id)


def confirm_order(
    session: Session, order_id: uuid.UUID, now: datetime, eta_model: TimeAwareEtaModel
) -> Order:
    order = _lock_order(session, order_id)
    if order.status is OrderStatus.CONFIRMED:
        return order  # idempotent
    if order.status is OrderStatus.CANCELLED:
        raise Conflict("Order is cancelled, so it can't be confirmed.")

    product_ids = sorted({a.product_id for a in order.allocations})
    lock_stock(session, product_ids)
    # PENDING-past-expiry and EXPIRED are the same state; the status column only
    # differs if the sweeper has run, and the outcome must not depend on that.
    if order.expires_at <= now:
        # The reservation lapsed. Its stock no longer counts as reserved, so confirm
        # only if every allocation is still free; otherwise offer a fresh plan.
        free = {
            (s.warehouse_id, s.product_id): s.available
            for s in stock_levels(session, now, product_ids)
        }
        if any(a.quantity > free.get((a.warehouse_id, a.product_id), 0) for a in order.allocations):
            order.status = OrderStatus.EXPIRED
            requests = _line_requests(order)
            fresh = select_warehouses(
                requests,
                warehouse_options(session, now, product_ids, eta_model, order.dispatch_at),
                order.urgency,
            )
            session.commit()
            raise Conflict(
                "The reservation expired and some of its stock has been taken. "
                "Place the order again; the current best plan is attached.",
                fresh,
            )

    for a in order.allocations:
        deducted = session.execute(
            update(WarehouseStock)
            .where(
                WarehouseStock.warehouse_id == a.warehouse_id,
                WarehouseStock.product_id == a.product_id,
                WarehouseStock.on_hand >= a.quantity,
            )
            .values(on_hand=WarehouseStock.on_hand - a.quantity)
        )
        if deducted.rowcount != 1:
            # Unreachable while reserved <= on_hand holds; never deduct into the negative.
            raise Conflict(f"{a.warehouse_id} no longer holds {a.quantity} of {a.product_id}.")
    order.status = OrderStatus.CONFIRMED
    order.confirmed_at = now
    session.commit()
    return get_order(session, order_id)


def cancel_order(session: Session, order_id: uuid.UUID, now: datetime) -> Order:
    order = _lock_order(session, order_id)
    if order.status is OrderStatus.CONFIRMED:
        raise Conflict("Order is already confirmed; its stock has been deducted.")
    if order.status in (OrderStatus.PENDING, OrderStatus.EXPIRED):
        order.status = OrderStatus.CANCELLED
        order.cancelled_at = now
    session.commit()
    return get_order(session, order_id)


def expire_stale_orders(session: Session, now: datetime) -> int:
    """Housekeeping only: availability already ignores expired reservations.
    A single UPDATE, so a concurrent confirm (which locks the row) is never overridden."""
    result = session.execute(
        update(Order)
        .where(Order.status == OrderStatus.PENDING, Order.expires_at <= now)
        .values(status=OrderStatus.EXPIRED)
    )
    session.commit()
    return result.rowcount


def get_order(session: Session, order_id: uuid.UUID) -> Order:
    order = session.scalar(_with_children(select(Order).where(Order.id == order_id)))
    if order is None:
        raise NotFound(f"Order {order_id} not found")
    return order


def list_orders(session: Session, limit: int) -> Sequence[Order]:
    return session.scalars(
        _with_children(select(Order).order_by(Order.created_at.desc()).limit(limit))
    ).all()


def plan_snapshot(plan: Plan) -> dict:
    return {**asdict(plan), "fully_fulfilled": plan.fully_fulfilled}


def _lock_order(session: Session, order_id: uuid.UUID) -> Order:
    order = session.scalar(
        _with_children(select(Order).where(Order.id == order_id)).with_for_update(of=Order)
    )
    if order is None:
        raise NotFound(f"Order {order_id} not found")
    return order


def _with_children(query):
    return query.options(
        selectinload(Order.lines).selectinload(OrderLine.product),
        selectinload(Order.allocations),
    ).execution_options(populate_existing=True)


def _line_requests(order: Order) -> list[LineRequest]:
    return [
        LineRequest(
            line.product_id, line.product.name, line.quantity, line.product.storage_condition
        )
        for line in order.lines
    ]
