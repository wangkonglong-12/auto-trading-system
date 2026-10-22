"""Dedicated XtQuant command transport for controlled QMT field integration.

This class is deliberately separate from QMT_READ_ONLY. An async sequence only
means the SDK accepted a request; callers must reconcile callbacks and queries
before treating an order or cancellation as completed.
"""

from __future__ import annotations

import importlib
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from queue import Empty, Queue
from threading import Event
from typing import Any

from auto_trading_system.gateways.broker.cicc_qmt import XtquantBindings, load_xtquant_bindings
from auto_trading_system.gateways.broker.models import CancelOrder, OrderSide, SubmitOrder
from auto_trading_system.monitoring.qmt_readonly import QmtProbeConfig


class QmtCommandError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class QmtConstants:
    stock_buy: int
    stock_sell: int
    fixed_price: int

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value <= 0
            for value in (self.stock_buy, self.stock_sell, self.fixed_price)
        ):
            raise QmtCommandError("QMT_CONSTANTS_INVALID")

    @classmethod
    def from_sdk(cls) -> QmtConstants:
        try:
            module = importlib.import_module("xtquant.xtconstant")
            values = (module.STOCK_BUY, module.STOCK_SELL, module.FIX_PRICE)
        except (ImportError, AttributeError) as exc:
            raise QmtCommandError("QMT_CONSTANTS_UNAVAILABLE") from exc
        if any(type(value) is not int or value <= 0 for value in values):
            raise QmtCommandError("QMT_CONSTANTS_INVALID")
        return cls(*values)


@dataclass(frozen=True, slots=True)
class QmtCallbackFact:
    kind: str
    received_at: datetime
    account_id: str | None = None
    sequence: int | None = None
    order_id: int | None = None
    remark: str | None = None
    error_id: int | None = None
    cancel_result: int | None = None
    account_status: int | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class QmtOrderFact:
    order_id: int
    status: int
    quantity: int
    filled: int
    symbol: str
    order_type: int
    price: float
    remark: str
    order_time: int | None = None
    strategy_name: str | None = None


@dataclass(frozen=True, slots=True)
class QmtTradeFact:
    trade_id: str
    order_id: int
    symbol: str
    order_type: int
    price: float
    quantity: int
    amount: float
    remark: str
    traded_time: int
    strategy_name: str | None = None


@dataclass(frozen=True, slots=True)
class QmtPositionFact:
    symbol: str
    quantity: int
    sellable: int
    frozen: int
    market_value: float
    average_price: float


@dataclass(frozen=True, slots=True)
class QmtAccountSnapshot:
    cash: float
    frozen_cash: float
    market_value: float
    total_asset: float
    positions: tuple[QmtPositionFact, ...]


def _attribute(value: object, name: str, kind: type[Any]) -> Any | None:
    result = getattr(value, name, None)
    return result if type(result) is kind else None


