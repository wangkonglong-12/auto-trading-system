from __future__ import annotations

from dataclasses import dataclass

import pytest

from auto_trading_system.domain import ShanghaiTimestamp
from auto_trading_system.gateways.broker import BrokerQueryError, validate_order_query_result
from auto_trading_system.journal import JournalHealth, JournalHealthStatus
from auto_trading_system.monitoring import JournalHealthFaultDetector
from auto_trading_system.runtime import (
    RuntimeCapability,
    RuntimeFaultComponent,
    RuntimeFaultKind,
    RuntimeFaultPolicy,
    RuntimeFaultReport,
    RuntimeHealth,
    RuntimeRecoveryPermit,
    RuntimeState,
    RuntimeStateMachine,
    TransitionAuthority,
    TransitionOutcome,
)
from auto_trading_system.sync import (
    OutboxPressureDecisionHandler,
    OutboxPressureLevel,
    OutboxPressureSignal,
)


@dataclass
class MonotonicClock:
    value: int = 0

    def monotonic_ns(self) -> int:
        self.value += 1
        return self.value


def _health() -> RuntimeHealth:
    return RuntimeHealth(True, True, True, True, True, False, False, True, True, True)


def _auto_machine() -> RuntimeStateMachine:
    machine = RuntimeStateMachine(
        capability=RuntimeCapability.FULL,
        monotonic_clock=MonotonicClock(),
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
        health=_health(),
    )
    machine.request_transition(
        RuntimeState.AUTO,
        authority=TransitionAuthority.RUNTIME,
        reason_code="AUTO_ACTIVATED",
        health=_health(),
        permit=RuntimeRecoveryPermit("recon_fault_acceptance_0001", True, True),
    )
    assert machine.state is RuntimeState.AUTO
    return machine


def _pressure(level: OutboxPressureLevel, pending: int) -> OutboxPressureSignal:
    return OutboxPressureSignal(
        level,
        pending,
        2,
        3,
        ShanghaiTimestamp.parse("2026-09-04T14:35:00+08:00"),
    )


def _apply_fault(
    machine: RuntimeStateMachine,
    kind: RuntimeFaultKind,
    component: RuntimeFaultComponent,
) -> None:
    decision = RuntimeFaultPolicy(machine, owner_guard=lambda: None).handle(
        RuntimeFaultReport(kind, component, 100, kind.value)
    )
    assert decision.outcome is TransitionOutcome.APPLIED
    assert machine.state is RuntimeState.HALT


def test_central_outage_backlog_pauses_buys_and_recovery_does_not_resume_auto() -> None:
    machine = _auto_machine()
    handler = OutboxPressureDecisionHandler(machine, owner_guard=lambda: None)

    high = handler.handle(_pressure(OutboxPressureLevel.HIGH, 2))
    normal = handler.handle(_pressure(OutboxPressureLevel.NORMAL, 0))

    assert high.current_state is RuntimeState.PAUSE_BUY
    assert normal.outcome is TransitionOutcome.NOOP
    assert machine.state is RuntimeState.PAUSE_BUY
    assert machine.restriction_authority is TransitionAuthority.HARD_SAFETY


def test_critical_central_backlog_halts_runtime() -> None:
    machine = _auto_machine()

    decision = OutboxPressureDecisionHandler(machine, owner_guard=lambda: None).handle(
        _pressure(OutboxPressureLevel.CRITICAL, 3)
    )

    assert decision.current_state is RuntimeState.HALT
    assert machine.restriction_authority is TransitionAuthority.HARD_SAFETY


def test_qmt_disconnect_reconnect_requires_reconciliation_and_stops_at_ready_paused() -> None:
    machine = _auto_machine()

    disconnected = machine.request_transition(
        RuntimeState.CONNECTING,
        authority=TransitionAuthority.BROKER_FACT,
        reason_code="QMT_DISCONNECTED",
    )
    reconnected = machine.request_transition(
        RuntimeState.RECONCILING,
        authority=TransitionAuthority.BROKER_FACT,
        reason_code="QMT_RECONNECTED",
    )
    reconciled = machine.request_transition(
        RuntimeState.READY_PAUSED,
        authority=TransitionAuthority.LIFECYCLE,
        reason_code="RECONNECT_RECONCILED",
        health=_health(),
    )

    assert [
        disconnected.current_state,
        reconnected.current_state,
        reconciled.current_state,
    ] == [RuntimeState.CONNECTING, RuntimeState.RECONCILING, RuntimeState.READY_PAUSED]
    assert machine.state is RuntimeState.READY_PAUSED


def test_none_broker_query_is_unknown_and_halts_instead_of_becoming_empty() -> None:
    machine = _auto_machine()

    with pytest.raises(BrokerQueryError):
        validate_order_query_result(None)
    _apply_fault(
        machine,
        RuntimeFaultKind.BROKER_QUERY_UNAVAILABLE,
        RuntimeFaultComponent.BROKER,
    )


def test_runtime_component_crash_halts_under_watchdog_authority() -> None:
    machine = _auto_machine()

    _apply_fault(
        machine,
        RuntimeFaultKind.RUNTIME_COMPONENT_CRASH,
        RuntimeFaultComponent.SCHEDULER,
    )

    assert machine.restriction_authority is TransitionAuthority.WATCHDOG


def test_disk_pressure_health_signal_halts_before_new_orders() -> None:
    machine = _auto_machine()
    health = JournalHealth(
        JournalHealthStatus.DEGRADED,
        True,
        1,
        1,
        0,
        0,
        ("LOW_DISK_SPACE",),
    )
    signal = JournalHealthFaultDetector().observe(health, now_monotonic_ns=100)
    assert signal is not None

    _apply_fault(machine, signal.kind, signal.component)


def test_clock_skew_fault_halts_under_hard_safety_authority() -> None:
    machine = _auto_machine()

    _apply_fault(
        machine,
        RuntimeFaultKind.CLOCK_SKEW_UNSAFE,
        RuntimeFaultComponent.CONTROL,
    )

    assert machine.restriction_authority is TransitionAuthority.HARD_SAFETY
