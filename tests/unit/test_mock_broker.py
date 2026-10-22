from __future__ import annotations

from dataclasses import dataclass

import pytest

from auto_trading_system.domain import (
    Clock,
    Money,
    Price,
    Quantity,
    SecurityCode,
    ShanghaiTimestamp,
)
from auto_trading_system.gateways.broker import (
    BrokerCancelError,
    BrokerEvent,
    BrokerGateway,
    BrokerNotStartedError,
    BrokerOrderStatus,
    BrokerOrderUpdate,
    BrokerQuery,
    BrokerQueryError,
    BrokerTimeoutError,
    BrokerTrade,
    CancelOrder,
    CancelOutcome,
    CancelScenario,
    DuplicateClientOrderError,
    EventDeliveryMode,
    MockBrokerGateway,
    OrderNotCancelableError,
    OrderSide,
    QueryFault,
    SubmitOrder,
    SubmitOutcome,
    SubmitScenario,
    UnknownOrderError,
)


@dataclass(frozen=True, slots=True)
class FixedClock:
    value: ShanghaiTimestamp

    def now(self) -> ShanghaiTimestamp:
        return self.value


def fixed_clock() -> FixedClock:
    return FixedClock(ShanghaiTimestamp.parse("2026-09-04T14:35:00+08:00"))


def command(client_order_id: str = "ord_01K43TAIL000000001") -> SubmitOrder:
    return SubmitOrder(
        client_order_id=client_order_id,
        symbol=SecurityCode.parse("000001.SZ"),
        side=OrderSide.BUY,
        quantity=Quantity(100),
        limit_price=Price.parse("12.34"),
        remark_token="ATS000000000000000000001",
    )


def started_broker() -> tuple[MockBrokerGateway, list[BrokerEvent]]:
    clock: Clock = fixed_clock()
    broker = MockBrokerGateway(clock)
    events: list[BrokerEvent] = []
    broker.start(events.append)
    return broker, events


def cancel_for(broker: MockBrokerGateway, client_order_id: str) -> CancelOrder:
    order = broker.find_order_by_client_id(client_order_id)
    assert order is not None
    assert order.broker_order_id is not None
    return CancelOrder(client_order_id, order.broker_order_id)


def test_mock_broker_structurally_satisfies_gateway_port() -> None:
    broker, _ = started_broker()
    port: BrokerGateway = broker

    assert port.query_orders() == ()
    assert port.query_trades() == ()


def test_mock_broker_requires_start_and_callable_sink() -> None:
    broker = MockBrokerGateway(fixed_clock())

    with pytest.raises(TypeError, match="event_sink"):
        broker.start(None)  # type: ignore[arg-type]
    with pytest.raises(BrokerNotStartedError):
        broker.submit_order(command())
    with pytest.raises(BrokerNotStartedError):
        broker.query_orders()


def test_default_submit_is_accepted_and_queryable() -> None:
    broker, events = started_broker()

    receipt = broker.submit_order(command())
    order = broker.find_order_by_client_id(command().client_order_id)

    assert receipt.request_seq == 1
    assert order is not None
    assert order.status is BrokerOrderStatus.ACKED
    assert broker.query_orders() == (order,)
    assert events == [BrokerOrderUpdate(order)]


def test_rejected_submit_returns_request_receipt_and_terminal_update() -> None:
    broker, events = started_broker()
    broker.queue_submit(
        SubmitScenario(outcome=SubmitOutcome.REJECT, reject_reason="MOCK_LIMIT_REJECTED")
    )

    receipt = broker.submit_order(command())
    update = events[0]

    assert receipt.request_seq == 1
    assert isinstance(update, BrokerOrderUpdate)
    assert update.order.status is BrokerOrderStatus.REJECTED
    assert update.order.status_reason == "MOCK_LIMIT_REJECTED"


def test_partial_fill_emits_ack_update_trade_and_query_facts() -> None:
    broker, events = started_broker()
    broker.queue_submit(
        SubmitScenario(
            outcome=SubmitOutcome.PARTIAL_FILL,
            partial_fill_quantity=Quantity(40),
            fill_price=Price.parse("12.35"),
            fill_notional=Money.parse("494.00"),
        )
    )

    broker.submit_order(command())

    assert len(events) == 3
    assert isinstance(events[0], BrokerOrderUpdate)
    assert isinstance(events[1], BrokerOrderUpdate)
    assert isinstance(events[2], BrokerTrade)
    partial = broker.query_orders()[0]
    assert partial.status is BrokerOrderStatus.PARTIALLY_FILLED
    assert partial.filled_quantity == Quantity(40)
    assert partial.remaining_quantity == Quantity(60)
    assert broker.query_trades() == (events[2],)


