"""Untruncated, identity-checked SDK reads. No order commands or strategy mapping."""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import TypeVar

from auto_trading_system.protocol.account_monitoring import (
    CN,
    Asset,
    Observation,
    Position,
    Trade,
)

T = TypeVar("T")


def read_observation(
    query: Callable[[str], object],
    account_id: str,
    clock: Callable[[], datetime],
    *,
    buy: int,
    sell: int,
) -> Observation:
    def field(row: object, key: str, kind: type[T]) -> T:
        value = getattr(row, key, None)
        if type(value) is not kind:
            raise ValueError("invalid account observation field")
        return value

    def identity(row: object) -> None:
        if field(row, "account_id", str) != account_id or field(row, "account_type", int) != 2:
            raise ValueError("account observation identity conflict")

    def amount(row: object, key: str, places: str = "0.01") -> str:
        value = field(row, key, float)
        if not math.isfinite(value) or value < 0:
            raise ValueError("invalid account observation amount")
        return format(Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_HALF_UP), "f")

    def rows(name: str) -> list[object]:
        value = query(name)
        if not isinstance(value, list) or len(value) > 10000:
            raise ValueError("incomplete account observation")
        for row in value:
            identity(row)
        return value

    started = clock()
    asset = query("query_stock_asset")
    identity(asset)
    asset_at = clock()
    positions = tuple(
        Position(
            symbol=field(p, "stock_code", str),
            quantity=field(p, "volume", int),
            sellable_quantity=field(p, "can_use_volume", int),
            frozen_quantity=field(p, "frozen_volume", int),
            market_value=amount(p, "market_value"),
        )
        for p in rows("query_stock_positions")
    )
    positions_at = clock()
    trades = []
    for row in rows("query_stock_trades"):
        side = field(row, "order_type", int)
        if side not in (buy, sell):
            raise ValueError("unsupported broker trade side")
        trades.append(
            Trade(
                trade_id=field(row, "traded_id", str),
                symbol=field(row, "stock_code", str),
                side="BUY" if side == buy else "SELL",
                quantity=field(row, "traded_volume", int),
                price=amount(row, "traded_price", "0.00000001"),
                amount=amount(row, "traded_amount"),
                traded_at=datetime.fromtimestamp(field(row, "traded_time", int), CN),
            )
        )
    captured = clock()
    if not 0 <= (captured - started).total_seconds() <= 5:
        raise ValueError("account query window too wide")
    return Observation(
        asset_observed_at=asset_at,
        positions_observed_at=positions_at,
        captured_at=captured,
        asset=Asset(
            total_asset=amount(asset, "total_asset"),
            available_cash=amount(asset, "cash"),
            frozen_cash=amount(asset, "frozen_cash"),
            market_value=amount(asset, "market_value"),
        ),
        positions=positions,
        trades=tuple(trades),
    )
