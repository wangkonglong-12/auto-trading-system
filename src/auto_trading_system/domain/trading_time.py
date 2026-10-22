"""Trading-date and Asia/Shanghai timestamp boundaries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Self
from zoneinfo import ZoneInfo

from auto_trading_system.domain.errors import DomainValueError

SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
SHANGHAI_UTC_OFFSET = timedelta(hours=8)
_ISO_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_SHANGHAI_TIMESTAMP_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?\+08:00"
)


@dataclass(frozen=True, slots=True, order=True)
class TradingDate:
    """Calendar date selected for a plan; exchange-open checks belong to a calendar policy."""

    value: date

    def __post_init__(self) -> None:
        if type(self.value) is not date:
            raise DomainValueError("INVALID_TRADING_DATE_TYPE", "trading_date", "must be a date")

    @classmethod
    def parse(cls, text: str) -> Self:
        if not isinstance(text, str) or _ISO_DATE_PATTERN.fullmatch(text) is None:
            raise DomainValueError("INVALID_TRADING_DATE", "trading_date", "must use YYYY-MM-DD")
        try:
            return cls(date.fromisoformat(text))
        except ValueError as exc:
            raise DomainValueError(
                "INVALID_TRADING_DATE", "trading_date", "must be a valid calendar date"
            ) from exc

    def at(self, local_time: time) -> ShanghaiTimestamp:
        if not isinstance(local_time, time) or local_time.tzinfo is not None:
            raise DomainValueError(
                "INVALID_LOCAL_TIME", "local_time", "must be a timezone-naive time"
            )
        return ShanghaiTimestamp(datetime.combine(self.value, local_time, tzinfo=SHANGHAI_TIMEZONE))

    def to_wire(self) -> str:
        return self.value.isoformat()

    def __str__(self) -> str:
        return self.to_wire()


@dataclass(frozen=True, slots=True, order=True)
class ShanghaiTimestamp:
    """Timezone-aware instant normalized to the Asia/Shanghai timezone."""

    value: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.value, datetime) or self.value.utcoffset() is None:
            raise DomainValueError(
                "NAIVE_TIMESTAMP", "timestamp", "must include an explicit timezone"
            )
        object.__setattr__(self, "value", self.value.astimezone(SHANGHAI_TIMEZONE))

    @classmethod
    def parse(cls, text: str) -> Self:
        """Parse a wire timestamp and require the protocol's explicit +08:00 offset."""
        if not isinstance(text, str):
            raise DomainValueError("INVALID_TIMESTAMP", "timestamp", "must be an ISO 8601 string")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise DomainValueError(
                "INVALID_TIMESTAMP", "timestamp", "must be a valid ISO 8601 timestamp"
            ) from exc
        if parsed.utcoffset() != SHANGHAI_UTC_OFFSET:
            raise DomainValueError(
                "INVALID_TIMEZONE_OFFSET", "timestamp", "wire value must use +08:00"
            )
        if _SHANGHAI_TIMESTAMP_PATTERN.fullmatch(text) is None:
            raise DomainValueError(
                "INVALID_TIMESTAMP", "timestamp", "must use canonical ISO 8601 format"
            )
        return cls(parsed)

    @classmethod
    def from_instant(cls, value: datetime) -> Self:
        """Normalize any timezone-aware internal instant to Asia/Shanghai."""
        return cls(value)

    @property
    def trading_date(self) -> TradingDate:
        return TradingDate(self.value.date())

    def to_wire(self) -> str:
        return self.value.isoformat()

    def __str__(self) -> str:
        return self.to_wire()
