from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import cast

import pytest

from auto_trading_system.domain import ShanghaiTimestamp
from auto_trading_system.journal import (
    AcknowledgeOutbox,
    CanonicalJsonDocument,
    CommitOutcome,
    DeadLetterOutbox,
    JournalCommitResult,
    JournalHealth,
    JournalHealthStatus,
    JournalTransaction,
    OutboxItem,
    OutboxKind,
    OutboxState,
    RescheduleOutbox,
)
from auto_trading_system.protocol.canonical_json import canonical_json_bytes
from auto_trading_system.runtime import (
    BoundedEventIngress,
    EventPriority,
    PublishOutcome,
    RuntimeEventSource,
)
from auto_trading_system.sync import (
    MAX_OUTBOX_ATTEMPTS,
    DurableOutboxWorker,
    OutboxDeliveryDisposition,
    OutboxDeliveryError,
    OutboxDeliveryReceipt,
    OutboxPressureLevel,
    OutboxPressurePublisher,
    OutboxPressureSignal,
    OutboxRunStatus,
    OutboxWorkerError,
    OutboxWorkerSettings,
    RoutedOutboxDispatcher,
)


@dataclass
class MutableClock:
    value: ShanghaiTimestamp = field(
        default_factory=lambda: ShanghaiTimestamp.parse("2026-09-04T10:00:00+08:00")
    )

    def now(self) -> ShanghaiTimestamp:
        return self.value


@dataclass
class ScriptedJournal:
    items: tuple[OutboxItem, ...]
    pending_counts: list[int]
    writable: bool = True
    commit_response: object | None = None
    honor_fetch_limit: bool = True
    fetch_calls: list[tuple[int, ShanghaiTimestamp]] = field(default_factory=list)
    commits: list[JournalTransaction] = field(default_factory=list)

    async def health(self) -> JournalHealth:
        pending = (
            self.pending_counts.pop(0) if len(self.pending_counts) > 1 else self.pending_counts[0]
        )
        return JournalHealth(
            JournalHealthStatus.HEALTHY if self.writable else JournalHealthStatus.UNAVAILABLE,
            self.writable,
            1,
            4096,
            pending,
            len(self.commits),
            () if self.writable else ("JOURNAL_READ_ONLY",),
        )

    async def fetch_outbox(
        self, *, limit: int, available_at: ShanghaiTimestamp
    ) -> tuple[OutboxItem, ...]:
        self.fetch_calls.append((limit, available_at))
        return self.items[:limit] if self.honor_fetch_limit else self.items

    async def commit(self, transaction: JournalTransaction) -> JournalCommitResult:
        self.commits.append(transaction)
        if isinstance(self.commit_response, BaseException):
            raise self.commit_response
        if self.commit_response is not None:
            return cast(JournalCommitResult, self.commit_response)
        return JournalCommitResult(transaction.transaction_id, CommitOutcome.COMMITTED, 1)


@dataclass
class ScriptedDispatcher:
    response: object
    calls: list[tuple[OutboxItem, ...]] = field(default_factory=list)

    async def deliver(self, items: tuple[OutboxItem, ...]) -> tuple[OutboxDeliveryReceipt, ...]:
        self.calls.append(items)
        if isinstance(self.response, BaseException):
            raise self.response
        return cast(tuple[OutboxDeliveryReceipt, ...], self.response)


def document(number: int) -> CanonicalJsonDocument:
    body = canonical_json_bytes({"number": number}, exclude=frozenset())
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    return CanonicalJsonDocument(f"doc_outbox_{number:04d}", digest, body)


def item(
    number: int,
    *,
    attempts: int = 0,
    kind: OutboxKind = OutboxKind.DOMAIN_EVENT,
) -> OutboxItem:
    created = ShanghaiTimestamp.parse("2026-09-04T09:59:00+08:00")
    return OutboxItem(
        number,
        kind,
        OutboxState.PENDING,
        document(number),
        attempts,
        created,
        created,
    )


def delivery(
    value: OutboxItem,
    disposition: OutboxDeliveryDisposition,
    reason_code: str | None = None,
) -> OutboxDeliveryReceipt:
    return OutboxDeliveryReceipt(
        value.outbox_id,
        value.document.content_hash,
        disposition,
        reason_code,
    )


def worker(
    journal: ScriptedJournal,
    dispatcher: ScriptedDispatcher | RoutedOutboxDispatcher,
    *,
    clock: MutableClock | None = None,
    settings: OutboxWorkerSettings | None = None,
    signals: list[OutboxPressureSignal] | None = None,
) -> DurableOutboxWorker:
    target_signals = [] if signals is None else signals
    return DurableOutboxWorker(
        journal,
        dispatcher,
        clock=clock or MutableClock(),
        settings=settings,
        pressure_sink=target_signals.append,
        transaction_id_factory=lambda: "txn_outbox_test_00000001",
    )


