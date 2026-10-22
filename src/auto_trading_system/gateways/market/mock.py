"""Deterministic in-memory market-data adapter."""

from __future__ import annotations

from collections import deque
from enum import StrEnum

from auto_trading_system.domain import SecurityCode
from auto_trading_system.gateways.market.errors import (
    MarketDataQueryError,
    MarketDataTimeoutError,
    MarketNotStartedError,
    SubscriptionLimitExceeded,
)
from auto_trading_system.gateways.market.models import MarketSubscription, MarketTick
from auto_trading_system.gateways.market.port import MarketEventSink


class MarketDeliveryMode(StrEnum):
    NORMAL = "NORMAL"
    DUPLICATE = "DUPLICATE"
    DEFERRED = "DEFERRED"


class MarketQueryFault(StrEnum):
    TIMEOUT = "TIMEOUT"
    FAILURE = "FAILURE"


class MockMarketGateway:
    """Market feed with explicit subscriptions, buffering, faults, and replay order."""

    def __init__(self, *, max_subscriptions: int = 50) -> None:
        if type(max_subscriptions) is not int or max_subscriptions <= 0:
            raise ValueError("max_subscriptions must be a positive integer")
        self._max_subscriptions = max_subscriptions
        self._event_sink: MarketEventSink | None = None
        self._next_subscription_id = 1
        self._subscriptions_by_symbol: dict[SecurityCode, MarketSubscription] = {}
        self._symbols_by_subscription: dict[int, SecurityCode] = {}
        self._latest: dict[SecurityCode, MarketTick] = {}
        self._pending: list[MarketTick] = []
        self._query_faults: deque[MarketQueryFault] = deque()

    def start(self, event_sink: MarketEventSink) -> None:
        if not callable(event_sink):
            raise TypeError("event_sink must be callable")
        self._event_sink = event_sink

    def stop(self) -> None:
        self._event_sink = None
        self._subscriptions_by_symbol.clear()
        self._symbols_by_subscription.clear()
        self._pending.clear()

    def subscribe(self, symbol: SecurityCode) -> MarketSubscription:
        self._require_started()
        existing = self._subscriptions_by_symbol.get(symbol)
        if existing is not None:
            return existing
        if len(self._subscriptions_by_symbol) >= self._max_subscriptions:
            raise SubscriptionLimitExceeded(
                f"market subscription limit {self._max_subscriptions} exceeded"
            )
        subscription = MarketSubscription(self._next_subscription_id, symbol)
        self._next_subscription_id += 1
        self._subscriptions_by_symbol[symbol] = subscription
        self._symbols_by_subscription[subscription.subscription_id] = symbol
        return subscription

    def unsubscribe(self, subscription_id: int) -> bool:
        self._require_started()
        symbol = self._symbols_by_subscription.pop(subscription_id, None)
        if symbol is None:
            return False
        del self._subscriptions_by_symbol[symbol]
        return True

    def latest_tick(self, symbol: SecurityCode) -> MarketTick | None:
        self._require_started()
        if self._query_faults:
            fault = self._query_faults.popleft()
            if fault is MarketQueryFault.TIMEOUT:
                raise MarketDataTimeoutError("mock latest-tick query timed out")
            raise MarketDataQueryError("mock latest-tick query failed")
        return self._latest.get(symbol)

    def queue_query_fault(self, fault: MarketQueryFault) -> None:
        self._query_faults.append(fault)

    def publish(
        self,
        tick: MarketTick,
        *,
        mode: MarketDeliveryMode = MarketDeliveryMode.NORMAL,
    ) -> bool:
        """Publish a fact; return whether its subscribed callback path was selected."""
        self._require_started()
        current = self._latest.get(tick.symbol)
        if current is None or tick.occurred_at >= current.occurred_at:
            self._latest[tick.symbol] = tick
        if tick.symbol not in self._subscriptions_by_symbol:
            return False
        if mode is MarketDeliveryMode.DEFERRED:
            self._pending.append(tick)
            return True
        self._emit(tick)
        if mode is MarketDeliveryMode.DUPLICATE:
            self._emit(tick)
        return True

    def flush_pending(self, *, reverse: bool = False, duplicate: bool = False) -> None:
        self._require_started()
        ticks = list(reversed(self._pending)) if reverse else list(self._pending)
        self._pending.clear()
        for tick in ticks:
            self._emit(tick)
            if duplicate:
                self._emit(tick)

    @property
    def pending_event_count(self) -> int:
        return len(self._pending)

    @property
    def subscription_count(self) -> int:
        return len(self._subscriptions_by_symbol)

    def _emit(self, tick: MarketTick) -> None:
        sink = self._event_sink
        if sink is None:  # pragma: no cover - defensive against concurrent lifecycle misuse
            raise MarketNotStartedError("market gateway is not started")
        sink(tick)

    def _require_started(self) -> None:
        if self._event_sink is None:
            raise MarketNotStartedError("market gateway is not started")
