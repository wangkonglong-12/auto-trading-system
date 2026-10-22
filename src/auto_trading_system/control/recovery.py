"""Fail-closed reconstruction of persisted local operator restrictions."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from auto_trading_system.control.models import (
    ControlConfirmation,
    LocalControlAction,
    LocalControlRecoveryError,
    LocalControlRecoveryState,
    LocalControlRequest,
    PersistedLocalControlReceipt,
)
from auto_trading_system.domain import ShanghaiTimestamp
from auto_trading_system.journal import LocalControlRecord
from auto_trading_system.runtime import (
    RuntimeState,
    RuntimeStateMachine,
    TransitionAuthority,
    TransitionDecision,
    TransitionOutcome,
)

_REASON_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
_REQUEST_FIELDS = {
    "schema_version",
    "record_type",
    "command_id",
    "action",
    "actor_id",
    "reason_code",
    "requested_at",
    "expected_state",
    "correlation_id",
    "confirmation",
    "cancel_active_orders",
}
_RESULT_FIELDS = {
    "schema_version",
    "record_type",
    "command_id",
    "action",
    "actor_id",
    "reason_code",
    "outcome",
    "previous_state",
    "requested_state",
    "current_state",
    "rejection_code",
    "blockers",
    "transition_sequence",
    "decided_at",
}
_CONFIRMATION_FIELDS = {"confirmation_id", "actor_id", "confirmed_at", "impact_hash"}
_RESTRICTIVE_STATES = {
    RuntimeState.PAUSE_BUY,
    RuntimeState.SELL_ONLY,
    RuntimeState.MANUAL,
    RuntimeState.HALT,
}


@dataclass(frozen=True, slots=True)
class _RecoveredResult:
    record: LocalControlRecord
    command_id: str
    action: LocalControlAction
    actor_id: str
    reason_code: str
    outcome: TransitionOutcome
    previous_state: RuntimeState
    current_state: RuntimeState
    transition_sequence: int | None
    rejection_code: str | None
    blockers: tuple[str, ...]
    decided_at: ShanghaiTimestamp

    @property
    def order_key(self) -> tuple[object, int, str]:
        return (
            self.record.created_at.value,
            self.transition_sequence or 0,
            self.record.control_id,
        )


def project_local_control_recovery(
    records: tuple[LocalControlRecord, ...],
    *,
    account_id: str,
) -> LocalControlRecoveryState:
    """Project immutable request/result records; any missing result recovers as HALT."""
    requests, parsed_results = _validated_control_history(records, account_id=account_id)
    active_state: RuntimeState | None = None
    last_command_id: str | None = None
    for result in parsed_results:
        last_command_id = result.command_id
        if result.outcome in {TransitionOutcome.APPLIED, TransitionOutcome.NOOP}:
            active_state = (
                result.current_state if result.current_state in _RESTRICTIVE_STATES else None
            )

    unresolved = tuple(sorted(set(requests) - {result.command_id for result in parsed_results}))
    if unresolved:
        active_state = RuntimeState.HALT
    return LocalControlRecoveryState(active_state, last_command_id, unresolved)


def project_local_control_receipts(
    records: tuple[LocalControlRecord, ...],
    *,
    account_id: str,
) -> tuple[PersistedLocalControlReceipt, ...]:
    """Return durable command receipts while applying the recovery integrity rules."""
    requests, parsed_results = _validated_control_history(records, account_id=account_id)
    results_by_command = {result.command_id: result for result in parsed_results}
    receipts: list[PersistedLocalControlReceipt] = []
    ordered_requests = sorted(
        requests.values(),
        key=lambda request: (request.requested_at.value, request.command_id),
    )
    for request in ordered_requests:
        result = results_by_command.get(request.command_id)
        if result is None:
            receipts.append(
                PersistedLocalControlReceipt(request, None, None, None, None, None, None, ())
            )
            continue
        receipts.append(
            PersistedLocalControlReceipt(
                request=request,
                outcome=result.outcome,
                decided_at=result.decided_at,
                previous_state=result.previous_state,
                current_state=result.current_state,
                transition_sequence=result.transition_sequence,
                rejection_code=result.rejection_code,
                blockers=result.blockers,
            )
        )
    return tuple(receipts)


def _validated_control_history(
    records: tuple[LocalControlRecord, ...],
    *,
    account_id: str,
) -> tuple[dict[str, LocalControlRequest], tuple[_RecoveredResult, ...]]:
    if not isinstance(records, tuple):
        raise LocalControlRecoveryError("records must be an immutable tuple")
    requests: dict[str, LocalControlRequest] = {}
    raw_results: list[tuple[LocalControlRecord, Mapping[str, Any]]] = []
    for record in records:
        if not isinstance(record, LocalControlRecord) or record.account_id != account_id:
            raise LocalControlRecoveryError("control record account or type is invalid")
        _validate_document_digest(record)
        try:
            payload = record.document.to_mapping()
        except (TypeError, ValueError) as exc:
            raise LocalControlRecoveryError("control document is invalid") from exc
        if payload.get("schema_version") != "1.0":
            raise LocalControlRecoveryError("control record version is unsupported")
        record_type = payload.get("record_type")
        if record_type == "LOCAL_CONTROL_REQUEST":
            recovered_request = _parse_request(record, payload)
            if recovered_request.command_id in requests:
                raise LocalControlRecoveryError("control request identity is duplicated")
            requests[recovered_request.command_id] = recovered_request
        elif record_type == "LOCAL_CONTROL_RESULT":
            raw_results.append((record, payload))
        else:
            raise LocalControlRecoveryError("control record type is unsupported")

    parsed_results = tuple(
        sorted(
            (_parse_result(record, payload) for record, payload in raw_results),
            key=lambda value: value.order_key,
        )
    )
    resolved: set[str] = set()
    for result in parsed_results:
        request = requests.get(result.command_id)
        if request is None or result.command_id in resolved:
            raise LocalControlRecoveryError("control result is orphaned or duplicated")
        if result.action is not request.action or result.actor_id != request.actor_id:
            raise LocalControlRecoveryError("control result does not match its request")
        if (
            result.outcome is not TransitionOutcome.REJECTED
            and result.reason_code != request.reason_code
        ):
            raise LocalControlRecoveryError("successful result reason does not match its request")
        resolved.add(result.command_id)
    return requests, parsed_results


def restore_local_control_restriction(
    state_machine: RuntimeStateMachine,
    recovery: LocalControlRecoveryState,
) -> TransitionDecision | None:
    """Reassert a recovered local restriction before remote commands are consumed."""
    if not isinstance(state_machine, RuntimeStateMachine):
        raise ValueError("state_machine must be a RuntimeStateMachine")
    if not isinstance(recovery, LocalControlRecoveryState):
        raise ValueError("recovery must be a LocalControlRecoveryState")
    if recovery.active_state is None:
        return None
    reason_code = (
        "LOCAL_CONTROL_UNRESOLVED"
        if recovery.unresolved_command_ids
        else "LOCAL_OVERRIDE_RECOVERED"
    )
    return state_machine.request_transition(
        RuntimeState.HALT,
        authority=TransitionAuthority.LOCAL_OPERATOR,
        reason_code=reason_code,
    )


def _parse_request(record: LocalControlRecord, payload: Mapping[str, Any]) -> LocalControlRequest:
    if set(payload) != _REQUEST_FIELDS:
        raise LocalControlRecoveryError("control request fields are invalid")
    raw_confirmation = payload.get("confirmation")
    confirmation: ControlConfirmation | None = None
    if raw_confirmation is not None:
        if not isinstance(raw_confirmation, Mapping) or set(raw_confirmation) != (
            _CONFIRMATION_FIELDS
        ):
            raise LocalControlRecoveryError("control confirmation fields are invalid")
        try:
            confirmation = ControlConfirmation(
                _required_string(raw_confirmation, "confirmation_id"),
                _required_string(raw_confirmation, "actor_id"),
                _timestamp(raw_confirmation, "confirmed_at"),
                _required_string(raw_confirmation, "impact_hash"),
            )
        except ValueError as exc:
            raise LocalControlRecoveryError("control confirmation values are invalid") from exc
    try:
        recovered = LocalControlRequest(
            _required_string(payload, "command_id"),
            LocalControlAction(_required_string(payload, "action")),
            _required_string(payload, "actor_id"),
            _required_string(payload, "reason_code"),
            _timestamp(payload, "requested_at"),
            RuntimeState(_required_string(payload, "expected_state")),
            _required_string(payload, "correlation_id"),
            confirmation,
            payload.get("cancel_active_orders"),
        )
    except (TypeError, ValueError) as exc:
        raise LocalControlRecoveryError("control request values are invalid") from exc
    if recovered.requested_at != record.created_at:
        raise LocalControlRecoveryError("control request time does not match its record")
    return recovered


def _parse_result(record: LocalControlRecord, payload: Mapping[str, Any]) -> _RecoveredResult:
    if set(payload) != _RESULT_FIELDS:
        raise LocalControlRecoveryError("control result fields are invalid")
    try:
        command_id = _required_string(payload, "command_id")
        action = LocalControlAction(_required_string(payload, "action"))
        actor_id = _required_string(payload, "actor_id")
        reason_code = _required_string(payload, "reason_code")
        outcome = TransitionOutcome(_required_string(payload, "outcome"))
        previous_state = RuntimeState(_required_string(payload, "previous_state"))
        requested_state = RuntimeState(_required_string(payload, "requested_state"))
        current_state = RuntimeState(_required_string(payload, "current_state"))
        decided_at = _timestamp(payload, "decided_at")
    except (TypeError, ValueError) as exc:
        raise LocalControlRecoveryError("control result values are invalid") from exc
    if _REASON_PATTERN.fullmatch(reason_code) is None:
        raise LocalControlRecoveryError("control result reason is invalid")
    if requested_state is not action.target_state or decided_at != record.created_at:
        raise LocalControlRecoveryError("control result target or time is invalid")
    blockers = payload.get("blockers")
    if not isinstance(blockers, list) or not all(
        isinstance(value, str) and _REASON_PATTERN.fullmatch(value) for value in blockers
    ):
        raise LocalControlRecoveryError("control result blockers are invalid")
    transition_sequence = payload.get("transition_sequence")
    rejection_code = payload.get("rejection_code")
    if outcome is TransitionOutcome.APPLIED:
        if (
            type(transition_sequence) is not int
            or transition_sequence < 1
            or rejection_code is not None
            or previous_state is current_state
            or current_state is not action.target_state
        ):
            raise LocalControlRecoveryError("applied control result is inconsistent")
    elif outcome is TransitionOutcome.NOOP:
        if (
            transition_sequence is not None
            or rejection_code is not None
            or previous_state is not current_state
            or current_state is not action.target_state
        ):
            raise LocalControlRecoveryError("noop control result is inconsistent")
    elif (
        transition_sequence is not None
        or not isinstance(rejection_code, str)
        or _REASON_PATTERN.fullmatch(rejection_code) is None
        or previous_state is not current_state
        or reason_code != rejection_code
    ):
        raise LocalControlRecoveryError("rejected control result is inconsistent")
    return _RecoveredResult(
        record,
        command_id,
        action,
        actor_id,
        reason_code,
        outcome,
        previous_state,
        current_state,
        transition_sequence,
        rejection_code,
        tuple(blockers),
        decided_at,
    )


def _validate_document_digest(record: LocalControlRecord) -> None:
    expected = "sha256:" + hashlib.sha256(record.document.body).hexdigest()
    if record.document.content_hash != expected:
        raise LocalControlRecoveryError("control document digest does not match its body")


def _required_string(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        raise LocalControlRecoveryError(f"control {field} must be a string")
    return value


def _timestamp(payload: Mapping[str, Any], field: str) -> ShanghaiTimestamp:
    try:
        return ShanghaiTimestamp.parse(_required_string(payload, field))
    except ValueError as exc:
        raise LocalControlRecoveryError(f"control {field} is invalid") from exc
