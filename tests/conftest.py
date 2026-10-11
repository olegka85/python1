import os
from dataclasses import dataclass
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from garment_erp.api.app import create_app
from garment_erp.infrastructure.database import Database
from garment_erp.infrastructure.models import Base
from garment_erp.settings import Settings

TOKEN = "test-only-secret"


@dataclass
class Harness:
    client: httpx.AsyncClient
    db: Database
    app: object


@pytest.fixture
async def harness(tmp_path):
    url = os.environ.get("ERP_TEST_DATABASE_URL")
    if url:
        parsed = make_url(url)
        if parsed.drivername != "postgresql+asyncpg" or not parsed.database.endswith("_test"):
            raise ValueError(
                "ERP_TEST_DATABASE_URL must be a disposable PostgreSQL database ending in _test"
            )
        schema = "test_" + uuid4().hex
        admin_db = Database(url)
        async with admin_db.engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        # Each test owns a fresh temporary schema, never an application schema.
        db = Database(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        db = Database(f"sqlite+aiosqlite:///{tmp_path}/test.db")
        admin_db = None
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=str(db.engine.url),
        auth_mode="dev",
        dev_token=TOKEN,
        dev_tenant="factory-a",
    )
    app = create_app(settings, db)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": f"Bearer {TOKEN}"},
            ) as client,
        ):
            yield Harness(client, db, app)
    finally:
        await db.close()
        if admin_db is not None:
            async with admin_db.engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin_db.close()


async def command(client, path, body=None, key=None, expected=200):
    response = await client.post(path, json=body, headers={"Idempotency-Key": key or uuid4().hex})
    assert response.status_code == expected, response.text
    return response


async def material(client, sku="cotton", unit="m", stock="0"):
    result = (
        await command(client, "/materials", {"sku": sku, "name": sku, "unit": unit}, expected=201)
    ).json()
    if stock != "0":
        await command(
            client,
            f"/materials/{result['id']}/receipts",
            {"quantity": stock, "reference": "initial-" + sku},
        )
    return result


async def style(client, material_id, quantity="1", waste="0", sku="tee"):
    return (
        await command(
            client,
            "/styles",
            {
                "sku": sku,
                "name": sku,
                "waste_percent": waste,
                "bom": [
                    {
                        "size": "M",
                        "color": "black",
                        "material_id": material_id,
                        "quantity": quantity,
                    }
                ],
            },
            expected=201,
        )
    ).json()


async def order(client, style_id, reference="SO-1", quantity=1):
    return (
        await command(
            client,
            "/orders",
            {
                "reference": reference,
                "customer": "Test buyer",
                "lines": [
                    {"style_id": style_id, "size": "M", "color": "black", "quantity": quantity}
                ],
            },
            expected=201,
        )
    ).json()
