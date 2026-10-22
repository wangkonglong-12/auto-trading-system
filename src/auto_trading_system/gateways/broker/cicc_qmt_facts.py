"""把真实 QMT 账户查询转换为只读事实；此模块没有报单或撤单入口。"""

from __future__ import annotations

import hmac
import math
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import uuid4

from auto_trading_system.domain import SecurityCode
from auto_trading_system.gateways.broker.cicc_qmt_snapshot import CiccQmtSnapshotClient
from auto_trading_system.monitoring.qmt_readonly import QmtReadOnlyAdapterError
from auto_trading_system.runtime.recovery_v2 import AccountFact, PositionFact

_CENT = Decimal("0.01")
_MAX_ROWS = 10_000


@dataclass(frozen=True, slots=True, repr=False)
class QmtAccountFacts:
    """真实资产及持仓，委托和成交仅计数，尚未完成归属映射。"""

    account: AccountFact
    order_count: int
    trade_count: int


def _field(record: object, name: str, expected: type[Any]) -> Any:
    try:
        value = getattr(record, name)
    except Exception as exc:
        raise QmtReadOnlyAdapterError("QMT_FACT_FIELD_MISSING", "required field missing") from exc
    if type(value) is not expected:
        raise QmtReadOnlyAdapterError("QMT_FACT_FIELD_INVALID", "field type invalid")
    return value


def _money(record: object, name: str) -> str:
    value: float = _field(record, name, float)
    if not math.isfinite(value) or value < 0:
        raise QmtReadOnlyAdapterError("QMT_FACT_MONEY_INVALID", "money value invalid")
    amount = Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)
    if amount >= Decimal("1000000000000"):
        raise QmtReadOnlyAdapterError("QMT_FACT_MONEY_INVALID", "money value too large")
    return format(amount, ".2f")


def _rows(value: object, dataset: str) -> list[object]:
    if not isinstance(value, list) or len(value) > _MAX_ROWS:
        # None is an unknown query result, never evidence of an empty account.
        raise QmtReadOnlyAdapterError(f"QMT_FACT_{dataset}_INVALID", "query result invalid")
    return value


class CiccQmtFactsClient(CiccQmtSnapshotClient):
    """SDK lifecycle is inherited from the proven snapshot client."""

    def read_facts(self, account_alias: str, observed_at: datetime) -> QmtAccountFacts:
        if observed_at.tzinfo is None:
            raise ValueError("observed_at must have a time zone")
        asset = self._query_sdk("query_stock_asset", "ASSET")
        if asset is None:
            raise QmtReadOnlyAdapterError("QMT_FACT_ASSET_NONE", "asset query unavailable")
        self._check_identity(asset)

        positions_raw = _rows(
            self._query_sdk("query_stock_positions", "POSITIONS", self._account), "POSITIONS"
        )
        positions: list[PositionFact] = []
        seen_symbols: set[str] = set()
        for row in positions_raw:
            self._check_identity(row)
            symbol: str = _field(row, "stock_code", str)
            quantity: int = _field(row, "volume", int)
            sellable: int = _field(row, "can_use_volume", int)
            frozen: int = _field(row, "frozen_volume", int)
            if (
                symbol in seen_symbols
                or quantity < 0
                or sellable < 0
                or frozen < 0
                or sellable + frozen > quantity
            ):
                raise QmtReadOnlyAdapterError("QMT_FACT_POSITION_INVALID", "position invalid")
            try:
                SecurityCode.parse(symbol)
            except ValueError as exc:
                raise QmtReadOnlyAdapterError(
                    "QMT_FACT_POSITION_INVALID", "position invalid"
                ) from exc
            seen_symbols.add(symbol)
            try:
                positions.append(
                    PositionFact(
                        symbol=symbol,
                        quantity=quantity,
                        sellable=sellable,
                        frozen=frozen,
                        market_value=_money(row, "market_value"),
                    )
                )
            except ValueError as exc:
                raise QmtReadOnlyAdapterError(
                    "QMT_FACT_POSITION_INVALID", "position invalid"
                ) from exc

        orders = _rows(
            self._query_sdk("query_stock_orders", "ORDERS", self._account, False), "ORDERS"
        )
        trades = _rows(self._query_sdk("query_stock_trades", "TRADES", self._account), "TRADES")
        seen_orders: set[int] = set()
        for row in orders:
            self._check_identity(row)
            order_id: int = _field(row, "order_id", int)
            if order_id <= 0 or order_id in seen_orders:
                raise QmtReadOnlyAdapterError("QMT_FACT_ORDER_INVALID", "order invalid")
            seen_orders.add(order_id)
        seen_trades: set[str] = set()
        for row in trades:
            self._check_identity(row)
            trade_id: str = _field(row, "traded_id", str)
            if not trade_id or trade_id in seen_trades:
                raise QmtReadOnlyAdapterError("QMT_FACT_TRADE_INVALID", "trade invalid")
            seen_trades.add(trade_id)

        try:
            account = AccountFact(
                account_id=account_alias,
                snapshot_id="qmt-" + uuid4().hex,
                observed_at=observed_at,
                cash=_money(asset, "cash"),
                cash_frozen=_money(asset, "frozen_cash"),
                market_value=_money(asset, "market_value"),
                total_equity=_money(asset, "total_asset"),
                positions=tuple(positions),
                observed_order_ids=(),
                # QMT does not prove which broker reservations already reduced cash.
                # Until real order-id linkage is implemented, recovery must stay blocked.
                coherent=False,
            )
        except ValueError as exc:
            raise QmtReadOnlyAdapterError("QMT_FACT_ASSET_INVALID", "asset invalid") from exc
        return QmtAccountFacts(account, len(orders), len(trades))

    def _check_identity(self, record: object) -> None:
        account_id: str = _field(record, "account_id", str)
        account_type: int = _field(record, "account_type", int)
        if not hmac.compare_digest(account_id, self._account_id) or account_type != 2:
            raise QmtReadOnlyAdapterError("QMT_FACT_ACCOUNT_MISMATCH", "account mismatch")
