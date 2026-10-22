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
    BrokerOrderStatus,
    BrokerOrderUpdate,
    BrokerTimeoutError,
    BrokerTrade,
    CancelOrder,
    CancelOutcome,
    CancelScenario,
    DuplicateClientOrderError,
    EventDeliveryMode,
    MockBrokerGateway,
    OrderSide,
    SubmitOrder,
    SubmitOutcome,
    SubmitScenario,
)


@dataclass(frozen=True, slots=True)
class MatrixClock:
    value: ShanghaiTimestamp

    def now(self) -> ShanghaiTimestamp:
        return self.value


def _broker() -> tuple[MockBrokerGateway, list[BrokerEvent]]:
    clock: Clock = MatrixClock(ShanghaiTimestamp.parse("2026-09-04T14:35:00+08:00"))
    broker = MockBrokerGateway(clock)
    events: list[BrokerEvent] = []
    broker.start(events.append)
    return broker, events


def _order(client_order_id: str = "ord_01K43TSTMATRIX00001") -> SubmitOrder:
    return SubmitOrder(
        client_order_id=client_order_id,
        symbol=SecurityCode.parse("000001.SZ"),
        side=OrderSide.BUY,
        quantity=Quantity(100),
        limit_price=Price.parse("12.34"),
        remark_token="ATSTSTMATRIX00000000001",
    )


def _cancel(broker: MockBrokerGateway, order: SubmitOrder) -> CancelOrder:
    fact = broker.find_order_by_client_id(order.client_order_id)
    assert fact is not None
    assert fact.broker_order_id is not None
    return CancelOrder(order.client_order_id, fact.broker_order_id)


def test_matrix_acceptance_creates_one_queryable_acked_fact() -> None:
    broker, events = _broker()
    order = _order()

    receipt = broker.submit_order(order)

    assert receipt.request_seq == 1
    assert len(events) == 1
    assert isinstance(events[0], BrokerOrderUpdate)
    assert events[0].order.status is BrokerOrderStatus.ACKED
    assert broker.query_orders() == (events[0].order,)


def test_matrix_rejection_is_a_terminal_broker_fact() -> None:
    broker, events = _broker()
    broker.queue_submit(
        SubmitScenario(outcome=SubmitOutcome.REJECT, reject_reason="MATRIX_REJECTED")
    )

    receipt = broker.submit_order(_order())

    assert receipt.request_seq == 1
    assert len(events) == 1
    assert isinstance(events[0], BrokerOrderUpdate)
    assert events[0].order.status is BrokerOrderStatus.REJECTED
    assert events[0].order.status_reason == "MATRIX_REJECTED"


def test_matrix_partial_fill_preserves_quantities_and_trade_fact() -> None:
    broker, events = _broker()
    broker.queue_submit(
        SubmitScenario(
            outcome=SubmitOutcome.PARTIAL_FILL,
            partial_fill_quantity=Quantity(40),
            fill_price=Price.parse("12.35"),
            fill_notional=Money.parse("494.00"),
        )
    )

    broker.submit_order(_order())

    order_fact = broker.query_orders()[0]
    assert order_fact.status is BrokerOrderStatus.PARTIALLY_FILLED
    assert order_fact.filled_quantity == Quantity(40)
    assert order_fact.remaining_quantity == Quantity(60)
    assert len(broker.query_trades()) == 1
    assert [type(event) for event in events] == [
        BrokerOrderUpdate,
        BrokerOrderUpdate,
        BrokerTrade,
    ]


@pytest.mark.parametrize(
    ("submit_scenario", "expected_status"),
    [
        (SubmitScenario(), BrokerOrderStatus.CANCELLED),
        (
            SubmitScenario(
                outcome=SubmitOutcome.PARTIAL_FILL,
                partial_fill_quantity=Quantity(40),
                fill_price=Price.parse("12.35"),
                fill_notional=Money.parse("494.00"),
            ),
            BrokerOrderStatus.PARTIALLY_FILLED_CANCELLED,
        ),
    ],
)
def test_matrix_cancel_applies_to_unfilled_and_partially_filled_orders(
    submit_scenario: SubmitScenario,
    expected_status: BrokerOrderStatus,
) -> None:
    broker, _ = _broker()
    order = _order()
    broker.queue_submit(submit_scenario)
    broker.submit_order(order)

    receipt = broker.cancel_order(_cancel(broker, order))

    assert receipt.request_seq == 2
    assert broker.query_orders()[0].status is expected_status


