from __future__ import annotations

from datetime import time

import pytest

from auto_trading_system.domain import Money, Price, Quantity, SecurityCode, TradingDate
from auto_trading_system.gateways.market import (
    MarketDataQueryError,
    MarketDataTimeoutError,
    MarketDeliveryMode,
    MarketGateway,
    MarketNotStartedError,
    MarketQueryFault,
    MarketTick,
    MockMarketGateway,
    SubscriptionLimitExceeded,
)


def tick(symbol: str = "600000.SH", *, second: int = 0, source_seq: int = 1) -> MarketTick:
    trading_date = TradingDate.parse("2026-09-04")
    return MarketTick(
        symbol=SecurityCode.parse(symbol),
        occurred_at=trading_date.at(time(14, 35, second)),
        received_at=trading_date.at(time(14, 35, second, 1000)),
        last_price=Price.parse("10.01"),
        cumulative_volume=Quantity(10000 + source_seq),
        cumulative_turnover=Money.parse("100100.00"),
        source_seq=source_seq,
    )


def started_market(*, max_subscriptions: int = 50) -> tuple[MockMarketGateway, list[MarketTick]]:
    market = MockMarketGateway(max_subscriptions=max_subscriptions)
    events: list[MarketTick] = []
    market.start(events.append)
    return market, events


def test_mock_market_structurally_satisfies_gateway_port() -> None:
    market, _ = started_market()
    port: MarketGateway = market

    assert port.latest_tick(SecurityCode.parse("600000.SH")) is None


@pytest.mark.parametrize("max_subscriptions", [True, 0, -1])
def test_mock_market_requires_positive_subscription_bound(max_subscriptions: object) -> None:
    with pytest.raises(ValueError, match="max_subscriptions"):
        MockMarketGateway(max_subscriptions=max_subscriptions)  # type: ignore[arg-type]


def test_mock_market_requires_start_and_callable_sink() -> None:
    market = MockMarketGateway()

    with pytest.raises(TypeError, match="event_sink"):
        market.start(None)  # type: ignore[arg-type]
    with pytest.raises(MarketNotStartedError):
        market.subscribe(SecurityCode.parse("600000.SH"))
    with pytest.raises(MarketNotStartedError):
        market.latest_tick(SecurityCode.parse("600000.SH"))


def test_subscribe_is_idempotent_per_symbol() -> None:
    market, _ = started_market()
    symbol = SecurityCode.parse("600000.SH")

    first = market.subscribe(symbol)
    second = market.subscribe(symbol)

    assert first == second
    assert first.subscription_id == 1
    assert market.subscription_count == 1


def test_subscription_limit_is_enforced_before_new_symbol() -> None:
    market, _ = started_market(max_subscriptions=1)
    market.subscribe(SecurityCode.parse("600000.SH"))

    with pytest.raises(SubscriptionLimitExceeded):
        market.subscribe(SecurityCode.parse("000001.SZ"))

    assert market.subscription_count == 1


def test_publish_delivers_only_subscribed_symbols_but_tracks_latest_fact() -> None:
    market, events = started_market()
    unsubscribed_tick = tick()

    assert not market.publish(unsubscribed_tick)
    assert events == []
    assert market.latest_tick(unsubscribed_tick.symbol) == unsubscribed_tick

    market.subscribe(unsubscribed_tick.symbol)
    subscribed_tick = tick(second=1, source_seq=2)
    assert market.publish(subscribed_tick)
    assert events == [subscribed_tick]
    assert market.latest_tick(subscribed_tick.symbol) == subscribed_tick


def test_duplicate_delivery_repeats_identical_tick() -> None:
    market, events = started_market()
    market.subscribe(SecurityCode.parse("600000.SH"))
    published = tick()

    market.publish(published, mode=MarketDeliveryMode.DUPLICATE)

    assert events == [published, published]
    assert events[0] is events[1]


def test_deferred_ticks_can_be_flushed_out_of_order_and_duplicated() -> None:
    market, events = started_market()
    market.subscribe(SecurityCode.parse("600000.SH"))
    first = tick(second=1, source_seq=1)
    second = tick(second=2, source_seq=2)
    market.publish(first, mode=MarketDeliveryMode.DEFERRED)
    market.publish(second, mode=MarketDeliveryMode.DEFERRED)
    assert market.pending_event_count == 2

    market.flush_pending(reverse=True, duplicate=True)

    assert market.pending_event_count == 0
    assert events == [second, second, first, first]


def test_out_of_order_tick_does_not_replace_newer_latest_fact() -> None:
    market, _ = started_market()
    symbol = SecurityCode.parse("600000.SH")
    market.subscribe(symbol)
    newer = tick(second=2, source_seq=2)
    older = tick(second=1, source_seq=1)

    market.publish(newer)
    market.publish(older)

    assert market.latest_tick(symbol) == newer


@pytest.mark.parametrize(
    ("fault", "error_type"),
    [
        (MarketQueryFault.TIMEOUT, MarketDataTimeoutError),
        (MarketQueryFault.FAILURE, MarketDataQueryError),
    ],
)
def test_latest_tick_query_faults_are_explicit_and_one_shot(
    fault: MarketQueryFault,
    error_type: type[Exception],
) -> None:
    market, _ = started_market()
    symbol = SecurityCode.parse("600000.SH")
    market.queue_query_fault(fault)

    with pytest.raises(error_type):
        market.latest_tick(symbol)

    assert market.latest_tick(symbol) is None


def test_unsubscribe_is_idempotent_and_stops_callback_delivery() -> None:
    market, events = started_market()
    subscription = market.subscribe(SecurityCode.parse("600000.SH"))

    assert market.unsubscribe(subscription.subscription_id)
    assert not market.unsubscribe(subscription.subscription_id)
    assert market.subscription_count == 0
    assert not market.publish(tick())
    assert events == []


def test_stop_clears_subscriptions_and_pending_callbacks_but_preserves_latest() -> None:
    market, events = started_market()
    symbol = SecurityCode.parse("600000.SH")
    market.subscribe(symbol)
    published = tick()
    market.publish(published, mode=MarketDeliveryMode.DEFERRED)

    market.stop()

    assert market.subscription_count == 0
    assert market.pending_event_count == 0
    with pytest.raises(MarketNotStartedError):
        market.flush_pending()
    market.start(events.append)
    assert market.latest_tick(symbol) == published
