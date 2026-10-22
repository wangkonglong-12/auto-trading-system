from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from auto_trading_system.domain import (
    Money,
    Price,
    Quantity,
    SecurityCode,
    ShanghaiTimestamp,
    TradingDate,
)
from auto_trading_system.journal import (
    MAX_DOCUMENT_BYTES,
    AcknowledgeOutbox,
    AggregateType,
    AppendDomainEvent,
    BrokerOrderLink,
    BrokerTradeRecord,
    CacheTradePlan,
    CanonicalJsonDocument,
    CheckpointRecord,
    CommitOutcome,
    DeadLetterOutbox,
    EnqueueOutbox,
    EventDraft,
    EventSeverity,
    EventSource,
    EventType,
    IntentSide,
    JournalCommitResult,
    JournalConflictError,
    JournalHealth,
    JournalHealthStatus,
    JournalOpenResult,
    JournalRecoverySnapshot,
    JournalStatus,
    JournalTransaction,
    LocalControlRecord,
    LocalJournal,
    LocalJournalError,
    MaintenanceKind,
    MaintenanceRequest,
    MaintenanceResult,
    OrderIntentRecord,
    OutboxItem,
    OutboxKind,
    OutboxState,
    PersistedEvent,
    PutCheckpoint,
    PutLocalControl,
    PutOrderIntent,
    RecordBrokerTrade,
    RescheduleOutbox,
    StoreCommand,
    UpsertBrokerOrderLink,
)

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64


def timestamp(text: str = "2026-09-04T14:35:01+08:00") -> ShanghaiTimestamp:
    return ShanghaiTimestamp.parse(text)


def trading_date() -> TradingDate:
    return TradingDate(date(2026, 9, 4))


def document(
    document_id: str = "plan_demo_0001",
    *,
    content_hash: str = HASH_A,
    payload: dict[str, object] | None = None,
) -> CanonicalJsonDocument:
    return CanonicalJsonDocument.from_mapping(
        document_id,
        content_hash,
        payload or {"schema_version": "1.0", "value": 1},
    )


def order_intent() -> OrderIntentRecord:
    return OrderIntentRecord(
        intent_id="intent_demo_0001",
        client_order_id="ord_demo_0001",
        plan_id="plan_demo_0001",
        account_id="acct_demo_001",
        trade_date=trading_date(),
        symbol=SecurityCode.parse("000001.SZ"),
        side=IntentSide.BUY,
        quantity=Quantity(100),
        limit_price=Price(Decimal("12.34")),
        max_notional=Money(Decimal("1500")),
        remark_token="ATS000000000000000000001",
        execution_policy="EXEC_BALANCED_V1",
        created_at=timestamp(),
    )


def order_link() -> BrokerOrderLink:
    return BrokerOrderLink(
        client_order_id="ord_demo_0001",
        remark_token="ATS000000000000000000001",
        updated_at=timestamp(),
        request_seq=7,
        broker_order_id="broker.1001",
        broker_order_sysid="sys:2002",
    )


def broker_trade() -> BrokerTradeRecord:
    return BrokerTradeRecord(
        trade_id="trade_demo_0001",
        client_order_id="ord_demo_0001",
        account_id="acct_demo_001",
        traded_at=timestamp("2026-09-04T14:36:01+08:00"),
        document=document("trade_demo_0001"),
    )


def event_draft() -> EventDraft:
    return EventDraft(
        event_id="evt_00000000000000000000000000000001",
        node_id="node-demo-001",
        account_id="acct_demo_001",
        boot_id="boot_11111111111111111111111111111111",
        correlation_id="corr_runtime_0001",
        causation_id="intent_demo_0001",
        source=EventSource.RUNTIME,
        severity=EventSeverity.INFO,
        event_type=EventType.RUNTIME_STATE_CHANGED,
        aggregate_type=AggregateType.RUNTIME,
        aggregate_id="node-demo-001",
        trade_date=trading_date(),
        occurred_at=timestamp(),
        payload=b'{"new_state":"READY_PAUSED"}',
    )


def local_control() -> LocalControlRecord:
    return LocalControlRecord(
        control_id="control_demo_0001",
        account_id="acct_demo_001",
        reason_code="LOCAL_EMERGENCY_HALT",
        created_at=timestamp(),
        document=document("control_demo_0001"),
    )


