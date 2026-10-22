"""Bounded asynchronous facade that gives one thread exclusive LocalJournal ownership."""

from __future__ import annotations

import asyncio
import queue
import threading
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Generic, TypeVar, cast

from auto_trading_system.domain import ShanghaiTimestamp, TradingDate
from auto_trading_system.journal.models import (
    JournalCommitResult,
    JournalHealth,
    JournalRecoverySnapshot,
    JournalTransaction,
    MaintenanceRequest,
    MaintenanceResult,
    OutboxItem,
)
from auto_trading_system.journal.port import (
    JournalOpenResult,
    JournalStatus,
    LocalJournal,
    LocalJournalError,
)

ResultT = TypeVar("ResultT")


class PersistenceWriterStatus(StrEnum):
    NEW = "NEW"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class PersistenceWriterError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class PersistenceQueueSnapshot:
    status: PersistenceWriterStatus
    capacity: int
    pending: int
    accepted: int
    completed: int
    rejected: int


class _Operation(StrEnum):
    COMMIT = "COMMIT"
    LOAD_RECOVERY = "LOAD_RECOVERY"
    FETCH_OUTBOX = "FETCH_OUTBOX"
    HEALTH = "HEALTH"
    MAINTAIN = "MAINTAIN"


@dataclass(frozen=True, slots=True)
class _Call(Generic[ResultT]):
    operation: _Operation
    arguments: tuple[object, ...]
    future: asyncio.Future[ResultT]


class _Stop:
    pass


_STOP = _Stop()


