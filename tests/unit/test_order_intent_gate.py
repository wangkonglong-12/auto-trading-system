from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import cast

import pytest

from auto_trading_system.domain import Money, Price, Quantity, SecurityCode, ShanghaiTimestamp
from auto_trading_system.execution import (
    OrderExecutionPermissionDenied,
    OrderIntentGateError,
    OrderIntentGateResult,
    OrderIntentGateStatus,
    OrderIntentPersistenceGate,
    OrderIntentSubmission,
    OrderSubmissionUnresolvedError,
    RuntimeOrderExecutionGuard,
    UnresolvedSubmissionStage,
)
from auto_trading_system.gateways.broker import (
    BrokerNotStartedError,
    BrokerTimeoutError,
    OrderSide,
    SubmissionReceipt,
    SubmitOrder,
)
from auto_trading_system.journal import (
    AppendDomainEvent,
    BrokerOrderLink,
    CanonicalJsonDocument,
    CommitOutcome,
    IntentSide,
    JournalCommitResult,
    JournalTransaction,
    LocalJournalError,
    OrderIntentRecord,
    PersistedEvent,
    PutOrderIntent,
    UpsertBrokerOrderLink,
)
from auto_trading_system.protocol.canonical_json import canonical_json_bytes
from auto_trading_system.runtime import RuntimeState


@dataclass
class ScriptedCommitter:
    responses: list[object]
    calls: list[JournalTransaction] = field(default_factory=list)

    async def commit(self, transaction: JournalTransaction) -> JournalCommitResult:
        self.calls.append(transaction)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return cast(JournalCommitResult, response)


@dataclass
class RecordingSubmitter:
    response: object
    error: Exception | None = None
    calls: list[SubmitOrder] = field(default_factory=list)

    def submit_order(self, command: SubmitOrder) -> SubmissionReceipt:
        self.calls.append(command)
        if self.error is not None:
            raise self.error
        return cast(SubmissionReceipt, self.response)


@dataclass
class OwnerGuard:
    error: Exception | None = None
    calls: int = 0

    def __call__(self) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


@dataclass
class PermissionGuard:
    fail_on_call: int | None = None
    calls: int = 0

    def __call__(self, value: OrderIntentRecord) -> None:
        assert isinstance(value, OrderIntentRecord)
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise RuntimeError("execution permission denied")


def timestamp() -> ShanghaiTimestamp:
    return ShanghaiTimestamp.parse("2026-09-04T14:35:01+08:00")


def intent() -> OrderIntentRecord:
    return OrderIntentRecord(
        intent_id="intent_01K43TAIL000000001",
        client_order_id="ord_01K43TAIL000000001",
        plan_id="plan_01K43TAIL20260904001",
        account_id="acct_demo_001",
        trade_date=timestamp().trading_date,
        symbol=SecurityCode.parse("000001.SZ"),
        side=IntentSide.BUY,
        quantity=Quantity(100),
        limit_price=Price.parse("12.34"),
        max_notional=Money.parse("1500.00"),
        remark_token="ATS000000000000000000001",
        execution_policy="EXEC_BALANCED_V1",
        created_at=timestamp(),
    )


def submission() -> OrderIntentSubmission:
    return OrderIntentSubmission(
        intent(),
        event_id="evt_00000000000000000000000000000003",
        node_id="node-demo-001",
        boot_id="boot_11111111111111111111111111111111",
        correlation_id="corr_plan_20260904_0001",
        causation_id="risk_01K43TAIL000000001",
    )


def receipt() -> SubmissionReceipt:
    return SubmissionReceipt(41, timestamp())


def event_document(event_id: str) -> CanonicalJsonDocument:
    body = canonical_json_bytes({"test": "event"}, exclude=frozenset())
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    return CanonicalJsonDocument(event_id, digest, body)


def committed_intent(value: OrderIntentSubmission) -> JournalCommitResult:
    event = PersistedEvent(
        value.event_id,
        value.boot_id,
        1,
        event_document(value.event_id),
    )
    return JournalCommitResult(
        value.intent_transaction_id,
        CommitOutcome.COMMITTED,
        1,
        (event,),
    )


def committed_receipt(
    value: OrderIntentSubmission, broker_receipt: SubmissionReceipt
) -> JournalCommitResult:
    return JournalCommitResult(
        value.receipt_transaction_id(broker_receipt),
        CommitOutcome.COMMITTED,
        2,
    )


def make_gate(
    committer: ScriptedCommitter,
    broker: RecordingSubmitter,
    owner: OwnerGuard | None = None,
    execution_guard: Callable[[OrderIntentRecord], None] | None = None,
) -> OrderIntentPersistenceGate:
    guard = execution_guard if execution_guard is not None else (lambda _: None)
    return OrderIntentPersistenceGate(
        committer,
        broker,
        owner_guard=owner or OwnerGuard(),
        execution_guard=guard,
    )


