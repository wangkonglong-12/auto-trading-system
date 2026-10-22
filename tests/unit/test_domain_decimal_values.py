from __future__ import annotations

from decimal import Decimal

import pytest

from auto_trading_system.domain import DomainValueError, Money, Price, Ratio


@pytest.mark.parametrize(
    ("text", "expected"),
    [("0", "0"), ("12.30", "12.3"), ("999999999999.99", "999999999999.99")],
)
def test_money_parses_and_canonicalizes_wire_values(text: str, expected: str) -> None:
    money = Money.parse(text)

    assert money.to_wire() == expected
    assert str(money) == expected


@pytest.mark.parametrize("value", [0, 1.5, True, object()])
def test_money_rejects_non_decimal_constructor_values(value: object) -> None:
    with pytest.raises(DomainValueError, match="INVALID_DECIMAL_TYPE") as error:
        Money(value)  # type: ignore[arg-type]

    assert error.value.field == "money"


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_money_rejects_non_finite_values(value: Decimal) -> None:
    with pytest.raises(DomainValueError, match="NON_FINITE_DECIMAL"):
        Money(value)


@pytest.mark.parametrize(
    ("value", "error_code"),
    [
        (Decimal("-0.01"), "DECIMAL_OUT_OF_RANGE"),
        (Decimal("1.001"), "DECIMAL_SCALE_EXCEEDED"),
        (Decimal("1000000000000"), "DECIMAL_PRECISION_EXCEEDED"),
    ],
)
def test_money_enforces_range_scale_and_precision(value: Decimal, error_code: str) -> None:
    with pytest.raises(DomainValueError, match=error_code):
        Money(value)


@pytest.mark.parametrize("text", ["", " 1", "+1", "01", "1.", ".1", "1e2", "NaN", "-0", "-0.00"])
def test_money_wire_parser_is_strict(text: str) -> None:
    with pytest.raises(DomainValueError, match="INVALID_DECIMAL_TEXT"):
        Money.parse(text)


def test_money_arithmetic_preserves_invariants() -> None:
    assert Money.parse("10.25").add(Money.parse("0.75")) == Money.parse("11")
    assert Money.parse("10.25").subtract(Money.parse("0.25")) == Money.parse("10")

    with pytest.raises(DomainValueError, match="DECIMAL_OUT_OF_RANGE"):
        Money.parse("1").subtract(Money.parse("2"))


@pytest.mark.parametrize(("minor_units", "expected"), [(0, "0"), (1, "0.01"), (1234, "12.34")])
def test_money_can_be_constructed_from_integer_minor_units(minor_units: int, expected: str) -> None:
    assert Money.from_minor_units(minor_units).to_wire() == expected


@pytest.mark.parametrize("minor_units", [True, 1.0, "1"])
def test_money_rejects_non_integer_minor_units(minor_units: object) -> None:
    with pytest.raises(DomainValueError, match="INVALID_INTEGER_TYPE"):
        Money.from_minor_units(minor_units)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("0.00000001", "0.00000001"), ("12.34560000", "12.3456"), ("9999999999", "9999999999")],
)
def test_price_parses_positive_protocol_precision(text: str, expected: str) -> None:
    price = Price.parse(text)

    assert price.to_wire() == expected
    assert str(price) == expected


@pytest.mark.parametrize(
    ("value", "error_code"),
    [
        (Decimal("0"), "DECIMAL_OUT_OF_RANGE"),
        (Decimal("-1"), "DECIMAL_OUT_OF_RANGE"),
        (Decimal("1.000000001"), "DECIMAL_SCALE_EXCEEDED"),
        (Decimal("10000000000"), "DECIMAL_PRECISION_EXCEEDED"),
    ],
)
def test_price_enforces_positive_range_and_precision(value: Decimal, error_code: str) -> None:
    with pytest.raises(DomainValueError, match=error_code):
        Price(value)


@pytest.mark.parametrize(("text", "expected"), [("0", "0"), ("0.6", "0.6"), ("1.0", "1")])
def test_ratio_accepts_closed_interval(text: str, expected: str) -> None:
    ratio = Ratio.parse(text)

    assert ratio.to_wire() == expected
    assert str(ratio) == expected


@pytest.mark.parametrize(
    ("value", "error_code"),
    [
        (Decimal("-0.00000001"), "DECIMAL_OUT_OF_RANGE"),
        (Decimal("1.00000001"), "DECIMAL_OUT_OF_RANGE"),
        (Decimal("0.123456789"), "DECIMAL_SCALE_EXCEEDED"),
    ],
)
def test_ratio_enforces_range_and_scale(value: Decimal, error_code: str) -> None:
    with pytest.raises(DomainValueError, match=error_code):
        Ratio(value)


def test_decimal_value_objects_are_orderable_and_immutable() -> None:
    assert Money.parse("1") < Money.parse("2")
    assert Price.parse("1") < Price.parse("2")
    assert Ratio.parse("0.1") < Ratio.parse("0.2")

    with pytest.raises((AttributeError, TypeError)):
        Money.parse("1").amount = Decimal("2")  # type: ignore[misc]
