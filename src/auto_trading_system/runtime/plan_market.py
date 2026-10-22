"""Keep market subscriptions exactly aligned with the currently loaded plan."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from queue import Empty, Full, Queue

from pydantic import JsonValue

from auto_trading_system.domain import SecurityCode
from auto_trading_system.gateways.market import MarketGateway, MarketSubscription, MarketTick
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.plans import PlanPayload
from auto_trading_system.runtime.engine_v2 import EngineKernel


@dataclass(frozen=True)
class PlanMarketSnapshot:
    state: str
    plan_hash: str | None
    symbols: tuple[str, ...]
    latest_symbols: tuple[str, ...]
    failure_code: str | None


class PlanMarketRuntime:
    """Owner-polled plan reconciler; SDK callbacks only enqueue immutable ticks."""

    def __init__(
        self,
        market: MarketGateway,
        *,
        fault_reader: Callable[[], str | None] = lambda: None,
        maximum_symbols: int = 15,
        queue_capacity: int = 2048,
    ) -> None:
        if not 1 <= maximum_symbols <= 15 or not 1 <= queue_capacity <= 65_536:
            raise ValueError("invalid plan market runtime bounds")
        self.market = market
        self.fault_reader = fault_reader
        self.maximum_symbols = maximum_symbols
        self.events: Queue[MarketTick] = Queue(maxsize=queue_capacity)
        self.subscriptions: dict[str, MarketSubscription] = {}
        self.latest: dict[str, MarketTick] = {}
        self.plan_hash: str | None = None
        self.failure_code: str | None = None
        market.start(self._enqueue)

    def _enqueue(self, tick: MarketTick) -> None:
        try:
            self.events.put_nowait(tick)
        except Full:
            self.failure_code = "MARKET_EVENT_QUEUE_OVERFLOW"

    def poll(
        self, engine: EngineKernel, *, extra_symbols: tuple[str, ...] = ()
    ) -> PlanMarketSnapshot:
        if self.failure_code is None:
            self.failure_code = self.fault_reader()
        try:
            if self.failure_code is None:
                loaded = engine.plans.plans.loaded()
                self.reconcile(loaded.payload if loaded is not None else None, extra_symbols)
                self._drain()
        except Exception:
            self.failure_code = "MARKET_SUBSCRIPTION_SYNC_FAILED"
        if self.failure_code is not None:
            engine.emergency_halt(self.failure_code)
        engine.poll()
        return self.snapshot()

    def reconcile(self, plan: PlanPayload | None, extra_symbols: tuple[str, ...] = ()) -> None:
        planned = (
            tuple(item.symbol for item in sorted(plan.allocations, key=lambda item: item.rank))
            if plan is not None
            else ()
        )
        wanted = tuple(dict.fromkeys((*extra_symbols, *planned)))
        if len(wanted) > self.maximum_symbols:
            raise ValueError("plan exceeds the installed market subscription limit")
        wanted_set = set(wanted)
        additions: list[MarketSubscription] = []
        try:
            for wire in wanted:
                if wire not in self.subscriptions:
                    subscription = self.market.subscribe(SecurityCode.parse(wire))
                    self.subscriptions[wire] = subscription
                    additions.append(subscription)
        except Exception:
            for subscription in reversed(additions):
                try:
                    self.market.unsubscribe(subscription.subscription_id)
                finally:
                    self.subscriptions.pop(subscription.symbol.to_wire(), None)
            raise
        for wire in tuple(self.subscriptions):
            if wire not in wanted_set:
                subscription = self.subscriptions[wire]
                if not self.market.unsubscribe(subscription.subscription_id):
                    raise ValueError("owned market subscription disappeared")
                del self.subscriptions[wire]
                self.latest.pop(wire, None)
        self.plan_hash = (
            digest(canonical_bytes(plan.model_dump(mode="json"))) if plan is not None else None
        )

    def _drain(self) -> None:
        while True:
            try:
                tick = self.events.get_nowait()
            except Empty:
                return
            wire = tick.symbol.to_wire()
            if wire not in self.subscriptions:
                continue
            current = self.latest.get(wire)
            if current is None or tick.occurred_at >= current.occurred_at:
                self.latest[wire] = tick

    def stop(self) -> None:
        self.market.stop()
        self.subscriptions.clear()
        self.latest.clear()
        self.plan_hash = None

    def snapshot(self) -> PlanMarketSnapshot:
        return PlanMarketSnapshot(
            "FAILED"
            if self.failure_code
            else "WAITING_PLAN"
            if self.plan_hash is None
            else "SUBSCRIBED",
            self.plan_hash,
            tuple(self.subscriptions),
            tuple(sorted(self.latest)),
            self.failure_code,
        )

    def status(self) -> dict[str, JsonValue]:
        value = self.snapshot()
        return {
            "state": value.state,
            "plan_hash": value.plan_hash,
            "subscription_count": len(value.symbols),
            "symbols": list(value.symbols),
            "latest_quote_count": len(value.latest_symbols),
            "failure_code": value.failure_code,
        }