def test_matrix_cancel_rejection_keeps_the_current_order_fact() -> None:
    broker, events = _broker()
    order = _order()
    broker.submit_order(order)
    broker.queue_cancel(CancelScenario(outcome=CancelOutcome.REJECT))

    receipt = broker.cancel_order(_cancel(broker, order))

    assert receipt.request_seq == 2
    assert isinstance(events[-1], BrokerCancelError)
    assert broker.query_orders()[0].status is BrokerOrderStatus.ACKED


def test_matrix_timeout_before_accept_is_known_not_applied() -> None:
    broker, events = _broker()
    order = _order()
    broker.queue_submit(SubmitScenario(outcome=SubmitOutcome.TIMEOUT_BEFORE_ACCEPT))

    with pytest.raises(BrokerTimeoutError) as captured:
        broker.submit_order(order)

    assert captured.value.outcome_unknown is False
    assert broker.find_order_by_client_id(order.client_order_id) is None
    assert events == []


def test_matrix_timeout_after_accept_is_queryable_and_blocks_resubmit() -> None:
    broker, _ = _broker()
    order = _order()
    broker.queue_submit(SubmitScenario(outcome=SubmitOutcome.TIMEOUT_AFTER_ACCEPT))

    with pytest.raises(BrokerTimeoutError) as captured:
        broker.submit_order(order)

    assert captured.value.outcome_unknown is True
    assert broker.find_order_by_client_id(order.client_order_id) is not None
    with pytest.raises(DuplicateClientOrderError):
        broker.submit_order(order)


@pytest.mark.parametrize(
    ("outcome", "outcome_unknown", "expected_status"),
    [
        (CancelOutcome.TIMEOUT_BEFORE_APPLY, False, BrokerOrderStatus.ACKED),
        (CancelOutcome.TIMEOUT_AFTER_APPLY, True, BrokerOrderStatus.CANCELLED),
    ],
)
def test_matrix_cancel_timeout_exposes_broker_outcome(
    outcome: CancelOutcome,
    outcome_unknown: bool,
    expected_status: BrokerOrderStatus,
) -> None:
    broker, _ = _broker()
    order = _order()
    broker.submit_order(order)
    broker.queue_cancel(CancelScenario(outcome=outcome))

    with pytest.raises(BrokerTimeoutError) as captured:
        broker.cancel_order(_cancel(broker, order))

    assert captured.value.outcome_unknown is outcome_unknown
    assert broker.query_orders()[0].status is expected_status


def test_matrix_duplicate_callbacks_do_not_duplicate_broker_facts() -> None:
    broker, events = _broker()
    broker.queue_submit(
        SubmitScenario(
            outcome=SubmitOutcome.PARTIAL_FILL,
            delivery=EventDeliveryMode.DUPLICATE,
            partial_fill_quantity=Quantity(40),
            fill_price=Price.parse("12.35"),
            fill_notional=Money.parse("494.00"),
        )
    )

    broker.submit_order(_order())

    assert len(events) == 6
    assert all(events[index] is events[index + 1] for index in range(0, 6, 2))
    assert len(broker.query_orders()) == 1
    assert len(broker.query_trades()) == 1


def test_matrix_reversed_callbacks_keep_final_broker_facts_consistent() -> None:
    broker, events = _broker()
    broker.queue_submit(
        SubmitScenario(
            outcome=SubmitOutcome.PARTIAL_FILL,
            delivery=EventDeliveryMode.REVERSED,
            partial_fill_quantity=Quantity(40),
            fill_price=Price.parse("12.35"),
            fill_notional=Money.parse("494.00"),
        )
    )

    broker.submit_order(_order())

    assert isinstance(events[0], BrokerTrade)
    assert isinstance(events[1], BrokerOrderUpdate)
    assert events[1].order.status is BrokerOrderStatus.PARTIALLY_FILLED
    assert isinstance(events[2], BrokerOrderUpdate)
    assert events[2].order.status is BrokerOrderStatus.ACKED
    assert broker.query_orders()[0].status is BrokerOrderStatus.PARTIALLY_FILLED
    assert len(broker.query_trades()) == 1
