from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Event
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
from auto_trading_system.domain import Money, Price, Quantity
from auto_trading_system.gateways.broker.mock import (
    BrokerQuery,
    EventDeliveryMode,
    QueryFault,
    SubmitOutcome,
    SubmitScenario,
)
from auto_trading_system.gateways.broker.port import BrokerGateway
from auto_trading_system.journal.apsw_store import APSWCriticalStore
from auto_trading_system.journal.critical_models import Submission
from auto_trading_system.journal.critical_writer import CriticalJournalOwner
from auto_trading_system.runtime.engine_v2 import EngineSnapshot
from auto_trading_system.runtime.recovery_v2 import (
    AccountFact,
    BrokerRecoverySnapshot,
    PositionFact,
)


def test_portfolio_cli_rebuilds_lots_without_restoring_old_authority(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--simulate-portfolio", str(tmp_path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["lot_count"] == 2 and output["remaining_shares"] == 100
    assert output["order_count"] == 2 and output["submission_records"] == 4
    assert (
        output["d0_sellable_policy_shares"] == 0
        and output["next_session_sellable_policy_shares"] == 100
    )
    assert output["lot_ids_rebuilt"] and output["query_repaired_missing_callbacks"]
    assert output["restart_paused"] and not output["real_trading_enabled"]


def test_page_account_refresh_reads_new_snapshot_without_granting_or_replacing_recovery(
    tmp_path: Path,
) -> None:
    with open_offline_engine(tmp_path) as rt:
        before = rt.kernel.snapshot()
        snapshot = rt.kernel.request_operator_snapshot().result(timeout=2)
        result = rt.kernel.operator_trade_details(snapshot)
        assert result["available"] is True
        assert result["observed_at"] == snapshot.account.observed_at.isoformat()
        after = rt.kernel.snapshot()
        assert before.halted is True and after.halted is True
        assert before.grant_id is None and after.grant_id is None
        assert before.observation_version == after.observation_version
        assert before.recovery == after.recovery
        for account in (
            snapshot.account.model_copy(update={"account_id": "wrong-account"}),
            snapshot.account.model_copy(
                update={"observed_at": snapshot.account.observed_at - timedelta(seconds=6)}
            ),
        ):
            with pytest.raises(ValueError, match="ACCOUNT_QUERY_FAILED"):
                rt.kernel.operator_trade_details(replace(snapshot, account=account))


def arm(rt: OfflineEngine) -> None:
    rt.kernel.start_recovery()
    wait_for(rt.kernel, lambda s: s.state == "REVIEW_REQUIRED")
    rt.confirmed_arm()


def until(rt: OfflineEngine, predicate: Callable[[EngineSnapshot], bool]) -> EngineSnapshot:
    deadline = monotonic() + 3
    while monotonic() < deadline:
        state = rt.kernel.poll()
        if predicate(state):
            return state
        sleep(0.001)
    raise AssertionError("observation did not settle")


def partial(rt: OfflineEngine, *, deferred: bool = False) -> Submission:
    rt.broker.queue_submit(
        SubmitScenario(
            outcome=SubmitOutcome.PARTIAL_FILL,
            partial_fill_quantity=Quantity(50),
            fill_price=Price.parse("10"),
            fill_notional=Money.parse("500"),
            delivery=EventDeliveryMode.DEFERRED if deferred else EventDeliveryMode.REVERSED,
        )
    )
    item = rt.buy()
    rt.kernel.submit(item, valid_until=monotonic() + 1)
    wait_for(rt.kernel, lambda s: s.submissions[-1].status == "SDK_ACCEPTED")
    return item


def cancel(rt: OfflineEngine, item: Submission) -> None:
    wait_for(rt.kernel, lambda _: rt.kernel.critical_idle)
    source = next(o for o in rt.broker.query_orders() if o.client_order_id == item.client_order_id)
    command = item.model_copy(
        update={
            "client_order_id": item.client_order_id + "c",
            "action_type": "CANCEL",
            "original_client_order_id": item.client_order_id,
            "original_order_id": source.broker_order_id,
        }
    )
    rt.kernel.submit(command, valid_until=monotonic() + 1)
    wait_for(rt.kernel, lambda s: s.submissions[-1].status == "SDK_ACCEPTED")
    wait_for(rt.kernel, lambda _: rt.kernel.critical_idle)


def test_active_and_terminal_refresh_preserve_grant_release_reservation_and_enable_next_buy(
    tmp_path: Path,
) -> None:
    with open_offline_engine(tmp_path, max_orders=2) as rt:
        arm(rt)
        grant_id = rt.kernel.snapshot().grant_id
        first = partial(rt)
        wait_for(rt.kernel, lambda s: s.observation_version >= 2 and rt.kernel.critical_idle)
        state = rt.kernel.snapshot()
        assert len(state.lots) == 1 and state.lots[0].remaining == 50
        assert state.lots[0].eligible_sell_date == DEMO_NOW.date() + timedelta(days=1)
        assert rt.kernel._broker_snapshot and rt.kernel._broker_snapshot.account.cash == "9000.00"
        assert len(rt.kernel._book.snapshot()) == 1
        assert state.orders[0].phase == "ACTIVE"
        with pytest.raises(ValueError, match="active"):
            rt.kernel.submit(rt.buy(), valid_until=monotonic() + 1)
        cancel(rt, first)
        assert rt.kernel._book.snapshot() == () and rt.kernel.snapshot().in_flight == ()
        assert rt.kernel.snapshot().grant_id == grant_id
        second = partial(rt, deferred=True)
        version = rt.kernel.snapshot().observation_version
        rt.kernel.refresh_account()
        wait_for(rt.kernel, lambda s: s.observation_version > version and rt.kernel.critical_idle)
        assert sum(lot.remaining for lot in rt.kernel.snapshot().lots) == 100
        rt.broker.flush_pending(reverse=True, duplicate=True)
        wait_for(rt.kernel, lambda _: rt.kernel.critical_idle)
        assert sum(lot.remaining for lot in rt.kernel.snapshot().lots) == 100
        cancel(rt, second)
        assert len(rt.broker.query_orders()) == 2 and rt.kernel.snapshot().grant_id == grant_id


def test_refresh_replaces_stale_account_without_new_authorization(tmp_path: Path) -> None:
    now = [DEMO_NOW]
    with open_offline_engine(tmp_path, clock=lambda: now[0]) as rt:
        arm(rt)
        now[0] += timedelta(seconds=6)
        with pytest.raises(ValueError, match="gate"):
            rt.kernel.submit(rt.buy(), valid_until=monotonic() + 1)
        version = rt.kernel.snapshot().observation_version
        rt.kernel.refresh_account()
        wait_for(rt.kernel, lambda s: s.observation_version > version)
        rt.kernel.submit(rt.buy(), valid_until=monotonic() + 1)
        wait_for(rt.kernel, lambda s: s.submissions[-1].status == "SDK_ACCEPTED")
        wait_for(rt.kernel, lambda _: rt.kernel.critical_idle)
        assert not rt.kernel.snapshot().halted


@pytest.mark.parametrize(
    "fault",
    ["query", "stale", "position", "account", "window", "coherence", "lost_trade", "lost_order"],
)
def test_failed_observation_keeps_last_trusted_state_and_halts(
    tmp_path: Path, fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with open_offline_engine(tmp_path) as rt:
        arm(rt)
        partial(rt)
        wait_for(rt.kernel, lambda _: rt.kernel.critical_idle)
        prior = rt.kernel.snapshot()
        original = rt.kernel._account_reader

        def bad_account() -> AccountFact:
            fact = original()
            if fault == "stale":
                return fact.model_copy(update={"observed_at": DEMO_NOW - timedelta(seconds=6)})
            if fault == "position":
                return fact.model_copy(
                    update={
                        "positions": (PositionFact(symbol="000001.SZ", quantity=51, sellable=0),)
                    }
                )
            if fault == "account":
                return fact.model_copy(update={"account_id": "wrong-account"})
            if fault == "window":
                return fact.model_copy(update={"observed_order_ids": ()})
            if fault == "coherence":
                return fact.model_copy(update={"coherent": False})
            return fact

        rt.kernel._account_reader = bad_account
        if fault == "query":
            rt.broker.queue_query_fault(BrokerQuery.TRADES, QueryFault.FAILURE)
        if fault in ("lost_trade", "lost_order"):
            original_capture = rt.kernel._capture

            # Fault injected at the typed SDK result, leaving the Mock's real
            # order/fill state unchanged for cleanup and diagnostic assertions.
            def capture(broker: BrokerGateway) -> BrokerRecoverySnapshot:
                fact = original_capture(broker)
                return (
                    replace(fact, trades=()) if fault == "lost_trade" else replace(fact, orders=())
                )

            monkeypatch.setattr(rt.kernel, "_capture", capture)
        rt.kernel.refresh_account()
        state = until(rt, lambda s: s.halted)
        assert state.grant_id is None and state.observation_version == prior.observation_version
        assert state.lots == prior.lots
        assert len(rt.kernel._book.snapshot()) == 1


@pytest.mark.parametrize("fault", ["HALT", "TIMEOUT", "EVENT"])
def test_query_in_flight_never_revives_authorization_or_overlaps_sdk(
    tmp_path: Path, fault: str
) -> None:
    entered, release = Event(), Event()
    with open_offline_engine(tmp_path) as rt:
        arm(rt)
        reader = rt.kernel._account_reader
        version = rt.kernel.snapshot().observation_version

        def delayed() -> AccountFact:
            entered.set()
            assert release.wait(2)
            return reader()

        rt.kernel._account_reader = delayed
        rt.kernel.refresh_account()
        try:
            assert entered.wait(1)
            assert rt.kernel.poll().observation_pending
            with pytest.raises(ValueError):
                rt.kernel.refresh_account()
            with pytest.raises(ValueError):
                rt.kernel.submit(rt.buy(), valid_until=monotonic() + 1)
            if fault == "HALT":
                rt.kernel.emergency_halt()
            elif fault == "TIMEOUT":
                rt.kernel._observation_deadline = monotonic() - 1
            else:
                # Event fencing is independently checked even for an empty query.
                with rt.kernel._event_lock:
                    rt.kernel._event_version += 1
            rt.kernel.poll()
        finally:
            release.set()
        state = until(rt, lambda s: s.halted and rt.kernel.dispatcher.idle)
        assert state.grant_id is None and state.observation_version == version
        assert rt.broker.query_orders() == ()


@pytest.mark.parametrize("fault", ["DISK", "TIMEOUT", "HALT", "EVENT"])
def test_query_repaired_facts_must_be_durable_before_observation_is_published(
    tmp_path: Path, fault: str
) -> None:
    entered, release = Event(), Event()

    class DelayedFacts(APSWCriticalStore):
        def append_evidence(
            self,
            table: Literal["broker_facts", "control_records", "recovery_checkpoints"],
            key: str,
            body: bytes,
        ) -> bool:
            if table == "broker_facts":
                entered.set()
                assert release.wait(2)
                if fault == "DISK":
                    raise OSError("injected query-repair persistence failure")
            return super().append_evidence(table, key, body)

    def factory(path: Path, account: str) -> CriticalJournalOwner:
        return CriticalJournalOwner(lambda: DelayedFacts(path, account))

    with open_offline_engine(tmp_path, journal_factory=factory) as rt:
        arm(rt)
        partial(rt, deferred=True)
        before = rt.kernel.snapshot()
        rt.kernel.refresh_account()
        try:
            until(rt, lambda _: entered.is_set())
            assert rt.kernel.snapshot().observation_version == before.observation_version
            assert not rt.kernel.critical_idle
            if fault == "HALT":
                rt.kernel.emergency_halt()
            elif fault == "EVENT":
                rt.broker.flush_pending()
            elif fault == "TIMEOUT":
                until(rt, lambda s: s.halted)
            release.set()
            state = until(rt, lambda s: s.halted)
            assert state.observation_version == before.observation_version
            assert state.lots == () and state.grant_id is None
            assert len(rt.kernel._book.snapshot()) == 1
        finally:
            release.set()


def test_periodic_observation_is_started_by_poll_and_keeps_existing_grant(tmp_path: Path) -> None:
    with open_offline_engine(tmp_path) as rt:
        arm(rt)
        before = rt.kernel.snapshot()
        rt.kernel._next_observation = monotonic() - 1
        assert rt.kernel.poll().observation_pending
        state = wait_for(rt.kernel, lambda s: s.observation_version > before.observation_version)
        assert state.grant_id == before.grant_id and not state.halted
        assert state.account_observed_at == DEMO_NOW
