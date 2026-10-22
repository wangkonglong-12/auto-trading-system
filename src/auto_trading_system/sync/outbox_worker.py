"""One-tick Durable Outbox delivery with atomic receipts and bounded backoff."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from typing import Protocol, runtime_checkable
from uuid import uuid4

from auto_trading_system.domain import Clock, ShanghaiTimestamp
from auto_trading_system.journal import (
    AcknowledgeOutbox,
    CommitOutcome,
    DeadLetterOutbox,
    JournalCommitResult,
    JournalHealth,
    JournalTransaction,
    OutboxItem,
    OutboxState,
    RescheduleOutbox,
)
from auto_trading_system.sync.models import (
    MAX_OUTBOX_ATTEMPTS,
    OutboxDeliveryDisposition,
    OutboxDeliveryError,
    OutboxDeliveryReceipt,
    OutboxPressureLevel,
    OutboxPressureSignal,
    OutboxRunResult,
    OutboxRunStatus,
    OutboxWorkerError,
    OutboxWorkerSettings,
)


@runtime_checkable
class AsyncOutboxJournal(Protocol):
    async def fetch_outbox(
        self, *, limit: int, available_at: ShanghaiTimestamp
    ) -> tuple[OutboxItem, ...]: ...

    async def commit(self, transaction: JournalTransaction) -> JournalCommitResult: ...

    async def health(self) -> JournalHealth: ...


@runtime_checkable
class OutboxDispatcher(Protocol):
    async def deliver(self, items: tuple[OutboxItem, ...]) -> tuple[OutboxDeliveryReceipt, ...]: ...


PressureSink = Callable[[OutboxPressureSignal], None]
TransactionIdFactory = Callable[[], str]


class DurableOutboxWorker:
    """Process one due batch per scheduler tick without owning another timer loop."""

    def __init__(
        self,
        journal: AsyncOutboxJournal,
        dispatcher: OutboxDispatcher,
        *,
        clock: Clock,
        settings: OutboxWorkerSettings | None = None,
        pressure_sink: PressureSink,
        transaction_id_factory: TransactionIdFactory | None = None,
    ) -> None:
        if not isinstance(journal, AsyncOutboxJournal):
            raise ValueError("journal must implement AsyncOutboxJournal")
        if not isinstance(dispatcher, OutboxDispatcher):
            raise ValueError("dispatcher must implement OutboxDispatcher")
        if not callable(getattr(clock, "now", None)):
            raise ValueError("clock must implement Clock")
        if not callable(pressure_sink):
            raise ValueError("pressure_sink must be callable")
        effective_settings = settings or OutboxWorkerSettings()
        if not isinstance(effective_settings, OutboxWorkerSettings):
            raise ValueError("settings must be OutboxWorkerSettings")
        effective_id_factory = transaction_id_factory or _new_transaction_id
        if not callable(effective_id_factory):
            raise ValueError("transaction_id_factory must be callable")
        self._journal = journal
        self._dispatcher = dispatcher
        self._clock = clock
        self._settings = effective_settings
        self._pressure_sink = pressure_sink
        self._transaction_id_factory = effective_id_factory
        self._run_lock = asyncio.Lock()
        self._last_pressure_level: OutboxPressureLevel | None = None

    async def run_once(self) -> OutboxRunResult:
        if self._run_lock.locked():
            raise OutboxWorkerError(
                "OUTBOX_RUN_IN_PROGRESS",
                "an outbox delivery run is already active",
                retryable=True,
            )
        async with self._run_lock:
            now = self._clock.now()
            before = await self._journal.health()
            self._publish_pressure(before.pending_outbox, now)
            if not before.writable:
                raise OutboxWorkerError(
                    "OUTBOX_JOURNAL_UNWRITABLE",
                    "outbox delivery cannot safely record receipts",
                    retryable=False,
                )

            items = await self._journal.fetch_outbox(
                limit=self._settings.batch_size,
                available_at=now,
            )
            self._validate_fetched_items(items)
            if not items:
                return OutboxRunResult(
                    OutboxRunStatus.IDLE,
                    fetched=0,
                    acknowledged=0,
                    retried=0,
                    dead_lettered=0,
                    pending_before=before.pending_outbox,
                    pending_after=before.pending_outbox,
                    pressure_level=self._settings.pressure_level(before.pending_outbox),
                )

            receipts, delivery_error = await self._deliver_or_reschedule(items)
            transaction = self._receipt_transaction(items, receipts, now)
            commit = await self._journal.commit(transaction)
            self._validate_commit(transaction, commit)
            after = await self._journal.health()
            pressure = self._publish_pressure(after.pending_outbox, self._clock.now())
            acknowledged = sum(
                receipt.disposition is OutboxDeliveryDisposition.ACKNOWLEDGE for receipt in receipts
            )
            retried = sum(
                receipt.disposition is OutboxDeliveryDisposition.RETRY for receipt in receipts
            )
            dead_lettered = sum(
                receipt.disposition is OutboxDeliveryDisposition.DEAD_LETTER for receipt in receipts
            )
            status = (
                OutboxRunStatus.TRANSPORT_FAILURE_RESCHEDULED
                if delivery_error is not None
                else OutboxRunStatus.APPLIED
            )
            return OutboxRunResult(
                status,
                fetched=len(items),
                acknowledged=acknowledged,
                retried=retried,
                dead_lettered=dead_lettered,
                pending_before=before.pending_outbox,
                pending_after=after.pending_outbox,
                pressure_level=pressure.level,
                commit_sequence=commit.commit_sequence,
                delivery_error_code=None if delivery_error is None else delivery_error.code,
                delivery_error_retryable=(
                    None if delivery_error is None else delivery_error.retryable
                ),
            )

    def _validate_fetched_items(self, items: object) -> None:
        if not isinstance(items, tuple):
            raise OutboxWorkerError(
                "OUTBOX_FETCH_INVALID",
                "journal returned a non-immutable outbox batch",
                retryable=False,
            )
        if len(items) > self._settings.batch_size:
            raise OutboxWorkerError(
                "OUTBOX_FETCH_INVALID",
                "journal returned more items than the requested batch size",
                retryable=False,
            )
        if any(
            not isinstance(item, OutboxItem) or item.state is not OutboxState.PENDING
            for item in items
        ):
            raise OutboxWorkerError(
                "OUTBOX_FETCH_INVALID",
                "journal returned an invalid or non-pending outbox item",
                retryable=False,
            )
        identities = {(item.outbox_id, item.document.content_hash) for item in items}
        if len(identities) != len(items):
            raise OutboxWorkerError(
                "OUTBOX_FETCH_INVALID",
                "journal returned duplicate outbox item identities",
                retryable=False,
            )

    async def _deliver_or_reschedule(
        self, items: tuple[OutboxItem, ...]
    ) -> tuple[tuple[OutboxDeliveryReceipt, ...], OutboxDeliveryError | None]:
        try:
            receipts: object = await self._dispatcher.deliver(items)
        except asyncio.CancelledError:
            raise
        except OutboxDeliveryError as exc:
            return self._retry_all(items, exc.code), exc
        except Exception as exc:
            error = OutboxDeliveryError(
                "OUTBOX_DELIVERY_UNEXPECTED",
                "outbox dispatcher raised an unexpected error",
                retryable=True,
            )
            error.__cause__ = exc
            return self._retry_all(items, error.code), error
        try:
            validated = self._validate_receipts(items, receipts)
        except (TypeError, ValueError) as exc:
            error = OutboxDeliveryError(
                "OUTBOX_RECEIPT_INVALID",
                "dispatcher returned an invalid receipt set",
                retryable=False,
            )
            error.__cause__ = exc
            return self._retry_all(items, error.code), error
        return validated, None

    def _retry_all(
        self, items: tuple[OutboxItem, ...], reason_code: str
    ) -> tuple[OutboxDeliveryReceipt, ...]:
        return tuple(
            OutboxDeliveryReceipt(
                item.outbox_id,
                item.document.content_hash,
                OutboxDeliveryDisposition.RETRY,
                reason_code,
            )
            for item in items
        )

    @staticmethod
    def _validate_receipts(
        items: tuple[OutboxItem, ...], receipts: object
    ) -> tuple[OutboxDeliveryReceipt, ...]:
        if not isinstance(receipts, tuple) or not all(
            isinstance(receipt, OutboxDeliveryReceipt) for receipt in receipts
        ):
            raise TypeError("receipts must be an immutable typed tuple")
        typed_receipts = receipts
        expected = {(item.outbox_id, item.document.content_hash) for item in items}
        actual = {(receipt.outbox_id, receipt.content_hash) for receipt in typed_receipts}
        if len(typed_receipts) != len(actual) or actual != expected:
            raise ValueError("receipts must match every fetched outbox item exactly once")
        by_identity = {
            (receipt.outbox_id, receipt.content_hash): receipt for receipt in typed_receipts
        }
        return tuple(by_identity[(item.outbox_id, item.document.content_hash)] for item in items)

    def _receipt_transaction(
        self,
        items: tuple[OutboxItem, ...],
        receipts: tuple[OutboxDeliveryReceipt, ...],
        now: ShanghaiTimestamp,
    ) -> JournalTransaction:
        operations: list[AcknowledgeOutbox | RescheduleOutbox | DeadLetterOutbox] = []
        item_by_identity = {(item.outbox_id, item.document.content_hash): item for item in items}
        for receipt in receipts:
            identity = (receipt.outbox_id, receipt.content_hash)
            item = item_by_identity[identity]
            if receipt.disposition is OutboxDeliveryDisposition.ACKNOWLEDGE:
                operations.append(AcknowledgeOutbox(*identity))
            elif receipt.disposition is OutboxDeliveryDisposition.DEAD_LETTER:
                assert receipt.reason_code is not None
                operations.append(DeadLetterOutbox(*identity, receipt.reason_code))
            else:
                if item.attempts >= MAX_OUTBOX_ATTEMPTS:
                    raise OutboxWorkerError(
                        "OUTBOX_ATTEMPT_EXHAUSTED",
                        "outbox retry counter is exhausted",
                        retryable=False,
                    )
                attempts = item.attempts + 1
                available_at = ShanghaiTimestamp(
                    now.value + timedelta(seconds=self._settings.retry_delay_seconds(attempts))
                )
                assert receipt.reason_code is not None
                operations.append(
                    RescheduleOutbox(
                        item.outbox_id,
                        item.document.content_hash,
                        attempts,
                        available_at,
                        receipt.reason_code,
                    )
                )
        return JournalTransaction(self._transaction_id_factory(), tuple(operations))

    @staticmethod
    def _validate_commit(transaction: JournalTransaction, result: object) -> None:
        if (
            not isinstance(result, JournalCommitResult)
            or result.transaction_id != transaction.transaction_id
            or result.persisted_events
            or result.outcome not in {CommitOutcome.COMMITTED, CommitOutcome.DUPLICATE}
        ):
            raise OutboxWorkerError(
                "OUTBOX_COMMIT_RESULT_INVALID",
                "persistence returned an invalid receipt transaction result",
                retryable=False,
            )

    def _publish_pressure(
        self, pending: int, observed_at: ShanghaiTimestamp
    ) -> OutboxPressureSignal:
        level = self._settings.pressure_level(pending)
        signal = OutboxPressureSignal(
            level,
            pending,
            self._settings.high_watermark,
            self._settings.critical_watermark,
            observed_at,
        )
        if level is not self._last_pressure_level:
            self._pressure_sink(signal)
            self._last_pressure_level = level
        return signal


def _new_transaction_id() -> str:
    return f"txn_outbox_{uuid4().hex}"
