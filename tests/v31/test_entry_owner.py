from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import pytest
from pydantic import JsonValue, ValidationError
from test_business_flow import freeze, rows

from auto_trading_system.apps.trade_runtime.__main__ import main
from auto_trading_system.apps.trade_runtime.engine_simulation import (
    DEMO_NOW,
    OfflineEngine,
    wait_for,
)
from auto_trading_system.apps.trade_runtime.entry_simulation import (
    entry_fixture,
    open_entry_owner,
    publish_entry,
    pump_entry,
)
from auto_trading_system.apps.trade_runtime.exit_owner_simulation import ExitScenarioClock
from auto_trading_system.apps.trade_runtime.exit_simulation import fill_scenario
from auto_trading_system.execution.entry_budget import buy_exposures
from auto_trading_system.gateways.broker.mock import (
    MockBrokerGateway,
    SubmitOutcome,
    SubmitScenario,
)
from auto_trading_system.journal.apsw_store import APSWCriticalStore
from auto_trading_system.journal.critical_models import CriticalStoreError, EntryBinding
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.plans import PlanPayload
from auto_trading_system.strategies.entry_gate import EntryQuote
from auto_trading_system.strategies.entry_owner import EntryStrategyOwner
from auto_trading_system.strategies.entry_state import (
    EntryInstrument,
    EntryPolicy,
    EntryTarget,
    append_entry_target,
    load_entry_targets,
    validate_policy,
)


def bound_policy(plan: PlanPayload, policy: EntryPolicy) -> PlanPayload:
    return plan.model_copy(
        update={
            "policy_bundle": tuple(
                p.model_copy(
                    update={"version": policy.version, "payload_hash": policy.payload_hash}
                )
                if p.component == "entry"
                else p
                for p in plan.policy_bundle
            )
        }
    )


@contextmanager
def armed(
    path: Path,
    *,
    plan: PlanPayload | None = None,
    policy: EntryPolicy | None = None,
) -> Iterator[tuple[OfflineEngine, EntryStrategyOwner, ExitScenarioClock]]:
    clock = ExitScenarioClock(DEMO_NOW)
    broker = MockBrokerGateway(clock)
    with open_entry_owner(path, clock, broker, plan=plan, policy=policy) as (runtime, owner):
        runtime.confirmed_arm()
        yield runtime, owner, clock


def target_fixture() -> EntryTarget:
    plan, policy = entry_fixture()
    return EntryTarget(
        plan_hash=digest(canonical_bytes(plan.model_dump(mode="json"))),
        policy_hash=policy.payload_hash,
        account_id=plan.account_id,
        trade_date=plan.trade_date,
        allocation_id=plan.allocations[0].allocation_id,
        symbol="000001.SZ",
        boot_id="mock-boot",
        decided_at=DEMO_NOW,
        arrival_price="10",
        target_quantity=100,
        reason="ELIGIBLE",
    )


def test_entry_restart_acceptance_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--simulate-entry-owner", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["child_quantities"] == [100, 60]
    assert result["filled_quantity"] == result["target_quantity"] == 100
    assert result["conservative_amount"] == "1010"
    assert result["target_restored"] and result["restart_paused"]
    assert not result["real_trading_enabled"]


def test_new_persisted_fields_have_descriptions_and_legacy_hash_is_stable() -> None:
    for model in (EntryInstrument, EntryPolicy, EntryTarget, EntryQuote, EntryBinding):
        assert all(f.description for f in model.model_fields.values())
    old: dict[str, JsonValue] = {"plan_id": "p", "allocation_id": "a", "target_quantity": 100}
    binding = EntryBinding.model_validate_json(canonical_bytes(old))
    assert canonical_bytes(binding.model_dump(mode="json")) == canonical_bytes(old)


@pytest.mark.parametrize("case", ["hash", "version", "rulebook", "symbol", "too-many"])
def test_policy_is_pinned_by_signed_plan(case: str) -> None:
    plan, policy = entry_fixture()
    if case == "hash":
        policy = policy.model_copy(update={"fee_reserve": "6"})
    elif case == "version":
        policy = policy.model_copy(update={"version": "changed"})
    elif case == "rulebook":
        policy = policy.model_copy(update={"rulebook_version": "changed"})
        plan = bound_policy(plan, policy)
    elif case == "symbol":
        policy = policy.model_copy(
            update={
                "instruments": (policy.instruments[0].model_copy(update={"symbol": "000002.SZ"}),)
            }
        )
        plan = bound_policy(plan, policy)
    else:
        policy = policy.model_copy(update={"instruments": policy.instruments * 16})
        with pytest.raises(ValidationError):
            EntryPolicy.model_validate_json(policy.model_dump_json())
        return
    with pytest.raises(ValueError):
        validate_policy(plan, policy)


