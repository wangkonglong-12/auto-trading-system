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
    BrokerQueryError,
    MockBrokerGateway,
    OrderSide,
    SubmitOrder,
    SubmitOutcome,
    SubmitScenario,
    validate_order_query_result,
    validate_trade_query_result,
)


@dataclass(frozen=True, slots=True)
class FixedClock:
    def now(self) -> ShanghaiTimestamp:
        return ShanghaiTimestamp.parse("2026-09-04T14:35:00+08:00")


def _started_broker() -> MockBrokerGateway:
    clock: Clock = FixedClock()
    broker = MockBrokerGateway(clock)
    broker.start(lambda _: None)
    return broker


def _order() -> SubmitOrder:
    return SubmitOrder(
        client_order_id="ord_01K43QUERYCHECK00001",
        symbol=SecurityCode.parse("000001.SZ"),
        side=OrderSide.BUY,
        quantity=Quantity(100),
        limit_price=Price.parse("12.34"),
        remark_token="ATSQUERYCHECK000000001",
    )


def test_empty_query_snapshots_are_authoritative_empty_facts() -> None:
    assert validate_order_query_result(()) == ()
    assert validate_trade_query_result(()) == ()


def test_typed_order_and_trade_snapshots_are_accepted() -> None:
    broker = _started_broker()
    broker.queue_submit(
        SubmitScenario(
            outcome=SubmitOutcome.PARTIAL_FILL,
            partial_fill_quantity=Quantity(40),
            fill_price=Price.parse("12.35"),
            fill_notional=Money.parse("494.00"),
        )
    )
    broker.submit_order(_order())

    assert validate_order_query_result(broker.query_orders()) == broker.query_orders()
    assert validate_trade_query_result(broker.query_trades()) == broker.query_trades()


@pytest.mark.parametrize("invalid", [None, [], object()])
def test_none_and_untyped_order_results_are_query_failures(invalid: object) -> None:
    with pytest.raises(BrokerQueryError, match="authoritative typed facts"):
        validate_order_query_result(invalid)


@pytest.mark.parametrize("invalid", [None, [], object()])
def test_none_and_untyped_trade_results_are_query_failures(invalid: object) -> None:
    with pytest.raises(BrokerQueryError, match="authoritative typed facts"):
        validate_trade_query_result(invalid)


def test_duplicate_order_or_trade_identities_are_query_failures() -> None:
    broker = _started_broker()
    broker.queue_submit(
        SubmitScenario(
            outcome=SubmitOutcome.PARTIAL_FILL,
            partial_fill_quantity=Quantity(40),
            fill_price=Price.parse("12.35"),
            fill_notional=Money.parse("494.00"),
        )
    )
    broker.submit_order(_order())
    order = broker.query_orders()[0]
    trade = broker.query_trades()[0]

    with pytest.raises(BrokerQueryError, match="duplicate order"):
        validate_order_query_result((order, order))
    with pytest.raises(BrokerQueryError, match="duplicate trade"):
        validate_trade_query_result((trade, trade))