def checkpoint() -> CheckpointRecord:
    return CheckpointRecord("runtime", "command_high_watermark", b"42", 1)


def persisted_event() -> PersistedEvent:
    return PersistedEvent(
        event_id="evt_00000000000000000000000000000001",
        boot_id="boot_11111111111111111111111111111111",
        local_seq=1,
        document=document("evt_00000000000000000000000000000001"),
    )


def outbox_item() -> OutboxItem:
    return OutboxItem(
        outbox_id=1,
        kind=OutboxKind.DOMAIN_EVENT,
        state=OutboxState.PENDING,
        document=document("evt_00000000000000000000000000000001"),
        attempts=0,
        available_at=timestamp("2026-09-04T14:35:02+08:00"),
        created_at=timestamp(),
    )


def recovery_snapshot() -> JournalRecoverySnapshot:
    return JournalRecoverySnapshot(
        account_id="acct_demo_001",
        trade_date=trading_date(),
        plans=(document(),),
        commands=(document("cmd_demo_0001"),),
        order_intents=(order_intent(),),
        broker_order_links=(order_link(),),
        broker_trades=(broker_trade(),),
        controls=(local_control(),),
        checkpoints=(checkpoint(),),
        command_high_watermark=42,
    )


def test_canonical_document_round_trips_immutable_sorted_utf8_json() -> None:
    value = document(payload={"中文": "值", "a": {"z": 2, "b": 1}})

    assert value.body == b'{"a":{"b":1,"z":2},"\xe4\xb8\xad\xe6\x96\x87":"\xe5\x80\xbc"}'
    assert value.to_mapping() == {"a": {"b": 1, "z": 2}, "中文": "值"}
    assert value.to_mapping() is not value.to_mapping()


