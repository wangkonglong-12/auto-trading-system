"""Independent fictional broker facts, durable before callbacks, never a live ledger."""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from uuid import uuid4

from pydantic import Field, model_validator

from auto_trading_system.gateways.broker.models import BrokerOrder, BrokerTrade
from auto_trading_system.gateways.broker.query_validation import (
    validate_order_query_result,
    validate_trade_query_result,
)
from auto_trading_system.protocol.v2.base import WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes


class PaperBrokerState(WireModel):
    account_id: str = Field(description="模拟账户别名")
    initial_cash: str = Field(description="不可变的模拟初始现金")
    request_sequence: int = Field(ge=1, description="下一个请求编号")
    order_sequence: int = Field(ge=1, description="下一个模拟券商委托编号")
    trade_sequence: int = Field(ge=1, description="下一个模拟成交编号")
    orders: tuple[BrokerOrder, ...] = Field(max_length=2048, description="模拟委托独立事实")
    trades: tuple[BrokerTrade, ...] = Field(max_length=4096, description="模拟成交独立事实")

    @model_validator(mode="after")
    def consistent(self) -> PaperBrokerState:
        validate_order_query_result(self.orders)
        validate_trade_query_result(self.trades)
        if self.order_sequence <= len(self.orders) or self.trade_sequence <= len(self.trades):
            raise ValueError("paper broker sequences cannot move backwards")
        if self.request_sequence < self.order_sequence:
            raise ValueError("paper request sequence cannot precede orders")
        orders = {order.client_order_id: order for order in self.orders}
        filled: Counter[str] = Counter()
        for order in self.orders:
            self._sequence(order.broker_order_id or "", "MOCK", self.order_sequence)
        for trade in self.trades:
            self._sequence(trade.trade_id, "trade_MOCK", self.trade_sequence)
            matched = orders.get(trade.client_order_id)
            if matched is None or (trade.broker_order_id, trade.symbol, trade.side) != (
                matched.broker_order_id,
                matched.symbol,
                matched.side,
            ):
                raise ValueError("paper fill has no matching order")
            filled[trade.client_order_id] += trade.quantity.value
        if any(filled[o.client_order_id] != o.filled_quantity.value for o in self.orders):
            raise ValueError("paper fills disagree with order totals")
        return self

    @staticmethod
    def _sequence(value: str, prefix: str, next_value: int) -> None:
        suffix = value.removeprefix(prefix)
        if not value.startswith(prefix) or len(suffix) != 16 or not suffix.isdecimal():
            raise ValueError("invalid fictional broker identity")
        if not 0 < int(suffix) < next_value:
            raise ValueError("paper broker sequence would reuse an identity")


class PaperStateFile:
    def __init__(self, path: Path, account_id: str, initial_cash: str) -> None:
        self.path, self.account_id, self.initial_cash = path, account_id, initial_cash

    def load(self) -> PaperBrokerState | None:
        if not self.path.exists():
            return None
        self._direct()
        with self.path.open("rb") as source:
            body = source.read(8 * 1024 * 1024 + 1)
        if len(body) > 8 * 1024 * 1024:
            raise ValueError("paper broker state exceeds capacity")
        state = PaperBrokerState.model_validate_json(body)
        if (state.account_id, state.initial_cash) != (self.account_id, self.initial_cash):
            raise ValueError("paper broker identity or initial cash changed")
        return state

    def _direct(self) -> None:
        if self.path.is_symlink() or self.path.resolve() != self.path.absolute():
            raise ValueError("paper state path must be direct")
        if self.path.exists() and (not self.path.is_file() or self.path.stat().st_nlink != 1):
            raise ValueError("paper state must be a single regular file")

    def save(self, state: PaperBrokerState) -> None:
        self._direct()
        if (state.account_id, state.initial_cash) != (self.account_id, self.initial_cash):
            raise ValueError("paper broker identity or initial cash changed")
        body = canonical_bytes(state.model_dump(mode="json"))
        if len(body) > 8 * 1024 * 1024:
            raise ValueError("paper broker state exceeds capacity")
        partial = self.path.with_name(".pending-paper-" + uuid4().hex)
        with partial.open("xb") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.replace(partial, self.path)
