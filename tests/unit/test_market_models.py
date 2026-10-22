from __future__ import annotations

from dataclasses import replace

import pytest

from auto_trading_system.domain import (
    DomainValueError,
    Money,
    Price,
    Quantity,
    SecurityCode,
    ShanghaiTimestamp,
)
from auto_trading_system.gateways.market import BookLevel, MarketSubscription, MarketTick


def market_tick() -> MarketTick:
    return MarketTick(
        symbol=SecurityCode.parse("600000.SH"),
        occurred_at=ShanghaiTimestamp.parse("2026-09-04T14:35:00+08:00"),
        received_at=ShanghaiTimestamp.parse("2026-09-04T14:35:00.001+08:00"),
        last_price=Price.parse("10.01"),
        cumulative_volume=Quantity(10000),
        cumulative_turnover=Money.parse("100100.00"),
        bids=(BookLevel(Price.parse("10.00"), Quantity(500)),),
        asks=(BookLevel(Price.parse("10.02"), Quantity(600)),),
        source_seq=1,
    )


def test_market_tick_accepts_bounded_book() -> None:
    tick = market_tick()

    assert tick.last_price == Price.parse("10.01")
    assert tick.bids[0].quantity == Quantity(500)


def test_market_tick_rejects_receive_time_before_exchange_time() -> None:
    with pytest.raises(DomainValueError, match="MARKET_TIME_REGRESSION"):
        replace(
            market_tick(),
            received_at=ShanghaiTimestamp.parse("2026-09-04T14:34:59+08:00"),
        )


@pytest.mark.parametrize("side", ["bids", "asks"])
def test_market_tick_caps_each_book_side_at_ten_levels(side: str) -> None:
    levels = tuple(BookLevel(Price.parse("10"), Quantity(1)) for _ in range(11))

    with pytest.raises(DomainValueError, match="ORDER_BOOK_DEPTH_EXCEEDED"):
        replace(market_tick(), **{side: levels})  # type: ignore[arg-type]


@pytest.mark.parametrize("source_seq", [True, -1])
def test_market_tick_rejects_invalid_source_sequence(source_seq: object) -> None:
    with pytest.raises(DomainValueError, match="INVALID_MARKET_SEQUENCE"):
        replace(market_tick(), source_seq=source_seq)  # type: ignore[arg-type]


def test_market_tick_allows_missing_source_sequence() -> None:
    assert replace(market_tick(), source_seq=None).source_seq is None


@pytest.mark.parametrize("status", [True, -1, 256])
def test_market_tick_rejects_invalid_trading_status(status: object) -> None:
    with pytest.raises(DomainValueError, match="INVALID_TRADING_STATUS"):
        replace(market_tick(), trading_status=status)  # type: ignore[arg-type]


@pytest.mark.parametrize("subscription_id", [True, 0, -1])
def test_market_subscription_requires_positive_integer(subscription_id: object) -> None:
    with pytest.raises(DomainValueError, match="INVALID_SUBSCRIPTION_ID"):
        MarketSubscription(
            subscription_id=subscription_id,  # type: ignore[arg-type]
            symbol=SecurityCode.parse("600000.SH"),
        )
