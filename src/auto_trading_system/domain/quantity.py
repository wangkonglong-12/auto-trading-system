"""Integer quantity and lot-size value objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from auto_trading_system.domain.errors import DomainValueError

MAX_STORED_INTEGER = 9_223_372_036_854_775_807


def _validate_integer(value: int, *, field: str, minimum: int) -> int:
    if type(value) is not int:
        raise DomainValueError("INVALID_INTEGER_TYPE", field, "must be an integer")
    if value < minimum or value > MAX_STORED_INTEGER:
        raise DomainValueError(
            "INTEGER_OUT_OF_RANGE",
            field,
            f"must be between {minimum} and {MAX_STORED_INTEGER}",
        )
    return value


@dataclass(frozen=True, slots=True, order=True)
class LotSize:
    """Positive lot unit supplied by a versioned market-rule policy."""

    value: int

    def __post_init__(self) -> None:
        _validate_integer(self.value, field="lot_size", minimum=1)


@dataclass(frozen=True, slots=True, order=True)
class Quantity:
    """Non-negative whole-share quantity; exchange rules remain separate policies."""

    value: int

    def __post_init__(self) -> None:
        _validate_integer(self.value, field="quantity", minimum=0)

    @classmethod
    def parse(cls, text: str) -> Self:
        if not isinstance(text, str) or not text or (len(text) > 1 and text.startswith("0")):
            raise DomainValueError(
                "INVALID_INTEGER_TEXT", "quantity", "must be a canonical unsigned integer string"
            )
        if not text.isascii() or not text.isdecimal():
            raise DomainValueError(
                "INVALID_INTEGER_TEXT", "quantity", "must be a canonical unsigned integer string"
            )
        return cls(int(text))

    @property
    def is_zero(self) -> bool:
        return self.value == 0

    def is_multiple_of(self, lot_size: LotSize) -> bool:
        return self.value % lot_size.value == 0

    def add(self, other: Quantity) -> Quantity:
        return Quantity(self.value + other.value)

    def subtract(self, other: Quantity) -> Quantity:
        return Quantity(self.value - other.value)

    def to_wire(self) -> int:
        return self.value
