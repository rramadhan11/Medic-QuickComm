import uuid
from datetime import datetime, time
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    MetaData,
    SmallInteger,
    String,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    # Deterministic constraint names so Alembic migrations can reference them.
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class StorageCondition(StrEnum):
    ROOM_TEMP = "ROOM_TEMP"
    COLD_2_8C = "COLD_2_8C"  # must stay 2–8 °C from storage to delivery


class Urgency(StrEnum):
    CITO = "CITO"  # urgent: earliest arrival wins
    REGULAR = "REGULAR"  # routine replenishment: fewest shipments wins


class OrderStatus(StrEnum):
    PENDING = "PENDING"  # stock reserved until expires_at
    CONFIRMED = "CONFIRMED"  # stock deducted from on_hand
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


def _enum(enum_cls: type[StrEnum]) -> Enum:
    # Stored as VARCHAR + CHECK so adding a value doesn't need ALTER TYPE.
    return Enum(
        enum_cls,
        name=enum_cls.__name__.lower(),
        native_enum=False,
        create_constraint=True,
        length=20,
        validate_strings=True,
    )


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(100))
    storage_condition: Mapped[StorageCondition] = mapped_column(_enum(StorageCondition))


class Warehouse(Base):
    __tablename__ = "warehouses"
    __table_args__ = (CheckConstraint("distance_km >= 0", name="distance_non_negative"),)

    id: Mapped[str] = mapped_column(String(10), primary_key=True)
    city: Mapped[str] = mapped_column(String(100))
    distance_km: Mapped[float]
    has_cold_storage: Mapped[bool]
    has_cold_fleet: Mapped[bool]

    stock: Mapped[list["WarehouseStock"]] = relationship(back_populates="warehouse")


class WarehouseStock(Base):
    """Physical stock. Reserved quantity is derived from PENDING, unexpired allocations,
    so an expired reservation frees stock without any write."""

    __tablename__ = "warehouse_stock"
    __table_args__ = (CheckConstraint("on_hand >= 0", name="on_hand_non_negative"),)

    warehouse_id: Mapped[str] = mapped_column(ForeignKey("warehouses.id"), primary_key=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), primary_key=True)
    on_hand: Mapped[int]
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    warehouse: Mapped[Warehouse] = relationship(back_populates="stock")
    product: Mapped[Product] = relationship()


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_status_expires_at", "status", "expires_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    urgency: Mapped[Urgency] = mapped_column(_enum(Urgency))
    status: Mapped[OrderStatus] = mapped_column(_enum(OrderStatus))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # When the shipments leave the warehouses; drives time-of-day ETAs.
    dispatch_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Snapshot of the selection reasoning at the time of the decision, for audit.
    decision: Mapped[dict | None] = mapped_column(JSONB)

    lines: Mapped[list["OrderLine"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderLine.id"
    )
    allocations: Mapped[list["Allocation"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="Allocation.id"
    )


class OrderLine(Base):
    __tablename__ = "order_lines"
    __table_args__ = (
        UniqueConstraint("order_id", "product_id"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint(
            "backordered_quantity >= 0 AND backordered_quantity <= quantity",
            name="backordered_within_quantity",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[int]
    backordered_quantity: Mapped[int] = mapped_column(default=0)

    order: Mapped[Order] = relationship(back_populates="lines")
    product: Mapped[Product] = relationship()


class Allocation(Base):
    """Quantity of one product taken from one warehouse for an order."""

    __tablename__ = "allocations"
    __table_args__ = (
        UniqueConstraint("order_id", "warehouse_id", "product_id"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        Index("ix_allocations_warehouse_product", "warehouse_id", "product_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    warehouse_id: Mapped[str] = mapped_column(ForeignKey("warehouses.id"))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[int]

    order: Mapped[Order] = relationship(back_populates="allocations")
    warehouse: Mapped[Warehouse] = relationship()
    product: Mapped[Product] = relationship()


class DeliveryRecord(Base):
    """Historical delivery durations, used for time-aware ETA estimates."""

    __tablename__ = "delivery_records"
    __table_args__ = (
        CheckConstraint("day_of_week BETWEEN 0 AND 6", name="day_of_week_range"),
        CheckConstraint("duration_minutes > 0", name="duration_positive"),
    )

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    warehouse_id: Mapped[str] = mapped_column(ForeignKey("warehouses.id"), index=True)
    day_of_week: Mapped[int] = mapped_column(SmallInteger)  # 0 = Monday
    dispatch_time: Mapped[time] = mapped_column(Time)
    duration_minutes: Mapped[int]
    note: Mapped[str | None] = mapped_column(String(500))