@pytest.mark.parametrize(
    "field,value",
    [
        ("reference_price", "0"),
        ("price_tick", "0"),
        ("lower_price", "12"),
        ("min_quantity", 0),
        ("quantity_step", 0),
        ("max_order_quantity", 1),
    ],
)
def test_instrument_validation(field: str, value: object) -> None:
    body = entry_fixture()[1].instruments[0].model_dump(mode="json")
    body[field] = value
    with pytest.raises(ValidationError):
        EntryInstrument.model_validate_json(canonical_bytes(body))


@pytest.mark.parametrize("case", ["reason", "zero", "price", "missing-price", "utc", "day"])
def test_target_validation(case: str) -> None:
    target = target_fixture()
    updates: dict[str, dict[str, object]] = {
        "reason": {"reason": "SKIPPED_MIN_ORDER"},
        "zero": {"target_quantity": 0},
        "price": {"arrival_price": "0"},
        "missing-price": {"arrival_price": None},
        "utc": {"decided_at": DEMO_NOW.astimezone(UTC)},
        "day": {"decided_at": DEMO_NOW + timedelta(days=1)},
    }
    with pytest.raises(ValidationError):
        EntryTarget.model_validate_json(target.model_copy(update=updates[case]).model_dump_json())


def test_targets_are_immutable_and_day_context_cannot_reset(tmp_path: Path) -> None:
    plan, policy = entry_fixture()
    store = APSWCriticalStore(tmp_path / "state.db", plan.account_id)
    store.open()
    try:
        assert load_entry_targets(store, plan, policy) == ()
        target = target_fixture()
        assert append_entry_target(store, target)
        assert not append_entry_target(store, target)
        assert load_entry_targets(store, plan, policy) == (target,)
        with pytest.raises(CriticalStoreError, match="CONFLICT"):
            append_entry_target(store, target.model_copy(update={"target_quantity": 90}))
        with pytest.raises(CriticalStoreError, match="CONFLICT"):
            load_entry_targets(store, plan.model_copy(update={"plan_id": "different"}), policy)
    finally:
        store.close()


@pytest.mark.parametrize(
    "case", ["stale-wall", "stale-ns", "path", "tradable", "spread", "cage", "chase", "floor"]
)
def test_market_rejection_waits_without_target_or_order(tmp_path: Path, case: str) -> None:
    with armed(tmp_path) as (runtime, owner, clock):
        publish_entry(owner, clock, 1)
        quote = owner.gate.quotes["000001.SZ"]
        updates: dict[str, dict[str, object]] = {
            "stale-wall": {"observed_at": clock.instant - timedelta(seconds=6)},
            "stale-ns": {"received_ns": 0},
            "path": {"path_complete": False},
            "tradable": {"tradable": False},
            "spread": {"bid_price": "9.9"},
            "cage": {"cage_upper": "9.99"},
            "chase": {"ask_price": "10.1", "bid_price": "10.1"},
            "floor": {"ask_price": "8", "bid_price": "8"},
        }
        if case == "stale-ns":
            clock.nanoseconds += 6_000_000_000
        owner.gate.quotes[quote.symbol] = quote.model_copy(update=updates[case])
        for _ in range(3):
            owner.poll()
        assert not owner.snapshot().targets and not runtime.kernel.snapshot().submissions
        assert dict(owner.snapshot().progress)[quote.symbol] == "WAITING_MARKET"


