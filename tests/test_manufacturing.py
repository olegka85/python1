import asyncio
from decimal import Decimal

from sqlalchemy import func, select

from garment_erp.infrastructure.events import DatabaseProjection, process_one
from garment_erp.infrastructure.models import AuditEvent, InventoryMovement, OutboxEvent
from tests.conftest import command, material, order, style


async def test_order_shortage_purchase_partial_receipts_and_production(harness):
    client = harness.client
    cotton = await material(client, stock="80")
    tee = (
        await command(
            client,
            "/styles",
            {
                "sku": "TEE-01",
                "name": "Футболка",
                "waste_percent": "10",
                "bom": [
                    {"size": size, "color": "black", "material_id": cotton["id"], "quantity": qty}
                    for size, qty in [("M", "1.2"), ("L", "1.4")]
                ],
            },
            expected=201,
        )
    ).json()
    sales = (
        await command(
            client,
            "/orders",
            {
                "reference": "SO-100",
                "customer": "Магазин",
                "lines": [
                    {"style_id": tee["id"], "size": size, "color": "black", "quantity": qty}
                    for size, qty in [("M", 60), ("L", 40)]
                ],
            },
            expected=201,
        )
    ).json()
    path = f"/orders/{sales['id']}"
    needs = (await client.get(path + "/requirements")).json()["materials"][0]
    assert (needs["required"], needs["shortage"]) == ("140.800", "60.800")
    await command(client, path + "/reserve", key="reserve-retry", expected=409)
    assert (await client.get(path)).json()["state"] == "DRAFT"
    purchase = (
        await command(
            client,
            "/purchases",
            {
                "reference": "PO-1",
                "supplier": "Текстиль",
                "lines": [{"material_id": cotton["id"], "quantity": "60.8"}],
            },
            expected=201,
        )
    ).json()
    receipts_path = f"/purchases/{purchase['id']}/receipts"
    first_body = {
        "reference": "delivery-1",
        "lines": [{"material_id": cotton["id"], "quantity": "20"}],
    }
    first = await command(client, receipts_path, first_body, "receipt-1")
    assert first.json()["state"] == "PARTIALLY_RECEIVED"
    replay = await command(client, receipts_path, first_body, "receipt-1")
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()
    assert (await client.get(f"/materials/{cotton['id']}")).json()["on_hand"] == "100.000"
    second = await command(
        client,
        receipts_path,
        {"reference": "delivery-2", "lines": [{"material_id": cotton["id"], "quantity": "40.8"}]},
    )
    assert second.json()["state"] == "RECEIVED"
    reserved = await command(client, path + "/reserve", key="reserve-retry")
    assert reserved.json()["state"] == "RESERVED"
    assert (await client.get(path + "/requirements")).json()["materials"][0]["shortage"] == "0.000"
    await command(client, path + "/start")
    complete = await command(client, path + "/complete", key="complete-1")
    assert complete.json()["state"] == "COMPLETED"
    await command(client, path + "/complete", key="complete-1")
    await command(client, path + "/complete", key="complete-2", expected=409)
    stock = (await client.get(f"/materials/{cotton['id']}")).json()
    assert (stock["on_hand"], stock["reserved"]) == ("0.000", "0.000")
    assert (await client.get("/reports/output")).json()["items"] == []
    while await process_one(harness.db, DatabaseProjection(harness.db)):
        pass
    report = (await client.get("/reports/output")).json()
    assert sum(x["units"] for x in report["items"]) == 100
    movements = (await client.get("/inventory/movements")).json()["items"]
    assert sum(Decimal(x["stock_delta"]) for x in movements) == 0
    assert sum(Decimal(x["reserved_delta"]) for x in movements) == 0


async def test_cancel_releases_stock_and_production_state_rules(harness):
    client = harness.client
    cotton = await material(client, stock="10")
    tee = await style(client, cotton["id"])
    sales = await order(client, tee["id"], quantity=4)
    path = f"/orders/{sales['id']}"
    await command(client, path + "/start", expected=409)
    await command(client, path + "/reserve")
    await command(client, path + "/cancel")
    result = (await client.get(f"/materials/{cotton['id']}")).json()
    assert (result["available"], result["reserved"]) == ("10.000", "0.000")
    await command(client, path + "/reserve", expected=409)
    second = await order(client, tee["id"], reference="SO-2")
    path = f"/orders/{second['id']}"
    await command(client, path + "/reserve")
    await command(client, path + "/start")
    await command(client, path + "/cancel", expected=409)


