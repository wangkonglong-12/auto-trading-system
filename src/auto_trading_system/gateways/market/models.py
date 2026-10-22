"""Market-neutral tick and order-book value objects."""

from __future__ import annotations

from dataclasses import dataclass

from auto_trading_system.domain import (
    DomainValueError,
    Money,
    Price,
    Quantity,
    SecurityCode,
    ShanghaiTimestamp,
)


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Price
    quantity: Quantity


@dataclass(frozen=True, slots=True)
class MarketTick:
    symbol: SecurityCode
    occurred_at: ShanghaiTimestamp
    received_at: ShanghaiTimestamp
    last_price: Price
    cumulative_volume: Quantity
    cumulative_turnover: Money
    bids: tuple[BookLevel, ...] = ()
    asks: tuple[BookLevel, ...] = ()
    source_seq: int | None = None
    trading_status: int | None = None

    def __post_init__(self) -> None:
        if self.received_at < self.occurred_at:
            raise DomainValueError(
                "MARKET_TIME_REGRESSION", "received_at", "must not precede occurred_at"
            )
        if len(self.bids) > 10 or len(self.asks) > 10:
            raise DomainValueError(
                "ORDER_BOOK_DEPTH_EXCEEDED", "bids/asks", "supports at most ten levels"
            )
        if self.source_seq is not None and (
            type(self.source_seq) is not int or self.source_seq < 0
        ):
            raise DomainValueError(
                "INVALID_MARKET_SEQUENCE", "source_seq", "must be a non-negative integer"
            )
        if self.trading_status is not None and (
            type(self.trading_status) is not int or not 0 <= self.trading_status <= 255
        ):
            raise DomainValueError(
                "INVALID_TRADING_STATUS", "trading_status", "must be an unsigned byte"
            )


@dataclass(frozen=True, slots=True)
class MarketSubscription:
    subscription_id: int
    symbol: SecurityCode

    def __post_init__(self) -> None:
        if type(self.subscription_id) is not int or self.subscription_id <= 0:
            raise DomainValueError(
                "INVALID_SUBSCRIPTION_ID",
                "subscription_id",
                "must be a positive integer",
            )