@pytest.mark.parametrize("case", ["sequence", "ns", "future-wall", "future-ns", "outside"])
def test_bad_quote_fences_or_rejects(tmp_path: Path, case: str) -> None:
    with armed(tmp_path) as (runtime, owner, clock):
        publish_entry(owner, clock, 1)
        quote = owner.gate.quotes["000001.SZ"]
        updates: dict[str, dict[str, object]] = {
            "sequence": {"sequence": 1},
            "ns": {"sequence": 2},
            "future-wall": {
                "sequence": 2,
                "received_ns": clock.nanoseconds + 1,
                "observed_at": clock.instant + timedelta(seconds=1),
            },
            "future-ns": {"sequence": 2, "received_ns": clock.nanoseconds + 1},
            "outside": {"symbol": "000002.SZ"},
        }
        if case == "outside":
            with pytest.raises(ValueError, match="outside"):
                owner.quote(quote.model_copy(update=updates[case]))
        else:
            owner.quote(quote.model_copy(update=updates[case]))
            assert owner.poll().state == "FAILED"
            assert runtime.kernel.snapshot().halted


def test_zero_depth_target_persists_without_j1_and_survives_restart(tmp_path: Path) -> None:
    clock = ExitScenarioClock(DEMO_NOW)
    broker = MockBrokerGateway(clock)
    with open_entry_owner(tmp_path, clock, broker) as (runtime, owner):
        runtime.confirmed_arm()
        publish_entry(owner, clock, 1, depth=0)
        pump_entry(owner, lambda: len(owner.snapshot().targets) == 1)
        target = owner.snapshot().targets[0]
        assert not runtime.kernel.snapshot().submissions
    clock.advance(1)
    with open_entry_owner(tmp_path, clock, broker) as (runtime, owner):
        assert owner.snapshot().targets == (target,)
        assert runtime.kernel.snapshot().halted
        publish_entry(owner, clock, 1, ask="9.9")
        owner.poll()
        assert not runtime.kernel.snapshot().submissions


@pytest.mark.parametrize("case", ["minimum", "slots", "window"])
def test_skip_is_durable_and_not_reopened(tmp_path: Path, case: str) -> None:
    plan, _ = entry_fixture()
    if case == "minimum":
        plan = plan.model_copy(
            update={"allocations": (plan.allocations[0].model_copy(update={"target_value": "9"}),)}
        )
    elif case == "window":
        plan = plan.model_copy(
            update={
                "execution_window": plan.execution_window.model_copy(
                    update={"stop_new_buy_at": DEMO_NOW + timedelta(seconds=60)}
                )
            }
        )
    with armed(tmp_path, plan=plan) as (runtime, owner, clock):
        if case == "slots":
            # Use a smaller authentic grant ceiling, not a client-supplied plan permission.
            assert runtime.kernel._grant is not None
            runtime.kernel._grant = runtime.kernel._grant.model_copy(update={"max_positions": 0})
        elif case == "window":
            clock.advance(31)
        publish_entry(owner, clock, 1)
        pump_entry(owner, lambda: len(owner.snapshot().targets) == 1)
        target = owner.snapshot().targets[0]
        assert target.target_quantity == 0
        assert (
            target.reason
            == {
                "minimum": "SKIPPED_MIN_ORDER",
                "slots": "POSITION_CAPACITY",
                "window": "MISSED_ENTRY_WINDOW",
            }[case]
        )
        clock.advance(1)
        publish_entry(owner, clock, 2)
        owner.poll()
        assert owner.snapshot().targets == (target,)
        assert not runtime.kernel.snapshot().submissions


def test_repeated_poll_or_same_quote_does_not_duplicate_child(tmp_path: Path) -> None:
    with armed(tmp_path) as (runtime, owner, clock):
        publish_entry(owner, clock, 1)
        pump_entry(
            owner, lambda: len(runtime.broker.query_orders()) == 1 and runtime.kernel.critical_idle
        )
        for _ in range(20):
            owner.poll()
        assert len(runtime.broker.query_orders()) == 1
        assert dict(owner.snapshot().progress)["000001.SZ"] == "ORDER_ACTIVE"


def test_timeout_cancels_unchanged_buy_and_requotes_only_after_terminal_evidence(
    tmp_path: Path,
) -> None:
    with armed(tmp_path) as (runtime, owner, clock):
        publish_entry(owner, clock, 1)
        pump_entry(
            owner, lambda: len(runtime.broker.query_orders()) == 1 and runtime.kernel.critical_idle
        )

        clock.advance(21)
        pump_entry(
            owner,
            lambda: runtime.kernel.snapshot().orders[0].phase == "TERMINAL_CONFIRMED"
            and runtime.kernel.critical_idle,
        )
        assert runtime.broker.query_orders()[0].status.value == "CANCELLED"

        runtime.broker.queue_submit(fill_scenario(100, full=True))
        publish_entry(owner, clock, 2)
        pump_entry(owner, lambda: dict(owner.snapshot().progress).get("000001.SZ") == "FINISHED")
        assert len(runtime.broker.query_orders()) == 2
        assert sum(lot.bought_quantity for lot in runtime.kernel.snapshot().lots) == 100


