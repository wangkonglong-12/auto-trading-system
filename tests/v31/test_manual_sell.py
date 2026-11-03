"""Real J1/owner/Mock execution for explicitly confirmed stock sales."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import pytest
from test_manual_buy import instrument

from auto_trading_system.apps.trade_runtime.engine_simulation import DEMO_NOW, open_offline_engine
from auto_trading_system.apps.trade_runtime.exit_simulation import (
    D1_NOW,
    close_buy_day,
    fill_scenario,
    next_day_plan,
)
from auto_trading_system.control.ipc_protocol import IPCError
from auto_trading_system.domain import ShanghaiTimestamp
from auto_trading_system.gateways.broker.mock import SubmitOutcome, SubmitScenario
from auto_trading_system.runtime.manual_sell import (
    ManualSellController,
    ManualSellInput,
    ManualSellOperation,
)
from auto_trading_system.security.operator_session import AuthenticatedOperatorSession
from auto_trading_system.testing.v31_scenarios import ScenarioClock


def session(key: str = "a") -> AuthenticatedOperatorSession:
    return AuthenticatedOperatorSession(
        key * 64,
        "test-operator",
        ShanghaiTimestamp(D1_NOW),
        ShanghaiTimestamp(D1_NOW + timedelta(hours=1)),
    )


def manager(rt: Any) -> ManualSellController:
    return ManualSellController(
        rt.kernel,
        strategy_id=rt.plan.payload.strategy_id,
        settings_hash="sha256:" + "b" * 64,
        instrument_reader=instrument,
        settings_current=lambda: True,
    )


def pump(rt: Any, controller: ManualSellController, predicate: Any) -> Any:
    until = monotonic() + 3
    while monotonic() < until:
        state = rt.kernel.poll()
        controller.poll()
        if predicate(state):
            return state
        sleep(0.001)
    raise AssertionError((rt.kernel.snapshot(), controller.active))


def prepare(rt: Any, controller: ManualSellController) -> dict[str, Any]:
    value = ManualSellInput(symbol="000001", limit_price="10", quantity=100)
    facts = controller.query(value).result(2)
    preview = controller.preview(session(), value, facts)
    pump(rt, controller, lambda _: rt.kernel.critical_idle)
    return preview


def test_sale_is_independent_of_auto_and_uses_actual_fills(tmp_path: Path) -> None:
    opening = close_buy_day(tmp_path)
    with open_offline_engine(
        tmp_path,
        clock=lambda: D1_NOW,
        plan_payload=next_day_plan(),
        opening_reference=opening,
        broker_opening_quantity=100,
    ) as rt:
        c = manager(rt)
        rt.kernel.start_recovery()
        pump(rt, c, lambda s: s.state == "REVIEW_REQUIRED")
        p = prepare(rt, c)
        assert rt.broker.query_orders() == ()
        rt.broker.queue_submit(fill_scenario(100, full=True))
        facts = c.query_confirmation(session(), str(p["operation_id"]))
        assert facts is not None
        response = c.confirm(session(), str(p["operation_id"]), facts.result(2))
        assert response["status"] == "PERSISTING"
        final = pump(rt, c, lambda s: bool(s.lots) and s.lots[0].remaining == 0)
        assert final.halted and final.grant_id is None
        assert len(rt.broker.query_orders()) == 1 and len(rt.broker.query_trades()) == 1
        c.confirm(session(), str(p["operation_id"]), None)
        assert len(rt.broker.query_orders()) == 1
        body = c.lookup(str(p["operation_id"])).result(2)
        assert body is not None
        op = ManualSellOperation.model_validate_json(body)
        result = c.result(session(), str(p["operation_id"]), op)
        assert result["children"][0]["status"] == "FILLED"
        records = rt.journal.recovery_page().result(2)
        assert records[-1].submission.manual_sell_binding is not None


@pytest.mark.parametrize("outcome", [SubmitOutcome.REJECT, SubmitOutcome.TIMEOUT_AFTER_ACCEPT])
def test_sale_failure_never_replays_on_repeated_confirmation(tmp_path: Path, outcome: Any) -> None:
    opening = close_buy_day(tmp_path)
    with open_offline_engine(
        tmp_path,
        clock=lambda: D1_NOW,
        plan_payload=next_day_plan(),
        opening_reference=opening,
        broker_opening_quantity=100,
    ) as rt:
        c = manager(rt)
        rt.kernel.start_recovery()
        pump(rt, c, lambda s: s.state == "REVIEW_REQUIRED")
        p = prepare(rt, c)
        rt.broker.queue_submit(SubmitScenario(outcome))
        f = c.query_confirmation(session(), str(p["operation_id"]))
        assert f is not None
        c.confirm(session(), str(p["operation_id"]), f.result(2))
        pump(
            rt,
            c,
            lambda s: bool(s.submissions)
            and s.submissions[0].status not in {"PERSISTING", "DISPATCHING"},
        )
        c.confirm(session(), str(p["operation_id"]), None)
        assert len(rt.broker.query_orders()) <= 1
        assert (
            len([r for r in rt.journal.recovery_page().result(2) if r.submission.side == "SELL"])
            == 1
        )


def test_safety_halt_and_other_session_fence_sale(tmp_path: Path) -> None:
    opening = close_buy_day(tmp_path)
    with open_offline_engine(
        tmp_path,
        clock=lambda: D1_NOW,
        plan_payload=next_day_plan(),
        opening_reference=opening,
        broker_opening_quantity=100,
    ) as rt:
        c = manager(rt)
        rt.kernel.start_recovery()
        pump(rt, c, lambda s: s.state == "REVIEW_REQUIRED")
        p = prepare(rt, c)
        with pytest.raises(IPCError, match="PREVIEW_NOT_FOUND"):
            c.query_confirmation(session("c"), str(p["operation_id"]))
        rt.kernel.emergency_halt("TEST_STOP")
        with pytest.raises((IPCError, ValueError)):
            c.query_confirmation(session(), str(p["operation_id"]))
        assert rt.broker.query_orders() == ()


def two_lots(directory: Path, first_fill: int = 100) -> Any:
    clock = ScenarioClock(DEMO_NOW)
    with open_offline_engine(directory, clock=lambda: clock.instant, max_orders=2) as rt:
        c = manager(rt)
        rt.kernel.start_recovery()
        pump(rt, c, lambda s: s.state == "REVIEW_REQUIRED")
        rt.confirmed_arm()
        for index in range(2):
            bought = first_fill if index == 0 else 100
            rt.broker.queue_submit(fill_scenario(bought, full=bought == 100))
            child = rt.buy()
            rt.kernel.submit(child, valid_until=monotonic() + 2)
            pump(
                rt,
                c,
                lambda s, child=child: len(s.lots) > 0
                and rt.kernel.critical_idle
                and any(
                    x.client_order_id == child.client_order_id and x.status == "SDK_ACCEPTED"
                    for x in s.submissions
                ),
            )
            if bought < 100:
                rt.kernel.operator_cancel_order(child.client_order_id)
            rt.kernel._next_observation = 0
            pump(
                rt,
                c,
                lambda s: rt.kernel.critical_idle
                and all(v.phase == "TERMINAL_CONFIRMED" for v in s.orders),
            )
        clock = ScenarioClock(DEMO_NOW.replace(hour=15, minute=1))
        rt.kernel.start_recovery()
        pump(rt, c, lambda s: s.state == "REVIEW_REQUIRED")
        return rt.kernel.seal_portfolio().result(2)


def test_two_lots_submit_distinct_children_once_and_cancel_keeps_other_reservation(
    tmp_path: Path,
) -> None:
    opening = two_lots(tmp_path)
    with open_offline_engine(
        tmp_path,
        clock=lambda: D1_NOW,
        plan_payload=next_day_plan(),
        opening_reference=opening,
        broker_opening_quantity=200,
        max_orders=2,
    ) as rt:
        c = manager(rt)
        rt.kernel.start_recovery()
        pump(rt, c, lambda s: s.state == "REVIEW_REQUIRED")
        value = ManualSellInput(symbol="000001", limit_price="10", quantity=200)
        p = c.preview(session(), value, c.query(value).result(2))
        pump(rt, c, lambda _: rt.kernel.critical_idle)
        assert p["child_count"] == 2
        f = c.query_confirmation(session(), str(p["operation_id"]))
        assert f is not None
        c.confirm(session(), str(p["operation_id"]), f.result(2))
        pump(
            rt,
            c,
            lambda s: len(s.submissions) == 2
            and all(x.status == "SDK_ACCEPTED" for x in s.submissions),
        )
        rt.kernel._next_observation = 0
        state = pump(
            rt,
            c,
            lambda s: sum(owned_lot.reserved_quantity for owned_lot in s.lots) == 200
            and rt.kernel.critical_idle,
        )
        assert len(rt.broker.query_orders()) == 2

        first = rt.broker.query_orders()[0]
        rt.kernel.operator_cancel_order(first.client_order_id)
        rt.kernel._next_observation = 0
        state = pump(
            rt,
            c,
            lambda s: sum(owned_lot.reserved_quantity for owned_lot in s.lots) == 100
            and rt.kernel.critical_idle,
        )
        assert sum(owned_lot.remaining for owned_lot in state.lots) == 200
        assert len(rt.broker.query_orders()) == 2


def test_restart_restores_sale_and_never_replays_saved_operation(tmp_path: Path) -> None:
    opening = close_buy_day(tmp_path)
    with open_offline_engine(
        tmp_path,
        clock=lambda: D1_NOW,
        plan_payload=next_day_plan(),
        opening_reference=opening,
        broker_opening_quantity=100,
    ) as rt:
        c = manager(rt)
        rt.kernel.start_recovery()
        pump(rt, c, lambda s: s.state == "REVIEW_REQUIRED")
        p = prepare(rt, c)
        rt.broker.queue_submit(fill_scenario(100, full=True))
        f = c.query_confirmation(session(), str(p["operation_id"]))
        assert f is not None
        c.confirm(session(), str(p["operation_id"]), f.result(2))
        pump(rt, c, lambda s: bool(s.lots) and s.lots[0].remaining == 0 and rt.kernel.critical_idle)
        broker = rt.broker
    with open_offline_engine(
        tmp_path,
        broker=broker,
        clock=lambda: D1_NOW,
        plan_payload=next_day_plan(),
        opening_reference=opening,
        broker_opening_quantity=100,
    ) as rt:
        c = manager(rt)
        rt.kernel.start_recovery()
        pump(rt, c, lambda s: s.state in {"REVIEW_REQUIRED", "RECOVERY_BLOCKED"})
        assert rt.kernel.snapshot().recovery.status == "CLEAR"
        op = ManualSellOperation.model_validate_json(c.lookup(str(p["operation_id"])).result(2))
        result = c.result(session(), str(p["operation_id"]), op)
        assert result["status"] == "SUBMITTED" and len(broker.query_orders()) == 1
        with pytest.raises(IPCError, match="PREVIEW_NOT_FOUND"):
            c.confirm(session(), str(p["operation_id"]), None)
        assert len(broker.query_orders()) == 1


def test_restart_does_not_send_a_saved_but_unsubmitted_plan(tmp_path: Path) -> None:
    opening = close_buy_day(tmp_path)
    with open_offline_engine(
        tmp_path,
        clock=lambda: D1_NOW,
        plan_payload=next_day_plan(),
        opening_reference=opening,
        broker_opening_quantity=100,
    ) as rt:
        c = manager(rt)
        rt.kernel.start_recovery()
        pump(rt, c, lambda s: s.state == "REVIEW_REQUIRED")
        p = prepare(rt, c)
        f = c.query_confirmation(session(), str(p["operation_id"]))
        assert f is not None
        c.confirm(session(), str(p["operation_id"]), f.result(2))
        c.active[str(p["operation_id"])].saved.result(2)
        assert rt.broker.query_orders() == ()
    with open_offline_engine(
        tmp_path,
        clock=lambda: D1_NOW,
        plan_payload=next_day_plan(),
        opening_reference=opening,
        broker_opening_quantity=100,
    ) as rt:
        c = manager(rt)
        rt.kernel.start_recovery()
        pump(rt, c, lambda s: s.state == "REVIEW_REQUIRED")
        op = ManualSellOperation.model_validate_json(c.lookup(str(p["operation_id"])).result(2))
        assert c.result(session(), str(p["operation_id"]), op)["status"] == "NOT_SUBMITTED"
        assert rt.broker.query_orders() == ()


def test_partial_round_lot_sale_skips_an_older_odd_remainder(tmp_path: Path) -> None:
    opening = two_lots(tmp_path, first_fill=50)
    with open_offline_engine(
        tmp_path,
        clock=lambda: D1_NOW,
        plan_payload=next_day_plan(),
        opening_reference=opening,
        broker_opening_quantity=150,
    ) as rt:
        c = manager(rt)
        rt.kernel.start_recovery()
        pump(rt, c, lambda state: state.state == "REVIEW_REQUIRED")
        preview = prepare(rt, c)
        assert preview["child_count"] == 1
        rt.broker.queue_submit(fill_scenario(100, full=True))
        facts = c.query_confirmation(session(), str(preview["operation_id"]))
        assert facts is not None
        c.confirm(session(), str(preview["operation_id"]), facts.result(2))
        state = pump(rt, c, lambda state: sum(lot.remaining for lot in state.lots) == 50)
        assert sum(lot.sold_quantity for lot in state.lots) == 100
