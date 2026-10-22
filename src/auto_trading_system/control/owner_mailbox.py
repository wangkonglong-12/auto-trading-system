"""Bounded owner-thread calls; timeout cancels only work not yet claimed."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, TimeoutError
from dataclasses import dataclass
from queue import Empty, Full, Queue
from threading import Lock, get_ident
from time import monotonic
from typing import Generic, TypeVar

from auto_trading_system.control.ipc_protocol import IPCError

T = TypeVar("T")


@dataclass
class _Call(Generic[T]):
    operation: Callable[[], T]
    future: Future[T]
    deadline: float


class OwnerMailbox(Generic[T]):
    def __init__(self) -> None:
        self._owner = get_ident()
        self._queue: Queue[_Call[T]] = Queue(maxsize=8)
        self._lock = Lock()
        self._closed = False

    def call(self, operation: Callable[[], T], *, timeout: float = 4) -> T:
        if get_ident() == self._owner or not 0 < timeout <= 4:
            raise IPCError("OWNER_CALL_INVALID")
        future: Future[T] = Future()
        with self._lock:
            if self._closed:
                raise IPCError("OWNER_CLOSED")
            try:
                self._queue.put_nowait(_Call(operation, future, monotonic() + timeout))
            except Full as exc:
                raise IPCError("OWNER_BUSY") from exc
        try:
            return future.result(timeout=timeout)
        except TimeoutError as exc:
            future.cancel()
            raise IPCError("OWNER_RESULT_UNCERTAIN") from exc

    def poll(self) -> None:
        if get_ident() != self._owner:
            raise IPCError("WRONG_OWNER")
        try:
            call = self._queue.get_nowait()
        except Empty:
            return
        if not call.future.set_running_or_notify_cancel():
            return
        if monotonic() >= call.deadline:
            call.future.set_exception(IPCError("OWNER_EXPIRED"))
            return
        try:
            call.future.set_result(call.operation())
        except Exception as exc:
            call.future.set_exception(exc)

    def close(self) -> None:
        if get_ident() != self._owner:
            raise IPCError("WRONG_OWNER")
        with self._lock:
            self._closed = True
            while True:
                try:
                    call = self._queue.get_nowait()
                except Empty:
                    return
                if call.future.set_running_or_notify_cancel():
                    call.future.set_exception(IPCError("OWNER_CLOSED"))