def test_submission_derives_complete_event_command_and_stable_transactions() -> None:
    value = submission()
    intent_transaction = value.intent_transaction()
    broker_command = value.broker_command()
    broker_receipt = receipt()
    receipt_transaction = value.receipt_transaction(broker_receipt)

    assert intent_transaction.transaction_id == value.intent_transaction_id
    assert intent_transaction.transaction_id == submission().intent_transaction_id
    assert intent_transaction.operations[0] == PutOrderIntent(value.intent)
    assert isinstance(intent_transaction.operations[1], AppendDomainEvent)
    draft = intent_transaction.operations[1].draft
    assert draft.aggregate_id == value.intent.client_order_id
    assert draft.payload == canonical_json_bytes(value.event_payload(), exclude=frozenset())
    assert value.event_payload() == {
        "client_order_id": "ord_01K43TAIL000000001",
        "plan_id": "plan_01K43TAIL20260904001",
        "intent_id": "intent_01K43TAIL000000001",
        "symbol": "000001.SZ",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": 100,
        "limit_price": "12.34",
        "max_notional": "1500",
        "remark_token": "ATS000000000000000000001",
        "execution_policy": "EXEC_BALANCED_V1",
    }
    assert broker_command == SubmitOrder(
        "ord_01K43TAIL000000001",
        SecurityCode.parse("000001.SZ"),
        OrderSide.BUY,
        Quantity(100),
        Price.parse("12.34"),
        "ATS000000000000000000001",
    )
    assert receipt_transaction.transaction_id == value.receipt_transaction_id(broker_receipt)
    link_operation = receipt_transaction.operations[0]
    assert isinstance(link_operation, UpsertBrokerOrderLink)
    assert link_operation.link == BrokerOrderLink(
        value.intent.client_order_id,
        value.intent.remark_token,
        broker_receipt.submitted_at,
        request_seq=41,
    )


def test_submission_rejects_order_above_its_notional_cap_and_invalid_context() -> None:
    with pytest.raises(ValueError, match="max_notional"):
        replace(submission(), intent=replace(intent(), max_notional=Money.parse("1200")))
    with pytest.raises(ValueError, match="node_id"):
        replace(submission(), node_id="bad")
    with pytest.raises(ValueError, match="intent"):
        OrderIntentSubmission(
            object(),  # type: ignore[arg-type]
            submission().event_id,
            submission().node_id,
            submission().boot_id,
            submission().correlation_id,
        )
    with pytest.raises(ValueError, match="receipt"):
        submission().receipt_transaction_id(cast(SubmissionReceipt, object()))


def test_gate_requires_structural_ports_and_owner_guard() -> None:
    broker = RecordingSubmitter(receipt())
    committer = ScriptedCommitter([])

    with pytest.raises(ValueError, match="committer"):
        OrderIntentPersistenceGate(
            object(),  # type: ignore[arg-type]
            broker,
            owner_guard=OwnerGuard(),
            execution_guard=lambda _: None,
        )
    with pytest.raises(ValueError, match="broker"):
        OrderIntentPersistenceGate(
            committer,
            object(),  # type: ignore[arg-type]
            owner_guard=OwnerGuard(),
            execution_guard=lambda _: None,
        )
    with pytest.raises(ValueError, match="owner_guard"):
        OrderIntentPersistenceGate(
            committer,
            broker,
            owner_guard=None,  # type: ignore[arg-type]
            execution_guard=lambda _: None,
        )
    with pytest.raises(ValueError, match="execution_guard"):
        OrderIntentPersistenceGate(
            committer,
            broker,
            owner_guard=OwnerGuard(),
            execution_guard=None,  # type: ignore[arg-type]
        )


def test_new_intent_is_durable_before_one_broker_call_and_receipt_link() -> None:
    value = submission()
    broker_receipt = receipt()
    committer = ScriptedCommitter(
        [committed_intent(value), committed_receipt(value, broker_receipt)]
    )
    broker = RecordingSubmitter(broker_receipt)
    owner = OwnerGuard()

    result = asyncio.run(make_gate(committer, broker, owner).persist_and_submit(value))

    assert result.status is OrderIntentGateStatus.SUBMITTED
    assert result.submission_receipt == broker_receipt
    assert result.receipt_commit == committed_receipt(value, broker_receipt)
    assert result.requires_reconciliation is False
    assert owner.calls == 1
    assert broker.calls == [value.broker_command()]
    assert len(committer.calls) == 2
    assert isinstance(committer.calls[0].operations[0], PutOrderIntent)
    assert isinstance(committer.calls[1].operations[0], UpsertBrokerOrderLink)


