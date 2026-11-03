"""Exercise the real journal/engine/entry owners across three signed batches."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from time import monotonic, sleep

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_business_flow import freeze, rows

from auto_trading_system.apps.trade_runtime.engine_simulation import (
    DEMO_NOW,
    open_offline_engine,
    wait_for,
)
from auto_trading_system.apps.trade_runtime.entry_simulation import entry_fixture, publish_entry
from auto_trading_system.apps.trade_runtime.exit_owner_simulation import ExitScenarioClock
from auto_trading_system.apps.trade_runtime.exit_simulation import fill_scenario
from auto_trading_system.gateways.broker.mock import MockBrokerGateway
from auto_trading_system.protocol.v2.api import PlanLookup
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.daily_sync import SessionGrant
from auto_trading_system.protocol.v2.plans import PlanPayload, sign_plan
from auto_trading_system.runtime.engine_v2 import EngineLimits
from auto_trading_system.strategies.entry_gate import EntryStateGate
from auto_trading_system.strategies.entry_owner import EntryStrategyOwner
from auto_trading_system.strategies.entry_state import EntryPolicy, EntryTarget


def phase(phase: str, symbols: tuple[str, ...]) -> tuple[PlanPayload, EntryPolicy]:
    base, policy = entry_fixture()
    source = base.source.model_dump(mode="json")
    source.update(
        {
            "decision_phase": phase,
            "selection_rule": "TAIL_EXCELLENT_V1",
            "source_run_id": "phase-" + phase,
            "scheduled_at": f"2026-09-07T14:{phase[2:]}:00+08:00",
            "market_data_cutoff": f"2026-09-07T14:{phase[2:]}:00+08:00",
            "completed_at": f"2026-09-07T14:{phase[2:]}:01+08:00",
            "published_at": f"2026-09-07T14:{phase[2:]}:01+08:00",
        }
    )
    payload = base.model_dump(mode="json")
    payload.update(
        {
            "source": source,
            "plan_id": "phase-" + phase,
            "allocations": [
                base.allocations[0]
                .model_copy(update={"symbol": symbol, "allocation_id": f"a-{index}", "rank": index})
                .model_dump(mode="json")
                for index, symbol in enumerate(symbols, 1)
            ],
            "risk_budget": base.risk_budget.model_copy(update={"max_buy_value": "2030"}).model_dump(
                mode="json"
            ),
        }
    )
    policy = policy.model_copy(
        update={
            "instruments": tuple(
                policy.instruments[0].model_copy(update={"symbol": symbol}) for symbol in symbols
            )
        }
    )
    return PlanPayload.model_validate_json(canonical_bytes(payload)), policy


def pump(owners: list[EntryStrategyOwner], predicate: object) -> None:
    deadline = monotonic() + 5
    while monotonic() < deadline:
        for owner in owners:
            snapshot = owner.poll()
            assert snapshot.state != "FAILED", snapshot.reason
        if callable(predicate) and predicate():
            return
        sleep(0.001)
    raise AssertionError("phase execution did not settle")


@pytest.mark.parametrize("daily_symbols,budget", [(2, "2030"), (1, "10000")])
def test_three_batches_share_daily_slots_deduplicate_and_restart_paused(
    tmp_path: Path, daily_symbols: int, budget: str
) -> None:
    clock = ExitScenarioClock(DEMO_NOW)
    broker = MockBrokerGateway(clock)
    key = Ed25519PrivateKey.generate()
    plans = [
        phase("1440", ("000001.SZ",)),
        phase("1445", ("000001.SZ", "600000.SH")),
        phase("1450", ("000002.SZ",)),
    ]
    plans = [
        (
            p.model_copy(
                update={"risk_budget": p.risk_budget.model_copy(update={"max_buy_value": budget})}
            ),
            policy,
        )
        for p, policy in plans
    ]
    limits = EngineLimits(
        max_buy_value=budget,
        max_symbol_buy_value="10000",
        max_order_value="1005",
        max_positions=5,
        max_order_quantity=100,
        max_daily_symbols=daily_symbols,
    )

    def targets(owners: list[EntryStrategyOwner]) -> tuple[EntryTarget, ...]:
        return tuple(t for owner in owners for t in owner.gate.export_targets())

    def attach(runtime: object, owners: list[EntryStrategyOwner], index: int) -> EntryStrategyOwner:
        # The runtime is the explicitly hard-wired Mock composition above.
        from typing import Any, cast

        engine = cast(Any, runtime).kernel
        payload, policy = plans[index]
        gate = EntryStateGate(
            payload,
            policy,
            clock=lambda: clock.instant,
            ns_clock=lambda: clock.nanoseconds,
            require_policy_payload_hash=False,
        )
        owner = EntryStrategyOwner(engine, gate)
        owners.append(owner)
        return owner

    with open_offline_engine(
        tmp_path,
        broker=broker,
        clock=lambda: clock.instant,
        plan_payload=plans[0][0],
        signing_key=key,
        limits=limits,
        tail_phases=True,
    ) as runtime:
        owners: list[EntryStrategyOwner] = []
        runtime.kernel.install_entry_runtime(
            lambda item: next(
                (o.gate.allows(item) for o in owners if o.gate.plan_hash == item.plan_hash), False
            ),
            lambda: targets(owners),
            lambda: tuple(o.gate.policy for o in owners),
        )
        first = attach(runtime, owners, 0)
        runtime.kernel.start_recovery()
        wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        pump(owners, lambda: first.gate.ready)
        runtime.confirmed_arm(duration_seconds=900)
        broker.queue_submit(fill_scenario(100, full=True))
        publish_entry(first, clock, 1)
        pump(owners, lambda: dict(first.snapshot().progress).get("000001.SZ") == "FINISHED")
        for index in (1, 2):
            clock.advance(241 if index == 1 else 300)
            payload, _ = plans[index]
            signed = sign_plan(payload, "offline-ephemeral-key", key)
            body = canonical_bytes(signed.model_dump(mode="json"))
            lookup = PlanLookup(
                account_id=payload.account_id,
                strategy_id=payload.strategy_id,
                trade_date=payload.trade_date,
                plan_id=payload.plan_id,
                state="PUBLISHED",
                state_version=index + 1,
                etag=digest(body),
                plan=signed,
            )
            assert (
                runtime.kernel.apply(lookup, runtime.kernel.plans._files.put(body)).status
                == "LOADED"
            )
            owner = attach(runtime, owners, index)
            pump(owners, lambda selected=owner: selected.gate.ready)
            if index == 1:
                broker.queue_submit(fill_scenario(100, full=True))
                for symbol in ("000001.SZ", "600000.SH"):
                    publish_entry(owner, clock, 1, symbol=symbol)
                if daily_symbols == 2:
                    pump(
                        owners,
                        lambda selected=owner: dict(selected.snapshot().progress).get("600000.SH")
                        == "FINISHED",
                    )
                else:
                    pump(owners, lambda selected=owner: bool(selected.snapshot().targets))
                    assert owner.snapshot().targets[0].reason == "POSITION_CAPACITY"
                assert dict(owner.snapshot().progress)["000001.SZ"] == "DUPLICATE_SIGNAL"
            else:
                publish_entry(owner, clock, 1, symbol="000002.SZ")
                pump(owners, lambda selected=owner: bool(selected.snapshot().targets))
                assert owner.snapshot().targets[0].reason == "POSITION_CAPACITY"
        _, _, known = runtime.kernel.exit_inputs()
        orders = [s for s in known if s.action_type == "NEW"]
        assert len(orders) == daily_symbols
        assert sum(Decimal(s.maximum_amount) for s in orders) <= Decimal(budget)
        assert {str(t.symbol) for t in broker.query_trades()} == (
            {"000001.SZ", "600000.SH"} if daily_symbols == 2 else {"000001.SZ"}
        )

    with open_offline_engine(
        tmp_path,
        broker=broker,
        clock=lambda: clock.instant,
        plan_payload=plans[0][0],
        signing_key=key,
        limits=limits,
        tail_phases=True,
    ) as restored:
        owners = []
        restored.kernel.install_entry_runtime(
            lambda item: next(
                (o.gate.allows(item) for o in owners if o.gate.plan_hash == item.plan_hash), False
            ),
            lambda: targets(owners),
            lambda: tuple(o.gate.policy for o in owners),
        )
        for index in range(3):
            attach(restored, owners, index)
        restored.kernel.start_recovery()
        wait_for(restored.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        pump(owners, lambda: all(o.gate.ready for o in owners))
        assert restored.kernel.snapshot().halted
        assert sum(t.target_quantity > 0 for t in targets(owners)) == daily_symbols
        restored.confirmed_arm(duration_seconds=120)
        for owner in owners:
            for symbol in owner.gate.rules:
                publish_entry(owner, clock, 2, symbol=symbol)
        pump(owners, lambda: all(o.snapshot().state == "READY" for o in owners))
        _, _, known = restored.kernel.exit_inputs()
        assert len([s for s in known if s.action_type == "NEW"]) == daily_symbols
        clock.advance(31 * 60)
        restored.kernel.start_recovery()
        wait_for(restored.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        snapshot = restored.kernel.capture_day()
        batch, files = freeze(snapshot, tmp_path / "export")
        results = rows(batch, files, "plan_results")
        assert len(results) == 4 and len({r.record_id for r in results}) == 4
        assert sum(r.data["status"] == "COMPLETED" for r in results) == daily_symbols
        assert sum(r.data["reason_code"] == "DUPLICATE_SIGNAL" for r in results) == 1


def test_live_tail_one_slot_excludes_preexisting_shares_and_never_adopts_them(
    tmp_path: Path,
) -> None:
    # LIVE rules with the explicitly hard-wired Mock composition; no QMT access.
    clock = ExitScenarioClock(DEMO_NOW)
    broker = MockBrokerGateway(clock)
    plan, policy = phase("1440", ("000001.SZ",))
    plan = plan.model_copy(
        update={"risk_budget": plan.risk_budget.model_copy(update={"max_buy_value": "10000"})}
    )
    limits = EngineLimits(
        mode="LIVE",
        max_buy_value="10000",
        max_symbol_buy_value="10000",
        max_order_value="1005",
        max_positions=1,
        max_order_quantity=100,
        max_daily_symbols=1,
        max_total_position_pct="100",
    )
    with open_offline_engine(
        tmp_path,
        broker=broker,
        clock=lambda: clock.instant,
        plan_payload=plan,
        limits=limits,
        tail_phases=True,
        broker_opening_quantity=900,
    ) as rt:
        gate = EntryStateGate(
            plan,
            policy,
            clock=lambda: clock.instant,
            ns_clock=lambda: clock.nanoseconds,
            require_policy_payload_hash=False,
        )
        owner = EntryStrategyOwner(rt.kernel, gate)
        rt.kernel.install_entry_runtime(
            gate.allows,
            gate.export_targets,
            lambda: (policy,),
        )
        rt.kernel.start_recovery()
        wait_for(rt.kernel, lambda state: state.state == "REVIEW_REQUIRED")
        pump([owner], lambda: gate.ready)
        assert rt.kernel._position_symbols() == set()
        report = rt.kernel.snapshot().recovery
        assert report and report.status == "CLEAR"
        confirm = rt.request("CONFIRM_RECOVERY", artifact_hash=report.payload_hash)
        rt.approvals.approve(confirm, authenticated_actor="offline-test-operator")
        assert rt.kernel.control(confirm).status == "PENDING"
        wait_for(rt.kernel, lambda state: state.state == "READY_PAUSED")
        grant = SessionGrant(
            grant_id="grant-live-mock-only",
            account_id=rt.kernel.account_id,
            engine_boot_id=rt.kernel.boot_id,
            session_generation=1,
            mode="LIVE",
            directions=("BUY",),
            max_buy_value="10000",
            max_order_value="1005",
            max_positions=1,
            valid_from=clock.instant,
            valid_until=clock.instant + timedelta(minutes=15),
            confirmed_by="offline-test-operator",
            recovery_report_hash=report.payload_hash,
        )
        arm = rt.request("ARM", grant=grant)
        rt.approvals.approve(arm, authenticated_actor="offline-test-operator")
        assert rt.kernel.control(arm).status == "PENDING"
        wait_for(rt.kernel, lambda state: state.state == "ARMED")
        broker.queue_submit(fill_scenario(100, full=True))
        publish_entry(owner, clock, 1)
        pump([owner], lambda: dict(owner.snapshot().progress).get("000001.SZ") == "FINISHED")
        assert rt.kernel._position_symbols() == {"000001.SZ"}
        assert sum(lot.remaining for lot in rt.kernel.snapshot().lots) == 100
        assert rt.kernel._portfolio.unmanaged_positions == (("000001.SZ", 900),)
        assert len(broker.query_orders()) == 1
        assert not rt.kernel.snapshot().halted
        assert rt.kernel.entry_resources("000001.SZ", gate.plan_hash)[2] == 1
