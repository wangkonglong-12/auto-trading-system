from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from threading import Thread

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
    JournalConflictError,
    JournalHealthStatus,
    JournalOperation,
    JournalStatus,
    JournalTransaction,
    LocalControlRecord,
    LocalJournal,
    LocalJournalError,
    MaintenanceKind,
    MaintenanceRequest,
    OrderIntentRecord,
    OutboxKind,
    PutCheckpoint,
    PutLocalControl,
    PutOrderIntent,
    RecordBrokerTrade,
    RescheduleOutbox,
    StoreCommand,
    UpsertBrokerOrderLink,
)
from auto_trading_system.journal.sqlite import (
    APPLICATION_ID,
    LATEST_SCHEMA_VERSION,
    SQLiteJournalSettings,
    SQLiteLocalJournal,
)
from auto_trading_system.protocol.canonical_json import canonical_json_bytes
from auto_trading_system.protocol.events import validate_domain_event_semantics


class FixedClock:
    def __init__(self, value: str = "2026-09-04T14:40:00+08:00") -> None:
        self.value = ShanghaiTimestamp.parse(value)

    def now(self) -> ShanghaiTimestamp:
        return self.value


def document(document_id: str, payload: dict[str, object]) -> CanonicalJsonDocument:
    body = canonical_json_bytes(payload, exclude=frozenset())
    content_hash = "sha256:" + hashlib.sha256(body).hexdigest()
    return CanonicalJsonDocument.from_mapping(document_id, content_hash, payload)


def plan_document(
    plan_id: str = "plan_demo_0001", *, account_id: str = "acct_demo_001"
) -> CanonicalJsonDocument:
    return document(
        plan_id,
        {
            "schema_version": "1.0",
            "plan_id": plan_id,
            "account_id": account_id,
            "trade_date": "2026-09-04",
            "status": "READY",
        },
    )


def command_document(
    command_id: str = "cmd_demo_0001",
    *,
    command_seq: int = 7,
    account_id: str = "acct_demo_001",
) -> CanonicalJsonDocument:
    return document(
        command_id,
        {
            "schema_version": "1.0",
            "command_id": command_id,
            "command_seq": command_seq,
            "account_id": account_id,
            "issued_at": "2026-09-04T14:39:00+08:00",
        },
    )


def intent(suffix: str = "0001", *, account_id: str = "acct_demo_001") -> OrderIntentRecord:
    return OrderIntentRecord(
        intent_id=f"intent_demo_{suffix}",
        client_order_id=f"ord_demo_{suffix}",
        plan_id="plan_demo_0001",
        account_id=account_id,
        trade_date=TradingDate.parse("2026-09-04"),
        symbol=SecurityCode.parse("000001.SZ"),
        side=IntentSide.BUY,
        quantity=Quantity(100),
        limit_price=Price(Decimal("12.34")),
        max_notional=Money(Decimal("1500")),
        remark_token=f"ATS{suffix:0>21}"[:24],
        execution_policy="EXEC_BALANCED_V1",
        created_at=ShanghaiTimestamp.parse("2026-09-04T14:35:01+08:00"),
    )


def event_draft(
    event_number: int = 1,
    *,
    boot_digit: str = "1",
) -> EventDraft:
    return EventDraft(
        event_id=f"evt_{event_number:032x}",
        node_id="node-demo-001",
        account_id="acct_demo_001",
        boot_id=f"boot_{boot_digit * 32}",
        correlation_id="corr_runtime_0001",
        causation_id="intent_demo_0001",
        source=EventSource.RUNTIME,
        severity=EventSeverity.INFO,
        event_type=EventType.RUNTIME_STATE_CHANGED,
        aggregate_type=AggregateType.RUNTIME,
        aggregate_id="node-demo-001",
        trade_date=TradingDate.parse("2026-09-04"),
        occurred_at=ShanghaiTimestamp.parse("2026-09-04T14:35:01+08:00"),
        payload=b'{"new_state":"READY_PAUSED"}',
    )


def journal_at(path: Path, **overrides: int) -> SQLiteLocalJournal:
    return SQLiteLocalJournal(
        SQLiteJournalSettings(path.resolve(), "acct_demo_001", minimum_free_bytes=0, **overrides),
        clock=FixedClock(),
    )


def test_settings_require_safe_absolute_bounded_values(tmp_path: Path) -> None:
    path = (tmp_path / "journal.db").resolve()
    assert SQLiteJournalSettings(path, "acct_demo_001").busy_timeout_ms == 3000

    with pytest.raises(ValueError, match="absolute"):
        SQLiteJournalSettings(Path("relative.db"), "acct_demo_001")
    with pytest.raises(ValueError, match="account_id"):
        SQLiteJournalSettings(path, "bad")
    with pytest.raises(ValueError, match="busy_timeout"):
        SQLiteJournalSettings(path, "acct_demo_001", busy_timeout_ms=0)
    with pytest.raises(ValueError, match="wal_autocheckpoint"):
        SQLiteJournalSettings(path, "acct_demo_001", wal_autocheckpoint_pages=0)
    with pytest.raises(ValueError, match="minimum_free_bytes"):
        SQLiteJournalSettings(path, "acct_demo_001", minimum_free_bytes=-1)
    with pytest.raises(ValueError, match="maximum_wal_bytes"):
        SQLiteJournalSettings(path, "acct_demo_001", maximum_wal_bytes=0)
    with pytest.raises(ValueError, match="maximum_pending_outbox"):
        SQLiteJournalSettings(path, "acct_demo_001", maximum_pending_outbox=0)


