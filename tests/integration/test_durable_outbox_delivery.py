from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import httpx

from auto_trading_system.domain import ShanghaiTimestamp
from auto_trading_system.journal import (
    AggregateType,
    AppendDomainEvent,
    CommitOutcome,
    EventDraft,
    EventSeverity,
    EventSource,
    EventType,
    JournalTransaction,
    OutboxKind,
)
from auto_trading_system.journal.sqlite import SQLiteJournalSettings, SQLiteLocalJournal
from auto_trading_system.journal.writer import PersistenceWriter
from auto_trading_system.protocol.canonical_json import canonical_json_bytes
from auto_trading_system.sync import (
    DurableOutboxWorker,
    OutboxPressureLevel,
    OutboxPressureSignal,
    OutboxRunStatus,
    OutboxWorkerSettings,
    RoutedOutboxDispatcher,
    SignedEventBatchHttpTransport,
)
from auto_trading_system.testing import (
    EventBatchScenario,
    MockCentralService,
    MockResponseFault,
    ReceiptOutcome,
)

SECRET = b"durable-outbox-integration-secret-at-least-32-bytes"
KEY_ID = "key_outbox_integration"
NODE_ID = "node-outbox-001"
ACCOUNT_ID = "acct_outbox_001"
EPOCH_SECONDS = 1_788_504_300


@dataclass
class MutableClock:
    value: ShanghaiTimestamp

    def now(self) -> ShanghaiTimestamp:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value = ShanghaiTimestamp(self.value.value + timedelta(seconds=seconds))


@dataclass(frozen=True, slots=True)
class ServiceEpochClock:
    service: MockCentralService

    def now_epoch_seconds(self) -> int:
        return self.service.epoch_seconds


def runtime_event(number: int, occurred_at: ShanghaiTimestamp) -> AppendDomainEvent:
    return AppendDomainEvent(
        EventDraft(
            event_id=f"evt_{number:032x}",
            node_id=NODE_ID,
            account_id=ACCOUNT_ID,
            boot_id="boot_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            correlation_id=f"corr_runtime_20260904_{number:04d}",
            causation_id=None,
            source=EventSource.RUNTIME,
            severity=EventSeverity.INFO,
            event_type=EventType.RUNTIME_STATE_CHANGED,
            aggregate_type=AggregateType.RUNTIME,
            aggregate_id=NODE_ID,
            trade_date=occurred_at.trading_date,
            occurred_at=occurred_at,
            payload=canonical_json_bytes(
                {
                    "previous_state": "RECONCILING",
                    "new_state": "READY_PAUSED",
                    "reason_code": "STARTUP_RECONCILED",
                    "execution_mode": "SHADOW",
                },
                exclude=frozenset(),
            ),
        )
    )


def sqlite_writer(path: Path, clock: MutableClock) -> PersistenceWriter:
    return PersistenceWriter(
        SQLiteLocalJournal(
            SQLiteJournalSettings(
                path,
                ACCOUNT_ID,
                minimum_free_bytes=0,
                maximum_pending_outbox=100,
            ),
            clock=clock,
        ),
        capacity=16,
    )


def central_service() -> MockCentralService:
    return MockCentralService(
        key_id=KEY_ID,
        secret=SECRET,
        node_id=NODE_ID,
        account_id=ACCOUNT_ID,
        epoch_seconds=EPOCH_SECONDS,
    )


def outbox_worker(
    writer: PersistenceWriter,
    client: httpx.AsyncClient,
    service: MockCentralService,
    clock: MutableClock,
    signals: list[OutboxPressureSignal],
    *,
    high_watermark: int = 2,
    critical_watermark: int = 3,
) -> DurableOutboxWorker:
    event_transport = SignedEventBatchHttpTransport(
        client,
        base_url="https://central.test/api/v1/trading",
        node_id=NODE_ID,
        account_id=ACCOUNT_ID,
        key_id=KEY_ID,
        secret=SECRET,
        clock=clock,
        epoch_clock=ServiceEpochClock(service),
    )
    return DurableOutboxWorker(
        writer,
        RoutedOutboxDispatcher({OutboxKind.DOMAIN_EVENT: event_transport}),
        clock=clock,
        settings=OutboxWorkerSettings(
            batch_size=50,
            retry_base_seconds=2,
            retry_max_seconds=300,
            high_watermark=high_watermark,
            critical_watermark=critical_watermark,
        ),
        pressure_sink=signals.append,
    )


