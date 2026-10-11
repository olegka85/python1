from garment_erp.application.contracts import MaterialCreate, StockReceipt, StyleCreate
from garment_erp.application.ports import FactoryRepository
from garment_erp.domain.planning import display_units, to_units


def material_view(material: dict) -> dict:
    return {
        "id": material["id"],
        "sku": material["sku"],
        "name": material["name"],
        "unit": material["unit"],
        "on_hand": display_units(material["on_hand"]),
        "reserved": display_units(material["reserved"]),
        "available": display_units(material["on_hand"] - material["reserved"]),
    }


def style_view(style: dict) -> dict:
    return {
        **{k: style[k] for k in ("id", "sku", "name", "waste_percent")},
        "bom": [
            {
                "size": x["size"],
                "color": x["color"],
                "material_id": x["material_id"],
                "quantity": display_units(x["quantity_units"]),
            }
            for x in style["bom"]
        ],
    }


class CatalogService:
    def __init__(self, repo: FactoryRepository):
        self.repo = repo

    async def create_material(self, command: MaterialCreate) -> dict:
        material = await self.repo.create_material(command.sku, command.name, command.unit)
        await self.repo.emit("material.created", material["id"], {"sku": command.sku})
        return material_view(material)

    async def receive_stock(self, material_id: str, command: StockReceipt) -> dict:
        material = await self.repo.material(material_id)
        quantity = to_units(command.quantity, material["unit"])
        await self.repo.adjust_stock(material_id, stock=quantity)
        await self.repo.movement(material_id, "opening_receipt", command.reference, quantity, 0)
        await self.repo.emit(
            "inventory.received",
            material_id,
            {"quantity_units": quantity, "reference": command.reference},
        )
        return material_view(await self.repo.material(material_id))

    async def create_style(self, command: StyleCreate) -> dict:
        bom = []
        for line in command.bom:
            material = await self.repo.material(str(line.material_id))
            bom.append(
                {
                    "material_id": material["id"],
                    "size": line.size,
                    "color": line.color,
                    "quantity_units": to_units(line.quantity, material["unit"]),
                }
            )
        style = await self.repo.create_style(command.sku, command.name, command.waste_percent, bom)
        await self.repo.emit("style.created", style["id"], {"sku": command.sku})
        return style_view(style)
