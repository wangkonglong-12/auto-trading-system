"""Small, immutable cost-based exit policy for one strategy-owned acquisition lot."""

from __future__ import annotations

from datetime import time
from decimal import Decimal

from pydantic import Field, field_validator, model_validator

from auto_trading_system.protocol.v2.base import WireModel


class SellRules(WireModel):
    """Percent values are human percentages (7 means 7%), never fractions."""

    take_profit_pct: Decimal = Field(gt=0, le=100, max_digits=5, decimal_places=2)
    stop_loss_pct: Decimal = Field(gt=0, lt=100, max_digits=5, decimal_places=2)
    sell_by: time = Field(description="Asia/Shanghai trading-day deadline, HH:MM")

    @field_validator("sell_by")
    @classmethod
    def minute_only(cls, value: time) -> time:
        if value.tzinfo is not None or value.second or value.microsecond:
            raise ValueError("sell-by time must be HH:MM in China market time")
        return value

    @model_validator(mode="after")
    def market_window(self) -> SellRules:
        # The owner stops new children at 14:55:30. Leave room for broker response.
        if not (
            time(9, 30) <= self.sell_by <= time(11, 25) or time(13) <= self.sell_by <= time(14, 50)
        ):
            raise ValueError("sell-by time must leave order time in an open market session")
        return self