def test_open_creates_strict_schema_pragmas_and_stable_identity(tmp_path: Path) -> None:
    path = (tmp_path / "data" / "journal.db").resolve()
    journal = journal_at(path, wal_autocheckpoint_pages=37)
    assert isinstance(journal, LocalJournal)
    opened = journal.open()
    assert opened.created is True
    assert opened.schema_version == LATEST_SCHEMA_VERSION
    assert journal.status is JournalStatus.OPEN
    journal.close()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        assert connection.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
        }
        assert "schema_migrations" in tables
        assert "boot_sequences" in tables
        assert not any("tick" in table.lower() for table in tables)
        assert (
            connection.execute(
                "SELECT length(checksum) FROM schema_migrations WHERE version = 1"
            ).fetchone()[0]
            == 71
        )

    reopened = journal_at(path, wal_autocheckpoint_pages=37)
    second = reopened.open()
    assert second.created is False
    assert second.journal_id == opened.journal_id
    reopened.close()


def test_all_record_types_commit_and_recover_without_cross_scope_leakage(
    tmp_path: Path,
) -> None:
    journal = journal_at((tmp_path / "journal.db").resolve())
    journal.open()
    order_intent = intent()
    trade_doc = document("trade_demo_0001", {"price": "12.30", "quantity": 100})
    trade = BrokerTradeRecord(
        trade_id="trade_demo_0001",
        client_order_id=order_intent.client_order_id,
        account_id=order_intent.account_id,
        traded_at=ShanghaiTimestamp.parse("2026-09-04T14:36:01+08:00"),
        document=trade_doc,
    )
    control_doc = document("control_demo_0001", {"mode": "HALT"})
    snapshot_doc = document("snapshot_demo_0001", {"account_id": "acct_demo_001", "asset": "1000"})
    transaction = JournalTransaction(
        "txn_all_records_0001",
        (
            CacheTradePlan("plan_demo_0001", plan_document()),
            StoreCommand("cmd_demo_0001", 7, command_document()),
            PutOrderIntent(order_intent),
            UpsertBrokerOrderLink(
                BrokerOrderLink(
                    client_order_id=order_intent.client_order_id,
                    remark_token=order_intent.remark_token,
                    request_seq=11,
                    updated_at=ShanghaiTimestamp.parse("2026-09-04T14:35:02+08:00"),
                )
            ),
            RecordBrokerTrade(trade),
            AppendDomainEvent(event_draft()),
            EnqueueOutbox(OutboxKind.ASSET_SNAPSHOT, snapshot_doc),
            PutLocalControl(
                LocalControlRecord(
                    control_id="control_demo_0001",
                    account_id="acct_demo_001",
                    reason_code="LOCAL_EMERGENCY_HALT",
                    created_at=ShanghaiTimestamp.parse("2026-09-04T14:37:00+08:00"),
                    document=control_doc,
                )
            ),
            PutCheckpoint(CheckpointRecord("runtime", "command_high_watermark", b"7", 1)),
        ),
    )

    result = journal.commit(transaction)
    assert result.outcome is CommitOutcome.COMMITTED
    assert result.commit_sequence == 1
    assert len(result.persisted_events) == 1
    event = result.persisted_events[0].document.to_mapping()
    validate_domain_event_semantics(event)
    assert event["local_seq"] == 1

    duplicate = journal.commit(transaction)
    assert duplicate.outcome is CommitOutcome.DUPLICATE
    assert duplicate.commit_sequence == 1
    assert duplicate.persisted_events == result.persisted_events

    recovery = journal.load_recovery(
        account_id="acct_demo_001", trade_date=TradingDate.parse("2026-09-04")
    )
    assert recovery.plans == (plan_document(),)
    assert recovery.commands == (command_document(),)
    assert recovery.order_intents == (order_intent,)
    assert recovery.broker_order_links[0].request_seq == 11
    assert recovery.broker_trades == (trade,)
    assert recovery.controls[0].reason_code == "LOCAL_EMERGENCY_HALT"
    assert recovery.checkpoints == (CheckpointRecord("runtime", "command_high_watermark", b"7", 1),)
    assert recovery.command_high_watermark == 7

    with pytest.raises(LocalJournalError, match="ACCOUNT_MISMATCH"):
        journal.load_recovery(
            account_id="acct_other_001", trade_date=TradingDate.parse("2026-09-04")
        )

    outbox = journal.fetch_outbox(
        limit=10, available_at=ShanghaiTimestamp.parse("2026-09-04T15:00:00+08:00")
    )
    assert [item.kind for item in outbox] == [
        OutboxKind.DOMAIN_EVENT,
        OutboxKind.ASSET_SNAPSHOT,
    ]
    assert journal.health().last_commit_sequence == 1
    assert journal.health().pending_outbox == 2
    journal.close()


def test_failed_operation_rolls_back_every_write_and_sequence(tmp_path: Path) -> None:
    journal = journal_at((tmp_path / "journal.db").resolve())
    journal.open()
    with pytest.raises(LocalJournalError) as raised:
        journal.commit(
            JournalTransaction(
                "txn_rollback_0001",
                (
                    CacheTradePlan("plan_rollback_0001", plan_document("plan_rollback_0001")),
                    AcknowledgeOutbox(999, "sha256:" + "a" * 64),
                ),
            )
        )
    assert raised.value.code == "JOURNAL_OUTBOX_NOT_FOUND"
    recovery = journal.load_recovery(
        account_id="acct_demo_001", trade_date=TradingDate.parse("2026-09-04")
    )
    assert recovery.plans == ()
    result = journal.commit(
        JournalTransaction(
            "txn_after_rollback_0001",
            (CacheTradePlan("plan_demo_0001", plan_document()),),
        )
    )
    assert result.commit_sequence == 1
    journal.close()


