"""Explicit Mock-only acceptance of the continuously polled D1 strategy owner.

All prices, calendar sessions and keys here are test fixtures. Neither this
entry point nor the driver can load a production gateway or create a live grant.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import JsonValue

from auto_trading_system.apps.trade_runtime.engine_simulation import (
    DEMO_NOW,
    OfflineEngine,
    open_offline_engine,
    wait_for,
)
from auto_trading_system.apps.trade_runtime.exit_simulation import (
    D1_NOW,
    close_buy_day,
    fill_scenario,
)
from auto_trading_system.domain import ShanghaiTimestamp
from auto_trading_system.gateways.broker.mock import MockBrokerGateway
from auto_trading_system.journal.apsw_store import APSWCriticalStore
from auto_trading_system.journal.checkpoint_blobs import read_checkpoint
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.reconcile.portfolio_checkpoint import OpeningReference, PortfolioCheckpoint
from auto_trading_system.reconcile.position_lots import ExchangeCalendar
from auto_trading_system.strategies.exit_context import (
    ExitLotContext,
    ExitSessionContext,
    sign_exit_context,
    verify_exit_context,
)
from auto_trading_system.strategies.exit_owner import D1StrategyOwner, ExitQuote, ExitStateGate


@dataclass
class ExitScenarioClock:
    instant: datetime = D1_NOW
    nanoseconds: int = 1_000_000_000

    def now(self) -> ShanghaiTimestamp:
        return ShanghaiTimestamp(self.instant)

    def advance(self, seconds: int) -> None:
        self.instant += timedelta(seconds=seconds)
        self.nanoseconds += seconds * 1_000_000_000


def fixture_context(
    directory: Path, opening: OpeningReference, *, open_price: str = "10"
) -> ExitSessionContext:
    """Read only a closed Mock Journal, then sign explicit fixture source facts."""
    store = APSWCriticalStore(directory / "critical/journal-v2.db", "simulation-account")
    store.open()
    try:
        body = read_checkpoint(store, opening.key)
        assert body is not None
        checkpoint = PortfolioCheckpoint.model_validate_json(body)
    finally:
        store.close()
    lot = checkpoint.lots[0]
    calendar = ExchangeCalendar(
        version="offline-calendar-v1",
        sessions=tuple((DEMO_NOW + timedelta(days=n)).date() for n in range(3)),
    )
    payload = ExitSessionContext(
        context_id="mock-opening-exit",
        account_id=checkpoint.account_id,
        strategy_id=lot.strategy_id,
        trade_date=D1_NOW.date(),
        opening_hash=opening.payload_hash,
        calendar_hash=digest(canonical_bytes(calendar.model_dump(mode="json"))),
        issued_at=D1_NOW.replace(hour=9, minute=30),
        valid_from=D1_NOW.replace(hour=9, minute=30),
        stop_new_at=D1_NOW.replace(hour=14, minute=55, second=30),
        cancel_by=D1_NOW.replace(hour=14, minute=56),
        valid_until=D1_NOW.replace(hour=15, minute=0),
        max_order_value="1005",
        max_order_quantity=100,
        lots=(
            ExitLotContext(
                lot_id=lot.lot_id,
                symbol=lot.symbol,
                cost="10",
                previous_close="10",
                open_price=open_price,
                price_tick="0.01",
                lower_price="8",
                upper_price="12",
                sell_step=1,
                allow_odd_liquidation=True,
                source_hash=digest(b"MOCK ONLY sources"),
            ),
        ),
    )
    private = Ed25519PrivateKey.generate()
    signed = sign_exit_context(payload, "mock-exit-key", private)
    return verify_exit_context(
        canonical_bytes(signed.model_dump(mode="json")),
        {signed.key_id: private.public_key()},
        now=D1_NOW,
    )


@contextmanager
def open_exit_owner(
    directory: Path,
    opening: OpeningReference,
    context: ExitSessionContext,
    clock: ExitScenarioClock,
    broker: MockBrokerGateway,
) -> Iterator[tuple[OfflineEngine, D1StrategyOwner]]:
    gate = ExitStateGate(context, clock=lambda: clock.instant, ns_clock=lambda: clock.nanoseconds)
    with open_offline_engine(
        directory,
        broker=broker,
        clock=lambda: clock.instant,
        opening_reference=opening,
        broker_opening_quantity=100,
        exit_context=context,
        exit_state_guard=gate.allows,
    ) as runtime:
        assert runtime.kernel.plans.plans.loaded() is None
        owner = D1StrategyOwner(runtime.kernel, gate)
        runtime.kernel.start_recovery()
        wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        pump(owner, lambda: owner.snapshot().state == "READY")
        yield runtime, owner


def pump(owner: D1StrategyOwner, predicate: Callable[[], bool]) -> None:
    """Blocking only in the test harness; the application owner never waits."""
    until = monotonic() + 3
    while monotonic() < until:
        state = owner.poll()
        if state.state == "FAILED":
            raise RuntimeError(state.reason)
        if predicate():
            return
        if owner.engine.snapshot().state == "HALTED":
            raise RuntimeError(owner.engine.permission.snapshot().reason)
        sleep(0.001)
    raise TimeoutError("D1 owner did not settle")


def publish(owner: D1StrategyOwner, clock: ExitScenarioClock, price: str, sequence: int) -> None:
    owner.quote(
        ExitQuote(
            symbol="000001.SZ",
            sequence=sequence,
            observed_at=clock.instant,
            received_ns=clock.nanoseconds,
            last_price=price,
            bid_price="10",
            tradable=True,
        )
    )


def run_exit_owner_simulation(output_root: Path) -> dict[str, JsonValue]:
    directory = output_root.resolve() / ("exit-owner-" + uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    quiet = directory / "no-order-restart"
    opening = close_buy_day(quiet)
    context = fixture_context(quiet, opening)
    clock = ExitScenarioClock(D1_NOW.replace(hour=10, minute=30))
    broker = MockBrokerGateway(clock)
    with open_exit_owner(quiet, opening, context, clock, broker) as (runtime, owner):
        runtime.confirmed_arm(directions=("SELL",))
        publish(owner, clock, "10.4", 1)
        pump(owner, lambda: owner.snapshot().revision == 1 and owner.snapshot().state == "READY")
        assert not broker.query_orders()
        assert "TIME_1030_OBSERVED" in owner.gate.latest[context.lots[0].lot_id].state.fired_rules
    clock.advance(60)
    with open_exit_owner(quiet, opening, context, clock, broker) as (runtime, owner):
        paused = runtime.kernel.snapshot().halted and runtime.kernel.snapshot().grant_id is None
        runtime.confirmed_arm(directions=("SELL",))
        publish(owner, clock, "10", 1)
        owner.poll()
        assert owner.snapshot().targets == ((context.lots[0].lot_id, 100),)
        assert owner.snapshot().revision == 1 and not broker.query_orders()

    active = directory / "active-target"
    opening = close_buy_day(active)
    context = fixture_context(active, opening, open_price="10.3")
    clock = ExitScenarioClock()
    broker = MockBrokerGateway(clock)
    with open_exit_owner(active, opening, context, clock, broker) as (runtime, owner):
        runtime.confirmed_arm(directions=("SELL",))
        broker.queue_submit(fill_scenario(20, full=False))
        publish(owner, clock, "10", 1)
        pump(
            owner,
            lambda: bool(runtime.kernel.snapshot().lots)
            and runtime.kernel.snapshot().lots[0].sold_quantity == 20
            and runtime.kernel.critical_idle,
        )
        clock.advance(1)
        publish(owner, clock, "10.7", 2)
        pump(owner, lambda: owner.snapshot().revision == 2 and owner.snapshot().state == "READY")
        assert owner.snapshot().targets == ((context.lots[0].lot_id, 0),)
        assert len(broker.query_orders()) == 1
        broker.queue_submit(fill_scenario(80, full=True))
        clock.advance(30)
        # A stale quote cannot stop a due cancellation.
        pump(owner, lambda: broker.query_orders()[0].is_terminal and runtime.kernel.critical_idle)
        assert len(broker.query_orders()) == 1
        publish(owner, clock, "10.7", 3)
        pump(
            owner,
            lambda: runtime.kernel.snapshot().lots[0].remaining == 0
            and runtime.kernel.critical_idle,
        )
        assert len(broker.query_orders()) == 2
        assert [o.order_quantity.value for o in broker.query_orders()] == [50, 80]
        revision = owner.snapshot().revision
    with open_exit_owner(active, opening, context, clock, broker) as (runtime, owner):
        assert runtime.kernel.snapshot().halted and owner.snapshot().revision == revision
        assert runtime.kernel.snapshot().lots[0].remaining == 0
    result: dict[str, JsonValue] = {
        "mode": "OFFLINE_EXIT_OWNER",
        "real_trading_enabled": False,
        "afternoon_plan_loaded": False,
        "restart_paused": paused,
        "no_order_state_restored": True,
        "time_check_not_repeated": True,
        "target_tightened_while_order_active": True,
        "stale_quote_cancelled": True,
        "first_child_quantity": 50,
        "first_fill": 20,
        "residual_child_quantity": 80,
        "final_remaining": 0,
        "active_state_revisions": revision,
        "d1_broker_orders": 2,
        "output_directory": str(directory),
    }
    (directory / "result.json").write_bytes(canonical_bytes(result))
    return result
