"""Explicit fictional session inputs for the normal MOCK process entrypoint."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from auto_trading_system.protocol.v2.base import WireModel
from auto_trading_system.protocol.v2.records import PriceText, Symbol
from auto_trading_system.reconcile.portfolio_checkpoint import OpeningReference
from auto_trading_system.runtime.engine_v2 import EngineLimits
from auto_trading_system.strategies.entry_state import EntryPolicy
from auto_trading_system.strategies.exit_context import SignedExitContext


class PaperSession(WireModel):
    label: Literal["FICTIONAL_ACCEPTANCE_ONLY"] = Field(description="明确的离线模拟标记")
    as_of: AwareDatetime = Field(description="固定模拟时刻；切换时停止并重启进程")
    phase: Literal["ENTRY", "EXIT"] = Field(description="本次进程执行的测试阶段")
    limits: EngineLimits = Field(description="仅用于模拟券商的本地额度")
    prices: dict[Symbol, PriceText] = Field(
        min_length=1, max_length=15, description="明确给定的模拟买卖一价，绝非真实行情"
    )
    entry_policy: EntryPolicy | None = Field(default=None, description="测试买入规则")
    opening: OpeningReference | None = Field(default=None, description="前日已封存的持仓引用")
    exit_context: SignedExitContext | None = Field(default=None, description="签名测试退出上下文")

    @model_validator(mode="after")
    def consistent(self) -> PaperSession:
        if self.as_of.utcoffset() != timedelta(hours=8):
            raise ValueError("paper time must explicitly use Asia/Shanghai offset")
        if self.limits.mode != "PAPER" or any(Decimal(p) <= 0 for p in self.prices.values()):
            raise ValueError("paper session requires positive prices and PAPER limits")
        if (
            Decimal(self.limits.max_buy_value) > 100000
            or Decimal(self.limits.max_order_value) > 100000
            or self.limits.max_order_quantity > 10000
        ):
            raise ValueError("fictional acceptance limits exceeded")
        if self.phase == "ENTRY":
            if self.entry_policy is None or self.exit_context is not None:
                raise ValueError("ENTRY requires a policy without an exit context")
            if set(self.prices) != {p.symbol for p in self.entry_policy.instruments}:
                raise ValueError("paper prices and entry instruments differ")
        elif self.exit_context is None or self.opening is None or self.entry_policy is not None:
            raise ValueError("EXIT requires signed context and verified opening")
        return self
