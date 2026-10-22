from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, get_ident

import pytest

from auto_trading_system.apps.trade_runtime.bootstrap import build_persistence_writer
from auto_trading_system.config import RuntimeConfig
from auto_trading_system.domain import ShanghaiTimestamp, TradingDate
from auto_trading_system.journal import (
    CommitOutcome,
    JournalCommitResult,
    JournalHealth,
    JournalHealthStatus,
    JournalOpenResult,
    JournalRecoverySnapshot,
    JournalStatus,
    JournalTransaction,
    LocalJournalError,
    MaintenanceRequest,
    MaintenanceResult,
    OutboxItem,
)
from auto_trading_system.journal.sqlite import SQLiteJournalSettings, SQLiteLocalJournal
from auto_trading_system.journal.writer import (
    PersistenceWriter,
    PersistenceWriterError,
    PersistenceWriterStatus,
)


@dataclass
class BlockingJournal:
    block_open: bool = False
    block_commit: bool = False
    block_close: bool = False
    unexpected_commit_error: bool = False
    open_error: BaseException | None = None
    commit_error: BaseException | None = None
    close_error: BaseException | None = None
    _status: JournalStatus = JournalStatus.NEW
    open_entered: Event = field(default_factory=Event)
    open_release: Event = field(default_factory=Event)
    commit_entered: Event = field(default_factory=Event)
    commit_release: Event = field(default_factory=Event)
    close_entered: Event = field(default_factory=Event)
    close_release: Event = field(default_factory=Event)
    owner_thread_id: int | None = None
    commit_calls: int = 0
    close_calls: int = 0

    @property
    def status(self) -> JournalStatus:
        return self._status

    def open(self) -> JournalOpenResult:
        self.owner_thread_id = get_ident()
        self.open_entered.set()
        if self.block_open:
            self.open_release.wait()
        if self.open_error is not None:
            raise self.open_error
        self._status = JournalStatus.OPEN
        return JournalOpenResult(1, "journal_" + "1" * 32, True)

    def commit(self, transaction: JournalTransaction) -> JournalCommitResult:
        assert get_ident() == self.owner_thread_id
        self.commit_calls += 1
        self.commit_entered.set()
        if self.block_commit:
            self.commit_release.wait()
        if self.unexpected_commit_error:
            raise RuntimeError("unexpected")
        if self.commit_error is not None:
            raise self.commit_error
        return JournalCommitResult(transaction.transaction_id, CommitOutcome.COMMITTED, 1)

    def load_recovery(self, *, account_id: str, trade_date: TradingDate) -> JournalRecoverySnapshot:
        assert get_ident() == self.owner_thread_id
        return JournalRecoverySnapshot(
            account_id,
            trade_date,
            (),
            (),
            (),
            (),
            (),
            (),
            (),
            0,
        )

    def fetch_outbox(
        self, *, limit: int, available_at: ShanghaiTimestamp
    ) -> tuple[OutboxItem, ...]:
        assert get_ident() == self.owner_thread_id
        return ()

    def health(self) -> JournalHealth:
        assert get_ident() == self.owner_thread_id
        return JournalHealth(JournalHealthStatus.HEALTHY, True, 1, 1, 0, 1)

    def maintain(self, request: MaintenanceRequest) -> MaintenanceResult:
        raise NotImplementedError

    def close(self) -> None:
        assert get_ident() == self.owner_thread_id
        self.close_calls += 1
        self.close_entered.set()
        if self.block_close:
            self.close_release.wait()
        if self.close_error is not None:
            raise self.close_error
        self._status = JournalStatus.CLOSED


def empty_transaction(number: int = 1) -> JournalTransaction:
    from auto_trading_system.journal import CheckpointRecord, PutCheckpoint

    return JournalTransaction(
        f"txn_writer_{number:08d}",
        (PutCheckpoint(CheckpointRecord("runtime", "writer_probe", b"1", number)),),
    )


def assert_status(writer: PersistenceWriter, expected: PersistenceWriterStatus) -> None:
    assert writer.status is expected


