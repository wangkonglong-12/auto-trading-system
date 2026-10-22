"""Canonical Chinese-market security identifier."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from auto_trading_system.domain.errors import DomainValueError

_SECURITY_CODE_PATTERN = re.compile(r"(?P<digits>[0-9]{6})\.(?P<market>SZ|SH|BJ)")


class Market(StrEnum):
    """Market suffix used by TradePlan and QMT security identifiers."""

    SHANGHAI = "SH"
    SHENZHEN = "SZ"
    BEIJING = "BJ"


@dataclass(frozen=True, slots=True)
class SecurityCode:
    """A strict six-digit code and explicit market suffix."""

    digits: str
    market: Market

    def __post_init__(self) -> None:
        if not isinstance(self.digits, str) or re.fullmatch(r"[0-9]{6}", self.digits) is None:
            raise DomainValueError(
                "INVALID_SECURITY_CODE", "security_code", "code must contain six ASCII digits"
            )
        if not isinstance(self.market, Market):
            raise DomainValueError(
                "INVALID_SECURITY_MARKET", "security_code", "market must be SH, SZ, or BJ"
            )

    @classmethod
    def parse(cls, text: str) -> Self:
        if not isinstance(text, str):
            raise DomainValueError(
                "INVALID_SECURITY_CODE", "security_code", "must be a canonical string"
            )
        match = _SECURITY_CODE_PATTERN.fullmatch(text)
        if match is None:
            raise DomainValueError(
                "INVALID_SECURITY_CODE",
                "security_code",
                "must match six ASCII digits followed by .SH, .SZ, or .BJ",
            )
        return cls(match.group("digits"), Market(match.group("market")))

    def to_wire(self) -> str:
        return f"{self.digits}.{self.market.value}"

    def __str__(self) -> str:
        return self.to_wire()
