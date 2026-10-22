"""Hard-wired Mock acceptance: fixed target, partial cancel, restart and replacement."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

from pydantic import JsonValue

from auto_trading_system.apps.trade_runtime.engine_simulation import (
    DEMO_NOW,
    OfflineEngine,
    open_offline_engine,
    wait_for,
)
from auto_trading_system.apps.trade_runtime.exit_owner_simulation import ExitScenarioClock
from auto_trading_system.apps.trade_runtime.exit_simulation import fill_scenario
from auto_trading_system.gateways.broker.mock import MockBrokerGateway
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.plans import PlanPayload
from auto_trading_system.runtime.engine_v2 import EngineLimits
from auto_trading_system.strategies.entry_gate import EntryQuote, EntryStateGate
from auto_trading_system.strategies.entry_owner import EntryStrategyOwner
from auto_trading_system.strategies.entry_state import EntryInstrument, EntryPolicy
from auto_trading_system.testing.v31_scenarios import example_plan


def entry_fixture() -> tuple[PlanPayload, EntryPolicy]:
    """Explicit fictional 10-share Mock rule, never inferred as an exchange rule."""
    policy = EntryPolicy(
        version="mock-entry-v1",
        rulebook_version="simulation-v1",
        fee_reserve="5",
        max_chase_bps=60,
        max_spread_bps=30,
        participation_bps=300,
        depth_bps=1000,
        quote_max_age_ms=5000,
        cancel_after_seconds=20,
        max_children=4,
        max_active_orders=3,
        instruments=(
            EntryInstrument(
                symbol="000001.SZ",
                reference_price="10",
                lower_price="9",
                upper_price="11",
                price_tick="0.01",
                min_quantity=10,
                quantity_step=10,
                max_order_quantity=100,
                source_hash=digest(b"fictional Mock source and quantity rules"),
            ),
        ),
    )
    plan = example_plan()
    plan = plan.model_copy(
        update={
            "allocations": (
                plan.allocations[0].model_copy(
                    update={
                        "target_value": "1000",
                        "max_value": "1020",
                        "max_qty": 100,
                    }
                ),
            ),
            "risk_budget": plan.risk_budget.model_copy(update={"max_buy_value": "1020"}),
            "policy_bundle": tuple(
                p.model_copy(
                    update={
                        "version": policy.version,
                        "payload_hash": policy.payload_hash,
                    }
                )
                if p.component == "entry"
                else p
                for p in plan.policy_bundle
            ),
        }
    )
    return PlanPayload.model_validate_json(plan.model_dump_json()), policy


@contextmanager
def open_entry_owner(
    directory: Path,
    clock: ExitScenarioClock,
    broker: MockBrokerGateway,
    *,
    plan: PlanPayload | None = None,
    policy: EntryPolicy | None = None,
) -> Iterator[tuple[OfflineEngine, EntryStrategyOwner]]:
    base_plan, base_policy = entry_fixture()
    plan, policy = plan or base_plan, policy or base_policy
    gate = EntryStateGate(
        plan, policy, clock=lambda: clock.instant, ns_clock=lambda: clock.nanoseconds
    )
    with open_offline_engine(
        directory,
        clock=lambda: clock.instant,
        broker=broker,
        plan_payload=plan,
        entry_state_guard=gate.allows,
        entry_target_reader=gate.export_targets,
        limits=EngineLimits(
            max_buy_value=plan.risk_budget.max_buy_value,
            max_order_value="1005",
            max_positions=3,
            max_order_quantity=100,
        ),
    ) as runtime:
        owner = EntryStrategyOwner(runtime.kernel, gate)
        runtime.kernel.start_recovery()
        wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        pump_entry(owner, lambda: owner.snapshot().state == "READY")
        yield runtime, owner


def pump_entry(owner: EntryStrategyOwner, predicate: Callable[[], bool]) -> None:
    deadline = monotonic() + 5
    while monotonic() < deadline:
        state = owner.poll()
        if state.state == "FAILED":
            raise RuntimeError(state.reason)
        if predicate():
            return
        sleep(0.001)
    raise TimeoutError("entry acceptance did not settle")


def publish_entry(
    owner: EntryStrategyOwner,
    clock: ExitScenarioClock,
    sequence: int,
    *,
    ask: str = "10",
    depth: int = 1000,
    symbol: str = "000001.SZ",
) -> None:
    owner.quote(
        EntryQuote(
            symbol=symbol,
            sequence=sequence,
            observed_at=clock.instant,
            received_ns=clock.nanoseconds,
            bid_price=ask,
            ask_price=ask,
            ask_quantity=depth,
            recent_volume=100000,
            cage_upper="11",
            path_complete=True,
            tradable=True,
        )
    )


def run_entry_simulation(output_root: Path) -> dict[str, JsonValue]:
    directory = output_root / ("entry-owner-" + uuid4().hex)
    clock = ExitScenarioClock(DEMO_NOW)
    broker = MockBrokerGateway(clock)
    with open_entry_owner(directory, clock, broker) as (runtime, owner):
        runtime.confirmed_arm()
        broker.queue_submit(fill_scenario(40, full=False))
        publish_entry(owner, clock, 1)
        pump_entry(
            owner, lambda: bool(runtime.kernel.snapshot().lots) and runtime.kernel.critical_idle
        )
        first = owner.snapshot().targets[0]
        clock.advance(21)
        publish_entry(owner, clock, 2, ask="10.2")
        pump_entry(
            owner,
            lambda: runtime.kernel.snapshot().orders[0].phase == "TERMINAL_CONFIRMED"
            and runtime.kernel.critical_idle,
        )
        runtime.kernel.emergency_halt("MOCK_RESTART")
        owner.poll()
    clock.advance(1)
    with open_entry_owner(directory, clock, broker) as (runtime, owner):
        paused = runtime.kernel.snapshot().halted
        restored = owner.snapshot().targets[0] == first
        runtime.confirmed_arm()
        broker.queue_submit(fill_scenario(60, full=True))
        publish_entry(owner, clock, 1)
        pump_entry(owner, lambda: dict(owner.snapshot().progress).get("000001.SZ") == "FINISHED")
        _, _, known = runtime.kernel.exit_inputs()
        quantities: list[JsonValue] = [s.quantity for s in known if s.action_type == "NEW"]
        result: dict[str, JsonValue] = {
            "real_trading_enabled": False,
            "restart_paused": paused,
            "target_restored": restored,
            "target_quantity": first.target_quantity,
            "child_quantities": quantities,
            "filled_quantity": sum(lot.bought_quantity for lot in runtime.kernel.snapshot().lots),
            "conservative_amount": str(sum(e.amount for e in runtime.kernel.entry_exposures())),
            "journal_directory": str(directory),
        }
    output_root.mkdir(parents=True, exist_ok=True)
    with (directory / "entry-result.json").open("xb") as output:
        output.write(canonical_bytes(result))
    return result
