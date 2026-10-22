from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field

from auto_trading_system.domain import ShanghaiTimestamp, SystemClock
from auto_trading_system.journal import (
    CanonicalJsonDocument,
    CommitOutcome,
    JournalCommitResult,
    JournalHealth,
    JournalHealthStatus,
    JournalTransaction,
    OutboxItem,
    OutboxKind,
    OutboxState,
)
from auto_trading_system.protocol.canonical_json import canonical_json_bytes
from auto_trading_system.runtime import (
    BoundedEventIngress,
    EventPriority,
    PublishOutcome,
    RuntimeEventSource,
)
from auto_trading_system.sync import (
    DurableOutboxWorker,
    OutboxDeliveryDisposition,
    OutboxDeliveryReceipt,
    OutboxWorkerSettings,
)
from auto_trading_system.testing.performance import (
    PerformanceThresholds,
    run_market_ingress_performance,
)


@dataclass(frozen=True, slots=True)
class FixedClock:
    def now(self) -> ShanghaiTimestamp:
        return ShanghaiTimestamp.parse("2026-09-04T14:35:00+08:00")


@dataclass
class OneItemJournal:
    item: OutboxItem
    pending_counts: list[int] = field(default_factory=lambda: [1, 0])
    commits: list[JournalTransaction] = field(default_factory=list)

    async def health(self) -> JournalHealth:
        pending = self.pending_counts.pop(0) if len(self.pending_counts) > 1 else 0
        return JournalHealth(JournalHealthStatus.HEALTHY, True, 1, 4096, pending, 0)

    async def fetch_outbox(
        self,
        *,
        limit: int,
        available_at: ShanghaiTimestamp,
    ) -> tuple[OutboxItem, ...]:
        del available_at
        return (self.item,)[:limit]

    async def commit(self, transaction: JournalTransaction) -> JournalCommitResult:
        self.commits.append(transaction)
        return JournalCommitResult(transaction.transaction_id, CommitOutcome.COMMITTED, 1)


@dataclass
class BlockingDispatcher:
    entered: asyncio.Event
    release: asyncio.Event

    async def deliver(
        self,
        items: tuple[OutboxItem, ...],
    ) -> tuple[OutboxDeliveryReceipt, ...]:
        self.entered.set()
        await self.release.wait()
        item = items[0]
        return (
            OutboxDeliveryReceipt(
                item.outbox_id,
                item.document.content_hash,
                OutboxDeliveryDisposition.ACKNOWLEDGE,
            ),
        )


def _outbox_item() -> OutboxItem:
    body = canonical_json_bytes({"event": "performance"}, exclude=frozenset())
    document = CanonicalJsonDocument(
        "doc_performance_0001",
        "sha256:" + hashlib.sha256(body).hexdigest(),
        body,
    )
    created_at = FixedClock().now()
    return OutboxItem(
        1,
        OutboxKind.DOMAIN_EVENT,
        OutboxState.PENDING,
        document,
        0,
        created_at,
        created_at,
    )


def test_fifteen_symbol_callback_burst_meets_local_resource_boundaries() -> None:
    thresholds = PerformanceThresholds()

    result = run_market_ingress_performance(symbol_count=15, events_per_symbol=500)

    assert result.events == 7_500
    assert result.accepted == result.drained == result.events
    assert result.overflows == 0
    assert result.passes(thresholds), result.to_mapping(thresholds)


def test_blocked_outbox_delivery_does_not_block_critical_trading_ingress() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        journal = OneItemJournal(_outbox_item())
        worker = DurableOutboxWorker(
            journal,
            BlockingDispatcher(entered, release),
            clock=FixedClock(),
            settings=OutboxWorkerSettings(high_watermark=10, critical_watermark=20),
            pressure_sink=lambda _: None,
            transaction_id_factory=lambda: "txn_performance_outbox_0001",
        )
        ingress = BoundedEventIngress[str](
            normal_capacity=1,
            critical_capacity=1,
            monotonic_clock=SystemClock(),
        )
        upload = asyncio.create_task(worker.run_once())
        await asyncio.wait_for(entered.wait(), timeout=1)

        started = time.perf_counter_ns()
        outcome = ingress.publish(
            "broker-critical-callback",
            source=RuntimeEventSource.BROKER,
            priority=EventPriority.CRITICAL,
        )
        elapsed_microseconds = (time.perf_counter_ns() - started) / 1_000

        assert outcome is PublishOutcome.ACCEPTED
        assert elapsed_microseconds <= 5_000
        release.set()
        result = await asyncio.wait_for(upload, timeout=1)
        assert result.acknowledged == 1
        assert len(journal.commits) == 1

    asyncio.run(scenario())
