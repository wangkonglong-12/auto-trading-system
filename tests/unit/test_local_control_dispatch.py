from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace

import pytest

from auto_trading_system.control import (
    LocalControlAction,
    LocalControlCoordinator,
    LocalControlDecisionHandler,
    LocalControlReceiptRegistry,
    LocalControlReceiptStatus,
    LocalControlRequest,
    LocalControlSubmissionError,
    LocalControlSubmissionFailureKind,
    LocalControlSubmissionService,
    PersistedLocalControlReceipt,
)
from auto_trading_system.domain import ShanghaiTimestamp
from auto_trading_system.journal import (
    AppendDomainEvent,
    CanonicalJsonDocument,
    CommitOutcome,
    JournalCommitResult,
    JournalTransaction,
    PersistedEvent,
)
from auto_trading_system.runtime import (
    EventPriority,
    PublishOutcome,
    RuntimeCapability,
    RuntimeEventSource,
    RuntimeHealth,
    RuntimeRecoveryPermit,
    RuntimeState,
    RuntimeStateMachine,
    TransitionAuthority,
    TransitionOutcome,
)


@dataclass
class MutableClock:
    value: ShanghaiTimestamp = field(
        default_factory=lambda: ShanghaiTimestamp.parse("2026-09-04T10:00:02+08:00")
    )

    def now(self) -> ShanghaiTimestamp:
        return self.value


@dataclass
class IncrementingClock:
    value: int = 0

    def monotonic_ns(self) -> int:
        self.value += 1
        return self.value


@dataclass
class StubReceiptReader:
    receipt: PersistedLocalControlReceipt | None = None
    failure: BaseException | None = None
    command_ids: list[str] = field(default_factory=list)

    async def find(self, command_id: str) -> PersistedLocalControlReceipt | None:
        self.command_ids.append(command_id)
        if self.failure is not None:
            raise self.failure
        return self.receipt


@dataclass
class StubIngress:
    outcome: PublishOutcome = PublishOutcome.ACCEPTED
    failure: BaseException | None = None
    calls: list[tuple[LocalControlRequest, RuntimeEventSource, EventPriority]] = field(
        default_factory=list
    )

    def publish(
        self,
        payload: LocalControlRequest,
        *,
        source: RuntimeEventSource,
        priority: EventPriority = EventPriority.NORMAL,
    ) -> PublishOutcome:
        self.calls.append((payload, source, priority))
        if self.failure is not None:
            raise self.failure
        return self.outcome


@dataclass
class ScriptedCommitter:
    failure: BaseException | None = None
    transactions: list[JournalTransaction] = field(default_factory=list)

    async def commit(self, transaction: JournalTransaction) -> JournalCommitResult:
        self.transactions.append(transaction)
        if self.failure is not None:
            raise self.failure
        events = tuple(
            PersistedEvent(
                operation.draft.event_id,
                operation.draft.boot_id,
                1,
                CanonicalJsonDocument(
                    operation.draft.event_id,
                    "sha256:" + "8" * 64,
                    b'{"stored":true}',
                ),
            )
            for operation in transaction.operations
            if isinstance(operation, AppendDomainEvent)
        )
        return JournalCommitResult(
            transaction.transaction_id,
            CommitOutcome.COMMITTED,
            len(self.transactions),
            events,
        )


def ready_auto_machine() -> RuntimeStateMachine:
    machine = RuntimeStateMachine(
        capability=RuntimeCapability.FULL,
        monotonic_clock=IncrementingClock(),
        owner_guard=lambda: None,
    )
    health = RuntimeHealth(True, True, True, True, True, False, False, True, True, True)
    permit = RuntimeRecoveryPermit("recon_dispatch_0001", True, True)
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
        health=health,
    )
    machine.request_transition(
        RuntimeState.AUTO,
        authority=TransitionAuthority.RUNTIME,
        reason_code="AUTO_ACTIVATED",
        health=health,
        permit=permit,
    )
    return machine


def control_request(*, suffix: str = "00000001") -> LocalControlRequest:
    return LocalControlRequest(
        f"cmd_dispatch_{suffix}",
        LocalControlAction.PAUSE_BUY,
        "operator_demo",
        "OPERATOR_REQUEST",
        ShanghaiTimestamp.parse("2026-09-04T10:00:00+08:00"),
        RuntimeState.AUTO,
        f"corr_dispatch_{suffix}",
    )


