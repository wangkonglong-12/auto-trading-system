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
    BrokerCancelError,
    BrokerOrder,
    BrokerOrderIdentity,
    BrokerOrderStatus,
    BrokerRequestError,
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
from auto_trading_system.protocol.account_monitoring import Observation
from auto_trading_system.protocol.qmt_recovery import QmtOrderRecoveryBinding
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


def _local_id(remark: str, order_id: int | str, strategy_name: str | None) -> str:
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

    def read_display_names(self, symbols: tuple[str, ...]) -> dict[str, str]:
        from auto_trading_system.gateways.market.qmt import read_instrument_names

        return read_instrument_names(symbols)

    def __init__(
        self,
        config: QmtProbeConfig,
        account_id: str,
        account_alias: str,
        clock: Callable[[], datetime],
        *,
        bindings: XtquantBindings | None = None,
        constants: QmtConstants | None = None,
        order_recovery_bindings: tuple[QmtOrderRecoveryBinding, ...] = (),
    ) -> None:
        self._clock = clock
        self._account_alias = account_alias
        self._constants = QmtConstants.from_sdk() if constants is None else constants
        self._recovery_bindings = order_recovery_bindings
        contracts = [(b.trade_date, b.symbol[-2:], b.order_sysid) for b in order_recovery_bindings]
        if (
            len(contracts) > 32
            or len(set(contracts)) != len(contracts)
            or len({b.client_order_id for b in order_recovery_bindings}) != len(contracts)
        ):
            raise QmtCommandError("QMT_RECOVERY_BINDING_CONFLICT")
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
        self._requests: dict[int, tuple[str, str]] = {}
        self._remarks: dict[str, str] = {}
        self._cancels: dict[str, str] = {}
        self._cancel_request_orders: dict[int, str] = {}
        self._ambiguous_cancel_orders: set[str] = set()

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
        if len(self._requests) >= 2048:
            raise QmtCommandError("QMT_REQUEST_CAPACITY")
        expected = "ord_" + command.remark_token
        if command.client_order_id != expected:
            raise QmtCommandError("QMT_ORDER_ID_REMARK_MISMATCH")
        self._transport.preflight(command)
        sequence = self._transport.submit(command)
        if sequence in self._requests and self._requests[sequence] != (
            command.client_order_id,
            "NEW",
        ):
            raise QmtCommandError("QMT_REQUEST_SEQUENCE_CONFLICT")
        self._requests[sequence] = (command.client_order_id, "NEW")
        self._remarks[command.remark_token] = command.client_order_id
        return SubmissionReceipt(sequence, ShanghaiTimestamp(self._clock()))

    def cancel_order(self, command: CancelOrder) -> CancellationReceipt:
        self._require_started()
        if len(self._requests) >= 2048:
            raise QmtCommandError("QMT_REQUEST_CAPACITY")
        request_id = command.cancel_request_id or command.client_order_id
        if command.broker_order_id.startswith("qmt_sys_"):
            target = next(
                (
                    o
                    for o in self._last_orders
                    if o.client_order_id == command.client_order_id
                    and o.broker_order_id == command.broker_order_id
                ),
                None,
            )
            if (
                target is None
                or target.client_order_id.startswith("ord_external_")
                or not target.broker_contract_id
                or target.is_terminal
            ):
                raise QmtCommandError("QMT_CANCEL_ORDER_ID_INVALID")
            market = {"SH": 0, "SZ": 1}.get(str(target.symbol)[-2:])
            if market is None:
                raise QmtCommandError("QMT_CANCEL_ORDER_ID_INVALID")
            sequence = self._transport.cancel_contract(market, target.broker_contract_id)
        else:
            sequence = self._transport.cancel(command)
        if sequence in self._requests and self._requests[sequence] != (
            request_id,
            "CANCEL",
        ):
            raise QmtCommandError("QMT_REQUEST_SEQUENCE_CONFLICT")
        self._requests[sequence] = (request_id, "CANCEL")
        if (
            command.broker_order_id in self._cancels
            and self._cancels[command.broker_order_id] != request_id
        ):
            self._ambiguous_cancel_orders.add(command.broker_order_id)
        self._cancels[command.broker_order_id] = request_id
        self._cancel_request_orders[sequence] = command.broker_order_id
        return CancellationReceipt(sequence, ShanghaiTimestamp(self._clock()))

    def query_orders(self) -> tuple[BrokerOrder, ...]:
        self._require_started()
        self._drain_health_events()
        orders = tuple(
            self._order(value)
            for value in self._transport.query_orders()
            if not self._historical_external_zero(value)
        )
        self._last_orders = orders
        self._orders_observed = True
        return orders

    def _historical_external_zero(self, fact: QmtOrderFact) -> bool:
        """QMT may retain yesterday's orders until its broker-day rollover.

        Only positively external zero-fill rejected/canceled orders are harmless.
        System, active, filled, future and invalid-timestamp facts still fail closed.
        Trades are queried separately and are never discarded here.
        """
        if (
            fact.status not in {54, 57}
            or fact.filled != 0
            or not _local_id(fact.remark, fact.order_id, fact.strategy_name).startswith(
                "ord_external_"
            )
            or fact.order_time is None
            or fact.order_time <= 0
        ):
            return False
        try:
            occurred = datetime.fromtimestamp(fact.order_time, tz=self._now().tzinfo)
        except (OSError, OverflowError, ValueError):
            return False
        return occurred.year >= 2000 and occurred.date() < self._now().date()

    def read_monitoring(self) -> Observation:
        self._require_started()
        self._drain_health_events()
        result = self._transport.read_monitoring()
        self._drain_health_events()
        return result

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
        reference = self._order_reference(fact)
        return BrokerOrder(
            client_order_id=self._client_id(fact, reference),
            broker_order_id=reference,
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
            broker_contract_id=fact.order_sysid,
        )

    def restore_order_identities(self, bindings: tuple[QmtOrderRecoveryBinding, ...]) -> None:
        merged = {(b.trade_date, b.symbol[-2:], b.order_sysid): b for b in self._recovery_bindings}
        clients = {
            b.client_order_id: (b.trade_date, b.symbol[-2:], b.order_sysid)
            for b in self._recovery_bindings
        }
        for binding in bindings:
            key = (binding.trade_date, binding.symbol[-2:], binding.order_sysid)
            if (
                key in merged
                and merged[key].model_copy(
                    update={"limit_price": str(Price.parse(merged[key].limit_price))}
                )
                != binding.model_copy(update={"limit_price": str(Price.parse(binding.limit_price))})
            ) or (binding.client_order_id in clients and clients[binding.client_order_id] != key):
                raise QmtCommandError("QMT_RECOVERY_BINDING_CONFLICT")
            merged[key], clients[binding.client_order_id] = binding, key
        if len(merged) > 2048:
            raise QmtCommandError("QMT_RECOVERY_BINDING_CONFLICT")
        self._recovery_bindings = tuple(merged.values())

    def _trade(self, fact: QmtTradeFact) -> BrokerTrade:
        reference = self._order_reference(fact)
        client_id = self._client_id(fact, reference)
        trade_id = self._trade_id(fact.trade_id)
        price = Price.parse(str(fact.price))
        quantity = Quantity(fact.quantity)
        notional = Money(
            (price.amount * quantity.value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )
        return BrokerTrade(
            trade_id=trade_id,
            client_order_id=client_id,
            broker_order_id=reference,
            symbol=SecurityCode.parse(fact.symbol),
            side=self._side(fact.order_type),
            price=price,
            quantity=quantity,
            notional=notional,
            traded_at=ShanghaiTimestamp(self._timestamp(fact.traded_time)),
        )

    def _order_reference(self, fact: QmtOrderFact | QmtTradeFact) -> str:
        if fact.order_id > 0:
            return str(fact.order_id)
        if not fact.order_sysid or fact.remark != "" or fact.strategy_name != "":
            raise QmtCommandError("QMT_EXTERNAL_ORDER_ID_INVALID")
        timestamp = fact.order_time if isinstance(fact, QmtOrderFact) else fact.traded_time
        day = self._timestamp(timestamp).date().isoformat()
        identity = f"{day}:{fact.symbol[-2:]}:{fact.order_sysid}"
        # A separate namespace prevents this read-only reference from being
        # confused with a local SDK order ID accepted by cancellation.
        return "qmt_sys_" + hashlib.sha256(identity.encode()).hexdigest()[:32]

    def _client_id(self, fact: QmtOrderFact | QmtTradeFact, reference: str) -> str:
        ordinary = _local_id(fact.remark, reference, fact.strategy_name)
        if fact.order_id != 0:
            return ordinary
        binding = next(
            (
                b
                for b in self._recovery_bindings
                if b.trade_date == self._now().date()
                and b.symbol[-2:] == fact.symbol[-2:]
                and b.order_sysid == fact.order_sysid
            ),
            None,
        )
        if binding is None:
            return ordinary
        price = Price.parse(str(fact.price))
        limit = Price.parse(binding.limit_price)
        valid = (
            fact.symbol == binding.symbol
            and self._side(fact.order_type).value == binding.side
            and (
                fact.quantity == binding.quantity and price == limit
                if isinstance(fact, QmtOrderFact)
                else fact.quantity <= binding.quantity
                and (
                    price.amount <= limit.amount
                    if binding.side == "BUY"
                    else price.amount >= limit.amount
                )
            )
        )
        if not valid:
            raise QmtCommandError("QMT_RECOVERY_BINDING_CONFLICT")
        # This only restores identity. The engine must still find the original
        # account/day-scoped J1 and reconcile quantities, fills and holdings.
        return binding.client_order_id

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
        failed = False
        for fact in self._transport.drain():
            if (
                fact.kind == "ORDER_UPDATE"
                and fact.account_id == self._transport._account_id
                and fact.order is not None
                and fact.order.order_id > 0
                and fact.order.order_sysid
                and fact.order.strategy_name == "personal-auto-trading"
                and _LOCAL_REMARK.fullmatch(fact.order.remark)
                and self._sink is not None
            ):
                observed_order = self._order(fact.order)
                self.restore_order_identities(
                    (
                        QmtOrderRecoveryBinding(
                            client_order_id=observed_order.client_order_id,
                            trade_date=observed_order.updated_at.value.date(),
                            symbol=fact.order.symbol,
                            order_sysid=fact.order.order_sysid,
                            side=observed_order.side.value,
                            quantity=fact.order.quantity,
                            limit_price=str(observed_order.limit_price),
                        ),
                    )
                )
                self._sink(BrokerOrderIdentity(observed_order))
            if fact.kind in {"ORDER_ERROR", "CANCEL_ERROR"}:
                failed = True
                action = "NEW" if fact.kind == "ORDER_ERROR" else "CANCEL"
                candidates = set()
                request = self._requests.get(fact.sequence or -1)
                if request is not None and request[1] == action:
                    candidates.add(request[0])
                if action == "NEW" and fact.remark in self._remarks:
                    candidates.add(self._remarks[fact.remark])
                if (
                    action == "CANCEL"
                    and request is None
                    and (fact.sequence is None or fact.sequence <= 0)
                    and str(fact.order_id) not in self._ambiguous_cancel_orders
                    and str(fact.order_id) in self._cancels
                ):
                    candidates.add(self._cancels[str(fact.order_id)])
                if len(candidates) == 1 and self._sink is not None:
                    number = abs(fact.error_id) if fact.error_id is not None else None
                    suffix = str(number) if number is not None and number <= 2**31 else "UNKNOWN"
                    if fact.error_id is not None and fact.error_id < 0:
                        suffix = "NEG_" + suffix
                    client_id = next(iter(candidates))
                    code = "QMT_" + fact.kind + "_" + suffix
                    if action == "NEW":
                        self._sink(
                            BrokerRequestError(
                                client_id, action, code, ShanghaiTimestamp(fact.received_at)
                            )
                        )
                    else:
                        broker_id = self._cancel_request_orders.get(fact.sequence or -1)
                        if (
                            broker_id is not None
                            and fact.order_id is not None
                            and fact.order_id > 0
                            and str(fact.order_id) != broker_id
                        ):
                            # Conflicting callback identities prove no outcome.
                            continue
                        if broker_id is None and str(fact.order_id) in self._cancels:
                            broker_id = str(fact.order_id)
                        if broker_id is not None:
                            self._sink(
                                BrokerCancelError(
                                    client_id,
                                    broker_id,
                                    code,
                                    "QMT cancellation error",
                                    ShanghaiTimestamp(fact.received_at),
                                )
                            )
        if failed:
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
