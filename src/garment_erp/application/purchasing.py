from garment_erp.application.contracts import PurchaseCreate, PurchaseReceipt
from garment_erp.application.ports import FactoryRepository
from garment_erp.domain.errors import BusinessError
from garment_erp.domain.planning import display_units, to_units


def purchase_view(purchase: dict) -> dict:
    return {
        **{k: purchase[k] for k in ("id", "reference", "supplier", "state")},
        "lines": [
            {
                "material_id": x["material_id"],
                "ordered": display_units(x["ordered"]),
                "received": display_units(x["received"]),
            }
            for x in purchase["lines"]
        ],
    }


class PurchasingService:
    def __init__(self, repo: FactoryRepository):
        self.repo = repo

    async def create(self, command: PurchaseCreate) -> dict:
        lines = []
        for line in command.lines:
            material = await self.repo.material(str(line.material_id))
            lines.append(
                {
                    "material_id": material["id"],
                    "ordered": to_units(line.quantity, material["unit"]),
                }
            )
        purchase = await self.repo.create_purchase(command.reference, command.supplier, lines)
        await self.repo.emit("purchase.created", purchase["id"], {"reference": command.reference})
        return purchase_view(purchase)

    async def receive(self, purchase_id: str, command: PurchaseReceipt) -> dict:
        await self.repo.purchase(purchase_id)
        if not await self.repo.claim_purchase(purchase_id):
            raise BusinessError("invalid_state", "Purchase is already fully received")
        for line in sorted(command.lines, key=lambda x: str(x.material_id)):
            material = await self.repo.material(str(line.material_id))
            quantity = to_units(line.quantity, material["unit"])
            if not await self.repo.receive_purchase_line(purchase_id, material["id"], quantity):
                raise BusinessError(
                    "over_receipt",
                    "Material is missing from purchase or quantity exceeds outstanding",
                )
            await self.repo.adjust_stock(material["id"], stock=quantity)
            await self.repo.movement(
                material["id"], "purchase_receipt", command.reference, quantity, 0
            )
        purchase = await self.repo.purchase(purchase_id)
        state = (
            "RECEIVED"
            if all(x["received"] == x["ordered"] for x in purchase["lines"])
            else "PARTIALLY_RECEIVED"
        )
        await self.repo.set_purchase_state(purchase_id, state)
        await self.repo.emit(
            "purchase.received", purchase_id, {"reference": command.reference, "state": state}
        )
        return purchase_view(await self.repo.purchase(purchase_id))