def test_rejected_children_count_toward_limit_across_fresh_quotes(tmp_path: Path) -> None:
    plan, policy = entry_fixture()
    policy = policy.model_copy(update={"max_children": 2})
    with armed(tmp_path, plan=bound_policy(plan, policy), policy=policy) as (runtime, owner, clock):
        for sequence in (1, 2):
            runtime.broker.queue_submit(SubmitScenario(outcome=SubmitOutcome.REJECT))
            publish_entry(owner, clock, sequence)

            def settled(expected: int = sequence) -> bool:
                return (
                    len(runtime.broker.query_orders()) == expected
                    and all(
                        v.phase == "TERMINAL_CONFIRMED" for v in runtime.kernel.snapshot().orders
                    )
                    and runtime.kernel.critical_idle
                )

            pump_entry(owner, settled)
            clock.advance(1)
        publish_entry(owner, clock, 3)
        pump_entry(owner, lambda: dict(owner.snapshot().progress).get("000001.SZ") == "CHILD_LIMIT")
        assert len(runtime.broker.query_orders()) == 2
        assert sum(e.amount for e in runtime.kernel.entry_exposures()) == Decimal(10)


@pytest.mark.parametrize(
    "case", ["held", "no-query", "missing-trade", "legacy", "no-guard", "confirmed"]
)
def test_budget_recycles_only_complete_terminal_evidence(tmp_path: Path, case: str) -> None:
    with armed(tmp_path) as (runtime, owner, clock):
        runtime.broker.queue_submit(SubmitScenario(outcome=SubmitOutcome.REJECT))
        publish_entry(owner, clock, 1)
        pump_entry(
            owner,
            lambda: bool(runtime.kernel.snapshot().orders)
            and runtime.kernel.snapshot().orders[0].phase == "TERMINAL_CONFIRMED"
            and runtime.kernel.critical_idle,
        )
        _, _, known = runtime.kernel.exit_inputs()
        child = known[0]
        orders = runtime.broker.query_orders()
        if case == "legacy":
            assert child.entry_binding
            child = child.model_copy(
                update={
                    "entry_binding": child.entry_binding.model_copy(update={"state_hash": None})
                }
            )
        if case == "missing-trade":
            # Terminal filled quantity without its unique trades must retain full exposure.
            from auto_trading_system.domain import Quantity
            from auto_trading_system.gateways.broker.models import BrokerOrderStatus

            orders = (
                replace(
                    orders[0],
                    status=BrokerOrderStatus.FILLED,
                    filled_quantity=Quantity(100),
                    remaining_quantity=Quantity(0),
                ),
            )
        exposure = buy_exposures(
            (child,),
            () if case == "no-query" else orders,
            (),
            frozenset({child.client_order_id}) if case == "held" else frozenset(),
            state_guard_installed=case != "no-guard",
        )[0]
        assert exposure.quantity == (0 if case == "confirmed" else 100)
        assert exposure.amount == (Decimal(5) if case == "confirmed" else Decimal(1005))


@pytest.mark.parametrize(
    "case", ["state", "target", "price", "fee", "step", "allocation", "action"]
)
def test_kernel_rejects_forged_child_after_real_target(tmp_path: Path, case: str) -> None:
    with armed(tmp_path) as (runtime, owner, clock):
        publish_entry(owner, clock, 1)
        pump_entry(owner, lambda: bool(runtime.kernel.snapshot().submissions))
        _, _, known = runtime.kernel.exit_inputs()
        child = known[0]
        assert child.entry_binding
        updates: dict[str, object] = {"client_order_id": "ord_forged_child"}
        if case in ("state", "target", "allocation"):
            binding_changes: dict[str, dict[str, object]] = {
                "state": {"state_hash": digest(b"wrong")},
                "target": {"target_quantity": 110},
                "allocation": {"allocation_id": "wrong"},
            }
            updates["entry_binding"] = child.entry_binding.model_copy(update=binding_changes[case])
        elif case == "price":
            updates.update(limit_price="9.9", maximum_amount="995")
        elif case == "fee":
            updates["maximum_amount"] = "1000"
        elif case == "step":
            updates.update(quantity=99, maximum_amount="995")
        else:
            updates["target"] = child.target.model_copy(update={"action_id": "wrong"})
        with pytest.raises(ValueError):
            runtime.kernel.submit(child.model_copy(update=updates), valid_until=monotonic() + 1)


