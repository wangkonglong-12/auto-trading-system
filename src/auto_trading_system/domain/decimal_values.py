"""Exact decimal value objects used by money and risk calculations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Self

from auto_trading_system.domain.errors import DomainValueError

_PLAIN_DECIMAL_PATTERN = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")


def _parse_decimal_text(text: str, field: str) -> Decimal:
    if not isinstance(text, str) or _PLAIN_DECIMAL_PATTERN.fullmatch(text) is None:
        raise DomainValueError("INVALID_DECIMAL_TEXT", field, "must be a plain decimal string")
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - guarded by the strict pattern
        raise DomainValueError(
            "INVALID_DECIMAL_TEXT", field, "must be a plain decimal string"
        ) from exc
    if parsed.is_zero() and parsed.is_signed():
        raise DomainValueError(
            "INVALID_DECIMAL_TEXT", field, "negative zero is not a canonical decimal string"
        )
    return parsed


def _canonical_decimal(value: Decimal, field: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise DomainValueError("INVALID_DECIMAL_TYPE", field, "must be Decimal")
    if not value.is_finite():
        raise DomainValueError("NON_FINITE_DECIMAL", field, "must be finite")
    if value.is_zero():
        return Decimal(0)
    return value.normalize()


def _decimal_places(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):  # finite values always have an integer exponent
        raise AssertionError(  # pragma: no cover
            "finite Decimal unexpectedly has a non-integer exponent"
        )
    return max(0, -exponent)


def _integer_digits(value: Decimal) -> int:
    if value.is_zero() or abs(value) < 1:
        return 1
    return value.adjusted() + 1


def _validate_bounded_decimal(
    value: Decimal,
    *,
    field: str,
    decimal_places: int,
    integer_digits: int,
    minimum: Decimal,
    maximum: Decimal | None = None,
    minimum_inclusive: bool = True,
) -> Decimal:
    canonical = _canonical_decimal(value, field)
    below_minimum = canonical < minimum if minimum_inclusive else canonical <= minimum
    if below_minimum:
        comparison = "greater than or equal to" if minimum_inclusive else "greater than"
        raise DomainValueError("DECIMAL_OUT_OF_RANGE", field, f"must be {comparison} {minimum}")
    if maximum is not None and canonical > maximum:
        raise DomainValueError("DECIMAL_OUT_OF_RANGE", field, f"must be at most {maximum}")
    if _decimal_places(value) > decimal_places:
        raise DomainValueError(
            "DECIMAL_SCALE_EXCEEDED", field, f"supports at most {decimal_places} decimal places"
        )
    if _integer_digits(canonical) > integer_digits:
        raise DomainValueError(
            "DECIMAL_PRECISION_EXCEEDED", field, f"supports at most {integer_digits} integer digits"
        )
    return canonical


def _to_plain_text(value: Decimal) -> str:
    return format(value, "f")


@dataclass(frozen=True, slots=True, order=True)
class Money:
    """Non-negative CNY amount with a maximum scale of two decimal places."""

    amount: Decimal

    def __post_init__(self) -> None:
        canonical = _validate_bounded_decimal(
            self.amount,
            field="money",
            decimal_places=2,
            integer_digits=12,
            minimum=Decimal(0),
        )
        object.__setattr__(self, "amount", canonical)

    @classmethod
    def parse(cls, text: str) -> Self:
        """Parse a strict, non-exponent wire value."""
        return cls(_parse_decimal_text(text, "money"))

    @classmethod
    def from_minor_units(cls, minor_units: int) -> Self:
        """Construct an amount from integer fen."""
        if type(minor_units) is not int:
            raise DomainValueError("INVALID_INTEGER_TYPE", "minor_units", "must be an integer")
        return cls(Decimal(minor_units) / Decimal(100))

    def add(self, other: Money) -> Money:
        return Money(self.amount + other.amount)

    def subtract(self, other: Money) -> Money:
        return Money(self.amount - other.amount)

    def to_wire(self) -> str:
        return _to_plain_text(self.amount)

    def __str__(self) -> str:
        return self.to_wire()


@dataclass(frozen=True, slots=True, order=True)
class Price:
    """Strictly positive security price with protocol-compatible precision."""

    amount: Decimal

    def __post_init__(self) -> None:
        canonical = _validate_bounded_decimal(
            self.amount,
            field="price",
            decimal_places=8,
            integer_digits=10,
            minimum=Decimal(0),
            minimum_inclusive=False,
        )
        object.__setattr__(self, "amount", canonical)

    @classmethod
    def parse(cls, text: str) -> Self:
        return cls(_parse_decimal_text(text, "price"))

    def to_wire(self) -> str:
        return _to_plain_text(self.amount)

    def __str__(self) -> str:
        return self.to_wire()


@dataclass(frozen=True, slots=True, order=True)
class Ratio:
    """Inclusive ratio in the closed interval [0, 1]."""

    value: Decimal

    def __post_init__(self) -> None:
        canonical = _validate_bounded_decimal(
            self.value,
            field="ratio",
            decimal_places=8,
            integer_digits=1,
            minimum=Decimal(0),
            maximum=Decimal(1),
        )
        object.__setattr__(self, "value", canonical)

    @classmethod
    def parse(cls, text: str) -> Self:
        return cls(_parse_decimal_text(text, "ratio"))

    def to_wire(self) -> str:
        return _to_plain_text(self.value)

    def __str__(self) -> str:
        return self.to_wire()