@pytest.mark.parametrize(
    ("document_id", "content_hash", "body", "message"),
    [
        ("bad id", HASH_A, b"{}", "document_id"),
        ("plan_demo_0001", "sha256:BAD", b"{}", "content_hash"),
        ("plan_demo_0001", HASH_A, cast(bytes, "{}"), "immutable bytes"),
        ("plan_demo_0001", HASH_A, b"", "size"),
        ("plan_demo_0001", HASH_A, b"\xff", "UTF-8 JSON"),
        ("plan_demo_0001", HASH_A, b"[]", "JSON object"),
        ("plan_demo_0001", HASH_A, b'{"b":1, "a":2}', "canonical"),
        ("plan_demo_0001", HASH_A, b'{"value":NaN}', "unsupported JSON"),
    ],
)
def test_canonical_document_rejects_invalid_identity_hash_or_body(
    document_id: str, content_hash: str, body: bytes, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        CanonicalJsonDocument(document_id, content_hash, body)


def test_canonical_document_enforces_size_limit() -> None:
    oversized = b'{"value":"' + b"x" * MAX_DOCUMENT_BYTES + b'"}'
    with pytest.raises(ValueError, match="size"):
        CanonicalJsonDocument("plan_demo_0001", HASH_A, oversized)


def test_document_factory_requires_mapping() -> None:
    with pytest.raises(ValueError, match="mapping"):
        CanonicalJsonDocument.from_mapping("plan_demo_0001", HASH_A, cast(dict[str, object], []))


def test_valid_order_intent_preserves_domain_value_objects() -> None:
    intent = order_intent()

    assert intent.quantity == Quantity(100)
    assert intent.limit_price.to_wire() == "12.34"
    assert intent.max_notional.to_wire() == "1500"
    assert intent.created_at.trading_date == intent.trade_date


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("intent_id", "bad", "intent_id"),
        ("client_order_id", "bad", "client_order_id"),
        ("plan_id", "bad", "plan_id"),
        ("account_id", "bad", "account_id"),
        ("trade_date", "2026-09-04", "TradingDate"),
        ("symbol", "000001.SZ", "SecurityCode"),
        ("side", "BUY", "IntentSide"),
        ("quantity", Quantity(0), "positive Quantity"),
        ("quantity", 100, "positive Quantity"),
        ("limit_price", Decimal("12.34"), "Price"),
        ("max_notional", Money(Decimal("0")), "positive Money"),
        ("remark_token", "x" * 25, "remark_token"),
        ("execution_policy", "bad", "execution_policy"),
        ("created_at", "2026-09-04T14:35:01+08:00", "ShanghaiTimestamp"),
        ("created_at", timestamp("2026-09-05T14:35:01+08:00"), "trade_date"),
    ],
)
def test_order_intent_rejects_invalid_or_untyped_fields(
    field: str, value: object, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(order_intent(), **{field: value})  # type: ignore[arg-type]


def test_broker_order_link_accepts_each_reference_form() -> None:
    assert order_link().request_seq == 7
    assert (
        BrokerOrderLink(
            "ord_demo_0001",
            "remark1",
            timestamp(),
            broker_order_id="broker-1",
        ).broker_order_id
        == "broker-1"
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"request_seq": None, "broker_order_id": None, "broker_order_sysid": None},
            "at least one",
        ),
        ({"request_seq": 0}, "request_seq"),
        ({"broker_order_id": "bad value"}, "broker_order_id"),
        ({"broker_order_sysid": "bad value"}, "broker_order_sysid"),
        ({"updated_at": "now"}, "ShanghaiTimestamp"),
    ],
)
def test_broker_order_link_rejects_invalid_references(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(order_link(), **overrides)  # type: ignore[arg-type]


def test_broker_trade_requires_matching_document_identity() -> None:
    value = broker_trade()
    assert value.document.document_id == value.trade_id

    with pytest.raises(ValueError, match="equal trade_id"):
        replace(value, document=document("trade_other_0001"))
    with pytest.raises(ValueError, match="document"):
        replace(value, document=cast(CanonicalJsonDocument, b"{}"))


def test_event_draft_is_canonical_and_aggregate_bound() -> None:
    value = event_draft()
    assert value.aggregate_type is AggregateType.RUNTIME

    with pytest.raises(ValueError, match="does not match"):
        replace(value, aggregate_type=AggregateType.ORDER)
    with pytest.raises(ValueError, match="canonical"):
        replace(value, payload=b'{"z": 1}')
    with pytest.raises(ValueError, match="trade_date"):
        replace(value, occurred_at=timestamp("2026-09-05T14:35:01+08:00"))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("event_id", "bad", "event_id"),
        ("node_id", "bad", "node_id"),
        ("account_id", "bad", "account_id"),
        ("boot_id", "bad", "boot_id"),
        ("correlation_id", "bad", "correlation_id"),
        ("causation_id", "bad", "causation_id"),
        ("source", "RUNTIME", "EventSource"),
        ("severity", "INFO", "EventSeverity"),
        ("event_type", "RUNTIME_STATE_CHANGED", "EventType"),
        ("aggregate_type", "RUNTIME", "AggregateType"),
        ("aggregate_id", "bad", "aggregate_id"),
        ("trade_date", "2026-09-04", "TradingDate"),
        ("occurred_at", "now", "ShanghaiTimestamp"),
    ],
)
def test_event_draft_rejects_invalid_fields(field: str, value: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        replace(event_draft(), **{field: value})  # type: ignore[arg-type]


def test_storage_operations_keep_domain_records_typed() -> None:
    plan = CacheTradePlan("plan_demo_0001", document())
    command = StoreCommand("cmd_demo_0001", 42, document("cmd_demo_0001"))
    intent = PutOrderIntent(order_intent())
    link = UpsertBrokerOrderLink(order_link())
    trade = RecordBrokerTrade(broker_trade())
    event = AppendDomainEvent(event_draft())
    control = PutLocalControl(local_control())
    check = PutCheckpoint(checkpoint())

    assert (
        JournalTransaction(
            "txn_demo_0001", (plan, command, intent, link, trade, event, control, check)
        ).operations[-1]
        == check
    )


def test_document_operations_require_matching_identity() -> None:
    with pytest.raises(ValueError, match="plan document"):
        CacheTradePlan("plan_demo_0001", document("plan_other_0001"))
    with pytest.raises(ValueError, match="command document"):
        StoreCommand("cmd_demo_0001", 1, document("cmd_other_0001"))
    with pytest.raises(ValueError, match="control document"):
        replace(local_control(), document=document("control_other_0001"))


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: PutOrderIntent(cast(OrderIntentRecord, "bad")), "intent"),
        (lambda: UpsertBrokerOrderLink(cast(BrokerOrderLink, "bad")), "link"),
        (lambda: RecordBrokerTrade(cast(BrokerTradeRecord, "bad")), "trade"),
        (lambda: AppendDomainEvent(cast(EventDraft, "bad")), "draft"),
        (lambda: PutLocalControl(cast(LocalControlRecord, "bad")), "control"),
        (lambda: PutCheckpoint(cast(CheckpointRecord, "bad")), "checkpoint"),
    ],
)
def test_typed_operations_reject_untyped_records(factory: object, message: str) -> None:
    assert callable(factory)
    with pytest.raises(ValueError, match=message):
        factory()