def test_idle_run_reads_health_publishes_pressure_and_does_not_commit() -> None:
    journal = ScriptedJournal((), [0])
    dispatcher = ScriptedDispatcher(())
    signals: list[OutboxPressureSignal] = []

    result = asyncio.run(worker(journal, dispatcher, signals=signals).run_once())

    assert result.status is OutboxRunStatus.IDLE
    assert result.fetched == 0
    assert result.commit_sequence is None
    assert dispatcher.calls == []
    assert journal.commits == []
    assert signals[0].level is OutboxPressureLevel.NORMAL
    assert signals[0].blocks_new_buys is False


def test_mixed_receipts_become_one_atomic_ordered_journal_transaction() -> None:
    first, second, third = item(1), item(2, attempts=2), item(3)
    receipts = (
        delivery(third, OutboxDeliveryDisposition.DEAD_LETTER, "REMOTE_SCHEMA_REJECTED"),
        delivery(first, OutboxDeliveryDisposition.ACKNOWLEDGE),
        delivery(second, OutboxDeliveryDisposition.RETRY, "REMOTE_TEMPORARY_REJECTED"),
    )
    journal = ScriptedJournal((first, second, third), [3, 1])
    settings = OutboxWorkerSettings(
        batch_size=3,
        retry_base_seconds=2,
        retry_max_seconds=300,
        high_watermark=10,
        critical_watermark=20,
    )

    result = asyncio.run(
        worker(journal, ScriptedDispatcher(receipts), settings=settings).run_once()
    )

    assert result.status is OutboxRunStatus.APPLIED
    assert (result.acknowledged, result.retried, result.dead_lettered) == (1, 1, 1)
    assert result.pending_after == 1
    transaction = journal.commits[0]
    assert transaction.transaction_id == "txn_outbox_test_00000001"
    assert isinstance(transaction.operations[0], AcknowledgeOutbox)
    assert isinstance(transaction.operations[1], RescheduleOutbox)
    retry = transaction.operations[1]
    assert retry.attempts == 3
    assert retry.available_at.to_wire() == "2026-09-04T10:00:08+08:00"
    assert retry.reason_code == "REMOTE_TEMPORARY_REJECTED"
    assert isinstance(transaction.operations[2], DeadLetterOutbox)


@pytest.mark.parametrize(
    ("failure", "reason_code", "retryable"),
    [
        (
            OutboxDeliveryError("OUTBOX_TRANSPORT_TIMEOUT", "timeout", retryable=True),
            "OUTBOX_TRANSPORT_TIMEOUT",
            True,
        ),
        (RuntimeError("bug"), "OUTBOX_DELIVERY_UNEXPECTED", True),
    ],
)
def test_transport_failure_reschedules_complete_batch_without_data_loss(
    failure: BaseException,
    reason_code: str,
    retryable: bool,
) -> None:
    items = (item(1), item(2))
    journal = ScriptedJournal(items, [2, 2])

    result = asyncio.run(worker(journal, ScriptedDispatcher(failure)).run_once())

    assert result.status is OutboxRunStatus.TRANSPORT_FAILURE_RESCHEDULED
    assert result.retried == 2
    assert result.delivery_error_code == reason_code
    assert result.delivery_error_retryable is retryable
    assert all(
        isinstance(operation, RescheduleOutbox) for operation in journal.commits[0].operations
    )
    retry_operations = tuple(
        operation
        for operation in journal.commits[0].operations
        if isinstance(operation, RescheduleOutbox)
    )
    assert {operation.reason_code for operation in retry_operations} == {reason_code}


@pytest.mark.parametrize(
    "invalid_receipts",
    [
        [],
        (),
        (delivery(item(1), OutboxDeliveryDisposition.ACKNOWLEDGE),) * 2,
        (delivery(item(2), OutboxDeliveryDisposition.ACKNOWLEDGE),),
    ],
)
def test_invalid_receipt_set_reschedules_every_item(invalid_receipts: object) -> None:
    journal = ScriptedJournal((item(1),), [1, 1])

    result = asyncio.run(worker(journal, ScriptedDispatcher(invalid_receipts)).run_once())

    assert result.status is OutboxRunStatus.TRANSPORT_FAILURE_RESCHEDULED
    assert result.delivery_error_code == "OUTBOX_RECEIPT_INVALID"
    operation = journal.commits[0].operations[0]
    assert isinstance(operation, RescheduleOutbox)
    assert operation.reason_code == "OUTBOX_RECEIPT_INVALID"


