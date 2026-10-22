from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, get_ident

import pytest

from auto_trading_system.domain import (
    Clock,
    Money,
    Price,
    Quantity,
    SecurityCode,
    ShanghaiTimestamp,
    TradingDate,
)
from auto_trading_system.execution import (
    OrderIntentGateError,
    OrderIntentPersistenceGate,
    OrderIntentSubmission,
    RuntimeOrderExecutionGuard,
)
from auto_trading_system.gateways.broker import MockBrokerGateway
from auto_trading_system.journal import (
    AppendDomainEvent,
    CanonicalJsonDocument,
    CheckpointRecord,
    CommitOutcome,
    IntentSide,
    JournalCommitResult,
    JournalHealth,
    JournalHealthStatus,
    JournalOpenResult,
    JournalRecoverySnapshot,
    JournalStatus,
    JournalTransaction,
    MaintenanceRequest,
    MaintenanceResult,
    OrderIntentRecord,
    OutboxItem,
    PersistedEvent,
    PutCheckpoint,
)
from auto_trading_system.journal.sqlite import SQLiteJournalSettings, SQLiteLocalJournal
from auto_trading_system.journal.writer import PersistenceWriter, PersistenceWriterError
from auto_trading_system.monitoring import (
    JournalHealthFaultDetector,
    PersistenceStallDetector,
)
from auto_trading_system.protocol.canonical_json import canonical_json_bytes
from auto_trading_system.runtime import (
    RuntimeCapability,
    RuntimeFaultComponent,
    RuntimeFaultKind,
    RuntimeFaultPolicy,
    RuntimeFaultReport,
    RuntimeHealth,
    RuntimeRecoveryPermit,
    RuntimeState,
    RuntimeStateMachine,
    TransitionAuthority,
)


@dataclass(frozen=True, slots=True)
class FixedClock:
    value: ShanghaiTimestamp = field(
        default_factory=lambda: ShanghaiTimestamp.parse("2026-09-04T14:35:01+08:00")
    )

    def now(self) -> ShanghaiTimestamp:
        return self.value


@dataclass
class IncrementingClock:
    value: int = 0

    def monotonic_ns(self) -> int:
        self.value += 1
        return self.value


@dataclass
class BlockingJournal:
    _status: JournalStatus = JournalStatus.NEW
    commit_entered: Event = field(default_factory=Event)
    commit_release: Event = field(default_factory=Event)
    owner_thread_id: int | None = None
    commit_sequence: int = 0

    @property
    def status(self) -> JournalStatus:
        return self._status

    def open(self) -> JournalOpenResult:
        self.owner_thread_id = get_ident()
        self._status = JournalStatus.OPEN
        return JournalOpenResult(1, "journal_" + "1" * 32, True)

    def commit(self, transaction: JournalTransaction) -> JournalCommitResult:
        assert get_ident() == self.owner_thread_id
        self.commit_entered.set()
        self.commit_release.wait()
        self.commit_sequence += 1
        persisted_events: tuple[PersistedEvent, ...] = ()
        event_operation = next(
            (
                operation
                for operation in transaction.operations
                if isinstance(operation, AppendDomainEvent)
            ),
            None,
        )
        if event_operation is not None:
            body = canonical_json_bytes(
                {"event_id": event_operation.draft.event_id}, exclude=frozenset()
            )
            digest = "sha256:" + hashlib.sha256(body).hexdigest()
            document = CanonicalJsonDocument(event_operation.draft.event_id, digest, body)
            persisted_events = (
                PersistedEvent(
                    event_operation.draft.event_id,
                    event_operation.draft.boot_id,
                    1,
                    document,
                ),
            )
        return JournalCommitResult(
            transaction.transaction_id,
            CommitOutcome.COMMITTED,
            self.commit_sequence,
            persisted_events,
        )

    def load_recovery(self, *, account_id: str, trade_date: TradingDate) -> JournalRecoverySnapshot:
        assert get_ident() == self.owner_thread_id
        return JournalRecoverySnapshot(account_id, trade_date, (), (), (), (), (), (), (), 0)

    def fetch_outbox(
        self, *, limit: int, available_at: ShanghaiTimestamp
    ) -> tuple[OutboxItem, ...]:
        assert get_ident() == self.owner_thread_id
        return ()

    def health(self) -> JournalHealth:
        assert get_ident() == self.owner_thread_id
        return JournalHealth(JournalHealthStatus.HEALTHY, True, 1, 1, 0, self.commit_sequence)

    def maintain(self, request: MaintenanceRequest) -> MaintenanceResult:
        raise NotImplementedError

    def close(self) -> None:
        assert get_ident() == self.owner_thread_id
        self._status = JournalStatus.CLOSED


def healthy_runtime() -> RuntimeHealth:
    return RuntimeHealth(True, True, True, True, True, False, False, True, True, True)


def auto_machine() -> RuntimeStateMachine:
    machine = RuntimeStateMachine(
        capability=RuntimeCapability.FULL,
        monotonic_clock=IncrementingClock(),
        owner_guard=lambda: None,
    )
    machine.request_transition(
        RuntimeState.CONNECTING,
        authority=TransitionAuthority.LIFECYCLE,
        reason_code="START_CONNECTING",
    )
    machine.request_transition(
        RuntimeState.RECONCILING,
        authority=TransitionAuthority.LIFECYCLE,
        reason_code="START_RECONCILING",
    )
    machine.request_transition(
        RuntimeState.READY_PAUSED,
        authority=TransitionAuthority.LIFECYCLE,
        reason_code="START_RECONCILED",
        health=healthy_runtime(),
    )
    machine.request_transition(
        RuntimeState.AUTO,
        authority=TransitionAuthority.RUNTIME,
        reason_code="AUTO_ACTIVATED",
        health=healthy_runtime(),
        permit=RuntimeRecoveryPermit("recon_fault_matrix_0001", True, True),
    )
    return machine


