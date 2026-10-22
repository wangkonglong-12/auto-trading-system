from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import cast

import pytest

from auto_trading_system.control import (
    ControlConfirmation,
    LocalControlAction,
    LocalControlCoordinator,
    LocalControlError,
    LocalControlRecoveryError,
    LocalControlRequest,
    project_local_control_recovery,
    restore_local_control_restriction,
)
from auto_trading_system.domain import ShanghaiTimestamp
from auto_trading_system.journal import (
    AppendDomainEvent,
    CanonicalJsonDocument,
    CommitOutcome,
    JournalCommitResult,
    JournalTransaction,
    PersistedEvent,
    PutLocalControl,
)
from auto_trading_system.protocol.canonical_json import canonical_json_bytes
from auto_trading_system.runtime import (
    RuntimeCapability,
    RuntimeHealth,
    RuntimeRecoveryPermit,
    RuntimeState,
    RuntimeStateMachine,
    TransitionAuthority,
    TransitionOutcome,
)

NODE_ID = "node-demo-001"
ACCOUNT_ID = "acct_demo_001"
BOOT_ID = "boot_11111111111111111111111111111111"
EVENT_ID = "evt_22222222222222222222222222222222"
IMPACT_HASH = "sha256:" + "3" * 64


@dataclass
class OwnerGuard:
    calls: int = 0
    failure: RuntimeError | None = None

    def __call__(self) -> None:
        self.calls += 1
        if self.failure is not None:
            raise self.failure


@dataclass
class IncrementingClock:
    value: int = 0

    def monotonic_ns(self) -> int:
        self.value += 1
        return self.value


@dataclass
class MutableWallClock:
    value: ShanghaiTimestamp = field(
        default_factory=lambda: ShanghaiTimestamp.parse("2026-09-04T10:00:02+08:00")
    )

    def now(self) -> ShanghaiTimestamp:
        return self.value


@dataclass
class ScriptedCommitter:
    responses: list[object] = field(default_factory=list)
    state_supplier: object | None = None
    transactions: list[JournalTransaction] = field(default_factory=list)
    states_at_commit: list[RuntimeState] = field(default_factory=list)

    async def commit(self, transaction: JournalTransaction) -> JournalCommitResult:
        self.transactions.append(transaction)
        if callable(self.state_supplier):
            self.states_at_commit.append(self.state_supplier())
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            if isinstance(response, CommitOutcome):
                return self._result(transaction, response)
            return cast(JournalCommitResult, response)
        return self._result(transaction, CommitOutcome.COMMITTED)

    @staticmethod
    def _result(transaction: JournalTransaction, outcome: CommitOutcome) -> JournalCommitResult:
        events = tuple(
            PersistedEvent(operation.draft.event_id, operation.draft.boot_id, 1, _event_document())
            for operation in transaction.operations
            if isinstance(operation, AppendDomainEvent)
        )
        return JournalCommitResult(
            transaction.transaction_id,
            outcome,
            len(transaction.transaction_id),
            events,
        )


def _event_document() -> CanonicalJsonDocument:
    return CanonicalJsonDocument(
        EVENT_ID,
        "sha256:" + "4" * 64,
        b'{"event":"stored"}',
    )


def healthy_runtime() -> RuntimeHealth:
    return RuntimeHealth(
        qmt_connected=True,
        broker_account_ok=True,
        market_healthy=True,
        journal_healthy=True,
        reconciliation_complete=True,
        critical_discrepancy=False,
        unknown_order=False,
        clock_within_limit=True,
        disk_writable=True,
        event_queue_intact=True,
    )


def full_permit() -> RuntimeRecoveryPermit:
    return RuntimeRecoveryPermit("recon_control_0001", True, True)


def state_machine(
    guard: OwnerGuard | None = None,
) -> tuple[RuntimeStateMachine, OwnerGuard]:
    effective_guard = guard or OwnerGuard()
    return (
        RuntimeStateMachine(
            capability=RuntimeCapability.FULL,
            monotonic_clock=IncrementingClock(),
            owner_guard=effective_guard,
        ),
        effective_guard,
    )


