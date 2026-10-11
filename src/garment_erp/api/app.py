import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID, uuid4

import httpx
import jwt
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from garment_erp.application.catalog import CatalogService, material_view, style_view
from garment_erp.application.contracts import (
    MaterialCreate,
    OrderCreate,
    PurchaseCreate,
    PurchaseReceipt,
    StockReceipt,
    StyleCreate,
)
from garment_erp.application.orders import OrderService
from garment_erp.application.purchasing import PurchasingService, purchase_view
from garment_erp.domain.errors import BusinessError
from garment_erp.domain.planning import display_units
from garment_erp.infrastructure.commands import CommandExecutor
from garment_erp.infrastructure.database import Database
from garment_erp.infrastructure.models import (
    AuditEvent,
    DailyOutput,
    InventoryMovement,
    Material,
    OutboxEvent,
)
from garment_erp.infrastructure.repository import SQLFactoryRepository
from garment_erp.infrastructure.security import Principal, authenticate, authorize
from garment_erp.settings import Settings

PrincipalDep = Annotated[Principal, Depends(authenticate)]
CommandKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
]
logger = logging.getLogger("garment_erp.http")


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    settings = settings or Settings()
    db = database or Database(settings.database_url)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        application.state.http = httpx.AsyncClient(timeout=5)
        try:
            yield
        finally:
            await application.state.http.aclose()
            if database is None:
                await db.close()

    app = FastAPI(
        title="Garment ERP",
        version="0.1.0",
        lifespan=lifespan,
        description="Garment orders, frozen BOM requirements, purchasing, inventory and production.",
    )
    app.state.settings, app.state.db = settings, db
    app.state.jwk_client = (
        jwt.PyJWKClient(settings.zitadel_jwks_url, timeout=5, lifespan=300)
        if settings.zitadel_jwks_url
        else None
    )
    executor = CommandExecutor(db)

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        try:
            request_id = str(UUID(request.headers.get("X-Request-ID", "")))
        except ValueError:
            request_id = str(uuid4())
        request.state.request_id = request_id
        started = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            json.dumps(
                {
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                }
            )
        )
        return response

    @app.exception_handler(BusinessError)
    async def business_error(request: Request, error: BusinessError):
        return JSONResponse(
            status_code=error.status,
            content={
                "error": {"code": error.code, "message": error.message, "details": error.details},
                "request_id": request.state.request_id,
            },
        )

    async def mutate(request, principal, action, key, body, handler, status=200):
        # Every replay goes through fresh authentication and authorization.
        await authorize(request, principal, action)
        result = await executor.execute(
            principal.tenant_id, principal.id, request.url.path, key, body, handler
        )
        return JSONResponse(
            result.payload,
            status_code=status,
            headers={"Idempotency-Replayed": str(result.replayed).lower()},
        )

    async def read(request, principal, callback, action="read"):
        await authorize(request, principal, action)
        async with db.sessions() as session:
            repo = SQLFactoryRepository(session, principal.tenant_id, principal.id)
            return await callback(repo)

    @app.get("/health/live", tags=["Health"])
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready", tags=["Health"])
    async def ready():
        try:
            async with db.sessions() as session:
                await session.execute(text("SELECT 1"))
                await session.execute(select(Material.id).limit(1))
        except (SQLAlchemyError, OSError):
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ready"}

    @app.post("/materials", tags=["Catalog"], status_code=201)
    async def create_material(
        body: MaterialCreate, request: Request, principal: PrincipalDep, key: CommandKey
    ):
        return await mutate(
            request,
            principal,
            "catalog:write",
            key,
            body.model_dump(mode="json"),
            lambda repo: CatalogService(repo).create_material(body),
            201,
        )

    @app.get("/materials", tags=["Catalog"])
    async def list_materials(request: Request, principal: PrincipalDep):
        async def get(repo):
            return {"items": [material_view(x) for x in await repo.materials()], "limit": 200}

        return await read(request, principal, get)

    @app.get("/materials/{material_id}", tags=["Catalog"])
    async def get_material(material_id: UUID, request: Request, principal: PrincipalDep):
        async def get(repo):
            return material_view(await repo.material(str(material_id)))

        return await read(request, principal, get)

    @app.post("/materials/{material_id}/receipts", tags=["Inventory"])
    async def receive_stock(
        material_id: UUID,
        body: StockReceipt,
        request: Request,
        principal: PrincipalDep,
        key: CommandKey,
    ):
        return await mutate(
            request,
            principal,
            "inventory:receive",
            key,
            body.model_dump(mode="json"),
            lambda repo: CatalogService(repo).receive_stock(str(material_id), body),
        )

    @app.post("/styles", tags=["Catalog"], status_code=201)
    async def create_style(
        body: StyleCreate, request: Request, principal: PrincipalDep, key: CommandKey
    ):
        return await mutate(
            request,
            principal,
            "catalog:write",
            key,
            body.model_dump(mode="json"),
            lambda repo: CatalogService(repo).create_style(body),
            201,
        )

    @app.get("/styles", tags=["Catalog"])
    async def list_styles(request: Request, principal: PrincipalDep):
        async def get(repo):
            return {"items": [style_view(x) for x in await repo.styles()], "limit": 200}

        return await read(request, principal, get)

    @app.get("/styles/{style_id}", tags=["Catalog"])
    async def get_style(style_id: UUID, request: Request, principal: PrincipalDep):
        async def get(repo):
            return style_view(await repo.style(str(style_id)))

        return await read(request, principal, get)

    @app.post("/orders", tags=["Orders"], status_code=201)
    async def create_order(
        body: OrderCreate, request: Request, principal: PrincipalDep, key: CommandKey
    ):
        return await mutate(
            request,
            principal,
            "orders:write",
            key,
            body.model_dump(mode="json"),
            lambda repo: OrderService(repo).create(body),
            201,
        )

    @app.get("/orders", tags=["Orders"])
    async def list_orders(request: Request, principal: PrincipalDep):
        async def get(repo):
            return {"items": await repo.orders(), "limit": 200}

        return await read(request, principal, get)

    @app.get("/orders/{order_id}", tags=["Orders"])
    async def get_order(order_id: UUID, request: Request, principal: PrincipalDep):
        return await read(request, principal, lambda repo: repo.order(str(order_id)))

    @app.get("/orders/{order_id}/requirements", tags=["Planning"])
    async def order_requirements(order_id: UUID, request: Request, principal: PrincipalDep):
        return await read(request, principal, lambda repo: OrderService(repo).needs(str(order_id)))

    @app.post("/orders/{order_id}/reserve", tags=["Orders"])
    async def reserve(order_id: UUID, request: Request, principal: PrincipalDep, key: CommandKey):
        return await mutate(
            request,
            principal,
            "orders:write",
            key,
            {},
            lambda repo: OrderService(repo).reserve(str(order_id)),
        )

    @app.post("/orders/{order_id}/start", tags=["Production"])
    async def start(order_id: UUID, request: Request, principal: PrincipalDep, key: CommandKey):
        return await mutate(
            request,
            principal,
            "production:write",
            key,
            {},
            lambda repo: OrderService(repo).start(str(order_id)),
        )

    @app.post("/orders/{order_id}/complete", tags=["Production"])
    async def complete(order_id: UUID, request: Request, principal: PrincipalDep, key: CommandKey):
        return await mutate(
            request,
            principal,
            "production:write",
            key,
            {},
            lambda repo: OrderService(repo).complete(str(order_id)),
        )

    @app.post("/orders/{order_id}/cancel", tags=["Orders"])
    async def cancel(order_id: UUID, request: Request, principal: PrincipalDep, key: CommandKey):
        return await mutate(
            request,
            principal,
            "orders:write",
            key,
            {},
            lambda repo: OrderService(repo).cancel(str(order_id)),
        )

    @app.post("/purchases", tags=["Purchasing"], status_code=201)
    async def create_purchase(
        body: PurchaseCreate, request: Request, principal: PrincipalDep, key: CommandKey
    ):
        return await mutate(
            request,
            principal,
            "purchases:write",
            key,
            body.model_dump(mode="json"),
            lambda repo: PurchasingService(repo).create(body),
            201,
        )

    @app.get("/purchases", tags=["Purchasing"])
    async def list_purchases(request: Request, principal: PrincipalDep):
        async def get(repo):
            return {"items": [purchase_view(x) for x in await repo.purchases()], "limit": 200}

        return await read(request, principal, get)

    @app.get("/purchases/{purchase_id}", tags=["Purchasing"])
    async def get_purchase(purchase_id: UUID, request: Request, principal: PrincipalDep):
        async def get(repo):
            return purchase_view(await repo.purchase(str(purchase_id)))

        return await read(request, principal, get)

    @app.post("/purchases/{purchase_id}/receipts", tags=["Purchasing"])
    async def receive_purchase(
        purchase_id: UUID,
        body: PurchaseReceipt,
        request: Request,
        principal: PrincipalDep,
        key: CommandKey,
    ):
        return await mutate(
            request,
            principal,
            "inventory:receive",
            key,
            body.model_dump(mode="json"),
            lambda repo: PurchasingService(repo).receive(str(purchase_id), body),
        )

    @app.get("/inventory/movements", tags=["Inventory"])
    async def movements(request: Request, principal: PrincipalDep):
        await authorize(request, principal, "read")
        async with db.sessions() as session:
            rows = await session.scalars(
                select(InventoryMovement)
                .where(InventoryMovement.tenant_id == principal.tenant_id)
                .order_by(InventoryMovement.created_at.desc(), InventoryMovement.id)
                .limit(200)
            )
            return {
                "items": [
                    {
                        "id": x.id,
                        "material_id": x.material_id,
                        "kind": x.kind,
                        "reference": x.source,
                        "stock_delta": display_units(x.stock_delta),
                        "reserved_delta": display_units(x.reserved_delta),
                        "created_at": x.created_at,
                    }
                    for x in rows
                ],
                "limit": 200,
            }

    @app.get("/reports/output", tags=["Reports"])
    async def output_report(request: Request, principal: PrincipalDep):
        await authorize(request, principal, "read")
        async with db.sessions() as session:
            rows = await session.scalars(
                select(DailyOutput)
                .where(DailyOutput.tenant_id == principal.tenant_id)
                .order_by(DailyOutput.day.desc())
                .limit(200)
            )
            return {
                "items": [{"day": x.day, "units": x.units} for x in rows],
                "timezone": "UTC",
                "consistency": "eventual",
            }

    @app.get("/operations/outbox", tags=["Operations"])
    async def outbox(request: Request, principal: PrincipalDep):
        await authorize(request, principal, "operations:read")
        async with db.sessions() as session:
            rows = await session.scalars(
                select(OutboxEvent)
                .where(OutboxEvent.tenant_id == principal.tenant_id)
                .order_by(OutboxEvent.created_at.desc(), OutboxEvent.id)
                .limit(200)
            )
            return {
                "items": [
                    {
                        "id": x.id,
                        "kind": x.kind,
                        "state": x.state,
                        "attempts": x.attempts,
                        "available_at": x.available_at,
                        "lease_until": x.lease_until,
                    }
                    for x in rows
                ],
                "limit": 200,
            }

    @app.get("/audit", tags=["Audit"])
    async def audit(request: Request, principal: PrincipalDep):
        await authorize(request, principal, "operations:read")
        async with db.sessions() as session:
            rows = await session.scalars(
                select(AuditEvent)
                .where(AuditEvent.tenant_id == principal.tenant_id)
                .order_by(AuditEvent.created_at.desc(), AuditEvent.id)
                .limit(200)
            )
            return {
                "items": [
                    {
                        "id": x.id,
                        "actor": x.principal_id,
                        "kind": x.kind,
                        "aggregate_id": x.aggregate_id,
                        "payload": x.payload,
                        "created_at": x.created_at,
                    }
                    for x in rows
                ],
                "limit": 200,
            }

    return app


app = create_app()
