"""Decision-loop fault policy that only narrows Runtime execution capability."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from auto_trading_system.runtime.decision_loop import QueueOverflowReport
from auto_trading_system.runtime.state_machine import (
    RuntimeState,
    RuntimeStateMachine,
    TransitionAuthority,
    TransitionDecision,
)

_REASON_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{2,63}")


class RuntimeFaultKind(StrEnum):
    BROKER_QUERY_UNAVAILABLE = "BROKER_QUERY_UNAVAILABLE"
    CLOCK_SKEW_UNSAFE = "CLOCK_SKEW_UNSAFE"
    JOURNAL_DISK_PRESSURE = "JOURNAL_DISK_PRESSURE"
    JOURNAL_UNWRITABLE = "JOURNAL_UNWRITABLE"
    PERSISTENCE_QUEUE_FULL = "PERSISTENCE_QUEUE_FULL"
    PERSISTENCE_STALLED = "PERSISTENCE_STALLED"
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"
    RUNTIME_COMPONENT_CRASH = "RUNTIME_COMPONENT_CRASH"
    CONTROL_AUDIT_UNRESOLVED = "CONTROL_AUDIT_UNRESOLVED"


class RuntimeFaultComponent(StrEnum):
    PERSISTENCE = "PERSISTENCE"
    DECISION_LOOP = "DECISION_LOOP"
    BROKER = "BROKER"
    MARKET = "MARKET"
    SYNC = "SYNC"
    CONTROL = "CONTROL"
    SCHEDULER = "SCHEDULER"


@dataclass(frozen=True, slots=True)
class RuntimeFaultReport:
    kind: RuntimeFaultKind
    component: RuntimeFaultComponent
    observed_monotonic_ns: int
    detail_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RuntimeFaultKind):
            raise ValueError("kind must be a RuntimeFaultKind")
        if not isinstance(self.component, RuntimeFaultComponent):
            raise ValueError("component must be a RuntimeFaultComponent")
        if type(self.observed_monotonic_ns) is not int or self.observed_monotonic_ns < 0:
            raise ValueError("observed_monotonic_ns must be non-negative")
        if (
            not isinstance(self.detail_code, str)
            or _REASON_PATTERN.fullmatch(self.detail_code) is None
        ):
            raise ValueError("detail_code has an invalid format")


class RuntimeFaultPolicy:
    """Convert trusted fault reports into fail-closed state transitions."""

    def __init__(
        self,
        state_machine: RuntimeStateMachine,
        *,
        owner_guard: Callable[[], None],
    ) -> None:
        if not isinstance(state_machine, RuntimeStateMachine):
            raise ValueError("state_machine must be a RuntimeStateMachine")
        if not callable(owner_guard):
            raise ValueError("owner_guard must be callable")
        self._state_machine = state_machine
        self._owner_guard = owner_guard

    def handle(self, report: RuntimeFaultReport) -> TransitionDecision:
        self._owner_guard()
        if not isinstance(report, RuntimeFaultReport):
            raise ValueError("report must be a RuntimeFaultReport")
        authority = TransitionAuthority.HARD_SAFETY
        if report.kind in {
            RuntimeFaultKind.PERSISTENCE_STALLED,
            RuntimeFaultKind.PERSISTENCE_FAILED,
            RuntimeFaultKind.RUNTIME_COMPONENT_CRASH,
        }:
            authority = TransitionAuthority.WATCHDOG
        elif report.kind is RuntimeFaultKind.CONTROL_AUDIT_UNRESOLVED:
            authority = TransitionAuthority.LOCAL_OPERATOR
        return self._state_machine.request_transition(
            RuntimeState.HALT,
            authority=authority,
            reason_code=report.kind.value,
        )

    def handle_queue_overflow(self, report: QueueOverflowReport) -> TransitionDecision:
        self._owner_guard()
        return self._state_machine.degrade_for_queue_overflow(report)