def test_unwritable_journal_never_contacts_remote_dispatcher() -> None:
    journal = ScriptedJournal((item(1),), [1], writable=False)
    dispatcher = ScriptedDispatcher(())

    with pytest.raises(OutboxWorkerError) as captured:
        asyncio.run(worker(journal, dispatcher).run_once())

    assert captured.value.code == "OUTBOX_JOURNAL_UNWRITABLE"
    assert dispatcher.calls == []
    assert journal.fetch_calls == []


@pytest.mark.parametrize(
    "invalid_items",
    [
        [],
        (item(1), item(1)),
        tuple(item(number) for number in range(1, 52)),
        (
            OutboxItem(
                1,
                OutboxKind.DOMAIN_EVENT,
                OutboxState.DEAD_LETTER,
                document(1),
                0,
                ShanghaiTimestamp.parse("2026-09-04T09:59:00+08:00"),
                ShanghaiTimestamp.parse("2026-09-04T09:59:00+08:00"),
            ),
        ),
    ],
)
def test_invalid_journal_batch_fails_before_remote_delivery(invalid_items: object) -> None:
    typed_items = cast(tuple[OutboxItem, ...], invalid_items)
    journal = ScriptedJournal(
        typed_items,
        [1],
        honor_fetch_limit=len(typed_items) <= 50,
    )
    dispatcher = ScriptedDispatcher(())

    with pytest.raises(OutboxWorkerError) as captured:
        asyncio.run(worker(journal, dispatcher).run_once())

    assert captured.value.code == "OUTBOX_FETCH_INVALID"
    assert dispatcher.calls == []
    assert journal.commits == []


def test_invalid_commit_result_is_not_reported_as_delivery_success() -> None:
    current = item(1)
    journal = ScriptedJournal(
        (current,),
        [1],
        commit_response=JournalCommitResult("txn_wrong_commit_0001", CommitOutcome.COMMITTED, 1),
    )
    dispatcher = ScriptedDispatcher((delivery(current, OutboxDeliveryDisposition.ACKNOWLEDGE),))

    with pytest.raises(OutboxWorkerError) as captured:
        asyncio.run(worker(journal, dispatcher).run_once())

    assert captured.value.code == "OUTBOX_COMMIT_RESULT_INVALID"
    assert len(journal.commits) == 1


def test_attempt_exhaustion_keeps_item_pending_instead_of_silent_dead_letter() -> None:
    current = item(1, attempts=MAX_OUTBOX_ATTEMPTS)
    journal = ScriptedJournal((current,), [1])
    dispatcher = ScriptedDispatcher(
        (delivery(current, OutboxDeliveryDisposition.RETRY, "REMOTE_TEMPORARY_REJECTED"),)
    )

    with pytest.raises(OutboxWorkerError) as captured:
        asyncio.run(worker(journal, dispatcher).run_once())

    assert captured.value.code == "OUTBOX_ATTEMPT_EXHAUSTED"
    assert journal.commits == []


def test_pressure_sink_emits_only_transitions_and_recovery_never_restores_auto() -> None:
    journal = ScriptedJournal((), [0])
    signals: list[OutboxPressureSignal] = []
    current_worker = worker(
        journal,
        ScriptedDispatcher(()),
        settings=OutboxWorkerSettings(high_watermark=2, critical_watermark=3),
        signals=signals,
    )
    for pending in (0, 2, 2, 3, 1, 1):
        journal.pending_counts[:] = [pending]
        asyncio.run(current_worker.run_once())

    assert [signal.level for signal in signals] == [
        OutboxPressureLevel.NORMAL,
        OutboxPressureLevel.HIGH,
        OutboxPressureLevel.CRITICAL,
        OutboxPressureLevel.NORMAL,
    ]
    assert signals[1].blocks_new_buys is True
    assert signals[2].blocks_new_buys is True


def test_cancellation_during_transport_leaves_journal_unchanged() -> None:
    journal = ScriptedJournal((item(1),), [1])
    dispatcher = ScriptedDispatcher(asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker(journal, dispatcher).run_once())

    assert journal.commits == []


def test_concurrent_scheduler_tick_is_rejected_instead_of_double_delivery() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        class BlockingDispatcher:
            async def deliver(
                self, items: tuple[OutboxItem, ...]
            ) -> tuple[OutboxDeliveryReceipt, ...]:
                entered.set()
                await release.wait()
                return (delivery(items[0], OutboxDeliveryDisposition.ACKNOWLEDGE),)

        journal = ScriptedJournal((item(1),), [1, 0])
        current_worker = DurableOutboxWorker(
            journal,
            BlockingDispatcher(),
            clock=MutableClock(),
            pressure_sink=lambda _: None,
            transaction_id_factory=lambda: "txn_outbox_test_00000001",
        )
        running = asyncio.create_task(current_worker.run_once())
        await entered.wait()
        with pytest.raises(OutboxWorkerError, match="OUTBOX_RUN_IN_PROGRESS"):
            await current_worker.run_once()
        release.set()
        assert (await running).acknowledged == 1

    asyncio.run(scenario())


