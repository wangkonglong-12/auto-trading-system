from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Event, get_ident
from time import monotonic, sleep
from typing import Literal

import pytest

from auto_trading_system.apps.trade_runtime.__main__ import main
from auto_trading_system.apps.trade_runtime.engine_simulation import (
    DEMO_NOW,
    OfflineEngine,
    open_offline_engine,
    wait_for,
)
from auto_trading_system.control.approval_v2 import OneShotApprovals
from auto_trading_system.domain import Price, Quantity, SecurityCode, ShanghaiTimestamp
from auto_trading_system.gateways.broker.mock import (
    BrokerQuery,
    QueryFault,
    SubmitOutcome,
    SubmitScenario,
)
from auto_trading_system.gateways.broker.models import (
    BrokerCancelError,
    BrokerOrder,
    BrokerOrderStatus,
    BrokerOrderUpdate,
    OrderSide,
)
from auto_trading_system.journal.apsw_store import APSWCriticalStore
from auto_trading_system.journal.critical_models import CriticalStoreError
from auto_trading_system.journal.critical_writer import CriticalJournalOwner
from auto_trading_system.protocol.v2.canonical import parse_bytes
from auto_trading_system.protocol.v2.daily_sync import LocalControlRequest, SessionGrant
from auto_trading_system.runtime.engine_v2 import EngineSnapshot
from auto_trading_system.runtime.memory_state import MarketWindow, QuotePoint
from auto_trading_system.runtime.recovery_v2 import AccountFact


def recover(runtime: OfflineEngine) -> None:
    runtime.kernel.start_recovery()
    wait_for(runtime.kernel, lambda s: s.state in ("REVIEW_REQUIRED", "RECOVERY_BLOCKED"))


def confirm(runtime: OfflineEngine) -> LocalControlRequest:
    recover(runtime)
    report = runtime.kernel.snapshot().recovery
    assert report
    request = runtime.request("CONFIRM_RECOVERY", artifact_hash=report.payload_hash)
    runtime.approvals.approve(request, authenticated_actor="operator")
    assert runtime.kernel.control(request).status == "PENDING"
    wait_for(runtime.kernel, lambda s: s.state == "READY_PAUSED")
    assert runtime.kernel.control(request).status == "APPLIED"
    return request


def grant(runtime: OfflineEngine, **updates: object) -> SessionGrant:
    report = runtime.kernel.snapshot().recovery
    assert report
    value = SessionGrant(
        grant_id="grant-test",
        account_id=runtime.kernel.account_id,
        engine_boot_id=runtime.kernel.boot_id,
        session_generation=1,
        mode="PAPER",
        directions=("BUY",),
        max_buy_value="1005",
        max_order_value="1005",
        max_positions=1,
        valid_from=runtime.clock(),
        valid_until=runtime.clock() + timedelta(minutes=1),
        confirmed_by="operator",
        recovery_report_hash=report.payload_hash,
    )
    return value.model_copy(update=updates)


def wait_even_halted(
    runtime: OfflineEngine, predicate: Callable[[EngineSnapshot], bool]
) -> EngineSnapshot:
    deadline = monotonic() + 3
    while monotonic() < deadline:
        state = runtime.kernel.poll()
        if predicate(state):
            return state
        sleep(0.001)
    raise AssertionError("state did not settle")