def advance_to_auto(machine: RuntimeStateMachine) -> None:
    machine.request_transition(
        RuntimeState.CONNECTING,
        authority=TransitionAuthority.LIFECYCLE,
        reason_code="START_CONNECTING",
    )
    machine.request_transition(
        RuntimeState.RECONCILING,
        authority=TransitionAuthority.LIFECYCLE,
        reason_code="START_RECONCILING",
    )
    machine.request_transition(
        RuntimeState.READY_PAUSED,
        authority=TransitionAuthority.LIFECYCLE,
        reason_code="START_RECONCILED",
        health=healthy_runtime(),
    )
    machine.request_transition(
        RuntimeState.AUTO,
        authority=TransitionAuthority.RUNTIME,
        reason_code="AUTO_ACTIVATED",
        health=healthy_runtime(),
        permit=full_permit(),
    )


def confirmation(*, confirmed_at: str = "2026-09-04T10:00:01+08:00") -> ControlConfirmation:
    return ControlConfirmation(
        "confirm_control_0001",
        "operator_demo",
        ShanghaiTimestamp.parse(confirmed_at),
        IMPACT_HASH,
    )


def request(
    action: LocalControlAction,
    expected_state: RuntimeState,
    *,
    command_suffix: str = "00000001",
    requested_at: str = "2026-09-04T10:00:00+08:00",
    confirmation_value: ControlConfirmation | None | object = ...,
) -> LocalControlRequest:
    effective_confirmation: ControlConfirmation | None
    if confirmation_value is ...:
        effective_confirmation = confirmation() if action.requires_confirmation else None
    else:
        effective_confirmation = cast(ControlConfirmation | None, confirmation_value)
    return LocalControlRequest(
        f"cmd_control_{command_suffix}",
        action,
        "operator_demo",
        "OPERATOR_REQUEST",
        ShanghaiTimestamp.parse(requested_at),
        expected_state,
        f"corr_control_{command_suffix}",
        effective_confirmation,
        True if action is LocalControlAction.EMERGENCY_HALT else None,
    )


def coordinator(
    committer: ScriptedCommitter,
    machine: RuntimeStateMachine,
    guard: OwnerGuard,
    *,
    clock: MutableWallClock | None = None,
) -> LocalControlCoordinator:
    return LocalControlCoordinator(
        committer,
        machine,
        node_id=NODE_ID,
        account_id=ACCOUNT_ID,
        boot_id=BOOT_ID,
        clock=clock or MutableWallClock(),
        owner_guard=guard,
        event_id_factory=lambda: EVENT_ID,
    )


@pytest.mark.parametrize(
    ("action", "target"),
    [
        (LocalControlAction.PAUSE_BUY, RuntimeState.PAUSE_BUY),
        (LocalControlAction.SELL_ONLY, RuntimeState.SELL_ONLY),
        (LocalControlAction.MANUAL_TAKEOVER, RuntimeState.MANUAL),
        (LocalControlAction.EMERGENCY_HALT, RuntimeState.HALT),
    ],
)
def test_restrictive_controls_are_audited_and_emit_domain_event(
    action: LocalControlAction, target: RuntimeState
) -> None:
    machine, guard = state_machine()
    advance_to_auto(machine)
    committer = ScriptedCommitter(state_supplier=lambda: machine.state)
    current = coordinator(committer, machine, guard)

    result = asyncio.run(current.apply(request(action, RuntimeState.AUTO)))

    assert result.outcome is TransitionOutcome.APPLIED
    assert result.current_state is target
    assert machine.state is target
    assert len(committer.transactions) == 2
    assert isinstance(committer.transactions[0].operations[0], PutLocalControl)
    assert [type(operation) for operation in committer.transactions[1].operations] == [
        PutLocalControl,
        AppendDomainEvent,
    ]
    event_operation = committer.transactions[1].operations[1]
    assert isinstance(event_operation, AppendDomainEvent)
    assert event_operation.draft.source.value == "OPERATOR"
    assert event_operation.draft.payload
    assert result.audit_commit.persisted_events[0].event_id == EVENT_ID


def test_emergency_halt_changes_state_before_first_persistence_attempt() -> None:
    machine, guard = state_machine()
    advance_to_auto(machine)
    committer = ScriptedCommitter(state_supplier=lambda: machine.state)

    asyncio.run(
        coordinator(committer, machine, guard).apply(
            request(LocalControlAction.EMERGENCY_HALT, RuntimeState.AUTO)
        )
    )

    assert committer.states_at_commit == [RuntimeState.HALT, RuntimeState.HALT]


