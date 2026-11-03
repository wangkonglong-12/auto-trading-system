"""Board isolation, configurable decisions and the real shared Mock order path."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from time import monotonic, sleep

import pytest

from auto_trading_system.apps.trade_runtime.engine_simulation import open_offline_engine, wait_for
from auto_trading_system.apps.trade_runtime.exit_owner_simulation import (
    ExitScenarioClock,
    fixture_context,
    publish,
    pump,
)
from auto_trading_system.apps.trade_runtime.exit_simulation import (
    D1_NOW,
    close_buy_day,
    fill_scenario,
    next_day_plan,
)
from auto_trading_system.gateways.broker.mock import MockBrokerGateway
from auto_trading_system.runtime.board_sell import BoardSellController
from auto_trading_system.runtime.engine_v2 import EngineLimits
from auto_trading_system.strategies.board_sell_configuration import (
    BoardSellConfiguration,
    BoardSellConfigurationStore,
    BoardSellRule,
)
from auto_trading_system.strategies.board_sell_engine import BoardSellEngine, RuleResult
from auto_trading_system.strategies.entry_state import EntryInstrument
from auto_trading_system.strategies.exit_context import ExitSessionContext
from auto_trading_system.strategies.exit_owner import ExitQuote, ExitStateGate
from auto_trading_system.strategies.exit_v3 import ExitState


def board_context(baseline, *, mode="TIME_ONLY", profit=None, at="10:17"):
    return ExitSessionContext.model_validate_json(
        baseline.model_copy(
            update={
                "board": "MAIN",
                "rule_id": "BOARD_MAIN_EXIT_V1",
                "configuration_version": 1,
                "board_rule": BoardSellRule(mode=mode, take_profit_pct=profit, sell_at=at),
            }
        ).model_dump_json()
    )


def instrument(symbol, price):
    return EntryInstrument(
        symbol=symbol,
        reference_price=price,
        price_tick="0.01",
        lower_price="8",
        upper_price="12",
        min_quantity=100,
        quantity_step=100,
        max_order_quantity=100,
        source_hash="sha256:" + "a" * 64,
    ), "测试股票"


def settle_controller(controller, predicate):
    deadline = monotonic() + 3
    while monotonic() < deadline:
        controller.engine.poll()
        controller.poll()
        if predicate():
            return
        sleep(0.001)
    raise AssertionError(controller.status())


def test_prepare_uses_saved_configuration_and_explicit_sources(tmp_path):
    opening = close_buy_day(tmp_path)
    clock = ExitScenarioClock()
    store = BoardSellConfigurationStore(tmp_path)
    store.save(
        BoardSellConfiguration(
            main=BoardSellRule(mode="TIME_ONLY", sell_at="10:17"), cancel_after_seconds=13
        ),
        0,
    )
    with open_offline_engine(
        tmp_path,
        clock=lambda clock=clock: clock.instant,
        plan_payload=next_day_plan(),
        preload_plan=False,
        opening_reference=opening,
        broker_opening_quantity=100,
    ) as runtime:
        runtime.kernel.start_recovery()
        wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        manager = BoardSellController(
            runtime.kernel,
            store,
            instrument,
            lambda clock=clock: clock.instant,
            runtime.plan.payload.strategy_id,
        )
        settle_controller(manager, lambda: manager.status()["restored"])
        context = manager.prepare("MAIN").result(timeout=2)
        assert context.board_rule.sell_at == "10:17"
        assert context.cancel_after_seconds == 13
        assert context.configuration_version == 1
        manager.accept_prepared(context)
        settle_controller(manager, lambda: manager.owners["MAIN"].snapshot().state == "READY")
        assert not manager.owners["MAIN"].enabled
        assert runtime.broker.query_orders() == ()
        assert runtime.kernel._scan.lot_sources[context.lots[0].lot_id] == "AUTOMATIC"
        store.save(BoardSellConfiguration(include_automatic=False), 1)
        assert manager.owners["MAIN"].context == context
        with pytest.raises(ValueError, match="NO_ELIGIBLE_LOTS"):
            manager.prepare("CHINEXT")


def test_configured_time_latches_without_quote_then_sells_once(tmp_path):
    opening = close_buy_day(tmp_path)
    context = board_context(fixture_context(tmp_path, opening), at="10:17")
    clock = ExitScenarioClock(D1_NOW.replace(hour=10, minute=17))
    broker = MockBrokerGateway(clock)
    with open_offline_engine(
        tmp_path,
        broker=broker,
        clock=lambda clock=clock: clock.instant,
        plan_payload=next_day_plan(),
        preload_plan=False,
        opening_reference=opening,
        broker_opening_quantity=100,
        limits=EngineLimits(
            max_buy_value="0", max_order_value="1005", max_positions=1, max_order_quantity=100
        ),
    ) as runtime:
        runtime.kernel.start_recovery()
        wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        gate = ExitStateGate(
            context, clock=lambda clock=clock: clock.instant, ns_clock=lambda: clock.nanoseconds
        )
        runtime.kernel.install_board_exit(
            context,
            lambda item, owner_ref=lambda: owner, gate=gate: owner_ref().enabled
            and gate.allows(item),
        )
        owner = BoardSellEngine(runtime.kernel, gate)
        pump(owner, lambda owner=owner: owner.snapshot().state == "READY")
        owner.set_enabled(True)
        runtime.confirmed_arm(directions=("SELL",))
        pump(
            owner,
            lambda: bool(gate.latest)
            and gate.latest[context.lots[0].lot_id].state.target_remaining == 0,
        )
        assert not broker.query_orders()
        assert gate.latest[context.lots[0].lot_id].state.fired_rules == ("SELL_BY_TIME",)
        broker.queue_submit(fill_scenario(100, full=True))
        publish(owner, clock, "10", 1)
        pump(owner, lambda: runtime.kernel.snapshot().lots[0].remaining == 0)
        assert len(broker.query_orders()) == 1
        assert runtime.kernel.exit_inputs()[2][0].target.rule_id == "BOARD_MAIN_EXIT_V1"
        owner.poll()
        assert len(broker.query_orders()) == 1


def test_saved_settings_update_running_owner_and_survive_restart(tmp_path):
    opening = close_buy_day(tmp_path)
    clock = ExitScenarioClock(D1_NOW.replace(hour=10, minute=18))
    store = BoardSellConfigurationStore(tmp_path)
    store.save(BoardSellConfiguration(main=BoardSellRule(mode="TIME_ONLY", sell_at="14:40")), 0)
    for boot in range(2):
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
            manager = BoardSellController(
                rt.kernel, store, instrument, lambda: clock.instant, rt.plan.payload.strategy_id
            )
            settle_controller(manager, lambda manager=manager: manager._restored)
            if not boot:
                manager.accept_prepared(manager.prepare("MAIN").result(timeout=2))
            owner = manager.owners["MAIN"]
            settle_controller(
                manager,
                lambda owner=owner: owner.configuration_ready and owner.snapshot().state == "READY",
            )
            if boot:
                assert not owner.enabled
                assert owner.configuration.version == 3
                assert owner.rule_for(owner.context.lots[0]).sell_at == "14:40"
                assert owner.gate.latest[owner.context.lots[0].lot_id].state.target_remaining == 0
                assert not owner.includes(owner.context.lots[0].lot_id)
                continue
            original_context = owner.context
            owner.set_enabled(True)
            rt.confirmed_arm(directions=("SELL",))
            settle_controller(manager, lambda owner=owner: bool(owner.gate.latest))
            assert owner.gate.latest[owner.context.lots[0].lot_id].state.target_remaining == 100
            store.save(
                BoardSellConfiguration(
                    main=BoardSellRule(mode="TIME_ONLY", sell_at="10:17"),
                    cancel_after_seconds=2,
                    max_reposts=8,
                ),
                1,
            )
            settle_controller(
                manager,
                lambda owner=owner: owner.configuration.version == 2
                and owner.gate.latest[owner.context.lots[0].lot_id].state.target_remaining == 0,
            )
            assert owner.enabled and owner.context == original_context
            assert owner.cancel_after_seconds == 2
            assert manager.status()["boards"]["MAIN"]["configuration_version"] == 2
            assert (
                owner.gate.latest[owner.context.lots[0].lot_id].state.rule_states[
                    "BOARD_CONFIGURATION"
                ]["version"]
                == 2
            )
            # Raising the threshold/time does not erase an already durable exit target.
            store.save(
                BoardSellConfiguration(
                    include_automatic=False, main=BoardSellRule(mode="TIME_ONLY", sell_at="14:40")
                ),
                2,
            )
            settle_controller(manager, lambda owner=owner: owner.configuration.version == 3)
            assert owner.gate.latest[owner.context.lots[0].lot_id].state.target_remaining == 0
            assert (
                manager.status()["boards"]["MAIN"]["targets"][0]["waiting_reason"]
                == "SOURCE_EXCLUDED"
            )
            # Corrupt saved settings fence execution until a valid atomic configuration returns.
            body = store.path.read_bytes()
            store.path.write_bytes(b"invalid-json")
            settle_controller(manager, lambda owner=owner: not owner.configuration_ready)
            assert (
                manager.status()["boards"]["MAIN"]["state"]
                == "BOARD_SELL_CONFIGURATION_UNAVAILABLE"
            )
            store.path.write_bytes(body)
            settle_controller(manager, lambda owner=owner: owner.configuration_ready)
            assert owner.enabled
            assert not rt.broker.query_orders()


def test_two_board_rules_are_independent_and_extensible(tmp_path):
    opening = close_buy_day(tmp_path)
    baseline = fixture_context(tmp_path, opening)
    main_context = board_context(baseline, mode="TAKE_PROFIT_OR_TIME", profit="2.50", at="10:17")
    chinext_context = main_context.model_copy(
        update={
            "board": "CHINEXT",
            "rule_id": "BOARD_CHINEXT_EXIT_V1",
            "board_rule": BoardSellRule(mode="TIME_ONLY", sell_at="10:43"),
            "lots": (
                main_context.lots[0].model_copy(
                    update={"lot_id": "growth-lot", "symbol": "300001.SZ"}
                ),
            ),
        }
    )
    clock = ExitScenarioClock(D1_NOW.replace(hour=10, minute=18))
    # Exercise the independent engines' pure plugin boundary with identical observations.
    main = object.__new__(BoardSellEngine)
    growth = object.__new__(BoardSellEngine)
    from auto_trading_system.strategies.board_sell_engine import TakeProfit, TimeExit

    for owner, context in ((main, main_context), (growth, chinext_context)):
        owner.context = context
        owner.gate = ExitStateGate(context, clock=lambda clock=clock: clock.instant)
        owner.rules = (TimeExit(), TakeProfit())
    with open_offline_engine(
        tmp_path,
        clock=lambda clock=clock: clock.instant,
        plan_payload=next_day_plan(),
        preload_plan=False,
        opening_reference=opening,
        broker_opening_quantity=100,
    ) as runtime:
        runtime.kernel.start_recovery()
        wait_for(runtime.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        lot = runtime.kernel.snapshot().lots[0]
        state = ExitState(100)
        assert main.evaluate_lot(lot, main_context.lots[0], state, None).target_remaining == 0
        assert (
            growth.evaluate_lot(lot, chinext_context.lots[0], state, None).target_remaining == 100
        )
        live = BoardSellConfigurationStore(tmp_path).save(
            BoardSellConfiguration(
                main=BoardSellRule(mode="TIME_ONLY", sell_at="14:40"),
                chinext=BoardSellRule(
                    mode="TAKE_PROFIT_OR_TIME", take_profit_pct="2.50", sell_at="14:40"
                ),
            ),
            0,
        )
        main.configuration = growth.configuration = live
        quote = ExitQuote(
            symbol="300001.SZ",
            sequence=1,
            observed_at=clock.instant,
            received_ns=clock.nanoseconds,
            last_price="11",
            bid_price="10",
            tradable=True,
        )
        growth.gate.ns_clock = lambda: clock.nanoseconds
        assert main.evaluate_lot(lot, main_context.lots[0], state, None).target_remaining == 100
        assert growth.evaluate_lot(lot, chinext_context.lots[0], state, quote).target_remaining == 0

        class HalfRule:
            def evaluate(self, value):
                return RuleResult("HALF_EXIT_V1", value.lot.bought_quantity // 2)

        growth.rules = (HalfRule(),)
        partial = growth.evaluate_lot(lot, chinext_context.lots[0], state, None)
        assert partial.target_remaining == 50
        assert growth.evaluate_lot(lot, chinext_context.lots[0], partial, None) == partial
        assert (
            growth.evaluate_lot(
                lot, chinext_context.lots[0], replace(partial, target_remaining=0), None
            ).target_remaining
            == 0
        )


def test_context_rejects_foreign_board_and_incomplete_policy(tmp_path):
    opening = close_buy_day(tmp_path)
    context = board_context(fixture_context(tmp_path, opening))
    with pytest.raises(ValueError, match="foreign holdings"):
        ExitSessionContext.model_validate_json(
            context.model_copy(
                update={"board": "CHINEXT", "rule_id": "BOARD_CHINEXT_EXIT_V1"}
            ).model_dump_json()
        )
    with pytest.raises(ValueError, match="complete"):
        ExitSessionContext.model_validate_json(
            context.model_copy(update={"board_rule": None}).model_dump_json()
        )


def close_dual_buy_day(directory):
    from auto_trading_system.apps.trade_runtime.engine_simulation import DEMO_NOW
    from auto_trading_system.apps.trade_runtime.exit_simulation import settle
    from auto_trading_system.testing.v31_scenarios import example_plan

    clock = ExitScenarioClock(DEMO_NOW)
    plan = example_plan()
    second = plan.allocations[0].model_copy(
        update={"symbol": "300001.SZ", "rank": 2, "allocation_id": "allocation-growth"}
    )
    plan = plan.model_copy(
        update={
            "allocations": (*plan.allocations, second),
            "risk_budget": plan.risk_budget.model_copy(
                update={"max_buy_value": "2010", "max_positions": 2}
            ),
        }
    )
    limits = EngineLimits(
        max_buy_value="2010",
        max_order_value="1005",
        max_positions=2,
        max_order_quantity=100,
        allowed_boards=("MAIN", "CHINEXT"),
    )
    with open_offline_engine(
        directory, clock=lambda clock=clock: clock.instant, plan_payload=plan, limits=limits
    ) as rt:
        rt.kernel._risk_guard = lambda item: True
        rt.kernel.start_recovery()
        wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        rt.confirmed_arm()
        for symbol in ("000001.SZ", "300001.SZ"):
            rt.broker.queue_submit(fill_scenario(100, full=True))
            settle(rt, rt.buy().model_copy(update={"symbol": symbol}))
        clock.instant = DEMO_NOW.replace(hour=15, minute=1)
        rt.kernel.start_recovery()
        wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        return rt.kernel.seal_portfolio().result(timeout=2)


@pytest.mark.parametrize("fail_main", [False, True])
@pytest.mark.parametrize("login_start", [False, True])
def test_both_boards_use_shared_broker_and_pausing_one_keeps_other_running(
    tmp_path, fail_main, login_start
):
    from decimal import Decimal
    from uuid import uuid4

    from auto_trading_system.runtime.recovery_v2 import AccountFact, PositionFact

    opening = close_dual_buy_day(tmp_path)
    clock = ExitScenarioClock(D1_NOW.replace(hour=10, minute=20))
    store = BoardSellConfigurationStore(tmp_path)
    store.save(
        BoardSellConfiguration(
            main=BoardSellRule(mode="TIME_ONLY", sell_at="10:17"),
            chinext=BoardSellRule(mode="TIME_ONLY", sell_at="10:18"),
        ),
        0,
    )
    broker = MockBrokerGateway(clock)
    with open_offline_engine(
        tmp_path,
        clock=lambda clock=clock: clock.instant,
        broker=broker,
        plan_payload=next_day_plan(),
        preload_plan=False,
        opening_reference=opening,
        limits=EngineLimits(
            max_buy_value="0", max_order_value="1005", max_positions=2, max_order_quantity=100
        ),
    ) as rt:

        def account():
            orders, trades = broker.query_orders(), broker.query_trades()
            positions = []
            for symbol in ("000001.SZ", "300001.SZ"):
                remaining = 100 - sum(t.quantity.value for t in trades if str(t.symbol) == symbol)
                frozen = sum(
                    o.remaining_quantity.value
                    for o in orders
                    if str(o.symbol) == symbol and not o.is_terminal
                )
                if remaining:
                    positions.append(
                        PositionFact(
                            symbol=symbol,
                            quantity=remaining,
                            frozen=frozen,
                            sellable=remaining - frozen,
                            market_value=str(remaining * 10),
                            cost_amount=str(remaining * 10),
                        )
                    )
            cash = Decimal(10000) + sum((t.notional.amount for t in trades), Decimal(0))
            market = sum(Decimal(p.market_value) for p in positions)
            return AccountFact(
                account_id=rt.kernel.account_id,
                snapshot_id="mock-" + uuid4().hex,
                observed_at=clock.instant,
                cash=str(cash),
                cash_frozen="0",
                market_value=str(market),
                total_equity=str(cash + market),
                positions=tuple(positions),
                observed_order_ids=tuple(o.client_order_id for o in orders),
                coherent=True,
            )

        rt.kernel._account_reader = account
        rt.kernel._risk_guard = lambda item: True
        rt.kernel.start_recovery()
        wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        manager = BoardSellController(
            rt.kernel,
            store,
            instrument,
            lambda clock=clock: clock.instant,
            rt.plan.payload.strategy_id,
        )
        settle_controller(manager, lambda manager=manager: manager._restored)
        if login_start:
            manager.start_authenticated(authenticated_session)
            settle_controller(manager, lambda: not manager._starts)
            assert all(
                b["startup_status"] == "STARTED" for b in manager.status()["boards"].values()
            )
        else:
            for board in ("MAIN", "CHINEXT"):
                context = manager.prepare(board).result(timeout=2)
                manager.accept_prepared(context)
            settle_controller(
                manager, lambda: all(o.snapshot().state == "READY" for o in manager.owners.values())
            )
        assert set(rt.kernel.board_exit_contexts) == {"MAIN", "CHINEXT"}
        for owner in manager.owners.values():
            owner.gate.ns_clock = lambda: clock.nanoseconds
            if not login_start:
                owner.set_enabled(True)
            owner.quote(
                ExitQuote(
                    symbol=owner.context.lots[0].symbol,
                    sequence=1,
                    observed_at=clock.instant,
                    received_ns=clock.nanoseconds,
                    last_price="10",
                    bid_price="10",
                    tradable=True,
                )
            )
        if fail_main:

            class BrokenRule:
                def evaluate(self, value):
                    raise ValueError("test rule failure")

            manager.owners["MAIN"].rules = (BrokenRule(),)
        else:
            manager.pause("MAIN")
        if not login_start:
            rt.confirmed_arm(directions=("SELL",))
        for owner in manager.owners.values():
            rt.kernel.retain_board_sell_grant(owner.context.payload_hash)
        rt.kernel._grant = rt.kernel._grant.model_copy(update={"directions": ("BUY",)})
        broker.queue_submit(fill_scenario(100, full=True))
        settle_controller(manager, lambda: len(broker.query_trades()) == 1)
        assert str(broker.query_trades()[0].symbol) == "300001.SZ"
        assert not manager.owners["MAIN"].enabled
        assert manager.owners["CHINEXT"].enabled
        if fail_main:
            assert manager.owners["MAIN"].snapshot().state == "FAILED"
            assert not rt.kernel.snapshot().halted
            assert len(broker.query_orders()) == 1
            return
        manager.owners["MAIN"].set_enabled(True)
        broker.queue_submit(fill_scenario(100, full=True))
        settle_controller(
            manager, lambda: all(lot.remaining == 0 for lot in rt.kernel.snapshot().lots)
        )
        assert len(broker.query_orders()) == 2
        assert {s.plan_hash for s in rt.kernel.exit_inputs()[2] if s.side == "SELL"} == {
            o.context.payload_hash for o in manager.owners.values()
        }


def test_restart_restores_latched_target_without_reenabling(tmp_path):
    opening = close_buy_day(tmp_path)
    context = board_context(fixture_context(tmp_path, opening), at="10:17")
    BoardSellConfigurationStore(tmp_path).save(BoardSellConfiguration(main=context.board_rule), 0)
    clock = ExitScenarioClock(D1_NOW.replace(hour=10, minute=17))
    limits = EngineLimits(
        max_buy_value="0", max_order_value="1005", max_positions=1, max_order_quantity=100
    )
    for boot in range(2):
        with open_offline_engine(
            tmp_path,
            clock=lambda clock=clock: clock.instant,
            plan_payload=next_day_plan(),
            preload_plan=False,
            opening_reference=opening,
            broker_opening_quantity=100,
            limits=limits,
        ) as rt:
            if boot:
                rt.kernel.install_board_exit(context, lambda item: False, restoring=True)
            rt.kernel.start_recovery()
            wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
            manager = BoardSellController(
                rt.kernel,
                BoardSellConfigurationStore(tmp_path),
                instrument,
                lambda clock=clock: clock.instant,
                rt.plan.payload.strategy_id,
            )
            settle_controller(manager, lambda manager=manager: manager._restored)
            if not boot:
                manager.accept_prepared(context)
            owner = manager.owners["MAIN"]
            settle_controller(manager, lambda owner=owner: owner.snapshot().state == "READY")
            if not boot:

                class Tracker:
                    def evaluate(self, value):
                        return RuleResult("TRACKER_V1", None, {"armed": True, "peak": "10.50"})

                owner.rules = (Tracker(), *owner.rules)
                owner.set_enabled(True)
                rt.confirmed_arm(directions=("SELL",))
                settle_controller(
                    manager,
                    lambda owner=owner: bool(owner.gate.latest)
                    and owner.gate.latest[context.lots[0].lot_id].state.target_remaining == 0,
                )
            else:
                assert not owner.enabled
                assert rt.kernel.snapshot().halted
                assert owner._states[context.lots[0].lot_id].rule_states["TRACKER_V1"] == {
                    "armed": True,
                    "peak": "10.50",
                }
                assert (
                    owner._states[context.lots[0].lot_id].rule_states["BOARD_CONFIGURATION"][
                        "version"
                    ]
                    == 1
                )
                with pytest.raises(ValueError, match="cannot be replaced"):
                    rt.kernel.install_board_exit(context, lambda item: True)
                assert owner.gate.latest[context.lots[0].lot_id].state.target_remaining == 0
            assert not rt.broker.query_orders()


@pytest.mark.parametrize("tail_phases", [False, True])
@pytest.mark.parametrize("activation", ["preview", "login"])
def test_authenticated_board_prepare_preview_confirmation_and_pause(
    tmp_path, tail_phases, activation
):
    from concurrent.futures import ThreadPoolExecutor
    from time import time
    from types import SimpleNamespace
    from uuid import uuid4

    from pydantic import SecretStr

    from auto_trading_system.control.ipc_protocol import IPCError, Request
    from auto_trading_system.control.node_authorization import NodeAuthorization
    from auto_trading_system.control.owner_mailbox import OwnerMailbox
    from auto_trading_system.domain import ShanghaiTimestamp, SystemClock
    from auto_trading_system.security.operator_password import OperatorPasswordHasher
    from auto_trading_system.security.operator_session import LocalOperatorSessionManager

    opening = close_buy_day(tmp_path)
    store = BoardSellConfigurationStore(tmp_path)
    store.save(BoardSellConfiguration(main=BoardSellRule(mode="TIME_ONLY", sell_at="09:30")), 0)
    with (
        open_offline_engine(
            tmp_path,
            clock=lambda: D1_NOW,
            plan_payload=next_day_plan(),
            preload_plan=False,
            tail_phases=tail_phases,
            opening_reference=opening,
            broker_opening_quantity=100,
        ) as rt,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        if tail_phases:
            # The offline phased harness otherwise admits only symbols in today's buy plans.
            rt.kernel._risk_guard = lambda item: item.side == "SELL" and item.symbol == "000001.SZ"
        rt.kernel.start_recovery()
        wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        manager = BoardSellController(
            rt.kernel, store, instrument, lambda: D1_NOW, rt.plan.payload.strategy_id
        )
        settle_controller(manager, lambda manager=manager: manager._restored)
        sessions = LocalOperatorSessionManager(
            SecretStr(OperatorPasswordHasher().hash_password("board-sell-test-password")),
            actor_id="test-operator",
            clock=SimpleNamespace(now=lambda: ShanghaiTimestamp(D1_NOW)),
            monotonic_clock=SystemClock(),
        )
        issued = sessions.login(SecretStr("board-sell-test-password"))
        mailbox = OwnerMailbox()
        auth = NodeAuthorization(
            rt.kernel, mailbox, sessions, clock=lambda: D1_NOW, board_sell=manager
        )

        def invoke(action, body):
            request = Request(
                request_id=uuid4().hex,
                engine_boot_id=rt.kernel.boot_id,
                sender_role="operator",
                deadline_ms=int(time() * 1000) + 7500,
                action=action,
                payload=body
                if action == "LOGIN"
                else {
                    "session_token": issued.session_token,
                    "csrf_token": issued.csrf_token,
                    **body,
                },
            )
            result = pool.submit(auth.handle, request)
            until = monotonic() + 6
            while not result.done() and monotonic() < until:
                rt.kernel.poll()
                mailbox.poll()
                manager.poll()
                sleep(0.001)
            return result.result(1)

        if activation == "login":
            with pytest.raises(IPCError, match="OPERATOR_LOGIN_FAILED"):
                invoke("LOGIN", {"password": "wrong", "start_board_sell": True})
            assert not manager._starts
            invoke("LOGIN", {"password": "board-sell-test-password"})
            assert not manager._starts
            invoke("LOGIN", {"password": "board-sell-test-password", "start_board_sell": True})
        else:
            assert (
                invoke("PREPARE_BOARD_SELL", {"board": "MAIN"})["boards"]["MAIN"]["enabled"]
                is False
            )
            settle_controller(manager, lambda: manager.owners["MAIN"].snapshot().state == "READY")
            preview = invoke("PREVIEW_BOARD_SELL", {"board": "MAIN"})
            with pytest.raises(IPCError, match="CONFIRMATION_INVALID"):
                invoke(
                    "CONFIRM_BOARD_SELL", {"preview_id": preview["preview_id"], "phrase": "wrong"}
                )
            assert not manager.owners["MAIN"].enabled
            manager.failures["MAIN"] = "BOARD_SELL_AUTHORIZATION_FAILED"
            preview = invoke("PREVIEW_BOARD_SELL", {"board": "MAIN"})
            reply = invoke(
                "CONFIRM_BOARD_SELL",
                {"preview_id": preview["preview_id"], "phrase": preview["confirmation_phrase"]},
            )
            assert reply["boards"]["MAIN"]["enabled"] is True
            assert reply["boards"]["MAIN"]["authorized"] is False
        settle_controller(manager, lambda: manager.status()["boards"]["MAIN"]["authorized"])
        if activation == "login":
            settle_controller(manager, lambda: not manager._starts)
            assert manager.status()["boards"]["MAIN"]["startup_status"] == "STARTED"
            assert (
                manager.status()["boards"]["CHINEXT"]["startup_status"]
                == "BOARD_SELL_NO_ELIGIBLE_LOTS"
            )
            context = manager.owners["MAIN"].context
            grant = rt.kernel._grant
            invoke("START_BOARD_SELL", {"board": "MAIN"})
            settle_controller(manager, lambda: not manager._starts)
            assert manager.owners["MAIN"].context == context
            assert rt.kernel._grant == grant
        assert "MAIN" not in manager.failures
        assert rt.kernel._grant.directions == ("SELL",)
        if tail_phases:
            with pytest.raises(ValueError, match="no active tail phase"):
                rt.kernel._validate_grant(
                    rt.kernel._grant.model_copy(update={"directions": ("BUY",)})
                )
        assert rt.kernel._grant.valid_until == manager.owners["MAIN"].context.valid_until
        assert (rt.kernel._grant.valid_until - D1_NOW).total_seconds() > 900
        assert not rt.broker.query_orders()
        owner = manager.owners["MAIN"]
        owner.gate.ns_clock = lambda: 1_000_000_000
        rt.broker.queue_submit(fill_scenario(100, full=True))
        owner.quote(
            ExitQuote(
                symbol="000001.SZ",
                sequence=1,
                observed_at=D1_NOW,
                received_ns=1_000_000_000,
                last_price="10",
                bid_price="10",
                tradable=True,
            )
        )
        settle_controller(manager, lambda: rt.kernel.snapshot().lots[0].remaining == 0)
        assert len(rt.broker.query_trades()) == 1
        assert invoke("PAUSE_BOARD_SELL", {"board": "MAIN"})["boards"]["MAIN"]["enabled"] is False
        invoke("BOARD_SELL_STATUS", {})
        assert not manager._starts
        assert not owner.enabled
        if activation == "login":
            invoke("LOGIN", {"password": "board-sell-test-password", "start_board_sell": True})
            settle_controller(manager, lambda: not manager._starts)
            assert owner.enabled
            assert len(rt.broker.query_trades()) == 1
            rt.kernel.emergency_halt()
            assert (
                manager.status()["boards"]["MAIN"]["startup_status"] == "BOARD_SELL_GLOBAL_PAUSED"
            )


def authenticated_session():
    from auto_trading_system.domain import ShanghaiTimestamp
    from auto_trading_system.security.operator_session import AuthenticatedOperatorSession

    return AuthenticatedOperatorSession(
        session_key="a" * 64,
        actor_id="test-operator",
        issued_at=ShanghaiTimestamp(D1_NOW),
        expires_at=ShanghaiTimestamp(D1_NOW.replace(hour=15)),
    )


@pytest.mark.parametrize(
    "boundary",
    [
        "global-stop",
        "pause",
        "expired",
        "early-expired",
        "early-timeout",
        "relogin",
        "missing-settings",
        "restore-failure",
    ],
)
def test_login_start_respects_stop_session_configuration_and_board_boundaries(tmp_path, boundary):
    from concurrent.futures import Future

    opening = close_buy_day(tmp_path)
    store = BoardSellConfigurationStore(tmp_path)
    if boundary != "missing-settings":
        store.save(BoardSellConfiguration(), 0)
    with open_offline_engine(
        tmp_path,
        clock=lambda: D1_NOW,
        plan_payload=next_day_plan(),
        preload_plan=False,
        opening_reference=opening,
        broker_opening_quantity=100,
    ) as rt:
        if boundary not in {"early-expired", "early-timeout"}:
            rt.kernel.start_recovery()
            wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        manager = BoardSellController(
            rt.kernel, store, instrument, lambda: D1_NOW, rt.plan.payload.strategy_id
        )
        valid = True

        def authenticate():
            if not valid:
                raise ValueError("OPERATOR_SESSION_EXPIRED")
            return authenticated_session()

        if boundary in {"pause", "expired", "relogin"}:
            settle_controller(
                manager, lambda: manager._restored and manager._configuration is not None
            )
            preparation = manager.prepare("MAIN").result(timeout=2)
            delayed = Future()
            manager.prepare = lambda board: delayed
            manager.start_authenticated(authenticate, ("MAIN",))
            manager.poll()
            assert manager._starts["MAIN"].preparation is delayed
            if boundary == "pause":
                manager.pause("MAIN")
            else:
                valid = False
            if boundary == "relogin":
                manager.start_authenticated(authenticated_session, ("MAIN",))
                assert manager._starts["MAIN"].preparation is delayed
            delayed.set_result(preparation)
        else:
            # Start before restoration; no browser status requests are needed.
            manager.start_authenticated(authenticate)
            if boundary == "global-stop":
                rt.kernel.emergency_halt()
            elif boundary == "early-expired":
                valid = False
            elif boundary == "early-timeout":
                for pending in manager._starts.values():
                    pending.deadline = monotonic() - 1
            elif boundary == "restore-failure":
                manager.failures["MAIN"] = "BOARD_SELL_RESTORE_FAILED"
        settle_controller(manager, lambda: not manager._starts)
        for _ in range(5):
            rt.kernel.poll()
            manager.poll()
        main = manager.status()["boards"]["MAIN"]
        if boundary == "relogin":
            assert main["enabled"] and main["authorized"]
            assert main["startup_status"] == "STARTED"
            assert not rt.broker.query_orders()
            return
        assert not main["enabled"]
        expected = {
            "global-stop": "BOARD_SELL_GLOBAL_PAUSED",
            "pause": "PAUSED",
            "expired": "OPERATOR_SESSION_EXPIRED",
            "early-expired": "OPERATOR_SESSION_EXPIRED",
            "early-timeout": "BOARD_SELL_START_TIMEOUT",
            "missing-settings": "BOARD_SELL_CONFIGURATION_REQUIRED",
            "restore-failure": "BOARD_SELL_RESTORE_FAILED",
        }
        assert main["startup_status"] == expected[boundary]
        if boundary == "global-stop":
            assert rt.kernel.permission.snapshot().reason == "LOCAL_HALT"
        if boundary == "restore-failure":
            assert (
                manager.status()["boards"]["CHINEXT"]["startup_status"]
                == "BOARD_SELL_NO_ELIGIBLE_LOTS"
            )
        assert not rt.broker.query_orders()
        assert rt.kernel._grant is None


def test_partial_fill_cancel_reprices_only_residual_and_honors_repost_limit(tmp_path):
    opening = close_buy_day(tmp_path)
    context = board_context(fixture_context(tmp_path, opening), at="09:30").model_copy(
        update={"max_children": 2, "cancel_after_seconds": 5}
    )
    clock = ExitScenarioClock()
    broker = MockBrokerGateway(clock)
    with open_offline_engine(
        tmp_path,
        clock=lambda: clock.instant,
        broker=broker,
        plan_payload=next_day_plan(),
        preload_plan=False,
        opening_reference=opening,
        broker_opening_quantity=100,
        limits=EngineLimits(
            max_buy_value="0", max_order_value="1005", max_positions=1, max_order_quantity=100
        ),
    ) as rt:
        rt.kernel._risk_guard = lambda item: True
        rt.kernel.start_recovery()
        wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        gate = ExitStateGate(
            context, clock=lambda: clock.instant, ns_clock=lambda: clock.nanoseconds
        )
        rt.kernel.install_board_exit(context, lambda item: owner.allows(item))
        owner = BoardSellEngine(rt.kernel, gate)
        pump(owner, lambda: owner.snapshot().state == "READY")
        owner.set_enabled(True)
        rt.confirmed_arm(directions=("SELL",))
        broker.queue_submit(fill_scenario(20, full=False))
        publish(owner, clock, "10", 1)
        pump(
            owner, lambda: rt.kernel.snapshot().lots[0].remaining == 80 and rt.kernel.critical_idle
        )
        draft = BoardSellConfigurationStore(tmp_path).save(
            BoardSellConfiguration(main=context.board_rule, cancel_after_seconds=2, max_reposts=1),
            0,
        )
        owner.apply_configuration(draft, {context.lots[0].lot_id: "AUTOMATIC"})
        clock.advance(2)
        pump(
            owner,
            lambda: rt.kernel.snapshot().orders[0].phase == "TERMINAL_CONFIRMED"
            and rt.kernel.critical_idle,
        )
        assert len(broker.query_orders()) == 1
        publish(owner, clock, "9.90", 2)
        owner.gate.quotes["000001.SZ"] = owner.gate.quotes["000001.SZ"].model_copy(
            update={"bid_price": "9.90"}
        )
        pump(owner, lambda: len(broker.query_orders()) == 2)
        assert broker.query_orders()[1].order_quantity.value == 80
        assert broker.query_orders()[1].limit_price.amount == Decimal("9.90")
        clock.advance(2)
        pump(
            owner,
            lambda: rt.kernel.snapshot().orders[-1].phase == "TERMINAL_CONFIRMED"
            and rt.kernel.critical_idle,
        )
        publish(owner, clock, "9.90", 3)
        pump(owner, lambda: bool(owner.waiting_reasons))
        assert len(broker.query_orders()) == 2
        assert owner.waiting_reasons[context.lots[0].lot_id] == "REPOST_LIMIT_REACHED"


def test_cross_day_keeps_unsubmitted_target_and_original_policy_when_source_unchecked(tmp_path):
    from datetime import timedelta

    from auto_trading_system.protocol.v2.plans import PlanPayload

    opening = close_buy_day(tmp_path)
    context = board_context(fixture_context(tmp_path, opening), at="10:17")
    BoardSellConfigurationStore(tmp_path).save(BoardSellConfiguration(main=context.board_rule), 0)
    clock = ExitScenarioClock(D1_NOW.replace(hour=10, minute=17))
    limits = EngineLimits(
        max_buy_value="0", max_order_value="1005", max_positions=1, max_order_quantity=100
    )
    with open_offline_engine(
        tmp_path,
        clock=lambda: clock.instant,
        plan_payload=next_day_plan(),
        preload_plan=False,
        opening_reference=opening,
        broker_opening_quantity=100,
        limits=limits,
    ) as rt:
        rt.kernel.start_recovery()
        wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        manager = BoardSellController(
            rt.kernel,
            BoardSellConfigurationStore(tmp_path),
            instrument,
            lambda: clock.instant,
            rt.plan.payload.strategy_id,
        )
        settle_controller(manager, lambda: manager._restored)
        manager.accept_prepared(context)
        owner = manager.owners["MAIN"]
        settle_controller(manager, lambda: owner.snapshot().state == "READY")
        owner.set_enabled(True)
        rt.confirmed_arm(directions=("SELL",))
        settle_controller(
            manager,
            lambda: bool(owner.gate.latest)
            and owner.gate.latest[context.lots[0].lot_id].state.target_remaining == 0,
        )
        clock.instant = clock.instant.replace(hour=15, minute=1)
        rt.kernel.start_recovery()
        wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        opening = rt.kernel.seal_portfolio().result(timeout=2)
    next_date = (D1_NOW + timedelta(days=1)).date()
    plan = PlanPayload.model_validate_json(
        next_day_plan().model_dump_json().replace(str(D1_NOW.date()), str(next_date))
    )
    clock.instant = D1_NOW + timedelta(days=1)
    store = BoardSellConfigurationStore(tmp_path)
    store.save(
        BoardSellConfiguration(
            include_automatic=False, main=BoardSellRule(mode="TIME_ONLY", sell_at="14:40")
        ),
        1,
    )
    with open_offline_engine(
        tmp_path,
        clock=lambda: clock.instant,
        plan_payload=plan,
        preload_plan=False,
        opening_reference=opening,
        broker_opening_quantity=100,
        limits=limits,
    ) as rt:
        rt.kernel.start_recovery()
        wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        manager = BoardSellController(
            rt.kernel, store, instrument, lambda: clock.instant, rt.plan.payload.strategy_id
        )
        settle_controller(manager, lambda: manager._restored)
        carried = manager.prepare("MAIN").result(timeout=2)
        assert carried.lots[0].prior_target_remaining == 0
        assert carried.lots[0].prior_fired_rules == ("SELL_BY_TIME",)
        assert carried.lots[0].board_rule.sell_at == "10:17"
        manager.accept_prepared(carried)
        owner = manager.owners["MAIN"]
        settle_controller(manager, lambda: owner.configuration is not None)
        assert owner.rule_for(carried.lots[0]).sell_at == "14:40"
        assert not owner.includes(carried.lots[0].lot_id)
        assert not rt.broker.query_orders()


def test_sell_only_session_does_not_freeze_afternoon_buy_targets(tmp_path):
    from auto_trading_system.apps.trade_runtime.engine_simulation import DEMO_NOW
    from auto_trading_system.apps.trade_runtime.entry_simulation import (
        open_entry_owner,
        publish_entry,
        pump_entry,
    )

    clock = ExitScenarioClock(DEMO_NOW)
    broker = MockBrokerGateway(clock)
    with open_entry_owner(tmp_path, clock, broker) as (rt, owner):
        grant = rt.confirmed_arm()
        rt.kernel._grant = grant.model_copy(update={"directions": ("SELL",), "max_buy_value": "0"})
        publish_entry(owner, clock, 1)
        owner.poll()
        assert owner.snapshot().targets == ()
        assert not broker.query_orders()
        rt.kernel._grant = grant
        broker.queue_submit(fill_scenario(100, full=True))
        pump_entry(owner, lambda: bool(broker.query_trades()))
        assert owner.snapshot().targets[0].target_quantity == 100
        assert broker.query_trades()[0].quantity.value == 100
