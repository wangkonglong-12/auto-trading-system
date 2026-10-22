"""Storage-neutral LocalJournal records and atomic transaction commands."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeAlias

from auto_trading_system.domain import (
    Money,
    Price,
    Quantity,
    SecurityCode,
    ShanghaiTimestamp,
    TradingDate,
)
from auto_trading_system.protocol.canonical_json import canonical_json_bytes

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_TRANSACTION_OPERATIONS = 500
MAX_STORED_SEQUENCE = 9_223_372_036_854_775_807

_CONTENT_HASH_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_DOCUMENT_ID_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{3,95}")
_TRANSACTION_ID_PATTERN = re.compile(r"txn_[A-Za-z0-9_-]{8,64}")
_PLAN_ID_PATTERN = re.compile(r"plan_[A-Za-z0-9_-]{8,64}")
_COMMAND_ID_PATTERN = re.compile(r"cmd_[A-Za-z0-9_-]{8,64}")
_INTENT_ID_PATTERN = re.compile(r"intent_[A-Za-z0-9_-]{8,64}")
_CLIENT_ORDER_ID_PATTERN = re.compile(r"ord_[A-Za-z0-9_-]{8,64}")
_EVENT_ID_PATTERN = re.compile(r"evt_[0-9a-f]{32}")
_BOOT_ID_PATTERN = re.compile(r"boot_[0-9a-f]{32}")
_CORRELATION_ID_PATTERN = re.compile(r"corr_[A-Za-z0-9_-]{8,64}")
_CAUSE_ID_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{7,95}")
_BROKER_REFERENCE_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_BROKER_TRADE_ID_PATTERN = re.compile(r"trade_[A-Za-z0-9_.:-]{4,80}")
_REMARK_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,24}")
_REASON_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
_NAMESPACE_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{2,63}")
_KEY_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{2,95}")
_ACCOUNT_ID_PATTERN = re.compile(r"acct_[A-Za-z0-9_-]{3,48}")
_NODE_ID_PATTERN = re.compile(r"node-[a-z0-9][a-z0-9-]{2,47}")


def _require_pattern(value: str, pattern: re.Pattern[str], field: str) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{field} has an invalid format")


def _require_positive_sequence(value: int, field: str) -> None:
    if type(value) is not int or not 1 <= value <= MAX_STORED_SEQUENCE:
        raise ValueError(f"{field} must be a positive 64-bit integer")


def _parse_canonical_object(value: bytes, field: str) -> dict[str, Any]:
    if type(value) is not bytes:
        raise ValueError(f"{field} must be immutable bytes")
    if not value or len(value) > MAX_DOCUMENT_BYTES:
        raise ValueError(f"{field} size is outside the supported range")
    try:
        parsed = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field} must contain UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field} must contain a JSON object")
    try:
        canonical = canonical_json_bytes(parsed, exclude=frozenset())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} contains unsupported JSON values") from exc
    if canonical != value:
        raise ValueError(f"{field} must use canonical JSON encoding")
    return parsed


@dataclass(frozen=True, slots=True)
class CanonicalJsonDocument:
    document_id: str
    content_hash: str
    body: bytes

    def __post_init__(self) -> None:
        _require_pattern(self.document_id, _DOCUMENT_ID_PATTERN, "document_id")
        _require_pattern(self.content_hash, _CONTENT_HASH_PATTERN, "content_hash")
        _parse_canonical_object(self.body, "body")

    @classmethod
    def from_mapping(
        cls,
        document_id: str,
        content_hash: str,
        payload: Mapping[str, Any],
    ) -> CanonicalJsonDocument:
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be a mapping")
        return cls(
            document_id=document_id,
            content_hash=content_hash,
            body=canonical_json_bytes(payload, exclude=frozenset()),
        )

    def to_mapping(self) -> dict[str, Any]:
        return _parse_canonical_object(self.body, "body")


class IntentSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True, slots=True)
class OrderIntentRecord:
    intent_id: str
    client_order_id: str
    plan_id: str
    account_id: str
    trade_date: TradingDate
    symbol: SecurityCode
    side: IntentSide
    quantity: Quantity
    limit_price: Price
    max_notional: Money
    remark_token: str
    execution_policy: str
    created_at: ShanghaiTimestamp

    def __post_init__(self) -> None:
        _require_pattern(self.intent_id, _INTENT_ID_PATTERN, "intent_id")
        _require_pattern(self.client_order_id, _CLIENT_ORDER_ID_PATTERN, "client_order_id")
        _require_pattern(self.plan_id, _PLAN_ID_PATTERN, "plan_id")
        _require_pattern(self.account_id, _ACCOUNT_ID_PATTERN, "account_id")
        if not isinstance(self.trade_date, TradingDate):
            raise ValueError("trade_date must be a TradingDate")
        if not isinstance(self.symbol, SecurityCode):
            raise ValueError("symbol must be a SecurityCode")
        if not isinstance(self.side, IntentSide):
            raise ValueError("side must be an IntentSide")
        if not isinstance(self.quantity, Quantity) or self.quantity.is_zero:
            raise ValueError("quantity must be a positive Quantity")
        if not isinstance(self.limit_price, Price):
            raise ValueError("limit_price must be a Price")
        if not isinstance(self.max_notional, Money) or self.max_notional.amount <= 0:
            raise ValueError("max_notional must be positive Money")
        _require_pattern(self.remark_token, _REMARK_PATTERN, "remark_token")
        _require_pattern(self.execution_policy, _REASON_PATTERN, "execution_policy")
        if not isinstance(self.created_at, ShanghaiTimestamp):
            raise ValueError("created_at must be a ShanghaiTimestamp")
        if self.created_at.trading_date != self.trade_date:
            raise ValueError("created_at must belong to trade_date")


@dataclass(frozen=True, slots=True)
class BrokerOrderLink:
    client_order_id: str
    remark_token: str
    updated_at: ShanghaiTimestamp
    request_seq: int | None = None
    broker_order_id: str | None = None
    broker_order_sysid: str | None = None

    def __post_init__(self) -> None:
        _require_pattern(self.client_order_id, _CLIENT_ORDER_ID_PATTERN, "client_order_id")
        _require_pattern(self.remark_token, _REMARK_PATTERN, "remark_token")
        if self.request_seq is not None:
            _require_positive_sequence(self.request_seq, "request_seq")
        for field, value in (
            ("broker_order_id", self.broker_order_id),
            ("broker_order_sysid", self.broker_order_sysid),
        ):
            if value is not None:
                _require_pattern(value, _BROKER_REFERENCE_PATTERN, field)
        if (
            self.request_seq is None
            and self.broker_order_id is None
            and self.broker_order_sysid is None
        ):
            raise ValueError("order link requires at least one broker reference")
        if not isinstance(self.updated_at, ShanghaiTimestamp):
            raise ValueError("updated_at must be a ShanghaiTimestamp")


@dataclass(frozen=True, slots=True)
class BrokerTradeRecord:
    trade_id: str
    client_order_id: str
    account_id: str
    traded_at: ShanghaiTimestamp
    document: CanonicalJsonDocument

    def __post_init__(self) -> None:
        _require_pattern(self.trade_id, _BROKER_TRADE_ID_PATTERN, "trade_id")
        _require_pattern(self.client_order_id, _CLIENT_ORDER_ID_PATTERN, "client_order_id")
        _require_pattern(self.account_id, _ACCOUNT_ID_PATTERN, "account_id")
        if not isinstance(self.traded_at, ShanghaiTimestamp):
            raise ValueError("traded_at must be a ShanghaiTimestamp")
        if not isinstance(self.document, CanonicalJsonDocument):
            raise ValueError("document must be a CanonicalJsonDocument")
        if self.document.document_id != self.trade_id:
            raise ValueError("trade document id must equal trade_id")


class EventSource(StrEnum):
    RUNTIME = "RUNTIME"
    BROKER_CALLBACK = "BROKER_CALLBACK"
    BROKER_QUERY = "BROKER_QUERY"
    MARKET_DATA = "MARKET_DATA"
    RISK_ENGINE = "RISK_ENGINE"
    RECONCILIATION = "RECONCILIATION"
    OPERATOR = "OPERATOR"
    CENTRAL_COMMAND = "CENTRAL_COMMAND"
    WATCHDOG = "WATCHDOG"


class EventSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class EventType(StrEnum):
    RUNTIME_STATE_CHANGED = "RUNTIME_STATE_CHANGED"
    PLAN_STATUS_CHANGED = "PLAN_STATUS_CHANGED"
    ORDER_INTENT_CREATED = "ORDER_INTENT_CREATED"
    ORDER_STATUS_CHANGED = "ORDER_STATUS_CHANGED"
    TRADE_RECORDED = "TRADE_RECORDED"
    RISK_DECISION = "RISK_DECISION"
    CONTROL_COMMAND_APPLIED = "CONTROL_COMMAND_APPLIED"
    RECONCILIATION_DISCREPANCY = "RECONCILIATION_DISCREPANCY"
    ALERT_RAISED = "ALERT_RAISED"


class AggregateType(StrEnum):
    RUNTIME = "RUNTIME"
    PLAN = "PLAN"
    ORDER = "ORDER"
    TRADE = "TRADE"
    RISK = "RISK"
    CONTROL = "CONTROL"
    RECONCILIATION = "RECONCILIATION"
    ALERT = "ALERT"


_EVENT_AGGREGATE_TYPES = {
    EventType.RUNTIME_STATE_CHANGED: AggregateType.RUNTIME,
    EventType.PLAN_STATUS_CHANGED: AggregateType.PLAN,
    EventType.ORDER_INTENT_CREATED: AggregateType.ORDER,
    EventType.ORDER_STATUS_CHANGED: AggregateType.ORDER,
    EventType.TRADE_RECORDED: AggregateType.TRADE,
    EventType.RISK_DECISION: AggregateType.RISK,
    EventType.CONTROL_COMMAND_APPLIED: AggregateType.CONTROL,
    EventType.RECONCILIATION_DISCREPANCY: AggregateType.RECONCILIATION,
    EventType.ALERT_RAISED: AggregateType.ALERT,
}


@dataclass(frozen=True, slots=True)
class EventDraft:
    event_id: str
    node_id: str
    account_id: str
    boot_id: str
    correlation_id: str
    causation_id: str | None
    source: EventSource
    severity: EventSeverity
    event_type: EventType
    aggregate_type: AggregateType
    aggregate_id: str
    trade_date: TradingDate
    occurred_at: ShanghaiTimestamp
    payload: bytes

    def __post_init__(self) -> None:
        _require_pattern(self.event_id, _EVENT_ID_PATTERN, "event_id")
        _require_pattern(self.node_id, _NODE_ID_PATTERN, "node_id")
        _require_pattern(self.account_id, _ACCOUNT_ID_PATTERN, "account_id")
        _require_pattern(self.boot_id, _BOOT_ID_PATTERN, "boot_id")
        _require_pattern(self.correlation_id, _CORRELATION_ID_PATTERN, "correlation_id")
        if self.causation_id is not None:
            _require_pattern(self.causation_id, _CAUSE_ID_PATTERN, "causation_id")
        if not isinstance(self.source, EventSource):
            raise ValueError("source must be an EventSource")
        if not isinstance(self.severity, EventSeverity):
            raise ValueError("severity must be an EventSeverity")
        if not isinstance(self.event_type, EventType):
            raise ValueError("event_type must be an EventType")
        if not isinstance(self.aggregate_type, AggregateType):
            raise ValueError("aggregate_type must be an AggregateType")
        if _EVENT_AGGREGATE_TYPES[self.event_type] is not self.aggregate_type:
            raise ValueError("aggregate_type does not match event_type")
        _require_pattern(self.aggregate_id, _DOCUMENT_ID_PATTERN, "aggregate_id")
        if not isinstance(self.trade_date, TradingDate):
            raise ValueError("trade_date must be a TradingDate")
        if not isinstance(self.occurred_at, ShanghaiTimestamp):
            raise ValueError("occurred_at must be a ShanghaiTimestamp")
        if self.occurred_at.trading_date != self.trade_date:
            raise ValueError("occurred_at must belong to trade_date")
        _parse_canonical_object(self.payload, "payload")


class OutboxKind(StrEnum):
    DOMAIN_EVENT = "DOMAIN_EVENT"
    ASSET_SNAPSHOT = "ASSET_SNAPSHOT"
    POSITION_SNAPSHOT = "POSITION_SNAPSHOT"
    RECONCILIATION = "RECONCILIATION"
    COMMAND_RESULT = "COMMAND_RESULT"


@dataclass(frozen=True, slots=True)
class LocalControlRecord:
    control_id: str
    account_id: str
    reason_code: str
    created_at: ShanghaiTimestamp
    document: CanonicalJsonDocument

    def __post_init__(self) -> None:
        _require_pattern(self.control_id, _DOCUMENT_ID_PATTERN, "control_id")
        _require_pattern(self.account_id, _ACCOUNT_ID_PATTERN, "account_id")
        _require_pattern(self.reason_code, _REASON_PATTERN, "reason_code")
        if not isinstance(self.created_at, ShanghaiTimestamp):
            raise ValueError("created_at must be a ShanghaiTimestamp")
        if not isinstance(self.document, CanonicalJsonDocument):
            raise ValueError("document must be a CanonicalJsonDocument")
        if self.document.document_id != self.control_id:
            raise ValueError("control document id must equal control_id")


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    namespace: str
    key: str
    value: bytes
    revision: int

    def __post_init__(self) -> None:
        _require_pattern(self.namespace, _NAMESPACE_PATTERN, "namespace")
        _require_pattern(self.key, _KEY_PATTERN, "key")
        if type(self.value) is not bytes or len(self.value) > MAX_DOCUMENT_BYTES:
            raise ValueError("checkpoint value must be bounded immutable bytes")
        _require_positive_sequence(self.revision, "revision")


@dataclass(frozen=True, slots=True)
class CacheTradePlan:
    plan_id: str
    document: CanonicalJsonDocument

    def __post_init__(self) -> None:
        _require_pattern(self.plan_id, _PLAN_ID_PATTERN, "plan_id")
        if not isinstance(self.document, CanonicalJsonDocument):
            raise ValueError("document must be a CanonicalJsonDocument")
        if self.document.document_id != self.plan_id:
            raise ValueError("plan document id must equal plan_id")


@dataclass(frozen=True, slots=True)
class StoreCommand:
    command_id: str
    command_seq: int
    document: CanonicalJsonDocument

    def __post_init__(self) -> None:
        _require_pattern(self.command_id, _COMMAND_ID_PATTERN, "command_id")
        _require_positive_sequence(self.command_seq, "command_seq")
        if not isinstance(self.document, CanonicalJsonDocument):
            raise ValueError("document must be a CanonicalJsonDocument")
        if self.document.document_id != self.command_id:
            raise ValueError("command document id must equal command_id")


@dataclass(frozen=True, slots=True)
class PutOrderIntent:
    intent: OrderIntentRecord

    def __post_init__(self) -> None:
        if not isinstance(self.intent, OrderIntentRecord):
            raise ValueError("intent must be an OrderIntentRecord")


@dataclass(frozen=True, slots=True)
class UpsertBrokerOrderLink:
    link: BrokerOrderLink

    def __post_init__(self) -> None:
        if not isinstance(self.link, BrokerOrderLink):
            raise ValueError("link must be a BrokerOrderLink")


@dataclass(frozen=True, slots=True)
class RecordBrokerTrade:
    trade: BrokerTradeRecord

    def __post_init__(self) -> None:
        if not isinstance(self.trade, BrokerTradeRecord):
            raise ValueError("trade must be a BrokerTradeRecord")


@dataclass(frozen=True, slots=True)
class AppendDomainEvent:
    draft: EventDraft

    def __post_init__(self) -> None:
        if not isinstance(self.draft, EventDraft):
            raise ValueError("draft must be an EventDraft")


@dataclass(frozen=True, slots=True)
class EnqueueOutbox:
    kind: OutboxKind
    document: CanonicalJsonDocument

    def __post_init__(self) -> None:
        if not isinstance(self.kind, OutboxKind):
            raise ValueError("kind must be an OutboxKind")
        if self.kind is OutboxKind.DOMAIN_EVENT:
            raise ValueError("domain events use AppendDomainEvent for atomic event/outbox storage")
        if not isinstance(self.document, CanonicalJsonDocument):
            raise ValueError("document must be a CanonicalJsonDocument")


@dataclass(frozen=True, slots=True)
class RescheduleOutbox:
    outbox_id: int
    content_hash: str
    attempts: int
    available_at: ShanghaiTimestamp
    reason_code: str

    def __post_init__(self) -> None:
        _require_positive_sequence(self.outbox_id, "outbox_id")
        _require_pattern(self.content_hash, _CONTENT_HASH_PATTERN, "content_hash")
        if type(self.attempts) is not int or not 1 <= self.attempts <= 2_147_483_647:
            raise ValueError("attempts must be a positive 32-bit integer")
        if not isinstance(self.available_at, ShanghaiTimestamp):
            raise ValueError("available_at must be a ShanghaiTimestamp")
        _require_pattern(self.reason_code, _REASON_PATTERN, "reason_code")


@dataclass(frozen=True, slots=True)
class AcknowledgeOutbox:
    outbox_id: int
    content_hash: str

    def __post_init__(self) -> None:
        _require_positive_sequence(self.outbox_id, "outbox_id")
        _require_pattern(self.content_hash, _CONTENT_HASH_PATTERN, "content_hash")


@dataclass(frozen=True, slots=True)
class DeadLetterOutbox:
    outbox_id: int
    content_hash: str
    reason_code: str

    def __post_init__(self) -> None:
        _require_positive_sequence(self.outbox_id, "outbox_id")
        _require_pattern(self.content_hash, _CONTENT_HASH_PATTERN, "content_hash")
        _require_pattern(self.reason_code, _REASON_PATTERN, "reason_code")


@dataclass(frozen=True, slots=True)
class PutLocalControl:
    control: LocalControlRecord

    def __post_init__(self) -> None:
        if not isinstance(self.control, LocalControlRecord):
            raise ValueError("control must be a LocalControlRecord")


@dataclass(frozen=True, slots=True)
class PutCheckpoint:
    checkpoint: CheckpointRecord

    def __post_init__(self) -> None:
        if not isinstance(self.checkpoint, CheckpointRecord):
            raise ValueError("checkpoint must be a CheckpointRecord")


JournalOperation: TypeAlias = (
    CacheTradePlan
    | StoreCommand
    | PutOrderIntent
    | UpsertBrokerOrderLink
    | RecordBrokerTrade
    | AppendDomainEvent
    | EnqueueOutbox
    | RescheduleOutbox
    | AcknowledgeOutbox
    | DeadLetterOutbox
    | PutLocalControl
    | PutCheckpoint
)


@dataclass(frozen=True, slots=True)
class JournalTransaction:
    transaction_id: str
    operations: tuple[JournalOperation, ...]

    def __post_init__(self) -> None:
        _require_pattern(self.transaction_id, _TRANSACTION_ID_PATTERN, "transaction_id")
        if not isinstance(self.operations, tuple):
            raise ValueError("operations must be an immutable tuple")
        if not 1 <= len(self.operations) <= MAX_TRANSACTION_OPERATIONS:
            raise ValueError("operations must contain between 1 and 500 items")
        if not all(
            isinstance(
                operation,
                CacheTradePlan
                | StoreCommand
                | PutOrderIntent
                | UpsertBrokerOrderLink
                | RecordBrokerTrade
                | AppendDomainEvent
                | EnqueueOutbox
                | RescheduleOutbox
                | AcknowledgeOutbox
                | DeadLetterOutbox
                | PutLocalControl
                | PutCheckpoint,
            )
            for operation in self.operations
        ):
            raise ValueError("operations contain an unsupported command")


class CommitOutcome(StrEnum):
    COMMITTED = "COMMITTED"
    DUPLICATE = "DUPLICATE"


@dataclass(frozen=True, slots=True)
class PersistedEvent:
    event_id: str
    boot_id: str
    local_seq: int
    document: CanonicalJsonDocument

    def __post_init__(self) -> None:
        _require_pattern(self.event_id, _EVENT_ID_PATTERN, "event_id")
        _require_pattern(self.boot_id, _BOOT_ID_PATTERN, "boot_id")
        _require_positive_sequence(self.local_seq, "local_seq")
        if not isinstance(self.document, CanonicalJsonDocument):
            raise ValueError("document must be a CanonicalJsonDocument")
        if self.document.document_id != self.event_id:
            raise ValueError("event document id must equal event_id")


@dataclass(frozen=True, slots=True)
class JournalCommitResult:
    transaction_id: str
    outcome: CommitOutcome
    commit_sequence: int
    persisted_events: tuple[PersistedEvent, ...] = ()

    def __post_init__(self) -> None:
        _require_pattern(self.transaction_id, _TRANSACTION_ID_PATTERN, "transaction_id")
        if not isinstance(self.outcome, CommitOutcome):
            raise ValueError("outcome must be a CommitOutcome")
        _require_positive_sequence(self.commit_sequence, "commit_sequence")
        if not isinstance(self.persisted_events, tuple):
            raise ValueError("persisted_events must be an immutable tuple")
        if not all(isinstance(event, PersistedEvent) for event in self.persisted_events):
            raise ValueError("persisted_events must contain PersistedEvent values")


class OutboxState(StrEnum):
    PENDING = "PENDING"
    DEAD_LETTER = "DEAD_LETTER"


@dataclass(frozen=True, slots=True)
class OutboxItem:
    outbox_id: int
    kind: OutboxKind
    state: OutboxState
    document: CanonicalJsonDocument
    attempts: int
    available_at: ShanghaiTimestamp
    created_at: ShanghaiTimestamp

    def __post_init__(self) -> None:
        _require_positive_sequence(self.outbox_id, "outbox_id")
        if not isinstance(self.kind, OutboxKind):
            raise ValueError("kind must be an OutboxKind")
        if not isinstance(self.state, OutboxState):
            raise ValueError("state must be an OutboxState")
        if not isinstance(self.document, CanonicalJsonDocument):
            raise ValueError("document must be a CanonicalJsonDocument")
        if type(self.attempts) is not int or not 0 <= self.attempts <= 2_147_483_647:
            raise ValueError("attempts must be a non-negative 32-bit integer")
        if not isinstance(self.available_at, ShanghaiTimestamp) or not isinstance(
            self.created_at, ShanghaiTimestamp
        ):
            raise ValueError("outbox timestamps must be ShanghaiTimestamp values")
        if self.available_at < self.created_at:
            raise ValueError("available_at cannot precede created_at")


@dataclass(frozen=True, slots=True)
class JournalRecoverySnapshot:
    account_id: str
    trade_date: TradingDate
    plans: tuple[CanonicalJsonDocument, ...]
    commands: tuple[CanonicalJsonDocument, ...]
    order_intents: tuple[OrderIntentRecord, ...]
    broker_order_links: tuple[BrokerOrderLink, ...]
    broker_trades: tuple[BrokerTradeRecord, ...]
    controls: tuple[LocalControlRecord, ...]
    checkpoints: tuple[CheckpointRecord, ...]
    command_high_watermark: int

    def __post_init__(self) -> None:
        _require_pattern(self.account_id, _ACCOUNT_ID_PATTERN, "account_id")
        if not isinstance(self.trade_date, TradingDate):
            raise ValueError("trade_date must be a TradingDate")
        collections: tuple[tuple[object, ...], ...] = (
            self.plans,
            self.commands,
            self.order_intents,
            self.broker_order_links,
            self.broker_trades,
            self.controls,
            self.checkpoints,
        )
        if not all(isinstance(items, tuple) for items in collections):
            raise ValueError("recovery collections must be immutable tuples")
        expected_types = (
            CanonicalJsonDocument,
            CanonicalJsonDocument,
            OrderIntentRecord,
            BrokerOrderLink,
            BrokerTradeRecord,
            LocalControlRecord,
            CheckpointRecord,
        )
        if any(
            not all(isinstance(item, expected_type) for item in items)
            for items, expected_type in zip(collections, expected_types, strict=True)
        ):
            raise ValueError("recovery collections contain an invalid record")
        account_mismatch = any(
            intent.account_id != self.account_id for intent in self.order_intents
        )
        account_mismatch = account_mismatch or any(
            trade.account_id != self.account_id for trade in self.broker_trades
        )
        account_mismatch = account_mismatch or any(
            control.account_id != self.account_id for control in self.controls
        )
        if account_mismatch:
            raise ValueError("recovery record account does not match account_id")
        if any(intent.trade_date != self.trade_date for intent in self.order_intents):
            raise ValueError("order intent trade_date does not match recovery trade_date")
        if any(trade.traded_at.trading_date != self.trade_date for trade in self.broker_trades):
            raise ValueError("broker trade date does not match recovery trade_date")
        if type(self.command_high_watermark) is not int or not (
            0 <= self.command_high_watermark <= MAX_STORED_SEQUENCE
        ):
            raise ValueError("command_high_watermark must be a non-negative 64-bit integer")


class JournalHealthStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class JournalHealth:
    status: JournalHealthStatus
    writable: bool
    schema_version: int
    database_bytes: int
    pending_outbox: int
    last_commit_sequence: int
    reason_codes: tuple[str, ...] = ()
    wal_bytes: int = 0
    free_disk_bytes: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.status, JournalHealthStatus):
            raise ValueError("status must be a JournalHealthStatus")
        if type(self.writable) is not bool:
            raise ValueError("writable must be a boolean")
        for field, value in (
            ("schema_version", self.schema_version),
            ("database_bytes", self.database_bytes),
            ("pending_outbox", self.pending_outbox),
            ("last_commit_sequence", self.last_commit_sequence),
            ("wal_bytes", self.wal_bytes),
            ("free_disk_bytes", self.free_disk_bytes),
        ):
            if type(value) is not int or not 0 <= value <= MAX_STORED_SEQUENCE:
                raise ValueError(f"{field} must be a non-negative 64-bit integer")
        if not isinstance(self.reason_codes, tuple):
            raise ValueError("reason_codes must be an immutable tuple")
        if any(_REASON_PATTERN.fullmatch(code) is None for code in self.reason_codes):
            raise ValueError("reason_codes contain an invalid reason")
        if self.status is JournalHealthStatus.HEALTHY and (not self.writable or self.reason_codes):
            raise ValueError("healthy journal must be writable without reason codes")
        if self.status is JournalHealthStatus.UNAVAILABLE and self.writable:
            raise ValueError("unavailable journal cannot be writable")


class MaintenanceKind(StrEnum):
    CHECKPOINT = "CHECKPOINT"
    BACKUP = "BACKUP"
    INTEGRITY_CHECK = "INTEGRITY_CHECK"


@dataclass(frozen=True, slots=True)
class MaintenanceRequest:
    kind: MaintenanceKind
    destination: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, MaintenanceKind):
            raise ValueError("kind must be a MaintenanceKind")
        if self.kind is MaintenanceKind.BACKUP:
            if not isinstance(self.destination, Path) or not self.destination.is_absolute():
                raise ValueError("backup requires an absolute destination path")
        elif self.destination is not None:
            raise ValueError("only backup accepts a destination path")


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    kind: MaintenanceKind
    completed: bool
    bytes_written: int
    detail_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, MaintenanceKind):
            raise ValueError("kind must be a MaintenanceKind")
        if type(self.completed) is not bool:
            raise ValueError("completed must be a boolean")
        if (
            type(self.bytes_written) is not int
            or not 0 <= self.bytes_written <= MAX_STORED_SEQUENCE
        ):
            raise ValueError("bytes_written must be a non-negative 64-bit integer")
        _require_pattern(self.detail_code, _REASON_PATTERN, "detail_code")