class QmtCommandTransport:
    """One account, one SDK session, bounded callback handoff, no auto retries."""

    def __init__(
        self,
        config: QmtProbeConfig,
        account_id: str,
        clock: Callable[[], datetime],
        *,
        bindings: XtquantBindings | None = None,
        constants: QmtConstants | None = None,
    ) -> None:
        if not account_id.isascii() or not account_id.isalnum() or not 4 <= len(account_id) <= 64:
            raise QmtCommandError("QMT_ACCOUNT_ID_INVALID")
        sdk = load_xtquant_bindings() if bindings is None else bindings
        self._constants = QmtConstants.from_sdk() if constants is None else constants
        self._account_id = account_id
        self._clock = clock
        self._events: Queue[QmtCallbackFact] = Queue(maxsize=1024)
        self._invalid = Event()
        self._overflow = False
        self._started = False
        self._closed = False
        self._stopped = False
        self._account = sdk.account_type(account_id, config.account_type)
        self._trader = sdk.trader_type(str(config.userdata_mini), config.session_id)
        base = sdk.callback_type
        owner = self

        class Callback(base):  # type: ignore[misc, valid-type]
            def on_disconnected(self) -> None:
                owner._capture("DISCONNECTED", None)

            def on_account_status(self, value: object) -> None:
                owner._capture("ACCOUNT_STATUS", value)

            def on_order_stock_async_response(self, value: object) -> None:
                owner._capture("ORDER_RESPONSE", value)

            def on_cancel_order_stock_async_response(self, value: object) -> None:
                owner._capture("CANCEL_RESPONSE", value)

            def on_stock_order(self, value: object) -> None:
                owner._capture("ORDER_UPDATE", value)

            def on_stock_trade(self, value: object) -> None:
                owner._capture("TRADE", value)

            def on_order_error(self, value: object) -> None:
                owner._capture("ORDER_ERROR", value)

            def on_cancel_error(self, value: object) -> None:
                owner._capture("CANCEL_ERROR", value)

        self._callback = Callback()

    def _capture(self, kind: str, value: object | None) -> None:
        # SDK callback thread: copy only bounded primitive fields; no queries,
        # disk I/O, HTTP or trading decisions here.
        try:
            if (
                kind == "ACCOUNT_STATUS"
                and _attribute(value, "account_id", str) != self._account_id
            ):
                return
            message = _attribute(value, "error_msg", str)
            fact = QmtCallbackFact(
                kind,
                self._clock(),
                _attribute(value, "account_id", str),
                _attribute(value, "seq", int),
                _attribute(value, "order_id", int),
                _attribute(value, "order_remark", str),
                _attribute(value, "error_id", int),
                _attribute(value, "cancel_result", int),
                _attribute(value, "status", int),
                message[:256] if message else None,
            )
            if kind == "DISCONNECTED" or (kind == "ACCOUNT_STATUS" and fact.account_status != 0):
                self._invalid.set()
            self._events.put_nowait(fact)
        except Exception:
            self._overflow = True
            self._invalid.set()

    def start(self) -> None:
        if self._started or self._closed:
            raise QmtCommandError("QMT_COMMAND_LIFECYCLE_INVALID")
        try:
            self._trader.register_callback(self._callback)
            self._trader.start()
            if self._trader.connect() != 0 or self._trader.subscribe(self._account) != 0:
                raise QmtCommandError("QMT_COMMAND_CONNECTION_FAILED")
            self._started = True
        except BaseException:
            self._closed = True
            self._trader.stop()
            self._stopped = True
            raise

    def _ready(self) -> None:
        if not self._started or self._closed or self._invalid.is_set():
            raise QmtCommandError("QMT_COMMAND_UNAVAILABLE")

    def _account_ok(self) -> None:
        try:
            statuses = self._trader.query_account_status()
        except Exception as exc:
            raise QmtCommandError("QMT_ACCOUNT_STATUS_UNKNOWN") from exc
        if not isinstance(statuses, list) or len(statuses) > 32:
            raise QmtCommandError("QMT_ACCOUNT_NOT_READY")
        target = [
            status
            for status in statuses
            if _attribute(status, "account_id", str) == self._account_id
        ]
        if (
            len(target) != 1
            or _attribute(target[0], "account_type", int) != 2
            or _attribute(target[0], "status", int) != 0
        ):
            raise QmtCommandError("QMT_ACCOUNT_NOT_READY")

    def submit(self, order: SubmitOrder) -> int:
        self._ready()
        self._account_ok()
        side = (
            self._constants.stock_buy if order.side is OrderSide.BUY else self._constants.stock_sell
        )
        try:
            sequence = self._trader.order_stock_async(
                self._account,
                str(order.symbol),
                side,
                order.quantity.value,
                self._constants.fixed_price,
                float(order.limit_price.amount),
                "personal-auto-trading",
                order.remark_token,
            )
        except Exception as exc:
            raise QmtCommandError("QMT_SUBMIT_RESULT_UNKNOWN") from exc
        if type(sequence) is not int or sequence <= 0:
            raise QmtCommandError("QMT_SUBMIT_REJECTED_BY_SDK")
        return sequence

    def preflight(self, order: SubmitOrder) -> None:
        """Recheck funds or sellable shares and existing activity before one send."""
        self._ready()
        self._account_ok()
        try:
            asset = self._trader.query_stock_asset(self._account)
            positions = self._trader.query_stock_positions(self._account)
            orders = self._trader.query_stock_orders(self._account, False)
            trades = self._trader.query_stock_trades(self._account)
        except Exception as exc:
            raise QmtCommandError("QMT_PREFLIGHT_QUERY_UNKNOWN") from exc
        if (
            asset is None
            or _attribute(asset, "account_id", str) != self._account_id
            or _attribute(asset, "account_type", int) != 2
            or not isinstance(positions, list)
            or not isinstance(orders, list)
            or not isinstance(trades, list)
            or any(len(rows) > 10000 for rows in (positions, orders, trades))
        ):
            raise QmtCommandError("QMT_PREFLIGHT_QUERY_UNKNOWN")
        for rows in (positions, orders, trades):
            for row in rows:
                if (
                    _attribute(row, "account_id", str) != self._account_id
                    or _attribute(row, "account_type", int) != 2
                ):
                    raise QmtCommandError("QMT_PREFLIGHT_ACCOUNT_MISMATCH")
        if any(
            _attribute(row, "order_remark", str) == order.remark_token for row in (*orders, *trades)
        ):
            raise QmtCommandError("QMT_PREFLIGHT_DUPLICATE_REMARK")
        if order.side is OrderSide.BUY:
            available = _attribute(asset, "cash", float)
            needed = float(order.limit_price.amount * order.quantity.value)
            if available is None or not math.isfinite(available) or available < needed:
                raise QmtCommandError("QMT_PREFLIGHT_CASH_INSUFFICIENT")
        else:
            target = [
                row for row in positions if _attribute(row, "stock_code", str) == str(order.symbol)
            ]
            sellable = _attribute(target[0], "can_use_volume", int) if len(target) == 1 else None
            if sellable is None or sellable < order.quantity.value:
                raise QmtCommandError("QMT_PREFLIGHT_SHARES_INSUFFICIENT")

    def cancel(self, order: CancelOrder) -> int:
        self._ready()
        self._account_ok()
        try:
            broker_id = int(order.broker_order_id)
        except ValueError as exc:
            raise QmtCommandError("QMT_CANCEL_ORDER_ID_INVALID") from exc
        if broker_id <= 0:
            raise QmtCommandError("QMT_CANCEL_ORDER_ID_INVALID")
        try:
            sequence = self._trader.cancel_order_stock_async(self._account, broker_id)
        except Exception as exc:
            raise QmtCommandError("QMT_CANCEL_RESULT_UNKNOWN") from exc
        if type(sequence) is not int or sequence <= 0:
            raise QmtCommandError("QMT_CANCEL_REJECTED_BY_SDK")
        return sequence

    def query_order(self, order_id: int, remark: str) -> QmtOrderFact | None:
        """Return a matching broker order, never infer absence from a failed query."""
        self._ready()
        self._account_ok()
        try:
            rows = self._trader.query_stock_orders(self._account, False)
        except Exception as exc:
            raise QmtCommandError("QMT_ORDER_QUERY_UNKNOWN") from exc
        if not isinstance(rows, list) or len(rows) > 10000:
            raise QmtCommandError("QMT_ORDER_QUERY_UNKNOWN")
        matched = [row for row in rows if _attribute(row, "order_id", int) == order_id]
        if not matched:
            return None
        if len(matched) != 1:
            raise QmtCommandError("QMT_ORDER_QUERY_AMBIGUOUS")
        row = matched[0]
        if (
            _attribute(row, "account_id", str) != self._account_id
            or _attribute(row, "account_type", int) != 2
            or _attribute(row, "order_remark", str) != remark
        ):
            raise QmtCommandError("QMT_ORDER_QUERY_IDENTITY_CONFLICT")
        return self._order_fact(row)

    def _order_fact(self, row: object) -> QmtOrderFact:
        order_id = _attribute(row, "order_id", int)
        remark = _attribute(row, "order_remark", str)
        status = _attribute(row, "order_status", int)
        quantity = _attribute(row, "order_volume", int)
        filled = _attribute(row, "traded_volume", int)
        symbol = _attribute(row, "stock_code", str)
        order_type = _attribute(row, "order_type", int)
        price = _attribute(row, "price", float)
        if (
            order_id is None
            or order_id <= 0
            or remark is None
            or status is None
            or status not in {48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 255}
            or quantity is None
            or quantity <= 0
            or filled is None
            or not 0 <= filled <= quantity
            or symbol is None
            or order_type not in (self._constants.stock_buy, self._constants.stock_sell)
            or price is None
            or not math.isfinite(price)
            or price <= 0
        ):
            raise QmtCommandError("QMT_ORDER_QUERY_INVALID")
        order_time = _attribute(row, "order_time", int)
        strategy_name = _attribute(row, "strategy_name", str)
        return QmtOrderFact(
            order_id,
            status,
            quantity,
            filled,
            symbol,
            order_type,
            price,
            remark,
            order_time,
            strategy_name,
        )

    def query_orders(self) -> tuple[QmtOrderFact, ...]:
        """Return all current-day orders as validated primitive facts."""
        self._ready()
        self._account_ok()
        try:
            rows = self._trader.query_stock_orders(self._account, False)
        except Exception as exc:
            raise QmtCommandError("QMT_ORDER_QUERY_UNKNOWN") from exc
        if not isinstance(rows, list) or len(rows) > 10000:
            raise QmtCommandError("QMT_ORDER_QUERY_UNKNOWN")
        facts: list[QmtOrderFact] = []
        seen: set[int] = set()
        for row in rows:
            order_id = _attribute(row, "order_id", int)
            if order_id is None or order_id <= 0 or order_id in seen:
                raise QmtCommandError("QMT_ORDER_QUERY_INVALID")
            if (
                _attribute(row, "account_id", str) != self._account_id
                or _attribute(row, "account_type", int) != 2
            ):
                raise QmtCommandError("QMT_ORDER_QUERY_IDENTITY_CONFLICT")
            fact = self._order_fact(row)
            if fact.order_time is None or fact.order_time <= 0:
                raise QmtCommandError("QMT_ORDER_QUERY_INVALID")
            seen.add(order_id)
            facts.append(fact)
        return tuple(facts)

    def query_trades(self) -> tuple[QmtTradeFact, ...]:
        """Return current-day fills; missing or malformed data never becomes an empty day."""
        self._ready()
        self._account_ok()
        try:
            rows = self._trader.query_stock_trades(self._account)
        except Exception as exc:
            raise QmtCommandError("QMT_TRADE_QUERY_UNKNOWN") from exc
        if not isinstance(rows, list) or len(rows) > 10000:
            raise QmtCommandError("QMT_TRADE_QUERY_UNKNOWN")
        facts: list[QmtTradeFact] = []
        seen: set[str] = set()
        for row in rows:
            if (
                _attribute(row, "account_id", str) != self._account_id
                or _attribute(row, "account_type", int) != 2
            ):
                raise QmtCommandError("QMT_TRADE_QUERY_IDENTITY_CONFLICT")
            trade_id = _attribute(row, "traded_id", str)
            order_id = _attribute(row, "order_id", int)
            symbol = _attribute(row, "stock_code", str)
            order_type = _attribute(row, "order_type", int)
            price = _attribute(row, "traded_price", float)
            quantity = _attribute(row, "traded_volume", int)
            amount = _attribute(row, "traded_amount", float)
            remark = _attribute(row, "order_remark", str)
            traded_time = _attribute(row, "traded_time", int)
            strategy_name = _attribute(row, "strategy_name", str)
            if (
                trade_id is None
                or not trade_id
                or trade_id in seen
                or order_id is None
                or order_id <= 0
                or symbol is None
                or order_type not in (self._constants.stock_buy, self._constants.stock_sell)
                or price is None
                or not math.isfinite(price)
                or price <= 0
                or quantity is None
                or quantity <= 0
                or amount is None
                or not math.isfinite(amount)
                or amount <= 0
                or remark is None
                or traded_time is None
                or traded_time <= 0
            ):
                raise QmtCommandError("QMT_TRADE_QUERY_INVALID")
            seen.add(trade_id)
            facts.append(
                QmtTradeFact(
                    trade_id,
                    order_id,
                    symbol,
                    order_type,
                    price,
                    quantity,
                    amount,
                    remark,
                    traded_time,
                    strategy_name,
                )
            )
        return tuple(facts)

    def query_account(self) -> QmtAccountSnapshot:
        """Return validated cash and aggregate positions for reconciliation and display."""
        self._ready()
        self._account_ok()
        try:
            asset = self._trader.query_stock_asset(self._account)
            rows = self._trader.query_stock_positions(self._account)
        except Exception as exc:
            raise QmtCommandError("QMT_ACCOUNT_QUERY_UNKNOWN") from exc
        if (
            asset is None
            or _attribute(asset, "account_id", str) != self._account_id
            or _attribute(asset, "account_type", int) != 2
            or not isinstance(rows, list)
            or len(rows) > 10000
        ):
            raise QmtCommandError("QMT_ACCOUNT_QUERY_INVALID")

        def money(source: object, name: str) -> float:
            value = _attribute(source, name, float)
            if value is None or not math.isfinite(value) or value < 0:
                raise QmtCommandError("QMT_ACCOUNT_QUERY_INVALID")
            return float(value)

        positions: list[QmtPositionFact] = []
        seen: set[str] = set()
        for row in rows:
            symbol = _attribute(row, "stock_code", str)
            quantity = _attribute(row, "volume", int)
            sellable = _attribute(row, "can_use_volume", int)
            frozen = _attribute(row, "frozen_volume", int)
            if (
                _attribute(row, "account_id", str) != self._account_id
                or _attribute(row, "account_type", int) != 2
                or symbol is None
                or symbol in seen
                or quantity is None
                or sellable is None
                or frozen is None
                or quantity < 0
                or sellable < 0
                or frozen < 0
                or sellable + frozen > quantity
            ):
                raise QmtCommandError("QMT_ACCOUNT_QUERY_INVALID")
            seen.add(symbol)
            positions.append(
                QmtPositionFact(
                    symbol,
                    quantity,
                    sellable,
                    frozen,
                    money(row, "market_value"),
                    money(row, "avg_price"),
                )
            )
        return QmtAccountSnapshot(
            money(asset, "cash"),
            money(asset, "frozen_cash"),
            money(asset, "market_value"),
            money(asset, "total_asset"),
            tuple(positions),
        )

    def drain(self) -> tuple[QmtCallbackFact, ...]:
        if not self._started or self._closed or self._overflow:
            raise QmtCommandError("QMT_COMMAND_UNAVAILABLE")
        result: list[QmtCallbackFact] = []
        while True:
            try:
                fact = self._events.get_nowait()
            except Empty:
                break
            if fact.account_id is not None and fact.account_id != self._account_id:
                raise QmtCommandError("QMT_CALLBACK_ACCOUNT_MISMATCH")
            result.append(fact)
            if fact.kind == "DISCONNECTED":
                self._closed = True
                raise QmtCommandError("QMT_COMMAND_DISCONNECTED")
            if fact.kind == "ACCOUNT_STATUS" and fact.account_status != 0:
                self._closed = True
                raise QmtCommandError("QMT_ACCOUNT_NOT_READY")
        return tuple(result)

    def stop(self) -> None:
        if self._stopped:
            return
        self._closed = True
        self._stopped = True
        self._trader.stop()