def test_outbox_operations_separate_domain_event_atomic_path() -> None:
    snapshot_document = document("asset_demo_0001")
    queued = EnqueueOutbox(OutboxKind.ASSET_SNAPSHOT, snapshot_document)
    acknowledged = AcknowledgeOutbox(1, HASH_A)
    dead_letter = DeadLetterOutbox(1, HASH_A, "SCHEMA_REJECTED")

    assert queued.kind is OutboxKind.ASSET_SNAPSHOT
    assert acknowledged.outbox_id == dead_letter.outbox_id == 1
    with pytest.raises(ValueError, match="AppendDomainEvent"):
        EnqueueOutbox(OutboxKind.DOMAIN_EVENT, document("evt_00000000000000000000000000000001"))


def test_outbox_reschedule_requires_monotonic_retry_metadata() -> None:
    operation = RescheduleOutbox(
        1,
        HASH_A,
        2,
        timestamp("2026-09-04T14:36:01+08:00"),
        "NETWORK_TIMEOUT",
    )
    assert operation.attempts == 2
    assert JournalTransaction("txn_demo_0001", (operation,)).operations == (operation,)

    with pytest.raises(ValueError, match="attempts"):
        replace(operation, attempts=0)
    with pytest.raises(ValueError, match="available_at"):
        replace(operation, available_at=cast(ShanghaiTimestamp, "later"))
    with pytest.raises(ValueError, match="reason_code"):
        replace(operation, reason_code="bad")


@pytest.mark.parametrize(
    "operation",
    [
        lambda: AcknowledgeOutbox(0, HASH_A),
        lambda: AcknowledgeOutbox(1, "bad"),
        lambda: DeadLetterOutbox(0, HASH_A, "SCHEMA_REJECTED"),
        lambda: DeadLetterOutbox(1, HASH_A, "bad"),
    ],
)
def test_outbox_result_operations_validate_identity(operation: object) -> None:
    assert callable(operation)
    with pytest.raises(ValueError):
        operation()


def test_checkpoint_is_bounded_versioned_binary_state() -> None:
    assert checkpoint().value == b"42"
    with pytest.raises(ValueError, match="namespace"):
        CheckpointRecord("BAD", "key", b"", 1)
    with pytest.raises(ValueError, match="key"):
        CheckpointRecord("runtime", "x", b"", 1)
    with pytest.raises(ValueError, match="immutable bytes"):
        CheckpointRecord("runtime", "valid_key", cast(bytes, "42"), 1)
    with pytest.raises(ValueError, match="revision"):
        CheckpointRecord("runtime", "valid_key", b"42", 0)


def test_transaction_requires_id_typed_tuple_and_bounded_nonempty_operations() -> None:
    operation = CacheTradePlan("plan_demo_0001", document())
    assert JournalTransaction("txn_demo_0001", (operation,)).operations == (operation,)

    with pytest.raises(ValueError, match="transaction_id"):
        JournalTransaction("bad", (operation,))
    with pytest.raises(ValueError, match="immutable tuple"):
        JournalTransaction("txn_demo_0001", cast(tuple[CacheTradePlan, ...], [operation]))
    with pytest.raises(ValueError, match="between 1 and 500"):
        JournalTransaction("txn_demo_0001", ())
    with pytest.raises(ValueError, match="unsupported"):
        JournalTransaction("txn_demo_0001", (cast(object, "bad"),))  # type: ignore[arg-type]