def test_state_write_is_nonblocking_and_halt_fences_late_success(tmp_path: Path) -> None:
    entered, release = Event(), Event()
    with armed(tmp_path) as (runtime, owner, clock):

        def hold(store: object) -> bool:
            entered.set()
            release.wait(2)
            return True

        blocker = runtime.journal.request(hold, deadline=monotonic() + 3)
        assert entered.wait(1)
        try:
            publish_entry(owner, clock, 1)
            started = monotonic()
            assert owner.poll().state == "PERSISTING"
            assert monotonic() - started < 0.1
            runtime.kernel.emergency_halt("TEST_HALT")
            release.set()
            blocker.result(1)
            assert owner.poll().state == "FAILED"
            assert not runtime.broker.query_orders()
        finally:
            release.set()


def test_thread_affinity_and_wrong_owner_plan(tmp_path: Path) -> None:
    with armed(tmp_path) as (runtime, owner, _):
        with ThreadPoolExecutor() as worker:
            for operation in (owner.poll, owner.snapshot, lambda: owner.gate.allows(runtime.buy())):
                with pytest.raises(RuntimeError, match="single owner"):
                    worker.submit(operation).result()
        with pytest.raises(ValueError):
            EntryStrategyOwner(runtime.kernel, owner.gate)


@pytest.mark.parametrize(
    "field",
    ["plan_hash", "policy_hash", "account_id", "allocation_id", "symbol", "target_quantity"],
)
def test_corrupt_recovered_target_fails_closed(tmp_path: Path, field: str) -> None:
    plan, policy = entry_fixture()
    target = target_fixture()
    changes: dict[str, object] = {
        "plan_hash": digest(b"other"),
        "policy_hash": digest(b"other"),
        "account_id": "other",
        "allocation_id": "other",
        "symbol": "000002.SZ",
        "target_quantity": 101,
    }
    store = APSWCriticalStore(tmp_path / "critical/journal-v2.db", plan.account_id)
    store.open()
    try:
        load_entry_targets(store, plan, policy)
        store.append_evidence(
            "recovery_checkpoints",
            target.key,
            canonical_bytes(
                target.model_copy(update={field: changes[field]}).model_dump(mode="json")
            ),
        )
    finally:
        store.close()
    clock = ExitScenarioClock(DEMO_NOW)
    with (
        pytest.raises(RuntimeError, match="ENTRY_STATE"),
        open_entry_owner(tmp_path, clock, MockBrokerGateway(clock)),
    ):
        pass


@pytest.mark.parametrize("fills", [False, True])
def test_multi_symbol_rank_and_account_active_cap(tmp_path: Path, fills: bool) -> None:
    plan, policy = entry_fixture()
    symbols = ("000001.SZ", "000002.SZ", "000003.SZ")
    policy = policy.model_copy(
        update={
            "max_active_orders": 2,
            "instruments": tuple(
                policy.instruments[0].model_copy(update={"symbol": s}) for s in symbols
            ),
        }
    )
    plan = bound_policy(
        plan.model_copy(
            update={
                "allocations": tuple(
                    plan.allocations[0].model_copy(
                        update={
                            "symbol": symbol,
                            "allocation_id": f"allocation-{n}",
                            "rank": 3 - n,
                        }
                    )
                    for n, symbol in enumerate(symbols)
                ),
                "risk_budget": plan.risk_budget.model_copy(update={"max_buy_value": "3060"}),
            }
        ),
        policy,
    )
    with armed(tmp_path, plan=plan, policy=policy) as (runtime, owner, clock):
        for symbol in symbols:
            if fills:
                runtime.broker.queue_submit(fill_scenario(100, full=True))
            publish_entry(owner, clock, 1, symbol=symbol)
        expected = 3 if fills else 2
        pump_entry(
            owner,
            lambda: len(runtime.broker.query_orders()) == expected and runtime.kernel.critical_idle,
        )
        for _ in range(10):
            owner.poll()
        orders = runtime.broker.query_orders()
        assert len(orders) == expected
        assert str(orders[0].symbol) == "000003.SZ"
        if fills:
            assert sum(lot.bought_quantity for lot in runtime.kernel.snapshot().lots) == 300
        else:
            pump_entry(owner, lambda: len(owner.snapshot().targets) == 3)
            assert len(owner.snapshot().targets) == 3
            assert (
                sum(v.phase != "TERMINAL_CONFIRMED" for v in runtime.kernel.snapshot().orders) == 2
            )


