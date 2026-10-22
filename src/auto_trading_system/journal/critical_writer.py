"""Bounded single-owner mailbox. Caller deadlines never imply I/O cancellation.

The engine polls Futures; it never blocks its decision loop on result(). Late
commits remain recovery evidence. Only the engine can decide whether a fresh
result still belongs to a live command; this thread never calls the broker.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from time import monotonic
from typing import Generic, Literal, Protocol, TypeVar

from auto_trading_system.journal.critical_models import (
    CommitEvidence,
    CriticalStoreError,
    RecoveryRecord,
    Submission,
)

T = TypeVar("T")


class CriticalStorePort(Protocol):
    def open(self) -> None: ...
    def commit_submission(self, submission: Submission) -> CommitEvidence: ...
    def read_submissions(self, after_seq: int, limit: int) -> tuple[RecoveryRecord, ...]: ...
    def append_evidence(
        self,
        table: Literal["broker_facts", "control_records", "recovery_checkpoints"],
        key: str,
        body: bytes,
    ) -> bool: ...
    def read_evidence(
        self,
        table: Literal["broker_facts", "control_records", "recovery_checkpoints"],
        key: str,
    ) -> bytes | None: ...
    def close(self) -> None: ...


class _Work(Protocol):
    def execute(self, store: CriticalStorePort) -> None: ...
    def reject(self) -> None: ...


@dataclass(frozen=True)
class _Request(Generic[T]):
    operation: Callable[[CriticalStorePort], T]
    future: Future[T]
    deadline: float

    def execute(self, store: CriticalStorePort) -> None:
        if not self.future.set_running_or_notify_cancel():
            return
        if monotonic() >= self.deadline:
            self.future.set_exception(CriticalStoreError("JOURNAL_COMMAND_EXPIRED"))
            return
        try:
            self.future.set_result(self.operation(store))
        except Exception as exc:
            self.future.set_exception(exc)
            raise

    def reject(self) -> None:
        if self.future.set_running_or_notify_cancel():
            self.future.set_exception(CriticalStoreError("JOURNAL_UNAVAILABLE"))


class CriticalJournalOwner:
    """Bounded lifetime and queue; all critical failures latch unhealthy."""

    def __init__(self, factory: Callable[[], CriticalStorePort], *, capacity: int = 2048) -> None:
        if capacity < 1:
            raise ValueError("journal capacity must be positive")
        self._factory = factory
        self._queue: Queue[_Work] = Queue(maxsize=capacity)
        self._closing = Event()
        self._failed = Event()
        self._ready: Future[None] = Future()
        self._lock = Lock()
        self._started = False
        self._thread = Thread(target=self._run, name="critical-journal-owner", daemon=True)

    def start(self, timeout: float = 5) -> None:
        with self._lock:
            if self._started:
                raise CriticalStoreError("JOURNAL_ALREADY_STARTED")
            self._started = True
            self._thread.start()
        self._ready.result(timeout=timeout)  # Startup only, before the decision loop.

    @property
    def healthy(self) -> bool:
        return self._ready.done() and not self._failed.is_set() and not self._closing.is_set()

    def commit(self, submission: Submission, *, deadline: float) -> Future[CommitEvidence]:
        return self.request(lambda store: store.commit_submission(submission), deadline=deadline)

    def recovery_page(self, after_seq: int = 0) -> Future[tuple[RecoveryRecord, ...]]:
        return self.request(
            lambda store: store.read_submissions(after_seq, 200), deadline=monotonic() + 5
        )

    def request(self, operation: Callable[[CriticalStorePort], T], *, deadline: float) -> Future[T]:
        future: Future[T] = Future()
        with self._lock:
            if not self.healthy:
                future.set_exception(CriticalStoreError("JOURNAL_UNAVAILABLE"))
                return future
            try:
                self._queue.put_nowait(_Request(operation, future, deadline))
            except Full:
                self._failed.set()
                future.set_exception(CriticalStoreError("JOURNAL_QUEUE_FULL"))
        return future

    def close(self, timeout: float = 1) -> bool:
        """False means an owner is still stuck in I/O; do not start a replacement."""
        with self._lock:
            self._closing.set()
        if self._started:
            self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        store: CriticalStorePort | None = None
        try:
            store = self._factory()
            store.open()
            self._ready.set_result(None)
            while not self._closing.is_set():
                try:
                    request = self._queue.get(timeout=0.02)
                except Empty:
                    continue
                if self._failed.is_set():
                    request.reject()
                    continue
                try:
                    request.execute(store)
                except Exception:
                    self._failed.set()
        except Exception as exc:
            self._failed.set()
            if not self._ready.done():
                self._ready.set_exception(exc)
        finally:
            while True:
                try:
                    self._queue.get_nowait().reject()
                except Empty:
                    break
            if store is not None:
                try:
                    store.close()
                except Exception:
                    self._failed.set()


def open_critical_journal(path: Path, account_id: str) -> CriticalJournalOwner:
    """Application factory; the adapter itself is constructed inside the owner."""
    from auto_trading_system.journal.apsw_store import APSWCriticalStore

    owner = CriticalJournalOwner(lambda: APSWCriticalStore(path, account_id))
    owner.start()
    return owner
