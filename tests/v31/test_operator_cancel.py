"""Cancel-only authority and durable outcomes, using only the offline broker."""

from concurrent.futures import Future
from pathlib import Path
from time import monotonic
from typing import Any, cast

import pytest
from test_engine_v2 import recover as recover_auto
from test_manual_buy import controller, prepare, recover, session, settle

from auto_trading_system.apps.trade_runtime.engine_simulation import (
    DEMO_NOW,
    open_offline_engine,
    wait_for,
)
from auto_trading_system.apps.trade_runtime.entry_simulation import (
    open_entry_owner,
    publish_entry,
    pump_entry,
)
from auto_trading_system.apps.trade_runtime.exit_owner_simulation import ExitScenarioClock
from auto_trading_system.domain import ShanghaiTimestamp
from auto_trading_system.gateways.broker.mock import (
    CancelOutcome,
    CancelScenario,
    MockBrokerGateway,
)
from auto_trading_system.gateways.broker.models import BrokerCancelError


def buy(rt: Any) -> str:
    recover(rt)
    manager = controller(rt)
    preview = prepare(rt, manager)
    identity = manager.confirm(session(), preview["preview_id"])["client_order_id"]
    settle(rt, lambda s: s.submissions[0].status == "SDK_ACCEPTED")
    rt.kernel._next_observation = 0
    settle(
        rt,
        lambda _: rt.kernel.critical_idle
        and rt.kernel._broker_snapshot is not None
        and rt.kernel._broker_snapshot.event_version == rt.kernel._events(),
    )
    return str(identity)


def cancel(rt: Any, identity: str) -> dict[str, Any]:
    observed = rt.kernel.request_cancel_snapshot(identity).result(2)
    return cast(dict[str, Any], rt.kernel.operator_cancel_order(identity, observed))


def row(rt: Any) -> dict[str, Any]:
    settle(rt, lambda _: rt.kernel.dispatcher.idle)
    observed = rt.kernel.dispatcher.observe(rt.kernel._capture).result(2)
    return cast(dict[str, Any], rt.kernel.operator_trade_details(observed)["orders"][0])


def test_paused_automatic_order_can_cancel_with_fresh_read_without_buy_authority(
    tmp_path: Path,
) -> None:
    clock = ExitScenarioClock(DEMO_NOW)
    with open_offline_engine(tmp_path, clock=lambda: clock.instant) as rt:
        recover_auto(rt)
        rt.confirmed_arm()
        item = rt.buy()
        rt.kernel.submit(item, valid_until=monotonic() + 1)
        wait_for(rt.kernel, lambda s: bool(s.orders) and s.orders[0].phase == "ACTIVE")
        wait_for(rt.kernel, lambda _: rt.kernel.critical_idle)
        rt.kernel.emergency_halt("OPERATOR_PAUSED")
        rt.kernel.poll()
        clock.advance(10)
        # Limits on new buys must not strand a larger existing order.
        rt.kernel.limits = rt.kernel.limits.model_copy(
            update={"max_order_quantity": 1, "max_order_value": "1"}
        )
        cancel(rt, item.client_order_id)
        wait_for(rt.kernel, lambda s: s.orders[0].phase == "TERMINAL_CONFIRMED")
        assert rt.broker.query_orders()[0].status.value == "CANCELLED"
        assert rt.kernel.snapshot().halted and rt.kernel.snapshot().grant_id is None
        assert len(rt.broker.query_orders()) == 1


def test_rejected_cancel_is_durable_and_can_retry_after_fresh_query(tmp_path: Path) -> None:
    with open_offline_engine(tmp_path, preload_plan=False) as rt:
        identity = buy(rt)
        rt.broker.queue_cancel(CancelScenario(CancelOutcome.REJECT))
        first = cancel(rt, identity)["cancel_request_id"]
        settle(rt, lambda _: first in rt.kernel._cancel_failures and rt.kernel.critical_idle)
        assert row(rt)["cancel_state"] == "FAILED"
        assert row(rt)["cancelable"] and not row(rt)["cancel_requested"]
        broker = rt.broker
    with open_offline_engine(tmp_path, broker=broker, preload_plan=False) as restarted:
        restarted.kernel.start_recovery()
        settle(restarted, lambda s: s.state == "RECOVERY_BLOCKED")
        assert first in restarted.kernel._cancel_failures
        assert row(restarted)["cancel_state"] == "FAILED"
        result = cancel(restarted, identity)
        assert result["cancel_request_id"] != first
        settle(restarted, lambda s: s.orders[0].phase == "TERMINAL_CONFIRMED")
        assert row(restarted)["cancel_state"] == "CONFIRMED"
        assert not row(restarted)["cancelable"]
        assert restarted.kernel.snapshot().halted and restarted.kernel.snapshot().grant_id is None


def test_unknown_cancel_never_retries_and_a_late_old_failure_cannot_release_it(
    tmp_path: Path,
) -> None:
    with open_offline_engine(tmp_path, preload_plan=False) as rt:
        identity = buy(rt)
        rt.broker.queue_cancel(CancelScenario(CancelOutcome.REJECT))
        first = cancel(rt, identity)["cancel_request_id"]
        settle(rt, lambda _: first in rt.kernel._cancel_failures and rt.kernel.critical_idle)
        rt.broker.queue_cancel(CancelScenario(CancelOutcome.TIMEOUT_BEFORE_APPLY))
        second = cancel(rt, identity)["cancel_request_id"]
        settle(rt, lambda s: s.submissions[-1].status == "UNCERTAIN" and rt.kernel.critical_idle)
        broker_id = rt.broker.query_orders()[0].broker_order_id
        assert broker_id is not None
        rt.kernel.broker_event(
            BrokerCancelError(
                first,
                broker_id,
                "MOCK_LATE_CANCEL_ERROR",
                "late",
                ShanghaiTimestamp(DEMO_NOW),
            )
        )
        rt.kernel.poll()
        assert second not in rt.kernel._cancel_failures
        assert row(rt)["cancel_state"] == "UNKNOWN" and not row(rt)["cancelable"]
        with pytest.raises(ValueError, match="ORDER_NOT_CANCELABLE"):
            cancel(rt, identity)
        assert len([s for s in rt.kernel._known.values() if s.action_type == "CANCEL"]) == 2