@pytest.mark.parametrize(
    ("fail_on_call", "code", "commit_count", "requires_reconciliation"),
    [
        (1, "ORDER_EXECUTION_NOT_ALLOWED", 0, False),
        (2, "ORDER_EXECUTION_PERMISSION_REVOKED", 1, True),
    ],
)
def test_execution_permission_is_checked_before_and_after_intent_commit(
    fail_on_call: int,
    code: str,
    commit_count: int,
    requires_reconciliation: bool,
) -> None:
    value = submission()
    guard = PermissionGuard(fail_on_call)
    committer = ScriptedCommitter([committed_intent(value)])
    broker = RecordingSubmitter(receipt())

    with pytest.raises(OrderIntentGateError) as captured:
        asyncio.run(make_gate(committer, broker, execution_guard=guard).persist_and_submit(value))

    assert captured.value.code == code
    assert captured.value.requires_reconciliation is requires_reconciliation
    assert len(committer.calls) == commit_count
    assert broker.calls == []


@pytest.mark.parametrize(
    ("state", "side", "allowed"),
    [
        (RuntimeState.AUTO, IntentSide.BUY, True),
        (RuntimeState.AUTO, IntentSide.SELL, True),
        (RuntimeState.PAUSE_BUY, IntentSide.BUY, False),
        (RuntimeState.PAUSE_BUY, IntentSide.SELL, True),
        (RuntimeState.SELL_ONLY, IntentSide.BUY, False),
        (RuntimeState.SELL_ONLY, IntentSide.SELL, True),
        (RuntimeState.READY_PAUSED, IntentSide.SELL, False),
        (RuntimeState.MANUAL, IntentSide.BUY, False),
        (RuntimeState.HALT, IntentSide.SELL, False),
    ],
)
def test_runtime_execution_guard_enforces_side_specific_state_permission(
    state: RuntimeState, side: IntentSide, allowed: bool
) -> None:
    guard = RuntimeOrderExecutionGuard(lambda: state)
    current_intent = replace(intent(), side=side)

    if allowed:
        guard(current_intent)
    else:
        with pytest.raises(OrderExecutionPermissionDenied) as captured:
            guard(current_intent)
        assert captured.value.state is state
        assert captured.value.side is side


def test_persistence_failure_or_cancellation_never_calls_broker() -> None:
    failure = LocalJournalError("JOURNAL_BUSY", "busy", retryable=True)
    for response in (failure, asyncio.CancelledError()):
        committer = ScriptedCommitter([response])
        broker = RecordingSubmitter(receipt())
        expected = (
            asyncio.CancelledError
            if isinstance(response, asyncio.CancelledError)
            else type(failure)
        )

        with pytest.raises(expected):
            asyncio.run(make_gate(committer, broker).persist_and_submit(submission()))
        assert broker.calls == []
        assert len(committer.calls) == 1


def test_duplicate_intent_requires_reconciliation_without_broker_retry() -> None:
    value = submission()
    duplicate = replace(committed_intent(value), outcome=CommitOutcome.DUPLICATE)
    committer = ScriptedCommitter([duplicate])
    broker = RecordingSubmitter(receipt())

    result = asyncio.run(make_gate(committer, broker).persist_and_submit(value))

    assert result.status is OrderIntentGateStatus.PERSISTED_RECONCILIATION_REQUIRED
    assert result.requires_reconciliation is True
    assert result.submission_receipt is None
    assert broker.calls == []
    assert len(committer.calls) == 1


@pytest.mark.parametrize("malformation", ["wrong_type", "wrong_transaction", "missing_event"])
def test_invalid_intent_commit_result_fails_closed(malformation: str) -> None:
    value = submission()
    result: object
    if malformation == "wrong_type":
        result = object()
    elif malformation == "wrong_transaction":
        result = replace(committed_intent(value), transaction_id="txn_wrong_result_0001")
    else:
        result = replace(committed_intent(value), persisted_events=())
    committer = ScriptedCommitter([result])
    broker = RecordingSubmitter(receipt())

    with pytest.raises(OrderIntentGateError) as captured:
        asyncio.run(make_gate(committer, broker).persist_and_submit(value))

    assert captured.value.requires_reconciliation is True
    assert broker.calls == []