@pytest.mark.parametrize("skip", [False, True])
def test_daily_export_preserves_target_even_without_any_order(tmp_path: Path, skip: bool) -> None:
    plan, _ = entry_fixture()
    if skip:
        plan = plan.model_copy(
            update={"allocations": (plan.allocations[0].model_copy(update={"target_value": "9"}),)}
        )
    with armed(tmp_path / "runtime", plan=plan) as (runtime, owner, clock):
        publish_entry(owner, clock, 1, depth=0)
        pump_entry(owner, lambda: len(owner.snapshot().targets) == 1)
        clock.advance(20 * 60)
        runtime.kernel.start_recovery()
        wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        snapshot = runtime.kernel.capture_day()
        assert len(snapshot.entry_targets) == 1 and not snapshot.submissions
    batch, files = freeze(snapshot, tmp_path / "export")
    result = rows(batch, files, "plan_results")[0].data
    assert result["target_quantity"] == (0 if skip else 100)
    assert result["status"] == ("SKIPPED" if skip else "CANCELLED")
    if skip:
        assert result["reason_code"] == "SKIPPED_MIN_ORDER"


@pytest.mark.parametrize("case", ["duplicate", "scope", "missing-state"])
def test_daily_export_requires_matching_state_reference(tmp_path: Path, case: str) -> None:
    with armed(tmp_path / "runtime") as (runtime, owner, clock):
        runtime.broker.queue_submit(fill_scenario(100, full=True))
        publish_entry(owner, clock, 1)
        pump_entry(owner, lambda: dict(owner.snapshot().progress).get("000001.SZ") == "FINISHED")
        clock.advance(20 * 60)
        runtime.kernel.start_recovery()
        wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        snapshot = runtime.kernel.capture_day()
    if case == "missing-state":
        batch, files = freeze(replace(snapshot, entry_targets=()), tmp_path / "export")
        assert rows(batch, files, "plan_results") == ()
    else:
        targets = (
            snapshot.entry_targets * 2
            if case == "duplicate"
            else (snapshot.entry_targets[0].model_copy(update={"policy_hash": digest(b"other")}),)
        )
        with pytest.raises(ValueError, match="entry target"):
            freeze(replace(snapshot, entry_targets=targets), tmp_path / "export")


def test_liquidity_budget_is_not_reused_after_rejection(tmp_path: Path) -> None:
    with armed(tmp_path) as (runtime, owner, clock):
        runtime.broker.queue_submit(SubmitScenario(outcome=SubmitOutcome.REJECT))
        publish_entry(owner, clock, 1)
        owner.gate.quotes["000001.SZ"] = owner.gate.quotes["000001.SZ"].model_copy(
            update={"recent_volume": 3400}
        )
        pump_entry(
            owner,
            lambda: bool(runtime.kernel.snapshot().orders)
            and runtime.kernel.snapshot().orders[0].phase == "TERMINAL_CONFIRMED"
            and runtime.kernel.critical_idle,
        )
        clock.advance(1)
        publish_entry(owner, clock, 2)
        owner.gate.quotes["000001.SZ"] = owner.gate.quotes["000001.SZ"].model_copy(
            update={"recent_volume": 3400}
        )
        for _ in range(10):
            owner.poll()
        assert len(runtime.broker.query_orders()) == 1
        assert dict(owner.snapshot().progress)["000001.SZ"] == "WAITING_CAPACITY"


