from datetime import time

from app.models import StorageCondition
from app.seed import PRODUCTS, WAREHOUSES, baseline_stock, load_delivery_history


def test_delivery_history_parses_every_row():
    records = load_delivery_history()

    assert len(records) == 38
    assert len({r["id"] for r in records}) == 38
    first = records[0]
    assert first == {
        "id": "DL001",
        "warehouse_id": "W01",
        "day_of_week": 0,
        "dispatch_time": time(8, 0),
        "duration_minutes": 50,
        "note": None,
    }


def test_delivery_history_keeps_real_notes_and_drops_placeholders():
    notes = {r["id"]: r["note"] for r in load_delivery_history() if r["note"]}

    assert notes == {
        "DL013": "Heavy traffic reported",
        "DL028": "Weekend travelers heading to Malang",
        "DL029": "Weekend travelers heading to Malang",
        "DL030": "Return traffic from Malang",
        "DL033": "Weekend travelers heading to Malang",
        "DL034": "Return traffic from Malang",
    }


def test_delivery_history_only_references_known_warehouses():
    warehouse_ids = {w[0] for w in WAREHOUSES}

    assert {r["warehouse_id"] for r in load_delivery_history()} <= warehouse_ids


def test_baseline_stock_only_references_known_products():
    product_ids = {p[0] for p in PRODUCTS}

    assert {s["product_id"] for s in baseline_stock()} <= product_ids


def test_only_vaccines_require_cold_chain():
    cold = {pid for pid, *_, cond in PRODUCTS if cond is StorageCondition.COLD_2_8C}

    assert cold == {"P002", "P004"}
