"""Monotonic, non-blocking detection of a stalled Persistence Writer."""

from __future__ import annotations

from dataclasses import dataclass

from auto_trading_system.journal.writer import (
    PersistenceQueueSnapshot,
    PersistenceWriterStatus,
)
from auto_trading_system.runtime import (
    RuntimeFaultComponent,
    RuntimeFaultKind,
    RuntimeFaultReport,
)


@dataclass(frozen=True, slots=True)
class PersistenceStallSignal:
    reason_code: str
    observed_monotonic_ns: int
    stalled_ns: int
    outstanding: int
    queue_pending: int

    def __post_init__(self) -> None:
        if self.reason_code not in {"PERSISTENCE_STALLED", "PERSISTENCE_FAILED"}:
            raise ValueError("reason_code is unsupported")
        values = (
            self.observed_monotonic_ns,
            self.stalled_ns,
            self.outstanding,
            self.queue_pending,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("persistence stall values must be non-negative integers")

    def to_fault_report(self) -> RuntimeFaultReport:
        return RuntimeFaultReport(
            RuntimeFaultKind(self.reason_code),
            RuntimeFaultComponent.PERSISTENCE,
            self.observed_monotonic_ns,
            self.reason_code,
        )


class PersistenceStallDetector:
    """Emit once when accepted work stops completing beyond a fixed threshold."""

    def __init__(self, *, stall_timeout_ns: int) -> None:
        if type(stall_timeout_ns) is not int or stall_timeout_ns < 1:
            raise ValueError("stall_timeout_ns must be a positive integer")
        self._stall_timeout_ns = stall_timeout_ns
        self._last_observed_ns: int | None = None
        self._last_accepted = 0
        self._last_completed = 0
        self._last_rejected = 0
        self._outstanding_since_ns: int | None = None
        self._latched_reason: str | None = None

    def observe(
        self,
        snapshot: PersistenceQueueSnapshot,
        *,
        now_monotonic_ns: int,
    ) -> PersistenceStallSignal | None:
        if not isinstance(snapshot, PersistenceQueueSnapshot):
            raise ValueError("snapshot must be a PersistenceQueueSnapshot")
        if type(now_monotonic_ns) is not int or now_monotonic_ns < 0:
            raise ValueError("now_monotonic_ns must be non-negative")
        if not isinstance(snapshot.status, PersistenceWriterStatus):
            raise ValueError("persistence snapshot status is invalid")
        counters = (
            snapshot.capacity,
            snapshot.pending,
            snapshot.accepted,
            snapshot.completed,
            snapshot.rejected,
        )
        if (
            any(type(value) is not int or value < 0 for value in counters)
            or snapshot.accepted < snapshot.completed
            or snapshot.capacity < 1
            or snapshot.pending > snapshot.capacity + 1
        ):
            raise ValueError("persistence snapshot counters are inconsistent")
        if self._last_observed_ns is not None and now_monotonic_ns < self._last_observed_ns:
            raise ValueError("monotonic time moved backwards")
        if (
            snapshot.accepted < self._last_accepted
            or snapshot.completed < self._last_completed
            or snapshot.rejected < self._last_rejected
        ):
            raise ValueError("persistence snapshot counters moved backwards")
        self._last_observed_ns = now_monotonic_ns
        self._last_accepted = snapshot.accepted
        self._last_rejected = snapshot.rejected
        outstanding = snapshot.accepted - snapshot.completed
        if snapshot.status is PersistenceWriterStatus.FAILED:
            return self._emit(
                "PERSISTENCE_FAILED",
                now_monotonic_ns,
                0,
                outstanding,
                snapshot.pending,
            )
        if snapshot.status is not PersistenceWriterStatus.RUNNING or outstanding == 0:
            self._last_completed = snapshot.completed
            self._outstanding_since_ns = None
            self._latched_reason = None
            return None
        if self._outstanding_since_ns is None or snapshot.completed != self._last_completed:
            self._last_completed = snapshot.completed
            self._outstanding_since_ns = now_monotonic_ns
            self._latched_reason = None
            return None
        stalled_ns = now_monotonic_ns - self._outstanding_since_ns
        if stalled_ns < 0:
            raise ValueError("monotonic time moved backwards")
        if stalled_ns < self._stall_timeout_ns:
            return None
        return self._emit(
            "PERSISTENCE_STALLED",
            now_monotonic_ns,
            stalled_ns,
            outstanding,
            snapshot.pending,
        )

    def _emit(
        self,
        reason_code: str,
        observed_monotonic_ns: int,
        stalled_ns: int,
        outstanding: int,
        queue_pending: int,
    ) -> PersistenceStallSignal | None:
        if self._latched_reason == reason_code:
            return None
        self._latched_reason = reason_code
        return PersistenceStallSignal(
            reason_code,
            observed_monotonic_ns,
            stalled_ns,
            outstanding,
            queue_pending,
        )
