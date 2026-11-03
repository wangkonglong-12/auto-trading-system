"""Debug source immutability and one-target consent, without live SDK access."""

import json
from datetime import timedelta
from time import monotonic, sleep
from types import SimpleNamespace

import pytest
from cryptography.exceptions import InvalidSignature
from test_daily_auto import settings, store
from test_tail_feed import NOW, local_feed, wire
from test_tail_phase_execution import phase

from auto_trading_system.apps.trade_runtime.engine_simulation import DEMO_NOW, open_offline_engine
from auto_trading_system.apps.trade_runtime.entry_simulation import publish_entry
from auto_trading_system.apps.trade_runtime.exit_owner_simulation import ExitScenarioClock
from auto_trading_system.control.ipc_protocol import IPCError
from auto_trading_system.gateways.broker.mock import MockBrokerGateway
from auto_trading_system.runtime.daily_auto import DailyAutoController
from auto_trading_system.runtime.debug_pick import DebugPickConfirm, DebugPickController, Preview
from auto_trading_system.runtime.engine_v2 import EngineLimits
from auto_trading_system.strategies.entry_gate import EntryStateGate
from auto_trading_system.strategies.entry_owner import EntryStrategyOwner


def test_debug_source_survives_restart_without_overwriting_empty_formal_batches(tmp_path):
    feed = local_feed(tmp_path)
    feed.accept(json.dumps(wire()).encode(), NOW)
    original = feed.path.read_bytes()
    feed.start_debug("000001.SZ", "平安银行", "operator-test", NOW)
    plan = feed.plans.plans.loaded()
    assert len(plan.payload.allocations) == 1
    assert plan.payload.source.algorithm_version == "local-debug-pick-v1"
    assert feed.path.read_bytes() == original
    assert feed.allows_buy(NOW + timedelta(seconds=40))
    assert not feed.allows_buy(NOW.replace(hour=14, minute=55))
    feed.accept(b"invalid remote data", NOW)
    assert feed.plans.plans.accepted() == (plan,)
    restored = local_feed(tmp_path)
    restored.restore(NOW)
    assert restored.plans.plans.accepted() == (plan,)
    with pytest.raises(ValueError, match="ALREADY_USED"):
        restored.start_debug("600000.SH", "浦发银行", "operator-test", NOW)


def test_debug_persistence_failure_never_installs_source(tmp_path, monkeypatch):
    feed = local_feed(tmp_path)

    def failed(*args):
        raise OSError("disk full")

    monkeypatch.setattr("auto_trading_system.runtime.tail_feed.write_private", failed)
    with pytest.raises(OSError):
        feed.start_debug("000001.SZ", "平安银行", "operator-test", NOW)
    assert feed.debug_plan is None and not feed.plans.plans.accepted()


def test_debug_restore_rejects_tampered_plan(tmp_path):
    feed = local_feed(tmp_path)
    feed.start_debug("000001.SZ", "平安银行", "operator-test", NOW)
    body = feed.debug_path.read_bytes().replace(b"000001.SZ", b"600000.SH")
    feed.debug_path.write_bytes(body)
    with pytest.raises((ValueError, InvalidSignature)):
        local_feed(tmp_path).restore(NOW)


@pytest.mark.parametrize("change", ["session", "phrase", "epoch", "expiry"])
def test_confirmation_is_bound_and_cannot_start_after_fence(change):
    calls = []
    engine = SimpleNamespace(permission=SimpleNamespace(snapshot=lambda: SimpleNamespace(epoch=3)))
    controller = DebugPickController(engine, None, None)
    item = Preview("session-a", "000001.SZ", "平安银行", "confirm", 3, monotonic() + 30)
    controller.previews["debug-" + "a" * 32] = item
    controller.check = lambda: calls.append("checked")
    session = SimpleNamespace(session_key="session-b" if change == "session" else "session-a")
    if change == "epoch":
        item.epoch = 2
    if change == "expiry":
        item.expires = 0
    with pytest.raises(IPCError, match="CONFIRMATION_INVALID"):
        controller.confirm(
            session,
            DebugPickConfirm(
                preview_id="debug-" + "a" * 32, phrase="wrong" if change == "phrase" else "confirm"
            ),
        )
    assert not calls and item.result is None


