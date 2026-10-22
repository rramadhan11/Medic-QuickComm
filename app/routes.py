import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session
from app.engine.eta import Period, TimeAwareEtaModel
from app.models import Order, Product, Warehouse
from app.schemas import (
    DeliveryTimesOut,
    ErrorOut,
    OrderCreate,
    OrderLineOut,
    OrderOut,
    PeriodEtaOut,
    PlanOut,
    ProductOut,
    ResetOut,
    RestockIn,
    RestockOut,
    StockOut,
    WarehouseOut,
)
from app.services import orders, stock

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]


def get_eta_model(request: Request) -> TimeAwareEtaModel:
    return request.app.state.eta_model


def get_now() -> datetime:
    return datetime.now(UTC)


EtaDep = Annotated[TimeAwareEtaModel, Depends(get_eta_model)]
NowDep = Annotated[datetime, Depends(get_now)]
ERRORS = {code: {"model": ErrorOut} for code in (404, 409, 422)}


def order_out(order: Order, now: datetime) -> OrderOut:
    return OrderOut(
        id=order.id,
        status=orders.effective_status(order, now),
        urgency=order.urgency,
        created_at=order.created_at,
        dispatch_at=order.dispatch_at,
        expires_at=order.expires_at,
        confirmed_at=order.confirmed_at,
        cancelled_at=order.cancelled_at,
        lines=[
            OrderLineOut(
                product_id=line.product_id,
                product_name=line.product.name,
                quantity=line.quantity,
                backordered_quantity=line.backordered_quantity,
            )
            for line in order.lines
        ],
        plan=PlanOut.model_validate(order.decision),
    )


@router.get("/health", tags=["system"])
def health(session: SessionDep) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}


@router.get("/products", tags=["catalog"])
def list_products(session: SessionDep) -> list[ProductOut]:
    return [
        ProductOut.model_validate(p) for p in session.scalars(select(Product).order_by(Product.id))
    ]


@router.get("/warehouses", tags=["catalog"])
def list_warehouses(session: SessionDep, eta_model: EtaDep, now: NowDep) -> list[WarehouseOut]:
    """Stock per warehouse, with the ETA for a shipment dispatched now."""
    levels = defaultdict(list)
    for level in stock.stock_levels(session, now):
        levels[level.warehouse_id].append(
            StockOut(
                product_id=level.product_id,
                on_hand=level.on_hand,
                reserved=level.reserved,
                available=level.available,
            )
        )
    out = []
    for w in session.scalars(select(Warehouse).order_by(Warehouse.id)):
        eta = eta_model.estimate(w.id, w.distance_km, now)
        out.append(
            WarehouseOut(
                id=w.id,
                city=w.city,
                distance_km=w.distance_km,
                eta_minutes=eta.minutes,
                eta_basis=eta.basis,
                has_cold_storage=w.has_cold_storage,
                has_cold_fleet=w.has_cold_fleet,
                can_ship_cold_chain=w.has_cold_storage and w.has_cold_fleet,
                stock=levels[w.id],
            )
        )
    return out


@router.get("/delivery-times", tags=["catalog"])
def delivery_times(session: SessionDep, eta_model: EtaDep) -> list[DeliveryTimesOut]:
    """Estimated travel time per warehouse for each dispatch period, with its evidence."""
    out = []
    for w in session.scalars(select(Warehouse).order_by(Warehouse.id)):
        periods = []
        for period in Period:
            eta = eta_model.estimate_for_period(w.id, w.distance_km, period)
            periods.append(
                PeriodEtaOut(
                    period=period,
                    label=period.label,
                    minutes=eta.minutes,
                    source=eta.source,
                    samples=eta.samples,
                    basis=eta.basis,
                )
            )
        out.append(
            DeliveryTimesOut(
                warehouse_id=w.id, city=w.city, distance_km=w.distance_km, periods=periods
            )
        )
    return out


@router.post("/orders", status_code=201, tags=["orders"], responses=ERRORS)
def create_order(
    body: OrderCreate, session: SessionDep, eta_model: EtaDep, now: NowDep
) -> OrderOut:
    """Plan the order and reserve its stock for the reservation window.
    Returns the plan for review; confirm or cancel it before it expires."""
    order = orders.create_order(
        session,
        [(line.product_id, line.quantity) for line in body.lines],
        body.urgency,
        now,
        timedelta(minutes=get_settings().reservation_ttl_minutes),
        eta_model,
        body.dispatch_at,
    )
    return order_out(order, now)


@router.get("/orders", tags=["orders"])
def list_orders(
    session: SessionDep, now: NowDep, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[OrderOut]:
    return [order_out(o, now) for o in orders.list_orders(session, limit)]


@router.get("/orders/{order_id}", tags=["orders"], responses=ERRORS)
def get_order(order_id: uuid.UUID, session: SessionDep, now: NowDep) -> OrderOut:
    return order_out(orders.get_order(session, order_id), now)


@router.post("/orders/{order_id}/confirm", tags=["orders"], responses=ERRORS)
def confirm_order(
    order_id: uuid.UUID, session: SessionDep, eta_model: EtaDep, now: NowDep
) -> OrderOut:
    return order_out(orders.confirm_order(session, order_id, now, eta_model), now)


@router.post("/orders/{order_id}/cancel", tags=["orders"], responses=ERRORS)
def cancel_order(order_id: uuid.UUID, session: SessionDep, now: NowDep) -> OrderOut:
    return order_out(orders.cancel_order(session, order_id, now), now)


@router.post("/warehouses/{warehouse_id}/restock", tags=["stock"], responses=ERRORS)
def restock(warehouse_id: str, body: RestockIn, session: SessionDep) -> RestockOut:
    on_hand = stock.restock(session, warehouse_id, body.product_id, body.quantity)
    return RestockOut(warehouse_id=warehouse_id, product_id=body.product_id, on_hand=on_hand)


@router.post("/stock/reset", tags=["stock"])
def reset_stock(session: SessionDep, now: NowDep) -> ResetOut:
    """Restore baseline stock for every warehouse; cancels pending orders."""
    return ResetOut(cancelled_orders=stock.reset_stock(session, now))
