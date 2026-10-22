"""HTTP-level tests of the order lifecycle against Postgres."""

import uuid


def place(client, urgency=None, dispatch_at=None, **quantities):
    ids = {"gloves": "P001", "hep_b": "P002", "syringe": "P003", "tetanus": "P004"}
    ids["paracetamol"] = "P005"
    body = {"lines": [{"product_id": ids[k], "quantity": q} for k, q in quantities.items()]}
    if urgency:
        body["urgency"] = urgency
    if dispatch_at:
        body["dispatch_at"] = dispatch_at
    return client.post("/orders", json=body)


def stock_of(client, warehouse_id, product_id):
    [warehouse] = [w for w in client.get("/warehouses").json() if w["id"] == warehouse_id]
    [level] = [s for s in warehouse["stock"] if s["product_id"] == product_id]
    return level


def shipped(order):
    return {
        s["warehouse_id"]: {i["product_id"]: i["quantity"] for i in s["items"]}
        for s in order["plan"]["shipments"]
    }


def test_urgency_defaults_to_cito(client):
    order = place(client, hep_b=45).json()

    assert order["urgency"] == "CITO"
    assert shipped(order) == {"W04": {"P002": 40}, "W07": {"P002": 5}}


def test_regular_urgency_changes_the_plan(client):
    order = place(client, urgency="REGULAR", hep_b=45).json()

    assert shipped(order) == {"W08": {"P002": 45}}


def test_placing_an_order_reserves_without_deducting(client):
    response = place(client, hep_b=45)

    assert response.status_code == 201
    order = response.json()
    assert order["status"] == "PENDING"
    assert stock_of(client, "W04", "P002") == {
        "product_id": "P002",
        "on_hand": 40,
        "reserved": 40,
        "available": 0,
    }
    assert stock_of(client, "W07", "P002")["available"] == 3


def test_next_order_only_sees_unreserved_stock(client):
    place(client, hep_b=45)

    second = place(client, hep_b=10).json()

    # W04 is fully reserved. W07 (3 left) + W08 finishes no sooner than W08 alone,
    # so CITO's tie-break (fewer shipments) picks W08 alone.
    assert shipped(second) == {"W08": {"P002": 10}}
    assert {(e["warehouse_id"], e["reason"]) for e in second["plan"]["exclusions"]} >= {
        ("W04", "no unreserved stock")
    }


def test_confirm_deducts_reserved_stock(client):
    order = place(client, hep_b=45).json()

    confirmed = client.post(f"/orders/{order['id']}/confirm")

    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "CONFIRMED"
    assert stock_of(client, "W04", "P002") == {
        "product_id": "P002",
        "on_hand": 0,
        "reserved": 0,
        "available": 0,
    }
    assert stock_of(client, "W07", "P002")["on_hand"] == 3


def test_confirm_is_idempotent(client):
    order = place(client, gloves=10).json()
    client.post(f"/orders/{order['id']}/confirm")

    again = client.post(f"/orders/{order['id']}/confirm")

    assert again.status_code == 200
    assert stock_of(client, "W01", "P001")["on_hand"] == 190


def test_cancel_releases_the_reservation(client):
    order = place(client, hep_b=45).json()

    cancelled = client.post(f"/orders/{order['id']}/cancel")

    assert cancelled.json()["status"] == "CANCELLED"
    assert stock_of(client, "W04", "P002")["available"] == 40


def test_confirmed_order_cannot_be_cancelled_and_cancelled_cannot_be_confirmed(client):
    confirmed = place(client, gloves=10).json()
    client.post(f"/orders/{confirmed['id']}/confirm")
    cancelled = place(client, gloves=10).json()
    client.post(f"/orders/{cancelled['id']}/cancel")

    assert client.post(f"/orders/{confirmed['id']}/cancel").status_code == 409
    assert client.post(f"/orders/{cancelled['id']}/confirm").status_code == 409


def test_partial_order_records_backorders(client):
    order = place(client, hep_b=200).json()

    assert order["plan"]["fully_fulfilled"] is False
    assert order["lines"] == [
        {
            "product_id": "P002",
            "product_name": "Vaccine — Hepatitis B (10-dose vial)",
            "quantity": 200,
            "backordered_quantity": 92,
        }
    ]


def test_order_with_nothing_fulfillable_is_rejected_with_explanation(client):
    place(client, hep_b=108)  # every eligible Hep B unit is now reserved

    response = place(client, hep_b=1)

    assert response.status_code == 422
    body = response.json()
    assert body["detail"] == "Nothing in this order can be fulfilled right now."
    assert body["plan"]["backorders"][0]["quantity"] == 1
    assert client.get("/orders").json()[0]["lines"][0]["quantity"] == 108  # nothing persisted


def test_input_validation(client):
    unknown = client.post("/orders", json={"lines": [{"product_id": "P999", "quantity": 1}]})
    duplicate = client.post(
        "/orders",
        json={
            "lines": [{"product_id": "P001", "quantity": 1}, {"product_id": "P001", "quantity": 2}]
        },
    )
    zero = client.post("/orders", json={"lines": [{"product_id": "P001", "quantity": 0}]})
    empty = client.post("/orders", json={"lines": []})

    assert unknown.status_code == 422 and "P999" in unknown.json()["detail"]
    assert duplicate.status_code == 422
    assert zero.status_code == 422
    assert empty.status_code == 422