def test_halt_fences_pending_cancel_and_proven_unsent_can_retry(tmp_path: Path) -> None:
    with open_offline_engine(tmp_path, preload_plan=False) as rt:
        identity = buy(rt)
        first = cancel(rt, identity)["cancel_request_id"]
        rt.kernel.emergency_halt("AUDIT_HALT_BEFORE_DISPATCH")
        settle(rt, lambda _: first in rt.kernel._cancel_failures and rt.kernel.critical_idle)
        assert rt.broker.query_orders()[0].status.value == "ACKED"
        assert row(rt)["cancel_state"] == "FAILED"
        assert row(rt)["cancel_reason"] == "CANCEL_NOT_DISPATCHED"
        cancel(rt, identity)
        settle(rt, lambda s: s.orders[0].phase == "TERMINAL_CONFIRMED")


def test_operator_cancel_rejects_stale_observation_and_external_identity(tmp_path: Path) -> None:
    clock = ExitScenarioClock(DEMO_NOW)
    with open_offline_engine(tmp_path, preload_plan=False, clock=lambda: clock.instant) as rt:
        identity = buy(rt)
        observed = rt.kernel.request_cancel_snapshot(identity).result(2)
        clock.advance(6)
        with pytest.raises(ValueError):
            rt.kernel.operator_cancel_order(identity, observed)
        with pytest.raises(ValueError, match="ORDER_NOT_CANCELABLE"):
            rt.kernel.request_cancel_snapshot("ord_external0001")
        assert not any(s.action_type == "CANCEL" for s in rt.kernel._known.values())


def test_operator_cancel_keeps_existing_automatic_replacement_behavior(tmp_path: Path) -> None:
    clock = ExitScenarioClock(DEMO_NOW)
    broker = MockBrokerGateway(clock)
    with open_entry_owner(tmp_path, clock, broker) as (rt, owner):
        rt.confirmed_arm()
        publish_entry(owner, clock, 1)
        pump_entry(owner, lambda: len(broker.query_orders()) == 1 and rt.kernel.critical_idle)
        identity = broker.query_orders()[0].client_order_id
        cancel(rt, identity)
        wait_for(rt.kernel, lambda s: s.orders[0].phase == "TERMINAL_CONFIRMED")
        rt.kernel._next_observation = 0
        wait_for(
            rt.kernel,
            lambda _: rt.kernel.critical_idle
            and rt.kernel._broker_snapshot is not None
            and rt.kernel._broker_snapshot.event_version == rt.kernel._events(),
        )
        clock.advance(1)
        publish_entry(owner, clock, 2)
        pump_entry(owner, lambda: len(broker.query_orders()) == 2)
        assert [o.status.value for o in broker.query_orders()] == ["CANCELLED", "ACKED"]


def test_failed_outcome_write_keeps_cancel_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with open_offline_engine(tmp_path, preload_plan=False) as rt:
        identity = buy(rt)
        failed: Future[bool] = Future()
        failed.set_exception(OSError("offline write failure"))
        rt.broker.queue_cancel(CancelScenario(CancelOutcome.REJECT))
        request_id = cancel(rt, identity)["cancel_request_id"]
        settle(rt, lambda s: s.submissions[-1].status in {"DISPATCHING", "SDK_ACCEPTED"})
        monkeypatch.setattr(rt.journal, "request", lambda *a, **k: failed)
        settle(
            rt,
            lambda _: request_id in rt.kernel._cancel_failure_attempted
            and request_id not in rt.kernel._cancel_failure_pending,
        )
        assert request_id not in rt.kernel._cancel_failures
        assert row(rt)["cancel_state"] == "UNKNOWN" and not row(rt)["cancelable"]
        with pytest.raises(ValueError, match="ORDER_NOT_CANCELABLE"):
            cancel(rt, identity)


def test_tampered_failure_binding_blocks_recovery(tmp_path: Path) -> None:
    from auto_trading_system.protocol.v2.canonical import canonical_bytes
    from auto_trading_system.runtime.cancel_state import CancelFailure, failure_key

    with open_offline_engine(tmp_path, preload_plan=False) as rt:
        identity = buy(rt)
        rt.broker.queue_cancel(CancelScenario(CancelOutcome.TIMEOUT_BEFORE_APPLY))
        request_id = cancel(rt, identity)["cancel_request_id"]
        settle(rt, lambda s: s.submissions[-1].status == "UNCERTAIN" and rt.kernel.critical_idle)
        forged = CancelFailure(
            cancel_request_id=request_id,
            submission_hash="sha256:" + "0" * 64,
            reason_code="MOCK_REJECTED",
            occurred_at=DEMO_NOW,
        )
        rt.journal.request(
            lambda store: store.append_evidence(
                "broker_facts",
                failure_key(request_id),
                canonical_bytes(forged.model_dump(mode="json")),
            ),
            deadline=monotonic() + 2,
        ).result(2)
        broker = rt.broker
    with open_offline_engine(tmp_path, broker=broker, preload_plan=False) as restarted:
        restarted.kernel.start_recovery()
        settle(restarted, lambda s: s.state == "HALTED")
        assert restarted.kernel.recovery_error() is not None
        assert not restarted.kernel._cancel_failures
