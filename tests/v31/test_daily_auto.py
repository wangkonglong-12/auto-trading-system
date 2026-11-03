"""Recurring consent and actual engine controls, always with a hard-wired Mock broker."""

from datetime import timedelta
from pathlib import Path
from time import monotonic, sleep

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_tail_phase_execution import phase

from auto_trading_system.apps.trade_runtime.engine_simulation import (
    DEMO_NOW,
    open_offline_engine,
    wait_for,
)
from auto_trading_system.apps.trade_runtime.entry_simulation import publish_entry
from auto_trading_system.apps.trade_runtime.exit_owner_simulation import ExitScenarioClock
from auto_trading_system.apps.trade_runtime.exit_simulation import fill_scenario
from auto_trading_system.config.daily_auto import DailyAutoConsent, DailyAutoStore
from auto_trading_system.gateways.broker.mock import MockBrokerGateway
from auto_trading_system.protocol.v2.api import PlanLookup
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.plans import PlanPayload, sign_plan
from auto_trading_system.runtime.daily_auto import DailyAutoController
from auto_trading_system.runtime.engine_v2 import EngineLimits
from auto_trading_system.runtime.opening_reference_file import (
    load_opening_reference,
    save_opening_reference,
)
from auto_trading_system.strategies.entry_gate import EntryStateGate
from auto_trading_system.strategies.entry_owner import EntryStrategyOwner
from auto_trading_system.strategies.trading_settings import TradingSettings


def settings() -> TradingSettings:
    return TradingSettings.model_validate(
        {
            "buy": {
                "signal_mode": "TAIL_EXCELLENT_3_PHASES",
                "max_daily_buy_value": "10000",
                "max_symbol_buy_value": "10000",
                "max_order_value": "1005",
                "cancel_after_seconds": 20,
                "max_retries": 3,
                "max_daily_symbols": 1,
            },
            "sell": None,
            "safety": {
                "max_positions": 1,
                "max_total_position_pct": "100",
                "max_daily_loss": "1000",
                "emergency_liquidation_enabled": False,
            },
            "advanced": {
                "max_order_quantity": 100,
                "max_chase_bps": 50,
                "max_spread_bps": 30,
                "quote_max_age_ms": 2000,
            },
        }
    )


def store(root: Path, key=None, scope=None) -> DailyAutoStore:
    (root / "critical").mkdir(exist_ok=True)
    (root / "artifacts").mkdir(exist_ok=True)
    return DailyAutoStore(root, key or Ed25519PrivateKey.generate(), scope or digest(b"test-scope"))


def consent(target: DailyAutoStore) -> DailyAutoConsent:
    return DailyAutoConsent(
        consent_id="auto-test-consent",
        scope_hash=target.scope_hash,
        settings_hash=digest(canonical_bytes(settings().model_dump(mode="json"))),
        enabled=True,
        actor_id="offline-test-operator",
        confirmed_at=DEMO_NOW,
    )


def test_consent_is_default_off_signed_scoped_and_archived(tmp_path: Path) -> None:
    target = store(tmp_path)
    assert target.read() is None and not target.path.exists()
    value = consent(target)
    target.write(value)
    assert target.read() == value
    archived = target.artifacts.read(target.artifact_hash(value))
    assert archived == target.path.read_bytes()
    with pytest.raises(ValueError):
        DailyAutoStore(tmp_path, target.key, digest(b"another-account")).read()
    with pytest.raises(ValueError):
        DailyAutoStore(tmp_path, Ed25519PrivateKey.generate(), target.scope_hash).read()
    target.path.write_bytes(target.path.read_bytes().replace(b'"enabled":true', b'"enabled":false'))
    with pytest.raises(ValueError, match="signature"):
        target.read()