def test_unknown_order_is_404(client):
    assert client.get(f"/orders/{uuid.uuid4()}").status_code == 404
    assert client.post(f"/orders/{uuid.uuid4()}/confirm").status_code == 404


def test_orders_are_listed_newest_first_with_stored_plan(client):
    first = place(client, gloves=1).json()
    second = place(client, syringe=1).json()

    listed = client.get("/orders").json()

    assert [o["id"] for o in listed] == [second["id"], first["id"]]
    assert listed[0]["plan"]["summary"].startswith("Fully fulfilled from W01")


def test_restock_adds_stock_and_can_introduce_a_new_product(client):
    response = client.post("/warehouses/W04/restock", json={"product_id": "P001", "quantity": 25})

    assert response.json() == {"warehouse_id": "W04", "product_id": "P001", "on_hand": 25}
    client.post("/warehouses/W04/restock", json={"product_id": "P001", "quantity": 5})
    assert stock_of(client, "W04", "P001")["on_hand"] == 30


def test_restock_refuses_vaccine_into_warehouse_without_cold_storage(client):
    response = client.post("/warehouses/W01/restock", json={"product_id": "P002", "quantity": 5})

    assert response.status_code == 422
    assert "no refrigerated storage" in response.json()["detail"]


def test_restock_unknown_warehouse_is_404(client):
    response = client.post("/warehouses/W99/restock", json={"product_id": "P001", "quantity": 1})

    assert response.status_code == 404


def test_reset_restores_baseline_and_cancels_pending_orders(client):
    confirmed = place(client, gloves=50).json()
    client.post(f"/orders/{confirmed['id']}/confirm")
    pending = place(client, hep_b=45).json()
    client.post("/warehouses/W04/restock", json={"product_id": "P001", "quantity": 25})

    response = client.post("/stock/reset")

    assert response.json() == {"cancelled_orders": 1}
    assert stock_of(client, "W01", "P001")["on_hand"] == 200
    assert stock_of(client, "W04", "P002")["available"] == 40
    assert all(s["product_id"] != "P001" for s in _warehouse(client, "W04")["stock"])
    assert client.get(f"/orders/{pending['id']}").json()["status"] == "CANCELLED"
    assert client.get(f"/orders/{confirmed['id']}").json()["status"] == "CONFIRMED"


def test_warehouses_report_cold_chain_capability(client):
    capable = {w["id"] for w in client.get("/warehouses").json() if w["can_ship_cold_chain"]}

    assert capable == {"W04", "W07", "W08"}


def _warehouse(client, warehouse_id):
    return next(w for w in client.get("/warehouses").json() if w["id"] == warehouse_id)


def test_ui_is_served(client):
    page = client.get("/")
    script = client.get("/static/app.js")

    assert page.status_code == 200 and "<title>Medic QuickComm</title>" in page.text
    assert script.status_code == 200


def test_dispatch_time_changes_the_decision(client):
    # 250 gloves: off-peak, W01 (200) + W02 (50) arrive within ~25 min. In Friday
    # rush hour W02's history shows ~100 min, while W03 alone (holds 300) takes ~75,
    # so the plan flips from two nearby warehouses to one farther one.
    off_peak = place(client, gloves=250, dispatch_at="2026-10-20T11:00:00+07:00").json()
    client.post(f"/orders/{off_peak['id']}/cancel")
    friday_rush = place(client, gloves=250, dispatch_at="2026-10-23T17:30:00+07:00").json()

    assert shipped(off_peak) == {"W01": {"P001": 200}, "W02": {"P001": 50}}
    assert shipped(friday_rush) == {"W03": {"P001": 250}}
    [nearby] = [
        a for a in friday_rush["plan"]["alternatives"] if a["warehouse_ids"] == ["W01", "W02"]
    ]
    assert nearby["comparison"] == "travels 8 km less, but arrives 25 min later"
    assert friday_rush["plan"]["shipments"][0]["eta_basis"] == (
        "Friday rush hour, from 1 past W03 delivery"
    )


def test_dispatch_time_without_zone_is_read_as_wib(client):
    order = place(client, gloves=1, dispatch_at="2026-10-23T17:30:00").json()

    assert order["dispatch_at"] == "2026-10-23T10:30:00Z"


def test_dispatch_defaults_to_now(client):
    order = place(client, gloves=1).json()

    assert order["dispatch_at"] == order["created_at"]


def test_delivery_times_expose_evidence_per_period(client):
    times = {w["warehouse_id"]: w for w in client.get("/delivery-times").json()}

    w02 = {p["period"]: p for p in times["W02"]["periods"]}
    assert round(w02["FRIDAY_PEAK"]["minutes"]) == 100
    assert w02["FRIDAY_PEAK"]["samples"] == 1
    w08 = {p["period"]: p for p in times["W08"]["periods"]}
    assert w08["WEEKDAY_PEAK"]["source"] == "DISTANCE"  # no intercity rush-hour data
    assert w08["WEEKEND"]["source"] == "ROUTE_TYPE"
    assert w02["FRIDAY_PEAK"]["source"] == "WAREHOUSE"