def test_emergency_halt_remains_active_when_request_audit_fails() -> None:
    machine, guard = state_machine()
    advance_to_auto(machine)
    committer = ScriptedCommitter([RuntimeError("disk unavailable")])

    with pytest.raises(LocalControlError) as captured:
        asyncio.run(
            coordinator(committer, machine, guard).apply(
                request(LocalControlAction.EMERGENCY_HALT, RuntimeState.AUTO)
            )
        )

    assert captured.value.code == "LOCAL_CONTROL_REQUEST_AUDIT_FAILED"
    assert captured.value.fail_closed_state is RuntimeState.HALT
    assert machine.state.value == RuntimeState.HALT.value


def test_non_emergency_request_never_changes_state_when_request_audit_fails() -> None:
    machine, guard = state_machine()
    advance_to_auto(machine)
    committer = ScriptedCommitter([RuntimeError("disk unavailable")])

    with pytest.raises(LocalControlError):
        asyncio.run(
            coordinator(committer, machine, guard).apply(
                request(LocalControlAction.PAUSE_BUY, RuntimeState.AUTO)
            )
        )

    assert machine.state is RuntimeState.AUTO


@pytest.mark.parametrize(
    ("action", "expected_state"),
    [
        (LocalControlAction.PAUSE_BUY, RuntimeState.AUTO),
        (LocalControlAction.EMERGENCY_HALT, RuntimeState.AUTO),
    ],
)
def test_duplicate_control_is_never_applied_twice(
    action: LocalControlAction, expected_state: RuntimeState
) -> None:
    machine, guard = state_machine()
    advance_to_auto(machine)
    committer = ScriptedCommitter([CommitOutcome.DUPLICATE])

    with pytest.raises(LocalControlError) as captured:
        asyncio.run(coordinator(committer, machine, guard).apply(request(action, expected_state)))

    assert captured.value.code == "LOCAL_CONTROL_DUPLICATE_RECONCILIATION_REQUIRED"
    assert machine.state is (
        RuntimeState.HALT if action is LocalControlAction.EMERGENCY_HALT else RuntimeState.AUTO
    )
    assert len(committer.transactions) == 1


def test_expected_state_mismatch_is_rejected_and_audited_without_mutation() -> None:
    machine, guard = state_machine()
    advance_to_auto(machine)
    committer = ScriptedCommitter()

    result = asyncio.run(
        coordinator(committer, machine, guard).apply(
            request(LocalControlAction.PAUSE_BUY, RuntimeState.READY_PAUSED)
        )
    )

    assert result.outcome is TransitionOutcome.REJECTED
    assert result.rejection_code == "CONTROL_EXPECTED_STATE_MISMATCH"
    assert result.current_state is RuntimeState.AUTO
    assert machine.state is RuntimeState.AUTO
    assert len(committer.transactions) == 2


def test_local_halt_cannot_be_cleared_by_central_and_recovery_requires_two_steps() -> None:
    machine, guard = state_machine()
    advance_to_auto(machine)
    committer = ScriptedCommitter()
    current = coordinator(committer, machine, guard)
    asyncio.run(current.apply(request(LocalControlAction.EMERGENCY_HALT, RuntimeState.AUTO)))

    remote_clear = machine.request_transition(
        RuntimeState.READY_PAUSED,
        authority=TransitionAuthority.CENTRAL,
        reason_code="CENTRAL_HALT_CLEAR",
        health=healthy_runtime(),
        permit=full_permit(),
    )
    assert remote_clear.outcome is TransitionOutcome.REJECTED
    assert machine.state is RuntimeState.HALT

    clear = asyncio.run(
        current.apply(
            request(
                LocalControlAction.CLEAR_HALT,
                RuntimeState.HALT,
                command_suffix="00000002",
                requested_at="2026-09-04T10:00:01+08:00",
            ),
            health=healthy_runtime(),
            permit=full_permit(),
        )
    )
    assert clear.current_state is RuntimeState.READY_PAUSED

    remote_auto = machine.request_transition(
        RuntimeState.AUTO,
        authority=TransitionAuthority.CENTRAL,
        reason_code="CENTRAL_AUTO",
        health=healthy_runtime(),
        permit=full_permit(),
    )
    assert remote_auto.rejection_code == "REMOTE_AUTO_RESUME_REQUIRES_LOCAL_DECISION"
    assert machine.state.value == RuntimeState.READY_PAUSED.value

    auto = asyncio.run(
        current.apply(
            request(
                LocalControlAction.REQUEST_AUTO,
                RuntimeState.READY_PAUSED,
                command_suffix="00000003",
                requested_at="2026-09-04T10:00:01+08:00",
            ),
            health=healthy_runtime(),
            permit=full_permit(),
        )
    )
    assert auto.current_state is RuntimeState.AUTO
    controls = tuple(
        operation.control
        for transaction in committer.transactions
        for operation in transaction.operations
        if isinstance(operation, PutLocalControl)
    )
    projected = project_local_control_recovery(controls, account_id=ACCOUNT_ID)
    assert projected.active_state is None
    assert projected.last_command_id == auto.request.command_id


