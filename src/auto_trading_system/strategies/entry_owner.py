"""Same-owner nonblocking Entry driver; order progress is derived from J1 and broker facts."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from time import monotonic
from uuid import uuid4

from auto_trading_system.domain import Money, Price
from auto_trading_system.execution.entry_budget import BuyExposure
from auto_trading_system.journal.critical_models import ActionTarget, EntryBinding, Submission
from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.protocol.v2.plans import Allocation
from auto_trading_system.runtime.engine_v2 import EngineKernel, EngineSnapshot
from auto_trading_system.strategies.entry_gate import EntryQuote, EntryStateGate, legal_quantity
from auto_trading_system.strategies.entry_sizing import BuyRule, EntryCapacity, size_entry
from auto_trading_system.strategies.entry_state import (
    EntryTarget,
    append_entry_target,
    load_entry_targets,
)


@dataclass(frozen=True)
class EntryOwnerSnapshot:
    state: str
    targets: tuple[EntryTarget, ...]
    progress: tuple[tuple[str, str], ...]
    reason: str | None


@dataclass
class _Pending:
    target: EntryTarget
    write: Future[bool]
    deadline: float
    epoch: int


class EntryStrategyOwner:
    def __init__(self, engine: EngineKernel, gate: EntryStateGate) -> None:
        gate.owner_only()
        loaded = next(
            (p for p in engine.plans.plans.accepted() if p.envelope.payload_hash == gate.plan_hash),
            None,
        )
        if (
            loaded is None
            or loaded.envelope.payload_hash != gate.plan_hash
            or engine.account_id != gate.plan.account_id
            or engine.trade_date != gate.plan.trade_date
            or gate.engine is not None
        ):
            raise ValueError("entry owner requires its verified plan and unused gate")
        self.engine, self.gate = engine, gate
        gate.engine = engine
        self._failure: str | None = None
        self._pending: _Pending | None = None
        self._progress: dict[str, str] = {}
        self.history: dict[str, EntryTarget] = {}
        self._restore_deadline = monotonic() + 5
        self._restore: Future[tuple[EntryTarget, ...]] | None = engine.journal.request(
            lambda store: load_entry_targets(
                store,
                gate.plan,
                gate.policy,
                require_payload_hash=gate.require_policy_payload_hash,
                allocations=engine.entry_allocations(gate.plan),
            ),
            deadline=self._restore_deadline,
        )

    def _fail(self, reason: str) -> None:
        self._failure, self.gate.blocked = reason, True
        self.engine.emergency_halt(reason)

    def quote(self, quote: EntryQuote) -> None:
        self.gate.owner_only()
        quote = EntryQuote.model_validate_json(quote.model_dump_json())
        if quote.symbol not in self.gate.rules:
            raise ValueError("quote outside entry policy")
        old = self.gate.quotes.get(quote.symbol)
        if old and (quote.sequence <= old.sequence or quote.received_ns <= old.received_ns):
            self._fail("ENTRY_QUOTE_ORDER_INVALID")
        elif quote.observed_at > self.gate.clock() or quote.received_ns > self.gate.ns_clock():
            self._fail("ENTRY_QUOTE_CLOCK_INVALID")
        else:
            self.gate.quotes[quote.symbol] = quote

    def _accept(self, target: EntryTarget) -> None:
        gate = self.gate
        allocation = next(
            (
                a
                for a in (
                    self.engine.entry_allocations(gate.plan, target.configuration)
                    if target.configuration
                    else gate.plan.allocations
                )
                if a.symbol == target.symbol
            ),
            None,
        )
        previous = gate.targets.get(target.symbol)
        if target.instrument is not None:
            gate.rules[target.symbol] = target.instrument
        if (
            allocation is None
            or target.plan_hash != gate.plan_hash
            or target.policy_hash != gate.policy.payload_hash
            or target.account_id != gate.plan.account_id
            or target.trade_date != gate.plan.trade_date
            or target.allocation_id != allocation.allocation_id
            or (target.revision == 0 and target.target_quantity > allocation.max_qty)
            or target.decided_at > gate.clock()
            or (
                target.arrival_price is not None
                and target.revision == 0
                and Decimal(target.arrival_price) * target.target_quantity
                > Decimal(allocation.target_value)
            )
            or target.target_quantity
            != legal_quantity(target.target_quantity, gate.rules[target.symbol])
            or (previous is None and target.revision != 0)
            or (
                previous is not None
                and (
                    target.revision != previous.revision + 1
                    or target.previous_hash != previous.payload_hash
                )
            )
        ):
            raise ValueError("entry target scope mismatch or duplicate")
        gate.targets[target.symbol] = target
        self.history[target.payload_hash] = target

    def _storage(self) -> None:
        if not self.engine.journal.healthy:
            raise ValueError("entry Journal unavailable")
        if self._restore is not None:
            if monotonic() >= self._restore_deadline:
                raise ValueError("entry restore expired")
            if not self._restore.done():
                return
            for target in self._restore.result():
                self._accept(target)
            self._restore, self.gate.ready = None, True
        pending = self._pending
        if pending:
            if (
                monotonic() >= pending.deadline
                or pending.epoch != self.engine.snapshot().permission_epoch
            ):
                raise ValueError("entry target write expired or fenced")
            if not pending.write.done():
                return
            if not pending.write.result():
                raise ValueError("duplicate target requires recovery")
            self._accept(pending.target)
            self._pending, self.gate.blocked = None, False

    def poll(self) -> EntryOwnerSnapshot:
        self.gate.owner_only()
        self.engine.poll()
        if not self._failure:
            try:
                self._storage()
                if self.gate.ready and self._pending is None:
                    self._evaluate()
            except Exception:
                self._fail("ENTRY_STATE_OR_EXECUTION_FAILED")
        if self.engine.execution_reporter is not None:
            try:
                plan = next(
                    p
                    for p in self.engine.plans.plans.accepted()
                    if p.envelope.payload_hash == self.gate.plan_hash
                )
                snapshot = self.snapshot()
                self.engine.execution_reporter.progress(
                    plan, snapshot.targets, snapshot.progress, self.gate.clock()
                )
            except Exception:
                self._fail("EXECUTION_REPORTING_FAILED")
        return self.snapshot()

    def _persist(self, target: EntryTarget) -> None:
        deadline = monotonic() + 0.25
        self.gate.blocked = True
        self._pending = _Pending(
            target,
            self.engine.journal.request(
                partial(append_entry_target, target=target), deadline=deadline
            ),
            deadline,
            self.engine.snapshot().permission_epoch,
        )

    def _evaluate(self) -> None:
        gate, engine = self.gate, self.engine
        snap = engine.snapshot()
        loaded = next(
            (p for p in engine.plans.plans.accepted() if p.envelope.payload_hash == gate.plan_hash),
            None,
        )
        if loaded is None or loaded.envelope.payload_hash != gate.plan_hash:
            raise ValueError("entry plan changed; explicit context migration required")
        _, account, known = engine.exit_inputs()
        for item in known:
            if item.plan_hash != gate.plan_hash:
                continue
            target = (
                self.history.get(item.entry_binding.state_hash or "")
                if item.entry_binding
                else None
            )
            if item.side == "BUY" and (
                target is None
                or item.entry_binding is None
                or item.entry_binding.state_hash != target.payload_hash
                or item.entry_binding.target_quantity != target.target_quantity
                or item.entry_binding.allocation_id != target.allocation_id
                or item.entry_binding.plan_id != gate.plan.plan_id
                or item.plan_hash != gate.plan_hash
                or item.target
                != ActionTarget(
                    action_id="entry-" + target.payload_hash[7:],
                    target_remaining=target.target_quantity,
                    rule_id=gate.policy.version,
                )
            ):
                raise ValueError("J1 target reference missing or inconsistent")
        if (
            snap.halted
            or engine.current_buy_grant() is None
            or not engine.critical_idle
            or account is None
            or any(s.status in ("PERSISTING", "DISPATCHING") for s in snap.submissions)
        ):
            return
        if any(s.status == "NOT_DISPATCHED" for s in snap.submissions):
            raise ValueError("entry child requires recovery")
        now, window = gate.clock(), gate.plan.execution_window
        if self._cancel_due(known, snap):
            return
        if not engine.has_buy_session():
            return
        if not 0 <= (now - account.observed_at).total_seconds() <= 5:
            engine.refresh_account()
            return
        exposures = engine.entry_exposures()
        for allocation in sorted(engine.entry_allocations(gate.plan), key=lambda a: a.rank):
            symbol = allocation.symbol
            if engine.manual_symbol_claimed(symbol):
                self._progress[symbol] = "MANUAL_BUY_MANAGED"
                continue
            if symbol not in gate.rules:
                continue
            quote, rule, target = (
                gate.quotes.get(symbol),
                gate.rules[symbol],
                gate.targets.get(symbol),
            )
            price = gate.price(quote) if quote else None
            cash, single, slots = engine.entry_resources(symbol, gate.plan_hash)
            if now < window.entry_not_before:
                self._progress[symbol] = "WAITING_WINDOW"
                continue
            if target is None:
                if self._initialize(allocation, quote, price, cash, slots):
                    return
                continue
            if (
                engine.buy_settings is not None
                and target.configuration_version != engine.buy_settings_version
            ) and self._revise(allocation, target, quote, price, cash, slots, exposures):
                return
            children = tuple(
                s
                for s in known
                if s.action_type == "NEW" and s.side == "BUY" and s.symbol == symbol
            )
            active = any(
                v.phase != "TERMINAL_CONFIRMED"
                and (v.order is None or str(v.order.symbol) == symbol)
                for v in snap.orders
            )
            remaining = target.target_quantity - sum(
                e.quantity for e in exposures if e.symbol == symbol
            )
            if target.target_quantity == 0:
                status: str = target.reason
            elif active:
                status = "ORDER_ACTIVE"
            elif remaining <= 0:
                status = "FINISHED"
            elif now >= window.stop_new_buy_at:
                status = "WINDOW_CLOSED"
            elif len(children) >= gate.execution_policy.max_children:
                status = "CHILD_LIMIT"
            elif price is None:
                status = "WAITING_MARKET"
            else:
                status = "WAITING_CAPACITY"
            self._progress[symbol] = status
            if status != "WAITING_CAPACITY" or quote is None or price is None or slots == 0:
                continue
            if (
                sum(v.phase != "TERMINAL_CONFIRMED" for v in snap.orders)
                >= gate.policy.max_active_orders
                or gate.used_quotes.get(symbol, (0, ""))[0] == quote.sequence
            ):
                continue
            volume = max(
                0,
                quote.recent_volume * gate.execution_policy.participation_bps // 10000
                - sum(s.quantity for s in children),
            )
            size = size_entry(
                Price(price),
                Price(price),
                BuyRule(
                    rule.min_quantity,
                    rule.quantity_step,
                    max(
                        rule.min_quantity,
                        min(
                            rule.max_order_quantity
                            if engine.buy_settings is None
                            else engine.buy_limits.max_order_quantity,
                            engine.buy_limits.max_order_quantity,
                        ),
                    ),
                    Price.parse(rule.price_tick),
                ),
                EntryCapacity(
                    Money(min(cash, single)),
                    Money.parse(gate.policy.fee_reserve),
                    remaining,
                    volume,
                    quote.ask_quantity,
                    True,
                ),
                participation_bps=10000,
                depth_bps=gate.execution_policy.depth_bps,
            )
            if size.quantity == 0 or size.quantity > engine.buy_limits.max_order_quantity:
                continue
            remark_token = uuid4().hex[:20]
            child = Submission(
                # QMT persists the short remark across restarts; keep the local
                # order identity derivable from the same durable token.
                client_order_id="ord_" + remark_token,
                account_id=engine.account_id,
                trade_date=engine.trade_date,
                action_type="NEW",
                plan_hash=gate.plan_hash,
                symbol=symbol,
                side="BUY",
                quantity=size.quantity,
                limit_price=str(price),
                maximum_amount=str(price * size.quantity + Decimal(gate.policy.fee_reserve)),
                remark_token=remark_token,
                engine_boot_id=engine.boot_id,
                permission_epoch=snap.permission_epoch,
                target=ActionTarget(
                    action_id="entry-" + target.payload_hash[7:],
                    target_remaining=target.target_quantity,
                    rule_id=gate.policy.version,
                ),
                entry_binding=EntryBinding(
                    strategy_id=self.gate.plan.strategy_id,
                    plan_id=gate.plan.plan_id,
                    allocation_id=allocation.allocation_id,
                    target_quantity=target.target_quantity,
                    state_hash=target.payload_hash,
                ),
            )
            engine.submit(child, valid_until=monotonic() + 0.25)
            return

    def _cancel_due(self, known: tuple[Submission, ...], snap: EngineSnapshot) -> bool:
        gate, engine = self.gate, self.engine
        now, window = gate.clock(), gate.plan.execution_window
        # Cutoff cancellations work with stale/missing quotes. Replacements wait
        # for terminal order+trade+account confirmation through the engine.
        by_id = {s.client_order_id: s for s in known}
        for view in snap.orders:
            order, original = view.order, by_id.get(view.client_order_id)
            if (
                order is None
                or original is None
                or original.side != "BUY"
                or original.plan_hash != gate.plan_hash
            ):
                continue
            if (
                not order.is_terminal
                and not view.cancel_request_ids
                and order.broker_order_id
                and now < window.cancel_by
                and (
                    now >= window.stop_new_buy_at
                    or (now - order.updated_at.value).total_seconds()
                    >= gate.execution_policy.cancel_after_seconds
                    or engine.buy_order_exceeds_limits(original)
                    or (
                        self.engine.buy_settings is not None
                        and self.engine.buy_limits.max_buy_value == "0"
                    )
                )
            ):
                engine.submit(
                    original.model_copy(
                        update={
                            "client_order_id": "ord_" + uuid4().hex,
                            "action_type": "CANCEL",
                            "original_order_id": order.broker_order_id,
                            "original_client_order_id": original.client_order_id,
                            "engine_boot_id": engine.boot_id,
                            "permission_epoch": snap.permission_epoch,
                        }
                    ),
                    valid_until=monotonic() + 0.25,
                )
                return True
        return False

    def _initialize(
        self,
        allocation: Allocation,
        quote: EntryQuote | None,
        price: Decimal | None,
        cash: Decimal,
        slots: int,
    ) -> bool:
        gate, engine = self.gate, self.engine
        symbol, rule = allocation.symbol, gate.rules[allocation.symbol]
        now, window = gate.clock(), gate.plan.execution_window
        claimed = engine.entry_symbol_claimed(symbol, gate.plan_hash)
        if claimed is not False:
            self._progress[symbol] = "WAITING_RECOVERY" if claimed is None else "DUPLICATE_SIGNAL"
            return False
        quantity, reason = 0, "ELIGIBLE"
        if (window.stop_new_buy_at - now).total_seconds() < window.min_execution_seconds:
            reason = "MISSED_ENTRY_WINDOW"
        elif slots == 0:
            reason = "POSITION_CAPACITY"
        elif price is None:
            self._progress[symbol] = "WAITING_MARKET"
            return False
        else:
            usable = min(
                Decimal(allocation.target_value),
                max(Decimal(0), cash - Decimal(gate.policy.fee_reserve)),
            )
            quantity = legal_quantity(min(allocation.max_qty, int(usable / price)), rule)
            if quantity == 0:
                reason = "SKIPPED_MIN_ORDER"
        self._persist(
            EntryTarget.model_validate_json(
                canonical_bytes(
                    {
                        "plan_hash": gate.plan_hash,
                        "policy_hash": gate.policy.payload_hash,
                        "account_id": engine.account_id,
                        "trade_date": engine.trade_date.isoformat(),
                        "allocation_id": allocation.allocation_id,
                        "symbol": symbol,
                        "boot_id": engine.boot_id,
                        "decided_at": now.isoformat(),
                        "arrival_price": quote.ask_price if price is not None and quote else None,
                        "target_quantity": quantity,
                        "reason": reason,
                        **(
                            {
                                "configuration": engine.buy_settings.model_dump(mode="json"),
                                "configuration_version": engine.buy_settings_version,
                                "instrument": rule.model_dump(mode="json"),
                            }
                            if engine.buy_settings is not None
                            else {}
                        ),
                    }
                )
            )
        )
        return True

    def _revise(
        self,
        allocation: Allocation,
        target: EntryTarget,
        quote: EntryQuote | None,
        price: Decimal | None,
        cash: Decimal,
        slots: int,
        exposures: tuple[BuyExposure, ...],
    ) -> bool:
        engine, gate = self.engine, self.gate
        if price is None or quote is None:
            return False
        if target.revision >= 255:
            engine.buy_settings_error = "BUY_TARGET_REVISION_CAPACITY"
            return True
        filled = sum(e.quantity for e in exposures if e.symbol == target.symbol)
        buy_ids = {
            s.client_order_id
            for s in engine.exit_inputs()[2]
            if s.side == "BUY" and s.symbol == target.symbol
        }
        actual_filled = (
            sum(
                t.quantity.value
                for t in engine._broker_snapshot.trades
                if t.client_order_id in buy_ids
            )
            if engine._broker_snapshot
            else 0
        )
        complete = target.target_quantity > 0 and actual_filled >= target.target_quantity
        quantity = target.target_quantity
        if not complete:
            usable = min(
                Decimal(allocation.target_value),
                max(Decimal(0), cash - Decimal(gate.policy.fee_reserve))
                + sum(e.amount for e in exposures if e.symbol == target.symbol),
            )
            quantity = (
                legal_quantity(
                    min(allocation.max_qty, int(usable / price)), gate.rules[target.symbol]
                )
                if slots
                else filled
            )
            if quantity < filled or quantity != legal_quantity(quantity, gate.rules[target.symbol]):
                quantity = target.target_quantity
        reason = "ELIGIBLE" if quantity else "POSITION_CAPACITY"
        self._persist(
            target.model_copy(
                update={
                    "revision": target.revision + 1,
                    "previous_hash": target.payload_hash,
                    "configuration": engine.buy_settings,
                    "configuration_version": engine.buy_settings_version,
                    "instrument": gate.rules[target.symbol],
                    "decided_at": gate.clock(),
                    "arrival_price": str(price),
                    "target_quantity": quantity,
                    "reason": reason,
                }
            )
        )
        return True

    def snapshot(self) -> EntryOwnerSnapshot:
        self.gate.owner_only()
        return EntryOwnerSnapshot(
            "FAILED"
            if self._failure
            else "RESTORING"
            if self._restore is not None
            else "PERSISTING"
            if self._pending
            else "READY",
            tuple(self.gate.targets[k] for k in sorted(self.gate.targets)),
            tuple(sorted(self._progress.items())),
            self._failure,
        )