async def seed_events(
    writer: PersistenceWriter,
    clock: MutableClock,
    count: int,
) -> None:
    result = await writer.commit(
        JournalTransaction(
            f"txn_seed_outbox_{count:08d}",
            tuple(runtime_event(number, clock.now()) for number in range(1, count + 1)),
        )
    )
    assert result.outcome is CommitOutcome.COMMITTED


async def exercise_mixed_receipts_and_restart(path: Path) -> None:
    clock = MutableClock(ShanghaiTimestamp.parse("2026-09-04T08:45:00+08:00"))
    service = central_service()
    service.queue_event_batch_scenario(
        EventBatchScenario(
            (
                ReceiptOutcome.ACCEPTED,
                ReceiptOutcome.RETRYABLE_REJECTED,
                ReceiptOutcome.PERMANENT_REJECTED,
            )
        )
    )
    signals: list[OutboxPressureSignal] = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=service.app),
        base_url="https://central.test",
    ) as client:
        writer = sqlite_writer(path, clock)
        await writer.start()
        await seed_events(writer, clock, 3)
        first = await outbox_worker(writer, client, service, clock, signals).run_once()
        assert first.status is OutboxRunStatus.APPLIED
        assert (first.acknowledged, first.retried, first.dead_lettered) == (1, 1, 1)
        assert first.pending_after == 1
        assert await writer.fetch_outbox(limit=50, available_at=clock.now()) == ()
        await writer.stop()

        clock.advance(2)
        service.advance_time(2)
        service.queue_event_batch_scenario(EventBatchScenario((ReceiptOutcome.ACCEPTED,)))
        recovered_writer = sqlite_writer(path, clock)
        await recovered_writer.start()
        recovered_signals: list[OutboxPressureSignal] = []
        second = await outbox_worker(
            recovered_writer,
            client,
            service,
            clock,
            recovered_signals,
        ).run_once()
        assert second.acknowledged == 1
        assert second.pending_after == 0
        assert (await recovered_writer.health()).pending_outbox == 0
        await recovered_writer.stop()

    assert [signal.level for signal in signals] == [
        OutboxPressureLevel.CRITICAL,
        OutboxPressureLevel.NORMAL,
    ]
    assert signals[0].blocks_new_buys is True
    assert [signal.level for signal in recovered_signals] == [OutboxPressureLevel.NORMAL]
    received_batches = service.received_messages
    assert [len(message.payload["events"]) for message in received_batches] == [3, 1]


def test_mixed_receipts_survive_writer_restart_and_retry_when_due(tmp_path: Path) -> None:
    asyncio.run(exercise_mixed_receipts_and_restart((tmp_path / "outbox.db").resolve()))


async def exercise_lost_authenticated_response(path: Path) -> None:
    clock = MutableClock(ShanghaiTimestamp.parse("2026-09-04T08:45:00+08:00"))
    service = central_service()
    service.queue_response_fault(MockResponseFault.INVALID_SIGNATURE)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=service.app),
        base_url="https://central.test",
    ) as client:
        writer = sqlite_writer(path, clock)
        await writer.start()
        await seed_events(writer, clock, 1)
        first = await outbox_worker(
            writer,
            client,
            service,
            clock,
            [],
            high_watermark=10,
            critical_watermark=20,
        ).run_once()
        assert first.status is OutboxRunStatus.TRANSPORT_FAILURE_RESCHEDULED
        assert first.delivery_error_code == "OUTBOX_RESPONSE_AUTH_FAILED"
        assert first.pending_after == 1
        await writer.stop()

        clock.advance(2)
        service.advance_time(2)
        service.queue_event_batch_scenario(EventBatchScenario((ReceiptOutcome.DUPLICATE,)))
        recovered_writer = sqlite_writer(path, clock)
        await recovered_writer.start()
        second = await outbox_worker(
            recovered_writer,
            client,
            service,
            clock,
            [],
            high_watermark=10,
            critical_watermark=20,
        ).run_once()
        assert second.acknowledged == 1
        assert second.pending_after == 0
        await recovered_writer.stop()

    assert len(service.received_messages) == 2
    first_event = service.received_messages[0].payload["events"][0]
    second_event = service.received_messages[1].payload["events"][0]
    assert first_event["event_id"] == second_event["event_id"]
    assert first_event["event_hash"] == second_event["event_hash"]


def test_lost_or_untrusted_response_reuploads_identical_event_after_restart(tmp_path: Path) -> None:
    asyncio.run(exercise_lost_authenticated_response((tmp_path / "lost-response.db").resolve()))