@pytest.mark.parametrize(
    ("confirmed_at", "code"),
    [
        ("2026-09-04T09:57:00+08:00", "CONTROL_CONFIRMATION_EXPIRED"),
        ("2026-09-04T10:00:03+08:00", "CONTROL_REQUEST_FROM_FUTURE"),
    ],
)
def test_stale_or_future_recovery_confirmation_is_rejected_and_audited(
    confirmed_at: str, code: str
) -> None:
    machine, guard = state_machine()
    advance_to_auto(machine)
    machine.request_transition(
        RuntimeState.HALT,
        authority=TransitionAuthority.LOCAL_OPERATOR,
        reason_code="LOCAL_EMERGENCY_HALT",
    )
    current_request = request(
        LocalControlAction.CLEAR_HALT,
        RuntimeState.HALT,
        requested_at="2026-09-04T09:56:59+08:00",
        confirmation_value=confirmation(confirmed_at=confirmed_at),
    )

    result = asyncio.run(
        coordinator(ScriptedCommitter(), machine, guard).apply(
            current_request,
            health=healthy_runtime(),
            permit=full_permit(),
        )
    )

    assert result.rejection_code == code
    assert machine.state is RuntimeState.HALT


def test_terminal_audit_failure_after_clear_forces_runtime_back_to_halt() -> None:
    machine, guard = state_machine()
    advance_to_auto(machine)
    machine.request_transition(
        RuntimeState.HALT,
        authority=TransitionAuthority.LOCAL_OPERATOR,
        reason_code="LOCAL_EMERGENCY_HALT",
    )
    committer = ScriptedCommitter([CommitOutcome.COMMITTED, RuntimeError("write failed")])

    with pytest.raises(LocalControlError) as captured:
        asyncio.run(
            coordinator(committer, machine, guard).apply(
                request(LocalControlAction.CLEAR_HALT, RuntimeState.HALT),
                health=healthy_runtime(),
                permit=full_permit(),
            )
        )

    assert captured.value.code == "LOCAL_CONTROL_RESULT_AUDIT_FAILED"
    assert captured.value.fail_closed_state is RuntimeState.HALT
    assert machine.state is RuntimeState.HALT


def test_invalid_event_factory_after_state_change_forces_runtime_to_halt() -> None:
    machine, guard = state_machine()
    advance_to_auto(machine)
    committer = ScriptedCommitter()
    current = LocalControlCoordinator(
        committer,
        machine,
        node_id=NODE_ID,
        account_id=ACCOUNT_ID,
        boot_id=BOOT_ID,
        clock=MutableWallClock(),
        owner_guard=guard,
        event_id_factory=lambda: "invalid-event-id",
    )

    with pytest.raises(LocalControlError) as captured:
        asyncio.run(current.apply(request(LocalControlAction.PAUSE_BUY, RuntimeState.AUTO)))

    assert captured.value.code == "LOCAL_CONTROL_RESULT_AUDIT_FAILED"
    assert machine.state is RuntimeState.HALT
    assert len(committer.transactions) == 1


def test_owner_failure_happens_before_any_persistence_or_state_mutation() -> None:
    guard = OwnerGuard(failure=RuntimeError("not owner"))
    machine, _ = state_machine(guard)
    committer = ScriptedCommitter()

    with pytest.raises(RuntimeError, match="not owner"):
        asyncio.run(
            coordinator(committer, machine, guard).apply(
                request(LocalControlAction.EMERGENCY_HALT, RuntimeState.BOOTING)
            )
        )

    assert committer.transactions == []
    assert machine.state is RuntimeState.BOOTING


