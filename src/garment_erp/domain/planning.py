from collections import defaultdict
from decimal import ROUND_CEILING, Decimal

from garment_erp.domain.errors import BusinessError

SCALE = 1000


def to_units(quantity: Decimal, unit: str) -> int:
    """Store quantities as integer milliunits; pieces must be integral."""
    scaled = quantity * SCALE
    if quantity <= 0 or scaled != scaled.to_integral_value():
        raise BusinessError("invalid_quantity", "Quantity must be positive with <=3 decimals", 422)
    if unit == "pcs" and quantity != quantity.to_integral_value():
        raise BusinessError("fractional_pieces", "Pieces must be whole numbers", 422)
    return int(scaled)


def display_units(value: int) -> str:
    return format(Decimal(value) / SCALE, ".3f")


def calculate_requirements(lines: list[tuple[int, list[dict], Decimal]]) -> dict[str, int]:
    """Aggregate before rounding up. BOM components already use milliunits.

    Waste is a percentage of the base requirement. No floating point arithmetic.
    """
    totals = defaultdict(Decimal)
    for count, components, waste in lines:
        if count <= 0 or waste < 0 or waste > 100 or not components:
            raise BusinessError("invalid_recipe", "Invalid quantity or recipe", 422)
        for component in components:
            totals[component["material_id"]] += (
                Decimal(component["quantity_units"]) * count * (100 + waste) / 100
            )
    return {
        material_id: int(quantity.to_integral_value(rounding=ROUND_CEILING))
        for material_id, quantity in totals.items()
    }