@pytest.mark.parametrize("case", ["expired", "disk-error"])
def test_persistence_failure_never_dispatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    entered, release = Event(), Event()
    with armed(tmp_path) as (runtime, owner, clock):
        if case == "disk-error":

            def fail(store: object, target: EntryTarget) -> bool:
                raise OSError("synthetic disk failure")

            monkeypatch.setattr(
                "auto_trading_system.strategies.entry_owner.append_entry_target", fail
            )
        else:

            def hold(store: object) -> bool:
                entered.set()
                release.wait(2)
                return True

            blocker = runtime.journal.request(hold, deadline=monotonic() + 3)
            assert entered.wait(1)
        try:
            publish_entry(owner, clock, 1)
            assert owner.poll().state == "PERSISTING"
            with pytest.raises(ValueError, match="settled"):
                owner.gate.export_targets()
            if case == "expired":
                sleep(0.27)
                assert owner.poll().state == "FAILED"
            else:
                deadline = monotonic() + 2
                while owner.poll().state != "FAILED" and monotonic() < deadline:
                    sleep(0.001)
                assert owner.snapshot().state == "FAILED"
            assert not runtime.broker.query_orders()
        finally:
            release.set()
            if case == "expired":
                blocker.result(1)


def test_cutoff_cancel_uses_no_quote_and_never_buys_after_stop(tmp_path: Path) -> None:
    plan, _ = entry_fixture()
    plan = plan.model_copy(
        update={
            "execution_window": plan.execution_window.model_copy(
                update={
                    "stop_new_buy_at": DEMO_NOW + timedelta(seconds=40),
                    "cancel_by": DEMO_NOW + timedelta(seconds=70),
                }
            )
        }
    )
    with armed(tmp_path, plan=plan) as (runtime, owner, clock):
        publish_entry(owner, clock, 1)
        pump_entry(
            owner, lambda: bool(runtime.broker.query_orders()) and runtime.kernel.critical_idle
        )
        owner.gate.quotes.clear()
        clock.advance(41)
        pump_entry(
            owner,
            lambda: runtime.kernel.snapshot().orders[0].phase == "TERMINAL_CONFIRMED"
            and runtime.kernel.critical_idle,
        )
        pump_entry(
            owner, lambda: dict(owner.snapshot().progress).get("000001.SZ") == "WINDOW_CLOSED"
        )
        assert len(runtime.broker.query_orders()) == 1


def test_new_same_price_quote_is_consumed_by_pending_child(tmp_path: Path) -> None:
    entered, release = Event(), Event()
    plan, _ = entry_fixture()
    plan = plan.model_copy(
        update={
            "allocations": (
                plan.allocations[0].model_copy(
                    update={
                        "max_qty": 200,
                        "target_value": "2000",
                        "max_value": "2020",
                    }
                ),
            ),
            "risk_budget": plan.risk_budget.model_copy(update={"max_buy_value": "2020"}),
        }
    )
    with armed(tmp_path, plan=plan) as (runtime, owner, clock):
        publish_entry(owner, clock, 1, depth=0)
        pump_entry(owner, lambda: len(owner.snapshot().targets) == 1)

        def hold(store: object) -> bool:
            entered.set()
            release.wait(2)
            return True

        blocker = runtime.journal.request(hold, deadline=monotonic() + 3)
        assert entered.wait(1)
        try:
            clock.advance(1)
            runtime.broker.queue_submit(fill_scenario(100, full=True))
            publish_entry(owner, clock, 2)
            owner.poll()
            assert runtime.kernel.snapshot().submissions[0].status == "PERSISTING"
            clock.advance(1)
            publish_entry(owner, clock, 3)
            release.set()
            blocker.result(1)
            pump_entry(
                owner, lambda: bool(runtime.kernel.snapshot().lots) and runtime.kernel.critical_idle
            )
            assert owner.gate.used_quotes["000001.SZ"][0] == 3
            for _ in range(10):
                owner.poll()
            assert len(runtime.broker.query_orders()) == 1
            clock.advance(1)
            runtime.broker.queue_submit(fill_scenario(100, full=True))
            publish_entry(owner, clock, 4)
            pump_entry(
                owner, lambda: dict(owner.snapshot().progress).get("000001.SZ") == "FINISHED"
            )
            assert len(runtime.broker.query_orders()) == 2
            assert sum(lot.bought_quantity for lot in runtime.kernel.snapshot().lots) == 200
        finally:
            release.set()
