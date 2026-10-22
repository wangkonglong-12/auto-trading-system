"""Same-thread D1 driver: bounded quote cache, durable discrete state, guarded J1.

poll() never waits for I/O. The application calls this driver instead of also
running an independent strategy thread. The engine remains the sole account/
order owner; this driver owns only exit-rule state and uses public engine ports.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from threading import get_ident
from time import monotonic, monotonic_ns
from uuid import uuid4

from pydantic import AwareDatetime, Field, model_validator

from auto_trading_system.domain import Price
from auto_trading_system.journal.critical_models import Submission
from auto_trading_system.protocol.v2.base import WireModel
from auto_trading_system.protocol.v2.records import PriceText, Symbol
from auto_trading_system.runtime.engine_v2 import EngineKernel
from auto_trading_system.strategies.exit_context import ExitSessionContext
from auto_trading_system.strategies.exit_execution import exit_child
from auto_trading_system.strategies.exit_state_store import (
    CAPACITY,
    DurableExitState,
    ExitTransition,
    append_exit_transition,
    load_exit_state,
)
from auto_trading_system.strategies.exit_v3 import (
    ExitDecision,
    ExitLot,
    ExitObservation,
    ExitState,
    evaluate_exit,
)
from auto_trading_system.strategies.market_session import market_open


class ExitQuote(WireModel):
    symbol: Symbol = Field(description="行情所属证券")
    sequence: int = Field(ge=1, description="本次行情通道严格递增序号")
    observed_at: AwareDatetime = Field(description="已标准化的行情时间")
    received_ns: int = Field(ge=0, description="本进程接收时的单调时钟纳秒值")
    last_price: PriceText = Field(description="最新成交价，元")
    bid_price: PriceText = Field(description="当前用于SELL限价的买一价，元")
    tradable: bool = Field(description="行情适配器确认证券当前可交易")

    @model_validator(mode="after")
    def positive(self) -> ExitQuote:
        if (
            Decimal(self.last_price) <= 0
            or Decimal(self.bid_price) <= 0
            or self.observed_at.utcoffset() != timedelta(hours=8)
        ):
            raise ValueError("exit quote prices must be positive")
        return self


class ExitStateGate:
    """Created before the engine; only its same-owner driver publishes evidence."""

    def __init__(
        self,
        context: ExitSessionContext,
        *,
        clock: Callable[[], datetime],
        ns_clock: Callable[[], int] = monotonic_ns,
    ) -> None:
        self.context, self.clock, self.ns_clock = context, clock, ns_clock
        self.owner = get_ident()
        self.ready = False
        self.blocked = False
        self.latest: dict[str, ExitTransition] = {}
        self.quotes: dict[str, ExitQuote] = {}

    def owner_only(self) -> None:
        if get_ident() != self.owner:
            raise RuntimeError("exit strategy has a single owner")

    def fresh(self, quote: ExitQuote) -> bool:
        now = self.clock()
        return (
            now.date() == self.context.trade_date
            and quote.tradable
            and market_open(now)
            and 0 <= (now - quote.observed_at).total_seconds() <= 2
            and 0 <= self.ns_clock() - quote.received_ns <= 2_000_000_000
            and self.context.valid_from <= now < self.context.stop_new_at
        )

    def allows(self, item: Submission) -> bool:
        self.owner_only()
        binding = item.sell_binding
        if not self.ready or self.blocked or binding is None or len(binding.allocations) != 1:
            return False
        record = self.latest.get(binding.allocations[0].lot_id)
        quote = self.quotes.get(item.symbol)
        policy = next(
            (p for p in self.context.lots if p.lot_id == binding.allocations[0].lot_id), None
        )
        return bool(
            record
            and quote
            and policy
            and self.fresh(quote)
            and item.context_type == "EXIT_SESSION"
            and item.plan_hash == self.context.payload_hash
            and binding.state_hash == record.payload_hash
            and item.target.target_remaining == record.state.target_remaining
            and binding.fired_rules == record.state.fired_rules
            and Decimal(item.limit_price) == Decimal(quote.bid_price)
            and Decimal(policy.lower_price)
            <= Decimal(item.limit_price)
            <= Decimal(policy.upper_price)
            and Decimal(item.limit_price) % Decimal(policy.price_tick) == 0
            and (
                item.quantity % policy.sell_step == 0
                or (policy.allow_odd_liquidation and item.target.target_remaining == 0)
            )
        )


@dataclass(frozen=True)
class ExitOwnerSnapshot:
    state: str
    revision: int
    targets: tuple[tuple[str, int], ...]
    reason: str | None


@dataclass
class _Pending:
    record: ExitTransition
    write: Future[bool]
    deadline: float
    epoch: int


class D1StrategyOwner:
    def __init__(self, engine: EngineKernel, gate: ExitStateGate) -> None:
        gate.owner_only()
        if engine.exit_context != gate.context:
            raise ValueError("driver and engine exit contexts differ")
        self.engine, self.gate = engine, gate
        self.context = gate.context
        self._states: dict[str, ExitState] = {}
        self._history: list[ExitTransition] = []
        # A bounded derived index avoids re-hashing the whole persisted chain
        # on every quote-driven poll. Only the owner adds verified records.
        self._by_hash: dict[str, ExitTransition] = {}
        self._pending: _Pending | None = None
        self._failure: str | None = None
        self._linked: set[str] = set()
        self._liquidation_requested = False
        self._restore_deadline = monotonic() + 5
        self._restore: Future[tuple[ExitTransition, ...]] | None = engine.journal.request(
            lambda store: load_exit_state(store, self.context),
            deadline=self._restore_deadline,
        )

    def _fail(self, reason: str) -> None:
        self._failure = reason
        self.gate.blocked = True
        self.engine.emergency_halt(reason)

    def quote(self, quote: ExitQuote) -> None:
        """Owner-only bounded conflation; adapters hand immutable quotes to this loop."""
        self.gate.owner_only()
        quote = ExitQuote.model_validate_json(quote.model_dump_json())
        if quote.symbol not in {p.symbol for p in self.context.lots}:
            raise ValueError("quote outside frozen exit context")
        old = self.gate.quotes.get(quote.symbol)
        if old is not None and (
            quote.sequence <= old.sequence or quote.received_ns <= old.received_ns
        ):
            self._fail("EXIT_QUOTE_ORDER_INVALID")
            return
        if quote.observed_at > self.gate.clock() or quote.received_ns > self.gate.ns_clock():
            self._fail("EXIT_QUOTE_CLOCK_INVALID")
            return
        self.gate.quotes[quote.symbol] = quote

    def request_liquidation(self) -> None:
        """Tighten every strategy-owned lot to zero through the durable state chain."""
        self.gate.owner_only()
        self._liquidation_requested = True

    def _accept(self, record: ExitTransition) -> None:
        previous = self.gate.latest.get(record.lot_id)
        if (
            record.context_hash != self.context.payload_hash
            or record.account_id != self.context.account_id
            or record.trade_date != self.context.trade_date
            or record.lot_id not in {p.lot_id for p in self.context.lots}
            or record.revision != len(self._history) + 1
            or record.previous_hash != (self._history[-1].payload_hash if self._history else None)
            or (
                previous
                and (
                    record.state.target_remaining > previous.state.target_remaining
                    or not set(previous.state.fired_rules) <= set(record.state.fired_rules)
                    or (previous.state.profit_armed and not record.state.profit_armed)
                    or (previous.state.path_uncertain and not record.state.path_uncertain)
                )
            )
        ):
            raise ValueError("exit transition violates monotonic scope")
        self._history.append(record)
        self._by_hash[record.payload_hash] = record
        self.gate.latest[record.lot_id] = record

    def _poll_storage(self) -> None:
        if not self.engine.journal.healthy:
            raise ValueError("critical Journal is unavailable")
        if self._restore is not None:
            if monotonic() >= self._restore_deadline:
                raise ValueError("exit state restoration expired")
            if not self._restore.done():
                return
            for record in self._restore.result():
                self._accept(record)
            for lot_id, record in self.gate.latest.items():
                self._states[lot_id] = ExitState(
                    record.state.target_remaining,
                    frozenset(record.state.fired_rules),
                    record.state.profit_armed,
                    path_complete=False,
                )
            self._restore = None
            self.gate.ready = True
        pending = self._pending
        if pending:
            if (
                monotonic() >= pending.deadline
                or pending.epoch != self.engine.snapshot().permission_epoch
                or not self.engine.journal.healthy
            ):
                raise ValueError("late or fenced exit transition")
            if not pending.write.done():
                return
            if not pending.write.result():
                raise ValueError("duplicate transition needs recovery")
            self._accept(pending.record)
            self._pending = None
            self.gate.blocked = False

    def poll(self) -> ExitOwnerSnapshot:
        self.gate.owner_only()
        self.engine.poll()
        if self._failure:
            return self.snapshot()
        try:
            self._poll_storage()
            if self.gate.ready and not self._pending:
                self._verify_bindings()
                self._evaluate()
        except Exception:
            self._fail("EXIT_STATE_OR_EXECUTION_FAILED")
        return self.snapshot()

    def _verify_bindings(self) -> None:
        _, _, known = self.engine.exit_inputs()
        # Every recovered/current J1 reference must resolve to the frozen chain.
        # Older children may reference older (looser) states; only NEW children
        # need the latest state. Missing history never reconstructs permission.
        for item in known:
            if item.client_order_id in self._linked:
                continue
            binding = item.sell_binding
            record = self._by_hash.get(binding.state_hash or "") if binding else None
            if (
                item.context_type != "EXIT_SESSION"
                or item.plan_hash != self.context.payload_hash
                or not binding
                or len(binding.allocations) != 1
                or record is None
                or record.lot_id != binding.allocations[0].lot_id
                or record.state.target_remaining != item.target.target_remaining
                or record.state.fired_rules != binding.fired_rules
            ):
                raise ValueError("J1 exit state reference is missing or inconsistent")
            self._linked.add(item.client_order_id)

    def _evaluate(self) -> None:
        snapshot = self.engine.snapshot()
        if (
            snapshot.halted
            or not self.engine.critical_idle
            or any(s.status in ("PERSISTING", "DISPATCHING") for s in snapshot.submissions)
        ):
            return
        if any(s.status == "NOT_DISPATCHED" for s in snapshot.submissions):
            self._fail("EXIT_CHILD_REQUIRES_RECOVERY")
            return
        portfolio, account, known = self.engine.exit_inputs()
        if account is None:
            return
        # Cancellation is risk reduction and does not require a fresh trade
        # quote. It remains fenced by the engine's grant, J1 and cancel cutoff.
        for view in snapshot.orders:
            original = next((s for s in known if s.client_order_id == view.client_order_id), None)
            if (
                view.order is not None
                and original is not None
                and not view.cancel_request_ids
                and not view.order.is_terminal
                and view.order.broker_order_id is not None
                and self.gate.clock() < self.context.cancel_by
                and (
                    self._liquidation_requested
                    or (self.gate.clock() - view.order.updated_at.value).total_seconds()
                    >= self.context.cancel_after_seconds
                    or self.gate.clock() >= self.context.stop_new_at
                )
            ):
                cancel = original.model_copy(
                    update={
                        "client_order_id": "ord_" + uuid4().hex,
                        "action_type": "CANCEL",
                        "original_order_id": view.order.broker_order_id,
                        "original_client_order_id": original.client_order_id,
                    }
                )
                self.engine.submit(cancel, valid_until=monotonic() + 0.25)
                return
        lots = {lot.lot_id: lot for lot in portfolio.lots}
        if self._liquidation_requested:
            for policy in self.context.lots:
                lot = lots.get(policy.lot_id)
                previous = self.gate.latest.get(policy.lot_id)
                if (
                    lot is None
                    or lot.remaining == 0
                    or (previous is not None and previous.state.target_remaining == 0)
                ):
                    continue
                state = self._states.get(
                    lot.lot_id, ExitState(lot.bought_quantity, path_complete=False)
                )
                durable = DurableExitState(
                    target_remaining=0,
                    fired_rules=tuple(sorted({*state.fired, "EMERGENCY_LIQUIDATION"})),
                    profit_armed=state.profit_armed,
                    path_uncertain=True,
                )
                record = ExitTransition(
                    revision=len(self._history) + 1,
                    previous_hash=self._history[-1].payload_hash if self._history else None,
                    context_hash=self.context.payload_hash,
                    account_id=self.context.account_id,
                    trade_date=self.context.trade_date,
                    lot_id=lot.lot_id,
                    boot_id=self.engine.boot_id,
                    observed_at=self.gate.clock(),
                    state=durable,
                )
                deadline = monotonic() + 0.25
                self.gate.blocked = True
                self._pending = _Pending(
                    record,
                    self.engine.journal.request(
                        partial(append_exit_transition, record=record), deadline=deadline
                    ),
                    deadline,
                    snapshot.permission_epoch,
                )
                self._states[lot.lot_id] = ExitState(
                    0,
                    frozenset(durable.fired_rules),
                    durable.profit_armed,
                    path_complete=False,
                )
                return
            self._liquidation_requested = False
        for policy in self.context.lots:
            lot, quote = lots.get(policy.lot_id), self.gate.quotes.get(policy.symbol)
            if lot is None or lot.remaining == 0 or quote is None or not self.gate.fresh(quote):
                continue
            state = self._states.get(
                lot.lot_id, ExitState(lot.bought_quantity, path_complete=False)
            )
            # Reuse an unconsumed quote after persistence, but never evaluate its
            # monotonic sample twice. Time rules use the quote's timestamp.
            if state.last_received_ns != quote.received_ns:
                decision = evaluate_exit(
                    ExitLot(
                        lot.lot_id,
                        lot.bought_quantity,
                        lot.trade_date_acquired,
                        lot.eligible_sell_date,
                        Price.parse(policy.cost),
                        Price.parse(policy.previous_close),
                        Price.parse(policy.open_price),
                    ),
                    state,
                    ExitObservation(
                        quote.observed_at,
                        quote.received_ns,
                        Price.parse(quote.last_price),
                        True,
                        False,
                    ),
                    self.context.sell_rules,
                )
                state = decision.state
                self._states[lot.lot_id] = state
            durable = DurableExitState(
                target_remaining=state.target_remaining,
                fired_rules=tuple(sorted(state.fired)),
                profit_armed=state.profit_armed,
                path_uncertain=not state.path_complete,
            )
            previous = self.gate.latest.get(lot.lot_id)
            if previous is None or previous.state != durable:
                if len(self._history) >= CAPACITY:
                    raise ValueError("exit state capacity exhausted")
                record = ExitTransition(
                    revision=len(self._history) + 1,
                    previous_hash=self._history[-1].payload_hash if self._history else None,
                    context_hash=self.context.payload_hash,
                    account_id=self.context.account_id,
                    trade_date=self.context.trade_date,
                    lot_id=lot.lot_id,
                    boot_id=self.engine.boot_id,
                    observed_at=self.gate.clock(),
                    state=durable,
                )
                deadline = monotonic() + 0.25
                self.gate.blocked = True
                self._pending = _Pending(
                    record,
                    self.engine.journal.request(
                        partial(append_exit_transition, record=record),
                        deadline=deadline,
                    ),
                    deadline,
                    snapshot.permission_epoch,
                )
                return
            # In-flight orders keep their reservations. A tighter target is now
            # durable, but another child waits for terminal account evidence.
            active = next(
                (
                    view
                    for view in snapshot.orders
                    if view.phase != "TERMINAL_CONFIRMED"
                    and (view.order is None or str(view.order.symbol) == policy.symbol)
                ),
                None,
            )
            if active is not None:
                continue
            if not 0 <= (self.gate.clock() - account.observed_at).total_seconds() <= 5:
                self.engine.refresh_account()
                return
            broker_sellable = next(
                (p.sellable for p in account.positions if p.symbol == policy.symbol), 0
            )
            quantity = min(
                lot.remaining - state.target_remaining,
                broker_sellable,
                self.context.max_order_quantity,
                self.engine.limits.max_order_quantity,
                int(Decimal(self.context.max_order_value) / Decimal(quote.bid_price)),
            )
            if not (
                policy.allow_odd_liquidation
                and state.target_remaining == 0
                and quantity == lot.remaining
            ):
                quantity = quantity // policy.sell_step * policy.sell_step
            if quantity <= 0:
                continue
            child = exit_child(
                lot=lot,
                decision=ExitDecision(state, (), True),
                account_id=self.context.account_id,
                trade_date=self.context.trade_date,
                plan_hash=self.context.payload_hash,
                opening_hash=self.context.opening_hash,
                boot_id=self.engine.boot_id,
                permission_epoch=snapshot.permission_epoch,
                limit_price=Price.parse(quote.bid_price),
                quantity_limit=quantity,
                broker_sellable=broker_sellable,
                rule_id=self.context.rule_id,
            )
            if child is not None:
                assert child.sell_binding is not None
                child = child.model_copy(
                    update={
                        "context_type": "EXIT_SESSION",
                        "sell_binding": child.sell_binding.model_copy(
                            update={"state_hash": previous.payload_hash}
                        ),
                    }
                )
                self.engine.submit(child, valid_until=monotonic() + 0.25)
                return

    def snapshot(self) -> ExitOwnerSnapshot:
        self.gate.owner_only()
        return ExitOwnerSnapshot(
            "FAILED"
            if self._failure
            else (
                "RESTORING"
                if self._restore is not None
                else "PERSISTING"
                if self._pending
                else "READY"
            ),
            len(self._history),
            tuple(
                sorted(
                    (key, record.state.target_remaining) for key, record in self.gate.latest.items()
                )
            ),
            self._failure,
        )