@pytest.mark.parametrize(
    ("error", "outcome_unknown"),
    [
        (BrokerNotStartedError(), False),
        (BrokerTimeoutError("submit", outcome_unknown=False), False),
        (BrokerTimeoutError("submit", outcome_unknown=True), True),
        (RuntimeError("adapter failed"), True),
    ],
)
def test_broker_failure_after_intent_commit_requires_reconciliation(
    error: Exception,
    outcome_unknown: bool,
) -> None:
    value = submission()
    committer = ScriptedCommitter([committed_intent(value)])
    broker = RecordingSubmitter(receipt(), error=error)

    with pytest.raises(OrderSubmissionUnresolvedError) as captured:
        asyncio.run(make_gate(committer, broker).persist_and_submit(value))

    unresolved = captured.value
    assert unresolved.stage is UnresolvedSubmissionStage.BROKER_SUBMISSION
    assert unresolved.broker_outcome_unknown is outcome_unknown
    assert unresolved.requires_reconciliation is True
    assert unresolved.__cause__ is error
    assert len(committer.calls) == 1
    assert len(broker.calls) == 1


def test_invalid_broker_receipt_is_treated_as_unknown_after_durable_intent() -> None:
    value = submission()
    broker = RecordingSubmitter(object())

    with pytest.raises(OrderSubmissionUnresolvedError) as captured:
        asyncio.run(
            make_gate(ScriptedCommitter([committed_intent(value)]), broker).persist_and_submit(
                value
            )
        )

    assert captured.value.stage is UnresolvedSubmissionStage.BROKER_SUBMISSION
    assert captured.value.broker_outcome_unknown is True
    assert isinstance(captured.value.__cause__, TypeError)


@pytest.mark.parametrize(
    "receipt_persistence_failure",
    [
        LocalJournalError("JOURNAL_BUSY", "busy", retryable=True),
        asyncio.CancelledError(),
        object(),
    ],
)
def test_receipt_link_failure_after_broker_call_is_never_reported_as_success(
    receipt_persistence_failure: object,
) -> None:
    value = submission()
    broker_receipt = receipt()
    committer = ScriptedCommitter([committed_intent(value), receipt_persistence_failure])
    broker = RecordingSubmitter(broker_receipt)

    with pytest.raises(OrderSubmissionUnresolvedError) as captured:
        asyncio.run(make_gate(committer, broker).persist_and_submit(value))

    unresolved = captured.value
    assert unresolved.stage is UnresolvedSubmissionStage.RECEIPT_PERSISTENCE
    assert unresolved.broker_outcome_unknown is False
    assert len(broker.calls) == 1
    assert len(committer.calls) == 2


def test_gate_result_invariants_reject_ambiguous_states() -> None:
    value = submission()
    broker_receipt = receipt()
    committed = committed_intent(value)
    duplicate = replace(committed, outcome=CommitOutcome.DUPLICATE)

    with pytest.raises(ValueError, match="new committed intent"):
        OrderIntentGateResult(
            OrderIntentGateStatus.SUBMITTED,
            duplicate,
            broker_receipt,
            committed_receipt(value, broker_receipt),
        )
    with pytest.raises(ValueError, match="durable broker receipt"):
        OrderIntentGateResult(OrderIntentGateStatus.SUBMITTED, committed)
    with pytest.raises(ValueError, match="duplicate intent"):
        OrderIntentGateResult(
            OrderIntentGateStatus.PERSISTED_RECONCILIATION_REQUIRED,
            committed,
        )
    with pytest.raises(ValueError, match="OrderIntentGateStatus"):
        OrderIntentGateResult(cast(OrderIntentGateStatus, "SUBMITTED"), committed)
    with pytest.raises(ValueError, match="JournalCommitResult"):
        OrderIntentGateResult(
            OrderIntentGateStatus.SUBMITTED,
            cast(JournalCommitResult, object()),
        )


def test_duplicate_receipt_link_commit_is_already_durable_success() -> None:
    value = submission()
    broker_receipt = receipt()
    duplicate_receipt = replace(
        committed_receipt(value, broker_receipt),
        outcome=CommitOutcome.DUPLICATE,
    )
    gate = make_gate(
        ScriptedCommitter([committed_intent(value), duplicate_receipt]),
        RecordingSubmitter(broker_receipt),
    )

    result = asyncio.run(gate.persist_and_submit(value))

    assert result.status is OrderIntentGateStatus.SUBMITTED
    assert result.receipt_commit == duplicate_receipt


def test_owner_guard_fails_before_persistence_and_broker_side_effects() -> None:
    committer = ScriptedCommitter([])
    broker = RecordingSubmitter(receipt())
    owner = OwnerGuard(error=RuntimeError("not owner"))

    with pytest.raises(RuntimeError, match="not owner"):
        asyncio.run(make_gate(committer, broker, owner).persist_and_submit(submission()))

    assert committer.calls == []
    assert broker.calls == []


def test_gate_rejects_untyped_submission_before_any_commit() -> None:
    committer = ScriptedCommitter([])
    broker = RecordingSubmitter(receipt())

    with pytest.raises(ValueError, match="submission"):
        asyncio.run(
            make_gate(committer, broker).persist_and_submit(object())  # type: ignore[arg-type]
        )

    assert committer.calls == []
    assert broker.calls == []