def submission_service(
    *,
    reader: StubReceiptReader | None = None,
    ingress: StubIngress | None = None,
    registry: LocalControlReceiptRegistry | None = None,
    clock: MutableClock | None = None,
) -> tuple[
    LocalControlSubmissionService,
    StubReceiptReader,
    StubIngress,
    LocalControlReceiptRegistry,
]:
    effective_reader = reader or StubReceiptReader()
    effective_ingress = ingress or StubIngress()
    effective_registry = registry or LocalControlReceiptRegistry(capacity=4)
    service = LocalControlSubmissionService(
        reader=effective_reader,
        ingress=effective_ingress,
        registry=effective_registry,
        clock=clock or MutableClock(),
    )
    return service, effective_reader, effective_ingress, effective_registry


def test_submission_reserves_receipt_and_uses_critical_operator_channel() -> None:
    service, reader, ingress, registry = submission_service()
    request = control_request()

    receipt = asyncio.run(service.submit(request))

    assert receipt.status is LocalControlReceiptStatus.ACCEPTED
    assert receipt.request == request
    assert reader.command_ids == [request.command_id]
    assert ingress.calls == [(request, RuntimeEventSource.OPERATOR, EventPriority.CRITICAL)]
    assert registry.get(request.command_id) == receipt


def test_same_in_memory_submission_is_idempotent_and_conflicting_content_is_rejected() -> None:
    service, _, ingress, _ = submission_service()
    request = control_request()

    first = asyncio.run(service.submit(request))
    second = asyncio.run(service.submit(request))

    assert second == first
    assert len(ingress.calls) == 1
    with pytest.raises(LocalControlSubmissionError) as captured:
        asyncio.run(service.submit(replace(request, reason_code="DIFFERENT_REASON")))
    assert captured.value.code == "LOCAL_CONTROL_IDEMPOTENCY_CONFLICT"
    assert captured.value.kind is LocalControlSubmissionFailureKind.CONFLICT


def test_in_memory_idempotent_retry_does_not_depend_on_audit_reader_availability() -> None:
    reader = StubReceiptReader()
    service, _, ingress, _ = submission_service(reader=reader)
    request = control_request()
    asyncio.run(service.submit(request))
    reader.failure = RuntimeError("temporarily unavailable")

    receipt = asyncio.run(service.submit(request))

    assert receipt.status is LocalControlReceiptStatus.ACCEPTED
    assert len(ingress.calls) == 1


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (PublishOutcome.OVERFLOW, "LOCAL_CONTROL_EVENT_QUEUE_FULL"),
        (PublishOutcome.CLOSED, "LOCAL_CONTROL_EVENT_LOOP_UNAVAILABLE"),
    ],
)
def test_unaccepted_publication_returns_stable_unavailable_error_and_releases_slot(
    outcome: PublishOutcome, code: str
) -> None:
    service, _, _, registry = submission_service(ingress=StubIngress(outcome=outcome))
    request = control_request()

    with pytest.raises(LocalControlSubmissionError) as captured:
        asyncio.run(service.submit(request))

    assert captured.value.code == code
    assert captured.value.kind is LocalControlSubmissionFailureKind.UNAVAILABLE
    assert registry.get(request.command_id) is None


def test_publication_exception_is_sanitized_and_releases_slot() -> None:
    service, _, _, registry = submission_service(
        ingress=StubIngress(failure=RuntimeError("internal queue detail"))
    )
    request = control_request()

    with pytest.raises(LocalControlSubmissionError) as captured:
        asyncio.run(service.submit(request))

    assert captured.value.code == "LOCAL_CONTROL_EVENT_LOOP_UNAVAILABLE"
    assert "internal queue detail" not in str(captured.value)
    assert registry.get(request.command_id) is None


def test_durable_terminal_receipt_is_returned_without_republication() -> None:
    request = control_request()
    decided_at = ShanghaiTimestamp.parse("2026-09-04T10:00:01+08:00")
    persisted = PersistedLocalControlReceipt(
        request=request,
        outcome=TransitionOutcome.APPLIED,
        decided_at=decided_at,
        previous_state=RuntimeState.AUTO,
        current_state=RuntimeState.PAUSE_BUY,
        transition_sequence=5,
        rejection_code=None,
        blockers=(),
    )
    service, _, ingress, _ = submission_service(reader=StubReceiptReader(persisted))

    receipt = asyncio.run(service.submit(request))

    assert receipt.status is LocalControlReceiptStatus.APPLIED
    assert receipt.updated_at == decided_at
    assert receipt.transition_sequence == 5
    assert ingress.calls == []


