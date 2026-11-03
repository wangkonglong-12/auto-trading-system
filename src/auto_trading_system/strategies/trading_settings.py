"""Simple operator-facing trading settings with fail-closed local ceilings."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from auto_trading_system.protocol.v2.base import WireModel
from auto_trading_system.protocol.v2.records import MoneyText
from auto_trading_system.strategies.sell_rules import SellRules

TradingBoard = Literal["MAIN", "CHINEXT", "STAR"]


def stock_board(symbol: str) -> TradingBoard | None:
    """Classify exchange-qualified equity codes; other instruments fail closed."""
    if not re.fullmatch(r"[0-9]{6}\.(SH|SZ)", symbol):
        return None
    code, market = symbol.split(".")
    if market == "SH":
        if code.startswith("60"):
            return "MAIN"
        if code.startswith(("688", "689")):
            return "STAR"
    if market == "SZ":
        if "000001" <= code <= "004999":
            return "MAIN"
        if code.startswith("30"):
            return "CHINEXT"
    return None


class BuySettings(WireModel):
    allowed_boards: tuple[TradingBoard, ...] = Field(
        default=("MAIN",),
        max_length=3,
        exclude_if=lambda value: value == ("MAIN",),
        description="尾盘可买板块；空列表禁止新增买入；默认主板省略以兼容旧设置摘要",
    )
    signal_mode: Literal["TAIL_EXCELLENT_3_PHASES"] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    order_method: Literal["ASK1_PROTECTED_LIMIT"] = Field(
        default="ASK1_PROTECTED_LIMIT", description="最新卖一价保护限价单"
    )
    max_daily_buy_value: MoneyText = Field(description="当日累计买入硬上限，0表示禁止自动买入")
    max_symbol_buy_value: MoneyText = Field(description="单只股票累计买入硬上限")
    max_order_value: MoneyText = Field(description="单笔委托金额硬上限")
    cancel_after_seconds: int = Field(
        default=5, ge=1, le=300, description="买入未成交后申请撤单的秒数"
    )
    max_retries: int = Field(ge=0, le=3, description="确认撤单后最多重新下单次数")
    max_daily_symbols: int | None = Field(
        default=None,
        ge=1,
        le=15,
        exclude_if=lambda value: value is None,
        description="当日最多买入股票只数；委托中占用名额",
    )

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

    @field_validator("allowed_boards")
    @classmethod
    def distinct_boards(cls, value: tuple[TradingBoard, ...]) -> tuple[TradingBoard, ...]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate trading board")
        ordered: tuple[TradingBoard, ...] = ("MAIN", "CHINEXT", "STAR")
        return tuple(board for board in ordered if board in value)

    @property
    def enabled(self) -> bool:
        return Decimal(self.max_daily_buy_value) > 0


class SafetySettings(WireModel):
    max_total_position_pct: MoneyText = Field(
        default="70", description="账户总仓位百分比上限，含在途买单"
    )
    max_daily_loss: MoneyText = Field(description="单日最大亏损硬阈值，0表示尚未配置")
    max_positions: int = Field(ge=0, le=15, description="最多同时持有的股票数量")
    emergency_liquidation_enabled: bool = Field(description="是否允许操作员使用紧急清仓")

    @model_validator(mode="after")
    def position_limit(self) -> SafetySettings:
        if not Decimal("0") < Decimal(self.max_total_position_pct) <= Decimal("100"):
            raise PydanticCustomError("position_pct_invalid", "position percentage invalid")
        return self


class AdvancedExecutionSettings(WireModel):
    max_order_quantity: int = Field(ge=100, le=1_000_000, description="单笔最大股数")
    max_chase_bps: int = Field(ge=0, le=1000, description="相对参考价最大追价，基点")
    max_spread_bps: int = Field(ge=0, le=1000, description="允许的最大买卖价差，基点")
    quote_max_age_ms: int = Field(ge=100, le=5000, description="行情最大允许延迟，毫秒")


class SellExecutionSettings(WireModel):
    cancel_after_seconds: int = Field(
        default=5, ge=1, le=300, description="卖出未成交后申请撤单的秒数"
    )


class TradingSettings(WireModel):
    buy: BuySettings
    sell: SellRules | None = None
    sell_execution: SellExecutionSettings = Field(default_factory=SellExecutionSettings)
    safety: SafetySettings
    advanced: AdvancedExecutionSettings

    @model_validator(mode="after")
    def usable_limits(self) -> TradingSettings:
        if self.buy.enabled and (
            self.safety.max_positions == 0 or Decimal(self.safety.max_daily_loss) == 0
        ):
            raise PydanticCustomError("buy_safety_required", "buy safety limits required")
        return self
