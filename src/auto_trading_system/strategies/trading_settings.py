"""Simple operator-facing trading settings with fail-closed local ceilings."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from auto_trading_system.protocol.v2.base import WireModel
from auto_trading_system.protocol.v2.records import MoneyText
from auto_trading_system.strategies.sell_rules import SellRules


class BuySettings(WireModel):
    order_method: Literal["ASK1_PROTECTED_LIMIT"] = Field(
        default="ASK1_PROTECTED_LIMIT", description="最新卖一价保护限价单"
    )
    max_daily_buy_value: MoneyText = Field(description="当日累计买入硬上限，0表示禁止自动买入")
    max_symbol_buy_value: MoneyText = Field(description="单只股票累计买入硬上限")
    max_order_value: MoneyText = Field(description="单笔委托金额硬上限")
    cancel_after_seconds: int = Field(ge=1, le=300, description="未成交后申请撤单的秒数")
    max_retries: int = Field(ge=0, le=3, description="确认撤单后最多重新下单次数")

    @model_validator(mode="after")
    def ceilings(self) -> BuySettings:
        daily = Decimal(self.max_daily_buy_value)
        symbol = Decimal(self.max_symbol_buy_value)
        order = Decimal(self.max_order_value)
        if any(value == 0 for value in (daily, symbol, order)):
            if any(value != 0 for value in (daily, symbol, order)):
                raise PydanticCustomError("buy_limits_incomplete", "buy limits incomplete")
        elif not order <= symbol <= daily:
            raise PydanticCustomError("buy_limits_order", "buy limits order invalid")
        return self

    @property
    def enabled(self) -> bool:
        return Decimal(self.max_daily_buy_value) > 0


class SafetySettings(WireModel):
    max_daily_loss: MoneyText = Field(description="单日最大亏损硬阈值，0表示尚未配置")
    max_positions: int = Field(ge=0, le=15, description="最多同时持有的股票数量")
    emergency_liquidation_enabled: bool = Field(description="是否允许操作员使用紧急清仓")


class AdvancedExecutionSettings(WireModel):
    max_order_quantity: int = Field(ge=100, le=1_000_000, description="单笔最大股数")
    max_chase_bps: int = Field(ge=0, le=1000, description="相对参考价最大追价，基点")
    max_spread_bps: int = Field(ge=0, le=1000, description="允许的最大买卖价差，基点")
    quote_max_age_ms: int = Field(ge=100, le=5000, description="行情最大允许延迟，毫秒")


class TradingSettings(WireModel):
    buy: BuySettings
    sell: SellRules
    safety: SafetySettings
    advanced: AdvancedExecutionSettings

    @model_validator(mode="after")
    def usable_limits(self) -> TradingSettings:
        if self.buy.enabled and (
            self.safety.max_positions == 0 or Decimal(self.safety.max_daily_loss) == 0
        ):
            raise PydanticCustomError("buy_safety_required", "buy safety limits required")
        return self
