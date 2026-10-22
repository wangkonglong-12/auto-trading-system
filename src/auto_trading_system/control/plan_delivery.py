"""Bounded in-process handoff from the network agent to the engine owner.

Only the agent waits. The engine drains this mailbox on its own loop; it never
does network I/O here. This is an internal port, not an authenticated IPC server.
Expiration before claim cancels delivery; expiration after claim is uncertain,
so retries must retain the same signed plan and use the engine's idempotence.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, TimeoutError
from dataclasses import dataclass
from queue import Empty, Full, Queue
from threading import Lock, get_ident
from time import monotonic

from auto_trading_system.protocol.v2.api import PlanLookup, PlanReceipt
from auto_trading_system.sync.daily_upload import RetryableSyncError


@dataclass(frozen=True)
class _Delivery:
    lookup: PlanLookup
    artifact_hash: str | None
    deadline: float
    future: Future[PlanReceipt]


class PlanDeliveryMailbox:
    def __init__(self, *, capacity: int = 8, timeout: float = 5) -> None:
        if type(capacity) is not int or not 1 <= capacity <= 32 or not 0 < timeout <= 8:
            raise ValueError("invalid plan delivery budget")
        self._owner = get_ident()
        self._timeout = timeout
        self._queue: Queue[_Delivery] = Queue(maxsize=capacity)
        self._lock = Lock()
        self._closed = False

    def apply(self, lookup: PlanLookup, artifact_hash: str | None) -> PlanReceipt:
        """Agent-thread port; timeout never means the engine rejected a plan."""
        if get_ident() == self._owner:
            raise RuntimeError("plan delivery must not block the engine owner")
        future: Future[PlanReceipt] = Future()
        item = _Delivery(lookup, artifact_hash, monotonic() + self._timeout, future)
        with self._lock:
            if self._closed:
                raise RetryableSyncError("PLAN_DELIVERY_CLOSED")
            try:
                self._queue.put_nowait(item)
            except Full as exc:
                raise RetryableSyncError("PLAN_DELIVERY_BUSY") from exc
        try:
            return future.result(timeout=self._timeout)
        except TimeoutError as exc:
            future.cancel()  # Succeeds only before owner claim; never undo an applied plan.
            raise RetryableSyncError("PLAN_DELIVERY_UNCERTAIN") from exc

    def poll(self, apply: Callable[[PlanLookup, str | None], PlanReceipt]) -> bool:
        """Process at most one delivery; callable executes on the sole engine owner."""
        if get_ident() != self._owner:
            raise RuntimeError("plan mailbox has a single engine owner")
        try:
            item = self._queue.get_nowait()
        except Empty:
            return False
        if not item.future.set_running_or_notify_cancel():
            return True
        if monotonic() >= item.deadline:
            item.future.set_exception(RetryableSyncError("PLAN_DELIVERY_EXPIRED"))
            return True
        try:
            item.future.set_result(apply(item.lookup, item.artifact_hash))
        except Exception as exc:
            item.future.set_exception(exc)
        return True

    def close(self) -> None:
        if get_ident() != self._owner:
            raise RuntimeError("plan mailbox must close on engine owner")
        with self._lock:
            self._closed = True
            while True:
                try:
                    item = self._queue.get_nowait()
                except Empty:
                    break
                if item.future.set_running_or_notify_cancel():
                    item.future.set_exception(RetryableSyncError("PLAN_DELIVERY_CLOSED"))