def test_transaction_and_business_id_conflicts_are_fail_closed(tmp_path: Path) -> None:
    journal = journal_at((tmp_path / "journal.db").resolve())
    journal.open()
    original = JournalTransaction(
        "txn_conflict_0001",
        (CacheTradePlan("plan_demo_0001", plan_document()),),
    )
    journal.commit(original)

    changed_doc = document(
        "plan_demo_0001",
        {
            "plan_id": "plan_demo_0001",
            "account_id": "acct_demo_001",
            "trade_date": "2026-09-04",
            "status": "NO_TRADE",
        },
    )
    with pytest.raises(JournalConflictError):
        journal.commit(
            JournalTransaction(
                "txn_conflict_0001",
                (CacheTradePlan("plan_demo_0001", changed_doc),),
            )
        )
    with pytest.raises(JournalConflictError):
        journal.commit(
            JournalTransaction(
                "txn_conflict_0002",
                (CacheTradePlan("plan_demo_0001", changed_doc),),
            )
        )
    assert journal.status is JournalStatus.OPEN
    assert journal.health().last_commit_sequence == 1
    journal.close()


def test_document_scope_and_command_sequence_are_strict(tmp_path: Path) -> None:
    journal = journal_at((tmp_path / "journal.db").resolve())
    journal.open()
    malformed = document("plan_bad_scope_0001", {"plan_id": "plan_bad_scope_0001"})
    with pytest.raises(LocalJournalError) as raised:
        journal.commit(
            JournalTransaction(
                "txn_bad_scope_0001",
                (CacheTradePlan("plan_bad_scope_0001", malformed),),
            )
        )
    assert raised.value.code == "JOURNAL_DOCUMENT_SCOPE_INVALID"

    journal.commit(
        JournalTransaction(
            "txn_command_0001",
            (StoreCommand("cmd_demo_0001", 7, command_document()),),
        )
    )
    with pytest.raises(JournalConflictError) as sequence_conflict:
        journal.commit(
            JournalTransaction(
                "txn_command_0002",
                (
                    StoreCommand(
                        "cmd_demo_0002",
                        7,
                        command_document("cmd_demo_0002", command_seq=7),
                    ),
                ),
            )
        )
    assert sequence_conflict.value.record_type == "COMMAND_SEQUENCE"
    journal.close()


def test_document_identity_and_wire_time_mismatches_rollback_cleanly(tmp_path: Path) -> None:
    journal = journal_at((tmp_path / "journal.db").resolve())
    journal.open()
    cases: tuple[JournalOperation, ...] = (
        CacheTradePlan(
            "plan_bad_identity_0001",
            document(
                "plan_bad_identity_0001",
                {
                    "plan_id": "plan_other_identity_0001",
                    "account_id": "acct_demo_001",
                    "trade_date": "2026-09-04",
                },
            ),
        ),
        CacheTradePlan(
            "plan_bad_date_0001",
            document(
                "plan_bad_date_0001",
                {
                    "plan_id": "plan_bad_date_0001",
                    "account_id": "acct_demo_001",
                    "trade_date": "bad-date",
                },
            ),
        ),
        StoreCommand(
            "cmd_bad_sequence_0001",
            8,
            command_document("cmd_bad_sequence_0001", command_seq=7),
        ),
        StoreCommand(
            "cmd_bad_time_0001",
            9,
            document(
                "cmd_bad_time_0001",
                {
                    "command_id": "cmd_bad_time_0001",
                    "command_seq": 9,
                    "account_id": "acct_demo_001",
                    "issued_at": "bad-time",
                },
            ),
        ),
    )
    for index, operation in enumerate(cases, start=1):
        with pytest.raises(LocalJournalError, match="DOCUMENT_SCOPE_INVALID"):
            journal.commit(JournalTransaction(f"txn_bad_document_{index:04d}", (operation,)))
    assert journal.health().last_commit_sequence == 0
    journal.close()


def test_immutable_replays_are_noops_and_client_order_identity_is_unique(
    tmp_path: Path,
) -> None:
    journal = journal_at((tmp_path / "journal.db").resolve())
    journal.open()
    plan = CacheTradePlan("plan_demo_0001", plan_document())
    journal.commit(JournalTransaction("txn_plan_replay_0001", (plan,)))
    journal.commit(JournalTransaction("txn_plan_replay_0002", (plan,)))

    first_intent = intent()
    journal.commit(JournalTransaction("txn_client_order_0001", (PutOrderIntent(first_intent),)))
    with pytest.raises(JournalConflictError) as conflict:
        journal.commit(
            JournalTransaction(
                "txn_client_order_0002",
                (PutOrderIntent(replace(first_intent, intent_id="intent_other_0001")),),
            )
        )
    assert conflict.value.record_type == "CLIENT_ORDER"
    journal.close()


