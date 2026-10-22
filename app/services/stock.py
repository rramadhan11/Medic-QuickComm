"""Stock levels, reservations and stock changes.

Available = on_hand - reserved, where reserved is the sum of allocations on PENDING
orders that haven't expired yet. An expired reservation therefore frees stock the
moment it expires, with no write needed (the sweeper only tidies order statuses).

Concurrency: every stock-changing transaction locks the affected `warehouse_stock`
rows with SELECT ... FOR UPDATE in (warehouse_id, product_id) order before reading
availability. Concurrent orders for the same products serialise instead of both
seeing the same free stock, and the fixed lock order rules out deadlocks.
Lock order across tables is always orders → warehouse_stock.
"""

from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.engine.eta import TimeAwareEtaModel
from app.engine.selection import WarehouseOption
from app.models import (
    Allocation,
    Order,
    OrderStatus,
    Product,
    StorageCondition,
    Warehouse,
    WarehouseStock,
)
from app.seed import baseline_stock
from app.services.errors import NotFound, Unprocessable


@dataclass(frozen=True)
class StockLevel:
    warehouse_id: str
    product_id: str
    on_hand: int
    reserved: int

    @property
    def available(self) -> int:
        return self.on_hand - self.reserved


def lock_stock(session: Session, product_ids: Collection[str] | None = None) -> None:
    query = select(WarehouseStock.warehouse_id).order_by(
        WarehouseStock.warehouse_id, WarehouseStock.product_id
    )
    if product_ids is not None:
        query = query.where(WarehouseStock.product_id.in_(product_ids))
    session.execute(query.with_for_update())


def stock_levels(
    session: Session, now: datetime, product_ids: Collection[str] | None = None
) -> list[StockLevel]:
    reserved = (
        select(
            Allocation.warehouse_id,
            Allocation.product_id,
            func.sum(Allocation.quantity).label("quantity"),
        )
        .join(Order)
        .where(Order.status == OrderStatus.PENDING, Order.expires_at > now)
        .group_by(Allocation.warehouse_id, Allocation.product_id)
        .subquery()
    )
    query = (
        select(
            WarehouseStock.warehouse_id,
            WarehouseStock.product_id,
            WarehouseStock.on_hand,
            func.coalesce(reserved.c.quantity, 0),
        )
        .outerjoin(
            reserved,
            (reserved.c.warehouse_id == WarehouseStock.warehouse_id)
            & (reserved.c.product_id == WarehouseStock.product_id),
        )
        .order_by(WarehouseStock.warehouse_id, WarehouseStock.product_id)
    )
    if product_ids is not None:
        query = query.where(WarehouseStock.product_id.in_(product_ids))
    return [StockLevel(*row) for row in session.execute(query)]


def warehouse_options(
    session: Session,
    now: datetime,
    product_ids: Collection[str],
    eta_model: TimeAwareEtaModel,
    dispatch_at: datetime,
) -> list[WarehouseOption]:
    """`now` decides which reservations are live; `dispatch_at` drives the ETAs."""
    available: dict[str, dict[str, int]] = defaultdict(dict)
    for level in stock_levels(session, now, product_ids):
        available[level.warehouse_id][level.product_id] = level.available
    options = []
    for w in session.scalars(select(Warehouse).order_by(Warehouse.id)):
        eta = eta_model.estimate(w.id, w.distance_km, dispatch_at)
        options.append(
            WarehouseOption(
                warehouse_id=w.id,
                city=w.city,
                distance_km=w.distance_km,
                eta_minutes=eta.minutes,
                has_cold_storage=w.has_cold_storage,
                has_cold_fleet=w.has_cold_fleet,
                available=available[w.id],
                eta_basis=eta.basis,
            )
        )
    return options


def restock(session: Session, warehouse_id: str, product_id: str, quantity: int) -> int:
    """Atomically add stock; returns the new on_hand."""
    warehouse = session.get(Warehouse, warehouse_id)
    if warehouse is None:
        raise NotFound(f"Warehouse {warehouse_id} not found")
    product = session.get(Product, product_id)
    if product is None:
        raise NotFound(f"Product {product_id} not found")
    if product.storage_condition is StorageCondition.COLD_2_8C and not warehouse.has_cold_storage:
        raise Unprocessable(
            f"{warehouse_id} has no refrigerated storage, so it cannot hold {product.name}"
        )

    stmt = pg_insert(WarehouseStock).values(
        warehouse_id=warehouse_id, product_id=product_id, on_hand=quantity
    )
    on_hand = session.scalar(
        stmt.on_conflict_do_update(
            index_elements=[WarehouseStock.warehouse_id, WarehouseStock.product_id],
            set_={
                "on_hand": WarehouseStock.on_hand + stmt.excluded.on_hand,
                "updated_at": func.now(),
            },
        ).returning(WarehouseStock.on_hand)
    )
    session.commit()
    return on_hand


def reset_stock(session: Session, now: datetime) -> int:
    """Restore baseline stock. Pending orders are cancelled first, because their
    reservations may exceed the restored quantities. Returns how many were cancelled."""
    pending = session.scalars(
        select(Order.id)
        .where(Order.status == OrderStatus.PENDING)
        .order_by(Order.id)
        .with_for_update()
    ).all()
    lock_stock(session)
    if pending:
        session.execute(
            update(Order)
            .where(Order.id.in_(pending))
            .values(status=OrderStatus.CANCELLED, cancelled_at=now)
        )
    session.execute(delete(WarehouseStock))
    session.execute(insert(WarehouseStock), baseline_stock())
    session.commit()
    return len(pending)