def test_writer_owns_real_sqlite_connection_and_serves_async_calls(tmp_path: Path) -> None:
    async def scenario() -> None:
        journal = SQLiteLocalJournal(
            SQLiteJournalSettings(
                (tmp_path / "journal.db").resolve(),
                "acct_demo_001",
                minimum_free_bytes=0,
            )
        )
        writer = PersistenceWriter(journal, capacity=8)
        assert_status(writer, PersistenceWriterStatus.NEW)
        await writer.start()
        assert writer.open_result is not None
        assert_status(writer, PersistenceWriterStatus.RUNNING)
        with pytest.raises(LocalJournalError, match="THREAD_OWNERSHIP"):
            journal.health()

        result = await writer.commit(empty_transaction())
        assert result.commit_sequence == 1
        health = await writer.health()
        assert health.status is JournalHealthStatus.HEALTHY
        snapshot = writer.snapshot()
        assert snapshot.accepted == 2
        assert snapshot.completed == 2
        await writer.stop()
        await writer.stop()
        assert_status(writer, PersistenceWriterStatus.STOPPED)

    asyncio.run(scenario())


def test_bootstrap_composes_account_scoped_journal_under_storage_root(tmp_path: Path) -> None:
    async def scenario() -> None:
        example_path = (
            Path(__file__).parents[2] / "config" / "v1" / "runtime.read-only.example.json"
        )
        payload = json.loads(example_path.read_text(encoding="utf-8"))
        payload["storage"]["root"] = str(tmp_path.resolve())
        settings = RuntimeConfig.model_validate(payload)
        writer = build_persistence_writer(settings, queue_capacity=4)
        await writer.start()
        assert writer.open_result is not None
        assert (tmp_path / "data" / "local_journal.db").is_file()
        await writer.stop()

    asyncio.run(scenario())


def test_bounded_queue_rejects_without_blocking_and_drains_before_stop() -> None:
    async def scenario() -> None:
        journal = BlockingJournal(block_commit=True)
        writer = PersistenceWriter(journal, capacity=1)
        await writer.start()
        first = asyncio.create_task(writer.commit(empty_transaction(1)))
        assert await asyncio.to_thread(journal.commit_entered.wait, 1)
        second = asyncio.create_task(writer.commit(empty_transaction(2)))
        await asyncio.sleep(0)
        with pytest.raises(PersistenceWriterError) as full:
            await writer.commit(empty_transaction(3))
        assert full.value.code == "PERSISTENCE_QUEUE_FULL"
        assert full.value.retryable is True
        journal.commit_release.set()
        await asyncio.gather(first, second)
        await writer.stop()
        snapshot = writer.snapshot()
        assert snapshot.accepted == 2
        assert snapshot.completed == 2
        assert snapshot.rejected == 1

    asyncio.run(scenario())


