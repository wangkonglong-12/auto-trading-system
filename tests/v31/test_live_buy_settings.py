"""Real engine/owner integration for live settings, without any live broker."""

from time import monotonic, sleep
from types import SimpleNamespace

from test_entry_owner import armed

from auto_trading_system.apps.trade_runtime.entry_simulation import publish_entry
from auto_trading_system.apps.trade_runtime.exit_simulation import fill_scenario
from auto_trading_system.runtime.buy_settings import BuySettingsController
from auto_trading_system.runtime.qmt_trading_runtime import QmtTradingRuntime
from auto_trading_system.strategies.trading_settings import TradingSettings
from auto_trading_system.strategies.trading_settings_drafts import TradingSettingsDraftStore


def settings(**buy):
    return TradingSettings.model_validate(
        {
            "buy": {"cancel_after_seconds": 5, "max_retries": 3, **buy},
            "safety": {
                "max_daily_loss": "1000",
                "max_positions": 2,
                "emergency_liquidation_enabled": False,
            },
            "advanced": {
                "max_order_quantity": 10000,
                "max_chase_bps": 30,
                "max_spread_bps": 20,
                "quote_max_age_ms": 1000,
            },
        }
    )


def pump(controller, owner, predicate):
    end = monotonic() + 5
    while monotonic() < end:
        owner.engine.poll()
        controller.poll()
        snapshot = owner.poll()
        assert snapshot.state != "FAILED", snapshot
        if predicate():
            return
        sleep(0.001)
    raise AssertionError((controller.snapshot(), owner.snapshot()))


def test_live_update_keeps_authority_and_partial_fill_budget(tmp_path):
    with armed(tmp_path) as (rt, owner, clock):
        base = settings(
            max_daily_buy_value="2030", max_symbol_buy_value="2030", max_order_value="1005"
        ).model_copy(update={"sell": None})
        store = TradingSettingsDraftStore(tmp_path)
        store.save(base, expected_version=0)
        market = object.__new__(QmtTradingRuntime)
        market.settings, market.phase_owners = base, {owner.gate.plan_hash: owner}
        rt.kernel.local_tail_feed = SimpleNamespace(snapshot=None)
        endpoint = SimpleNamespace(
            engine=rt.kernel, market=market, daily_auto=None, manual_buy=None, authorization=None
        )
        controller = BuySettingsController(endpoint, store)
        before = rt.kernel.permission.snapshot()
        sell_limits = rt.kernel.limits
        rt.broker.queue_submit(fill_scenario(20, full=False))
        publish_entry(owner, clock, 1)
        pump(controller, owner, lambda: bool(rt.broker.query_trades()))
        target = owner.gate.targets["000001.SZ"]
        changed = base.model_copy(
            update={
                "buy": base.buy.model_copy(
                    update={
                        "cancel_after_seconds": 1,
                        "max_daily_buy_value": "500",
                        "max_symbol_buy_value": "500",
                        "max_order_value": "500",
                    }
                )
            }
        )
        store.save(changed, expected_version=1)
        pump(controller, owner, lambda: controller.active.version == 2)
        assert not rt.kernel.snapshot().halted
        assert rt.kernel.permission.snapshot().epoch == before.epoch
        assert rt.kernel.limits == sell_limits
        assert rt.kernel.buy_limits.max_buy_value == "500"
        assert owner.gate.execution_policy.cancel_after_seconds == 1
        clock.advance(1)
        pump(
            controller,
            owner,
            lambda: rt.kernel.snapshot().orders[0].phase == "TERMINAL_CONFIRMED"
            and rt.kernel.entry_exposures()[0].quantity == 20,
        )
        assert rt.kernel.entry_exposures()[0].quantity == 20
        assert target.payload_hash in owner.history
        publish_entry(owner, clock, 2)
        pump(controller, owner, lambda: owner.gate.targets["000001.SZ"].configuration_version == 2)
        assert owner.gate.targets["000001.SZ"].revision == 1
        assert len(rt.broker.query_orders()) == 1
        # Failed save leaves the active switch, version and SELL limits intact.
        import pytest

        with pytest.raises(RuntimeError):
            store.save(base, expected_version=0)
        assert controller.active.version == 2
        assert rt.kernel.limits == sell_limits
        from auto_trading_system.strategies.entry_state import load_entry_targets

        recovered = rt.kernel.journal.request(
            lambda storage: load_entry_targets(
                storage,
                owner.gate.plan,
                owner.gate.policy,
                allocations=rt.kernel.entry_allocations(owner.gate.plan),
            ),
            deadline=monotonic() + 2,
        ).result(timeout=2)
        assert [t.revision for t in recovered] == [0, 1]
        assert recovered[0].payload_hash == target.payload_hash
        assert recovered[1].previous_hash == recovered[0].payload_hash


