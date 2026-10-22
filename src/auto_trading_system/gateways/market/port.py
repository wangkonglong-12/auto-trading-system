"""Market-data gateway port owned by the trading application."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from auto_trading_system.domain import SecurityCode
from auto_trading_system.gateways.market.models import MarketSubscription, MarketTick

MarketEventSink = Callable[[MarketTick], None]


class MarketGateway(Protocol):
    """Subscription and latest-fact boundary for live and replay adapters."""

    def start(self, event_sink: MarketEventSink) -> None: ...

    def stop(self) -> None: ...

    def subscribe(self, symbol: SecurityCode) -> MarketSubscription: ...

    def unsubscribe(self, subscription_id: int) -> bool: ...

    def latest_tick(self, symbol: SecurityCode) -> MarketTick | None: ...
