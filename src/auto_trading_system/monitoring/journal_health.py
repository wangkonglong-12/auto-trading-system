"""Translate LocalJournal health into latched Runtime safety faults."""

from __future__ import annotations

from auto_trading_system.journal import JournalHealth, JournalHealthStatus
from auto_trading_system.runtime import (
    RuntimeFaultComponent,
    RuntimeFaultKind,
    RuntimeFaultReport,
)


class JournalHealthFaultDetector:
    """Emit a new storage safety fault only when its effective kind changes."""

    def __init__(self) -> None:
        self._latched_kind: RuntimeFaultKind | None = None

    def observe(
        self,
        health: JournalHealth,
        *,
        now_monotonic_ns: int,
    ) -> RuntimeFaultReport | None:
        if not isinstance(health, JournalHealth):
            raise ValueError("health must be a JournalHealth")
        if type(now_monotonic_ns) is not int or now_monotonic_ns < 0:
            raise ValueError("now_monotonic_ns must be non-negative")

        kind: RuntimeFaultKind | None = None
        if health.status is JournalHealthStatus.UNAVAILABLE or not health.writable:
            kind = RuntimeFaultKind.JOURNAL_UNWRITABLE
        elif "LOW_DISK_SPACE" in health.reason_codes:
            kind = RuntimeFaultKind.JOURNAL_DISK_PRESSURE

        if kind is None:
            self._latched_kind = None
            return None
        if kind is self._latched_kind:
            return None
        self._latched_kind = kind
        return RuntimeFaultReport(
            kind,
            RuntimeFaultComponent.PERSISTENCE,
            now_monotonic_ns,
            kind.value,
        )