def test_audit_documents_are_strict_and_recovery_projects_active_and_unresolved() -> None:
    machine, guard = state_machine()
    advance_to_auto(machine)
    committer = ScriptedCommitter()
    result = asyncio.run(
        coordinator(committer, machine, guard).apply(
            request(LocalControlAction.EMERGENCY_HALT, RuntimeState.AUTO)
        )
    )
    controls = tuple(
        operation.control
        for transaction in committer.transactions
        for operation in transaction.operations
        if isinstance(operation, PutLocalControl)
    )

    projected = project_local_control_recovery(controls, account_id=ACCOUNT_ID)

    assert projected.active_state is RuntimeState.HALT
    assert projected.last_command_id == result.request.command_id
    assert projected.unresolved_command_ids == ()
    request_only = project_local_control_recovery(controls[:1], account_id=ACCOUNT_ID)
    assert request_only.active_state is RuntimeState.HALT
    assert request_only.unresolved_command_ids == (result.request.command_id,)
    restarted, _ = state_machine()
    restored = restore_local_control_restriction(restarted, projected)
    assert restored is not None
    assert restored.current_state is RuntimeState.HALT
    assert restarted.restriction_authority is TransitionAuthority.LOCAL_OPERATOR
    audit_event = cast(AppendDomainEvent, committer.transactions[1].operations[1]).draft
    assert audit_event.payload


def test_any_non_halt_override_recovers_fail_closed_as_halt() -> None:
    recovery = project_local_control_recovery((), account_id=ACCOUNT_ID)
    assert restore_local_control_restriction(state_machine()[0], recovery) is None
    active = replace(recovery, active_state=RuntimeState.SELL_ONLY)
    booting, _ = state_machine()
    restored = restore_local_control_restriction(booting, active)
    assert restored is not None
    assert restored.current_state is RuntimeState.HALT
    assert restored.reason_code == "LOCAL_OVERRIDE_RECOVERED"


def test_recovery_rejects_malformed_or_wrong_account_records() -> None:
    machine, guard = state_machine()
    advance_to_auto(machine)
    committer = ScriptedCommitter()
    asyncio.run(
        coordinator(committer, machine, guard).apply(
            request(LocalControlAction.EMERGENCY_HALT, RuntimeState.AUTO)
        )
    )
    record = cast(PutLocalControl, committer.transactions[0].operations[0]).control

    with pytest.raises(LocalControlRecoveryError):
        project_local_control_recovery((record,), account_id="acct_other_001")
    malformed_body = canonical_json_bytes({"schema_version": "1.0"}, exclude=frozenset())
    malformed_document = CanonicalJsonDocument(
        record.document.document_id,
        "sha256:" + hashlib.sha256(malformed_body).hexdigest(),
        malformed_body,
    )
    malformed = replace(
        record,
        document=malformed_document,
    )
    with pytest.raises(LocalControlRecoveryError):
        project_local_control_recovery((malformed,), account_id=ACCOUNT_ID)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: replace(value, command_id="bad"),
        lambda value: replace(value, actor_id="x"),
        lambda value: replace(value, reason_code="bad"),
        lambda value: replace(value, confirmation=None),
        lambda value: replace(value, expected_state=RuntimeState.AUTO),
    ],
)
def test_clear_halt_request_rejects_unsafe_model_values(
    mutator: Callable[[LocalControlRequest], LocalControlRequest],
) -> None:
    original = request(LocalControlAction.CLEAR_HALT, RuntimeState.HALT)

    with pytest.raises(ValueError):
        mutator(original)


def test_active_order_disposition_is_required_only_for_emergency_halt() -> None:
    with pytest.raises(ValueError, match="active-order disposition"):
        replace(
            request(LocalControlAction.EMERGENCY_HALT, RuntimeState.AUTO),
            cancel_active_orders=None,
        )
    with pytest.raises(ValueError, match="only EMERGENCY_HALT"):
        replace(
            request(LocalControlAction.PAUSE_BUY, RuntimeState.AUTO),
            cancel_active_orders=True,
        )
