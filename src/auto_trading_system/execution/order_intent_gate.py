"""Fail-closed boundary between durable order intent and broker submission."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from auto_trading_system.gateways.broker.errors import (
    BrokerNotStartedError,
    BrokerTimeoutError,
)
from auto_trading_system.gateways.broker.models import (
    OrderSide,
    SubmissionReceipt,
    SubmitOrder,
)
from auto_trading_system.journal import (
    AggregateType,
    AppendDomainEvent,
    BrokerOrderLink,
    CommitOutcome,
    EventDraft,
    EventSeverity,
    EventSource,
    EventType,
    JournalCommitResult,
    JournalTransaction,
    OrderIntentRecord,
    PutOrderIntent,
    UpsertBrokerOrderLink,
)
from auto_trading_system.protocol.canonical_json import canonical_json_bytes


@runtime_checkable
class OrderIntentCommitter(Protocol):
    """Minimum asynchronous persistence capability required by the gate."""

    async def commit(self, transaction: JournalTransaction) -> JournalCommitResult: ...


@runtime_checkable
class BrokerOrderSubmitter(Protocol):
    """Minimum broker capability exposed to the gate."""

    def submit_order(self, command: SubmitOrder) -> SubmissionReceipt: ...


OrderExecutionGuard = Callable[[OrderIntentRecord], None]


class OrderIntentGateStatus(StrEnum):
    SUBMITTED = "SUBMITTED"
    PERSISTED_RECONCILIATION_REQUIRED = "PERSISTED_RECONCILIATION_REQUIRED"


class UnresolvedSubmissionStage(StrEnum):
    BROKER_SUBMISSION = "BROKER_SUBMISSION"
    RECEIPT_PERSISTENCE = "RECEIPT_PERSISTENCE"


class OrderIntentGateError(RuntimeError):
    """A fail-closed gate invariant failed before broker submission was allowed."""

    def __init__(self, code: str, message: str, *, requires_reconciliation: bool) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.requires_reconciliation = requires_reconciliation


class OrderSubmissionUnresolvedError(RuntimeError):
    """An error occurred after the order intent became durable."""

    def __init__(
        self,
        *,
        stage: UnresolvedSubmissionStage,
        intent_id: str,
        client_order_id: str,
        broker_outcome_unknown: bool,
    ) -> None:
        super().__init__(
            "ORDER_SUBMISSION_RECONCILIATION_REQUIRED: "
            f"{stage.value} failed after intent {intent_id} became durable"
        )
        self.code = "ORDER_SUBMISSION_RECONCILIATION_REQUIRED"
        self.stage = stage
        self.intent_id = intent_id
        self.client_order_id = client_order_id
        self.broker_outcome_unknown = broker_outcome_unknown
        self.requires_reconciliation = True


@dataclass(frozen=True, slots=True)
class OrderIntentSubmission:
    """Stable IDs and context needed to persist and submit one order intent."""

    intent: OrderIntentRecord
    event_id: str
    node_id: str
    boot_id: str
    correlation_id: str
    causation_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.intent, OrderIntentRecord):
            raise ValueError("intent must be an OrderIntentRecord")
        proposed_notional = self.intent.limit_price.amount * self.intent.quantity.value
        if proposed_notional > self.intent.max_notional.amount:
            raise ValueError("order notional exceeds intent max_notional")
        self.event_draft()

    @property
    def intent_transaction_id(self) -> str:
        return _transaction_id(
            "intent",
            self.intent.account_id,
            self.intent.intent_id,
        )

    def receipt_transaction_id(self, receipt: SubmissionReceipt) -> str:
        if not isinstance(receipt, SubmissionReceipt):
            raise ValueError("receipt must be a SubmissionReceipt")
        return _transaction_id(
            "receipt",
            self.intent.account_id,
            self.intent.client_order_id,
            str(receipt.request_seq),
        )

    def event_payload(self) -> dict[str, str | int]:
        intent = self.intent
        return {
            "client_order_id": intent.client_order_id,
            "plan_id": intent.plan_id,
            "intent_id": intent.intent_id,
            "symbol": intent.symbol.to_wire(),
            "side": intent.side.value,
            "order_type": "LIMIT",
            "quantity": intent.quantity.to_wire(),
            "limit_price": intent.limit_price.to_wire(),
            "max_notional": intent.max_notional.to_wire(),
            "remark_token": intent.remark_token,
            "execution_policy": intent.execution_policy,
        }

    def event_draft(self) -> EventDraft:
        intent = self.intent
        return EventDraft(
            event_id=self.event_id,
            node_id=self.node_id,
            account_id=intent.account_id,
            boot_id=self.boot_id,
            correlation_id=self.correlation_id,
            causation_id=self.causation_id,
            source=EventSource.RUNTIME,
            severity=EventSeverity.INFO,
            event_type=EventType.ORDER_INTENT_CREATED,
            aggregate_type=AggregateType.ORDER,
            aggregate_id=intent.client_order_id,
            trade_date=intent.trade_date,
            occurred_at=intent.created_at,
            payload=canonical_json_bytes(self.event_payload(), exclude=frozenset()),
        )

    def intent_transaction(self) -> JournalTransaction:
        return JournalTransaction(
            self.intent_transaction_id,
            (PutOrderIntent(self.intent), AppendDomainEvent(self.event_draft())),
        )

    def broker_command(self) -> SubmitOrder:
        intent = self.intent
        return SubmitOrder(
            client_order_id=intent.client_order_id,
            symbol=intent.symbol,
            side=OrderSide(intent.side.value),
            quantity=intent.quantity,
            limit_price=intent.limit_price,
            remark_token=intent.remark_token,
        )

    def receipt_transaction(self, receipt: SubmissionReceipt) -> JournalTransaction:
        link = BrokerOrderLink(
            client_order_id=self.intent.client_order_id,
            remark_token=self.intent.remark_token,
            request_seq=receipt.request_seq,
            updated_at=receipt.submitted_at,
        )
        return JournalTransaction(
            self.receipt_transaction_id(receipt),
            (UpsertBrokerOrderLink(link),),
        )


@dataclass(frozen=True, slots=True)
class OrderIntentGateResult:
    status: OrderIntentGateStatus
    intent_commit: JournalCommitResult
    submission_receipt: SubmissionReceipt | None = None
    receipt_commit: JournalCommitResult | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, OrderIntentGateStatus):
            raise ValueError("status must be an OrderIntentGateStatus")
        if not isinstance(self.intent_commit, JournalCommitResult):
            raise ValueError("intent_commit must be a JournalCommitResult")
        if self.status is OrderIntentGateStatus.SUBMITTED:
            if self.intent_commit.outcome is not CommitOutcome.COMMITTED:
                raise ValueError("submitted result requires a new committed intent")
            if not isinstance(self.submission_receipt, SubmissionReceipt) or not isinstance(
                self.receipt_commit, JournalCommitResult
            ):
                raise ValueError("submitted result requires durable broker receipt linkage")
        elif (
            self.intent_commit.outcome is not CommitOutcome.DUPLICATE
            or self.submission_receipt is not None
            or self.receipt_commit is not None
        ):
            raise ValueError("reconciliation result requires a duplicate intent without submission")

    @property
    def requires_reconciliation(self) -> bool:
        return self.status is OrderIntentGateStatus.PERSISTED_RECONCILIATION_REQUIRED


class OrderIntentPersistenceGate:
    """Persist first, submit once, then durably link the broker request receipt."""

    def __init__(
        self,
        committer: OrderIntentCommitter,
        broker: BrokerOrderSubmitter,
        *,
        owner_guard: Callable[[], None],
        execution_guard: OrderExecutionGuard,
    ) -> None:
        if not isinstance(committer, OrderIntentCommitter):
            raise ValueError("committer must implement OrderIntentCommitter")
        if not isinstance(broker, BrokerOrderSubmitter):
            raise ValueError("broker must implement BrokerOrderSubmitter")
        if not callable(owner_guard):
            raise ValueError("owner_guard must be callable")
        if not callable(execution_guard):
            raise ValueError("execution_guard must be callable")
        self._committer = committer
        self._broker = broker
        self._owner_guard = owner_guard
        self._execution_guard = execution_guard

    async def persist_and_submit(self, submission: OrderIntentSubmission) -> OrderIntentGateResult:
        self._owner_guard()
        if not isinstance(submission, OrderIntentSubmission):
            raise ValueError("submission must be an OrderIntentSubmission")
        self._assert_execution_allowed(submission.intent, intent_is_durable=False)

        intent_transaction = submission.intent_transaction()
        intent_commit = await self._committer.commit(intent_transaction)
        self._validate_intent_commit(submission, intent_transaction, intent_commit)
        if intent_commit.outcome is CommitOutcome.DUPLICATE:
            return OrderIntentGateResult(
                OrderIntentGateStatus.PERSISTED_RECONCILIATION_REQUIRED,
                intent_commit,
            )
        self._assert_execution_allowed(submission.intent, intent_is_durable=True)

        try:
            receipt_value: object = self._broker.submit_order(submission.broker_command())
        except Exception as exc:
            outcome_unknown = not isinstance(exc, BrokerNotStartedError)
            if isinstance(exc, BrokerTimeoutError):
                outcome_unknown = exc.outcome_unknown
            raise OrderSubmissionUnresolvedError(
                stage=UnresolvedSubmissionStage.BROKER_SUBMISSION,
                intent_id=submission.intent.intent_id,
                client_order_id=submission.intent.client_order_id,
                broker_outcome_unknown=outcome_unknown,
            ) from exc
        if not isinstance(receipt_value, SubmissionReceipt):
            error = TypeError("broker returned an invalid SubmissionReceipt")
            raise OrderSubmissionUnresolvedError(
                stage=UnresolvedSubmissionStage.BROKER_SUBMISSION,
                intent_id=submission.intent.intent_id,
                client_order_id=submission.intent.client_order_id,
                broker_outcome_unknown=True,
            ) from error
        receipt = receipt_value

        receipt_transaction = submission.receipt_transaction(receipt)
        try:
            receipt_commit = await self._committer.commit(receipt_transaction)
            self._validate_receipt_commit(receipt_transaction, receipt_commit)
        except asyncio.CancelledError as exc:
            raise OrderSubmissionUnresolvedError(
                stage=UnresolvedSubmissionStage.RECEIPT_PERSISTENCE,
                intent_id=submission.intent.intent_id,
                client_order_id=submission.intent.client_order_id,
                broker_outcome_unknown=False,
            ) from exc
        except Exception as exc:
            raise OrderSubmissionUnresolvedError(
                stage=UnresolvedSubmissionStage.RECEIPT_PERSISTENCE,
                intent_id=submission.intent.intent_id,
                client_order_id=submission.intent.client_order_id,
                broker_outcome_unknown=False,
            ) from exc

        return OrderIntentGateResult(
            OrderIntentGateStatus.SUBMITTED,
            intent_commit,
            receipt,
            receipt_commit,
        )

    def _assert_execution_allowed(
        self,
        intent: OrderIntentRecord,
        *,
        intent_is_durable: bool,
    ) -> None:
        try:
            self._execution_guard(intent)
        except Exception as exc:
            code = (
                "ORDER_EXECUTION_PERMISSION_REVOKED"
                if intent_is_durable
                else "ORDER_EXECUTION_NOT_ALLOWED"
            )
            raise OrderIntentGateError(
                code,
                "runtime execution permission does not allow broker submission",
                requires_reconciliation=intent_is_durable,
            ) from exc

    @staticmethod
    def _validate_intent_commit(
        submission: OrderIntentSubmission,
        transaction: JournalTransaction,
        result: object,
    ) -> None:
        if not isinstance(result, JournalCommitResult) or result.transaction_id != (
            transaction.transaction_id
        ):
            raise OrderIntentGateError(
                "ORDER_INTENT_COMMIT_RESULT_INVALID",
                "persistence returned a result for an unexpected transaction",
                requires_reconciliation=True,
            )
        if result.outcome is CommitOutcome.COMMITTED and (
            len(result.persisted_events) != 1
            or result.persisted_events[0].event_id != submission.event_id
            or result.persisted_events[0].boot_id != submission.boot_id
        ):
            raise OrderIntentGateError(
                "ORDER_INTENT_EVENT_NOT_DURABLE",
                "committed intent did not return its expected durable event",
                requires_reconciliation=True,
            )

    @staticmethod
    def _validate_receipt_commit(transaction: JournalTransaction, result: object) -> None:
        if (
            not isinstance(result, JournalCommitResult)
            or result.transaction_id != transaction.transaction_id
            or result.persisted_events
        ):
            raise OrderIntentGateError(
                "BROKER_RECEIPT_COMMIT_RESULT_INVALID",
                "persistence returned an invalid broker receipt linkage result",
                requires_reconciliation=True,
            )


def _transaction_id(kind: str, *identity: str) -> str:
    digest = hashlib.sha256(("order-intent-gate:v1:" + ":".join(identity)).encode()).hexdigest()
    return f"txn_{kind}_{digest[:32]}"