async def test_reservation_rolls_back_all_materials_audit_and_outbox(harness):
    client = harness.client
    materials = [await material(client, sku="a"), await material(client, sku="b")]
    first, last = sorted(materials, key=lambda x: x["id"])
    await command(
        client, f"/materials/{first['id']}/receipts", {"quantity": "10", "reference": "opening"}
    )
    tee = (
        await command(
            client,
            "/styles",
            {
                "sku": "tee",
                "name": "tee",
                "bom": [
                    {"material_id": x["id"], "size": "M", "color": "black", "quantity": "1"}
                    for x in materials
                ],
            },
            expected=201,
        )
    ).json()
    sales = await order(client, tee["id"])
    before = len((await client.get("/audit")).json()["items"])
    await command(client, f"/orders/{sales['id']}/reserve", expected=409)
    for item in (first, last):
        assert (await client.get(f"/materials/{item['id']}")).json()["reserved"] == "0.000"
    assert len((await client.get("/audit")).json()["items"]) == before
    async with harness.db.sessions() as session:
        audit_count = await session.scalar(select(func.count()).select_from(AuditEvent))
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxEvent))
        reservations = await session.scalar(
            select(func.count())
            .select_from(InventoryMovement)
            .where(InventoryMovement.kind == "reservation")
        )
    assert audit_count == outbox_count == before
    assert reservations == 0


async def test_competing_orders_cannot_reserve_last_material_twice(harness):
    client = harness.client
    cotton = await material(client, stock="1")
    tee = await style(client, cotton["id"])
    orders = [await order(client, tee["id"], reference=f"SO-{i}") for i in range(2)]
    responses = await asyncio.gather(
        *[
            client.post(f"/orders/{x['id']}/reserve", headers={"Idempotency-Key": f"reserve-{i}"})
            for i, x in enumerate(orders)
        ]
    )
    assert sorted(x.status_code for x in responses) == [200, 409]
    stock = (await client.get(f"/materials/{cotton['id']}")).json()
    assert (stock["on_hand"], stock["reserved"], stock["available"]) == ("1.000", "1.000", "0.000")


async def test_receipt_over_delivery_and_duplicate_reference_are_atomic(harness):
    client = harness.client
    cotton = await material(client)
    po = (
        await command(
            client,
            "/purchases",
            {
                "reference": "PO-1",
                "supplier": "supplier",
                "lines": [{"material_id": cotton["id"], "quantity": "10"}],
            },
            expected=201,
        )
    ).json()
    path = f"/purchases/{po['id']}/receipts"
    await command(
        client,
        path,
        {"reference": "too-much", "lines": [{"material_id": cotton["id"], "quantity": "11"}]},
        expected=409,
    )
    assert (await client.get(f"/purchases/{po['id']}")).json()["state"] == "OPEN"
    body = {"reference": "DEL-1", "lines": [{"material_id": cotton["id"], "quantity": "4"}]}
    await command(client, path, body)
    await command(client, path, body, expected=409)
    assert (await client.get(f"/materials/{cotton['id']}")).json()["on_hand"] == "4.000"
    assert (await client.get(f"/purchases/{po['id']}")).json()["lines"][0]["received"] == "4.000"


async def test_pieces_waste_rounding_and_missing_color(harness):
    client = harness.client
    labels = await material(client, unit="pcs", stock="4")
    await command(
        client,
        f"/materials/{labels['id']}/receipts",
        {"quantity": "0.5", "reference": "fraction"},
        expected=422,
    )
    tee = await style(client, labels["id"], waste="5")
    sales = await order(client, tee["id"], quantity=3)
    needs = (await client.get(f"/orders/{sales['id']}/requirements")).json()["materials"][0]
    assert needs["required"] == "4.000"
    await command(
        client,
        "/orders",
        {
            "reference": "RED-1",
            "customer": "buyer",
            "lines": [{"style_id": tee["id"], "size": "M", "color": "red", "quantity": 1}],
        },
        expected=422,
    )


async def test_idempotency_body_conflict_concurrent_replay_and_missing_key(harness):
    client = harness.client
    body = {"sku": "cotton", "name": "Cotton", "unit": "m"}
    results = await asyncio.gather(
        *[
            client.post("/materials", json=body, headers={"Idempotency-Key": "same"})
            for _ in range(2)
        ]
    )
    assert [x.status_code for x in results] == [201, 201]
    assert results[0].json()["id"] == results[1].json()["id"]
    assert sorted(x.headers["Idempotency-Replayed"] for x in results) == ["false", "true"]
    await command(client, "/materials", {**body, "name": "Other"}, "same", expected=409)
    assert (await client.post("/materials", json=body)).status_code == 422
    assert len((await client.get("/materials")).json()["items"]) == 1
    assert len((await client.get("/audit")).json()["items"]) == 1
