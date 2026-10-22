"""Live QMT entry driver: signed plan -> subscriptions -> quotes -> order owner.

The driver never grants permission.  It only supplies fresh QMT market facts to
the existing durable entry owner.  The engine still requires recovery and an
explicit local LIVE grant before any order can cross the SDK boundary.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from time import monotonic_ns

from pydantic import JsonValue

from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.plans import PlanPayload, PolicyVersion
from auto_trading_system.runtime.engine_v2 import EngineKernel
from auto_trading_system.runtime.plan_market import PlanMarketRuntime
from auto_trading_system.strategies.entry_gate import EntryQuote, EntryStateGate
from auto_trading_system.strategies.entry_owner import EntryStrategyOwner
from auto_trading_system.strategies.entry_state import EntryInstrument, EntryPolicy
from auto_trading_system.strategies.exit_owner import D1StrategyOwner, ExitQuote, ExitStateGate
from auto_trading_system.strategies.trading_settings import TradingSettings


def _price(value: Decimal) -> str:
    """Preserve the broker's verified price precision as plain decimal text."""
    normalized = value.normalize()
    return format(normalized, "f")


_VOLUME_WINDOW = timedelta(seconds=60)
_MAX_VOLUME_SAMPLES_PER_SYMBOL = 4096
_MAX_RECENT_ACTIVITY = 100


