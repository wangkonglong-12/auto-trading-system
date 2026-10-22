from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

import pytest

from auto_trading_system.apps.trade_runtime.bootstrap import (
    build_order_intent_gate,
)
from auto_trading_system.config import RuntimeConfig
from auto_trading_system.domain import (
    Clock,
    Money,
    Price,
    Quantity,
    SecurityCode,
    ShanghaiTimestamp,
)
from auto_trading_system.execution import (
    HardSafetyViolation,
    OrderIntentGateError,
    OrderIntentSubmission,
)
from auto_trading_system.gateways.broker import MockBrokerGateway
from auto_trading_system.journal import IntentSide, OrderIntentRecord
from auto_trading_system.journal.sqlite import SQLiteJournalSettings, SQLiteLocalJournal
from auto_trading_system.journal.writer import PersistenceWriter
from auto_trading_system.risk import HardSafetyFacts
from auto_trading_system.runtime import RuntimeState


@dataclass(frozen=True, slots=True)
class FixedClock:
    value: ShanghaiTimestamp

    def now(self) -> ShanghaiTimestamp:
        return self.value


@dataclass
class FactSequence:
    values: list[HardSafetyFacts]
    calls: int = 0

    def __call__(self) -> HardSafetyFacts:
        self.calls += 1
        return self.values.pop(0)


def fixed_clock() -> FixedClock:
    return FixedClock(ShanghaiTimestamp.parse("2026-09-04T14:35:01+08:00"))


def settings(tmp_path: Path) -> RuntimeConfig:
    path = Path(__file__).parents[2] / "config" / "v1" / "runtime.read-only.example.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["storage"]["root"] = str(tmp_path.resolve())
    return RuntimeConfig.model_validate(payload)


def facts() -> HardSafetyFacts:
    return HardSafetyFacts(
        "acct_demo_001",
        True,
        True,
        Money.parse("100000"),
        Money.parse("0"),
        (),
        (),
        Money.parse("0"),
        Decimal("0.5"),
        Decimal("1"),
    )


def submission(*, price: str = "12.34", max_notional: str = "5000") -> OrderIntentSubmission:
    created_at = fixed_clock().now()
    intent = OrderIntentRecord(
        "intent_01K43HARDGATE00001",
        "ord_01K43HARDGATE00001",
        "plan_01K43HARD20260904001",
        "acct_demo_001",
        created_at.trading_date,
        SecurityCode.parse("000001.SZ"),
        IntentSide.BUY,
        Quantity(100),
        Price.parse(price),
        Money.parse(max_notional),
        "ATS000000000000000000087",
        "EXEC_BALANCED_V1",
        created_at,
    )
    return OrderIntentSubmission(
        intent,
        "evt_00000000000000000000000000000087",
        "node-demo-001",
        "boot_88888888888888888888888888888888",
        "corr_hard_gate_20260904_0001",
        "risk_hard_01K43HARDGATE00001",
    )


def test_hard_safety_rechecks_facts_after_intent_commit_and_before_broker(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        configured = settings(tmp_path)
        supplier = FactSequence([facts(), replace(facts(), tick_age_seconds=Decimal("2"))])
        clock: Clock = fixed_clock()
        writer = PersistenceWriter(
            SQLiteLocalJournal(
                SQLiteJournalSettings(
                    (tmp_path / "hard-gate.db").resolve(),
                    configured.node.account_id,
                    minimum_free_bytes=0,
                ),
                clock=clock,
            )
        )
        broker = MockBrokerGateway(clock)
        broker.start(lambda _: None)
        gate = build_order_intent_gate(
            configured,
            writer,
            broker,
            owner_guard=lambda: None,
            state_supplier=lambda: RuntimeState.AUTO,
            hard_safety_facts_supplier=supplier,
        )
        value = submission()
        await writer.start()
        try:
            with pytest.raises(OrderIntentGateError) as captured:
                await gate.persist_and_submit(value)
            assert captured.value.code == "ORDER_EXECUTION_PERMISSION_REVOKED"
            assert isinstance(captured.value.__cause__, HardSafetyViolation)
            assert supplier.calls == 2
            assert broker.query_orders() == ()
            recovered = await writer.load_recovery(
                account_id=value.intent.account_id,
                trade_date=value.intent.trade_date,
            )
            assert recovered.order_intents == (value.intent,)
        finally:
            await writer.stop()
            broker.stop()

    asyncio.run(scenario())


def test_central_plan_limit_cannot_expand_windows_single_order_limit(tmp_path: Path) -> None:
    async def scenario() -> None:
        configured = settings(tmp_path)
        clock: Clock = fixed_clock()
        writer = PersistenceWriter(
            SQLiteLocalJournal(
                SQLiteJournalSettings(
                    (tmp_path / "central-cannot-expand.db").resolve(),
                    configured.node.account_id,
                    minimum_free_bytes=0,
                ),
                clock=clock,
            )
        )
        broker = MockBrokerGateway(clock)
        broker.start(lambda _: None)
        gate = build_order_intent_gate(
            configured,
            writer,
            broker,
            owner_guard=lambda: None,
            state_supplier=lambda: RuntimeState.AUTO,
            hard_safety_facts_supplier=facts,
        )
        await writer.start()
        try:
            with pytest.raises(OrderIntentGateError) as captured:
                await gate.persist_and_submit(submission(price="60", max_notional="7000"))
            assert captured.value.code == "ORDER_EXECUTION_NOT_ALLOWED"
            assert isinstance(captured.value.__cause__, HardSafetyViolation)
            assert writer.snapshot().accepted == 0
            assert broker.query_orders() == ()
        finally:
            await writer.stop()
            broker.stop()

    asyncio.run(scenario())