@pytest.mark.parametrize(
    "scenario",
    [
        SubmitScenario(outcome=SubmitOutcome.PARTIAL_FILL),
        SubmitScenario(
            outcome=SubmitOutcome.PARTIAL_FILL,
            partial_fill_quantity=Quantity(0),
            fill_price=Price.parse("12.35"),
            fill_notional=Money.parse("1.00"),
        ),
        SubmitScenario(
            outcome=SubmitOutcome.PARTIAL_FILL,
            partial_fill_quantity=Quantity(100),
            fill_price=Price.parse("12.35"),
            fill_notional=Money.parse("1235.00"),
        ),
    ],
)
def test_invalid_partial_fill_scenario_does_not_create_broker_fact(
    scenario: SubmitScenario,
) -> None:
    broker, _ = started_broker()
    broker.queue_submit(scenario)

    with pytest.raises(ValueError, match="partial-fill"):
        broker.submit_order(command())

    assert broker.query_orders() == ()


def test_reversed_delivery_simulates_trade_before_order_callbacks() -> None:
    broker, events = started_broker()
    broker.queue_submit(
        SubmitScenario(
            outcome=SubmitOutcome.PARTIAL_FILL,
            delivery=EventDeliveryMode.REVERSED,
            partial_fill_quantity=Quantity(40),
            fill_price=Price.parse("12.35"),
            fill_notional=Money.parse("494.00"),
        )
    )

    broker.submit_order(command())

    assert isinstance(events[0], BrokerTrade)
    assert isinstance(events[1], BrokerOrderUpdate)
    assert events[1].order.status is BrokerOrderStatus.PARTIALLY_FILLED
    assert isinstance(events[2], BrokerOrderUpdate)
    assert events[2].order.status is BrokerOrderStatus.ACKED


def test_duplicate_delivery_repeats_each_broker_callback_unchanged() -> None:
    broker, events = started_broker()
    broker.queue_submit(SubmitScenario(delivery=EventDeliveryMode.DUPLICATE))

    broker.submit_order(command())

    assert len(events) == 2
    assert events[0] is events[1]


def test_deferred_events_can_be_flushed_reversed_and_duplicated() -> None:
    broker, events = started_broker()
    broker.queue_submit(
        SubmitScenario(
            outcome=SubmitOutcome.PARTIAL_FILL,
            delivery=EventDeliveryMode.DEFERRED,
            partial_fill_quantity=Quantity(40),
            fill_price=Price.parse("12.35"),
            fill_notional=Money.parse("494.00"),
        )
    )
    broker.submit_order(command())
    assert broker.pending_event_count == 3
    assert events == []

    broker.flush_pending(reverse=True, duplicate=True)

    assert broker.pending_event_count == 0
    assert len(events) == 6
    assert isinstance(events[0], BrokerTrade)
    assert events[0] is events[1]


def test_arbitrary_stale_callback_can_be_injected() -> None:
    broker, events = started_broker()
    broker.submit_order(command())
    stale = events[0]

    broker.inject_event(stale)

    assert events == [stale, stale]


def test_timeout_before_accept_has_known_not_applied_outcome() -> None:
    broker, events = started_broker()
    broker.queue_submit(SubmitScenario(outcome=SubmitOutcome.TIMEOUT_BEFORE_ACCEPT))

    with pytest.raises(BrokerTimeoutError) as error:
        broker.submit_order(command())

    assert not error.value.outcome_unknown
    assert broker.find_order_by_client_id(command().client_order_id) is None
    assert events == []


def test_timeout_after_accept_requires_query_instead_of_resubmit() -> None:
    broker, events = started_broker()
    broker.queue_submit(SubmitScenario(outcome=SubmitOutcome.TIMEOUT_AFTER_ACCEPT))

    with pytest.raises(BrokerTimeoutError) as error:
        broker.submit_order(command())

    assert error.value.outcome_unknown
    assert broker.find_order_by_client_id(command().client_order_id) is not None
    assert len(events) == 1
    with pytest.raises(DuplicateClientOrderError):
        broker.submit_order(command())


