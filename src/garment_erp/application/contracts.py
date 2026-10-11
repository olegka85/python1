from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

Quantity = Annotated[Decimal, Field(gt=0, le=1_000_000, max_digits=10, decimal_places=3)]
Name = Annotated[str, Field(min_length=1, max_length=150)]
Code = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[\w.\-/]+$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MaterialCreate(Contract):
    sku: Code
    name: Name
    unit: Literal["m", "pcs"]


class StockReceipt(Contract):
    quantity: Quantity
    reference: Code


class BomLine(Contract):
    size: Annotated[str, Field(min_length=1, max_length=20)]
    color: Annotated[str, Field(min_length=1, max_length=40)]
    material_id: UUID
    quantity: Quantity


class StyleCreate(Contract):
    sku: Code
    name: Name
    waste_percent: Annotated[Decimal, Field(ge=0, le=100, decimal_places=2)] = Decimal(0)
    bom: list[BomLine] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_components(self):
        keys = [(x.size, x.color, x.material_id) for x in self.bom]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate material in a size/color recipe")
        return self


class OrderLine(Contract):
    style_id: UUID
    size: Annotated[str, Field(min_length=1, max_length=20)]
    color: Annotated[str, Field(min_length=1, max_length=40)]
    quantity: Annotated[int, Field(gt=0, le=1_000_000, strict=True)]


class OrderCreate(Contract):
    reference: Code
    customer: Name
    lines: list[OrderLine] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_variants(self):
        keys = [(x.style_id, x.size, x.color) for x in self.lines]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate order variant; combine its quantity")
        return self


class PurchaseLineInput(Contract):
    material_id: UUID
    quantity: Quantity


class PurchaseCreate(Contract):
    reference: Code
    supplier: Name
    lines: list[PurchaseLineInput] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_materials(self):
        keys = [x.material_id for x in self.lines]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate purchase material")
        return self


class PurchaseReceipt(Contract):
    reference: Code
    lines: list[PurchaseLineInput] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_materials(self):
        keys = [x.material_id for x in self.lines]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate receipt material")
        return self