def test_durable_unresolved_request_requires_reconciliation() -> None:
    request = control_request()
    persisted = PersistedLocalControlReceipt(request, None, None, None, None, None, None, ())
    service, _, _, _ = submission_service(reader=StubReceiptReader(persisted))

    receipt = asyncio.run(service.get_receipt(request.command_id))

    assert receipt.status is LocalControlReceiptStatus.UNRESOLVED
    assert receipt.error_code == "LOCAL_CONTROL_UNRESOLVED"
    assert receipt.requires_reconciliation is True
    assert receipt.status.terminal is True


def test_durable_conflicting_request_is_rejected_without_publication() -> None:
    request = control_request()
    stored = replace(request, reason_code="STORED_REASON")
    persisted = PersistedLocalControlReceipt(stored, None, None, None, None, None, None, ())
    service, _, ingress, _ = submission_service(reader=StubReceiptReader(persisted))

    with pytest.raises(LocalControlSubmissionError) as captured:
        asyncio.run(service.submit(request))

    assert captured.value.kind is LocalControlSubmissionFailureKind.CONFLICT
    assert ingress.calls == []


def test_unknown_receipt_and_reader_failure_have_distinct_errors() -> None:
    service, _, _, _ = submission_service()
    with pytest.raises(LocalControlSubmissionError) as missing:
        asyncio.run(service.get_receipt(control_request().command_id))
    assert missing.value.kind is LocalControlSubmissionFailureKind.NOT_FOUND

    unavailable, _, _, _ = submission_service(
        reader=StubReceiptReader(failure=RuntimeError("database path"))
    )
    with pytest.raises(LocalControlSubmissionError) as failed:
        asyncio.run(unavailable.get_receipt(control_request().command_id))
    assert failed.value.code == "LOCAL_CONTROL_AUDIT_UNAVAILABLE"
    assert "database path" not in str(failed.value)


def test_registry_evicts_oldest_terminal_receipt_but_never_active_receipt() -> None:
    registry = LocalControlReceiptRegistry(capacity=1)
    clock = MutableClock()
    first = control_request(suffix="00000001")
    second = control_request(suffix="00000002")
    registry.reserve(first, accepted_at=clock.now())

    with pytest.raises(LocalControlSubmissionError) as full:
        registry.reserve(second, accepted_at=clock.now())
    assert full.value.code == "LOCAL_CONTROL_RECEIPT_CAPACITY_EXHAUSTED"

    registry.mark_failed(
        first,
        updated_at=clock.now(),
        error_code="LOCAL_CONTROL_TEST_FAILURE",
        current_state=RuntimeState.HALT,
        retryable=False,
        requires_reconciliation=True,
    )
    receipt, created = registry.reserve(second, accepted_at=clock.now())
    assert created is True
    assert receipt.request == second
    assert registry.get(first.command_id) is None


@pytest.mark.parametrize("failure", [None, RuntimeError("journal unavailable")])
def test_decision_handler_publishes_terminal_success_or_sanitized_failure(
    failure: BaseException | None,
) -> None:
    request = control_request()
    clock = MutableClock()
    registry = LocalControlReceiptRegistry(capacity=2)
    registry.reserve(request, accepted_at=clock.now())
    machine = ready_auto_machine()
    coordinator = LocalControlCoordinator(
        ScriptedCommitter(failure=failure),
        machine,
        node_id="node-demo-001",
        account_id="acct_demo_001",
        boot_id="boot_11111111111111111111111111111111",
        clock=clock,
        owner_guard=lambda: None,
        event_id_factory=lambda: "evt_22222222222222222222222222222222",
    )
    handler = LocalControlDecisionHandler(coordinator, registry, clock=clock)

    result = asyncio.run(handler.handle(request))
    receipt = registry.get(request.command_id)

    assert receipt is not None
    if failure is None:
        assert result is not None
        assert receipt.status is LocalControlReceiptStatus.APPLIED
        assert receipt.current_state is RuntimeState.PAUSE_BUY
    else:
        assert result is None
        assert receipt.status is LocalControlReceiptStatus.FAILED
        assert receipt.error_code == "LOCAL_CONTROL_REQUEST_AUDIT_FAILED"
        assert "journal unavailable" not in repr(receipt)


@pytest.mark.parametrize("value", ["bad", "cmd_short", "cmd_bad value_0001"])
def test_receipt_lookup_rejects_invalid_command_identity(value: str) -> None:
    service, _, _, _ = submission_service()
    with pytest.raises(ValueError, match="command_id"):
        asyncio.run(service.get_receipt(value))
