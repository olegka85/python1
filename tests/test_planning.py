from decimal import Decimal

import pytest

from garment_erp.domain.errors import BusinessError
from garment_erp.domain.planning import calculate_requirements, display_units, to_units


def test_requirements_aggregate_before_rounding():
    component = [{"material_id": "cotton", "quantity_units": 1}]
    result = calculate_requirements([(1, component, Decimal(5)), (1, component, Decimal(5))])
    assert result == {"cotton": 3}
    assert calculate_requirements(
        [(3, [{"material_id": "cotton", "quantity_units": 333}], Decimal(5))]
    ) == {"cotton": 1049}


@pytest.mark.parametrize(
    "quantity,unit", [("0", "m"), ("-1", "m"), ("0.0001", "m"), ("1.5", "pcs")]
)
def test_invalid_quantities(quantity, unit):
    with pytest.raises(BusinessError):
        to_units(Decimal(quantity), unit)


def test_milliunits_round_trip():
    assert to_units(Decimal("140.800"), "m") == 140800
    assert display_units(140800) == "140.800"
