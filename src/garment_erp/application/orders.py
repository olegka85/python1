from decimal import Decimal

from garment_erp.application.contracts import OrderCreate
from garment_erp.application.ports import FactoryRepository
from garment_erp.domain.errors import BusinessError
from garment_erp.domain.planning import calculate_requirements, display_units


class OrderService:
    def __init__(self, repo: FactoryRepository):
        self.repo = repo

    async def create(self, command: OrderCreate) -> dict:
        inputs = []
        for line in command.lines:
            style = await self.repo.style(str(line.style_id))
            components = [
                x for x in style["bom"] if x["size"] == line.size and x["color"] == line.color
            ]
            if not components:
                raise BusinessError("missing_recipe", "No recipe for requested size/color", 422)
            inputs.append((line.quantity, components, Decimal(style["waste_percent"])))
        requirements = calculate_requirements(inputs)
        # Whole pieces round upward after aggregation, including waste allowance.
        for material_id, quantity in requirements.items():
            if (await self.repo.material(material_id))["unit"] == "pcs":
                requirements[material_id] = ((quantity + 999) // 1000) * 1000
        order = await self.repo.create_order(
            command.reference,
            command.customer,
            [x.model_dump(mode="json") for x in command.lines],
            requirements,
        )
        await self.repo.emit("order.created", order["id"], {"reference": order["reference"]})
        return order

    async def needs(self, order_id: str) -> dict:
        order = await self.repo.order(order_id)
        lines = await self.repo.requirements(order_id)
        items = []
        for line in lines:
            available = line["on_hand"] - line["reserved"]
            own_reserve = (
                line["quantity_units"] if order["state"] in {"RESERVED", "IN_PRODUCTION"} else 0
            )
            shortage = (
                max(0, line["quantity_units"] - available - own_reserve)
                if order["state"] in {"DRAFT", "RESERVED", "IN_PRODUCTION"}
                else 0
            )
            items.append(
                {
                    "material_id": line["material_id"],
                    "sku": line["sku"],
                    "unit": line["unit"],
                    "required": display_units(line["quantity_units"]),
                    "available": display_units(available),
                    "reserved_for_order": display_units(own_reserve),
                    "shortage": display_units(shortage),
                }
            )
        return {"order_id": order_id, "state": order["state"], "materials": items}

    async def reserve(self, order_id: str) -> dict:
        await self.repo.order(order_id)
        if not await self.repo.transition_order(order_id, "DRAFT", "RESERVED"):
            raise BusinessError("invalid_state", "Only a draft order can reserve materials")
        for line in sorted(await self.repo.requirements(order_id), key=lambda x: x["material_id"]):
            if not await self.repo.adjust_stock(
                line["material_id"], reserved=line["quantity_units"]
            ):
                raise BusinessError(
                    "insufficient_stock",
                    "Insufficient available material",
                    details={"material_id": line["material_id"]},
                )
            await self.repo.movement(
                line["material_id"], "reservation", order_id, 0, line["quantity_units"]
            )
        await self.repo.emit("order.reserved", order_id, {})
        return await self.repo.order(order_id)

    async def start(self, order_id: str) -> dict:
        await self.repo.order(order_id)
        if not await self.repo.transition_order(order_id, "RESERVED", "IN_PRODUCTION"):
            raise BusinessError("invalid_state", "Production requires a reserved order")
        await self.repo.emit("production.started", order_id, {})
        return await self.repo.order(order_id)

    async def complete(self, order_id: str) -> dict:
        await self.repo.order(order_id)
        if not await self.repo.transition_order(order_id, "IN_PRODUCTION", "COMPLETED"):
            raise BusinessError("invalid_state", "Only an in-production order can complete")
        for line in sorted(await self.repo.requirements(order_id), key=lambda x: x["material_id"]):
            quantity = line["quantity_units"]
            if not await self.repo.adjust_stock(
                line["material_id"], stock=-quantity, reserved=-quantity
            ):
                raise BusinessError("inventory_invariant", "Reserved stock is inconsistent")
            await self.repo.movement(
                line["material_id"], "consumption", order_id, -quantity, -quantity
            )
        order = await self.repo.order(order_id)
        await self.repo.emit(
            "production.completed", order_id, {"units": sum(x["quantity"] for x in order["lines"])}
        )
        return order

    async def cancel(self, order_id: str) -> dict:
        order = await self.repo.order(order_id)
        if order["state"] not in {"DRAFT", "RESERVED"}:
            raise BusinessError("invalid_state", "Only draft/reserved orders can cancel")
        if not await self.repo.transition_order(order_id, order["state"], "CANCELLED"):
            raise BusinessError("concurrent_change", "Order state changed; reload it")
        if order["state"] == "RESERVED":
            for line in sorted(
                await self.repo.requirements(order_id), key=lambda x: x["material_id"]
            ):
                quantity = line["quantity_units"]
                if not await self.repo.adjust_stock(line["material_id"], reserved=-quantity):
                    raise BusinessError("inventory_invariant", "Reserved stock is inconsistent")
                await self.repo.movement(line["material_id"], "release", order_id, 0, -quantity)
        await self.repo.emit("order.cancelled", order_id, {})
        return await self.repo.order(order_id)