def test_broker_link_only_allows_monotonic_enrichment(tmp_path: Path) -> None:
    journal = journal_at((tmp_path / "journal.db").resolve())
    journal.open()
    order_intent = intent()
    journal.commit(JournalTransaction("txn_intent_0001", (PutOrderIntent(order_intent),)))
    first = BrokerOrderLink(
        client_order_id=order_intent.client_order_id,
        remark_token=order_intent.remark_token,
        request_seq=11,
        updated_at=ShanghaiTimestamp.parse("2026-09-04T14:35:03+08:00"),
    )
    enriched = BrokerOrderLink(
        client_order_id=order_intent.client_order_id,
        remark_token=order_intent.remark_token,
        broker_order_id="broker.1001",
        updated_at=ShanghaiTimestamp.parse("2026-09-04T14:35:02+08:00"),
    )
    journal.commit(JournalTransaction("txn_link_0001", (UpsertBrokerOrderLink(first),)))
    journal.commit(JournalTransaction("txn_link_0002", (UpsertBrokerOrderLink(enriched),)))
    recovered = journal.load_recovery(
        account_id="acct_demo_001", trade_date=TradingDate.parse("2026-09-04")
    ).broker_order_links[0]
    assert recovered.request_seq == 11
    assert recovered.broker_order_id == "broker.1001"
    assert recovered.updated_at == first.updated_at

    with pytest.raises(JournalConflictError):
        journal.commit(
            JournalTransaction(
                "txn_link_0003",
                (UpsertBrokerOrderLink(replace(enriched, broker_order_id="broker.9999")),),
            )
        )
    journal.close()


def test_event_sequences_are_per_boot_and_event_outbox_is_atomic(tmp_path: Path) -> None:
    journal = journal_at((tmp_path / "journal.db").resolve())
    journal.open()
    results = [
        journal.commit(
            JournalTransaction(
                f"txn_event_{number:04d}",
                (AppendDomainEvent(event_draft(number, boot_digit=boot)),),
            )
        )
        for number, boot in ((1, "1"), (2, "1"), (3, "2"))
    ]
    assert [result.persisted_events[0].local_seq for result in results] == [1, 2, 1]
    assert (
        len(
            journal.fetch_outbox(
                limit=10, available_at=ShanghaiTimestamp.parse("2026-09-04T15:00:00+08:00")
            )
        )
        == 3
    )

    with pytest.raises(JournalConflictError):
        journal.commit(
            JournalTransaction(
                "txn_event_conflict_0001",
                (AppendDomainEvent(replace(event_draft(1), payload=b'{"new_state":"AUTO"}')),),
            )
        )
    journal.close()


def test_duplicate_event_operation_is_returned_once_and_optional_causation_is_omitted(
    tmp_path: Path,
) -> None:
    journal = journal_at((tmp_path / "journal.db").resolve())
    journal.open()
    draft = replace(event_draft(), causation_id=None)
    result = journal.commit(
        JournalTransaction(
            "txn_duplicate_event_0001",
            (AppendDomainEvent(draft), AppendDomainEvent(draft)),
        )
    )
    assert len(result.persisted_events) == 1
    assert "causation_id" not in result.persisted_events[0].document.to_mapping()
    assert (
        len(
            journal.fetch_outbox(
                limit=10, available_at=ShanghaiTimestamp.parse("2026-09-04T15:00:00+08:00")
            )
        )
        == 1
    )
    journal.close()


def test_semantically_invalid_event_is_rejected_without_poisoning_journal(
    tmp_path: Path,
) -> None:
    journal = journal_at((tmp_path / "journal.db").resolve())
    journal.open()
    invalid = EventDraft(
        event_id="evt_00000000000000000000000000000009",
        node_id="node-demo-001",
        account_id="acct_demo_001",
        boot_id="boot_11111111111111111111111111111111",
        correlation_id="corr_runtime_0001",
        causation_id=None,
        source=EventSource.RUNTIME,
        severity=EventSeverity.INFO,
        event_type=EventType.PLAN_STATUS_CHANGED,
        aggregate_type=AggregateType.PLAN,
        aggregate_id="plan_demo_0001",
        trade_date=TradingDate.parse("2026-09-04"),
        occurred_at=ShanghaiTimestamp.parse("2026-09-04T14:35:01+08:00"),
        payload=b'{"plan_id":"plan_different_0001"}',
    )
    with pytest.raises(LocalJournalError) as raised:
        journal.commit(JournalTransaction("txn_invalid_event_0001", (AppendDomainEvent(invalid),)))
    assert raised.value.code == "JOURNAL_EVENT_INVALID"
    assert journal.status is JournalStatus.OPEN
    assert journal.health().last_commit_sequence == 0
    journal.close()


