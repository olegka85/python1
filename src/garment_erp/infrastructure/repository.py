from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from garment_erp.domain.errors import NotFound
from garment_erp.infrastructure.models import (
    AuditEvent,
    BomComponent,
    InventoryMovement,
    Material,
    OrderRequirement,
    OutboxEvent,
    Purchase,
    PurchaseLine,
    SalesOrder,
    Style,
    new_id,
)


def values(row, fields: tuple[str, ...]) -> dict:
    return {field: getattr(row, field) for field in fields}


class SQLFactoryRepository:
    def __init__(self, session: AsyncSession, tenant_id: str, principal_id: str):
        self.session, self.tenant_id, self.principal_id = session, tenant_id, principal_id

    async def _one(self, model, record_id: str):
        row = await self.session.scalar(
            select(model).where(model.id == record_id, model.tenant_id == self.tenant_id)
        )
        if row is None:
            raise NotFound(model.__tablename__)
        return row

    async def material(self, material_id: str) -> dict:
        return values(
            await self._one(Material, material_id),
            ("id", "sku", "name", "unit", "on_hand", "reserved"),
        )

    async def materials(self) -> list[dict]:
        rows = await self.session.scalars(
            select(Material)
            .where(Material.tenant_id == self.tenant_id)
            .order_by(Material.sku)
            .limit(200)
        )
        return [values(x, ("id", "sku", "name", "unit", "on_hand", "reserved")) for x in rows]

    async def create_material(self, sku: str, name: str, unit: str) -> dict:
        row = Material(tenant_id=self.tenant_id, sku=sku, name=name, unit=unit)
        self.session.add(row)
        await self.session.flush()
        return await self.material(row.id)

    async def adjust_stock(self, material_id: str, stock: int = 0, reserved: int = 0) -> bool:
        # Conditional UPDATE serializes contention in PostgreSQL without a read/modify/write race.
        result = await self.session.execute(
            update(Material)
            .where(
                Material.id == material_id,
                Material.tenant_id == self.tenant_id,
                Material.on_hand + stock >= 0,
                Material.reserved + reserved >= 0,
                Material.on_hand + stock >= Material.reserved + reserved,
            )
            .values(on_hand=Material.on_hand + stock, reserved=Material.reserved + reserved)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    async def movement(
        self, material_id: str, kind: str, source: str, stock: int, reserved: int
    ) -> None:
        self.session.add(
            InventoryMovement(
                tenant_id=self.tenant_id,
                material_id=material_id,
                kind=kind,
                source=source,
                stock_delta=stock,
                reserved_delta=reserved,
            )
        )
        await self.session.flush()

    async def create_style(self, sku: str, name: str, waste: Decimal, bom: list[dict]) -> dict:
        row = Style(tenant_id=self.tenant_id, sku=sku, name=name, waste_percent=str(waste))
        self.session.add(row)
        await self.session.flush()
        self.session.add_all(
            [BomComponent(tenant_id=self.tenant_id, style_id=row.id, **x) for x in bom]
        )
        await self.session.flush()
        return await self.style(row.id)

    async def style(self, style_id: str) -> dict:
        row = await self._one(Style, style_id)
        bom = await self.session.scalars(
            select(BomComponent)
            .where(BomComponent.style_id == style_id, BomComponent.tenant_id == self.tenant_id)
            .order_by(BomComponent.size, BomComponent.color, BomComponent.material_id)
        )
        return {
            **values(row, ("id", "sku", "name", "waste_percent")),
            "bom": [values(x, ("size", "color", "material_id", "quantity_units")) for x in bom],
        }

    async def styles(self) -> list[dict]:
        ids = await self.session.scalars(
            select(Style.id).where(Style.tenant_id == self.tenant_id).order_by(Style.sku).limit(200)
        )
        return [await self.style(style_id) for style_id in ids]

    async def create_order(
        self, reference: str, customer: str, lines: list[dict], requirements: dict[str, int]
    ) -> dict:
        row = SalesOrder(
            tenant_id=self.tenant_id, reference=reference, customer=customer, lines=lines
        )
        self.session.add(row)
        await self.session.flush()
        self.session.add_all(
            [
                OrderRequirement(
                    order_id=row.id, tenant_id=self.tenant_id, material_id=mid, quantity_units=qty
                )
                for mid, qty in requirements.items()
            ]
        )
        await self.session.flush()
        return await self.order(row.id)

    async def order(self, order_id: str) -> dict:
        # Refresh cached state after SQL UPDATE statements.
        self.session.expire_all()
        return values(
            await self._one(SalesOrder, order_id),
            ("id", "reference", "customer", "state", "lines", "created_at"),
        )

    async def orders(self) -> list[dict]:
        rows = await self.session.scalars(
            select(SalesOrder)
            .where(SalesOrder.tenant_id == self.tenant_id)
            .order_by(SalesOrder.created_at.desc(), SalesOrder.id)
            .limit(200)
        )
        return [
            values(x, ("id", "reference", "customer", "state", "lines", "created_at")) for x in rows
        ]

    async def transition_order(self, order_id: str, expected: str, target: str) -> bool:
        result = await self.session.execute(
            update(SalesOrder)
            .where(
                SalesOrder.id == order_id,
                SalesOrder.tenant_id == self.tenant_id,
                SalesOrder.state == expected,
            )
            .values(state=target)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    async def requirements(self, order_id: str) -> list[dict]:
        rows = await self.session.execute(
            select(
                OrderRequirement.material_id,
                OrderRequirement.quantity_units,
                Material.sku,
                Material.unit,
                Material.on_hand,
                Material.reserved,
            )
            .join(
                Material,
                (Material.id == OrderRequirement.material_id)
                & (Material.tenant_id == OrderRequirement.tenant_id),
            )
            .where(
                OrderRequirement.order_id == order_id, OrderRequirement.tenant_id == self.tenant_id
            )
            .order_by(OrderRequirement.material_id)
        )
        return [dict(x._mapping) for x in rows]

    async def create_purchase(self, reference: str, supplier: str, lines: list[dict]) -> dict:
        row = Purchase(tenant_id=self.tenant_id, reference=reference, supplier=supplier)
        self.session.add(row)
        await self.session.flush()
        self.session.add_all(
            [PurchaseLine(tenant_id=self.tenant_id, purchase_id=row.id, **x) for x in lines]
        )
        await self.session.flush()
        return await self.purchase(row.id)

    async def purchase(self, purchase_id: str) -> dict:
        self.session.expire_all()
        row = await self._one(Purchase, purchase_id)
        lines = await self.session.scalars(
            select(PurchaseLine)
            .where(
                PurchaseLine.purchase_id == purchase_id, PurchaseLine.tenant_id == self.tenant_id
            )
            .order_by(PurchaseLine.material_id)
        )
        return {
            **values(row, ("id", "reference", "supplier", "state")),
            "lines": [values(x, ("material_id", "ordered", "received")) for x in lines],
        }

    async def purchases(self) -> list[dict]:
        ids = await self.session.scalars(
            select(Purchase.id)
            .where(Purchase.tenant_id == self.tenant_id)
            .order_by(Purchase.reference)
            .limit(200)
        )
        return [await self.purchase(purchase_id) for purchase_id in ids]

    async def claim_purchase(self, purchase_id: str) -> bool:
        # Locks the purchase row before any partial-receipt mutation.
        result = await self.session.execute(
            update(Purchase)
            .where(
                Purchase.id == purchase_id,
                Purchase.tenant_id == self.tenant_id,
                Purchase.state.in_(["OPEN", "PARTIALLY_RECEIVED"]),
            )
            .values(revision=Purchase.revision + 1)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    async def receive_purchase_line(
        self, purchase_id: str, material_id: str, quantity: int
    ) -> bool:
        result = await self.session.execute(
            update(PurchaseLine)
            .where(
                PurchaseLine.purchase_id == purchase_id,
                PurchaseLine.material_id == material_id,
                PurchaseLine.tenant_id == self.tenant_id,
                PurchaseLine.received + quantity <= PurchaseLine.ordered,
            )
            .values(received=PurchaseLine.received + quantity)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    async def set_purchase_state(self, purchase_id: str, state: str) -> None:
        await self.session.execute(
            update(Purchase)
            .where(Purchase.id == purchase_id, Purchase.tenant_id == self.tenant_id)
            .values(state=state)
            .execution_options(synchronize_session=False)
        )

    async def emit(self, kind: str, aggregate_id: str, payload: dict) -> None:
        event_id = new_id()
        self.session.add(
            OutboxEvent(
                id=event_id,
                tenant_id=self.tenant_id,
                kind=kind,
                aggregate_id=aggregate_id,
                payload=payload,
            )
        )
        self.session.add(
            AuditEvent(
                id=event_id,
                tenant_id=self.tenant_id,
                principal_id=self.principal_id,
                kind=kind,
                aggregate_id=aggregate_id,
                payload=payload,
            )
        )
        await self.session.flush()