def test_worker_models_and_constructor_reject_ambiguous_values() -> None:
    current = item(1)
    with pytest.raises(ValueError, match="acknowledgement"):
        delivery(current, OutboxDeliveryDisposition.ACKNOWLEDGE, "BAD_REASON")
    with pytest.raises(ValueError, match="require a reason"):
        delivery(current, OutboxDeliveryDisposition.RETRY)
    with pytest.raises(ValueError, match="batch_size"):
        OutboxWorkerSettings(batch_size=51)
    with pytest.raises(ValueError, match="retry_max_seconds"):
        OutboxWorkerSettings(retry_base_seconds=2, retry_max_seconds=1)
    with pytest.raises(ValueError, match="critical_watermark"):
        OutboxWorkerSettings(high_watermark=2, critical_watermark=2)
    settings = OutboxWorkerSettings(retry_base_seconds=2, retry_max_seconds=300)
    assert [settings.retry_delay_seconds(attempt) for attempt in (1, 2, 3, 20)] == [
        2,
        4,
        8,
        300,
    ]
    with pytest.raises(ValueError, match="attempts"):
        settings.retry_delay_seconds(0)
    with pytest.raises(ValueError, match="journal"):
        DurableOutboxWorker(
            object(),  # type: ignore[arg-type]
            ScriptedDispatcher(()),
            clock=MutableClock(),
            pressure_sink=lambda _: None,
        )


def test_routed_dispatcher_groups_kinds_and_rejects_unregistered_data() -> None:
    domain_item = item(1)
    asset_item = item(2, kind=OutboxKind.ASSET_SNAPSHOT)
    domain_handler = ScriptedDispatcher(
        (delivery(domain_item, OutboxDeliveryDisposition.ACKNOWLEDGE),)
    )
    asset_handler = ScriptedDispatcher(
        (delivery(asset_item, OutboxDeliveryDisposition.ACKNOWLEDGE),)
    )
    router = RoutedOutboxDispatcher(
        {
            OutboxKind.DOMAIN_EVENT: domain_handler,
            OutboxKind.ASSET_SNAPSHOT: asset_handler,
        }
    )

    receipts = asyncio.run(router.deliver((domain_item, asset_item)))

    assert len(receipts) == 2
    assert domain_handler.calls == [(domain_item,)]
    assert asset_handler.calls == [(asset_item,)]

    unsupported = RoutedOutboxDispatcher({OutboxKind.DOMAIN_EVENT: domain_handler})
    with pytest.raises(OutboxDeliveryError, match="OUTBOX_KIND_UNSUPPORTED"):
        asyncio.run(unsupported.deliver((asset_item,)))
    with pytest.raises(ValueError, match="non-empty"):
        RoutedOutboxDispatcher({})


def test_pressure_signal_requires_level_to_match_watermarks() -> None:
    with pytest.raises(ValueError, match="does not match"):
        OutboxPressureSignal(
            OutboxPressureLevel.NORMAL,
            3,
            2,
            3,
            MutableClock().now(),
        )
    signal = OutboxPressureSignal(
        OutboxPressureLevel.CRITICAL,
        3,
        2,
        3,
        MutableClock().now(),
    )
    assert signal.blocks_new_buys is True


def test_pressure_publisher_uses_reserved_decision_loop_lane_and_detects_loss() -> None:
    @dataclass
    class Monotonic:
        value: int = 0

        def monotonic_ns(self) -> int:
            self.value += 1
            return self.value

    ingress = BoundedEventIngress[OutboxPressureSignal](
        normal_capacity=1,
        critical_capacity=1,
        monotonic_clock=Monotonic(),
    )
    publisher = OutboxPressurePublisher(ingress)
    signal = OutboxPressureSignal(
        OutboxPressureLevel.HIGH,
        2,
        2,
        3,
        MutableClock().now(),
    )

    publisher(signal)
    envelope = ingress.take_blocking()
    assert envelope is not None
    assert envelope.payload == signal
    assert envelope.source is RuntimeEventSource.SYNC
    assert envelope.priority is EventPriority.CRITICAL

    assert (
        ingress.publish(
            signal,
            source=RuntimeEventSource.SYNC,
            priority=EventPriority.CRITICAL,
        )
        is PublishOutcome.ACCEPTED
    )
    with pytest.raises(OutboxWorkerError, match="OUTBOX_PRESSURE_SIGNAL_DROPPED"):
        publisher(signal)

    with pytest.raises(ValueError, match="ingress"):
        OutboxPressurePublisher(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="signal"):
        publisher(object())  # type: ignore[arg-type]