def test_outbox_reschedule_ack_and_dead_letter_are_hash_guarded(tmp_path: Path) -> None:
    journal = journal_at((tmp_path / "journal.db").resolve())
    journal.open()
    first_doc = document("snapshot_demo_0001", {"value": 1})
    second_doc = document("snapshot_demo_0002", {"value": 2})
    journal.commit(
        JournalTransaction(
            "txn_outbox_0001",
            (
                EnqueueOutbox(OutboxKind.ASSET_SNAPSHOT, first_doc),
                EnqueueOutbox(OutboxKind.POSITION_SNAPSHOT, second_doc),
            ),
        )
    )
    available = ShanghaiTimestamp.parse("2026-09-04T14:41:00+08:00")
    journal.commit(
        JournalTransaction(
            "txn_reschedule_0001",
            (RescheduleOutbox(1, first_doc.content_hash, 1, available, "NETWORK_TIMEOUT"),),
        )
    )
    pending = journal.fetch_outbox(limit=10, available_at=available)
    assert pending[0].outbox_id == 1
    assert pending[0].attempts == 1
    journal.commit(
        JournalTransaction(
            "txn_reschedule_retry_0001",
            (RescheduleOutbox(1, first_doc.content_hash, 1, available, "NETWORK_TIMEOUT"),),
        )
    )
    with pytest.raises(JournalConflictError):
        journal.commit(
            JournalTransaction(
                "txn_reschedule_regress_0001",
                (
                    RescheduleOutbox(
                        1,
                        first_doc.content_hash,
                        2,
                        ShanghaiTimestamp.parse("2026-09-04T14:40:30+08:00"),
                        "NETWORK_TIMEOUT",
                    ),
                ),
            )
        )

    journal.commit(
        JournalTransaction("txn_ack_0001", (AcknowledgeOutbox(1, first_doc.content_hash),))
    )
    journal.commit(
        JournalTransaction(
            "txn_dead_0001",
            (DeadLetterOutbox(2, second_doc.content_hash, "SCHEMA_REJECTED"),),
        )
    )
    assert journal.fetch_outbox(limit=10, available_at=available) == ()

    with pytest.raises(JournalConflictError):
        journal.commit(
            JournalTransaction(
                "txn_ack_bad_hash_0001",
                (AcknowledgeOutbox(1, "sha256:" + "f" * 64),),
            )
        )
    with pytest.raises(LocalJournalError, match="ALREADY_TERMINAL"):
        journal.commit(
            JournalTransaction(
                "txn_dead_after_ack_0001",
                (DeadLetterOutbox(1, first_doc.content_hash, "LATE_REJECTION"),),
            )
        )
    journal.close()


def test_outbox_identity_conflict_and_fetch_input_bounds(tmp_path: Path) -> None:
    journal = journal_at((tmp_path / "journal.db").resolve())
    journal.open()
    original = document("snapshot_demo_0001", {"value": 1})
    changed = document("snapshot_demo_0001", {"value": 2})
    journal.commit(
        JournalTransaction(
            "txn_outbox_identity_0001",
            (EnqueueOutbox(OutboxKind.ASSET_SNAPSHOT, original),),
        )
    )
    journal.commit(
        JournalTransaction(
            "txn_outbox_identity_replay_0001",
            (EnqueueOutbox(OutboxKind.ASSET_SNAPSHOT, original),),
        )
    )
    with pytest.raises(JournalConflictError):
        journal.commit(
            JournalTransaction(
                "txn_outbox_identity_0002",
                (EnqueueOutbox(OutboxKind.ASSET_SNAPSHOT, changed),),
            )
        )
    with pytest.raises(ValueError, match="limit"):
        journal.fetch_outbox(
            limit=0, available_at=ShanghaiTimestamp.parse("2026-09-04T15:00:00+08:00")
        )
    with pytest.raises(ValueError, match="available_at"):
        journal.fetch_outbox(limit=1, available_at="later")  # type: ignore[arg-type]
    journal.close()


def test_outbox_terminal_and_reschedule_commands_are_idempotent_and_state_safe(
    tmp_path: Path,
) -> None:
    journal = journal_at((tmp_path / "journal.db").resolve())
    journal.open()
    item = document("snapshot_demo_0001", {"value": 1})
    journal.commit(
        JournalTransaction(
            "txn_outbox_state_0001",
            (EnqueueOutbox(OutboxKind.ASSET_SNAPSHOT, item),),
        )
    )
    with pytest.raises(LocalJournalError, match="OUTBOX_NOT_FOUND"):
        journal.commit(
            JournalTransaction(
                "txn_reschedule_missing_0001",
                (
                    RescheduleOutbox(
                        999,
                        item.content_hash,
                        1,
                        ShanghaiTimestamp.parse("2026-09-04T14:41:00+08:00"),
                        "NETWORK_TIMEOUT",
                    ),
                ),
            )
        )
    with pytest.raises(JournalConflictError):
        journal.commit(
            JournalTransaction(
                "txn_reschedule_hash_0001",
                (
                    RescheduleOutbox(
                        1,
                        "sha256:" + "f" * 64,
                        1,
                        ShanghaiTimestamp.parse("2026-09-04T14:41:00+08:00"),
                        "NETWORK_TIMEOUT",
                    ),
                ),
            )
        )
    ack = AcknowledgeOutbox(1, item.content_hash)
    journal.commit(JournalTransaction("txn_ack_state_0001", (ack,)))
    journal.commit(JournalTransaction("txn_ack_state_replay_0001", (ack,)))
    with pytest.raises(LocalJournalError, match="ALREADY_TERMINAL"):
        journal.commit(
            JournalTransaction(
                "txn_reschedule_terminal_0001",
                (
                    RescheduleOutbox(
                        1,
                        item.content_hash,
                        1,
                        ShanghaiTimestamp.parse("2026-09-04T14:41:00+08:00"),
                        "NETWORK_TIMEOUT",
                    ),
                ),
            )
        )
    journal.close()


