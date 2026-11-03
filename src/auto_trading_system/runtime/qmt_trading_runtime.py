"""Live QMT entry driver: signed plan -> subscriptions -> quotes -> order owner.

The driver never grants permission.  It only supplies fresh QMT market facts to
the existing durable entry owner.  The engine still requires recovery and an
explicit local LIVE grant before any order can cross the SDK boundary.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from datetime import datetime, timedelta
from decimal import Decimal
from time import monotonic, monotonic_ns, perf_counter_ns
from typing import TYPE_CHECKING

from pydantic import JsonValue

if TYPE_CHECKING:
    from auto_trading_system.runtime.board_sell import BoardSellController

from auto_trading_system.journal.critical_models import Submission
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.plans import PlanPayload, PolicyVersion
from auto_trading_system.recorder.tick_archive import TickHistory
from auto_trading_system.runtime.engine_v2 import EngineKernel
from auto_trading_system.runtime.plan_market import PlanMarketRuntime
from auto_trading_system.strategies.entry_gate import EntryQuote, EntryStateGate
from auto_trading_system.strategies.entry_owner import EntryStrategyOwner
from auto_trading_system.strategies.entry_state import EntryInstrument, EntryPolicy, EntryTarget
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
        self.board_sell: BoardSellController | None = None
        self.owner: EntryStrategyOwner | None = None
        self.phase_owners: dict[str, EntryStrategyOwner] = {}
        self._tail_installed = False
        self._policy_reads: dict[str, tuple[Future[bytes | None], float]] = {}
        self._expected_phases: set[str] = set()
        self.plan_hash: str | None = None
        self.last_sequence: dict[str, int] = {}
        self.volume_windows: dict[str, deque[tuple[datetime, int]]] = {}
        self.history_windows: dict[str, TickHistory] = {}
        self._history_reads: dict[str, Future[TickHistory]] = {}
        self._history_deadlines: dict[str, float] = {}
        self._live_volume_start: dict[str, datetime] = {}
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
        name: str | None = None,
        context: str | None = None,
        phase: str | None = None,
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
        if name:
            event["name"] = name[:64]
        if context is not None:
            event["context"] = context
        if phase is not None:
            event["phase"] = phase
        self._activity.append(event)

    def _observe_activity(self, engine: EngineKernel) -> None:
        if self.failure_code:
            self._remember("driver", self.failure_code, "ERROR")
        if self.owner is not None:
            snapshot = self.owner.snapshot()
            context = self.owner.gate.plan_hash
            phase = self.owner.gate.plan.source.decision_phase
            self._remember("entry-driver:" + context, snapshot.state, "ENTRY_DRIVER")
            for symbol, state in getattr(snapshot, "progress", ()):
                self._remember(
                    "entry:" + context + ":" + symbol,
                    state,
                    "ENTRY_SIGNAL",
                    symbol=symbol,
                    context=context,
                    phase=phase,
                )
            for target in getattr(snapshot, "targets", ()):
                state = "BUY_TARGET_READY" if target.target_quantity > 0 else "BUY_TARGET_SKIPPED"
                self._remember(
                    "entry-target:" + context + ":" + target.symbol,
                    state + ":" + target.reason + ":" + str(target.target_quantity),
                    "ENTRY_TARGET",
                    symbol=target.symbol,
                    detail=target.reason,
                    context=context,
                    phase=phase,
                )
        if self.exit_owner is not None:
            self._remember("exit-driver", self.exit_owner.snapshot().state, "EXIT_DRIVER")
        if self.liquidation_requested:
            self._remember("liquidation", "REQUESTED", "LIQUIDATION")
        for view in engine.snapshot().orders:
            order = view.order
            original = engine._known.get(view.client_order_id)
            if order is None:
                state = view.phase
                symbol = original.symbol if original else None
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
                context=view.client_order_id,
                detail=detail,
                name=(
                    original.manual_binding.name
                    if original.manual_binding
                    else original.manual_sell_binding.name
                    if original.manual_sell_binding
                    else None
                )
                if original
                else None,
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

    def _feed(self, selected_owner: EntryStrategyOwner | None = None) -> None:
        owner = selected_owner or self.owner
        assert owner is not None
        for symbol, tick in tuple(self.market.latest.items()):
            if symbol not in owner.gate.rules:
                continue
            key = owner.gate.plan_hash + ":" + symbol if selected_owner is not None else symbol
            sequence = tick.source_seq
            if sequence is None or sequence <= self.last_sequence.get(key, 0):
                continue
            current = tick.cumulative_volume.value
            archive = getattr(self.market, "archive", None)
            if archive is not None and key not in self.history_windows:
                if key not in self._history_reads:
                    self._history_reads[key] = archive.query(
                        symbol,
                        tick.occurred_at.value - _VOLUME_WINDOW,
                        tick.occurred_at.value,
                        limit=_MAX_VOLUME_SAMPLES_PER_SYMBOL,
                    )
                    self._history_deadlines[key] = monotonic() + 5
                pending = self._history_reads[key]
                if not pending.done():
                    if monotonic() >= self._history_deadlines[key]:
                        raise RuntimeError("market history restore timed out")
                    continue
                history = pending.result()
                if not isinstance(history, TickHistory):
                    raise ValueError("invalid archived volume history")
                self.history_windows[key] = history
                self.volume_windows[key] = deque(
                    (r.occurred_at, r.volume)
                    for r in history.records
                    if r.occurred_at < tick.occurred_at.value
                )
                del self._history_reads[key]
                del self._history_deadlines[key]
            self._live_volume_start.setdefault(key, tick.occurred_at.value)
            samples = self.volume_windows.setdefault(key, deque())
            if samples and (tick.occurred_at.value < samples[-1][0] or current < samples[-1][1]):
                # A reconnect/day reset makes the old cumulative-volume anchor
                # unusable. Rebuild a complete window before admitting a buy.
                samples.clear()
                self._live_volume_start[key] = tick.occurred_at.value
            if samples and tick.occurred_at.value == samples[-1][0]:
                samples[-1] = (tick.occurred_at.value, current)
            else:
                samples.append((tick.occurred_at.value, current))
            cutoff = tick.occurred_at.value - _VOLUME_WINDOW
            while len(samples) > 1 and samples[1][0] <= cutoff:
                samples.popleft()
            if len(samples) > _MAX_VOLUME_SAMPLES_PER_SYMBOL:
                raise ValueError("QMT rolling-volume sample capacity exceeded")
            path_complete = bool(
                samples
                and samples[0][0] <= cutoff
                and (archive is None or self._live_volume_start[key] <= cutoff)
            )
            recent = 0
            if path_complete:
                # Use the first sample on or after the 60-second boundary.  The
                # older anchor proves the boundary was crossed, but subtracting
                # it would include volume from before the requested window.
                baseline = samples[0]
                if baseline[0] < cutoff:
                    baseline = samples[1]
                recent = current - baseline[1]
            self.last_sequence[key] = sequence
            bid = tick.bids[0] if tick.bids else None
            ask = tick.asks[0] if tick.asks else None
            if bid is None or ask is None:
                continue
            rule = owner.gate.rules[symbol]
            owner.quote(
                EntryQuote(
                    symbol=symbol,
                    sequence=sequence,
                    observed_at=tick.occurred_at.value,
                    received_ns=perf_counter_ns(),
                    bid_price=_price(bid.price.amount),
                    ask_price=_price(ask.price.amount),
                    ask_quantity=ask.quantity.value,
                    recent_volume=recent,
                    cage_upper=rule.upper_price,
                    path_complete=path_complete,
                    tradable=tick.trading_status == 13,
                )
            )

    def _phase_targets(self) -> tuple[EntryTarget, ...]:
        if self._expected_phases - self.phase_owners.keys():
            raise ValueError("phase entry owners are still restoring")
        targets: list[EntryTarget] = []
        for owner in self.phase_owners.values():
            targets.extend(owner.gate.export_targets())
        return tuple(targets)

    def _phase_allows(self, item: Submission) -> bool:
        owner = self.phase_owners.get(item.plan_hash)
        return owner.gate.allows(item) if owner is not None else False

    def _phase_policy(self, engine: EngineKernel, plan: PlanPayload) -> EntryPolicy | None:
        plan_hash = digest(canonical_bytes(plan.model_dump(mode="json")))
        pending = self._policy_reads.get(plan_hash)
        if pending is None:
            deadline = monotonic() + 5
            future = engine.journal.request(
                lambda store: store.read_evidence(
                    "recovery_checkpoints", f"entry-context:{plan.trade_date}:{plan_hash}"
                ),
                deadline=deadline,
            )
            self._policy_reads[plan_hash] = (future, deadline)
            return None
        future, deadline = pending
        if not future.done():
            if monotonic() >= deadline:
                raise ValueError("phase policy restore expired")
            return None
        body = future.result()
        if body is not None:
            from auto_trading_system.protocol.v2.canonical import parse_bytes

            record = parse_bytes(body)
            if not isinstance(record, dict) or record.get("plan") != plan.model_dump(mode="json"):
                raise ValueError("persisted phase entry context differs")
            policy = EntryPolicy.model_validate_json(canonical_bytes(record["policy"]))
            settings = self.settings
            if getattr(engine, "buy_settings", None) is None and (
                settings is None
                or (
                    policy.max_chase_bps != settings.advanced.max_chase_bps
                    or policy.max_spread_bps != settings.advanced.max_spread_bps
                    or policy.quote_max_age_ms != settings.advanced.quote_max_age_ms
                    or policy.cancel_after_seconds != settings.buy.cancel_after_seconds
                    or policy.max_children != settings.buy.max_retries + 1
                    or any(
                        rule.max_order_quantity != settings.advanced.max_order_quantity
                        for rule in policy.instruments
                    )
                )
            ):
                raise ValueError(
                    "persisted phase execution settings differ from installed settings"
                )
            return policy
        originals = {a.symbol for a in plan.allocations}
        allocations = tuple(
            a
            for a in engine.entry_allocations(plan)
            if a.symbol in originals or engine.buy_board_allowed(a.symbol)
        )
        if not allocations:
            return None
        return self._policy(plan.model_copy(update={"allocations": allocations}))

    def _poll_phases(self, engine: EngineKernel) -> None:
        if self.exit_gate is not None:
            raise ValueError("tail buy-only mode cannot install automatic exits")
        if not self._tail_installed:
            engine.install_entry_runtime(
                self._phase_allows,
                self._phase_targets,
                lambda: tuple(owner.gate.policy for owner in self.phase_owners.values()),
            )
            self._tail_installed = True
        self._expected_phases = {
            p.envelope.payload_hash
            for p in engine.plans.plans.accepted()
            if p.payload.allocations
            or any(engine.buy_board_allowed(a.symbol) for a in engine.entry_allocations(p.payload))
        }
        for plan in engine.plans.plans.accepted():
            plan_hash = plan.envelope.payload_hash
            if plan_hash not in self.phase_owners and (
                plan.payload.allocations
                or any(
                    engine.buy_board_allowed(a.symbol)
                    for a in engine.entry_allocations(plan.payload)
                )
            ):
                policy = self._phase_policy(engine, plan.payload)
                if policy is not None:
                    gate = EntryStateGate(
                        plan.payload,
                        policy,
                        clock=self.clock,
                        ns_clock=perf_counter_ns,
                        require_policy_payload_hash=False,
                    )
                    self.phase_owners[plan_hash] = EntryStrategyOwner(engine, gate)
            owner = self.phase_owners.get(plan_hash)
            if owner is not None:
                self.owner = owner
                missing = [
                    a
                    for a in engine.entry_allocations(owner.gate.plan)
                    if a.symbol not in owner.gate.rules and engine.buy_board_allowed(a.symbol)
                ]
                if missing and all(a.symbol in self.market.latest for a in missing):
                    policy = self._policy(
                        owner.gate.plan.model_copy(update={"allocations": tuple(missing)})
                    )
                    if policy:
                        owner.gate.rules.update({i.symbol: i for i in policy.instruments})
                if missing and any(a.symbol not in owner.gate.rules for a in missing):
                    continue
                self._feed(owner)
                owner.poll()
                self._observe_activity(engine)
        self.plan_hash = self.market.plan_hash

    def poll(self, engine: EngineKernel) -> None:
        held_symbols: tuple[str, ...] = ()
        if getattr(self.market, "archive", None) is not None:
            portfolio, account, _ = engine.exit_inputs()
            held_symbols = tuple(
                dict.fromkeys(lot.symbol for lot in portfolio.lots if lot.remaining)
            )
            required = set(portfolio.quantities())
            if account is not None:
                required.update(p.symbol for p in account.positions if p.quantity)
            for plan in engine.plans.plans.accepted():
                if self.clock() < plan.payload.execution_window.cancel_by:
                    required.update(a.symbol for a in plan.payload.allocations)
            self.market.retain(frozenset(required))
            self.market.cleanup_sold(engine.disposable_market_symbols())
        exit_symbols = (
            tuple(dict.fromkeys(lot.symbol for lot in self.exit_gate.context.lots))
            if self.exit_gate is not None
            else ()
        )
        phased = engine.plans.plans.tail_history is not None
        if phased:
            exit_symbols = tuple(
                dict.fromkeys(
                    a.symbol
                    for p in engine.plans.plans.accepted()
                    for a in engine.entry_allocations(p.payload)
                )
            )
        exit_symbols = tuple(dict.fromkeys((*exit_symbols, *held_symbols)))
        if self.board_sell is not None:
            exit_symbols = tuple(
                dict.fromkeys(
                    (
                        *exit_symbols,
                        *(
                            p.symbol
                            for o in self.board_sell.owners.values()
                            for p in o.context.lots
                        ),
                    )
                )
            )
        self.market.poll(engine, extra_symbols=exit_symbols)
        if getattr(self.market, "archive", None) is not None:
            self.market.cleanup_sold(engine.disposable_market_symbols())
        if self.failure_code is not None:
            engine.emergency_halt(self.failure_code)
            self._remember("driver", self.failure_code, "ERROR")
            return
        try:
            if self.board_sell is not None:
                board_symbols = {
                    p.symbol
                    for owner in self.board_sell.owners.values()
                    for p in owner.context.lots
                }
                for symbol, tick in tuple(self.market.latest.items()):
                    if symbol in board_symbols and tick.source_seq is not None and tick.bids:
                        self.board_sell.quote(
                            ExitQuote(
                                symbol=symbol,
                                sequence=tick.source_seq,
                                observed_at=tick.occurred_at.value,
                                received_ns=monotonic_ns(),
                                last_price=_price(tick.last_price.amount),
                                bid_price=_price(tick.bids[0].price.amount),
                                tradable=tick.trading_status == 13,
                            )
                        )
                self.board_sell.poll()
            if phased:
                self._poll_phases(engine)
                return
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
                        ns_clock=perf_counter_ns,
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
                            last_price=_price(tick.last_price.amount),
                            bid_price=_price(tick.bids[0].price.amount),
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
        if self.phase_owners:
            targets_by_symbol: dict[str, JsonValue] = {}
            progress_by_symbol: dict[str, JsonValue] = {}
            for phase_owner in self.phase_owners.values():
                phase = phase_owner.snapshot()
                for target in phase.targets:
                    if target.symbol not in targets_by_symbol or target.target_quantity > 0:
                        targets_by_symbol[target.symbol] = {
                            "symbol": target.symbol,
                            "target_quantity": target.target_quantity,
                            "reason": target.reason,
                            "decided_at": target.decided_at.isoformat(),
                        }
                for symbol, progress in phase.progress:
                    progress_by_symbol.setdefault(symbol, {"symbol": symbol, "state": progress})
            entry_targets = list(targets_by_symbol.values())
            entry_progress = list(progress_by_symbol.values())
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