def test_one_shot_uses_real_automatic_owner_and_never_saves_continuous_switch(tmp_path):
    clock = ExitScenarioClock(DEMO_NOW)
    broker = MockBrokerGateway(clock)
    payload, policy = phase("1440", ("000001.SZ",))
    payload = payload.model_copy(
        update={"risk_budget": payload.risk_budget.model_copy(update={"max_buy_value": "10000"})}
    )
    target = store(tmp_path)
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
        plan_payload=payload,
        limits=limits,
        tail_phases=True,
        broker_opening_quantity=900,
    ) as rt:
        manager = DailyAutoController(
            rt.kernel,
            target,
            settings(),
            clock=lambda: clock.instant,
            save_opening=lambda value: None,
        )
        manager.start_debug_once(
            "offline-test-operator", expected_epoch=rt.kernel.permission.snapshot().epoch
        )
        gate = EntryStateGate(
            payload,
            policy,
            clock=lambda: clock.instant,
            ns_clock=lambda: clock.nanoseconds,
            require_policy_payload_hash=False,
        )
        owner = EntryStrategyOwner(rt.kernel, gate)
        rt.kernel.install_entry_runtime(gate.allows, gate.export_targets, lambda: (policy,))
        deadline = monotonic() + 5
        while manager.state != "RUNNING" and monotonic() < deadline:
            rt.kernel.poll()
            manager.poll()
            sleep(0.001)
        assert manager.state == "RUNNING", manager.snapshot()
        publish_entry(owner, clock, 2)
        for _ in range(100):
            owner.poll()
            manager.poll()
            sleep(0.001)
            if broker.query_orders():
                break
        assert len(broker.query_orders()) == 1
        assert not target.path.exists()
        restarted = DailyAutoController(
            rt.kernel,
            target,
            settings(),
            clock=lambda: clock.instant,
            save_opening=lambda value: None,
        )
        assert not restarted.enabled


def test_confirmation_is_one_shot_and_cached_after_lost_response():
    calls = []
    feed = SimpleNamespace(start_debug=lambda *args: calls.append("source") or "plan-debug")
    engine = SimpleNamespace(
        local_tail_feed=feed,
        _now=lambda: NOW,
        permission=SimpleNamespace(snapshot=lambda: SimpleNamespace(epoch=3)),
        emergency_halt=lambda reason: calls.append(reason),
    )
    automatic = SimpleNamespace(start_debug_once=lambda actor, **kw: calls.append("consent"))
    controller = DebugPickController(engine, automatic, None)
    controller.check = lambda: None
    controller.previews["debug-" + "a" * 32] = Preview(
        "s", "000001.SZ", "平安银行", "confirm", 3, monotonic() + 30
    )
    session = SimpleNamespace(session_key="s", actor_id="operator-test")
    value = DebugPickConfirm(preview_id="debug-" + "a" * 32, phrase="confirm")
    first = controller.confirm(session, value)
    assert first["status"] == "STARTED"
    assert controller.confirm(session, value) == first
    assert calls == ["source", "consent"]


def test_uncertain_source_write_consumes_confirmation_and_halts():
    calls = []

    def fail(*args):
        raise OSError("uncertain disk write")

    engine = SimpleNamespace(
        local_tail_feed=SimpleNamespace(start_debug=fail),
        _now=lambda: NOW,
        permission=SimpleNamespace(snapshot=lambda: SimpleNamespace(epoch=3)),
        emergency_halt=lambda reason: calls.append(reason),
    )
    controller = DebugPickController(
        engine, SimpleNamespace(start_debug_once=lambda *a: calls.append("BAD")), None
    )
    controller.check = lambda: None
    controller.previews["debug-" + "a" * 32] = Preview(
        "s", "000001.SZ", "平安银行", "confirm", 3, monotonic() + 30
    )
    session = SimpleNamespace(session_key="s", actor_id="operator-test")
    value = DebugPickConfirm(preview_id="debug-" + "a" * 32, phrase="confirm")
    with pytest.raises(OSError):
        controller.confirm(session, value)
    assert controller.confirm(session, value) == {"status": "CHECK_REQUIRED"}
    assert calls == ["DEBUG_PICK_START_FAILED"]


