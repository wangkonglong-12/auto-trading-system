"""Fail-closed Runtime lifecycle and operational-mode state machine."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, fields
from enum import StrEnum

from auto_trading_system.domain.clock import MonotonicClock
from auto_trading_system.runtime.decision_loop import QueueOverflowReport


class RuntimeState(StrEnum):
    BOOTING = "BOOTING"
    CONNECTING = "CONNECTING"
    RECONCILING = "RECONCILING"
    READY_PAUSED = "READY_PAUSED"
    AUTO = "AUTO"
    PAUSE_BUY = "PAUSE_BUY"
    SELL_ONLY = "SELL_ONLY"
    MANUAL = "MANUAL"
    HALT = "HALT"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


class RuntimeCapability(StrEnum):
    """Maximum order capability granted by the deployment profile."""

    OBSERVE_ONLY = "OBSERVE_ONLY"
    EXIT_ONLY = "EXIT_ONLY"
    FULL = "FULL"


class TransitionAuthority(StrEnum):
    STRATEGY = "STRATEGY"
    RUNTIME = "RUNTIME"
    CENTRAL = "CENTRAL"
    BROKER_FACT = "BROKER_FACT"
    HARD_SAFETY = "HARD_SAFETY"
    WATCHDOG = "WATCHDOG"
    LOCAL_OPERATOR = "LOCAL_OPERATOR"
    LIFECYCLE = "LIFECYCLE"


class TransitionOutcome(StrEnum):
    APPLIED = "APPLIED"
    NOOP = "NOOP"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class RuntimeHealth:
    """Facts required for recovery and AUTO activation."""

    qmt_connected: bool
    broker_account_ok: bool
    market_healthy: bool
    journal_healthy: bool
    reconciliation_complete: bool
    critical_discrepancy: bool
    unknown_order: bool
    clock_within_limit: bool
    disk_writable: bool
    event_queue_intact: bool

    def __post_init__(self) -> None:
        for field in fields(self):
            if type(getattr(self, field.name)) is not bool:
                raise ValueError(f"{field.name} must be a boolean")

    def recovery_blockers(self) -> tuple[str, ...]:
        blockers: list[str] = []
        checks = (
            (self.qmt_connected, "QMT_DISCONNECTED"),
            (self.broker_account_ok, "BROKER_ACCOUNT_NOT_OK"),
            (self.journal_healthy, "JOURNAL_UNAVAILABLE"),
            (self.reconciliation_complete, "RECONCILIATION_REQUIRED"),
            (not self.critical_discrepancy, "CRITICAL_DISCREPANCY_OPEN"),
            (not self.unknown_order, "UNKNOWN_ORDER_OPEN"),
            (self.clock_within_limit, "CLOCK_SKEW_UNSAFE"),
            (self.disk_writable, "DISK_UNWRITABLE"),
            (self.event_queue_intact, "EVENT_QUEUE_INCOMPLETE"),
        )
        for condition, code in checks:
            if not condition:
                blockers.append(code)
        return tuple(blockers)

    def auto_blockers(self) -> tuple[str, ...]:
        blockers = list(self.recovery_blockers())
        if not self.market_healthy:
            blockers.append("MARKET_DATA_UNHEALTHY")
        return tuple(blockers)


@dataclass(frozen=True, slots=True)
class RuntimeRecoveryPermit:
    """Explicit local and central authorization captured before recovery."""

    reconciliation_id: str
    local_authorized: bool
    central_authorized: bool

    def __post_init__(self) -> None:
        if re.fullmatch(r"recon_[A-Za-z0-9_-]{8,64}", self.reconciliation_id) is None:
            raise ValueError("reconciliation_id is invalid")
        if type(self.local_authorized) is not bool:
            raise ValueError("local_authorized must be a boolean")
        if type(self.central_authorized) is not bool:
            raise ValueError("central_authorized must be a boolean")


@dataclass(frozen=True, slots=True)
class TransitionDecision:
    outcome: TransitionOutcome
    previous_state: RuntimeState
    requested_state: RuntimeState
    current_state: RuntimeState
    authority: TransitionAuthority
    reason_code: str
    decided_monotonic_ns: int
    transition_sequence: int | None = None
    rejection_code: str | None = None
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.decided_monotonic_ns < 0:
            raise ValueError("decided_monotonic_ns must be non-negative")
        if self.outcome is TransitionOutcome.APPLIED:
            if self.transition_sequence is None or self.transition_sequence < 1:
                raise ValueError("applied decision requires a positive transition_sequence")
            if (
                self.previous_state is self.requested_state
                or self.current_state is not self.requested_state
                or self.rejection_code is not None
            ):
                raise ValueError("applied decision has inconsistent fields")
        elif self.transition_sequence is not None:
            raise ValueError("non-applied decision cannot have a transition_sequence")
        if (
            self.outcome is not TransitionOutcome.APPLIED
            and self.current_state is not self.previous_state
        ):
            raise ValueError("non-applied decision cannot change current_state")
        if (
            self.outcome is TransitionOutcome.NOOP
            and self.requested_state is not self.previous_state
        ):
            raise ValueError("noop decision must request the current state")
        if self.outcome is TransitionOutcome.REJECTED and self.rejection_code is None:
            raise ValueError("rejected decision requires a rejection_code")
        if self.outcome is not TransitionOutcome.REJECTED and self.rejection_code is not None:
            raise ValueError("only rejected decisions can have a rejection_code")


_AUTHORITY_PRIORITY = {
    TransitionAuthority.STRATEGY: 10,
    TransitionAuthority.RUNTIME: 20,
    TransitionAuthority.LIFECYCLE: 20,
    TransitionAuthority.CENTRAL: 30,
    TransitionAuthority.BROKER_FACT: 40,
    TransitionAuthority.WATCHDOG: 45,
    TransitionAuthority.HARD_SAFETY: 50,
    TransitionAuthority.LOCAL_OPERATOR: 60,
}

_SAFETY_RANK = {
    RuntimeState.AUTO: 0,
    RuntimeState.PAUSE_BUY: 1,
    RuntimeState.SELL_ONLY: 2,
    RuntimeState.MANUAL: 3,
    RuntimeState.READY_PAUSED: 4,
    RuntimeState.HALT: 5,
}

_OPERATIONAL_STATES = frozenset(_SAFETY_RANK)

_ALLOWED_TRANSITIONS = {
    RuntimeState.BOOTING: frozenset(
        {RuntimeState.CONNECTING, RuntimeState.HALT, RuntimeState.STOPPING}
    ),
    RuntimeState.CONNECTING: frozenset(
        {RuntimeState.RECONCILING, RuntimeState.HALT, RuntimeState.STOPPING}
    ),
    RuntimeState.RECONCILING: frozenset(
        {RuntimeState.READY_PAUSED, RuntimeState.HALT, RuntimeState.STOPPING}
    ),
    RuntimeState.READY_PAUSED: frozenset(
        {
            RuntimeState.AUTO,
            RuntimeState.PAUSE_BUY,
            RuntimeState.SELL_ONLY,
            RuntimeState.MANUAL,
            RuntimeState.HALT,
            RuntimeState.CONNECTING,
            RuntimeState.STOPPING,
        }
    ),
    RuntimeState.AUTO: frozenset(
        {
            RuntimeState.READY_PAUSED,
            RuntimeState.PAUSE_BUY,
            RuntimeState.SELL_ONLY,
            RuntimeState.MANUAL,
            RuntimeState.HALT,
            RuntimeState.CONNECTING,
            RuntimeState.RECONCILING,
            RuntimeState.STOPPING,
        }
    ),
    RuntimeState.PAUSE_BUY: frozenset(
        {
            RuntimeState.READY_PAUSED,
            RuntimeState.AUTO,
            RuntimeState.SELL_ONLY,
            RuntimeState.MANUAL,
            RuntimeState.HALT,
            RuntimeState.CONNECTING,
            RuntimeState.RECONCILING,
            RuntimeState.STOPPING,
        }
    ),
    RuntimeState.SELL_ONLY: frozenset(
        {
            RuntimeState.READY_PAUSED,
            RuntimeState.AUTO,
            RuntimeState.PAUSE_BUY,
            RuntimeState.MANUAL,
            RuntimeState.HALT,
            RuntimeState.CONNECTING,
            RuntimeState.RECONCILING,
            RuntimeState.STOPPING,
        }
    ),
    RuntimeState.MANUAL: frozenset(
        {
            RuntimeState.READY_PAUSED,
            RuntimeState.AUTO,
            RuntimeState.PAUSE_BUY,
            RuntimeState.SELL_ONLY,
            RuntimeState.HALT,
            RuntimeState.CONNECTING,
            RuntimeState.RECONCILING,
            RuntimeState.STOPPING,
        }
    ),
    RuntimeState.HALT: frozenset({RuntimeState.READY_PAUSED, RuntimeState.STOPPING}),
    RuntimeState.STOPPING: frozenset({RuntimeState.STOPPED}),
    RuntimeState.STOPPED: frozenset(),
}

_AUTO_AUTHORITIES = frozenset({TransitionAuthority.RUNTIME, TransitionAuthority.LOCAL_OPERATOR})
_LIFECYCLE_AUTHORITIES = frozenset({TransitionAuthority.LIFECYCLE, TransitionAuthority.BROKER_FACT})
_SHUTDOWN_AUTHORITIES = frozenset(
    {
        TransitionAuthority.LIFECYCLE,
        TransitionAuthority.LOCAL_OPERATOR,
        TransitionAuthority.WATCHDOG,
    }
)


class RuntimeStateMachine:
    """Own Runtime state; every mutation must execute inside the decision-loop owner."""

    def __init__(
        self,
        *,
        capability: RuntimeCapability,
        monotonic_clock: MonotonicClock,
        owner_guard: Callable[[], None],
    ) -> None:
        if not isinstance(capability, RuntimeCapability):
            raise ValueError("capability must be a RuntimeCapability")
        self._capability = capability
        self._clock = monotonic_clock
        self._owner_guard = owner_guard
        self._state = RuntimeState.BOOTING
        self._transition_sequence = 0
        self._restriction_authority: TransitionAuthority | None = None

    @property
    def state(self) -> RuntimeState:
        return self._state

    @property
    def transition_sequence(self) -> int:
        return self._transition_sequence

    @property
    def restriction_authority(self) -> TransitionAuthority | None:
        return self._restriction_authority

    def request_transition(
        self,
        target: RuntimeState,
        *,
        authority: TransitionAuthority,
        reason_code: str,
        health: RuntimeHealth | None = None,
        permit: RuntimeRecoveryPermit | None = None,
    ) -> TransitionDecision:
        self._owner_guard()
        self._validate_request(target, authority, reason_code)
        previous = self._state

        if target is previous:
            self._promote_restriction_authority(target, authority)
            return self._decision(
                TransitionOutcome.NOOP,
                previous,
                target,
                authority,
                reason_code,
            )
        if target not in _ALLOWED_TRANSITIONS[previous]:
            return self._reject(
                previous,
                target,
                authority,
                reason_code,
                "INVALID_RUNTIME_TRANSITION",
            )

        authority_rejection = self._validate_authority(previous, target, authority)
        if authority_rejection is not None:
            return self._reject(
                previous,
                target,
                authority,
                reason_code,
                authority_rejection,
            )

        if previous is RuntimeState.RECONCILING and target is RuntimeState.READY_PAUSED:
            if health is None:
                return self._reject(
                    previous,
                    target,
                    authority,
                    reason_code,
                    "RECOVERY_HEALTH_REQUIRED",
                )
            blockers = health.recovery_blockers()
            if blockers:
                return self._reject(
                    previous,
                    target,
                    authority,
                    reason_code,
                    "RECOVERY_PRECONDITION_FAILED",
                    blockers,
                )

        if target is RuntimeState.AUTO:
            rejection_code, blockers = self._validate_auto(authority, health, permit)
            if rejection_code is not None:
                return self._reject(
                    previous,
                    target,
                    authority,
                    reason_code,
                    rejection_code,
                    blockers,
                )
        elif (
            target is RuntimeState.SELL_ONLY and self._capability is RuntimeCapability.OBSERVE_ONLY
        ):
            return self._reject(
                previous,
                target,
                authority,
                reason_code,
                "EXECUTION_CAPABILITY_FORBIDS_SELL",
            )

        if self._is_restriction_clearance(previous, target):
            rejection_code, blockers = self._validate_restriction_clearance(
                authority, health, permit
            )
            if rejection_code is not None:
                return self._reject(
                    previous,
                    target,
                    authority,
                    reason_code,
                    rejection_code,
                    blockers,
                )

        return self._apply(previous, target, authority, reason_code)

    def degrade_for_queue_overflow(self, report: QueueOverflowReport) -> TransitionDecision:
        if (
            report.dropped_total < 1
            or report.dropped_normal < 0
            or report.dropped_critical < 0
            or report.first_monotonic_ns < 0
            or report.last_monotonic_ns < report.first_monotonic_ns
            or (report.dropped_normal + report.dropped_critical != report.dropped_total)
        ):
            raise ValueError("overflow report must contain dropped events")
        if report.dropped_critical > 0 or self._state not in _OPERATIONAL_STATES:
            target = RuntimeState.HALT
            reason_code = "QUEUE_CRITICAL_OVERFLOW"
        elif _SAFETY_RANK[self._state] >= _SAFETY_RANK[RuntimeState.PAUSE_BUY]:
            target = self._state
            reason_code = "QUEUE_NORMAL_OVERFLOW_ALREADY_SAFE"
        else:
            target = RuntimeState.PAUSE_BUY
            reason_code = "QUEUE_NORMAL_OVERFLOW"
        return self.request_transition(
            target,
            authority=TransitionAuthority.HARD_SAFETY,
            reason_code=reason_code,
        )

    def _promote_restriction_authority(
        self, target: RuntimeState, authority: TransitionAuthority
    ) -> None:
        if target not in {
            RuntimeState.PAUSE_BUY,
            RuntimeState.SELL_ONLY,
            RuntimeState.MANUAL,
            RuntimeState.HALT,
        }:
            return
        current = self._restriction_authority
        if current is None or _AUTHORITY_PRIORITY[authority] > _AUTHORITY_PRIORITY[current]:
            self._restriction_authority = authority

    def _validate_request(
        self,
        target: RuntimeState,
        authority: TransitionAuthority,
        reason_code: str,
    ) -> None:
        if not isinstance(target, RuntimeState):
            raise ValueError("target must be a RuntimeState")
        if not isinstance(authority, TransitionAuthority):
            raise ValueError("authority must be a TransitionAuthority")
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", reason_code) is None:
            raise ValueError("reason_code is invalid")

    def _validate_authority(
        self,
        previous: RuntimeState,
        target: RuntimeState,
        authority: TransitionAuthority,
    ) -> str | None:
        if (
            previous is RuntimeState.BOOTING
            and target is RuntimeState.CONNECTING
            and authority is not TransitionAuthority.LIFECYCLE
        ):
            return "LIFECYCLE_AUTHORITY_REQUIRED"
        if (
            target in {RuntimeState.CONNECTING, RuntimeState.RECONCILING}
            and authority not in _LIFECYCLE_AUTHORITIES
        ):
            return "LIFECYCLE_AUTHORITY_REQUIRED"
        if (
            previous is RuntimeState.RECONCILING
            and target is RuntimeState.READY_PAUSED
            and authority is not TransitionAuthority.LIFECYCLE
        ):
            return "LIFECYCLE_AUTHORITY_REQUIRED"
        if (
            target in {RuntimeState.STOPPING, RuntimeState.STOPPED}
            and authority not in _SHUTDOWN_AUTHORITIES
        ):
            return "SHUTDOWN_AUTHORITY_REQUIRED"
        return None

    def _validate_auto(
        self,
        authority: TransitionAuthority,
        health: RuntimeHealth | None,
        permit: RuntimeRecoveryPermit | None,
    ) -> tuple[str | None, tuple[str, ...]]:
        if self._capability is not RuntimeCapability.FULL:
            return "EXECUTION_CAPABILITY_FORBIDS_AUTO", ()
        if authority not in _AUTO_AUTHORITIES:
            return "REMOTE_AUTO_RESUME_REQUIRES_LOCAL_DECISION", ()
        if health is None:
            return "AUTO_HEALTH_REQUIRED", ()
        blockers = health.auto_blockers()
        if blockers:
            return "AUTO_HEALTH_PRECONDITION_FAILED", blockers
        if permit is None:
            return "AUTO_PERMIT_REQUIRED", ()
        if not permit.local_authorized:
            return "LOCAL_AUTO_AUTHORIZATION_REQUIRED", ()
        if not permit.central_authorized:
            return "CENTRAL_AUTO_AUTHORIZATION_REQUIRED", ()
        return None, ()

    def _is_restriction_clearance(self, previous: RuntimeState, target: RuntimeState) -> bool:
        if self._restriction_authority is None:
            return False
        if previous not in _OPERATIONAL_STATES or target not in _OPERATIONAL_STATES:
            return False
        return _SAFETY_RANK[target] < _SAFETY_RANK[previous]

    def _validate_restriction_clearance(
        self,
        authority: TransitionAuthority,
        health: RuntimeHealth | None,
        permit: RuntimeRecoveryPermit | None,
    ) -> tuple[str | None, tuple[str, ...]]:
        controller = self._restriction_authority
        if controller is None:  # pragma: no cover - caller establishes the invariant
            return None, ()
        if _AUTHORITY_PRIORITY[authority] < _AUTHORITY_PRIORITY[controller]:
            return "HIGHER_PRIORITY_RESTRICTION_ACTIVE", ()
        if authority is not TransitionAuthority.LOCAL_OPERATOR:
            return "LOCAL_RECOVERY_DECISION_REQUIRED", ()
        if health is None:
            return "RECOVERY_HEALTH_REQUIRED", ()
        blockers = health.recovery_blockers()
        if blockers:
            return "RECOVERY_PRECONDITION_FAILED", blockers
        if permit is None or not permit.local_authorized:
            return "LOCAL_RECOVERY_AUTHORIZATION_REQUIRED", ()
        return None, ()

    def _apply(
        self,
        previous: RuntimeState,
        target: RuntimeState,
        authority: TransitionAuthority,
        reason_code: str,
    ) -> TransitionDecision:
        self._state = target
        self._transition_sequence += 1
        if target is RuntimeState.AUTO:
            self._restriction_authority = None
        elif target in {
            RuntimeState.PAUSE_BUY,
            RuntimeState.SELL_ONLY,
            RuntimeState.MANUAL,
            RuntimeState.HALT,
        }:
            self._promote_restriction_authority(target, authority)
        elif previous is RuntimeState.HALT and target is RuntimeState.READY_PAUSED:
            self._restriction_authority = None
        return self._decision(
            TransitionOutcome.APPLIED,
            previous,
            target,
            authority,
            reason_code,
            transition_sequence=self._transition_sequence,
        )

    def _reject(
        self,
        previous: RuntimeState,
        target: RuntimeState,
        authority: TransitionAuthority,
        reason_code: str,
        rejection_code: str,
        blockers: tuple[str, ...] = (),
    ) -> TransitionDecision:
        return self._decision(
            TransitionOutcome.REJECTED,
            previous,
            target,
            authority,
            reason_code,
            rejection_code=rejection_code,
            blockers=blockers,
        )

    def _decision(
        self,
        outcome: TransitionOutcome,
        previous: RuntimeState,
        target: RuntimeState,
        authority: TransitionAuthority,
        reason_code: str,
        *,
        transition_sequence: int | None = None,
        rejection_code: str | None = None,
        blockers: tuple[str, ...] = (),
    ) -> TransitionDecision:
        return TransitionDecision(
            outcome=outcome,
            previous_state=previous,
            requested_state=target,
            current_state=self._state,
            authority=authority,
            reason_code=reason_code,
            decided_monotonic_ns=self._clock.monotonic_ns(),
            transition_sequence=transition_sequence,
            rejection_code=rejection_code,
            blockers=blockers,
        )
