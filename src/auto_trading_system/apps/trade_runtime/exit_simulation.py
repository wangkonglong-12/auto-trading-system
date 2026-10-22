"""Mock-only D0 close / D1 partial exit / restart / residual child acceptance."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from time import monotonic
from uuid import uuid4

from pydantic import JsonValue

from auto_trading_system.apps.trade_runtime.engine_simulation import (
    DEMO_NOW,
    OfflineEngine,
    open_offline_engine,
    wait_for,
)
from auto_trading_system.domain import Money, Price, Quantity
from auto_trading_system.gateways.broker.mock import (
    MockBrokerGateway,
    SubmitOutcome,
    SubmitScenario,
)
from auto_trading_system.journal.critical_models import Submission
from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.protocol.v2.plans import PlanPayload
from auto_trading_system.reconcile.portfolio_checkpoint import OpeningReference
from auto_trading_system.strategies.exit_execution import exit_child, restore_exit_state
from auto_trading_system.strategies.exit_v3 import (
    ExitLot,
    ExitObservation,
    ExitState,
    evaluate_exit,
)
from auto_trading_system.testing.v31_scenarios import ScenarioClock, example_plan

D1_NOW = (DEMO_NOW + timedelta(days=1)).replace(hour=9, minute=31)


def next_day_plan() -> PlanPayload:
    """Deterministic fixture only; no fabricated production source/price evidence."""
    plan = example_plan()
    source = plan.source
    source = source.model_copy(
        update={
            name: D1_NOW.replace(hour=9, minute=25)
            for name in ("completed_at", "market_data_cutoff", "scheduled_at", "published_at")
        }
    )
    window = plan.execution_window.model_copy(
        update={
            name: getattr(plan.execution_window, name) + timedelta(days=1)
            for name in ("entry_not_before", "stop_new_buy_at", "cancel_by")
        }
    )
    return PlanPayload.model_validate_json(
        plan.model_copy(
            update={
                "plan_id": "simulation-d1-plan",
                "trade_date": D1_NOW.date(),
                "source": source,
                "execution_window": window,
                "risk_budget": plan.risk_budget.model_copy(update={"budget_basis_as_of": D1_NOW}),
                "offline_authorization": plan.offline_authorization.model_copy(
                    update={
                        "valid_until": D1_NOW.replace(hour=15, minute=0),
                    }
                ),
            }
        ).model_dump_json()
    )


def fill_scenario(quantity: int, *, full: bool) -> SubmitScenario:
    return SubmitScenario(
        outcome=SubmitOutcome.FULL_FILL if full else SubmitOutcome.PARTIAL_FILL,
        partial_fill_quantity=Quantity(quantity),
        fill_price=Price.parse("10"),
        fill_notional=Money.parse(str(10 * quantity)),
    )


def settle(runtime: OfflineEngine, order: Submission) -> None:
    runtime.kernel.submit(order, valid_until=monotonic() + 2)
    wait_for(
        runtime.kernel,
        lambda s: any(
            item.client_order_id == order.client_order_id and item.status == "SDK_ACCEPTED"
            for item in s.submissions
        ),
    )
    wait_for(runtime.kernel, lambda _: runtime.kernel.critical_idle)


def close_buy_day(directory: Path) -> OpeningReference:
    clock = ScenarioClock(DEMO_NOW)
    broker = MockBrokerGateway(clock)
    with open_offline_engine(directory, broker=broker, clock=lambda: clock.instant) as runtime:
        runtime.kernel.start_recovery()
        wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        runtime.confirmed_arm()
        broker.queue_submit(fill_scenario(100, full=True))
        settle(runtime, runtime.buy())
        assert runtime.kernel.snapshot().lots[0].remaining == 100
        clock = ScenarioClock(DEMO_NOW.replace(hour=15, minute=1))
        runtime.kernel.start_recovery()
        wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        return runtime.kernel.seal_portfolio().result(timeout=2)  # Harness only.


def prepare_child(
    runtime: OfflineEngine, opening: OpeningReference, *, state: ExitState | None = None
) -> Submission | None:
    lot = runtime.kernel.snapshot().lots[0]
    # Costs/open/previous close are explicit test fixtures. Formal market and
    # adjusted-cost sources must be qualified before wiring the live strategy.
    context = ExitLot(
        lot.lot_id,
        lot.bought_quantity,
        lot.trade_date_acquired,
        lot.eligible_sell_date,
        Price.parse("10"),
        Price.parse("10"),
        Price.parse("10.30"),
    )
    decision = evaluate_exit(
        context,
        state or ExitState(lot.bought_quantity),
        ExitObservation(
            runtime.clock(),
            1_000_000,
            Price.parse("10"),
            True,
            True,
        ),
    )
    return exit_child(
        lot=lot,
        decision=decision,
        account_id=runtime.kernel.account_id,
        trade_date=runtime.kernel.trade_date,
        plan_hash=runtime.plan.envelope.payload_hash,
        opening_hash=opening.payload_hash,
        boot_id=runtime.kernel.boot_id,
        permission_epoch=runtime.kernel.snapshot().permission_epoch,
        limit_price=Price.parse("10"),
        quantity_limit=100,
        broker_sellable=lot.remaining,
    )


def run_exit_simulation(output_root: Path) -> dict[str, JsonValue]:
    directory = output_root.resolve() / ("exit-simulation-" + uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    opening = close_buy_day(directory)
    broker = MockBrokerGateway(ScenarioClock(D1_NOW))
    with open_offline_engine(
        directory,
        broker=broker,
        clock=lambda: D1_NOW,
        plan_payload=next_day_plan(),
        opening_reference=opening,
        broker_opening_quantity=100,
    ) as runtime:
        runtime.kernel.start_recovery()
        wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        runtime.confirmed_arm(directions=("SELL",))
        order = prepare_child(runtime, opening)
        assert order and order.quantity == 50 and order.target.target_remaining == 50
        broker.queue_submit(fill_scenario(20, full=False))
        settle(runtime, order)
        observed = broker.query_orders()[0]
        cancel = order.model_copy(
            update={
                "client_order_id": "ord_" + uuid4().hex,
                "action_type": "CANCEL",
                "original_order_id": observed.broker_order_id,
                "original_client_order_id": order.client_order_id,
            }
        )
        settle(runtime, cancel)
        assert runtime.kernel.snapshot().lots[0].remaining == 80
        first_boot = runtime.kernel.boot_id
    with open_offline_engine(
        directory,
        broker=broker,
        clock=lambda: D1_NOW,
        plan_payload=next_day_plan(),
        opening_reference=opening,
        broker_opening_quantity=100,
    ) as runtime:
        runtime.kernel.start_recovery()
        recovered = wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        assert (
            recovered.halted and recovered.grant_id is None and first_boot != runtime.kernel.boot_id
        )
        records = runtime.journal.recovery_page().result(timeout=2)
        state = restore_exit_state(
            recovered.lots[0],
            tuple(r.submission for r in records),
            account_id=runtime.kernel.account_id,
        )
        assert state.target_remaining == 50 and not state.path_complete
        runtime.confirmed_arm(directions=("SELL",))
        residual = prepare_child(runtime, opening, state=state)
        assert residual and residual.quantity == 30
        broker.queue_submit(fill_scenario(30, full=True))
        settle(runtime, residual)
        assert prepare_child(runtime, opening, state=state) is None
        final = runtime.kernel.snapshot()
        assert final.lots[0].remaining == 50 and final.lots[0].reserved_quantity == 0
        # No saved monotonic duration is inferred to span the restart.
        assert state.drawdown_since_ns is None
        result: dict[str, JsonValue] = {
            "mode": "OFFLINE_EXIT",
            "real_trading_enabled": False,
            "opening_quantity": 100,
            "initial_exit_quantity": 50,
            "first_fill": 20,
            "residual_child_quantity": 30,
            "final_remaining": 50,
            "target_remaining": 50,
            "restart_paused": recovered.halted,
            "restored_path_complete": state.path_complete,
            "d1_broker_orders": len(broker.query_orders()),
            "opening_checkpoint_hash": opening.payload_hash,
            "output_directory": str(directory),
        }
    (directory / "result.json").write_bytes(canonical_bytes(result))
    return result
