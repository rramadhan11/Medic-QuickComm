import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.engine.eta import WIB, EtaSource, Period
from app.models import OrderStatus, StorageCondition, Urgency


class OrderLineIn(BaseModel):
    product_id: str = Field(min_length=1, max_length=10, examples=["P002"])
    quantity: int = Field(gt=0, le=100_000, examples=[45])


class OrderCreate(BaseModel):
    lines: list[OrderLineIn] = Field(min_length=1, max_length=50)
    urgency: Urgency = Field(
        default=Urgency.CITO,
        description="CITO (default): earliest arrival wins. REGULAR: fewest shipments wins.",
    )
    dispatch_at: datetime | None = Field(
        default=None,
        description="When shipments leave the warehouses (default: now). "
        "A time without a zone is read as WIB (UTC+7). Drives time-of-day ETAs.",
        examples=["2026-10-23T17:30:00+07:00"],
    )

    @field_validator("dispatch_at")
    @classmethod
    def assume_wib(cls, value: datetime | None) -> datetime | None:
        return value.replace(tzinfo=WIB) if value and value.tzinfo is None else value

    @field_validator("lines")
    @classmethod
    def unique_products(cls, lines: list[OrderLineIn]) -> list[OrderLineIn]:
        ids = [line.product_id for line in lines]
        if len(set(ids)) != len(ids):
            raise ValueError("each product may appear only once; combine the quantities")
        return lines


class RestockIn(BaseModel):
    product_id: str = Field(min_length=1, max_length=10)
    quantity: int = Field(gt=0, le=100_000)


class ShipmentItemOut(BaseModel):
    product_id: str
    quantity: int


class ShipmentOut(BaseModel):
    warehouse_id: str
    city: str
    distance_km: float
    eta_minutes: float
    eta_basis: str
    items: list[ShipmentItemOut]


class BackorderOut(BaseModel):
    product_id: str
    quantity: int
    reason: str


class ExclusionOut(BaseModel):
    warehouse_id: str
    product_id: str
    reason: str


class PlanMetricsOut(BaseModel):
    shipments: int
    last_arrival_minutes: float
    total_distance_km: float


class AlternativeOut(BaseModel):
    warehouse_ids: list[str]
    metrics: PlanMetricsOut
    comparison: str


class PlanOut(BaseModel):
    urgency: Urgency
    summary: str
    reasons: list[str]
    fully_fulfilled: bool
    metrics: PlanMetricsOut | None
    shipments: list[ShipmentOut]
    backorders: list[BackorderOut]
    exclusions: list[ExclusionOut]
    alternatives: list[AlternativeOut]


class OrderLineOut(BaseModel):
    product_id: str
    product_name: str
    quantity: int
    backordered_quantity: int


class OrderOut(BaseModel):
    id: uuid.UUID
    status: OrderStatus
    urgency: Urgency
    created_at: datetime
    dispatch_at: datetime
    expires_at: datetime
    confirmed_at: datetime | None
    cancelled_at: datetime | None
    lines: list[OrderLineOut]
    plan: PlanOut


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    category: str
    storage_condition: StorageCondition


class StockOut(BaseModel):
    product_id: str
    on_hand: int
    reserved: int
    available: int


class WarehouseOut(BaseModel):
    id: str
    city: str
    distance_km: float
    eta_minutes: float
    eta_basis: str
    has_cold_storage: bool
    has_cold_fleet: bool
    can_ship_cold_chain: bool
    stock: list[StockOut]


class PeriodEtaOut(BaseModel):
    period: Period
    label: str
    minutes: float
    source: EtaSource
    samples: int
    basis: str


class DeliveryTimesOut(BaseModel):
    warehouse_id: str
    city: str
    distance_km: float
    periods: list[PeriodEtaOut]


class RestockOut(BaseModel):
    warehouse_id: str
    product_id: str
    on_hand: int


class ResetOut(BaseModel):
    cancelled_orders: int


class ErrorOut(BaseModel):
    detail: str
    plan: PlanOut | None = None
