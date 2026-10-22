"""Baseline reference data.

Seeding only inserts rows that don't exist yet, so restarting the app never
overwrites stock that has changed through orders or restocks.
"""

import csv
from datetime import datetime
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import DeliveryRecord, Product, StorageCondition, Warehouse, WarehouseStock

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DELIVERY_HISTORY_CSV = DATA_DIR / "customer_1_historical_deliveries.csv"

COLD = StorageCondition.COLD_2_8C
ROOM = StorageCondition.ROOM_TEMP

PRODUCTS = [
    ("P001", "Surgical Gloves (Box of 100)", "Medical Supplies", ROOM),
    ("P002", "Vaccine — Hepatitis B (10-dose vial)", "Pharmaceutical", COLD),
    ("P003", "Disposable Syringe (Box of 50)", "Medical Supplies", ROOM),
    ("P004", "Vaccine — Tetanus Toxoid (10-dose vial)", "Pharmaceutical", COLD),
    ("P005", "Paracetamol Tablets (Box of 100)", "Pharmaceutical", ROOM),
]

# (id, city, distance_km, has_cold_storage, has_cold_fleet, {product_id: on_hand})
WAREHOUSES = [
    ("W01", "Surabaya", 5, False, False, {"P001": 200, "P003": 500, "P005": 300}),
    ("W02", "Surabaya", 12, True, False, {"P001": 50, "P002": 30, "P004": 20}),
    ("W03", "Sidoarjo", 25, False, False, {"P001": 300, "P003": 200}),
    ("W04", "Gresik", 30, True, True, {"P002": 40, "P004": 25, "P005": 100}),
    ("W05", "Malang", 90, False, False, {"P001": 400, "P003": 300, "P005": 500}),
    ("W06", "Malang", 95, True, False, {"P002": 20, "P004": 30, "P003": 100}),
    ("W07", "Pasuruan", 45, True, True, {"P002": 8, "P004": 10, "P001": 150}),
    ("W08", "Jember", 150, True, True, {"P002": 60, "P004": 50, "P005": 200}),
]

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
NO_NOTE = "—"


def baseline_stock() -> list[dict]:
    return [
        {"warehouse_id": wid, "product_id": pid, "on_hand": qty}
        for wid, *_, stock in WAREHOUSES
        for pid, qty in stock.items()
    ]


def load_delivery_history(path: Path = DELIVERY_HISTORY_CSV) -> list[dict]:
    records = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            day, clock = (part.strip() for part in row["Day/Time"].split(","))
            note = row["Notes"].strip()
            records.append(
                {
                    "id": row["Delivery ID"],
                    "warehouse_id": row["Warehouse"],
                    "day_of_week": DAYS.index(day),
                    "dispatch_time": datetime.strptime(clock, "%H:%M").time(),
                    "duration_minutes": int(row["Actual Duration (minutes)"]),
                    "note": None if note in ("", NO_NOTE) else note,
                }
            )
    return records


def seed(session: Session) -> None:
    def insert_missing(model, rows: list[dict]) -> None:
        session.execute(insert(model).values(rows).on_conflict_do_nothing())

    insert_missing(
        Product,
        [
            {"id": pid, "name": name, "category": cat, "storage_condition": cond}
            for pid, name, cat, cond in PRODUCTS
        ],
    )
    insert_missing(
        Warehouse,
        [
            {
                "id": wid,
                "city": city,
                "distance_km": km,
                "has_cold_storage": cold_storage,
                "has_cold_fleet": cold_fleet,
            }
            for wid, city, km, cold_storage, cold_fleet, _ in WAREHOUSES
        ],
    )
    insert_missing(WarehouseStock, baseline_stock())
    insert_missing(DeliveryRecord, load_delivery_history())
    session.commit()


if __name__ == "__main__":
    from app.db import SessionLocal

    with SessionLocal() as session:
        seed(session)
    print("Seed complete.")