class QmtTradingRuntime:
    """Single-owner live entry composition with fail-closed configuration."""

    def __init__(
        self,
        market: PlanMarketRuntime,
        settings: TradingSettings | None,
        *,
        entry_install: PolicyVersion,
        rulebook: str,
        clock: Callable[[], datetime],
        exit_gate: ExitStateGate | None = None,
    ) -> None:
        self.market = market
        self.settings = settings
        self.entry_install = entry_install
        self.rulebook = rulebook
        self.clock = clock
        self.exit_gate = exit_gate
        self.exit_owner: D1StrategyOwner | None = None
        self.owner: EntryStrategyOwner | None = None
        self.plan_hash: str | None = None
        self.last_sequence: dict[str, int] = {}
        self.volume_windows: dict[str, deque[tuple[datetime, int]]] = {}
        self.failure_code: str | None = None
        self.liquidation_requested = False
        self._activity: deque[dict[str, JsonValue]] = deque(maxlen=_MAX_RECENT_ACTIVITY)
        self._activity_state: dict[str, str] = {}

    def _remember(
        self,
        key: str,
        state: str,
        category: str,
        *,
        symbol: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Keep one bounded, redaction-safe transition history for the local UI."""
        if self._activity_state.get(key) == state:
            return
        self._activity_state[key] = state
        event: dict[str, JsonValue] = {
            "occurred_at": self.clock().isoformat(),
            "category": category,
            "state": state,
        }
        if symbol is not None:
            event["symbol"] = symbol
        if detail is not None:
            event["detail"] = detail
        self._activity.append(event)

    def _observe_activity(self, engine: EngineKernel) -> None:
        if self.failure_code:
            self._remember("driver", self.failure_code, "ERROR")
        if self.owner is not None:
            snapshot = self.owner.snapshot()
            self._remember("entry-driver", snapshot.state, "ENTRY_DRIVER")
            for symbol, state in getattr(snapshot, "progress", ()):
                self._remember("entry:" + symbol, state, "ENTRY_SIGNAL", symbol=symbol)
            for target in getattr(snapshot, "targets", ()):
                state = "BUY_TARGET_READY" if target.target_quantity > 0 else "BUY_TARGET_SKIPPED"
                self._remember(
                    "entry-target:" + target.symbol,
                    state + ":" + target.reason + ":" + str(target.target_quantity),
                    "ENTRY_TARGET",
                    symbol=target.symbol,
                    detail=target.reason,
                )
        if self.exit_owner is not None:
            self._remember("exit-driver", self.exit_owner.snapshot().state, "EXIT_DRIVER")
        if self.liquidation_requested:
            self._remember("liquidation", "REQUESTED", "LIQUIDATION")
        for view in engine.snapshot().orders:
            order = view.order
            if order is None:
                state = view.phase
                symbol = None
                detail = None
            else:
                state = order.status.value
                symbol = str(order.symbol)
                detail = (
                    f"{order.side.value}:{order.filled_quantity.value}/{order.order_quantity.value}"
                )
            self._remember(
                "order:" + view.client_order_id,
                state + (":" + detail if detail else ""),
                "ORDER",
                symbol=symbol,
                detail=detail,
            )

    def liquidation_preview(self) -> tuple[dict[str, JsonValue], ...]:
        if self.exit_gate is None:
            return ()
        return tuple(
            {
                "lot_id": lot.lot_id,
                "symbol": lot.symbol,
                "cost": lot.cost,
            }
            for lot in self.exit_gate.context.lots
        )

    def request_liquidation(self) -> tuple[dict[str, JsonValue], ...]:
        lots = self.liquidation_preview()
        if not lots:
            raise ValueError("NO_STRATEGY_POSITIONS")
        self.liquidation_requested = True
        if self.exit_owner is not None:
            self.exit_owner.request_liquidation()
        return lots

    def _policy(self, plan: PlanPayload) -> EntryPolicy | None:
        settings = self.settings
        if settings is None or not settings.buy.enabled:
            return None
        ticks = self.market.latest
        if any(item.symbol not in ticks for item in plan.allocations):
            return None
        instruments = []
        for allocation in sorted(plan.allocations, key=lambda item: item.rank):
            tick = ticks[allocation.symbol]
            reference = tick.last_price.amount
            rule_reader = getattr(self.market.market, "instrument_rule", None)
            if not callable(rule_reader):
                raise ValueError("live QMT instrument rules are unavailable")
            rule = rule_reader(tick.symbol)
            evidence: dict[str, JsonValue] = {
                "symbol": allocation.symbol,
                "occurred_at": tick.occurred_at.value.isoformat(),
                "last_price": str(reference),
                "source_seq": tick.source_seq,
                "rulebook": self.rulebook,
                "price_tick": str(rule.price_tick),
                "lower_price": str(rule.lower_price),
                "upper_price": str(rule.upper_price),
            }
            instruments.append(
                EntryInstrument(
                    symbol=allocation.symbol,
                    reference_price=_price(reference),
                    lower_price=_price(rule.lower_price),
                    upper_price=_price(rule.upper_price),
                    price_tick=_price(rule.price_tick),
                    min_quantity=rule.min_buy_quantity,
                    quantity_step=rule.buy_quantity_step,
                    max_order_quantity=settings.advanced.max_order_quantity,
                    source_hash=digest(canonical_bytes(evidence)),
                )
            )
        return EntryPolicy(
            version=self.entry_install.version,
            rulebook_version=self.rulebook,
            fee_reserve="5.00",
            max_chase_bps=settings.advanced.max_chase_bps,
            max_spread_bps=settings.advanced.max_spread_bps,
            participation_bps=1000,
            depth_bps=5000,
            quote_max_age_ms=settings.advanced.quote_max_age_ms,
            cancel_after_seconds=settings.buy.cancel_after_seconds,
            max_children=settings.buy.max_retries + 1,
            max_active_orders=1,
            instruments=tuple(instruments),
        )

    def _feed(self) -> None:
        assert self.owner is not None
        for symbol, tick in tuple(self.market.latest.items()):
            if symbol not in self.owner.gate.rules:
                continue
            sequence = tick.source_seq
            if sequence is None or sequence <= self.last_sequence.get(symbol, 0):
                continue
            current = tick.cumulative_volume.value
            samples = self.volume_windows.setdefault(symbol, deque())
            if samples and (tick.occurred_at.value < samples[-1][0] or current < samples[-1][1]):
                # A reconnect/day reset makes the old cumulative-volume anchor
                # unusable. Rebuild a complete window before admitting a buy.
                samples.clear()
            if samples and tick.occurred_at.value == samples[-1][0]:
                samples[-1] = (tick.occurred_at.value, current)
            else:
                samples.append((tick.occurred_at.value, current))
            cutoff = tick.occurred_at.value - _VOLUME_WINDOW
            while len(samples) > 1 and samples[1][0] <= cutoff:
                samples.popleft()
            if len(samples) > _MAX_VOLUME_SAMPLES_PER_SYMBOL:
                raise ValueError("QMT rolling-volume sample capacity exceeded")
            path_complete = bool(samples and samples[0][0] <= cutoff)
            recent = 0
            if path_complete:
                # Use the first sample on or after the 60-second boundary.  The
                # older anchor proves the boundary was crossed, but subtracting
                # it would include volume from before the requested window.
                baseline = samples[0]
                if baseline[0] < cutoff:
                    baseline = samples[1]
                recent = current - baseline[1]
            self.last_sequence[symbol] = sequence
            bid = tick.bids[0] if tick.bids else None
            ask = tick.asks[0] if tick.asks else None
            if bid is None or ask is None:
                continue
            rule = self.owner.gate.rules[symbol]
            self.owner.quote(
                EntryQuote(
                    symbol=symbol,
                    sequence=sequence,
                    observed_at=tick.occurred_at.value,
                    received_ns=monotonic_ns(),
                    bid_price=str(bid.price.amount),
                    ask_price=str(ask.price.amount),
                    ask_quantity=ask.quantity.value,
                    recent_volume=recent,
                    cage_upper=rule.upper_price,
                    path_complete=path_complete,
                    tradable=tick.trading_status == 13,
                )
            )

    def poll(self, engine: EngineKernel) -> None:
        exit_symbols = (
            tuple(dict.fromkeys(lot.symbol for lot in self.exit_gate.context.lots))
            if self.exit_gate is not None
            else ()
        )
        self.market.poll(engine, extra_symbols=exit_symbols)
        if self.failure_code is not None:
            engine.emergency_halt(self.failure_code)
            self._remember("driver", self.failure_code, "ERROR")
            return
        try:
            loaded = engine.plans.plans.loaded()
            if self.exit_gate is not None and self.exit_owner is None:
                self.exit_owner = D1StrategyOwner(engine, self.exit_gate)
                if self.liquidation_requested:
                    self.exit_owner.request_liquidation()
            if loaded is not None:
                current_hash = loaded.envelope.payload_hash
                if self.plan_hash is not None and self.plan_hash != current_hash:
                    raise ValueError("live plan changed after entry owner initialization")
            if loaded is not None and self.owner is None:
                policy = self._policy(loaded.payload)
                if policy is not None:
                    gate = EntryStateGate(
                        loaded.payload,
                        policy,
                        clock=self.clock,
                        require_policy_payload_hash=False,
                    )
                    self.owner = EntryStrategyOwner(engine, gate)
                    engine.install_entry_runtime(gate.allows, gate.export_targets)
                    self.plan_hash = current_hash
            if self.owner is not None and not self.liquidation_requested:
                self._feed()
                self.owner.poll()
            if self.exit_owner is not None:
                assert self.exit_gate is not None
                for symbol, tick in tuple(self.market.latest.items()):
                    if symbol not in exit_symbols or tick.source_seq is None or not tick.bids:
                        continue
                    previous = self.exit_gate.quotes.get(symbol)
                    if previous is not None and tick.source_seq <= previous.sequence:
                        continue
                    self.exit_owner.quote(
                        ExitQuote(
                            symbol=symbol,
                            sequence=tick.source_seq,
                            observed_at=tick.occurred_at.value,
                            received_ns=monotonic_ns(),
                            last_price=str(tick.last_price.amount),
                            bid_price=str(tick.bids[0].price.amount),
                            tradable=tick.trading_status == 13,
                        )
                    )
                self.exit_owner.poll()
            self._observe_activity(engine)
        except (ValueError, RuntimeError):
            self.failure_code = "QMT_LIVE_ENTRY_DRIVER_FAILED"
            engine.emergency_halt(self.failure_code)
            # Keep diagnostics best-effort after a fail-closed transition. Do
            # not repeat the observation that may itself have raised.
            self._remember("driver", self.failure_code, "ERROR")

    def stop(self) -> None:
        self.market.stop()

    def status(self) -> dict[str, JsonValue]:
        owner = self.owner.snapshot() if self.owner else None
        state = owner.state if owner else "WAITING_MARKET"
        entry_progress: list[JsonValue] = (
            [
                {"symbol": symbol, "state": progress}
                for symbol, progress in getattr(owner, "progress", ())
            ]
            if owner
            else []
        )
        entry_targets: list[JsonValue] = (
            [
                {
                    "symbol": target.symbol,
                    "target_quantity": target.target_quantity,
                    "reason": target.reason,
                    "decided_at": target.decided_at.isoformat(),
                }
                for target in getattr(owner, "targets", ())
            ]
            if owner
            else []
        )
        exit_progress: list[JsonValue] = []
        if self.exit_gate is not None and hasattr(self.exit_gate, "latest"):
            lot_symbols = {lot.lot_id: lot.symbol for lot in self.exit_gate.context.lots}
            exit_progress = [
                {
                    "lot_id": lot_id,
                    "symbol": lot_symbols.get(lot_id, "UNKNOWN"),
                    "target_remaining": record.state.target_remaining,
                    "reasons": list(record.state.fired_rules),
                }
                for lot_id, record in sorted(self.exit_gate.latest.items())
            ]
        return {
            **self.market.status(),
            "driver_state": "FAILED" if self.failure_code else state,
            "configured": self.settings is not None and self.settings.buy.enabled,
            "exit_state": self.exit_owner.snapshot().state if self.exit_owner else None,
            "entry_progress": entry_progress,
            "entry_targets": entry_targets,
            "exit_progress": exit_progress,
            "recent_activity": list(reversed(self._activity)),
            "liquidation_requested": self.liquidation_requested,
            "failure_code": self.failure_code or self.market.failure_code,
        }
