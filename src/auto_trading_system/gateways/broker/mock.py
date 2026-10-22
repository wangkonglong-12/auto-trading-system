"""Deterministic scripted broker for state-machine and recovery tests."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from auto_trading_system.domain import Clock, Money, Price, Quantity
from auto_trading_system.gateways.broker.errors import (
    BrokerNotStartedError,
    BrokerQueryError,
    BrokerTimeoutError,
    DuplicateClientOrderError,
    OrderNotCancelableError,
    UnknownOrderError,
)
from auto_trading_system.gateways.broker.models import (
    BrokerCancelError,
    BrokerEvent,
    BrokerOrder,
    BrokerOrderStatus,
    BrokerOrderUpdate,
    BrokerTrade,
    CancellationReceipt,
    CancelOrder,
    SubmissionReceipt,
    SubmitOrder,
)
from auto_trading_system.gateways.broker.paper_state import PaperBrokerState, PaperStateFile
from auto_trading_system.gateways.broker.port import BrokerEventSink


class EventDeliveryMode(StrEnum):
    NORMAL = "NORMAL"
    REVERSED = "REVERSED"
    DUPLICATE = "DUPLICATE"
    DEFERRED = "DEFERRED"


class SubmitOutcome(StrEnum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    PARTIAL_FILL = "PARTIAL_FILL"
    FULL_FILL = "FULL_FILL"
    TIMEOUT_BEFORE_ACCEPT = "TIMEOUT_BEFORE_ACCEPT"
    TIMEOUT_AFTER_ACCEPT = "TIMEOUT_AFTER_ACCEPT"


class CancelOutcome(StrEnum):
    APPLY = "APPLY"
    REJECT = "REJECT"
    TIMEOUT_BEFORE_APPLY = "TIMEOUT_BEFORE_APPLY"
    TIMEOUT_AFTER_APPLY = "TIMEOUT_AFTER_APPLY"


class BrokerQuery(StrEnum):
    ORDERS = "ORDERS"
    TRADES = "TRADES"
    FIND_ORDER = "FIND_ORDER"


class QueryFault(StrEnum):
    TIMEOUT = "TIMEOUT"
    FAILURE = "FAILURE"


@dataclass(frozen=True, slots=True)
class SubmitScenario:
    outcome: SubmitOutcome = SubmitOutcome.ACCEPT
    delivery: EventDeliveryMode = EventDeliveryMode.NORMAL
    reject_reason: str = "MOCK_ORDER_REJECTED"
    partial_fill_quantity: Quantity | None = None
    fill_price: Price | None = None
    fill_notional: Money | None = None


@dataclass(frozen=True, slots=True)
class CancelScenario:
    outcome: CancelOutcome = CancelOutcome.APPLY
    delivery: EventDeliveryMode = EventDeliveryMode.NORMAL
    reject_code: str = "MOCK_CANCEL_REJECTED"
    reject_message: str = "mock broker rejected the cancel request"


class MockBrokerGateway:
    """In-memory broker whose behavior is entirely controlled by queued scenarios."""

    def __init__(self, clock: Clock, *, paper_state: PaperStateFile | None = None) -> None:
        self._clock = clock
        self._event_sink: BrokerEventSink | None = None
        self._next_request_seq = 1
        self._next_broker_id = 1
        self._next_trade_id = 1
        self._orders: dict[str, BrokerOrder] = {}
        self._trades: dict[str, BrokerTrade] = {}
        self._submit_scenarios: deque[SubmitScenario] = deque()
        self._cancel_scenarios: deque[CancelScenario] = deque()
        self._query_faults: dict[BrokerQuery, deque[QueryFault]] = {
            query: deque() for query in BrokerQuery
        }
        self._pending_events: list[BrokerEvent] = []
        self._paper_state = paper_state
        restored = paper_state.load() if paper_state else None
        if restored:
            self._orders = {order.client_order_id: order for order in restored.orders}
            self._trades = {trade.trade_id: trade for trade in restored.trades}
            self._next_request_seq = restored.request_sequence
            self._next_broker_id = restored.order_sequence
            self._next_trade_id = restored.trade_sequence

    def start(self, event_sink: BrokerEventSink) -> None:
        if not callable(event_sink):
            raise TypeError("event_sink must be callable")
        self._event_sink = event_sink

    def stop(self) -> None:
        self._event_sink = None
        self._pending_events.clear()

    def queue_submit(self, scenario: SubmitScenario) -> None:
        self._submit_scenarios.append(scenario)

    def queue_cancel(self, scenario: CancelScenario) -> None:
        self._cancel_scenarios.append(scenario)

    def queue_query_fault(self, query: BrokerQuery, fault: QueryFault) -> None:
        self._query_faults[query].append(fault)

    def submit_order(self, command: SubmitOrder) -> SubmissionReceipt:
        self._require_started()
        if command.client_order_id in self._orders:
            raise DuplicateClientOrderError(command.client_order_id)

        scenario = (
            self._submit_scenarios.popleft()
            if self._submit_scenarios
            else (
                SubmitScenario(
                    outcome=SubmitOutcome.FULL_FILL,
                    partial_fill_quantity=command.quantity,
                    fill_price=command.limit_price,
                    fill_notional=Money(command.limit_price.amount * command.quantity.value),
                )
                if self._paper_state
                else SubmitScenario()
            )
        )
        if scenario.outcome is SubmitOutcome.TIMEOUT_BEFORE_ACCEPT:
            raise BrokerTimeoutError("submit", outcome_unknown=False)
        self._validate_submit_scenario(command, scenario)

        request_seq = self._take_request_seq()
        broker_order_id = self._take_broker_order_id()
        now = self._clock.now()
        events: list[BrokerEvent]
        if scenario.outcome is SubmitOutcome.REJECT:
            rejected = BrokerOrder(
                client_order_id=command.client_order_id,
                broker_order_id=broker_order_id,
                symbol=command.symbol,
                side=command.side,
                status=BrokerOrderStatus.REJECTED,
                order_quantity=command.quantity,
                filled_quantity=Quantity(0),
                remaining_quantity=command.quantity,
                limit_price=command.limit_price,
                broker_status_code=57,
                status_reason=scenario.reject_reason,
                updated_at=now,
            )
            self._orders[command.client_order_id] = rejected
            events = [BrokerOrderUpdate(rejected)]
        else:
            accepted = BrokerOrder(
                client_order_id=command.client_order_id,
                broker_order_id=broker_order_id,
                symbol=command.symbol,
                side=command.side,
                status=BrokerOrderStatus.ACKED,
                order_quantity=command.quantity,
                filled_quantity=Quantity(0),
                remaining_quantity=command.quantity,
                limit_price=command.limit_price,
                broker_status_code=50,
                status_reason="MOCK_ORDER_ACCEPTED",
                updated_at=now,
            )
            self._orders[command.client_order_id] = accepted
            events = [BrokerOrderUpdate(accepted)]
            if scenario.outcome in (SubmitOutcome.PARTIAL_FILL, SubmitOutcome.FULL_FILL):
                events.extend(self._apply_fill(command, accepted, scenario))

        self._deliver(events, scenario.delivery)
        if scenario.outcome is SubmitOutcome.TIMEOUT_AFTER_ACCEPT:
            raise BrokerTimeoutError("submit", outcome_unknown=True)
        return SubmissionReceipt(request_seq=request_seq, submitted_at=now)

    def cancel_order(self, command: CancelOrder) -> CancellationReceipt:
        self._require_started()
        order = self._orders.get(command.client_order_id)
        if order is None or order.broker_order_id != command.broker_order_id:
            raise UnknownOrderError(command.client_order_id)
        if order.is_terminal:
            raise OrderNotCancelableError(command.client_order_id)

        scenario = self._cancel_scenarios.popleft() if self._cancel_scenarios else CancelScenario()
        if scenario.outcome is CancelOutcome.TIMEOUT_BEFORE_APPLY:
            raise BrokerTimeoutError("cancel", outcome_unknown=False)

        request_seq = self._take_request_seq()
        now = self._clock.now()
        if scenario.outcome is CancelOutcome.REJECT:
            events: list[BrokerEvent] = [
                BrokerCancelError(
                    client_order_id=command.client_order_id,
                    broker_order_id=command.broker_order_id,
                    error_code=scenario.reject_code,
                    message=scenario.reject_message,
                    occurred_at=now,
                )
            ]
        else:
            status = (
                BrokerOrderStatus.CANCELLED
                if order.filled_quantity.is_zero
                else BrokerOrderStatus.PARTIALLY_FILLED_CANCELLED
            )
            cancelled = BrokerOrder(
                client_order_id=order.client_order_id,
                broker_order_id=order.broker_order_id,
                symbol=order.symbol,
                side=order.side,
                status=status,
                order_quantity=order.order_quantity,
                filled_quantity=order.filled_quantity,
                remaining_quantity=order.remaining_quantity,
                limit_price=order.limit_price,
                broker_status_code=54 if status is BrokerOrderStatus.CANCELLED else 53,
                status_reason="MOCK_ORDER_CANCELLED",
                updated_at=now,
            )
            self._orders[command.client_order_id] = cancelled
            events = [BrokerOrderUpdate(cancelled)]

        self._deliver(events, scenario.delivery)
        if scenario.outcome is CancelOutcome.TIMEOUT_AFTER_APPLY:
            raise BrokerTimeoutError("cancel", outcome_unknown=True)
        return CancellationReceipt(request_seq=request_seq, submitted_at=now)

    def query_orders(self) -> tuple[BrokerOrder, ...]:
        self._require_started()
        self._raise_query_fault(BrokerQuery.ORDERS)
        return tuple(
            o
            for o in self._orders.values()
            if self._paper_state is None
            or o.updated_at.value.date() == self._clock.now().value.date()
        )

    def query_trades(self) -> tuple[BrokerTrade, ...]:
        self._require_started()
        self._raise_query_fault(BrokerQuery.TRADES)
        return tuple(
            t
            for t in self._trades.values()
            if self._paper_state is None
            or t.traded_at.value.date() == self._clock.now().value.date()
        )

    def paper_history(self) -> tuple[BrokerTrade, ...]:
        """All fictional fills for independent balance calculation on SDK owner."""
        self._require_started()
        return tuple(self._trades.values())

    def find_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        self._require_started()
        self._raise_query_fault(BrokerQuery.FIND_ORDER)
        return self._orders.get(client_order_id)

    def inject_event(self, event: BrokerEvent) -> None:
        """Deliver an arbitrary duplicate or stale callback without mutating broker facts."""
        self._require_started()
        self._emit(event)

    def flush_pending(self, *, reverse: bool = False, duplicate: bool = False) -> None:
        self._require_started()
        events = list(reversed(self._pending_events)) if reverse else list(self._pending_events)
        self._pending_events.clear()
        for event in events:
            self._emit(event)
            if duplicate:
                self._emit(event)

    @property
    def pending_event_count(self) -> int:
        return len(self._pending_events)

    def _apply_fill(
        self,
        command: SubmitOrder,
        accepted: BrokerOrder,
        scenario: SubmitScenario,
    ) -> list[BrokerEvent]:
        fill_quantity = cast(Quantity, scenario.partial_fill_quantity)
        fill_price = cast(Price, scenario.fill_price)
        fill_notional = cast(Money, scenario.fill_notional)

        filled_order = BrokerOrder(
            client_order_id=accepted.client_order_id,
            broker_order_id=accepted.broker_order_id,
            symbol=accepted.symbol,
            side=accepted.side,
            status=BrokerOrderStatus.FILLED
            if scenario.outcome is SubmitOutcome.FULL_FILL
            else BrokerOrderStatus.PARTIALLY_FILLED,
            order_quantity=accepted.order_quantity,
            filled_quantity=fill_quantity,
            remaining_quantity=Quantity(accepted.order_quantity.value - fill_quantity.value),
            limit_price=accepted.limit_price,
            broker_status_code=56 if scenario.outcome is SubmitOutcome.FULL_FILL else 55,
            status_reason="MOCK_" + scenario.outcome.value,
            updated_at=self._clock.now(),
        )
        trade = BrokerTrade(
            trade_id=self._take_trade_id(),
            client_order_id=accepted.client_order_id,
            broker_order_id=accepted.broker_order_id or "MISSING_BROKER_ORDER_ID",
            symbol=accepted.symbol,
            side=accepted.side,
            price=fill_price,
            quantity=fill_quantity,
            notional=fill_notional,
            traded_at=self._clock.now(),
        )
        self._orders[command.client_order_id] = filled_order
        self._trades[trade.trade_id] = trade
        return [BrokerOrderUpdate(filled_order), trade]

    @staticmethod
    def _validate_submit_scenario(command: SubmitOrder, scenario: SubmitScenario) -> None:
        if scenario.outcome not in (SubmitOutcome.PARTIAL_FILL, SubmitOutcome.FULL_FILL):
            return
        fill_quantity = scenario.partial_fill_quantity
        if scenario.fill_price is None or scenario.fill_notional is None or fill_quantity is None:
            raise ValueError("partial-fill scenario requires quantity, price, and notional")
        if scenario.outcome is SubmitOutcome.FULL_FILL:
            if fill_quantity != command.quantity:
                raise ValueError("full-fill quantity must equal order quantity")
        elif fill_quantity.is_zero or fill_quantity.value >= command.quantity.value:
            raise ValueError("partial-fill quantity must be between zero and order quantity")

    def _deliver(self, events: list[BrokerEvent], mode: EventDeliveryMode) -> None:
        # Fictional broker durability is independent of the engine Journal.
        # Persist acceptance/fill BEFORE callbacks or a successful return.
        if self._paper_state is not None:
            self._paper_state.save(
                PaperBrokerState(
                    account_id=self._paper_state.account_id,
                    initial_cash=self._paper_state.initial_cash,
                    request_sequence=self._next_request_seq,
                    order_sequence=self._next_broker_id,
                    trade_sequence=self._next_trade_id,
                    orders=tuple(self._orders.values()),
                    trades=tuple(self._trades.values()),
                )
            )
        if mode is EventDeliveryMode.DEFERRED:
            self._pending_events.extend(events)
            return
        delivered = list(reversed(events)) if mode is EventDeliveryMode.REVERSED else events
        for event in delivered:
            self._emit(event)
            if mode is EventDeliveryMode.DUPLICATE:
                self._emit(event)

    def _emit(self, event: BrokerEvent) -> None:
        sink = self._event_sink
        if sink is None:  # pragma: no cover - defensive against concurrent lifecycle misuse
            raise BrokerNotStartedError("broker gateway is not started")
        sink(event)

    def _require_started(self) -> None:
        if self._event_sink is None:
            raise BrokerNotStartedError("broker gateway is not started")

    def _take_request_seq(self) -> int:
        current = self._next_request_seq
        self._next_request_seq += 1
        return current

    def _take_broker_order_id(self) -> str:
        current = f"MOCK{self._next_broker_id:016d}"
        self._next_broker_id += 1
        return current

    def _take_trade_id(self) -> str:
        current = f"trade_MOCK{self._next_trade_id:016d}"
        self._next_trade_id += 1
        return current

    def _raise_query_fault(self, query: BrokerQuery) -> None:
        faults = self._query_faults[query]
        if not faults:
            return
        fault = faults.popleft()
        if fault is QueryFault.TIMEOUT:
            raise BrokerTimeoutError(f"query_{query.value.lower()}", outcome_unknown=True)
        raise BrokerQueryError(f"mock broker {query.value.lower()} query failed")