def test_checkpoint_revision_is_monotonic(tmp_path: Path) -> None:
    journal = journal_at((tmp_path / "journal.db").resolve())
    journal.open()
    journal.commit(
        JournalTransaction(
            "txn_checkpoint_0001",
            (PutCheckpoint(CheckpointRecord("runtime", "cursor", b"one", 1)),),
        )
    )
    journal.commit(
        JournalTransaction(
            "txn_checkpoint_replay_0001",
            (PutCheckpoint(CheckpointRecord("runtime", "cursor", b"two", 2)),),
        )
    )
    journal.commit(
        JournalTransaction(
            "txn_checkpoint_0002",
            (PutCheckpoint(CheckpointRecord("runtime", "cursor", b"two", 2)),),
        )
    )
    with pytest.raises(JournalConflictError):
        journal.commit(
            JournalTransaction(
                "txn_checkpoint_0003",
                (PutCheckpoint(CheckpointRecord("runtime", "cursor", b"old", 1)),),
            )
        )
    journal.close()


def test_journal_is_bound_to_one_logical_account(tmp_path: Path) -> None:
    path = (tmp_path / "journal.db").resolve()
    journal = journal_at(path)
    opened = journal.open()
    with pytest.raises(LocalJournalError, match="ACCOUNT_MISMATCH"):
        journal.commit(
            JournalTransaction(
                "txn_other_account_0001",
                (
                    CacheTradePlan(
                        "plan_other_0001",
                        plan_document("plan_other_0001", account_id="acct_other_001"),
                    ),
                ),
            )
        )
    journal.close()

    other_account = SQLiteLocalJournal(
        SQLiteJournalSettings(path, "acct_other_001", minimum_free_bytes=0),
        clock=FixedClock(),
    )
    with pytest.raises(LocalJournalError, match="ACCOUNT_MISMATCH"):
        other_account.open()
    assert other_account.status is JournalStatus.FAILED
    assert opened.journal_id.startswith("journal_")


def test_broker_links_and_trades_require_matching_persisted_intent(tmp_path: Path) -> None:
    journal = journal_at((tmp_path / "journal.db").resolve())
    journal.open()
    orphan_link = BrokerOrderLink(
        client_order_id="ord_orphan_0001",
        remark_token="WRONG_TOKEN_000000000001",
        request_seq=1,
        updated_at=ShanghaiTimestamp.parse("2026-09-04T14:35:01+08:00"),
    )
    with pytest.raises(LocalJournalError, match="ORDER_INTENT_NOT_FOUND"):
        journal.commit(
            JournalTransaction("txn_orphan_link_0001", (UpsertBrokerOrderLink(orphan_link),))
        )

    orphan_trade = BrokerTradeRecord(
        trade_id="trade_orphan_0001",
        client_order_id="ord_orphan_0001",
        account_id="acct_demo_001",
        traded_at=ShanghaiTimestamp.parse("2026-09-04T14:36:01+08:00"),
        document=document("trade_orphan_0001", {"quantity": 100}),
    )
    with pytest.raises(LocalJournalError, match="ORDER_INTENT_NOT_FOUND"):
        journal.commit(
            JournalTransaction("txn_orphan_trade_0001", (RecordBrokerTrade(orphan_trade),))
        )

    stored_intent = intent()
    journal.commit(JournalTransaction("txn_store_intent_0001", (PutOrderIntent(stored_intent),)))
    wrong_remark = replace(orphan_link, client_order_id=stored_intent.client_order_id)
    with pytest.raises(JournalConflictError):
        journal.commit(
            JournalTransaction("txn_wrong_remark_0001", (UpsertBrokerOrderLink(wrong_remark),))
        )

    wrong_account_trade = BrokerTradeRecord(
        trade_id="trade_wrong_account_0001",
        client_order_id=stored_intent.client_order_id,
        account_id="acct_other_001",
        traded_at=ShanghaiTimestamp.parse("2026-09-04T14:36:01+08:00"),
        document=document("trade_wrong_account_0001", {"quantity": 100}),
    )
    with pytest.raises(LocalJournalError, match="ACCOUNT_MISMATCH"):
        journal.commit(
            JournalTransaction(
                "txn_wrong_trade_account_0001",
                (RecordBrokerTrade(wrong_account_trade),),
            )
        )
    assert journal.status is JournalStatus.OPEN
    journal.close()


def test_thread_ownership_and_external_write_lock_fail_explicitly(tmp_path: Path) -> None:
    path = (tmp_path / "journal.db").resolve()
    journal = journal_at(path, busy_timeout_ms=5)
    journal.open()
    errors: list[BaseException] = []

    def access_from_other_thread() -> None:
        try:
            journal.health()
        except BaseException as exc:
            errors.append(exc)

    thread = Thread(target=access_from_other_thread)
    thread.start()
    thread.join()
    assert isinstance(errors[0], LocalJournalError)
    assert errors[0].code == "JOURNAL_THREAD_OWNERSHIP_VIOLATION"

    with sqlite3.connect(path, isolation_level=None) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(LocalJournalError) as busy:
            journal.commit(
                JournalTransaction(
                    "txn_locked_0001",
                    (CacheTradePlan("plan_demo_0001", plan_document()),),
                )
            )
        assert busy.value.code == "JOURNAL_BUSY"
        assert busy.value.retryable is True
        blocker.execute("ROLLBACK")
    assert journal.status is JournalStatus.OPEN
    journal.close()


def test_health_write_probe_reports_external_lock_without_closing(tmp_path: Path) -> None:
    path = (tmp_path / "journal.db").resolve()
    journal = journal_at(path, busy_timeout_ms=5)
    journal.open()
    with sqlite3.connect(path, isolation_level=None) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        health = journal.health()
        assert health.status is JournalHealthStatus.DEGRADED
        assert health.writable is False
        assert health.reason_codes == ("JOURNAL_BUSY",)
        blocker.execute("ROLLBACK")
    assert journal.health().status is JournalHealthStatus.HEALTHY
    journal.close()


