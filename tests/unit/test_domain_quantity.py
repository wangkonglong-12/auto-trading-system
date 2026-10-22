from __future__ import annotations

import pytest

from auto_trading_system.domain import DomainValueError, LotSize, Quantity
from auto_trading_system.domain.quantity import MAX_STORED_INTEGER


@pytest.mark.parametrize("value", [0, 1, 100, MAX_STORED_INTEGER])
def test_quantity_accepts_non_negative_storage_safe_integers(value: int) -> None:
    assert Quantity(value).to_wire() == value


@pytest.mark.parametrize("value", [True, False, 1.0, "1", None])
def test_quantity_rejects_non_integer_types_including_bool(value: object) -> None:
    with pytest.raises(DomainValueError, match="INVALID_INTEGER_TYPE"):
        Quantity(value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [-1, MAX_STORED_INTEGER + 1])
def test_quantity_enforces_storage_range(value: int) -> None:
    with pytest.raises(DomainValueError, match="INTEGER_OUT_OF_RANGE"):
        Quantity(value)


@pytest.mark.parametrize(("text", "expected"), [("0", 0), ("1", 1), ("100", 100)])
def test_quantity_parses_canonical_unsigned_text(text: str, expected: int) -> None:
    assert Quantity.parse(text) == Quantity(expected)


@pytest.mark.parametrize("text", ["", "00", "01", "-1", "+1", " 1", "1.0", "\uff11"])
def test_quantity_rejects_non_canonical_text(text: str) -> None:
    with pytest.raises(DomainValueError, match="INVALID_INTEGER_TEXT"):
        Quantity.parse(text)


def test_quantity_zero_and_lot_multiple_are_explicit() -> None:
    lot = LotSize(100)

    assert Quantity(0).is_zero
    assert Quantity(200).is_multiple_of(lot)
    assert not Quantity(250).is_multiple_of(lot)


@pytest.mark.parametrize("value", [0, -1, MAX_STORED_INTEGER + 1])
def test_lot_size_must_be_positive_and_storage_safe(value: int) -> None:
    with pytest.raises(DomainValueError, match="INTEGER_OUT_OF_RANGE"):
        LotSize(value)


@pytest.mark.parametrize("value", [True, 1.0, "100"])
def test_lot_size_rejects_non_integer_types(value: object) -> None:
    with pytest.raises(DomainValueError, match="INVALID_INTEGER_TYPE"):
        LotSize(value)  # type: ignore[arg-type]


def test_quantity_arithmetic_cannot_underflow_or_overflow() -> None:
    assert Quantity(100).add(Quantity(50)) == Quantity(150)
    assert Quantity(100).subtract(Quantity(50)) == Quantity(50)

    with pytest.raises(DomainValueError, match="INTEGER_OUT_OF_RANGE"):
        Quantity(1).subtract(Quantity(2))
    with pytest.raises(DomainValueError, match="INTEGER_OUT_OF_RANGE"):
        Quantity(MAX_STORED_INTEGER).add(Quantity(1))
