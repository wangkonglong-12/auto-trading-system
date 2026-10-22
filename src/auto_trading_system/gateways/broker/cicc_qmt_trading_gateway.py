"""MiniQMT trading gateway with query-backed order and trade reconciliation."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from auto_trading_system.domain import Money, Price, Quantity, SecurityCode, ShanghaiTimestamp
from auto_trading_system.gateways.broker.cicc_qmt import XtquantBindings
from auto_trading_system.gateways.broker.models import (
    BrokerOrder,
    BrokerOrderStatus,
    BrokerTrade,
    CancellationReceipt,
    CancelOrder,
    OrderSide,
    SubmissionReceipt,
    SubmitOrder,
)
from auto_trading_system.gateways.broker.port import BrokerEventSink
from auto_trading_system.gateways.broker.qmt_command_transport import (
    QmtCommandError,
    QmtCommandTransport,
    QmtConstants,
    QmtOrderFact,
    QmtTradeFact,
)
from auto_trading_system.monitoring.qmt_readonly import QmtProbeConfig
from auto_trading_system.runtime.recovery_v2 import AccountFact, PositionFact

_LOCAL_REMARK = re.compile(r"[A-Za-z0-9_-]{8,24}")
_STATUS = {
    48: BrokerOrderStatus.SUBMITTING,
    49: BrokerOrderStatus.SUBMITTING,
    50: BrokerOrderStatus.ACKED,
    51: BrokerOrderStatus.CANCEL_PENDING,
    52: BrokerOrderStatus.PARTIAL_FILL_CANCEL_PENDING,
    53: BrokerOrderStatus.PARTIALLY_FILLED_CANCELLED,
    54: BrokerOrderStatus.CANCELLED,
    55: BrokerOrderStatus.PARTIALLY_FILLED,
    56: BrokerOrderStatus.FILLED,
    57: BrokerOrderStatus.REJECTED,
    255: BrokerOrderStatus.UNKNOWN,
}


def _local_id(remark: str, order_id: int, strategy_name: str | None) -> str:
    if strategy_name == "personal-auto-trading" and _LOCAL_REMARK.fullmatch(remark):
        return "ord_" + remark
    identity = f"{order_id}:{remark}:{strategy_name or ''}".encode("utf-8", "replace")
    return "ord_external_" + hashlib.sha256(identity).hexdigest()[:20]


class CiccQmtTradingGateway:
    """Broker-neutral live adapter used only by an explicit trading profile.

    SDK request sequences are receipts, not acceptance. Broker orders and fills
    are always rebuilt from account queries so missed or delayed callbacks are
    repaired on the next engine observation.
    """

    def __init__(
        self,
        config: QmtProbeConfig,
        account_id: str,
        account_alias: str,
        clock: Callable[[], datetime],
        *,
        bindings: XtquantBindings | None = None,
        constants: QmtConstants | None = None,
    ) -> None:
        self._clock = clock
        self._account_alias = account_alias
        self._constants = QmtConstants.from_sdk() if constants is None else constants
        self._transport = QmtCommandTransport(
            config,
            account_id,
            clock,
            bindings=bindings,
            constants=self._constants,
        )
        self._sink: BrokerEventSink | None = None
        self._started = False
        self._last_account: AccountFact | None = None
        self._last_orders: tuple[BrokerOrder, ...] = ()
        self._orders_observed = False

    def start(self, event_sink: BrokerEventSink) -> None:
        if self._started or not callable(event_sink):
            raise QmtCommandError("QMT_GATEWAY_LIFECYCLE_INVALID")
        self._transport.start()
        self._sink = event_sink
        self._started = True

    def stop(self) -> None:
        self._transport.stop()
        self._sink = None
        self._started = False

    def submit_order(self, command: SubmitOrder) -> SubmissionReceipt:
        self._require_started()
        expected = "ord_" + command.remark_token
        if command.client_order_id != expected:
            raise QmtCommandError("QMT_ORDER_ID_REMARK_MISMATCH")
        self._transport.preflight(command)
        sequence = self._transport.submit(command)
        return SubmissionReceipt(sequence, ShanghaiTimestamp(self._clock()))

    def cancel_order(self, command: CancelOrder) -> CancellationReceipt:
        self._require_started()
        sequence = self._transport.cancel(command)
        return CancellationReceipt(sequence, ShanghaiTimestamp(self._clock()))

    def query_orders(self) -> tuple[BrokerOrder, ...]:
        self._require_started()
        self._drain_health_events()
        orders = tuple(self._order(value) for value in self._transport.query_orders())
        self._last_orders = orders
        self._orders_observed = True
        return orders

    def query_trades(self) -> tuple[BrokerTrade, ...]:
        self._require_started()
        self._drain_health_events()
        return tuple(self._trade(value) for value in self._transport.query_trades())

    def find_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        matched = [
            order for order in self.query_orders() if order.client_order_id == client_order_id
        ]
        if len(matched) > 1:
            raise QmtCommandError("QMT_CLIENT_ORDER_ID_AMBIGUOUS")
        return matched[0] if matched else None

    def read_account(self) -> AccountFact:
        self._require_started()
        if not self._orders_observed:
            raise QmtCommandError("QMT_ACCOUNT_ORDER_WINDOW_MISSING")
        snapshot = self._transport.query_account()
        positions = tuple(
            PositionFact(
                symbol=item.symbol,
                quantity=item.quantity,
                sellable=item.sellable,
                frozen=item.frozen,
                market_value=self._money(item.market_value),
                cost_amount=self._money(item.average_price * item.quantity),
            )
            for item in snapshot.positions
        )
        observed = self._now()
        account = AccountFact(
            account_id=self._account_alias,
            snapshot_id="qmt-" + hashlib.sha256(observed.isoformat().encode()).hexdigest()[:24],
            observed_at=observed,
            cash=self._money(snapshot.cash),
            cash_frozen=self._money(snapshot.frozen_cash),
            market_value=self._money(snapshot.market_value),
            total_equity=self._money(snapshot.total_asset),
            positions=positions,
            observed_order_ids=tuple(order.client_order_id for order in self._last_orders),
            coherent=True,
        )
        self._last_account = account
        return account

    def _order(self, fact: QmtOrderFact) -> BrokerOrder:
        symbol = SecurityCode.parse(fact.symbol)
        side = self._side(fact.order_type)
        filled = Quantity(fact.filled)
        quantity = Quantity(fact.quantity)
        return BrokerOrder(
            client_order_id=_local_id(fact.remark, fact.order_id, fact.strategy_name),
            broker_order_id=str(fact.order_id),
            symbol=symbol,
            side=side,
            status=_STATUS[fact.status],
            order_quantity=quantity,
            filled_quantity=filled,
            remaining_quantity=Quantity(quantity.value - filled.value),
            limit_price=Price.parse(str(fact.price)),
            broker_status_code=fact.status,
            status_reason=f"QMT_STATUS_{fact.status}",
            updated_at=ShanghaiTimestamp(self._timestamp(fact.order_time)),
        )

    def _trade(self, fact: QmtTradeFact) -> BrokerTrade:
        client_id = _local_id(fact.remark, fact.order_id, fact.strategy_name)
        trade_id = self._trade_id(fact.trade_id)
        price = Price.parse(str(fact.price))
        quantity = Quantity(fact.quantity)
        notional = Money(
            (price.amount * quantity.value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )
        return BrokerTrade(
            trade_id=trade_id,
            client_order_id=client_id,
            broker_order_id=str(fact.order_id),
            symbol=SecurityCode.parse(fact.symbol),
            side=self._side(fact.order_type),
            price=price,
            quantity=quantity,
            notional=notional,
            traded_at=ShanghaiTimestamp(self._timestamp(fact.traded_time)),
        )

    def _side(self, value: int) -> OrderSide:
        if value == self._constants.stock_buy:
            return OrderSide.BUY
        if value == self._constants.stock_sell:
            return OrderSide.SELL
        raise QmtCommandError("QMT_ORDER_SIDE_INVALID")

    def _timestamp(self, value: int | None) -> datetime:
        if value is None or value <= 0:
            return self._now()
        try:
            parsed = datetime.fromtimestamp(value, tz=self._now().tzinfo)
        except (OSError, OverflowError, ValueError) as exc:
            raise QmtCommandError("QMT_TIMESTAMP_INVALID") from exc
        if parsed.date() != self._now().date():
            raise QmtCommandError("QMT_TIMESTAMP_OUTSIDE_TRADE_DATE")
        return parsed

    def _now(self) -> datetime:
        current = self._clock()
        if current.utcoffset() is None:
            raise QmtCommandError("QMT_CLOCK_INVALID")
        return current

    def _drain_health_events(self) -> None:
        # Query results remain authoritative. Callback errors and disconnects
        # still fence the session before any result is accepted.
        for fact in self._transport.drain():
            if fact.kind in {"ORDER_ERROR", "CANCEL_ERROR"}:
                raise QmtCommandError("QMT_CALLBACK_ERROR")

    def _require_started(self) -> None:
        if not self._started:
            raise QmtCommandError("QMT_GATEWAY_NOT_STARTED")

    @staticmethod
    def _money(value: float) -> str:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return format(amount, ".2f")

    @staticmethod
    def _trade_id(value: str) -> str:
        candidate = "trade_" + value
        if re.fullmatch(r"trade_[A-Za-z0-9_.:-]{4,80}", candidate):
            return candidate
        return "trade_qmt_" + hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:24]