def test_managed_engine_cli_and_installed_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--simulate-engine", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["submission_records"] == 2 and result["filled_quantity"] == 50
    assert result["restart_paused"] and result["old_grant_not_restored"]
    assert not result["real_trading_enabled"]
    output = subprocess.run(
        [
            sys.executable,
            "-m",
            "auto_trading_system.apps.trade_runtime",
            "--simulate-engine",
            str(tmp_path),
        ],
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    assert json.loads(output.stdout)["recovery_status"] == "CLEAR"


def test_recovery_confirmation_and_arm_are_durable_distinct_and_idempotent(tmp_path: Path) -> None:
    with open_offline_engine(tmp_path) as rt:
        request = confirm(rt)
        assert rt.kernel.snapshot().halted
        saved = rt.journal.request(
            lambda store: store.read_evidence("control_records", request.request_id),
            deadline=monotonic() + 1,
        ).result(2)
        assert saved and b'"actor":"operator"' in saved
        arm = rt.request("ARM", grant=grant(rt))
        assert rt.kernel.control(arm).status == "REJECTED"  # A role string is not approval.
        arm = rt.request("ARM", grant=grant(rt))
        rt.approvals.approve(arm, authenticated_actor="operator")
        assert rt.kernel.control(arm).status == "PENDING"
        assert rt.kernel.snapshot().halted
        wait_for(rt.kernel, lambda s: s.state == "ARMED")
        receipt = rt.kernel.control(arm)
        assert receipt.status == "APPLIED" and not rt.kernel.snapshot().halted
        assert rt.kernel.control(arm) == receipt
        with pytest.raises(ValueError, match="content conflict"):
            rt.kernel.control(arm.model_copy(update={"expected_state_version": 999}))
        checkpoint = rt.kernel.snapshot().recovery
        assert checkpoint
        data = rt.journal.request(
            lambda store: store.read_evidence("recovery_checkpoints", checkpoint.report_id),
            deadline=monotonic() + 1,
        ).result(2)
        assert data and parse_bytes(data)


@pytest.mark.parametrize(
    "mutation",
    [
        {"account_id": "wrong"},
        {"mode": "LIVE"},
        {"max_buy_value": "1006"},
        {"max_order_value": "1006"},
        {"max_positions": 2},
        {"recovery_report_hash": "sha256:" + "f" * 64},
        {"valid_until": DEMO_NOW + timedelta(hours=1)},
        {"valid_from": DEMO_NOW + timedelta(seconds=1)},
    ],
)
def test_grant_cannot_expand_installed_or_plan_ceiling(
    tmp_path: Path, mutation: dict[str, object]
) -> None:
    with open_offline_engine(tmp_path) as rt:
        confirm(rt)
        arm = rt.request("ARM", grant=grant(rt, **mutation))
        rt.approvals.approve(arm, authenticated_actor="operator")
        assert rt.kernel.control(arm).status == "REJECTED"
        assert rt.kernel.snapshot().halted and rt.broker.query_orders() == ()


@pytest.mark.parametrize("failure", ["HALT", "TIMEOUT", "DISK", "EVENT"])
def test_authorization_commit_failure_or_late_success_never_reopens(
    tmp_path: Path,
    failure: str,
) -> None:
    entered, release = Event(), Event()

    class DelayedGrantStore(APSWCriticalStore):
        def append_evidence(
            self,
            table: Literal["broker_facts", "control_records", "recovery_checkpoints"],
            key: str,
            body: bytes,
        ) -> bool:
            if b'"action":"ARM"' in body:
                entered.set()
                assert release.wait(3)
                if failure == "DISK":
                    raise OSError("injected grant I/O failure")
            return super().append_evidence(table, key, body)

    def journal(path: Path, account: str) -> CriticalJournalOwner:
        owner = CriticalJournalOwner(lambda: DelayedGrantStore(path, account))
        return owner

    with open_offline_engine(tmp_path, journal_factory=journal) as rt:
        confirm(rt)
        arm = rt.request("ARM", grant=grant(rt))
        rt.approvals.approve(arm, authenticated_actor="operator")
        assert rt.kernel.control(arm).status == "PENDING"
        try:
            assert entered.wait(1)
            market = MarketWindow()
            for seq in range(1, 1001):
                market.add(QuotePoint(seq, seq, Price.parse("10")))
            assert market.snapshot().state_version == 1000
            if failure == "HALT":
                rt.kernel.emergency_halt()
            elif failure == "TIMEOUT":
                wait_even_halted(rt, lambda s: s.state == "HALTED")
            elif failure == "EVENT":
                rt.kernel.broker_event(example_order(rt))
            release.set()
            state = wait_even_halted(rt, lambda s: s.state == "HALTED")
            assert state.grant_id is None and state.halted
            assert rt.kernel.control(arm).status == "REJECTED"
            assert rt.broker.query_orders() == ()
        finally:
            release.set()


def example_order(rt: OfflineEngine) -> BrokerOrderUpdate:
    return BrokerOrderUpdate(
        BrokerOrder(
            client_order_id="ord_external001",
            broker_order_id="external001",
            symbol=SecurityCode.parse("000001.SZ"),
            side=OrderSide.BUY,
            status=BrokerOrderStatus.ACKED,
            order_quantity=Quantity(100),
            filled_quantity=Quantity(0),
            remaining_quantity=Quantity(100),
            limit_price=Price.parse("10"),
            broker_status_code=1,
            status_reason="TEST_EXTERNAL",
            updated_at=ShanghaiTimestamp.from_instant(rt.clock()),
        )
    )


def test_missing_broker_order_is_unknown_not_permission_to_replay(tmp_path: Path) -> None:
    with open_offline_engine(tmp_path) as rt:
        order = rt.buy()
        rt.journal.commit(order, deadline=monotonic() + 1).result(2)
        recover(rt)
        report = rt.kernel.snapshot().recovery
        assert report and report.status == "BLOCKED"
        assert "ORDER_NOT_OBSERVED_IS_NOT_PROOF" in report.issue_codes
        request = rt.request("CONFIRM_RECOVERY", artifact_hash=report.payload_hash)
        rt.approvals.approve(request, authenticated_actor="operator")
        assert rt.kernel.control(request).status == "REJECTED"
        with pytest.raises(ValueError, match="cannot be replayed"):
            rt.kernel.submit(order, valid_until=monotonic() + 1)
        assert rt.broker.query_orders() == ()


def test_sdk_uncertain_order_recovery_stays_blocked_until_terminal_evidence(tmp_path: Path) -> None:
    with open_offline_engine(tmp_path) as rt:
        recover(rt)
        rt.confirmed_arm()
        rt.broker.queue_submit(SubmitScenario(outcome=SubmitOutcome.TIMEOUT_AFTER_ACCEPT))
        rt.kernel.submit(rt.buy(), valid_until=monotonic() + 1)
        wait_even_halted(
            rt,
            lambda s: s.halted and bool(s.submissions) and s.submissions[0].status == "UNCERTAIN",
        )
        recover(rt)
        report = rt.kernel.snapshot().recovery
        assert report and report.status == "BLOCKED"
        assert "ORDER_ACTIVE_OR_TRADE_MISMATCH" in report.issue_codes
        assert len(rt.broker.query_orders()) == 1


@pytest.mark.parametrize("query", [BrokerQuery.ORDERS, BrokerQuery.TRADES])
def test_unavailable_broker_query_never_becomes_empty_success(
    tmp_path: Path, query: BrokerQuery
) -> None:
    with open_offline_engine(tmp_path) as rt:
        rt.broker.queue_query_fault(query, QueryFault.FAILURE)
        rt.kernel.start_recovery()
        state = wait_even_halted(rt, lambda s: s.state == "HALTED")
        assert state.recovery is None and state.halted


def test_stale_control_approval_and_observation_are_rejected(tmp_path: Path) -> None:
    now = [DEMO_NOW]
    with open_offline_engine(tmp_path, clock=lambda: now[0]) as rt:
        confirm(rt)
        request = rt.request("ARM", grant=grant(rt))
        stale = request.model_copy(
            update={"expected_state_version": 0, "request_id": "stale-request"}
        )
        assert rt.kernel.control(stale).reason_code == "STALE_OR_PENDING_CONTROL"
        old_boot = rt.request("SNAPSHOT").model_copy(update={"engine_boot_id": "old-boot"})
        assert rt.kernel.control(old_boot).reason_code == "WRONG_ENGINE_BOOT"
        expired = rt.request("ARM", grant=grant(rt)).model_copy(update={"deadline": DEMO_NOW})
        assert rt.kernel.control(expired).status == "REJECTED"
        rt.approvals.approve(request, authenticated_actor="operator")
        changed = request.model_copy(
            update={"deadline": DEMO_NOW + timedelta(seconds=9), "request_id": "changed-request"}
        )
        assert rt.kernel.control(changed).status == "REJECTED"
        assert rt.kernel.control(request).status == "REJECTED"
        now[0] += timedelta(seconds=31)
        assert rt.kernel.control(rt.request("ARM", grant=grant(rt))).status == "REJECTED"


@pytest.mark.parametrize("change", ["EXPIRE", "CLOCK_BACK", "DAY_CHANGE"])
def test_clock_and_session_end_revoke_permissions(tmp_path: Path, change: str) -> None:
    now = [DEMO_NOW]
    with open_offline_engine(tmp_path, clock=lambda: now[0]) as rt:
        recover(rt)
        rt.confirmed_arm()
        now[0] += {
            "EXPIRE": timedelta(minutes=6),
            "CLOCK_BACK": timedelta(seconds=-1),
            "DAY_CHANGE": timedelta(days=1),
        }[change]
        state = rt.kernel.poll()
        assert state.halted and state.grant_id is None
        with pytest.raises(ValueError):
            rt.kernel.submit(rt.buy(), valid_until=monotonic() + 1)


@pytest.mark.parametrize(
    "mutation",
    [
        {"symbol": "000002.SZ"},
        {"quantity": 101, "maximum_amount": "1015"},
        {"side": "SELL"},
        {"plan_hash": "sha256:" + "e" * 64},
        {"engine_boot_id": "other-boot"},
        {"permission_epoch": 1},
        {"account_id": "other-account"},
        {"limit_price": "9.99"},
    ],
)
def test_submission_uses_local_grant_plan_and_instrument_guard(
    tmp_path: Path, mutation: dict[str, object]
) -> None:
    with open_offline_engine(tmp_path) as rt:
        recover(rt)
        rt.confirmed_arm()
        with pytest.raises(ValueError):
            rt.kernel.submit(rt.buy().model_copy(update=mutation), valid_until=monotonic() + 1)
        assert rt.broker.query_orders() == ()


def test_cancel_cannot_target_another_broker_order(tmp_path: Path) -> None:
    with open_offline_engine(tmp_path) as rt:
        recover(rt)
        rt.confirmed_arm()
        order = rt.buy()
        rt.kernel.submit(order, valid_until=monotonic() + 1)
        wait_for(
            rt.kernel, lambda s: bool(s.submissions) and s.submissions[0].status == "SDK_ACCEPTED"
        )
        wait_for(rt.kernel, lambda _: rt.kernel.critical_idle)
        cancel = order.model_copy(
            update={
                "client_order_id": "ord_wrongcancel01",
                "action_type": "CANCEL",
                "original_client_order_id": order.client_order_id,
                "original_order_id": "wrong-order",
            }
        )
        with pytest.raises(ValueError, match="gate rejected"):
            rt.kernel.submit(cancel, valid_until=monotonic() + 1)
        assert len(rt.broker.query_orders()) == 1
        assert not rt.broker.query_orders()[0].is_terminal


def test_operator_cancel_persists_and_cancels_only_owned_active_order(tmp_path: Path) -> None:
    with open_offline_engine(tmp_path) as rt:
        recover(rt)
        rt.confirmed_arm()
        order = rt.buy()
        rt.kernel.submit(order, valid_until=monotonic() + 1)
        wait_for(
            rt.kernel,
            lambda state: bool(state.orders) and state.orders[0].phase == "ACTIVE",
        )
        wait_for(rt.kernel, lambda _: rt.kernel.critical_idle)
        result = rt.kernel.operator_cancel_order(order.client_order_id)
        assert result["status"] == "REQUESTED"
        assert result["original_client_order_id"] == order.client_order_id
        wait_for(
            rt.kernel,
            lambda state: bool(state.orders) and state.orders[0].phase == "TERMINAL_CONFIRMED",
        )
        assert rt.broker.query_orders()[0].status is BrokerOrderStatus.CANCELLED
        with pytest.raises(ValueError, match="ORDER_NOT_CANCELABLE"):
            rt.kernel.operator_cancel_order(order.client_order_id)


def test_control_and_kernel_state_have_one_owner(tmp_path: Path) -> None:
    with open_offline_engine(tmp_path) as rt:
        request = rt.request("SNAPSHOT")
        with ThreadPoolExecutor(max_workers=1) as worker:
            for call in (
                rt.kernel.poll,
                rt.kernel.snapshot,
                lambda: rt.kernel.control(request),
                rt.kernel.start_recovery,
            ):
                with pytest.raises(RuntimeError, match="owner"):
                    worker.submit(call).result()
        assert rt.kernel.control(request).status == "APPLIED"
        assert rt.kernel.control(rt.request("MAINTENANCE")).reason_code == "ACTION_NOT_IMPLEMENTED"


def test_approval_capacity_actor_boot_and_once_only(tmp_path: Path) -> None:
    with open_offline_engine(tmp_path) as rt:
        confirm(rt)
        approvals = OneShotApprovals(boot_id=rt.kernel.boot_id, clock=rt.clock, capacity=1)
        arm = rt.request("ARM", grant=grant(rt))
        with pytest.raises(ValueError, match="actor"):
            approvals.approve(arm, authenticated_actor="someone-else")
        approvals.approve(arm, authenticated_actor="operator")
        with pytest.raises(ValueError, match="reused"):
            approvals.approve(arm, authenticated_actor="operator")
        assert approvals.consume(arm) == "operator"
        with pytest.raises(ValueError, match="unavailable"):
            approvals.consume(arm)
        with pytest.raises(ValueError):
            OneShotApprovals(boot_id=rt.kernel.boot_id, clock=rt.clock, capacity=0)


def test_evidence_reads_are_owner_only_checked_and_bounded(tmp_path: Path) -> None:
    store = APSWCriticalStore(tmp_path / "journal.db", "test-account")
    store.open()
    try:
        assert store.read_evidence("control_records", "none") is None
        store.append_evidence("control_records", "key", b'{"status":"test"}')
        assert store.read_evidence("control_records", "key") == b'{"status":"test"}'
        with pytest.raises(CriticalStoreError):
            store.read_evidence("control_records", "")
        store._db().execute("UPDATE control_records SET payload_json='{}' WHERE control_id='key'")
        with pytest.raises(CriticalStoreError, match="CORRUPT"):
            store.read_evidence("control_records", "key")
    finally:
        store.close()


def test_sdk_queries_use_one_worker_and_timeout_does_not_free_the_slot(tmp_path: Path) -> None:
    entered, release = Event(), Event()
    with open_offline_engine(tmp_path) as rt:
        owner_thread = get_ident()

        def delayed_query() -> int:
            entered.set()
            assert release.wait(2)
            return get_ident()

        query = rt.kernel.dispatcher.observe(lambda _: delayed_query())
        try:
            assert entered.wait(1)
            assert not rt.kernel.dispatcher.idle
            with pytest.raises(TimeoutError):
                query.result(timeout=0.001)
            with pytest.raises(RuntimeError, match="SDK_BUSY"):
                rt.kernel.dispatcher.observe(lambda _: None).result()
            with pytest.raises(ValueError, match="actual SDK"):
                rt.kernel.start_recovery()
            assert rt.kernel.poll().halted
        finally:
            release.set()
        sdk_thread = query.result(timeout=2)
        assert sdk_thread != owner_thread
        assert rt.kernel.dispatcher.observe(lambda _: get_ident()).result(2) == sdk_thread


def test_recovery_timeout_discards_late_result_and_requires_new_recovery(tmp_path: Path) -> None:
    entered, release = Event(), Event()
    with open_offline_engine(tmp_path) as rt:
        original = rt.kernel._account_reader

        def stalled_account() -> AccountFact:
            entered.set()
            assert release.wait(2)
            return original()

        rt.kernel._account_reader = stalled_account
        rt.kernel.start_recovery()
        try:
            wait_even_halted(rt, lambda _: entered.is_set())
            rt.kernel._recovery_deadline = monotonic() - 1
            assert rt.kernel.poll().state == "HALTED"
            with pytest.raises(ValueError, match="actual SDK"):
                rt.kernel.start_recovery()
        finally:
            release.set()
        wait_even_halted(rt, lambda _: rt.kernel.dispatcher.idle)
        assert rt.kernel.snapshot().recovery is None
        rt.kernel._account_reader = original
        recover(rt)
        assert rt.kernel.snapshot().state == "REVIEW_REQUIRED"


@pytest.mark.parametrize("fault", ["PRICE", "TERMINAL", "CANCEL_ERROR", "OVERFLOW"])
def test_conflicting_or_lost_callbacks_revoke_grant(tmp_path: Path, fault: str) -> None:
    with open_offline_engine(tmp_path) as rt:
        recover(rt)
        rt.confirmed_arm()
        update = example_order(rt)
        rt.kernel.broker_event(update)
        # An unowned callback absent from the authoritative query now causes
        # an earlier ownership halt; it must not be silently dropped.
        wait_even_halted(rt, lambda _: rt.kernel.critical_idle)
        if fault == "PRICE":
            rt.kernel.broker_event(
                BrokerOrderUpdate(replace(update.order, limit_price=Price.parse("11")))
            )
        elif fault == "TERMINAL":
            rt.kernel.broker_event(
                BrokerOrderUpdate(replace(update.order, status=BrokerOrderStatus.CANCELLED))
            )
            wait_even_halted(rt, lambda _: rt.kernel.critical_idle)
            rt.kernel.broker_event(
                BrokerOrderUpdate(
                    replace(
                        update.order,
                        status=BrokerOrderStatus.FILLED,
                        filled_quantity=Quantity(100),
                        remaining_quantity=Quantity(0),
                    )
                )
            )
        elif fault == "CANCEL_ERROR":
            rt.kernel.broker_event(
                BrokerCancelError(
                    update.order.client_order_id,
                    "external001",
                    "TEST_ERROR",
                    "private SDK text",
                    update.order.updated_at,
                )
            )
        else:
            for _ in range(1025):
                rt.kernel.broker_event(update)
        state = rt.kernel.poll()
        assert state.halted and state.grant_id is None
        assert rt.broker.query_orders() == ()


def test_duplicate_and_delayed_callbacks_cannot_reopen_a_terminal_order(tmp_path: Path) -> None:
    with open_offline_engine(tmp_path) as rt:
        update = example_order(rt)
        terminal = BrokerOrderUpdate(replace(update.order, status=BrokerOrderStatus.CANCELLED))
        for event in (update, terminal, update, terminal):
            rt.kernel.broker_event(event)
        wait_even_halted(rt, lambda _: rt.kernel.critical_idle)
        assert rt.kernel._orders[update.order.client_order_id].status == BrokerOrderStatus.CANCELLED
        assert rt.journal.healthy


def test_pause_audit_is_async_and_emergency_survives_reply_capacity(tmp_path: Path) -> None:
    with open_offline_engine(tmp_path) as rt:
        recover(rt)
        rt.confirmed_arm()
        pause = rt.request("PAUSE").model_copy(
            update={"expected_state_version": 0, "deadline": DEMO_NOW}
        )
        assert rt.kernel.control(pause).status == "APPLIED"
        assert rt.kernel.snapshot().halted and rt.kernel.snapshot().grant_id is None
        wait_even_halted(rt, lambda _: rt.kernel.critical_idle)
        saved = rt.journal.request(
            lambda s: s.read_evidence("control_records", pause.request_id), deadline=monotonic() + 1
        ).result(2)
        assert saved
        for _ in range(256 - len(rt.kernel._requests)):
            rt.kernel.control(rt.request("SNAPSHOT"))
        epoch = rt.kernel.snapshot().permission_epoch
        with pytest.raises(ValueError, match="capacity"):
            rt.kernel.control(rt.request("HALT"))
        assert rt.kernel.snapshot().permission_epoch > epoch


def test_recovery_control_and_mismatched_confirmation(tmp_path: Path) -> None:
    with open_offline_engine(tmp_path) as rt:
        assert rt.kernel.control(rt.request("REQUEST_RECOVERY")).status == "APPLIED"
        wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
        wrong = rt.request("CONFIRM_RECOVERY", artifact_hash="sha256:" + "a" * 64)
        rt.approvals.approve(wrong, authenticated_actor="operator")
        assert rt.kernel.control(wrong).status == "REJECTED"


def test_approval_rejects_wrong_boot_time_and_thread(tmp_path: Path) -> None:
    with open_offline_engine(tmp_path) as rt:
        confirm(rt)
        request = rt.request("ARM", grant=grant(rt))
        for change in (
            {"engine_boot_id": "old"},
            {"deadline": DEMO_NOW},
            {"deadline": DEMO_NOW + timedelta(seconds=61)},
        ):
            with pytest.raises(ValueError):
                rt.approvals.approve(
                    request.model_copy(update=change), authenticated_actor="operator"
                )
        with (
            ThreadPoolExecutor(max_workers=1) as worker,
            pytest.raises(RuntimeError, match="owner"),
        ):
            worker.submit(rt.approvals.approve, request, authenticated_actor="operator").result()


def test_failed_journal_start_is_closed_before_a_new_engine_can_start(tmp_path: Path) -> None:
    closed = Event()

    class FailedOpen(APSWCriticalStore):
        def open(self) -> None:
            raise OSError("injected startup failure")

        def close(self) -> None:
            closed.set()
            super().close()

    def factory(path: Path, account: str) -> CriticalJournalOwner:
        return CriticalJournalOwner(lambda: FailedOpen(path, account))

    with (
        pytest.raises(OSError, match="startup"),
        open_offline_engine(tmp_path, journal_factory=factory),
    ):
        pytest.fail("startup should fail")
    assert closed.is_set()
    with open_offline_engine(tmp_path) as rt:
        assert rt.kernel.snapshot().halted


@pytest.mark.parametrize("failure", ["FACT_DISK", "FACT_TIMEOUT", "CHECKPOINT_DISK"])
def test_critical_fact_or_recovery_commit_failure_keeps_permission_closed(
    tmp_path: Path, failure: str
) -> None:
    entered, release = Event(), Event()

    class FailedEvidence(APSWCriticalStore):
        def append_evidence(
            self,
            table: Literal["broker_facts", "control_records", "recovery_checkpoints"],
            key: str,
            body: bytes,
        ) -> bool:
            if table == "recovery_checkpoints" and failure == "CHECKPOINT_DISK":
                raise OSError("injected checkpoint failure")
            if table == "broker_facts":
                entered.set()
                if failure == "FACT_DISK":
                    raise OSError("injected fact failure")
                assert release.wait(2)
            return super().append_evidence(table, key, body)

    def factory(path: Path, account: str) -> CriticalJournalOwner:
        return CriticalJournalOwner(lambda: FailedEvidence(path, account))

    with open_offline_engine(tmp_path, journal_factory=factory) as rt:
        if failure == "CHECKPOINT_DISK":
            rt.kernel.start_recovery()
            state = wait_even_halted(rt, lambda s: s.state == "HALTED")
            assert state.recovery is None
        else:
            recover(rt)
            rt.confirmed_arm()
            try:
                rt.kernel.broker_event(example_order(rt))
                rt.kernel.poll()
                assert entered.wait(1)
                state = wait_even_halted(rt, lambda s: s.state == "HALTED")
                assert state.grant_id is None
                with pytest.raises(ValueError):
                    rt.kernel.submit(rt.buy(), valid_until=monotonic() + 1)
            finally:
                release.set()
        assert rt.kernel.snapshot().halted
        assert rt.broker.query_orders() == ()


def test_control_persistence_excludes_recovery_and_competing_controls(tmp_path: Path) -> None:
    entered, release = Event(), Event()

    class DelayedConfirmation(APSWCriticalStore):
        def append_evidence(
            self,
            table: Literal["broker_facts", "control_records", "recovery_checkpoints"],
            key: str,
            body: bytes,
        ) -> bool:
            if b'"action":"CONFIRM_RECOVERY"' in body:
                entered.set()
                assert release.wait(2)
            return super().append_evidence(table, key, body)

    def factory(path: Path, account: str) -> CriticalJournalOwner:
        return CriticalJournalOwner(lambda: DelayedConfirmation(path, account))

    with open_offline_engine(tmp_path, journal_factory=factory) as rt:
        recover(rt)
        report = rt.kernel.snapshot().recovery
        assert report
        request = rt.request("CONFIRM_RECOVERY", artifact_hash=report.payload_hash)
        rt.approvals.approve(request, authenticated_actor="operator")
        assert rt.kernel.control(request).status == "PENDING"
        try:
            assert entered.wait(1)
            with pytest.raises(ValueError, match="pending"):
                rt.kernel.start_recovery()
            competing = rt.request("CONFIRM_RECOVERY", artifact_hash=report.payload_hash)
            assert rt.kernel.control(competing).status == "REJECTED"
        finally:
            release.set()
        wait_for(rt.kernel, lambda s: s.state == "READY_PAUSED")