def test_health_checkpoint_integrity_and_backup_include_wal_data(tmp_path: Path) -> None:
    path = (tmp_path / "journal.db").resolve()
    journal = journal_at(path, maximum_wal_bytes=1)
    journal.open()
    journal.commit(
        JournalTransaction("txn_backup_0001", (CacheTradePlan("plan_demo_0001", plan_document()),))
    )
    health = journal.health()
    assert health.status is JournalHealthStatus.DEGRADED
    assert health.database_bytes == path.stat().st_size
    assert health.wal_bytes > 1
    assert health.free_disk_bytes > 0
    assert health.reason_codes == ("WAL_SIZE_HIGH",)

    integrity = journal.maintain(MaintenanceRequest(MaintenanceKind.INTEGRITY_CHECK))
    assert integrity.completed is True
    assert integrity.detail_code == "INTEGRITY_CHECK_OK"

    backup_path = (tmp_path / "backups" / "journal-20260904.db").resolve()
    backup = journal.maintain(MaintenanceRequest(MaintenanceKind.BACKUP, backup_path))
    assert backup.completed is True
    assert backup.bytes_written == backup_path.stat().st_size
    with sqlite3.connect(backup_path) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM trade_plans").fetchone()[0] == 1

    with pytest.raises(LocalJournalError, match="BACKUP_EXISTS"):
        journal.maintain(MaintenanceRequest(MaintenanceKind.BACKUP, backup_path))
    with pytest.raises(LocalJournalError, match="TARGET_INVALID"):
        journal.maintain(MaintenanceRequest(MaintenanceKind.BACKUP, path))

    checkpoint = journal.maintain(MaintenanceRequest(MaintenanceKind.CHECKPOINT))
    assert checkpoint.completed is True
    assert checkpoint.detail_code == "WAL_CHECKPOINT_COMPLETE"
    journal.close()


def test_health_reports_low_disk_and_outbox_backlog_separately(tmp_path: Path) -> None:
    path = (tmp_path / "journal.db").resolve()
    journal = SQLiteLocalJournal(
        SQLiteJournalSettings(
            path,
            "acct_demo_001",
            minimum_free_bytes=10**30,
            maximum_pending_outbox=1,
        ),
        clock=FixedClock(),
    )
    journal.open()
    journal.commit(
        JournalTransaction(
            "txn_health_backlog_0001",
            (
                EnqueueOutbox(
                    OutboxKind.ASSET_SNAPSHOT,
                    document("snapshot_demo_0001", {"value": 1}),
                ),
                EnqueueOutbox(
                    OutboxKind.POSITION_SNAPSHOT,
                    document("snapshot_demo_0002", {"value": 2}),
                ),
            ),
        )
    )
    health = journal.health()
    assert health.status is JournalHealthStatus.DEGRADED
    assert health.writable is True
    assert health.reason_codes == ("LOW_DISK_SPACE", "OUTBOX_BACKLOG_HIGH")
    journal.close()


def test_migration_checksum_future_schema_and_foreign_database_are_rejected(
    tmp_path: Path,
) -> None:
    checksum_path = (tmp_path / "checksum.db").resolve()
    journal = journal_at(checksum_path)
    journal.open()
    journal.close()
    with sqlite3.connect(checksum_path) as connection:
        connection.execute("UPDATE schema_migrations SET checksum = 'sha256:tampered'")
    corrupt_history = journal_at(checksum_path)
    with pytest.raises(LocalJournalError, match="MIGRATION_CHECKSUM_MISMATCH"):
        corrupt_history.open()
    assert corrupt_history.status is JournalStatus.FAILED

    future_path = (tmp_path / "future.db").resolve()
    with sqlite3.connect(future_path) as connection:
        connection.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION + 1}")
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    with pytest.raises(LocalJournalError, match="SCHEMA_TOO_NEW"):
        journal_at(future_path).open()

    foreign_path = (tmp_path / "foreign.db").resolve()
    with sqlite3.connect(foreign_path) as connection:
        connection.execute("PRAGMA user_version = 1")
        connection.execute("PRAGMA application_id = 123")
    with pytest.raises(LocalJournalError, match="APPLICATION_MISMATCH"):
        journal_at(foreign_path).open()

    unversioned_foreign_path = (tmp_path / "unversioned-foreign.db").resolve()
    with sqlite3.connect(unversioned_foreign_path) as connection:
        connection.execute("CREATE TABLE foreign_data(value TEXT)")
    with pytest.raises(LocalJournalError, match="APPLICATION_MISMATCH"):
        journal_at(unversioned_foreign_path).open()

    zero_application_path = (tmp_path / "zero-application.db").resolve()
    journal = journal_at(zero_application_path)
    journal.open()
    journal.close()
    with sqlite3.connect(zero_application_path) as connection:
        connection.execute("PRAGMA application_id = 0")
    with pytest.raises(LocalJournalError, match="APPLICATION_MISMATCH"):
        journal_at(zero_application_path).open()


