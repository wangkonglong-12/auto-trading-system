"""Decision-loop-owned local controls with persist-before-widen and fail-closed audit."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol, runtime_checkable

from auto_trading_system.control.models import (
    MAX_RECOVERY_CONFIRMATION_AGE,
    LocalControlAction,
    LocalControlError,
    LocalControlRequest,
    LocalControlResult,
)
from auto_trading_system.domain import Clock, ShanghaiTimestamp
from auto_trading_system.journal import (
    AggregateType,
    AppendDomainEvent,
    CanonicalJsonDocument,
    CommitOutcome,
    EventDraft,
    EventSeverity,
    EventSource,
    EventType,
    JournalCommitResult,
    JournalTransaction,
    LocalControlRecord,
    PutLocalControl,
)
from auto_trading_system.protocol import new_event_id
from auto_trading_system.protocol.canonical_json import canonical_json_bytes
from auto_trading_system.runtime import (
    RuntimeHealth,
    RuntimeRecoveryPermit,
    RuntimeState,
    RuntimeStateMachine,
    TransitionAuthority,
    TransitionOutcome,
)

_NODE_ID_PATTERN = re.compile(r"node-[a-z0-9][a-z0-9-]{2,47}")
_ACCOUNT_ID_PATTERN = re.compile(r"acct_[A-Za-z0-9_-]{3,48}")
_BOOT_ID_PATTERN = re.compile(r"boot_[0-9a-f]{32}")


@runtime_checkable
class LocalControlCommitter(Protocol):
    async def commit(self, transaction: JournalTransaction) -> JournalCommitResult: ...


@dataclass(frozen=True, slots=True)
class _DecisionSnapshot:
    outcome: TransitionOutcome
    previous_state: RuntimeState
    current_state: RuntimeState
    transition_sequence: int | None = None
    rejection_code: str | None = None
    blockers: tuple[str, ...] = ()


class LocalControlCoordinator:
    """Apply one local command from the sole decision-loop owner and audit both stages."""

    def __init__(
        self,
        committer: LocalControlCommitter,
        state_machine: RuntimeStateMachine,
        *,
        node_id: str,
        account_id: str,
        boot_id: str,
        clock: Clock,
        owner_guard: Callable[[], None],
        event_id_factory: Callable[[], str] = new_event_id,
    ) -> None:
        if not isinstance(committer, LocalControlCommitter):
            raise ValueError("committer must implement LocalControlCommitter")
        if not isinstance(state_machine, RuntimeStateMachine):
            raise ValueError("state_machine must be a RuntimeStateMachine")
        if not isinstance(node_id, str) or _NODE_ID_PATTERN.fullmatch(node_id) is None:
            raise ValueError("node_id has an invalid format")
        if not isinstance(account_id, str) or _ACCOUNT_ID_PATTERN.fullmatch(account_id) is None:
            raise ValueError("account_id has an invalid format")
        if not isinstance(boot_id, str) or _BOOT_ID_PATTERN.fullmatch(boot_id) is None:
            raise ValueError("boot_id has an invalid format")
        if not callable(getattr(clock, "now", None)):
            raise ValueError("clock must implement Clock")
        if not callable(owner_guard):
            raise ValueError("owner_guard must be callable")
        if not callable(event_id_factory):
            raise ValueError("event_id_factory must be callable")
        self._committer = committer
        self._state_machine = state_machine
        self._node_id = node_id
        self._account_id = account_id
        self._boot_id = boot_id
        self._clock = clock
        self._owner_guard = owner_guard
        self._event_id_factory = event_id_factory

    async def apply(
        self,
        request: LocalControlRequest,
        *,
        health: RuntimeHealth | None = None,
        permit: RuntimeRecoveryPermit | None = None,
    ) -> LocalControlResult:
        self._owner_guard()
        if not isinstance(request, LocalControlRequest):
            raise ValueError("request must be a LocalControlRequest")

        if request.action is LocalControlAction.EMERGENCY_HALT:
            decision = self._request_transition(request, health=health, permit=permit)
        else:
            decision = None

        try:
            request_transaction = self._request_transaction(request)
            request_commit = await self._committer.commit(request_transaction)
            self._validate_request_commit(request_transaction, request_commit)
        except asyncio.CancelledError as exc:
            raise self._request_error("LOCAL_CONTROL_REQUEST_CANCELLED") from exc
        except Exception as exc:
            raise self._request_error("LOCAL_CONTROL_REQUEST_AUDIT_FAILED") from exc

        if request_commit.outcome is CommitOutcome.DUPLICATE:
            raise LocalControlError(
                "LOCAL_CONTROL_DUPLICATE_RECONCILIATION_REQUIRED",
                "the local control request already exists and will not be applied twice",
                fail_closed_state=self._state_machine.state,
                retryable=False,
                requires_reconciliation=True,
            )

        if decision is None:
            decision = self._decide(request, health=health, permit=permit)

        try:
            event_id = self._event_id_factory()
            decided_at = self._clock.now()
            audit_transaction = self._audit_transaction(request, decision, event_id, decided_at)
            audit_commit = await self._committer.commit(audit_transaction)
            self._validate_audit_commit(audit_transaction, audit_commit, event_id)
        except asyncio.CancelledError as exc:
            self._force_halt()
            raise self._audit_error("LOCAL_CONTROL_AUDIT_CANCELLED") from exc
        except Exception as exc:
            self._force_halt()
            raise self._audit_error("LOCAL_CONTROL_RESULT_AUDIT_FAILED") from exc

        return LocalControlResult(
            request=request,
            outcome=decision.outcome,
            previous_state=decision.previous_state,
            current_state=decision.current_state,
            transition_sequence=decision.transition_sequence,
            rejection_code=decision.rejection_code,
            blockers=decision.blockers,
            decided_at=decided_at,
            event_id=event_id,
            request_commit=request_commit,
            audit_commit=audit_commit,
        )

    def _decide(
        self,
        request: LocalControlRequest,
        *,
        health: RuntimeHealth | None,
        permit: RuntimeRecoveryPermit | None,
    ) -> _DecisionSnapshot:
        current = self._state_machine.state
        if current is not request.expected_state:
            return _DecisionSnapshot(
                TransitionOutcome.REJECTED,
                current,
                current,
                rejection_code="CONTROL_EXPECTED_STATE_MISMATCH",
            )
        if request.action.broadens_capability:
            assert request.confirmation is not None
            now = self._clock.now().value
            confirmation_age = now - request.confirmation.confirmed_at.value
            if confirmation_age < timedelta(0):
                return _DecisionSnapshot(
                    TransitionOutcome.REJECTED,
                    current,
                    current,
                    rejection_code="CONTROL_REQUEST_FROM_FUTURE",
                )
            if confirmation_age > MAX_RECOVERY_CONFIRMATION_AGE:
                return _DecisionSnapshot(
                    TransitionOutcome.REJECTED,
                    current,
                    current,
                    rejection_code="CONTROL_CONFIRMATION_EXPIRED",
                )
        return self._request_transition(request, health=health, permit=permit)

    def _request_transition(
        self,
        request: LocalControlRequest,
        *,
        health: RuntimeHealth | None,
        permit: RuntimeRecoveryPermit | None,
    ) -> _DecisionSnapshot:
        decision = self._state_machine.request_transition(
            request.action.target_state,
            authority=TransitionAuthority.LOCAL_OPERATOR,
            reason_code=request.reason_code,
            health=health,
            permit=permit,
        )
        return _DecisionSnapshot(
            decision.outcome,
            decision.previous_state,
            decision.current_state,
            decision.transition_sequence,
            decision.rejection_code,
            decision.blockers,
        )

    def _request_transaction(self, request: LocalControlRequest) -> JournalTransaction:
        confirmation = request.confirmation
        confirmation_payload = None
        if confirmation is not None:
            confirmation_payload = {
                "confirmation_id": confirmation.confirmation_id,
                "actor_id": confirmation.actor_id,
                "confirmed_at": confirmation.confirmed_at.to_wire(),
                "impact_hash": confirmation.impact_hash,
            }
        payload = {
            "schema_version": "1.0",
            "record_type": "LOCAL_CONTROL_REQUEST",
            "command_id": request.command_id,
            "action": request.action.value,
            "actor_id": request.actor_id,
            "reason_code": request.reason_code,
            "requested_at": request.requested_at.to_wire(),
            "expected_state": request.expected_state.value,
            "correlation_id": request.correlation_id,
            "confirmation": confirmation_payload,
            "cancel_active_orders": request.cancel_active_orders,
        }
        control_id = _control_record_id("request", request.command_id)
        record = LocalControlRecord(
            control_id,
            self._account_id,
            request.reason_code,
            request.requested_at,
            _document(control_id, payload),
        )
        return JournalTransaction(
            _transaction_id("request", request.command_id),
            (PutLocalControl(record),),
        )

    def _audit_transaction(
        self,
        request: LocalControlRequest,
        decision: _DecisionSnapshot,
        event_id: str,
        decided_at: ShanghaiTimestamp,
    ) -> JournalTransaction:
        result_reason = decision.rejection_code or request.reason_code
        payload = {
            "schema_version": "1.0",
            "record_type": "LOCAL_CONTROL_RESULT",
            "command_id": request.command_id,
            "action": request.action.value,
            "actor_id": request.actor_id,
            "reason_code": result_reason,
            "outcome": decision.outcome.value,
            "previous_state": decision.previous_state.value,
            "requested_state": request.action.target_state.value,
            "current_state": decision.current_state.value,
            "rejection_code": decision.rejection_code,
            "blockers": list(decision.blockers),
            "transition_sequence": decision.transition_sequence,
            "decided_at": decided_at.to_wire(),
        }
        control_id = _control_record_id("result", request.command_id)
        record = LocalControlRecord(
            control_id,
            self._account_id,
            result_reason,
            decided_at,
            _document(control_id, payload),
        )
        event_payload = {
            "command_id": request.command_id,
            "command_type": request.action.value,
            "actor_id": request.actor_id,
            "result": decision.outcome.value,
            "reason_code": result_reason,
            "previous_state": decision.previous_state.value,
            "new_state": decision.current_state.value,
        }
        event = EventDraft(
            event_id=event_id,
            node_id=self._node_id,
            account_id=self._account_id,
            boot_id=self._boot_id,
            correlation_id=request.correlation_id,
            causation_id=request.command_id,
            source=EventSource.OPERATOR,
            severity=(
                EventSeverity.CRITICAL
                if request.action is LocalControlAction.EMERGENCY_HALT
                else EventSeverity.WARNING
            ),
            event_type=EventType.CONTROL_COMMAND_APPLIED,
            aggregate_type=AggregateType.CONTROL,
            aggregate_id=request.command_id,
            trade_date=decided_at.trading_date,
            occurred_at=decided_at,
            payload=canonical_json_bytes(event_payload, exclude=frozenset()),
        )
        return JournalTransaction(
            _transaction_id("result", request.command_id),
            (PutLocalControl(record), AppendDomainEvent(event)),
        )

    @staticmethod
    def _validate_request_commit(
        transaction: JournalTransaction, result: object
    ) -> JournalCommitResult:
        if (
            not isinstance(result, JournalCommitResult)
            or result.transaction_id != transaction.transaction_id
            or result.persisted_events
        ):
            raise ValueError("persistence returned an invalid request audit result")
        return result

    @staticmethod
    def _validate_audit_commit(
        transaction: JournalTransaction,
        result: object,
        event_id: str,
    ) -> JournalCommitResult:
        if (
            not isinstance(result, JournalCommitResult)
            or result.transaction_id != transaction.transaction_id
            or result.outcome is not CommitOutcome.COMMITTED
            or len(result.persisted_events) != 1
            or result.persisted_events[0].event_id != event_id
        ):
            raise ValueError("persistence returned an invalid terminal audit result")
        return result

    def _request_error(self, code: str) -> LocalControlError:
        return LocalControlError(
            code,
            "the local control request audit is not known to be durable",
            fail_closed_state=self._state_machine.state,
            retryable=False,
            requires_reconciliation=True,
        )

    def _audit_error(self, code: str) -> LocalControlError:
        return LocalControlError(
            code,
            "the local control result audit is unresolved; runtime was forced closed",
            fail_closed_state=self._state_machine.state,
            retryable=False,
            requires_reconciliation=True,
        )

    def _force_halt(self) -> None:
        if self._state_machine.state in {RuntimeState.STOPPING, RuntimeState.STOPPED}:
            return
        self._state_machine.request_transition(
            RuntimeState.HALT,
            authority=TransitionAuthority.LOCAL_OPERATOR,
            reason_code="CONTROL_AUDIT_FAILURE",
        )


def _transaction_id(stage: str, command_id: str) -> str:
    digest = hashlib.sha256(f"local-control:v1:{stage}:{command_id}".encode()).hexdigest()
    return f"txn_control_{stage}_{digest[:32]}"


def _control_record_id(stage: str, command_id: str) -> str:
    digest = hashlib.sha256(f"local-control-record:v1:{stage}:{command_id}".encode()).hexdigest()
    return f"control_{stage}_{digest[:32]}"


def _document(document_id: str, payload: Mapping[str, object]) -> CanonicalJsonDocument:
    body = canonical_json_bytes(payload, exclude=frozenset())
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    return CanonicalJsonDocument(document_id, digest, body)
