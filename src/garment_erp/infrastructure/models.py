import time
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    return str(uuid4())


def now() -> int:
    return int(time.time())


class Base(DeclarativeBase):
    pass


class Material(Base):
    __tablename__ = "materials"
    __table_args__ = (
        UniqueConstraint("tenant_id", "sku"),
        UniqueConstraint("id", "tenant_id"),
        CheckConstraint(
            "on_hand >= 0 AND reserved >= 0 AND reserved <= on_hand", name="ck_material_balance"
        ),
        CheckConstraint("unit IN ('m', 'pcs')", name="ck_material_unit"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(150), index=True)
    sku: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(150))
    unit: Mapped[str] = mapped_column(String(10))
    on_hand: Mapped[int] = mapped_column(BigInteger, default=0)
    reserved: Mapped[int] = mapped_column(BigInteger, default=0)


class Style(Base):
    __tablename__ = "styles"
    __table_args__ = (UniqueConstraint("tenant_id", "sku"), UniqueConstraint("id", "tenant_id"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(150), index=True)
    sku: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(150))
    waste_percent: Mapped[str] = mapped_column(String(10))


class BomComponent(Base):
    __tablename__ = "bom_components"
    __table_args__ = (
        ForeignKeyConstraint(["style_id", "tenant_id"], ["styles.id", "styles.tenant_id"]),
        ForeignKeyConstraint(["material_id", "tenant_id"], ["materials.id", "materials.tenant_id"]),
        UniqueConstraint("style_id", "size", "color", "material_id"),
        CheckConstraint("quantity_units > 0", name="ck_bom_quantity"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(150))
    style_id: Mapped[str] = mapped_column(String(36), index=True)
    material_id: Mapped[str] = mapped_column(String(36))
    size: Mapped[str] = mapped_column(String(20))
    color: Mapped[str] = mapped_column(String(40))
    quantity_units: Mapped[int] = mapped_column(BigInteger)


class SalesOrder(Base):
    __tablename__ = "sales_orders"
    __table_args__ = (
        UniqueConstraint("tenant_id", "reference"),
        UniqueConstraint("id", "tenant_id"),
        CheckConstraint(
            "state IN ('DRAFT', 'RESERVED', 'IN_PRODUCTION', 'COMPLETED', 'CANCELLED')",
            name="ck_order_state",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(150), index=True)
    reference: Mapped[str] = mapped_column(String(80))
    customer: Mapped[str] = mapped_column(String(150))
    state: Mapped[str] = mapped_column(String(30), default="DRAFT")
    lines: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)


class OrderRequirement(Base):
    __tablename__ = "order_requirements"
    __table_args__ = (
        ForeignKeyConstraint(
            ["order_id", "tenant_id"], ["sales_orders.id", "sales_orders.tenant_id"]
        ),
        ForeignKeyConstraint(["material_id", "tenant_id"], ["materials.id", "materials.tenant_id"]),
        CheckConstraint("quantity_units > 0", name="ck_requirement_quantity"),
    )
    order_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    material_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(150))
    quantity_units: Mapped[int] = mapped_column(BigInteger)


class Purchase(Base):
    __tablename__ = "purchases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "reference"),
        UniqueConstraint("id", "tenant_id"),
        CheckConstraint(
            "state IN ('OPEN', 'PARTIALLY_RECEIVED', 'RECEIVED')", name="ck_purchase_state"
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(150), index=True)
    reference: Mapped[str] = mapped_column(String(80))
    supplier: Mapped[str] = mapped_column(String(150))
    state: Mapped[str] = mapped_column(String(30), default="OPEN")
    revision: Mapped[int] = mapped_column(Integer, default=0)


class PurchaseLine(Base):
    __tablename__ = "purchase_lines"
    __table_args__ = (
        ForeignKeyConstraint(["purchase_id", "tenant_id"], ["purchases.id", "purchases.tenant_id"]),
        ForeignKeyConstraint(["material_id", "tenant_id"], ["materials.id", "materials.tenant_id"]),
        CheckConstraint(
            "ordered > 0 AND received >= 0 AND received <= ordered", name="ck_purchase_line_balance"
        ),
    )
    purchase_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    material_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(150))
    ordered: Mapped[int] = mapped_column(BigInteger)
    received: Mapped[int] = mapped_column(BigInteger, default=0)


class InventoryMovement(Base):
    __tablename__ = "inventory_movements"
    __table_args__ = (
        ForeignKeyConstraint(["material_id", "tenant_id"], ["materials.id", "materials.tenant_id"]),
        UniqueConstraint("tenant_id", "kind", "source", "material_id", name="uq_movement_source"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(150), index=True)
    material_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(30))
    source: Mapped[str] = mapped_column(String(80))
    stock_delta: Mapped[int] = mapped_column(BigInteger)
    reserved_delta: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    tenant_id: Mapped[str] = mapped_column(String(150), primary_key=True)
    principal_id: Mapped[str] = mapped_column(String(150), primary_key=True)
    scope: Mapped[str] = mapped_column(String(200), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    body_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("ix_outbox_due", "state", "available_at", "lease_until"),
        CheckConstraint(
            "state IN ('PENDING', 'PROCESSING', 'PUBLISHED', 'DEAD')", name="ck_outbox_state"
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(150), index=True)
    kind: Mapped[str] = mapped_column(String(80))
    aggregate_id: Mapped[str] = mapped_column(String(36))
    payload: Mapped[dict] = mapped_column(JSON)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(20), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[int] = mapped_column(BigInteger, default=now)
    lease_until: Mapped[int] = mapped_column(BigInteger, default=0)
    claimed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(150), index=True)
    principal_id: Mapped[str] = mapped_column(String(150))
    kind: Mapped[str] = mapped_column(String(80))
    aggregate_id: Mapped[str] = mapped_column(String(36))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)


class InboxRecord(Base):
    __tablename__ = "inbox_records"
    tenant_id: Mapped[str] = mapped_column(String(150), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    body_hash: Mapped[str] = mapped_column(String(64))
    processed_at: Mapped[int] = mapped_column(BigInteger, default=now)


class DailyOutput(Base):
    __tablename__ = "daily_output"
    tenant_id: Mapped[str] = mapped_column(String(150), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    units: Mapped[int] = mapped_column(BigInteger, default=0)
    __table_args__ = (CheckConstraint("units >= 0", name="ck_output_nonnegative"),)