def test_submitted_commit_reaches_terminal_result_before_cancellation_returns() -> None:
    async def scenario() -> None:
        journal = BlockingJournal(block_commit=True)
        writer = PersistenceWriter(journal)
        await writer.start()
        task = asyncio.create_task(writer.commit(empty_transaction()))
        assert await asyncio.to_thread(journal.commit_entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        journal.commit_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert journal.commit_calls == 1
        assert writer.snapshot().completed == 1
        await writer.stop()

    asyncio.run(scenario())


def test_cancelled_start_signals_thread_and_closes_journal() -> None:
    async def scenario() -> None:
        journal = BlockingJournal(block_open=True)
        writer = PersistenceWriter(journal)
        task = asyncio.create_task(writer.start())
        assert await asyncio.to_thread(journal.open_entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        journal.open_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert journal.close_calls == 1
        assert_status(writer, PersistenceWriterStatus.STOPPED)

    asyncio.run(scenario())


def test_cancelled_stop_can_be_awaited_again_without_stuck_state() -> None:
    async def scenario() -> None:
        journal = BlockingJournal(block_close=True)
        writer = PersistenceWriter(journal)
        await writer.start()
        stop_task = asyncio.create_task(writer.stop())
        assert await asyncio.to_thread(journal.close_entered.wait, 1)
        stop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stop_task
        assert_status(writer, PersistenceWriterStatus.STOPPING)
        journal.close_release.set()
        await writer.stop()
        assert_status(writer, PersistenceWriterStatus.STOPPED)

    asyncio.run(scenario())


def test_unknown_journal_exception_fails_writer_and_pending_requests() -> None:
    async def scenario() -> None:
        journal = BlockingJournal(unexpected_commit_error=True)
        writer = PersistenceWriter(journal, capacity=2)
        await writer.start()
        first = asyncio.create_task(writer.commit(empty_transaction(1)))
        second = asyncio.create_task(writer.commit(empty_transaction(2)))
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert isinstance(results[0], RuntimeError)
        assert isinstance(results[1], PersistenceWriterError)
        assert_status(writer, PersistenceWriterStatus.FAILED)
        await writer.stop()
        assert_status(writer, PersistenceWriterStatus.STOPPED)

    asyncio.run(scenario())


def test_expected_journal_error_does_not_poison_writer() -> None:
    async def scenario() -> None:
        journal = BlockingJournal(
            commit_error=LocalJournalError(
                "EXPECTED_CONFLICT", "request was rejected", retryable=False
            )
        )
        writer = PersistenceWriter(journal)
        await writer.start()
        with pytest.raises(LocalJournalError, match="EXPECTED_CONFLICT"):
            await writer.commit(empty_transaction())
        assert_status(writer, PersistenceWriterStatus.RUNNING)
        assert (await writer.health()).status is JournalHealthStatus.HEALTHY
        await writer.stop()

    asyncio.run(scenario())


def test_open_and_close_failures_have_terminal_writer_state() -> None:
    async def scenario() -> None:
        open_failure = PersistenceWriter(
            BlockingJournal(
                open_error=LocalJournalError("JOURNAL_OPEN_FAILED", "open failed", retryable=False)
            )
        )
        with pytest.raises(LocalJournalError, match="JOURNAL_OPEN_FAILED"):
            await open_failure.start()
        assert_status(open_failure, PersistenceWriterStatus.FAILED)

        close_failure = PersistenceWriter(BlockingJournal(close_error=RuntimeError("close failed")))
        await close_failure.start()
        with pytest.raises(PersistenceWriterError, match="CLOSE_FAILED"):
            await close_failure.stop()
        assert_status(close_failure, PersistenceWriterStatus.FAILED)

    asyncio.run(scenario())


def test_stop_timeout_can_be_completed_after_blocked_close_recovers() -> None:
    async def scenario() -> None:
        journal = BlockingJournal(block_close=True)
        writer = PersistenceWriter(journal, stop_timeout_seconds=0.1)
        await writer.start()
        with pytest.raises(PersistenceWriterError, match="STOP_TIMEOUT"):
            await writer.stop()
        assert_status(writer, PersistenceWriterStatus.FAILED)
        journal.close_release.set()
        await writer.stop()
        assert_status(writer, PersistenceWriterStatus.STOPPED)

    asyncio.run(scenario())


def test_writer_rejects_calls_before_start_after_stop_and_from_another_loop() -> None:
    async def scenario() -> None:
        writer = PersistenceWriter(BlockingJournal())
        with pytest.raises(PersistenceWriterError, match="NOT_RUNNING"):
            await writer.health()
        await writer.start()

        def call_from_other_loop() -> BaseException | None:
            try:
                asyncio.run(writer.health())
            except BaseException as exc:
                return exc
            return None

        loop_error = await asyncio.to_thread(call_from_other_loop)
        assert isinstance(loop_error, PersistenceWriterError)
        assert loop_error.code == "PERSISTENCE_WRITER_LOOP_MISMATCH"
        await writer.stop()
        with pytest.raises(PersistenceWriterError, match="NOT_RUNNING"):
            await writer.health()

    asyncio.run(scenario())


@pytest.mark.parametrize("capacity", [0, 100_001, True])
def test_writer_rejects_invalid_capacity(capacity: int) -> None:
    with pytest.raises(ValueError, match="capacity"):
        PersistenceWriter(BlockingJournal(), capacity=capacity)


@pytest.mark.parametrize("timeout", [0, 301, True])
def test_writer_rejects_invalid_stop_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="stop_timeout_seconds"):
        PersistenceWriter(BlockingJournal(), stop_timeout_seconds=timeout)


def test_writer_requires_local_journal_protocol() -> None:
    with pytest.raises(ValueError, match="LocalJournal"):
        PersistenceWriter(object())  # type: ignore[arg-type]


def test_writer_stop_before_start_and_repeated_start_are_explicit() -> None:
    async def scenario() -> None:
        stopped = PersistenceWriter(BlockingJournal())
        await stopped.stop()
        assert_status(stopped, PersistenceWriterStatus.STOPPED)
        with pytest.raises(PersistenceWriterError, match="NOT_NEW"):
            await stopped.start()

        running = PersistenceWriter(BlockingJournal())
        await running.start()
        with pytest.raises(PersistenceWriterError, match="NOT_NEW"):
            await running.start()
        await running.stop()

    asyncio.run(scenario())
