"""Actual J1/SDK execution with an offline broker; never connects to QMT."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import pytest
from pydantic import ValidationError

from auto_trading_system.apps.trade_runtime.engine_simulation import DEMO_NOW, open_offline_engine
from auto_trading_system.control.ipc_protocol import IPCError
from auto_trading_system.domain import Money, Price, Quantity, ShanghaiTimestamp
from auto_trading_system.execution.dispatch_boundary import DispatchPermission
from auto_trading_system.gateways.broker.mock import SubmitOutcome, SubmitScenario
from auto_trading_system.journal.critical_models import Submission
from auto_trading_system.runtime.manual_buy import ManualBuyController, ManualBuyInput
from auto_trading_system.security.operator_session import AuthenticatedOperatorSession
from auto_trading_system.strategies.entry_state import EntryInstrument


def session(key: str = "a") -> AuthenticatedOperatorSession:
    return AuthenticatedOperatorSession(
        key * 64,
        "test-operator",
        ShanghaiTimestamp(DEMO_NOW),
        ShanghaiTimestamp(DEMO_NOW + timedelta(hours=1)),
    )


def instrument(symbol: str, price: str) -> tuple[EntryInstrument, str]:
    return EntryInstrument(
        symbol=symbol,
        reference_price=price,
        lower_price="9",
        upper_price="11",
        price_tick="0.01",
        min_quantity=100,
        quantity_step=100,
        max_order_quantity=1000,
        source_hash="sha256:" + "a" * 64,
    ), "平安银行"


def controller(rt: Any) -> Any:
    return ManualBuyController(
        rt.kernel,
        strategy_id=rt.plan.payload.strategy_id,
        settings_hash="sha256:" + "b" * 64,
        instrument_reader=instrument,
        settings_current=lambda: True,
    )


def settle(rt: Any, predicate: Any) -> Any:
    until = monotonic() + 3
    while monotonic() < until:
        state = rt.kernel.poll()
        if predicate(state):
            return state
        sleep(0.001)
    raise AssertionError((rt.kernel.snapshot(), rt.kernel.permission.snapshot()))


def prepare(rt: Any, manager: Any) -> Any:
    value = ManualBuyInput(symbol="000001", limit_price="10", quantity=100)
    facts = manager.query(value).result(2)
    result = manager.preview(session(), value, facts)
    settle(rt, lambda _: rt.kernel.critical_idle)
    return result


def recover(rt: Any) -> None:
    rt.kernel.start_recovery()
    settle(rt, lambda s: s.state in {"REVIEW_REQUIRED", "RECOVERY_BLOCKED"})
    assert rt.kernel.snapshot().recovery.status == "CLEAR"


def test_no_plan_purchase_never_arms_auto_and_owns_actual_partial_fills(tmp_path: Path) -> None:
    with open_offline_engine(tmp_path, preload_plan=False) as rt:
        recover(rt)
        manager = controller(rt)
        preview = prepare(rt, manager)
        assert rt.broker.query_orders() == ()
        rt.broker.queue_submit(
            SubmitScenario(
                SubmitOutcome.PARTIAL_FILL,
                partial_fill_quantity=Quantity(40),
                fill_price=Price.parse("9.99"),
                fill_notional=Money.parse("399.60"),
            )
        )
        result = manager.confirm(session(), preview["preview_id"])
        settle(rt, lambda s: s.submissions[0].status == "SDK_ACCEPTED")
        assert (
            manager.confirm(session(), preview["preview_id"])["client_order_id"]
            == result["client_order_id"]
        )
        assert len(rt.broker.query_orders()) == 1
        assert rt.kernel.snapshot().halted and rt.kernel.snapshot().grant_id is None
        rt.kernel._next_observation = 0
        state = settle(rt, lambda s: len(s.lots) == 1)
        assert state.lots[0].bought_quantity == 40
        assert Decimal(state.lots[0].cost_price) == Decimal("9.99")
        assert state.lots[0].eligible_sell_date > DEMO_NOW.date()
        assert rt.kernel.entry_symbol_claimed("000001.SZ", "sha256:" + "f" * 64)
        records = rt.journal.recovery_page().result(2)
        assert len(records) == 1
        binding = records[0].submission.manual_binding
        assert binding is not None and binding.actor_id == "test-operator"


def test_halt_and_other_session_fence_confirmation(tmp_path: Path) -> None:
    with open_offline_engine(tmp_path, preload_plan=False) as rt:
        recover(rt)
        manager = controller(rt)
        preview = prepare(rt, manager)
        with pytest.raises(IPCError, match="PREVIEW_NOT_FOUND"):
            manager.confirm(session("c"), preview["preview_id"])
        rt.kernel.emergency_halt("TEST_SAFETY_FAULT")
        with pytest.raises(IPCError, match="REJECTED"):
            manager.confirm(session(), preview["preview_id"])
        assert rt.broker.query_orders() == ()


@pytest.mark.parametrize(
    "limit, value",
    [
        ("max_buy_value", "1004"),
        ("max_order_value", "1004"),
        ("max_symbol_buy_value", "1004"),
        ("max_positions", 0),
        ("allowed_boards", ()),
    ],
)
def test_local_limits_without_auto_grant(tmp_path: Path, limit: Any, value: Any) -> None:
    with open_offline_engine(tmp_path, preload_plan=False) as rt:
        recover(rt)
        rt.kernel.limits = rt.kernel.limits.model_copy(update={limit: value})
        manager = controller(rt)
        preview = prepare(rt, manager)
        with pytest.raises(IPCError, match="REJECTED"):
            manager.confirm(session(), preview["preview_id"])
        assert rt.broker.query_orders() == () and rt.journal.recovery_page().result(2) == ()


def test_duplicate_symbol_and_auto_order_blocked(tmp_path: Path) -> None:
    with open_offline_engine(tmp_path, max_orders=2) as rt:
        recover(rt)
        rt.confirmed_arm()
        manager = controller(rt)
        preview = prepare(rt, manager)
        manager.confirm(session(), preview["preview_id"])
        settle(rt, lambda s: s.submissions[0].status == "SDK_ACCEPTED")
        rt.kernel._next_observation = 0
        settle(
            rt,
            lambda _: rt.kernel.critical_idle
            and rt.kernel._broker_snapshot is not None
            and rt.kernel._broker_snapshot.event_version == rt.kernel._events(),
        )
        duplicate = prepare(rt, manager)
        with pytest.raises(IPCError, match="REJECTED"):
            manager.confirm(session(), duplicate["preview_id"])
        with pytest.raises(ValueError, match="gate rejected"):
            rt.kernel.submit(rt.buy(), valid_until=monotonic() + 1)
        assert len(rt.broker.query_orders()) == 1


def test_uncertain_send_keeps_one_order_and_never_replays(tmp_path: Path) -> None:
    with open_offline_engine(tmp_path, preload_plan=False) as rt:
        recover(rt)
        manager = controller(rt)
        preview = prepare(rt, manager)
        rt.broker.queue_submit(SubmitScenario(SubmitOutcome.TIMEOUT_AFTER_ACCEPT))
        manager.confirm(session(), preview["preview_id"])
        settle(rt, lambda s: s.submissions[0].status == "UNCERTAIN")
        manager.confirm(session(), preview["preview_id"])
        assert len(rt.broker.query_orders()) == 1 and rt.kernel.snapshot().halted


def test_one_shot_cannot_release_other_orders_and_halt_revokes_it() -> None:
    permission = DispatchPermission()
    permission.allow_once("one", 1, monotonic() + 2)
    assert permission.snapshot().halted
    assert not permission.claim("other", 1, monotonic() + 1)
    permission.halt("STOP")
    assert not permission.claim("one", 1, monotonic() + 1)


@pytest.mark.parametrize(
    "change",
    [
        {"quantity": 0},
        {"quantity": 100.5},
        {"limit_price": "NaN"},
        {"limit_price": "0"},
        {"symbol": "bad"},
    ],
)
def test_invalid_inputs(change: Any) -> None:
    with pytest.raises((ValidationError, ValueError)):
        ManualBuyInput.model_validate(
            {"symbol": "000001", "limit_price": "10", "quantity": 100, **change}
        )


def test_legacy_j1_byte_compatible(submission: Submission) -> None:
    from auto_trading_system.protocol.v2.canonical import canonical_bytes

    body = canonical_bytes(submission.model_dump(mode="json"))
    assert b"manual_binding" not in body and b"context_type" not in body
    assert canonical_bytes(Submission.model_validate_json(body).model_dump(mode="json")) == body


def test_restart_restores_managed_fill_but_never_old_send_permission(tmp_path: Path) -> None:
    with open_offline_engine(tmp_path, preload_plan=False) as rt:
        recover(rt)
        manager = controller(rt)
        preview = prepare(rt, manager)
        rt.broker.queue_submit(
            SubmitScenario(
                SubmitOutcome.FULL_FILL,
                partial_fill_quantity=Quantity(100),
                fill_price=Price.parse("10"),
                fill_notional=Money.parse("1000"),
            )
        )
        result = manager.confirm(session(), preview["preview_id"])
        settle(rt, lambda s: bool(s.lots))
        broker = rt.broker
    with open_offline_engine(tmp_path, broker=broker, preload_plan=False) as restarted:
        recover(restarted)
        assert restarted.kernel.snapshot().halted and restarted.kernel.snapshot().grant_id is None
        assert restarted.kernel.snapshot().lots[0].remaining == 100
        assert (
            controller(restarted).result(session(), result["client_order_id"])["status"]
            == "RECOVERED"
        )
        assert restarted.kernel._manual_permits == {}
        assert len(broker.query_orders()) == 1


def test_manual_holdings_are_sealed_without_continuous_auto_buy(tmp_path: Path) -> None:
    now = [DEMO_NOW]
    sealed: list[Any] = []
    with open_offline_engine(tmp_path, preload_plan=False, clock=lambda: now[0]) as rt:
        recover(rt)
        manager = controller(rt)
        manager.save_opening = sealed.append
        preview = prepare(rt, manager)
        rt.broker.queue_submit(
            SubmitScenario(
                SubmitOutcome.FULL_FILL,
                partial_fill_quantity=Quantity(100),
                fill_price=Price.parse("10"),
                fill_notional=Money.parse("1000"),
            )
        )
        manager.confirm(session(), preview["preview_id"])
        settle(rt, lambda s: bool(s.lots) and rt.kernel.critical_idle)
        now[0] = DEMO_NOW.replace(hour=15, minute=1)
        until = monotonic() + 3
        while not sealed and monotonic() < until:
            rt.kernel.poll()
            manager.poll()
            sleep(0.001)
        assert len(sealed) == 1 and sealed[0].trade_date == DEMO_NOW.date()
        assert rt.kernel.snapshot().halted and rt.kernel._sealed
        from auto_trading_system.journal.checkpoint_blobs import read_checkpoint
        from auto_trading_system.reconcile.portfolio_checkpoint import load_opening

        body = rt.journal.request(
            lambda store: read_checkpoint(store, sealed[0].key), deadline=monotonic() + 1
        ).result(2)
        checkpoint = load_opening(
            body,
            sealed[0],
            account_id=rt.kernel.account_id,
            trade_date=DEMO_NOW.date() + timedelta(days=1),
            calendar=rt.kernel._calendar,
        )
        assert checkpoint.lots[0].remaining == 100
        assert checkpoint.lots[0].eligible_sell_date == DEMO_NOW.date() + timedelta(days=1)


def test_expired_unsent_confirmation_can_be_cleared_only_after_checked_query(
    tmp_path: Path,
) -> None:
    with open_offline_engine(tmp_path, preload_plan=False) as rt:
        recover(rt)
        manager = controller(rt)
        preview = prepare(rt, manager)
        with pytest.raises(IPCError, match="RESULT_UNKNOWN"):
            manager.result(session(), preview["client_order_id"])
        manager.previews[preview["preview_id"]].expires = 0
        assert manager.result(session(), preview["client_order_id"])["status"] == "NOT_SUBMITTED"
        assert rt.broker.query_orders() == ()


def test_authorization_worker_rechecks_session_and_only_confirms_frozen_purchase(
    tmp_path: Path,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from time import time_ns
    from uuid import uuid4

    from pydantic import SecretStr

    from auto_trading_system.control.ipc_protocol import Request
    from auto_trading_system.control.node_authorization import NodeAuthorization
    from auto_trading_system.control.owner_mailbox import OwnerMailbox
    from auto_trading_system.domain.clock import SystemClock
    from auto_trading_system.runtime.node_engine import _Clock
    from auto_trading_system.security.operator_password import OperatorPasswordHasher
    from auto_trading_system.security.operator_session import LocalOperatorSessionManager

    with open_offline_engine(tmp_path, preload_plan=False) as rt, ThreadPoolExecutor(1) as pool:
        recover(rt)
        manager = controller(rt)
        mailbox: OwnerMailbox[object] = OwnerMailbox()
        password = "offline-manual-test-password-123"
        sessions = LocalOperatorSessionManager(
            SecretStr(OperatorPasswordHasher().hash_password(password)),
            actor_id="test-operator",
            clock=_Clock(rt.clock),
            monotonic_clock=SystemClock(),
        )
        auth = NodeAuthorization(rt.kernel, mailbox, sessions, clock=rt.clock, manual_buy=manager)

        def run(action: str, payload: dict[str, Any]) -> Any:
            request = Request(
                request_id="test-" + uuid4().hex,
                engine_boot_id=rt.kernel.boot_id,
                sender_role="operator",
                deadline_ms=time_ns() // 1_000_000 + 8000,
                action=action,
                payload=payload,
            )
            future = pool.submit(auth.handle, request)
            until = monotonic() + 5
            while not future.done() and monotonic() < until:
                rt.kernel.poll()
                mailbox.poll()
                sleep(0.001)
            return future.result(1)

        issued = run("LOGIN", {"password": password})
        proofs = {k: issued[k] for k in ("session_token", "csrf_token")}
        data = {"symbol": "000001", "limit_price": "10", "quantity": 100}
        with pytest.raises(IPCError):
            run("PREVIEW_MANUAL_BUY", {**proofs, **data, "csrf_token": "wrong"})
        preview = run("PREVIEW_MANUAL_BUY", {**proofs, **data})
        assert rt.broker.query_orders() == ()
        with pytest.raises(ValueError):
            run(
                "CONFIRM_MANUAL_BUY",
                {**proofs, "preview_id": preview["preview_id"], "quantity": 200},
            )
        confirmed = run("CONFIRM_MANUAL_BUY", {**proofs, "preview_id": preview["preview_id"]})
        settle(rt, lambda s: s.submissions[0].status == "SDK_ACCEPTED")
        assert (
            run("MANUAL_BUY_RESULT", {**proofs, "client_order_id": confirmed["client_order_id"]})[
                "status"
            ]
            == "SDK_ACCEPTED"
        )
        assert len(rt.broker.query_orders()) == 1 and rt.kernel.snapshot().halted
        mailbox.close()


@pytest.mark.parametrize("filled", [0, 40])
def test_operator_cancel_reports_pending_and_preserves_manual_fills(
    tmp_path: Path, filled: int
) -> None:
    with open_offline_engine(tmp_path, preload_plan=False) as rt:
        recover(rt)
        manager = controller(rt)
        preview = prepare(rt, manager)
        if filled:
            rt.broker.queue_submit(
                SubmitScenario(
                    SubmitOutcome.PARTIAL_FILL,
                    partial_fill_quantity=Quantity(filled),
                    fill_price=Price.parse("10"),
                    fill_notional=Money.parse(str(filled * 10)),
                )
            )
        result = manager.confirm(session(), preview["preview_id"])
        settle(rt, lambda s: s.submissions[0].status == "SDK_ACCEPTED")
        rt.kernel._next_observation = 0
        settle(
            rt,
            lambda _: rt.kernel.critical_idle
            and rt.kernel._broker_snapshot is not None
            and rt.kernel._broker_snapshot.event_version == rt.kernel._events(),
        )
        before: Any = rt.kernel.operator_trade_details()["orders"]
        assert before[0]["cancelable"] and not before[0]["cancel_requested"]
        assert before[0]["remaining"] == 100 - filled

        rt.kernel.operator_cancel_order(result["client_order_id"])
        # The page may still be observing the pre-cancel broker snapshot.
        pending: Any = rt.kernel.operator_trade_details()["orders"]
        assert not pending[0]["cancelable"] and pending[0]["cancel_requested"]
        with pytest.raises(ValueError):
            rt.kernel.operator_cancel_order(result["client_order_id"])
        assert sum(s.action_type == "CANCEL" for s in rt.kernel._known.values()) == 1

        rt.kernel._next_observation = 0
        settle(
            rt,
            lambda _: rt.kernel.critical_idle
            and rt.kernel._broker_snapshot is not None
            and rt.kernel._broker_snapshot.orders[0].is_terminal,
        )
        after: Any = rt.kernel.operator_trade_details()["orders"]
        assert not after[0]["cancelable"] and not after[0]["cancel_requested"]
        assert after[0]["filled"] == filled
        assert after[0]["status"] == ("PARTIALLY_FILLED_CANCELLED" if filled else "CANCELLED")
        assert sum(lot.remaining for lot in rt.kernel.snapshot().lots) == filled
        assert rt.kernel.snapshot().halted and rt.kernel.snapshot().grant_id is None


def test_zero_fill_cancel_does_not_hand_stock_back_to_auto_and_manual_can_retry(
    tmp_path: Path,
) -> None:
    with open_offline_engine(tmp_path, preload_plan=False, max_orders=2) as rt:
        recover(rt)
        manager = controller(rt)
        preview = prepare(rt, manager)
        result = manager.confirm(session(), preview["preview_id"])
        settle(rt, lambda s: s.submissions[0].status == "SDK_ACCEPTED")
        rt.kernel._next_observation = 0
        settle(
            rt,
            lambda _: rt.kernel.critical_idle
            and rt.kernel._broker_snapshot is not None
            and rt.kernel._broker_snapshot.event_version == rt.kernel._events(),
        )
        rt.kernel.operator_cancel_order(result["client_order_id"])
        rt.kernel._next_observation = 0
        settle(
            rt, lambda _: rt.kernel.critical_idle and rt.kernel.entry_exposures()[0].quantity == 0
        )
        assert rt.kernel.manual_symbol_claimed("000001.SZ")
        retry = prepare(rt, manager)
        manager.confirm(session(), retry["preview_id"])
        settle(rt, lambda s: s.submissions[-1].status == "SDK_ACCEPTED")
        assert len(rt.broker.query_orders()) == 2


@pytest.mark.parametrize(
    "quantity,price", [(150, "10"), (100, "10.001"), (100, "8.99"), (100, "11.01")]
)
def test_verified_quantity_step_tick_and_price_limits_are_required(
    tmp_path: Path, quantity: int, price: str
) -> None:
    with open_offline_engine(tmp_path, preload_plan=False) as rt:
        recover(rt)
        manager = controller(rt)
        value = ManualBuyInput(symbol="000001", limit_price=price, quantity=quantity)
        facts = manager.query(value).result(2)
        with pytest.raises(ValueError):
            manager.preview(session(), value, facts)
        assert rt.broker.query_orders() == ()


def test_dashboard_metadata_stays_inside_authenticated_rpc_frame(tmp_path: Path) -> None:
    from dataclasses import replace

    from auto_trading_system.protocol.v2.canonical import canonical_bytes

    with open_offline_engine(tmp_path, preload_plan=False) as rt:
        recover(rt)
        manager = controller(rt)
        preview = prepare(rt, manager)
        item = manager.previews[preview["preview_id"]].item
        rt.kernel._known = {
            f"ord_{n:020}": item.model_copy(update={"client_order_id": f"ord_{n:020}"})
            for n in range(120)
        }
        observation = rt.kernel._broker_snapshot
        assert observation is not None
        observation = replace(
            observation, display_names=tuple((f"{n:06}.SZ", "股" * 64) for n in range(500))
        )
        details = rt.kernel.operator_trade_details(observation)
        assert details["truncated"] is True
        assert len(canonical_bytes(details)) <= 48 * 1024
        assert rt.broker.query_orders() == ()
        rt.kernel._known.clear()