def test_commit_result_and_persisted_event_enforce_sequence_and_identity() -> None:
    event = persisted_event()
    result = JournalCommitResult("txn_demo_0001", CommitOutcome.COMMITTED, 1, (event,))
    assert result.persisted_events == (event,)

    with pytest.raises(ValueError, match="event document"):
        replace(event, document=document("evt_00000000000000000000000000000002"))
    with pytest.raises(ValueError, match="commit_sequence"):
        replace(result, commit_sequence=0)
    with pytest.raises(ValueError, match="CommitOutcome"):
        replace(result, outcome=cast(CommitOutcome, "COMMITTED"))
    with pytest.raises(ValueError, match="immutable tuple"):
        replace(result, persisted_events=cast(tuple[PersistedEvent, ...], [event]))


def test_outbox_item_validates_state_attempts_and_time_order() -> None:
    value = outbox_item()
    assert value.state is OutboxState.PENDING

    with pytest.raises(ValueError, match="outbox_id"):
        replace(value, outbox_id=0)
    with pytest.raises(ValueError, match="OutboxKind"):
        replace(value, kind=cast(OutboxKind, "DOMAIN_EVENT"))
    with pytest.raises(ValueError, match="OutboxState"):
        replace(value, state=cast(OutboxState, "PENDING"))
    with pytest.raises(ValueError, match="attempts"):
        replace(value, attempts=-1)
    with pytest.raises(ValueError, match="timestamps"):
        replace(value, available_at=cast(ShanghaiTimestamp, "now"))
    with pytest.raises(ValueError, match="cannot precede"):
        replace(value, available_at=timestamp("2026-09-04T14:34:59+08:00"))


def test_recovery_snapshot_is_immutable_account_scoped_and_high_watermarked() -> None:
    value = recovery_snapshot()
    assert value.command_high_watermark == 42
    assert value.order_intents[0].account_id == value.account_id

    with pytest.raises(ValueError, match="immutable tuples"):
        replace(value, plans=cast(tuple[CanonicalJsonDocument, ...], [document()]))
    with pytest.raises(ValueError, match="invalid record"):
        replace(value, plans=(cast(CanonicalJsonDocument, "bad"),))
    with pytest.raises(ValueError, match="account does not match"):
        replace(value, order_intents=(replace(order_intent(), account_id="acct_other_001"),))
    with pytest.raises(ValueError, match="trade_date does not match"):
        replace(
            value,
            order_intents=(
                replace(
                    order_intent(),
                    trade_date=TradingDate.parse("2026-09-05"),
                    created_at=timestamp("2026-09-05T14:35:01+08:00"),
                ),
            ),
        )
    with pytest.raises(ValueError, match="broker trade date"):
        replace(
            value,
            broker_trades=(
                replace(
                    broker_trade(),
                    traded_at=timestamp("2026-09-05T14:36:01+08:00"),
                ),
            ),
        )
    with pytest.raises(ValueError, match="high_watermark"):
        replace(value, command_high_watermark=-1)


def test_journal_health_encodes_fail_closed_status_invariants() -> None:
    healthy = JournalHealth(JournalHealthStatus.HEALTHY, True, 1, 1024, 0, 7)
    degraded = JournalHealth(
        JournalHealthStatus.DEGRADED,
        False,
        1,
        1024,
        3,
        7,
        ("DISK_SPACE_LOW",),
    )
    assert healthy.writable is True
    assert degraded.reason_codes == ("DISK_SPACE_LOW",)

    with pytest.raises(ValueError, match="without reason codes"):
        replace(healthy, reason_codes=("DISK_SPACE_LOW",))
    with pytest.raises(ValueError, match="cannot be writable"):
        replace(healthy, status=JournalHealthStatus.UNAVAILABLE)
    with pytest.raises(ValueError, match="immutable tuple"):
        replace(degraded, reason_codes=cast(tuple[str, ...], ["DISK_SPACE_LOW"]))
    with pytest.raises(ValueError, match="invalid reason"):
        replace(degraded, reason_codes=("bad",))
    with pytest.raises(ValueError, match="non-negative"):
        replace(degraded, database_bytes=-1)
    with pytest.raises(ValueError, match="non-negative"):
        replace(degraded, wal_bytes=-1)
    with pytest.raises(ValueError, match="non-negative"):
        replace(degraded, free_disk_bytes=-1)