def test_recurring_consent_automatically_recovers_arms_and_preserves_daily_limits(
    tmp_path: Path,
) -> None:
    clock = ExitScenarioClock(DEMO_NOW.replace(hour=13, minute=0))
    broker = MockBrokerGateway(clock)
    signing = Ed25519PrivateKey.generate()
    payload, policy = phase("1440", ("000001.SZ",))
    payload = payload.model_copy(
        update={"risk_budget": payload.risk_budget.model_copy(update={"max_buy_value": "10000"})}
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
    target = store(tmp_path)
    opening_path = tmp_path / "opening-reference.json"

    def make_manager(engine):
        return DailyAutoController(
            engine,
            target,
            settings(),
            clock=lambda: clock.instant,
            save_opening=lambda reference: save_opening_reference(opening_path, reference),
        )

    def attach(rt):
        gate = EntryStateGate(
            payload,
            policy,
            clock=lambda: clock.instant,
            ns_clock=lambda: clock.nanoseconds,
            require_policy_payload_hash=False,
        )
        owner = EntryStrategyOwner(rt.kernel, gate)
        rt.kernel.install_entry_runtime(gate.allows, gate.export_targets, lambda: (policy,))
        return owner

    def pump(rt, manager, owner, predicate):
        until = monotonic() + 5
        while monotonic() < until:
            if owner is not None:
                owner.poll()
            else:
                rt.kernel.poll()
            manager.poll()
            assert manager.state not in {"FAULT", "PAUSED"}, manager.snapshot()
            if predicate():
                return
            sleep(0.001)
        raise AssertionError(manager.snapshot())

    with open_offline_engine(
        tmp_path,
        broker=broker,
        clock=lambda: clock.instant,
        plan_payload=payload,
        signing_key=signing,
        limits=limits,
        tail_phases=True,
        preload_plan=False,
        broker_opening_quantity=900,
    ) as rt:
        manager = make_manager(rt.kernel)
        assert not manager.enabled
        manager.enable("offline-test-operator")
        manager.poll()
        assert manager.reason == "WAITING_WINDOW" and rt.kernel.snapshot().halted
        assert rt.kernel.snapshot().grant_id is None
        clock.advance(101 * 60)
        manager.poll()
        assert manager.reason == "WAITING_FORMAL_PICKS" and rt.kernel.snapshot().halted
        signed = sign_plan(payload, "offline-ephemeral-key", signing)
        lookup = PlanLookup(
            account_id=payload.account_id,
            strategy_id=payload.strategy_id,
            trade_date=payload.trade_date,
            plan_id=payload.plan_id,
            state="PUBLISHED",
            state_version=1,
            etag=signed.envelope.payload_hash,
            plan=signed,
        )
        artifact = rt.kernel.plans._files.put(canonical_bytes(signed.model_dump(mode="json")))
        assert rt.kernel.apply(lookup, artifact).status == "LOADED"
        owner = attach(rt)
        pump(rt, manager, owner, lambda: manager.state == "RUNNING")
        assert rt.kernel._grant.directions == ("BUY",)
        assert rt.kernel._grant.max_buy_value == "10000"
        assert rt.kernel._grant.max_positions == 1
        broker.queue_submit(fill_scenario(100, full=True))
        publish_entry(owner, clock, 1)
        pump(
            rt,
            manager,
            owner,
            lambda: dict(owner.snapshot().progress).get("000001.SZ") == "FINISHED",
        )
        assert len(broker.query_orders()) == 1
        assert sum(lot.remaining for lot in rt.kernel.snapshot().lots) == 100
        assert rt.kernel._portfolio.unmanaged_positions == (("000001.SZ", 900),)
        assert target.read().enabled

    # A new boot uses the same signed consent, but needs entirely new recovery and grants.
    with open_offline_engine(
        tmp_path,
        broker=broker,
        clock=lambda: clock.instant,
        plan_payload=payload,
        signing_key=signing,
        limits=limits,
        tail_phases=True,
        broker_opening_quantity=900,
    ) as rt:
        manager = make_manager(rt.kernel)
        assert manager.enabled and rt.kernel.snapshot().halted
        owner = attach(rt)
        pump(rt, manager, owner, lambda: manager.state == "RUNNING")
        publish_entry(owner, clock, 2)
        for _ in range(10):
            owner.poll()
            manager.poll()
        assert len(broker.query_orders()) == 1
        clock.advance(20 * 60)
        pump(rt, manager, owner, lambda: manager._closed)
        assert load_opening_reference(opening_path).trade_date == clock.instant.date()
        assert manager.enabled and rt.kernel.snapshot().halted

    # The following day has a new broker query window and reuses only sealed custody.
    clock.advance((23 * 60 + 40) * 60)
    payload = PlanPayload.model_validate_json(
        payload.model_dump_json().replace("2026-09-07", "2026-09-08")
    )
    broker = MockBrokerGateway(clock)
    with open_offline_engine(
        tmp_path,
        broker=broker,
        clock=lambda: clock.instant,
        plan_payload=payload,
        signing_key=signing,
        limits=limits,
        tail_phases=True,
        opening_reference=load_opening_reference(opening_path),
        broker_opening_quantity=1000,
    ) as rt:
        manager = make_manager(rt.kernel)
        owner = attach(rt)
        assert manager.enabled and rt.kernel.snapshot().halted
        pump(rt, manager, owner, lambda: manager.state == "RUNNING")
        assert rt.kernel.entry_exposures() == ()
        broker.queue_submit(fill_scenario(100, full=True))
        publish_entry(owner, clock, 1)
        pump(
            rt,
            manager,
            owner,
            lambda: dict(owner.snapshot().progress).get("000001.SZ") == "FINISHED",
        )
        assert len(broker.query_orders()) == 1
        assert sum(lot.remaining for lot in rt.kernel.snapshot().lots) == 200
        assert rt.kernel._portfolio.unmanaged_positions == (("000001.SZ", 900),)
        manager.disable()
        assert not manager.enabled and not target.read().enabled
        assert not make_manager(rt.kernel).enabled


def test_check_timeout_disables_consent_and_never_retries_authority(tmp_path: Path) -> None:
    payload, _ = phase("1440", ("000001.SZ",))
    target = store(tmp_path)
    elapsed = [0.0]
    with open_offline_engine(
        tmp_path,
        plan_payload=payload,
        tail_phases=True,
        limits=EngineLimits(
            mode="LIVE",
            max_buy_value="1005",
            max_order_value="1005",
            max_positions=1,
            max_order_quantity=100,
            max_daily_symbols=1,
        ),
    ) as rt:
        manager = DailyAutoController(
            rt.kernel,
            target,
            settings(),
            clock=lambda: DEMO_NOW,
            save_opening=lambda _: None,
            elapsed=lambda: elapsed[0],
        )
        manager.enable("offline-test-operator")
        manager.poll()
        assert manager.state == "VERIFYING"
        elapsed[0] = 31
        manager.poll()
        assert not manager.enabled and manager.reason == "AUTO_CHECK_FAILED"
        for _ in range(10):
            manager.poll()
        assert rt.kernel.snapshot().grant_id is None
        assert target.read().enabled is False
        reason = target.read().disabled_detail
        assert reason == "RECOVERY_REQUESTED"
        rt.kernel.emergency_halt("ANOTHER_BOOT_PAUSED")
        restarted = DailyAutoController(
            rt.kernel,
            target,
            settings(),
            clock=lambda: DEMO_NOW,
            save_opening=lambda _: None,
        )
        assert not restarted.enabled and restarted.snapshot()["pause_reason"] == reason


def test_no_picks_day_seals_without_creating_an_execution_scope_or_order(tmp_path: Path) -> None:
    clock = ExitScenarioClock(DEMO_NOW.replace(hour=15, minute=1))
    target = store(tmp_path)
    path = tmp_path / "opening-reference.json"
    with open_offline_engine(
        tmp_path,
        clock=lambda: clock.instant,
        tail_phases=True,
        preload_plan=False,
        limits=EngineLimits(
            mode="LIVE",
            max_buy_value="10000",
            max_order_value="1005",
            max_positions=1,
            max_order_quantity=100,
        ),
    ) as rt:
        manager = DailyAutoController(
            rt.kernel,
            target,
            settings(),
            clock=lambda: clock.instant,
            save_opening=lambda reference: save_opening_reference(path, reference),
        )
        manager.enable("offline-test-operator")
        until = monotonic() + 5
        while not manager._closed and monotonic() < until:
            rt.kernel.poll()
            manager.poll()
            assert manager.state not in {"FAULT", "PAUSED"}, manager.snapshot()
            sleep(0.001)
        assert manager._closed and manager.enabled
        assert load_opening_reference(path).trade_date == clock.instant.date()
        assert not rt.kernel._execution_scopes() and rt.kernel.snapshot().grant_id is None
        assert rt.broker.query_orders() == ()


def test_emergency_stop_is_thread_safe_and_fences_an_old_enable_preview(tmp_path: Path) -> None:
    from threading import Thread

    target = store(tmp_path)
    with open_offline_engine(
        tmp_path,
        tail_phases=True,
        preload_plan=False,
        limits=EngineLimits(
            mode="LIVE",
            max_buy_value="10000",
            max_order_value="1005",
            max_positions=1,
            max_order_quantity=100,
        ),
    ) as rt:
        manager = DailyAutoController(
            rt.kernel,
            target,
            settings(),
            clock=lambda: DEMO_NOW,
            save_opening=lambda _: None,
        )
        manager.enable("offline-test-operator")
        old_epoch = rt.kernel.snapshot().permission_epoch
        worker = Thread(target=manager.request_stop)
        worker.start()
        worker.join(timeout=1)
        assert not worker.is_alive() and not manager.enabled
        assert rt.kernel.snapshot().halted
        with pytest.raises(ValueError, match="CONFIRMATION_INVALID"):
            manager.enable("offline-test-operator", expected_epoch=old_epoch)
        manager.poll()
        assert not target.read().enabled and manager.reason == "OPERATOR_DISABLED"
        manager.enable(
            "offline-test-operator", expected_epoch=rt.kernel.snapshot().permission_epoch
        )
        assert manager.enabled and rt.kernel.snapshot().grant_id is None


def test_changed_settings_or_corrupt_consent_cannot_restore_automatic_permission(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    target = store(tmp_path)
    target.write(consent(target))
    changed = settings().model_copy(
        update={"safety": settings().safety.model_copy(update={"max_positions": 2})}
    )
    manager = DailyAutoController(
        SimpleNamespace(), target, changed, clock=lambda: DEMO_NOW, save_opening=lambda _: None
    )
    assert not manager.enabled and manager.reason == "SETTINGS_CHANGED"
    target.path.write_text("{}", encoding="utf-8")
    manager = DailyAutoController(
        SimpleNamespace(), target, settings(), clock=lambda: DEMO_NOW, save_opening=lambda _: None
    )
    assert not manager.enabled and manager.reason == "CONSENT_INVALID"


def test_sealed_reference_keeps_previous_day_for_same_day_restart(tmp_path: Path) -> None:
    from auto_trading_system.reconcile.portfolio_checkpoint import OpeningReference

    path = tmp_path / "opening-reference.json"
    first = OpeningReference(trade_date=DEMO_NOW.date(), payload_hash=digest(b"day-zero"))
    second = OpeningReference(
        trade_date=DEMO_NOW.date() + timedelta(days=1), payload_hash=digest(b"day-one")
    )
    save_opening_reference(path, first)
    save_opening_reference(path, second)
    save_opening_reference(path, second)
    assert load_opening_reference(path) == second
    assert load_opening_reference(path.with_name("opening-reference.previous.json")) == first
    with pytest.raises(ValueError):
        save_opening_reference(path, first)


def test_active_market_fault_blocks_enable_without_saving_consent(tmp_path: Path) -> None:
    target = store(tmp_path)
    with open_offline_engine(
        tmp_path,
        tail_phases=True,
        preload_plan=False,
        limits=EngineLimits(
            mode="LIVE",
            max_buy_value="10000",
            max_order_value="1005",
            max_positions=1,
            max_order_quantity=100,
        ),
    ) as rt:
        manager = DailyAutoController(
            rt.kernel, target, settings(), clock=lambda: DEMO_NOW, save_opening=lambda _: None
        )
        rt.kernel.market_data_failure = "QMT_TICK_CALLBACK_INVALID"
        with pytest.raises(ValueError, match="DAILY_AUTO_MARKET_UNAVAILABLE"):
            manager.enable("offline-test-operator")
        assert target.read() is None and not manager.enabled
        rt.kernel.market_data_failure = None
        manager.enable("offline-test-operator")
        assert manager.enabled and rt.kernel.snapshot().grant_id is None


@pytest.mark.parametrize("reason", ["SETTINGS_CHANGED", "OPERATOR_DISABLED"])
def test_already_disabled_normal_stop_preserves_checked_account_and_epoch(tmp_path, reason):
    target = store(tmp_path)
    with open_offline_engine(tmp_path) as rt:
        rt.kernel.start_recovery()
        wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        manager = DailyAutoController(
            rt.kernel, target, settings(), clock=lambda: DEMO_NOW, save_opening=lambda _: None
        )
        before = rt.kernel.snapshot()
        pause = rt.kernel.permission.snapshot().reason
        for _ in range(2):
            manager.disable(reason)
            rt.kernel.poll()
            after = rt.kernel.snapshot()
            assert after.state == before.state
            assert after.permission_epoch == before.permission_epoch
            assert after.recovery.status == "CLEAR" and after.grant_id is None
            assert rt.kernel.permission.snapshot().reason == pause
        assert manager.state == "DISABLED" and manager.reason == reason
        assert rt.broker.query_orders() == ()


@pytest.mark.parametrize("mode", ["waiting", "armed", "pending_control", "existing_fault"])
def test_settings_change_still_fences_authority_and_preserves_actual_fault(tmp_path, mode):
    target = store(tmp_path)
    if mode in {"waiting", "existing_fault"}:
        target.write(consent(target))
    with open_offline_engine(tmp_path) as rt:
        rt.kernel.start_recovery()
        wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        manager = DailyAutoController(
            rt.kernel, target, settings(), clock=lambda: DEMO_NOW, save_opening=lambda _: None
        )
        if mode == "armed":
            rt.confirmed_arm()
        elif mode == "pending_control":
            report = rt.kernel.snapshot().recovery
            request = rt.request("CONFIRM_RECOVERY", artifact_hash=report.payload_hash)
            rt.approvals.approve(request, authenticated_actor="offline-test-operator")
            assert rt.kernel.control(request).status == "PENDING"
        elif mode == "existing_fault":
            rt.kernel.emergency_halt("QMT_TICK_CALLBACK_INVALID")
            rt.kernel.poll()
        before = rt.kernel.snapshot().permission_epoch
        manager.disable("SETTINGS_CHANGED")
        rt.kernel.poll()
        after = rt.kernel.snapshot()
        assert after.halted and after.permission_epoch > before and after.grant_id is None
        expected = {
            "existing_fault": "QMT_TICK_CALLBACK_INVALID",
            "pending_control": "CONTROL_STALE",
        }.get(mode, "DAILY_AUTO_SETTINGS_CHANGED")
        assert rt.kernel.permission.snapshot().reason == expected
        assert not manager.enabled
        if target.read() is not None:
            assert not target.read().enabled and target.read().disabled_detail is None
        assert rt.broker.query_orders() == ()
