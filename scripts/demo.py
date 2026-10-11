"""Run a real HTTP manufacturing scenario against a local initialized API."""

import argparse
import asyncio
import json
import os
from uuid import uuid4

import httpx


async def run(base_url: str, token: str):
    suffix = uuid4().hex[:8]
    async with httpx.AsyncClient(
        base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=15
    ) as client:

        async def post(path, body=None, key=None):
            response = await client.post(
                path, json=body, headers={"Idempotency-Key": key or uuid4().hex}
            )
            response.raise_for_status()
            return response.json()

        cotton = await post(
            "/materials", {"sku": f"COTTON-{suffix}", "name": "Чёрная ткань", "unit": "m"}
        )
        await post(
            f"/materials/{cotton['id']}/receipts", {"quantity": "80", "reference": f"OPEN-{suffix}"}
        )
        tee = await post(
            "/styles",
            {
                "sku": f"TEE-{suffix}",
                "name": "Футболка",
                "waste_percent": "10",
                "bom": [
                    {"size": size, "color": "black", "material_id": cotton["id"], "quantity": qty}
                    for size, qty in [("M", "1.2"), ("L", "1.4")]
                ],
            },
        )
        order = await post(
            "/orders",
            {
                "reference": f"SO-{suffix}",
                "customer": "Демо-магазин",
                "lines": [
                    {"style_id": tee["id"], "size": size, "color": "black", "quantity": qty}
                    for size, qty in [("M", 60), ("L", 40)]
                ],
            },
        )
        needs_response = await client.get(f"/orders/{order['id']}/requirements")
        needs_response.raise_for_status()
        needs = needs_response.json()
        purchase = await post(
            "/purchases",
            {
                "reference": f"PO-{suffix}",
                "supplier": "Поставщик ткани",
                "lines": [
                    {"material_id": cotton["id"], "quantity": needs["materials"][0]["shortage"]}
                ],
            },
        )
        await post(
            f"/purchases/{purchase['id']}/receipts",
            {
                "reference": f"DEL-A-{suffix}",
                "lines": [{"material_id": cotton["id"], "quantity": "20"}],
            },
        )
        receipt = {
            "reference": f"DEL-B-{suffix}",
            "lines": [{"material_id": cotton["id"], "quantity": "40.8"}],
        }
        await post(f"/purchases/{purchase['id']}/receipts", receipt, f"delivery-{suffix}")
        await post(f"/purchases/{purchase['id']}/receipts", receipt, f"delivery-{suffix}")
        for action in ["reserve", "start", "complete"]:
            order = await post(f"/orders/{order['id']}/{action}")
        print(
            json.dumps(
                {"order": order, "planning": needs, "expected_output_units": 100},
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    token = os.environ.get("ERP_DEV_TOKEN", "")
    if not token:
        parser.error("Set ERP_DEV_TOKEN (or pass a JWT through that environment variable)")
    asyncio.run(run(args.base_url, token))