def test_maintenance_contract_is_storage_neutral_and_path_safe(tmp_path: Path) -> None:
    backup_path = (tmp_path / "journal.backup").resolve()
    backup = MaintenanceRequest(MaintenanceKind.BACKUP, backup_path)
    checkpoint_request = MaintenanceRequest(MaintenanceKind.CHECKPOINT)
    result = MaintenanceResult(MaintenanceKind.BACKUP, True, 2048, "BACKUP_COMPLETED")

    assert backup.destination == backup_path
    assert checkpoint_request.destination is None
    assert result.bytes_written == 2048
    with pytest.raises(ValueError, match="absolute"):
        MaintenanceRequest(MaintenanceKind.BACKUP, Path("relative.db"))
    with pytest.raises(ValueError, match="only backup"):
        MaintenanceRequest(MaintenanceKind.CHECKPOINT, backup_path)
    with pytest.raises(ValueError, match="bytes_written"):
        replace(result, bytes_written=-1)


def test_stable_journal_errors_do_not_expose_conflicting_identity() -> None:
    conflict = JournalConflictError("ORDER_INTENT", "intent_demo_0001")
    assert conflict.code == "JOURNAL_IDEMPOTENCY_CONFLICT"
    assert conflict.retryable is False
    assert "intent_demo_0001" not in str(conflict)
    assert conflict.record_id == "intent_demo_0001"

    temporary = LocalJournalError("JOURNAL_BUSY", "write temporarily unavailable", retryable=True)
    assert temporary.retryable is True
    with pytest.raises(ValueError, match="code"):
        LocalJournalError("bad", "message", retryable=True)
    with pytest.raises(ValueError, match="retryable"):
        LocalJournalError("JOURNAL_BUSY", "message", retryable=cast(bool, 1))
    with pytest.raises(ValueError, match="record_type"):
        JournalConflictError("bad", "intent_demo_0001")


def test_journal_open_result_is_strict() -> None:
    value = JournalOpenResult(1, "journal_" + "1" * 32, True)
    assert value.created is True
    with pytest.raises(ValueError, match="schema_version"):
        replace(value, schema_version=0)
    with pytest.raises(ValueError, match="journal_id"):
        replace(value, journal_id="bad")
    with pytest.raises(ValueError, match="created"):
        replace(value, created=cast(bool, 1))


class ContractJournal:
    def __init__(self) -> None:
        self._status = JournalStatus.NEW

    @property
    def status(self) -> JournalStatus:
        return self._status

    def open(self) -> JournalOpenResult:
        self._status = JournalStatus.OPEN
        return JournalOpenResult(1, "journal_" + "1" * 32, True)

    def commit(self, transaction: JournalTransaction) -> JournalCommitResult:
        return JournalCommitResult(transaction.transaction_id, CommitOutcome.COMMITTED, 1)

    def load_recovery(self, *, account_id: str, trade_date: TradingDate) -> JournalRecoverySnapshot:
        return replace(recovery_snapshot(), account_id=account_id, trade_date=trade_date)

    def fetch_outbox(
        self, *, limit: int, available_at: ShanghaiTimestamp
    ) -> tuple[OutboxItem, ...]:
        return (replace(outbox_item(), available_at=available_at),)[:limit]

    def health(self) -> JournalHealth:
        return JournalHealth(JournalHealthStatus.HEALTHY, True, 1, 1024, 1, 1)

    def maintain(self, request: MaintenanceRequest) -> MaintenanceResult:
        return MaintenanceResult(request.kind, True, 0, "MAINTENANCE_COMPLETED")

    def close(self) -> None:
        self._status = JournalStatus.CLOSED


def test_local_journal_protocol_is_structural_and_storage_implementation_neutral() -> None:
    journal = ContractJournal()

    assert isinstance(journal, LocalJournal)
    assert journal.open().schema_version == 1
    assert (
        journal.commit(
            JournalTransaction("txn_demo_0001", (CacheTradePlan("plan_demo_0001", document()),))
        ).outcome
        is CommitOutcome.COMMITTED
    )
    assert journal.fetch_outbox(limit=1, available_at=timestamp())
    assert journal.health().status is JournalHealthStatus.HEALTHY
    assert journal.maintain(MaintenanceRequest(MaintenanceKind.INTEGRITY_CHECK)).completed
    journal.close()
    assert journal.status is JournalStatus.CLOSED
