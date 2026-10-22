from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from auto_trading_system.control import (
    ControlConfirmation,
    LocalControlAction,
    LocalControlCoordinator,
    LocalControlError,
    LocalControlRequest,
    project_local_control_recovery,
    restore_local_control_restriction,
)
from auto_trading_system.domain import ShanghaiTimestamp, TradingDate
from auto_trading_system.journal.sqlite import SQLiteJournalSettings, SQLiteLocalJournal
from auto_trading_system.journal.writer import PersistenceWriter
from auto_trading_system.protocol import validate_domain_event_semantics
from auto_trading_system.runtime import (
    RuntimeCapability,
    RuntimeHealth,
    RuntimeRecoveryPermit,
    RuntimeState,
    RuntimeStateMachine,
    TransitionAuthority,
    TransitionOutcome,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ACCOUNT_ID = "acct_demo_001"
NOW = ShanghaiTimestamp.parse("2026-09-04T14:40:01+08:00")


@dataclass(frozen=True, slots=True)
class FixedClock:
    value: ShanghaiTimestamp = field(default=NOW)

    def now(self) -> ShanghaiTimestamp:
        return self.value


@dataclass
class IncrementingClock:
    value: int = 0

    def monotonic_ns(self) -> int:
        self.value += 1
        return self.value


def healthy_runtime() -> RuntimeHealth:
    return RuntimeHealth(True, True, True, True, True, False, False, True, True, True)


def full_permit() -> RuntimeRecoveryPermit:
    return RuntimeRecoveryPermit("recon_control_sqlite_0001", True, True)


def ready_auto_machine() -> RuntimeStateMachine:
    machine = RuntimeStateMachine(
        capability=RuntimeCapability.FULL,
        monotonic_clock=IncrementingClock(),
        owner_guard=lambda: None,
    )
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
    return machine


def emergency_request() -> LocalControlRequest:
    return LocalControlRequest(
        "cmd_control_sqlite_0001",
        LocalControlAction.EMERGENCY_HALT,
        "operator_demo",
        "LOCAL_EMERGENCY_HALT",
        ShanghaiTimestamp.parse("2026-09-04T14:40:00+08:00"),
        RuntimeState.AUTO,
        "corr_control_sqlite_0001",
        ControlConfirmation(
            "confirm_control_sqlite_0001",
            "operator_demo",
            ShanghaiTimestamp.parse("2026-09-04T14:40:00.500+08:00"),
            "sha256:" + "5" * 64,
        ),
        True,
    )


def writer_at(database_path: Path, *, busy_timeout_ms: int = 2_500) -> PersistenceWriter:
    return PersistenceWriter(
        SQLiteLocalJournal(
            SQLiteJournalSettings(
                database_path,
                ACCOUNT_ID,
                busy_timeout_ms=busy_timeout_ms,
                minimum_free_bytes=0,
            ),
            clock=FixedClock(),
        ),
        capacity=8,
    )


async def exercise_durable_local_halt(database_path: Path) -> None:
    writer = writer_at(database_path)
    machine = ready_auto_machine()
    coordinator = LocalControlCoordinator(
        writer,
        machine,
        node_id="node-demo-001",
        account_id=ACCOUNT_ID,
        boot_id="boot_11111111111111111111111111111111",
        clock=FixedClock(),
        owner_guard=lambda: None,
        event_id_factory=lambda: "evt_33333333333333333333333333333333",
    )
    await writer.start()
    try:
        result = await coordinator.apply(emergency_request())
        assert result.outcome is TransitionOutcome.APPLIED
        assert machine.state is RuntimeState.HALT

        remote_clear = machine.request_transition(
            RuntimeState.READY_PAUSED,
            authority=TransitionAuthority.CENTRAL,
            reason_code="CENTRAL_HALT_CLEAR",
            health=healthy_runtime(),
            permit=full_permit(),
        )
        assert remote_clear.outcome is TransitionOutcome.REJECTED
        assert machine.state is RuntimeState.HALT

        recovery = await writer.load_recovery(
            account_id=ACCOUNT_ID,
            trade_date=TradingDate.parse("2026-09-04"),
        )
        assert len(recovery.controls) == 2
        projected = project_local_control_recovery(recovery.controls, account_id=ACCOUNT_ID)
        assert projected.active_state is RuntimeState.HALT
        assert projected.unresolved_command_ids == ()

        outbox = await writer.fetch_outbox(limit=10, available_at=NOW)
        assert len(outbox) == 1
        event = outbox[0].document.to_mapping()
        with (PROJECT_ROOT / "contracts" / "v1" / "domain-event.schema.json").open(
            encoding="utf-8"
        ) as handle:
            schema = cast(dict[str, Any], json.load(handle))
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        assert list(validator.iter_errors(event)) == []
        validate_domain_event_semantics(event)
        assert event["payload"]["command_type"] == "EMERGENCY_HALT"
        assert event["payload"]["new_state"] == "HALT"
    finally:
        await writer.stop()

    restarted = writer_at(database_path)
    await restarted.start()
    try:
        recovery = await restarted.load_recovery(
            account_id=ACCOUNT_ID,
            trade_date=TradingDate.parse("2026-09-04"),
        )
        projected = project_local_control_recovery(recovery.controls, account_id=ACCOUNT_ID)
        assert projected.active_state is RuntimeState.HALT
        assert projected.last_command_id == emergency_request().command_id
        restarted_machine = RuntimeStateMachine(
            capability=RuntimeCapability.FULL,
            monotonic_clock=IncrementingClock(),
            owner_guard=lambda: None,
        )
        restored = restore_local_control_restriction(restarted_machine, projected)
        assert restored is not None
        assert restarted_machine.state is RuntimeState.HALT
        assert restarted_machine.restriction_authority is TransitionAuthority.LOCAL_OPERATOR
    finally:
        await restarted.stop()


def test_local_halt_audit_and_restriction_survive_sqlite_restart(tmp_path: Path) -> None:
    asyncio.run(exercise_durable_local_halt((tmp_path / "local-control.db").resolve()))


async def exercise_locked_emergency_halt(database_path: Path) -> None:
    writer = writer_at(database_path, busy_timeout_ms=10)
    machine = ready_auto_machine()
    coordinator = LocalControlCoordinator(
        writer,
        machine,
        node_id="node-demo-001",
        account_id=ACCOUNT_ID,
        boot_id="boot_11111111111111111111111111111111",
        clock=FixedClock(),
        owner_guard=lambda: None,
    )
    await writer.start()
    competing_writer = sqlite3.connect(database_path, isolation_level=None, timeout=0)
    try:
        competing_writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(LocalControlError) as captured:
            await coordinator.apply(emergency_request())
        assert captured.value.code == "LOCAL_CONTROL_REQUEST_AUDIT_FAILED"
        assert machine.state is RuntimeState.HALT
    finally:
        competing_writer.execute("ROLLBACK")
        competing_writer.close()
    try:
        recovery = await writer.load_recovery(
            account_id=ACCOUNT_ID,
            trade_date=TradingDate.parse("2026-09-04"),
        )
        assert recovery.controls == ()
    finally:
        await writer.stop()


def test_emergency_halt_is_immediate_even_when_sqlite_is_write_locked(tmp_path: Path) -> None:
    asyncio.run(exercise_locked_emergency_halt((tmp_path / "locked-control.db").resolve()))