def test_cancel_unfilled_order_emits_cancelled_terminal_fact() -> None:
    broker, events = started_broker()
    broker.submit_order(command())

    receipt = broker.cancel_order(cancel_for(broker, command().client_order_id))

    assert receipt.request_seq == 2
    update = events[-1]
    assert isinstance(update, BrokerOrderUpdate)
    assert update.order.status is BrokerOrderStatus.CANCELLED
    assert update.order.broker_status_code == 54


def test_cancel_partial_order_preserves_fill_and_emits_partial_cancel() -> None:
    broker, events = started_broker()
    broker.queue_submit(
        SubmitScenario(
            outcome=SubmitOutcome.PARTIAL_FILL,
            partial_fill_quantity=Quantity(40),
            fill_price=Price.parse("12.35"),
            fill_notional=Money.parse("494.00"),
        )
    )
    broker.submit_order(command())

    broker.cancel_order(cancel_for(broker, command().client_order_id))

    update = events[-1]
    assert isinstance(update, BrokerOrderUpdate)
    assert update.order.status is BrokerOrderStatus.PARTIALLY_FILLED_CANCELLED
    assert update.order.filled_quantity == Quantity(40)
    assert update.order.remaining_quantity == Quantity(60)
    assert update.order.broker_status_code == 53


def test_cancel_rejection_is_callback_fact_not_command_exception() -> None:
    broker, events = started_broker()
    broker.submit_order(command())
    broker.queue_cancel(CancelScenario(outcome=CancelOutcome.REJECT))

    receipt = broker.cancel_order(cancel_for(broker, command().client_order_id))

    assert receipt.request_seq == 2
    assert isinstance(events[-1], BrokerCancelError)
    assert broker.query_orders()[0].status is BrokerOrderStatus.ACKED


@pytest.mark.parametrize(
    ("outcome", "unknown", "expected_status"),
    [
        (CancelOutcome.TIMEOUT_BEFORE_APPLY, False, BrokerOrderStatus.ACKED),
        (CancelOutcome.TIMEOUT_AFTER_APPLY, True, BrokerOrderStatus.CANCELLED),
    ],
)
def test_cancel_timeout_exposes_outcome_uncertainty(
    outcome: CancelOutcome,
    unknown: bool,
    expected_status: BrokerOrderStatus,
) -> None:
    broker, _ = started_broker()
    broker.submit_order(command())
    broker.queue_cancel(CancelScenario(outcome=outcome))

    with pytest.raises(BrokerTimeoutError) as error:
        broker.cancel_order(cancel_for(broker, command().client_order_id))

    assert error.value.outcome_unknown is unknown
    assert broker.query_orders()[0].status is expected_status


def test_cancel_rejects_unknown_or_terminal_order() -> None:
    broker, _ = started_broker()
    broker.submit_order(command())

    with pytest.raises(UnknownOrderError):
        broker.cancel_order(CancelOrder(command().client_order_id, "WRONG"))

    cancel = cancel_for(broker, command().client_order_id)
    broker.cancel_order(cancel)
    with pytest.raises(OrderNotCancelableError):
        broker.cancel_order(cancel)


@pytest.mark.parametrize("query", list(BrokerQuery))
@pytest.mark.parametrize("fault", list(QueryFault))
def test_query_faults_are_explicit_and_one_shot(query: BrokerQuery, fault: QueryFault) -> None:
    broker, _ = started_broker()
    broker.queue_query_fault(query, fault)

    with pytest.raises(BrokerTimeoutError if fault is QueryFault.TIMEOUT else BrokerQueryError):
        if query is BrokerQuery.ORDERS:
            broker.query_orders()
        elif query is BrokerQuery.TRADES:
            broker.query_trades()
        else:
            broker.find_order_by_client_id(command().client_order_id)

    if query is BrokerQuery.ORDERS:
        assert broker.query_orders() == ()
    elif query is BrokerQuery.TRADES:
        assert broker.query_trades() == ()
    else:
        assert broker.find_order_by_client_id(command().client_order_id) is None


def test_stop_drops_old_pending_callbacks_but_preserves_broker_facts() -> None:
    broker, events = started_broker()
    broker.queue_submit(SubmitScenario(delivery=EventDeliveryMode.DEFERRED))
    broker.submit_order(command())
    assert broker.pending_event_count == 1

    broker.stop()

    assert broker.pending_event_count == 0
    with pytest.raises(BrokerNotStartedError):
        broker.query_orders()
    broker.start(events.append)
    assert len(broker.query_orders()) == 1