def test_debug_list_and_process_log_preserve_source_and_stock_name(tmp_path):
    from test_buy_dashboard import sample
    from test_trading_process import log

    from auto_trading_system.apps.local_console.buy_dashboard import buy_dashboard

    feed = local_feed(tmp_path)
    feed.start_debug("000001.SZ", "平安银行", "operator-test", NOW)
    details, engine = sample()
    engine["plan"]["source"] = "DEBUG"
    engine["plan"]["symbols"] = ["000001.SZ"]
    result = buy_dashboard(details, engine, maximum=1)
    row = next(row for row in result["rows"] if row["symbol"] == "000001.SZ")
    assert row["source"] == "DEBUG" and row["quantity"] == 300
    collector = log(tmp_path)
    collector.collect()
    events = collector.snapshot()["events"]
    event = next(event for event in events if "调试选股已接收" in event["message"])
    assert event["name"] == "平安银行" and event["symbol"] == "000001.SZ"


def test_public_status_keeps_debug_provenance(tmp_path):
    from auto_trading_system.apps.local_console.node_status import project_status

    feed = local_feed(tmp_path)
    config = feed.config
    projected = project_status(
        "engine",
        {
            "component": "engine",
            "node_id": config.node_id,
            "account_id": config.account_id,
            "node_generation": config.node_generation,
            "broker": config.broker,
            "real_trading_enabled": True,
            "owner_sequence": 1,
            "boot_id": "test-boot",
            "daily_auto": {"supported": True, "mode": "DEBUG_ONCE", "enabled": True},
            "plan": {"plan_id": "debug-pick-test", "source": "DEBUG", "symbols": ["000001.SZ"]},
        },
        config,
    )
    assert projected["plan"]["source"] == "DEBUG"
    assert projected["daily_auto"]["mode"] == "DEBUG_ONCE"


@pytest.mark.parametrize(
    "blocked", ["window", "continuous", "settings", "formal", "order", "target", "already"]
)
def test_debug_entry_checks_prevent_conflicting_execution(blocked):
    feed = SimpleNamespace(debug_plan=None)
    engine = SimpleNamespace(
        local_tail_feed=feed,
        _now=lambda: NOW,
        dispatcher=SimpleNamespace(idle=True),
        critical_idle=True,
        plans=SimpleNamespace(plans=SimpleNamespace(accepted=lambda: ())),
        _broker_snapshot=SimpleNamespace(orders=()),
        _known={},
        check_manual_ready=lambda: None,
    )
    automatic = SimpleNamespace(enabled=False, consent=None, check_enable=lambda: None)
    manual = SimpleNamespace(settings_current=lambda: True)
    expected = {
        "window": "OUTSIDE_WINDOW",
        "continuous": "DISABLE_AUTO_FIRST",
        "settings": "SETTINGS_CHANGED",
        "already": "ALREADY_USED",
    }.get(blocked, "FORMAL_CONFLICT")
    if blocked == "window":
        engine._now = lambda: NOW.replace(hour=14, minute=55)
    if blocked == "continuous":
        automatic.enabled = True
    if blocked == "settings":
        manual.settings_current = lambda: False
    if blocked == "formal":
        engine.plans.plans.accepted = lambda: (
            SimpleNamespace(payload=SimpleNamespace(allocations=(1,))),
        )
    if blocked == "order":
        engine._broker_snapshot.orders = (SimpleNamespace(is_terminal=False),)
    if blocked == "target":
        engine._known = {"a": SimpleNamespace(action_type="NEW", context_type="ENTRY_PLAN")}
    if blocked == "already":
        feed.debug_plan = object()
    with pytest.raises(IPCError, match=expected):
        DebugPickController(engine, automatic, manual).check()
