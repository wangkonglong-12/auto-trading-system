from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from auto_trading_system.domain import (
    Clock,
    Money,
    Price,
    Quantity,
    SecurityCode,
    ShanghaiTimestamp,
)
from auto_trading_system.execution import (
    OrderIntentGateStatus,
    OrderIntentPersistenceGate,
    OrderIntentSubmission,
)
from auto_trading_system.gateways.broker import BrokerEvent, MockBrokerGateway
from auto_trading_system.journal import IntentSide, LocalJournalError, OrderIntentRecord
from auto_trading_system.journal.sqlite import SQLiteJournalSettings, SQLiteLocalJournal
from auto_trading_system.journal.writer import PersistenceWriter
from auto_trading_system.protocol import validate_domain_event_semantics

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class FixedClock:
    value: ShanghaiTimestamp

    def now(self) -> ShanghaiTimestamp:
        return self.value


def fixed_clock() -> FixedClock:
    return FixedClock(ShanghaiTimestamp.parse("2026-09-04T14:35:01.001+08:00"))


def submission() -> OrderIntentSubmission:
    created_at = ShanghaiTimestamp.parse("2026-09-04T14:35:01+08:00")
    intent = OrderIntentRecord(
        "intent_01K43TAIL000000001",
        "ord_01K43TAIL000000001",
        "plan_01K43TAIL20260904001",
        "acct_demo_001",
        created_at.trading_date,
        SecurityCode.parse("000001.SZ"),
        IntentSide.BUY,
        Quantity(100),
        Price.parse("12.34"),
        Money.parse("1500.00"),
        "ATS000000000000000000001",
        "EXEC_BALANCED_V1",
        created_at,
    )
    return OrderIntentSubmission(
        intent,
        "evt_00000000000000000000000000000003",
        "node-demo-001",
        "boot_11111111111111111111111111111111",
        "corr_plan_20260904_0001",
        "risk_01K43TAIL000000001",
    )


async def exercise_real_gate(database_path: Path) -> None:
    clock: Clock = fixed_clock()
    writer = PersistenceWriter(
        SQLiteLocalJournal(
            SQLiteJournalSettings(database_path, "acct_demo_001", minimum_free_bytes=0),
            clock=clock,
        ),
        capacity=8,
    )
    broker = MockBrokerGateway(clock)
    events: list[BrokerEvent] = []
    broker.start(events.append)
    gate = OrderIntentPersistenceGate(
        writer,
        broker,
        owner_guard=lambda: None,
        execution_guard=lambda _: None,
    )
    value = submission()
    await writer.start()
    try:
        first = await gate.persist_and_submit(value)
        second = await gate.persist_and_submit(value)

        assert first.status is OrderIntentGateStatus.SUBMITTED
        assert first.submission_receipt is not None
        assert first.receipt_commit is not None
        assert first.receipt_commit.commit_sequence == 2
        assert second.status is OrderIntentGateStatus.PERSISTED_RECONCILIATION_REQUIRED
        assert broker.query_orders() == (
            broker.find_order_by_client_id(value.intent.client_order_id),
        )
        assert len(events) == 1

        recovered = await writer.load_recovery(
            account_id=value.intent.account_id,
            trade_date=value.intent.trade_date,
        )
        assert recovered.order_intents == (value.intent,)
        assert len(recovered.broker_order_links) == 1
        link = recovered.broker_order_links[0]
        assert link.client_order_id == value.intent.client_order_id
        assert link.remark_token == value.intent.remark_token
        assert link.request_seq == first.submission_receipt.request_seq

        outbox = await writer.fetch_outbox(limit=10, available_at=fixed_clock().value)
        assert len(outbox) == 1
        event = outbox[0].document.to_mapping()
        with (PROJECT_ROOT / "contracts" / "v1" / "domain-event.schema.json").open(
            encoding="utf-8"
        ) as handle:
            schema = cast(dict[str, Any], json.load(handle))
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        assert list(validator.iter_errors(event)) == []
        validate_domain_event_semantics(event)
        assert event["payload"] == value.event_payload()
        assert event["local_seq"] == 1
        assert event["recorded_at"] == fixed_clock().value.to_wire()
        assert (await writer.health()).last_commit_sequence == 2
    finally:
        await writer.stop()
        broker.stop()


def test_real_writer_sqlite_and_mock_broker_enforce_persistence_gate(tmp_path: Path) -> None:
    asyncio.run(exercise_real_gate((tmp_path / "journal.db").resolve()))


async def exercise_locked_database_gate(database_path: Path) -> None:
    clock: Clock = fixed_clock()
    writer = PersistenceWriter(
        SQLiteLocalJournal(
            SQLiteJournalSettings(
                database_path,
                "acct_demo_001",
                busy_timeout_ms=10,
                minimum_free_bytes=0,
            ),
            clock=clock,
        ),
        capacity=8,
    )
    broker = MockBrokerGateway(clock)
    broker.start(lambda _: None)
    gate = OrderIntentPersistenceGate(
        writer,
        broker,
        owner_guard=lambda: None,
        execution_guard=lambda _: None,
    )
    await writer.start()
    competing_writer = sqlite3.connect(database_path, isolation_level=None, timeout=0)
    try:
        competing_writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(LocalJournalError) as captured:
            await gate.persist_and_submit(submission())
        assert captured.value.code == "JOURNAL_BUSY"
        assert broker.query_orders() == ()
    finally:
        competing_writer.execute("ROLLBACK")
        competing_writer.close()
    try:
        recovered = await writer.load_recovery(
            account_id="acct_demo_001",
            trade_date=submission().intent.trade_date,
        )
        assert recovered.order_intents == ()
        assert await writer.fetch_outbox(limit=10, available_at=fixed_clock().value) == ()
    finally:
        await writer.stop()
        broker.stop()


def test_real_sqlite_commit_lock_never_reaches_broker(tmp_path: Path) -> None:
    asyncio.run(exercise_locked_database_gate((tmp_path / "locked-journal.db").resolve()))