def order_submission() -> OrderIntentSubmission:
    created_at = FixedClock().now()
    intent = OrderIntentRecord(
        "intent_01K43FAULT000000001",
        "ord_01K43FAULT000000001",
        "plan_01K43FAULT20260904001",
        "acct_demo_001",
        created_at.trading_date,
        SecurityCode.parse("000001.SZ"),
        IntentSide.BUY,
        Quantity(100),
        Price.parse("12.34"),
        Money.parse("1500.00"),
        "ATS000000000000000000099",
        "EXEC_BALANCED_V1",
        created_at,
    )
    return OrderIntentSubmission(
        intent,
        "evt_00000000000000000000000000000099",
        "node-demo-001",
        "boot_99999999999999999999999999999999",
        "corr_fault_20260904_0001",
        "risk_01K43FAULT000000001",
    )


def checkpoint_transaction(number: int) -> JournalTransaction:
    return JournalTransaction(
        f"txn_fault_checkpoint_{number:08d}",
        (PutCheckpoint(CheckpointRecord("runtime", f"fault_probe_{number}", b"1", number)),),
    )


def test_writer_stall_halts_and_rechecks_permission_before_broker_submission() -> None:
    async def scenario() -> None:
        machine = auto_machine()
        journal = BlockingJournal()
        writer = PersistenceWriter(journal, capacity=2)
        clock: Clock = FixedClock()
        broker = MockBrokerGateway(clock)
        broker.start(lambda _: None)
        gate = OrderIntentPersistenceGate(
            writer,
            broker,
            owner_guard=lambda: None,
            execution_guard=RuntimeOrderExecutionGuard(lambda: machine.state),
        )
        await writer.start()
        task = asyncio.create_task(gate.persist_and_submit(order_submission()))
        try:
            assert await asyncio.to_thread(journal.commit_entered.wait, 1)
            snapshot = writer.snapshot()
            assert snapshot.pending == 0
            assert snapshot.accepted - snapshot.completed == 1
            detector = PersistenceStallDetector(stall_timeout_ns=100)
            assert detector.observe(snapshot, now_monotonic_ns=1_000) is None
            signal = detector.observe(writer.snapshot(), now_monotonic_ns=1_100)
            assert signal is not None

            RuntimeFaultPolicy(machine, owner_guard=lambda: None).handle(signal.to_fault_report())
            assert machine.state is RuntimeState.HALT
            journal.commit_release.set()
            with pytest.raises(OrderIntentGateError) as captured:
                await task
            assert captured.value.code == "ORDER_EXECUTION_PERMISSION_REVOKED"
            assert captured.value.requires_reconciliation is True
            assert broker.query_orders() == ()
        finally:
            journal.commit_release.set()
            if not task.done():
                await asyncio.gather(task, return_exceptions=True)
            await writer.stop()
            broker.stop()

    asyncio.run(scenario())


def test_real_sqlite_low_disk_threshold_halts_before_any_intent(tmp_path: Path) -> None:
    async def scenario() -> None:
        machine = auto_machine()
        clock: Clock = FixedClock()
        writer = PersistenceWriter(
            SQLiteLocalJournal(
                SQLiteJournalSettings(
                    (tmp_path / "low-disk.db").resolve(),
                    "acct_demo_001",
                    minimum_free_bytes=10**30,
                ),
                clock=clock,
            )
        )
        broker = MockBrokerGateway(clock)
        broker.start(lambda _: None)
        await writer.start()
        try:
            health = await writer.health()
            assert health.status is JournalHealthStatus.DEGRADED
            assert "LOW_DISK_SPACE" in health.reason_codes
            report = JournalHealthFaultDetector().observe(health, now_monotonic_ns=500)
            assert report is not None
            RuntimeFaultPolicy(machine, owner_guard=lambda: None).handle(report)

            gate = OrderIntentPersistenceGate(
                writer,
                broker,
                owner_guard=lambda: None,
                execution_guard=RuntimeOrderExecutionGuard(lambda: machine.state),
            )
            with pytest.raises(OrderIntentGateError) as captured:
                await gate.persist_and_submit(order_submission())
            assert captured.value.code == "ORDER_EXECUTION_NOT_ALLOWED"
            assert writer.snapshot().accepted == 1
            assert broker.query_orders() == ()
        finally:
            await writer.stop()
            broker.stop()

    asyncio.run(scenario())


def test_writer_queue_saturation_halts_runtime_and_rejects_without_blocking() -> None:
    async def scenario() -> None:
        machine = auto_machine()
        journal = BlockingJournal()
        writer = PersistenceWriter(journal, capacity=1)
        await writer.start()
        first = asyncio.create_task(writer.commit(checkpoint_transaction(1)))
        assert await asyncio.to_thread(journal.commit_entered.wait, 1)
        second = asyncio.create_task(writer.commit(checkpoint_transaction(2)))
        try:
            await asyncio.sleep(0)
            with pytest.raises(PersistenceWriterError) as captured:
                await writer.commit(checkpoint_transaction(3))
            assert captured.value.code == "PERSISTENCE_QUEUE_FULL"
            RuntimeFaultPolicy(machine, owner_guard=lambda: None).handle(
                RuntimeFaultReport(
                    RuntimeFaultKind.PERSISTENCE_QUEUE_FULL,
                    RuntimeFaultComponent.PERSISTENCE,
                    600,
                    "PERSISTENCE_QUEUE_FULL",
                )
            )
            assert machine.state is RuntimeState.HALT
        finally:
            journal.commit_release.set()
            await asyncio.gather(first, second, return_exceptions=True)
            await writer.stop()

    asyncio.run(scenario())
