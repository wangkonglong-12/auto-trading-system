from __future__ import annotations

from pathlib import Path
from threading import Event
from time import monotonic, sleep

import pytest

from auto_trading_system.domain import Price
from auto_trading_system.domain.clock import SystemClock
from auto_trading_system.execution.dispatch_boundary import DispatchPermission, SDKDispatcher
from auto_trading_system.execution.submission_coordinator import SubmissionCoordinator
from auto_trading_system.gateways.broker.mock import (
    MockBrokerGateway,
    SubmitOutcome,
    SubmitScenario,
)
from auto_trading_system.journal.apsw_store import APSWCriticalStore
from auto_trading_system.journal.critical_models import CommitEvidence, Submission
from auto_trading_system.journal.critical_writer import CriticalJournalOwner, open_critical_journal
from auto_trading_system.runtime.memory_state import MarketWindow, QuotePoint


def run_until_settled(coordinator: SubmissionCoordinator) -> str:
    deadline = monotonic() + 3
    while monotonic() < deadline:
        state = coordinator.poll()[0].status
        if state not in ("PERSISTING", "DISPATCHING"):
            return state
        sleep(0.001)
    raise AssertionError("coordinator did not settle")


def test_new_j1_dispatch_once_and_restart_never_replays(
    tmp_path: Path,
    submission: Submission,
) -> None:
    path = tmp_path / "critical.db"
    journal = open_critical_journal(path, submission.account_id)
    broker = MockBrokerGateway(SystemClock())
    broker.start(lambda event: None)
    permission = DispatchPermission()
    assert permission.snapshot().halted
    permission.allow_after_durable_grant(1)
    dispatcher = SDKDispatcher(broker, permission)
    coordinator = SubmissionCoordinator(journal, dispatcher, permission, lambda _: True)
    try:
        coordinator.begin(submission, valid_until=monotonic() + 2)
        assert run_until_settled(coordinator) == "SDK_ACCEPTED"
        with pytest.raises(ValueError, match="already"):
            coordinator.begin(submission, valid_until=monotonic() + 2)
        assert len(broker.query_orders()) == 1
        second = SubmissionCoordinator(journal, dispatcher, permission, lambda _: True)
        second.begin(submission, valid_until=monotonic() + 2)
        assert run_until_settled(second) == "RECOVERY_REQUIRED"
        assert permission.snapshot().halted
        assert len(broker.query_orders()) == 1
    finally:
        dispatcher.close()
        assert journal.close()
        broker.stop()
    recovery = open_critical_journal(path, submission.account_id)
    try:
        assert recovery.recovery_page().result(timeout=1)[0].submission == submission
        assert DispatchPermission().snapshot().halted
    finally:
        assert recovery.close()


@pytest.mark.parametrize("halt_first", [True, False])
def test_halt_or_j1_deadline_invalidates_late_success_without_stalling_ticks(
    tmp_path: Path,
    submission: Submission,
    halt_first: bool,
) -> None:
    entered, release = Event(), Event()

    class SlowStore(APSWCriticalStore):
        def commit_submission(self, item: Submission) -> CommitEvidence:
            entered.set()
            assert release.wait(3)
            return super().commit_submission(item)

    journal = CriticalJournalOwner(
        lambda: SlowStore(tmp_path / "critical.db", submission.account_id)
    )
    journal.start()
    broker = MockBrokerGateway(SystemClock())
    broker.start(lambda event: None)
    permission = DispatchPermission()
    permission.allow_after_durable_grant(1)
    dispatcher = SDKDispatcher(broker, permission)
    coordinator = SubmissionCoordinator(journal, dispatcher, permission, lambda _: True)
    try:
        coordinator.begin(submission, valid_until=monotonic() + 2, wait_seconds=0.03)
        assert entered.wait(1)
        window = MarketWindow()
        for seq in range(1, 1001):
            window.add(QuotePoint(seq, seq, Price.parse("10")))
        assert window.snapshot().state_version == 1000
        if halt_first:
            permission.halt("OPERATOR_HALT")
            release.set()
            assert run_until_settled(coordinator) in ("NOT_DISPATCHED", "PERSISTENCE_UNCERTAIN")
        else:
            assert run_until_settled(coordinator) == "PERSISTENCE_UNCERTAIN"
            release.set()
        assert journal.recovery_page().result(timeout=2)[0].submission == submission
        coordinator.poll()
        assert broker.query_orders() == ()
    finally:
        release.set()
        dispatcher.close()
        assert journal.close()
        broker.stop()


def test_sdk_timeout_after_accept_never_retries(tmp_path: Path, submission: Submission) -> None:
    journal = open_critical_journal(tmp_path / "critical.db", submission.account_id)
    broker = MockBrokerGateway(SystemClock())
    broker.start(lambda event: None)
    broker.queue_submit(SubmitScenario(outcome=SubmitOutcome.TIMEOUT_AFTER_ACCEPT))
    permission = DispatchPermission()
    permission.allow_after_durable_grant(1)
    dispatcher = SDKDispatcher(broker, permission)
    coordinator = SubmissionCoordinator(journal, dispatcher, permission, lambda _: True)
    try:
        coordinator.begin(submission, valid_until=monotonic() + 2)
        assert run_until_settled(coordinator) == "UNCERTAIN"
        assert len(broker.query_orders()) == 1
        assert permission.snapshot().in_flight == (submission.client_order_id,)
        assert permission.snapshot().halted
    finally:
        dispatcher.close()
        assert journal.close()
        broker.stop()


def test_permission_epoch_fences_halt_during_grant() -> None:
    permission = DispatchPermission()
    permission.halt("NEW_HALT")
    with pytest.raises(ValueError, match="changed"):
        permission.allow_after_durable_grant(1)
    assert not permission.claim("order-1", 2, monotonic() + 1)


def test_j1_failure_fails_closed(tmp_path: Path, submission: Submission) -> None:
    class FailedStore(APSWCriticalStore):
        def commit_submission(self, item: Submission) -> CommitEvidence:
            raise OSError("simulated disk full")

    journal = CriticalJournalOwner(
        lambda: FailedStore(tmp_path / "critical.db", submission.account_id)
    )
    journal.start()
    permission = DispatchPermission()
    permission.allow_after_durable_grant(1)
    broker = MockBrokerGateway(SystemClock())
    broker.start(lambda event: None)
    dispatcher = SDKDispatcher(broker, permission)
    coordinator = SubmissionCoordinator(journal, dispatcher, permission, lambda _: True)
    try:
        coordinator.begin(submission, valid_until=monotonic() + 2)
        assert run_until_settled(coordinator) == "PERSISTENCE_FAILED"
        assert permission.snapshot().halted and broker.query_orders() == ()
    finally:
        dispatcher.close()
        assert journal.close()
        broker.stop()