def test_existing_schema_missing_metadata_or_required_index_is_not_self_healed(
    tmp_path: Path,
) -> None:
    missing_meta_path = (tmp_path / "missing-meta.db").resolve()
    journal = journal_at(missing_meta_path)
    journal.open()
    journal.close()
    with sqlite3.connect(missing_meta_path) as connection:
        connection.execute("DELETE FROM journal_meta")
    with pytest.raises(LocalJournalError, match="STORAGE_INVARIANT"):
        journal_at(missing_meta_path).open()

    missing_index_path = (tmp_path / "missing-index.db").resolve()
    journal = journal_at(missing_index_path)
    journal.open()
    journal.close()
    with sqlite3.connect(missing_index_path) as connection:
        connection.execute("DROP INDEX ix_outbox_pending")
    with pytest.raises(LocalJournalError, match="SCHEMA_INCOMPLETE"):
        journal_at(missing_index_path).open()


def test_corrupt_transaction_result_and_record_fail_closed(tmp_path: Path) -> None:
    result_path = (tmp_path / "bad-result.db").resolve()
    journal = journal_at(result_path)
    journal.open()
    transaction = JournalTransaction(
        "txn_corrupt_result_0001",
        (CacheTradePlan("plan_demo_0001", plan_document()),),
    )
    journal.commit(transaction)
    journal.close()
    with sqlite3.connect(result_path) as connection:
        connection.execute(
            "UPDATE committed_transactions SET result_body = x'00' WHERE transaction_id = ?",
            (transaction.transaction_id,),
        )
    reopened = journal_at(result_path)
    reopened.open()
    with pytest.raises(LocalJournalError, match="STORAGE_INVARIANT"):
        reopened.commit(transaction)
    assert reopened.status is JournalStatus.FAILED

    record_path = (tmp_path / "bad-record.db").resolve()
    journal = journal_at(record_path)
    journal.open()
    journal.commit(
        JournalTransaction(
            "txn_corrupt_record_0001",
            (CacheTradePlan("plan_demo_0001", plan_document()),),
        )
    )
    journal.close()
    with sqlite3.connect(record_path) as connection:
        connection.execute("UPDATE trade_plans SET body = x'00'")
    reopened = journal_at(record_path)
    reopened.open()
    with pytest.raises(LocalJournalError, match="STORAGE_INVARIANT"):
        reopened.load_recovery(
            account_id="acct_demo_001", trade_date=TradingDate.parse("2026-09-04")
        )
    assert reopened.status is JournalStatus.FAILED


def test_event_sequence_exhaustion_fails_closed_without_partial_commit(tmp_path: Path) -> None:
    path = (tmp_path / "journal.db").resolve()
    journal = journal_at(path)
    journal.open()
    journal.commit(JournalTransaction("txn_event_seed_0001", (AppendDomainEvent(event_draft()),)))
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE boot_sequences SET last_local_seq = 9223372036854775807")
    with pytest.raises(LocalJournalError, match="SEQUENCE_EXHAUSTED"):
        journal.commit(
            JournalTransaction("txn_event_exhausted_0001", (AppendDomainEvent(event_draft(2)),))
        )
    assert journal.status is JournalStatus.FAILED


def test_commit_sequence_exhaustion_is_terminal(tmp_path: Path) -> None:
    path = (tmp_path / "journal.db").resolve()
    journal = journal_at(path)
    journal.open()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE journal_meta SET commit_sequence = 9223372036854775807")
    with pytest.raises(LocalJournalError, match="SEQUENCE_EXHAUSTED"):
        journal.commit(
            JournalTransaction(
                "txn_commit_exhausted_0001",
                (PutCheckpoint(CheckpointRecord("runtime", "cursor", b"one", 1)),),
            )
        )
    assert journal.status is JournalStatus.FAILED


def test_closed_journal_reports_unavailable_and_cannot_reopen(tmp_path: Path) -> None:
    journal = journal_at((tmp_path / "journal.db").resolve())
    journal.open()
    journal.close()
    journal.close()
    assert journal.status is JournalStatus.CLOSED
    assert journal.health().status is JournalHealthStatus.UNAVAILABLE
    with pytest.raises(LocalJournalError, match="JOURNAL_NOT_NEW"):
        journal.open()
    with pytest.raises(LocalJournalError, match="JOURNAL_NOT_OPEN"):
        journal.fetch_outbox(
            limit=1,
            available_at=ShanghaiTimestamp.parse("2026-09-04T15:00:00+08:00"),
        )


def test_public_methods_reject_invalid_types_without_touching_storage(tmp_path: Path) -> None:
    path = (tmp_path / "journal.db").resolve()
    with pytest.raises(ValueError, match="SQLiteJournalSettings"):
        SQLiteLocalJournal("bad")  # type: ignore[arg-type]
    journal = journal_at(path)
    journal.open()
    with pytest.raises(LocalJournalError, match="JOURNAL_NOT_NEW"):
        journal.open()
    with pytest.raises(ValueError, match="JournalTransaction"):
        journal.commit("bad")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="TradingDate"):
        journal.load_recovery(
            account_id="acct_demo_001",
            trade_date="bad",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="MaintenanceRequest"):
        journal.maintain("bad")  # type: ignore[arg-type]
    journal.close()


def test_new_close_and_invalid_database_fail_safely(tmp_path: Path) -> None:
    new_journal = journal_at((tmp_path / "new.db").resolve())
    new_journal.close()
    assert new_journal.status is JournalStatus.CLOSED

    invalid_path = (tmp_path / "invalid.db").resolve()
    invalid_path.write_bytes(b"not-a-sqlite-database")
    invalid = journal_at(invalid_path)
    with pytest.raises(LocalJournalError) as raised:
        invalid.open()
    assert raised.value.code in {"JOURNAL_CORRUPT", "JOURNAL_STORAGE_FAILURE"}
    assert invalid.status is JournalStatus.FAILED