class PersistenceWriter:
    """Own a LocalJournal in one dedicated thread behind a bounded request queue."""

    def __init__(
        self,
        journal: LocalJournal,
        *,
        capacity: int = 1024,
        stop_timeout_seconds: float = 10.0,
    ) -> None:
        if not isinstance(journal, LocalJournal):
            raise ValueError("journal must implement LocalJournal")
        if type(capacity) is not int or not 1 <= capacity <= 100_000:
            raise ValueError("capacity must be between 1 and 100000")
        if type(stop_timeout_seconds) not in {int, float} or not (
            0.1 <= stop_timeout_seconds <= 300
        ):
            raise ValueError("stop_timeout_seconds must be between 0.1 and 300")
        self._journal = journal
        self._capacity = capacity
        self._stop_timeout_seconds = float(stop_timeout_seconds)
        self._requests: queue.Queue[_Call[object] | _Stop] = queue.Queue(maxsize=capacity + 1)
        self._state_lock = threading.Lock()
        self._status = PersistenceWriterStatus.NEW
        self._accepted = 0
        self._completed = 0
        self._rejected = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._open_result: JournalOpenResult | None = None
        self._start_future: asyncio.Future[JournalOpenResult] | None = None
        self._closed_future: asyncio.Future[None] | None = None

    @property
    def status(self) -> PersistenceWriterStatus:
        with self._state_lock:
            return self._status

    @property
    def open_result(self) -> JournalOpenResult | None:
        with self._state_lock:
            return self._open_result

    def snapshot(self) -> PersistenceQueueSnapshot:
        with self._state_lock:
            return PersistenceQueueSnapshot(
                status=self._status,
                capacity=self._capacity,
                pending=self._requests.qsize(),
                accepted=self._accepted,
                completed=self._completed,
                rejected=self._rejected,
            )

    async def start(self) -> None:
        with self._state_lock:
            if self._status is not PersistenceWriterStatus.NEW:
                raise PersistenceWriterError(
                    "PERSISTENCE_WRITER_NOT_NEW",
                    "persistence writer cannot be started",
                    retryable=False,
                )
            self._status = PersistenceWriterStatus.STARTING
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._start_future = loop.create_future()
        self._closed_future = loop.create_future()
        self._thread = threading.Thread(
            target=self._run,
            name="persistence-writer",
            daemon=True,
        )
        self._thread.start()
        try:
            result = await asyncio.shield(self._start_future)
        except asyncio.CancelledError:
            with self._state_lock:
                self._status = PersistenceWriterStatus.STOPPING
            self._requests.put_nowait(_STOP)
            try:
                await self._await_thread_closed()
            except BaseException:
                with self._state_lock:
                    self._status = PersistenceWriterStatus.FAILED
            else:
                with self._state_lock:
                    self._status = PersistenceWriterStatus.STOPPED
            raise
        except BaseException:
            with self._state_lock:
                self._status = PersistenceWriterStatus.FAILED
            await self._await_thread_closed()
            raise
        with self._state_lock:
            self._open_result = result
            self._status = PersistenceWriterStatus.RUNNING

    async def stop(self) -> None:
        signal_stop = False
        with self._state_lock:
            if self._status is PersistenceWriterStatus.STOPPED:
                return
            if self._status is PersistenceWriterStatus.NEW:
                self._status = PersistenceWriterStatus.STOPPED
                return
            if self._status not in {
                PersistenceWriterStatus.RUNNING,
                PersistenceWriterStatus.FAILED,
                PersistenceWriterStatus.STOPPING,
            }:
                raise PersistenceWriterError(
                    "PERSISTENCE_WRITER_NOT_STOPPABLE",
                    "persistence writer cannot be stopped from its current state",
                    retryable=False,
                )
            if self._status is not PersistenceWriterStatus.STOPPING:
                self._status = PersistenceWriterStatus.STOPPING
                signal_stop = True
        closed_future = self._closed_future
        thread = self._thread
        if closed_future is None or thread is None:
            with self._state_lock:
                self._status = PersistenceWriterStatus.FAILED
            raise PersistenceWriterError(
                "PERSISTENCE_WRITER_INTERNAL_ERROR",
                "persistence writer lifecycle is incomplete",
                retryable=False,
            )
        if signal_stop and not closed_future.done():
            self._requests.put_nowait(_STOP)
        try:
            await self._await_thread_closed()
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            with self._state_lock:
                self._status = PersistenceWriterStatus.FAILED
            raise PersistenceWriterError(
                "PERSISTENCE_WRITER_STOP_TIMEOUT",
                "persistence writer did not stop before the deadline",
                retryable=False,
            ) from exc
        except BaseException as exc:
            with self._state_lock:
                self._status = PersistenceWriterStatus.FAILED
            raise PersistenceWriterError(
                "PERSISTENCE_WRITER_CLOSE_FAILED",
                "persistence writer could not close its journal",
                retryable=False,
            ) from exc
        with self._state_lock:
            self._status = PersistenceWriterStatus.STOPPED

    async def commit(self, transaction: JournalTransaction) -> JournalCommitResult:
        return cast(JournalCommitResult, await self._submit(_Operation.COMMIT, transaction))

    async def load_recovery(
        self, *, account_id: str, trade_date: TradingDate
    ) -> JournalRecoverySnapshot:
        return cast(
            JournalRecoverySnapshot,
            await self._submit(_Operation.LOAD_RECOVERY, account_id, trade_date),
        )

    async def fetch_outbox(
        self, *, limit: int, available_at: ShanghaiTimestamp
    ) -> tuple[OutboxItem, ...]:
        return cast(
            tuple[OutboxItem, ...],
            await self._submit(_Operation.FETCH_OUTBOX, limit, available_at),
        )

    async def health(self) -> JournalHealth:
        return cast(JournalHealth, await self._submit(_Operation.HEALTH))

    async def maintain(self, request: MaintenanceRequest) -> MaintenanceResult:
        return cast(MaintenanceResult, await self._submit(_Operation.MAINTAIN, request))

    async def _submit(self, operation: _Operation, *arguments: object) -> object:
        with self._state_lock:
            if self._status is not PersistenceWriterStatus.RUNNING:
                raise PersistenceWriterError(
                    "PERSISTENCE_WRITER_NOT_RUNNING",
                    "persistence writer is not accepting requests",
                    retryable=False,
                )
        loop = asyncio.get_running_loop()
        if loop is not self._loop:
            raise PersistenceWriterError(
                "PERSISTENCE_WRITER_LOOP_MISMATCH",
                "persistence writer must be used from its owning event loop",
                retryable=False,
            )
        future: asyncio.Future[object] = loop.create_future()
        with self._state_lock:
            if self._status is not PersistenceWriterStatus.RUNNING:
                raise PersistenceWriterError(
                    "PERSISTENCE_WRITER_NOT_RUNNING",
                    "persistence writer is not accepting requests",
                    retryable=False,
                )
            if self._requests.qsize() >= self._capacity:
                self._rejected += 1
                raise PersistenceWriterError(
                    "PERSISTENCE_QUEUE_FULL",
                    "persistence request queue is full",
                    retryable=True,
                )
            self._requests.put_nowait(_Call(operation, arguments, future))
            self._accepted += 1
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            with suppress(Exception):
                await asyncio.shield(future)
            raise

    def _run(self) -> None:
        loop = self._loop
        start_future = self._start_future
        closed_future = self._closed_future
        if loop is None or start_future is None or closed_future is None:
            return  # pragma: no cover - start creates all three before the thread
        try:
            try:
                result = self._journal.open()
            except BaseException as exc:
                loop.call_soon_threadsafe(_set_future_exception, start_future, exc)
                return
            loop.call_soon_threadsafe(_set_future_result, start_future, result)
            while True:
                request = self._requests.get()
                if isinstance(request, _Stop):
                    break
                try:
                    result_value = self._execute(request)
                except BaseException as exc:
                    with self._state_lock:
                        self._completed += 1
                    loop.call_soon_threadsafe(_set_future_exception, request.future, exc)
                    fatal = (
                        not isinstance(exc, LocalJournalError)
                        or self._journal.status is JournalStatus.FAILED
                    )
                    if fatal:
                        with self._state_lock:
                            self._status = PersistenceWriterStatus.FAILED
                        self._fail_pending(loop)
                        break
                else:
                    with self._state_lock:
                        self._completed += 1
                    loop.call_soon_threadsafe(_set_future_result, request.future, result_value)
        finally:
            try:
                self._journal.close()
            except BaseException as exc:
                loop.call_soon_threadsafe(_set_future_exception, closed_future, exc)
            else:
                loop.call_soon_threadsafe(_set_future_result, closed_future, None)

    async def _await_thread_closed(self) -> None:
        closed_future = self._closed_future
        thread = self._thread
        if closed_future is None or thread is None:
            raise PersistenceWriterError(
                "PERSISTENCE_WRITER_INTERNAL_ERROR",
                "persistence writer lifecycle is incomplete",
                retryable=False,
            )
        await asyncio.wait_for(asyncio.shield(closed_future), timeout=self._stop_timeout_seconds)
        await asyncio.to_thread(thread.join, self._stop_timeout_seconds)
        if thread.is_alive():
            raise TimeoutError("persistence writer thread is still running")

    def _execute(self, request: _Call[object]) -> object:
        if request.operation is _Operation.COMMIT:
            return self._journal.commit(cast(JournalTransaction, request.arguments[0]))
        if request.operation is _Operation.LOAD_RECOVERY:
            return self._journal.load_recovery(
                account_id=cast(str, request.arguments[0]),
                trade_date=cast(TradingDate, request.arguments[1]),
            )
        if request.operation is _Operation.FETCH_OUTBOX:
            return self._journal.fetch_outbox(
                limit=cast(int, request.arguments[0]),
                available_at=cast(ShanghaiTimestamp, request.arguments[1]),
            )
        if request.operation is _Operation.HEALTH:
            return self._journal.health()
        if request.operation is _Operation.MAINTAIN:
            return self._journal.maintain(cast(MaintenanceRequest, request.arguments[0]))
        raise AssertionError("unsupported writer operation")  # pragma: no cover

    def _fail_pending(self, loop: asyncio.AbstractEventLoop) -> None:
        failure = PersistenceWriterError(
            "PERSISTENCE_WRITER_FAILED",
            "persistence writer stopped after a journal failure",
            retryable=False,
        )
        while True:
            try:
                request = self._requests.get_nowait()
            except queue.Empty:
                return
            if isinstance(request, _Call):
                loop.call_soon_threadsafe(_set_future_exception, request.future, failure)
                with self._state_lock:
                    self._completed += 1


def _set_future_result(future: asyncio.Future[ResultT], result: ResultT) -> None:
    if not future.done():
        future.set_result(result)


def _set_future_exception(future: asyncio.Future[Any], exception: BaseException) -> None:
    if not future.done():
        future.set_exception(exception)
