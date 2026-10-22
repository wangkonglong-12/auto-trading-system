"""Single-owner decision dispatch with thread-safe bounded ingress."""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import Condition, Lock, get_ident
from typing import Generic, TypeVar

from auto_trading_system.domain.clock import MonotonicClock

EventT = TypeVar("EventT")


class RuntimeEventSource(StrEnum):
    BROKER = "BROKER"
    MARKET = "MARKET"
    SYNC = "SYNC"
    SCHEDULER = "SCHEDULER"
    OPERATOR = "OPERATOR"
    PERSISTENCE = "PERSISTENCE"
    WATCHDOG = "WATCHDOG"
    INTERNAL = "INTERNAL"


class EventPriority(StrEnum):
    CRITICAL = "CRITICAL"
    NORMAL = "NORMAL"


class PublishOutcome(StrEnum):
    ACCEPTED = "ACCEPTED"
    OVERFLOW = "OVERFLOW"
    CLOSED = "CLOSED"


class DispatchKind(StrEnum):
    EVENT = "EVENT"
    QUEUE_OVERFLOW = "QUEUE_OVERFLOW"


class DecisionLoopStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class DecisionLoopError(RuntimeError):
    """Stable fail-closed event-loop error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class DecisionLoopDispatchError(DecisionLoopError):
    """Raised when the sole state-mutation handler fails."""

    def __init__(self, dispatch_sequence: int) -> None:
        super().__init__(
            "DECISION_HANDLER_FAILED",
            f"dispatch sequence {dispatch_sequence} failed",
        )
        self.dispatch_sequence = dispatch_sequence


@dataclass(frozen=True, slots=True)
class RuntimeEventEnvelope(Generic[EventT]):
    ingress_sequence: int
    source: RuntimeEventSource
    priority: EventPriority
    published_monotonic_ns: int
    payload: EventT


@dataclass(frozen=True, slots=True)
class QueueDepth:
    normal: int
    critical: int
    normal_capacity: int
    critical_capacity: int
    closed: bool

    @property
    def total(self) -> int:
        return self.normal + self.critical


@dataclass(frozen=True, slots=True)
class QueueOverflowReport:
    dropped_total: int
    dropped_normal: int
    dropped_critical: int
    sources: tuple[RuntimeEventSource, ...]
    first_monotonic_ns: int
    last_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class RuntimeDispatch(Generic[EventT]):
    dispatch_sequence: int
    kind: DispatchKind
    envelope: RuntimeEventEnvelope[EventT] | None = None
    overflow: QueueOverflowReport | None = None

    def __post_init__(self) -> None:
        if self.dispatch_sequence < 1:
            raise ValueError("dispatch_sequence must be positive")
        if self.kind is DispatchKind.EVENT and (self.envelope is None or self.overflow is not None):
            raise ValueError("event dispatch requires only an envelope")
        if self.kind is DispatchKind.QUEUE_OVERFLOW and (
            self.overflow is None or self.envelope is not None
        ):
            raise ValueError("overflow dispatch requires only an overflow report")


class BoundedEventIngress(Generic[EventT]):
    """Thread-safe dual-lane ingress with a loss-proof overflow latch."""

    def __init__(
        self,
        *,
        normal_capacity: int,
        critical_capacity: int,
        monotonic_clock: MonotonicClock,
    ) -> None:
        if type(normal_capacity) is not int or normal_capacity < 1:
            raise ValueError("normal_capacity must be a positive integer")
        if type(critical_capacity) is not int or critical_capacity < 1:
            raise ValueError("critical_capacity must be a positive integer")
        self._normal_capacity = normal_capacity
        self._critical_capacity = critical_capacity
        self._clock = monotonic_clock
        self._normal: deque[RuntimeEventEnvelope[EventT]] = deque()
        self._critical: deque[RuntimeEventEnvelope[EventT]] = deque()
        self._condition = Condition()
        self._closed = False
        self._next_ingress_sequence = 1
        self._overflow_counts: Counter[tuple[EventPriority, RuntimeEventSource]] = Counter()
        self._overflow_first_ns: int | None = None
        self._overflow_last_ns: int | None = None

    def publish(
        self,
        payload: EventT,
        *,
        source: RuntimeEventSource,
        priority: EventPriority = EventPriority.NORMAL,
    ) -> PublishOutcome:
        if not isinstance(source, RuntimeEventSource):
            raise ValueError("source must be a RuntimeEventSource")
        if not isinstance(priority, EventPriority):
            raise ValueError("priority must be an EventPriority")
        with self._condition:
            if self._closed:
                return PublishOutcome.CLOSED
            published_at = self._clock.monotonic_ns()
            lane = self._critical if priority is EventPriority.CRITICAL else self._normal
            capacity = (
                self._critical_capacity
                if priority is EventPriority.CRITICAL
                else self._normal_capacity
            )
            if len(lane) >= capacity:
                self._overflow_counts[(priority, source)] += 1
                if self._overflow_first_ns is None:
                    self._overflow_first_ns = published_at
                self._overflow_last_ns = published_at
                self._condition.notify()
                return PublishOutcome.OVERFLOW
            envelope = RuntimeEventEnvelope(
                ingress_sequence=self._next_ingress_sequence,
                source=source,
                priority=priority,
                published_monotonic_ns=published_at,
                payload=payload,
            )
            self._next_ingress_sequence += 1
            lane.append(envelope)
            self._condition.notify()
            return PublishOutcome.ACCEPTED

    def take_blocking(self) -> RuntimeEventEnvelope[EventT] | None:
        """Block a worker thread; critical FIFO is always drained before normal FIFO."""
        with self._condition:
            while not self._critical and not self._normal and not self._closed:
                self._condition.wait()
            if self._critical:
                return self._critical.popleft()
            if self._normal:
                return self._normal.popleft()
            return None

    def consume_overflow(self) -> QueueOverflowReport | None:
        """Atomically consume the latched overflow summary once in the decision loop."""
        with self._condition:
            if not self._overflow_counts:
                return None
            dropped_normal = sum(
                count
                for (priority, _), count in self._overflow_counts.items()
                if priority is EventPriority.NORMAL
            )
            dropped_critical = sum(
                count
                for (priority, _), count in self._overflow_counts.items()
                if priority is EventPriority.CRITICAL
            )
            sources = tuple(
                sorted({source for _, source in self._overflow_counts}, key=lambda item: item.value)
            )
            first_ns = self._overflow_first_ns
            last_ns = self._overflow_last_ns
            if first_ns is None or last_ns is None:  # pragma: no cover - invariant guard
                raise DecisionLoopError("OVERFLOW_LATCH_INVALID", "overflow timestamps are missing")
            report = QueueOverflowReport(
                dropped_total=dropped_normal + dropped_critical,
                dropped_normal=dropped_normal,
                dropped_critical=dropped_critical,
                sources=sources,
                first_monotonic_ns=first_ns,
                last_monotonic_ns=last_ns,
            )
            self._overflow_counts.clear()
            self._overflow_first_ns = None
            self._overflow_last_ns = None
            return report

    def depth(self) -> QueueDepth:
        with self._condition:
            return QueueDepth(
                normal=len(self._normal),
                critical=len(self._critical),
                normal_capacity=self._normal_capacity,
                critical_capacity=self._critical_capacity,
                closed=self._closed,
            )

    def close(self, *, discard_pending: bool = False) -> int:
        """Stop producers, optionally discard queued messages, and wake the consumer."""
        with self._condition:
            self._closed = True
            discarded = 0
            if discard_pending:
                discarded = len(self._critical) + len(self._normal)
                self._critical.clear()
                self._normal.clear()
            self._condition.notify_all()
            return discarded


DispatchHandler = Callable[[RuntimeDispatch[EventT]], Awaitable[None]]


class DecisionEventLoop(Generic[EventT]):
    """Dispatch every state-affecting input through one asyncio task."""

    def __init__(
        self,
        *,
        ingress: BoundedEventIngress[EventT],
        handler: DispatchHandler[EventT],
    ) -> None:
        self._ingress = ingress
        self._handler = handler
        self._lifecycle_lock = Lock()
        self._status = DecisionLoopStatus.CREATED
        self._owner_task: asyncio.Task[object] | None = None
        self._owner_thread_id: int | None = None
        self._dispatch_sequence = 0

    @property
    def status(self) -> DecisionLoopStatus:
        with self._lifecycle_lock:
            return self._status

    @property
    def dispatch_count(self) -> int:
        with self._lifecycle_lock:
            return self._dispatch_sequence

    def assert_owner(self) -> None:
        """Reject state mutation outside the current decision handler task."""
        try:
            current_task = asyncio.current_task()
        except RuntimeError as exc:
            raise DecisionLoopError(
                "DECISION_STATE_OWNER_REQUIRED",
                "state mutation requires the decision loop task",
            ) from exc
        if (
            current_task is None
            or current_task is not self._owner_task
            or get_ident() != self._owner_thread_id
            or self.status not in {DecisionLoopStatus.RUNNING, DecisionLoopStatus.STOPPING}
        ):
            raise DecisionLoopError(
                "DECISION_STATE_OWNER_REQUIRED",
                "state mutation requires the decision loop task",
            )

    def request_stop(self, *, discard_pending: bool = False) -> int:
        """Thread-safely stop producers and let the owner finish or discard queued work."""
        with self._lifecycle_lock:
            if self._status in {DecisionLoopStatus.STOPPED, DecisionLoopStatus.FAILED}:
                return 0
            if self._status is DecisionLoopStatus.CREATED:
                self._status = DecisionLoopStatus.STOPPED
                return self._ingress.close(discard_pending=discard_pending)
            self._status = DecisionLoopStatus.STOPPING
        return self._ingress.close(discard_pending=discard_pending)

    async def run(self) -> None:
        """Run once until ingress closes and drains; handler failures stop the loop."""
        with self._lifecycle_lock:
            if self._status is not DecisionLoopStatus.CREATED:
                raise DecisionLoopError(
                    "DECISION_LOOP_NOT_STARTABLE",
                    f"cannot start from {self._status}",
                )
            current_task = asyncio.current_task()
            if current_task is None:  # pragma: no cover - asyncio always provides one here
                raise DecisionLoopError("DECISION_LOOP_TASK_MISSING", "no current asyncio task")
            self._owner_task = current_task
            self._owner_thread_id = get_ident()
            self._status = DecisionLoopStatus.RUNNING

        try:
            while True:
                envelope = await asyncio.to_thread(self._ingress.take_blocking)
                if envelope is None:
                    break
                overflow = self._ingress.consume_overflow()
                if overflow is not None:
                    await self._dispatch(
                        RuntimeDispatch(
                            dispatch_sequence=self._next_dispatch_sequence(),
                            kind=DispatchKind.QUEUE_OVERFLOW,
                            overflow=overflow,
                        )
                    )
                await self._dispatch(
                    RuntimeDispatch(
                        dispatch_sequence=self._next_dispatch_sequence(),
                        kind=DispatchKind.EVENT,
                        envelope=envelope,
                    )
                )
        except asyncio.CancelledError:
            self._ingress.close(discard_pending=True)
            with self._lifecycle_lock:
                self._status = DecisionLoopStatus.STOPPED
            raise
        except Exception:
            self._ingress.close(discard_pending=True)
            with self._lifecycle_lock:
                self._status = DecisionLoopStatus.FAILED
            raise
        else:
            with self._lifecycle_lock:
                self._status = DecisionLoopStatus.STOPPED
        finally:
            with self._lifecycle_lock:
                self._owner_task = None
                self._owner_thread_id = None

    async def _dispatch(self, dispatch: RuntimeDispatch[EventT]) -> None:
        try:
            await self._handler(dispatch)
        except Exception as exc:
            raise DecisionLoopDispatchError(dispatch.dispatch_sequence) from exc

    def _next_dispatch_sequence(self) -> int:
        with self._lifecycle_lock:
            self._dispatch_sequence += 1
            return self._dispatch_sequence
