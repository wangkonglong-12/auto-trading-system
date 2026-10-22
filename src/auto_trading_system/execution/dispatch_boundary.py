"""Thread-safe HALT latch and the sole V2 SDK submission boundary.

Claiming a dispatch and tightening permission share one short lock. SDK calls
run after the lock is released: HALT must never wait for a hung vendor call.
Claimed commands are conservatively reported as in-flight even if the native
call has not yet started. Only broker evidence can later resolve that set.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import TypeVar

from auto_trading_system.domain import Price, Quantity, SecurityCode
from auto_trading_system.gateways.broker.models import (
    CancelOrder,
    OrderSide,
    SubmitOrder,
)
from auto_trading_system.gateways.broker.port import BrokerGateway
from auto_trading_system.journal.critical_models import Submission

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    client_order_id: str
    status: str
    request_seq: int | None = None


@dataclass(frozen=True, slots=True)
class PermissionSnapshot:
    epoch: int
    halted: bool
    reason: str
    in_flight: tuple[str, ...]


class DispatchPermission:
    def __init__(self) -> None:
        self._lock = Lock()
        self._epoch = 1
        self._halted = True
        self._reason = "STARTUP_PAUSED"
        self._claimed: set[str] = set()
        self._attempted: set[str] = set()

    def snapshot(self) -> PermissionSnapshot:
        with self._lock:
            return PermissionSnapshot(
                self._epoch,
                self._halted,
                self._reason,
                tuple(sorted(self._claimed)),
            )

    def halt(self, reason: str) -> PermissionSnapshot:
        with self._lock:
            self._epoch += 1
            self._halted = True
            self._reason = reason
            return PermissionSnapshot(self._epoch, True, reason, tuple(sorted(self._claimed)))

    def allow_after_durable_grant(self, expected_epoch: int) -> int:
        """Application calls only after its boot-bound authorization was committed.

        expected_epoch fences a HALT that arrived while the grant was being saved.
        Recovery never invokes this method from an old database authorization.
        """
        with self._lock:
            if expected_epoch != self._epoch:
                raise ValueError("permission changed while granting authorization")
            self._epoch += 1
            self._halted = False
            self._reason = "AUTHORIZED"
            return self._epoch

    def claim(self, client_order_id: str, epoch: int, deadline: float) -> bool:
        with self._lock:
            if (
                self._halted
                or self._epoch != epoch
                or monotonic() >= deadline
                or client_order_id in self._attempted
            ):
                return False
            self._claimed.add(client_order_id)
            self._attempted.add(client_order_id)
            return True

    def resolve_terminal(self, client_order_id: str, *, confirmed: bool) -> None:
        """Remove a reconciled terminal order from HALT's in-flight report.

        Keep the attempt tombstone for this boot so resolution cannot permit a
        second SDK call using the same client_order_id.
        """
        if not confirmed:
            raise ValueError("broker terminal evidence is required")
        with self._lock:
            self._claimed.discard(client_order_id)


class SDKDispatcher:
    """One outstanding SDK command; executor queue cannot grow without bound."""

    def __init__(self, broker: BrokerGateway, permission: DispatchPermission) -> None:
        self._broker = broker
        self._permission = permission
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="broker-sdk")
        self._lock = Lock()
        self._busy = False
        self._closed = False

    def dispatch(self, submission: Submission, *, deadline: float) -> Future[DispatchOutcome]:
        with self._lock:
            if self._closed or self._busy:
                future: Future[DispatchOutcome] = Future()
                future.set_result(DispatchOutcome(submission.client_order_id, "NOT_DISPATCHED"))
                return future
            self._busy = True
            return self._executor.submit(self._call, submission, deadline)

    def observe(self, query: Callable[[BrokerGateway], T]) -> Future[T]:
        """Serialize recovery reads with submit/cancel on the same SDK worker.

        A timed-out native call keeps this slot occupied until it actually ends;
        a caller timeout never permits overlapping queries or replacement work.
        """
        with self._lock:
            if self._closed or self._busy:
                future: Future[T] = Future()
                future.set_exception(RuntimeError("SDK_BUSY_OR_CLOSED"))
                return future
            self._busy = True
            return self._executor.submit(self._observe, query)

    def _observe(self, query: Callable[[BrokerGateway], T]) -> T:
        try:
            return query(self._broker)
        finally:
            with self._lock:
                self._busy = False

    @property
    def idle(self) -> bool:
        with self._lock:
            return not self._busy

    def _call(self, submission: Submission, deadline: float) -> DispatchOutcome:
        key = submission.client_order_id
        try:
            if not self._permission.claim(key, submission.permission_epoch, deadline):
                return DispatchOutcome(key, "NOT_DISPATCHED")
            if submission.action_type == "CANCEL":
                assert submission.original_order_id is not None
                assert submission.original_client_order_id is not None
                sequence = self._broker.cancel_order(
                    CancelOrder(
                        submission.original_client_order_id,
                        submission.original_order_id,
                    )
                ).request_seq
            else:
                sequence = self._broker.submit_order(
                    SubmitOrder(
                        client_order_id=key,
                        symbol=SecurityCode.parse(submission.symbol),
                        side=OrderSide(submission.side),
                        quantity=Quantity(submission.quantity),
                        limit_price=Price.parse(submission.limit_price),
                        remark_token=submission.remark_token,
                    )
                ).request_seq
            return DispatchOutcome(key, "SDK_ACCEPTED", sequence)
        except Exception:
            # A native exception is not proof of non-submission. Never resubmit here.
            self._permission.halt("SDK_RESULT_UNCERTAIN")
            return DispatchOutcome(key, "UNCERTAIN")
        finally:
            with self._lock:
                self._busy = False

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._permission.halt("SDK_SHUTDOWN")
        self._executor.shutdown(wait=False, cancel_futures=True)