def test_bad_configuration_only_blocks_buy_and_recovers_without_enabling(tmp_path):
    with armed(tmp_path) as (rt, owner, _clock):
        base = settings(
            max_daily_buy_value="2030", max_symbol_buy_value="2030", max_order_value="1005"
        )
        store = TradingSettingsDraftStore(tmp_path)
        store.save(base, expected_version=0)
        market = object.__new__(QmtTradingRuntime)
        market.settings, market.phase_owners = base, {owner.gate.plan_hash: owner}
        rt.kernel.local_tail_feed = SimpleNamespace(snapshot=None)
        endpoint = SimpleNamespace(
            engine=rt.kernel, market=market, daily_auto=None, manual_buy=None, authorization=None
        )
        controller = BuySettingsController(endpoint, store)
        rt.kernel.buy_enabled = False
        epoch = rt.kernel.permission.snapshot().epoch
        original = store.path.read_bytes()
        store.path.write_bytes(b"broken")
        pump(controller, owner, lambda: bool(controller.reason))
        assert rt.kernel.journal.healthy and not rt.kernel.snapshot().halted
        assert rt.kernel.permission.snapshot().epoch == epoch
        assert not rt.broker.query_orders()
        store.path.write_bytes(original)
        pump(controller, owner, lambda: controller.reason is None)
        assert not rt.kernel.buy_enabled
        assert rt.kernel.buy_settings_error is None


def test_saved_buy_limits_do_not_interrupt_authorized_sell(tmp_path):
    from test_board_sell_engines import board_context

    from auto_trading_system.apps.trade_runtime.engine_simulation import (
        open_offline_engine,
        wait_for,
    )
    from auto_trading_system.apps.trade_runtime.exit_owner_simulation import (
        ExitScenarioClock,
        fixture_context,
        publish,
    )
    from auto_trading_system.apps.trade_runtime.exit_owner_simulation import (
        pump as sell_pump,
    )
    from auto_trading_system.apps.trade_runtime.exit_simulation import close_buy_day, next_day_plan
    from auto_trading_system.runtime.engine_v2 import EngineLimits
    from auto_trading_system.strategies.board_sell_engine import BoardSellEngine
    from auto_trading_system.strategies.exit_owner import ExitStateGate

    opening = close_buy_day(tmp_path)
    clock = ExitScenarioClock()
    context = board_context(fixture_context(tmp_path, opening), at="09:30")
    with open_offline_engine(
        tmp_path,
        clock=lambda: clock.instant,
        plan_payload=next_day_plan(),
        preload_plan=False,
        opening_reference=opening,
        broker_opening_quantity=100,
        limits=EngineLimits(
            max_buy_value="0", max_order_value="1005", max_positions=1, max_order_quantity=100
        ),
    ) as rt:
        rt.kernel.start_recovery()
        wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        gate = ExitStateGate(
            context, clock=lambda: clock.instant, ns_clock=lambda: clock.nanoseconds
        )
        rt.kernel.install_board_exit(context, lambda item: owner.allows(item))
        owner = BoardSellEngine(rt.kernel, gate)
        sell_pump(owner, lambda: owner.snapshot().state == "READY")
        owner.set_enabled(True)
        rt.confirmed_arm(directions=("SELL",))
        rt.kernel.retain_board_sell_grant(context.payload_hash)
        sell_grants = dict(rt.kernel._board_exit_grants)
        epoch = rt.kernel.permission.snapshot().epoch
        base = settings(
            max_daily_buy_value="2030", max_symbol_buy_value="2030", max_order_value="1005"
        )
        store = TradingSettingsDraftStore(tmp_path)
        store.save(base, expected_version=0)
        market = object.__new__(QmtTradingRuntime)
        market.settings, market.phase_owners = base, {}
        rt.kernel.local_tail_feed = SimpleNamespace(snapshot=None)
        endpoint = SimpleNamespace(
            engine=rt.kernel, market=market, daily_auto=None, manual_buy=None, authorization=None
        )
        controller = BuySettingsController(endpoint, store)
        controller._apply(
            store.save(
                base.model_copy(
                    update={"buy": base.buy.model_copy(update={"max_order_value": "500"})}
                ),
                expected_version=1,
            )
        )
        assert rt.kernel._board_exit_grants == sell_grants
        assert rt.kernel.permission.snapshot().epoch == epoch
        assert owner.enabled and not rt.kernel.snapshot().halted
        rt.broker.queue_submit(fill_scenario(100, full=True))
        publish(owner, clock, "10", 1)
        sell_pump(owner, lambda: rt.kernel.snapshot().lots[0].remaining == 0)
        assert len(rt.broker.query_trades()) == 1


def test_archived_settings_restore_original_source_and_recover_filtered_candidates(tmp_path):
    import json

    from test_tail_feed import NOW, local_feed, wire

    from auto_trading_system.runtime.engine_v2 import EngineKernel
    from auto_trading_system.runtime.tail_feed import LocalTailFeed

    feed = local_feed(tmp_path)
    value = wire()
    value["batches"][0]["stocks"] = [{"code": "300001.SZ", "name": "测试股票", "rank": 1}]
    feed.accept(json.dumps(value).encode(), NOW)
    original_plan = feed.plans.plans.accepted()[0].payload
    assert not original_plan.allocations
    store = TradingSettingsDraftStore(tmp_path)
    store.save(feed.settings, expected_version=0)
    changed = feed.settings.model_copy(
        update={
            "buy": feed.settings.buy.model_copy(
                update={"allowed_boards": ("CHINEXT",), "max_daily_buy_value": "30000"}
            )
        }
    )
    store.save(changed, expected_version=1)
    restored = LocalTailFeed(feed.config, changed, local_feed(tmp_path).plans, feed.key)
    restored.restore(NOW)
    assert restored.complete and restored.scope == feed.scope
    engine = object.__new__(EngineKernel)
    engine.buy_settings, engine.local_tail_feed = changed, restored
    allocations = engine.entry_allocations(original_plan)
    assert [a.symbol for a in allocations] == ["300001.SZ"]
    assert original_plan.allocations == ()
