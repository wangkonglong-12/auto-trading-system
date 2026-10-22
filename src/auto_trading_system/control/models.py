"""Strongly typed local operator controls and durable audit results."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from auto_trading_system.domain import ShanghaiTimestamp
from auto_trading_system.journal import CommitOutcome, JournalCommitResult
from auto_trading_system.runtime import RuntimeState, TransitionOutcome

_COMMAND_ID_PATTERN = re.compile(r"cmd_[A-Za-z0-9_-]{8,64}")
_CORRELATION_ID_PATTERN = re.compile(r"corr_[A-Za-z0-9_-]{8,64}")
_CONFIRMATION_ID_PATTERN = re.compile(r"confirm_[A-Za-z0-9_-]{8,64}")
_ACTOR_ID_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{2,95}")
_REASON_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
_CONTENT_HASH_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_EVENT_ID_PATTERN = re.compile(r"evt_[0-9a-f]{32}")
MAX_CONFIRMATION_DELAY = timedelta(minutes=5)
MAX_RECOVERY_CONFIRMATION_AGE = timedelta(minutes=2)


def _require_pattern(value: str, pattern: re.Pattern[str], field: str) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{field} has an invalid format")


class LocalControlAction(StrEnum):
    PAUSE_BUY = "PAUSE_BUY"
    SELL_ONLY = "SELL_ONLY"
    MANUAL_TAKEOVER = "MANUAL_TAKEOVER"
    EMERGENCY_HALT = "EMERGENCY_HALT"
    CLEAR_HALT = "CLEAR_HALT"
    REQUEST_AUTO = "REQUEST_AUTO"

    @property
    def target_state(self) -> RuntimeState:
        return _ACTION_TARGETS[self]

    @property
    def requires_confirmation(self) -> bool:
        return self is not LocalControlAction.PAUSE_BUY

    @property
    def broadens_capability(self) -> bool:
        return self in {LocalControlAction.CLEAR_HALT, LocalControlAction.REQUEST_AUTO}


_ACTION_TARGETS = {
    LocalControlAction.PAUSE_BUY: RuntimeState.PAUSE_BUY,
    LocalControlAction.SELL_ONLY: RuntimeState.SELL_ONLY,
    LocalControlAction.MANUAL_TAKEOVER: RuntimeState.MANUAL,
    LocalControlAction.EMERGENCY_HALT: RuntimeState.HALT,
    LocalControlAction.CLEAR_HALT: RuntimeState.READY_PAUSED,
    LocalControlAction.REQUEST_AUTO: RuntimeState.AUTO,
}


@dataclass(frozen=True, slots=True)
class ControlConfirmation:
    confirmation_id: str
    actor_id: str
    confirmed_at: ShanghaiTimestamp
    impact_hash: str

    def __post_init__(self) -> None:
        _require_pattern(self.confirmation_id, _CONFIRMATION_ID_PATTERN, "confirmation_id")
        _require_pattern(self.actor_id, _ACTOR_ID_PATTERN, "actor_id")
        if not isinstance(self.confirmed_at, ShanghaiTimestamp):
            raise ValueError("confirmed_at must be a ShanghaiTimestamp")
        _require_pattern(self.impact_hash, _CONTENT_HASH_PATTERN, "impact_hash")


@dataclass(frozen=True, slots=True)
class LocalControlRequest:
    command_id: str
    action: LocalControlAction
    actor_id: str
    reason_code: str
    requested_at: ShanghaiTimestamp
    expected_state: RuntimeState
    correlation_id: str
    confirmation: ControlConfirmation | None = None
    cancel_active_orders: bool | None = None

    def __post_init__(self) -> None:
        _require_pattern(self.command_id, _COMMAND_ID_PATTERN, "command_id")
        if not isinstance(self.action, LocalControlAction):
            raise ValueError("action must be a LocalControlAction")
        _require_pattern(self.actor_id, _ACTOR_ID_PATTERN, "actor_id")
        _require_pattern(self.reason_code, _REASON_PATTERN, "reason_code")
        if not isinstance(self.requested_at, ShanghaiTimestamp):
            raise ValueError("requested_at must be a ShanghaiTimestamp")
        if not isinstance(self.expected_state, RuntimeState):
            raise ValueError("expected_state must be a RuntimeState")
        _require_pattern(self.correlation_id, _CORRELATION_ID_PATTERN, "correlation_id")
        if self.confirmation is not None:
            if not isinstance(self.confirmation, ControlConfirmation):
                raise ValueError("confirmation must be ControlConfirmation")
            if self.confirmation.actor_id != self.actor_id:
                raise ValueError("confirmation actor must equal request actor")
            confirmation_delay = self.confirmation.confirmed_at.value - self.requested_at.value
            if not timedelta(0) <= confirmation_delay <= MAX_CONFIRMATION_DELAY:
                raise ValueError("confirmation must follow the request within five minutes")
        if self.action.requires_confirmation and self.confirmation is None:
            raise ValueError("action requires confirmation evidence")
        if self.action is LocalControlAction.EMERGENCY_HALT:
            if type(self.cancel_active_orders) is not bool:
                raise ValueError("EMERGENCY_HALT requires an active-order disposition")
        elif self.cancel_active_orders is not None:
            raise ValueError("only EMERGENCY_HALT can set an active-order disposition")
        if (
            self.action is LocalControlAction.CLEAR_HALT
            and self.expected_state is not RuntimeState.HALT
        ):
            raise ValueError("CLEAR_HALT must expect HALT")
        if (
            self.action is LocalControlAction.REQUEST_AUTO
            and self.expected_state is not RuntimeState.READY_PAUSED
        ):
            raise ValueError("REQUEST_AUTO must expect READY_PAUSED")


@dataclass(frozen=True, slots=True)
class LocalControlResult:
    request: LocalControlRequest
    outcome: TransitionOutcome
    previous_state: RuntimeState
    current_state: RuntimeState
    transition_sequence: int | None
    rejection_code: str | None
    blockers: tuple[str, ...]
    decided_at: ShanghaiTimestamp
    event_id: str
    request_commit: JournalCommitResult
    audit_commit: JournalCommitResult

    def __post_init__(self) -> None:
        if not isinstance(self.request, LocalControlRequest):
            raise ValueError("request must be a LocalControlRequest")
        if not isinstance(self.outcome, TransitionOutcome):
            raise ValueError("outcome must be a TransitionOutcome")
        if not isinstance(self.previous_state, RuntimeState) or not isinstance(
            self.current_state, RuntimeState
        ):
            raise ValueError("control result states are invalid")
        if not isinstance(self.blockers, tuple) or not all(
            isinstance(blocker, str) and _REASON_PATTERN.fullmatch(blocker)
            for blocker in self.blockers
        ):
            raise ValueError("blockers must be immutable reason codes")
        if not isinstance(self.decided_at, ShanghaiTimestamp):
            raise ValueError("decided_at must be a ShanghaiTimestamp")
        if self.outcome is TransitionOutcome.APPLIED:
            if self.transition_sequence is None or self.transition_sequence < 1:
                raise ValueError("applied control requires a transition sequence")
            if self.current_state is not self.request.action.target_state:
                raise ValueError("applied control must reach its requested state")
            if self.rejection_code is not None:
                raise ValueError("applied control cannot contain a rejection code")
        elif self.transition_sequence is not None:
            raise ValueError("non-applied control cannot contain a transition sequence")
        if self.outcome is TransitionOutcome.REJECTED:
            if self.rejection_code is None:
                raise ValueError("rejected control requires a rejection code")
            _require_pattern(self.rejection_code, _REASON_PATTERN, "rejection_code")
        elif self.rejection_code is not None:
            raise ValueError("only rejected controls can contain a rejection code")
        if self.outcome is not TransitionOutcome.APPLIED and (
            self.current_state is not self.previous_state
        ):
            raise ValueError("non-applied control cannot change state")
        _require_pattern(self.event_id, _EVENT_ID_PATTERN, "event_id")
        if not isinstance(self.request_commit, JournalCommitResult) or not isinstance(
            self.audit_commit, JournalCommitResult
        ):
            raise ValueError("control result requires journal commit results")
        if (
            self.request_commit.outcome is not CommitOutcome.COMMITTED
            or self.request_commit.persisted_events
            or self.audit_commit.outcome is not CommitOutcome.COMMITTED
            or len(self.audit_commit.persisted_events) != 1
            or self.audit_commit.persisted_events[0].event_id != self.event_id
        ):
            raise ValueError("control commits must contain only the terminal control event")


@dataclass(frozen=True, slots=True)
class PersistedLocalControlReceipt:
    """Storage-neutral projection of one durable request and its optional terminal result."""

    request: LocalControlRequest
    outcome: TransitionOutcome | None
    decided_at: ShanghaiTimestamp | None
    previous_state: RuntimeState | None
    current_state: RuntimeState | None
    transition_sequence: int | None
    rejection_code: str | None
    blockers: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.request, LocalControlRequest):
            raise ValueError("request must be a LocalControlRequest")
        if not isinstance(self.blockers, tuple) or not all(
            isinstance(blocker, str) and _REASON_PATTERN.fullmatch(blocker)
            for blocker in self.blockers
        ):
            raise ValueError("blockers must be immutable reason codes")
        terminal_values = (
            self.decided_at,
            self.previous_state,
            self.current_state,
            self.transition_sequence,
            self.rejection_code,
        )
        if self.outcome is None:
            if any(value is not None for value in terminal_values) or self.blockers:
                raise ValueError("unresolved receipt cannot contain terminal fields")
            return
        if not isinstance(self.outcome, TransitionOutcome):
            raise ValueError("outcome must be a TransitionOutcome")
        if (
            not isinstance(self.decided_at, ShanghaiTimestamp)
            or not isinstance(self.previous_state, RuntimeState)
            or not isinstance(self.current_state, RuntimeState)
        ):
            raise ValueError("resolved receipt requires timestamps and runtime states")
        if self.outcome is TransitionOutcome.APPLIED:
            if (
                self.transition_sequence is None
                or self.transition_sequence < 1
                or self.rejection_code is not None
                or self.current_state is not self.request.action.target_state
            ):
                raise ValueError("applied receipt is inconsistent")
        elif self.outcome is TransitionOutcome.NOOP:
            if (
                self.transition_sequence is not None
                or self.rejection_code is not None
                or self.previous_state is not self.current_state
                or self.current_state is not self.request.action.target_state
            ):
                raise ValueError("noop receipt is inconsistent")
        elif (
            self.transition_sequence is not None
            or self.rejection_code is None
            or _REASON_PATTERN.fullmatch(self.rejection_code) is None
            or self.previous_state is not self.current_state
        ):
            raise ValueError("rejected receipt is inconsistent")

    @property
    def resolved(self) -> bool:
        return self.outcome is not None


class LocalControlError(RuntimeError):
    """A local control could not reach a fully audited terminal outcome."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        fail_closed_state: RuntimeState,
        retryable: bool,
        requires_reconciliation: bool,
    ) -> None:
        _require_pattern(code, _REASON_PATTERN, "code")
        if not isinstance(fail_closed_state, RuntimeState):
            raise ValueError("fail_closed_state must be a RuntimeState")
        if type(retryable) is not bool or type(requires_reconciliation) is not bool:
            raise ValueError("error flags must be booleans")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.fail_closed_state = fail_closed_state
        self.retryable = retryable
        self.requires_reconciliation = requires_reconciliation


@dataclass(frozen=True, slots=True)
class LocalControlRecoveryState:
    active_state: RuntimeState | None
    last_command_id: str | None
    unresolved_command_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.active_state not in {
            None,
            RuntimeState.PAUSE_BUY,
            RuntimeState.SELL_ONLY,
            RuntimeState.MANUAL,
            RuntimeState.HALT,
        }:
            raise ValueError("active_state must be a restrictive runtime state")
        if self.last_command_id is not None:
            _require_pattern(self.last_command_id, _COMMAND_ID_PATTERN, "last_command_id")
        if not isinstance(self.unresolved_command_ids, tuple):
            raise ValueError("unresolved_command_ids must be an immutable tuple")
        for command_id in self.unresolved_command_ids:
            _require_pattern(command_id, _COMMAND_ID_PATTERN, "unresolved_command_id")
        if self.unresolved_command_ids and self.active_state is not RuntimeState.HALT:
            raise ValueError("unresolved local controls must recover to HALT")


class LocalControlRecoveryError(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(f"LOCAL_CONTROL_RECOVERY_INVALID: {message}")
        self.code = "LOCAL_CONTROL_RECOVERY_INVALID"
