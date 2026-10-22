from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass, replace

import pytest

from auto_trading_system.journal import JournalHealth, JournalHealthStatus
from auto_trading_system.journal.writer import (
    PersistenceQueueSnapshot,
    PersistenceWriterStatus,
)
from auto_trading_system.monitoring import (
    JournalHealthFaultDetector,
    PersistenceStallDetector,
    PersistenceStallSignal,
)
from auto_trading_system.runtime import (
    QueueOverflowReport,
    RuntimeCapability,
    RuntimeEventSource,
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


@dataclass
class OwnerGuard:
    calls: int = 0
    failure: RuntimeError | None = None

    def __call__(self) -> None:
        self.calls += 1
        if self.failure is not None:
            raise self.failure


@dataclass
class IncrementingClock:
    value: int = 0

    def monotonic_ns(self) -> int:
        self.value += 1
        return self.value


def healthy_runtime() -> RuntimeHealth:
    return RuntimeHealth(True, True, True, True, True, False, False, True, True, True)


def ready_auto_machine(
    guard: OwnerGuard | None = None,
) -> tuple[RuntimeStateMachine, OwnerGuard]:
    effective_guard = guard or OwnerGuard()
    machine = RuntimeStateMachine(
        capability=RuntimeCapability.FULL,
        monotonic_clock=IncrementingClock(),
        owner_guard=effective_guard,
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
        permit=RuntimeRecoveryPermit("recon_faults_0001", True, True),
    )
    return machine, effective_guard


@pytest.mark.parametrize("kind", list(RuntimeFaultKind))
def test_every_runtime_fault_kind_halts_auto_with_expected_authority(
    kind: RuntimeFaultKind,
) -> None:
    machine, guard = ready_auto_machine()
    policy = RuntimeFaultPolicy(machine, owner_guard=guard)
    component = (
        RuntimeFaultComponent.CONTROL
        if kind is RuntimeFaultKind.CONTROL_AUDIT_UNRESOLVED
        else RuntimeFaultComponent.PERSISTENCE
    )

    decision = policy.handle(RuntimeFaultReport(kind, component, 100, kind.value))

    assert decision.outcome is TransitionOutcome.APPLIED
    assert decision.current_state is RuntimeState.HALT
    expected_authority = TransitionAuthority.HARD_SAFETY
    if kind in {
        RuntimeFaultKind.PERSISTENCE_STALLED,
        RuntimeFaultKind.PERSISTENCE_FAILED,
        RuntimeFaultKind.RUNTIME_COMPONENT_CRASH,
    }:
        expected_authority = TransitionAuthority.WATCHDOG
    elif kind is RuntimeFaultKind.CONTROL_AUDIT_UNRESOLVED:
        expected_authority = TransitionAuthority.LOCAL_OPERATOR
    assert machine.restriction_authority is expected_authority


def test_fault_policy_routes_normal_and_critical_queue_loss_to_existing_rules() -> None:
    normal_machine, normal_guard = ready_auto_machine()
    normal = RuntimeFaultPolicy(normal_machine, owner_guard=normal_guard).handle_queue_overflow(
        QueueOverflowReport(2, 2, 0, (RuntimeEventSource.INTERNAL,), 1, 2)
    )
    critical_machine, critical_guard = ready_auto_machine()
    critical = RuntimeFaultPolicy(
        critical_machine, owner_guard=critical_guard
    ).handle_queue_overflow(QueueOverflowReport(1, 0, 1, (RuntimeEventSource.BROKER,), 1, 1))

    assert normal.current_state is RuntimeState.PAUSE_BUY
    assert critical.current_state is RuntimeState.HALT


def test_fault_policy_owner_failure_precedes_state_mutation() -> None:
    machine, _ = ready_auto_machine()
    failing_guard = OwnerGuard(failure=RuntimeError("not owner"))
    policy = RuntimeFaultPolicy(machine, owner_guard=failing_guard)

    with pytest.raises(RuntimeError, match="not owner"):
        policy.handle(
            RuntimeFaultReport(
                RuntimeFaultKind.JOURNAL_UNWRITABLE,
                RuntimeFaultComponent.PERSISTENCE,
                1,
                "JOURNAL_UNWRITABLE",
            )
        )

    assert machine.state is RuntimeState.AUTO


def snapshot(
    *,
    status: PersistenceWriterStatus = PersistenceWriterStatus.RUNNING,
    pending: int = 0,
    accepted: int = 1,
    completed: int = 0,
) -> PersistenceQueueSnapshot:
    return PersistenceQueueSnapshot(status, 4, pending, accepted, completed, 0)


def test_stall_detector_uses_monotonic_progress_and_latches_until_recovery() -> None:
    detector = PersistenceStallDetector(stall_timeout_ns=100)

    assert detector.observe(snapshot(), now_monotonic_ns=1_000) is None
    assert detector.observe(snapshot(), now_monotonic_ns=1_099) is None
    signal = detector.observe(snapshot(pending=1), now_monotonic_ns=1_100)

    assert signal == PersistenceStallSignal("PERSISTENCE_STALLED", 1_100, 100, 1, 1)
    assert signal.to_fault_report().kind is RuntimeFaultKind.PERSISTENCE_STALLED
    assert detector.observe(snapshot(), now_monotonic_ns=2_000) is None
    assert detector.observe(snapshot(accepted=1, completed=1), now_monotonic_ns=2_001) is None
    assert detector.observe(snapshot(accepted=2, completed=1), now_monotonic_ns=3_000) is None
    assert detector.observe(snapshot(accepted=2, completed=1), now_monotonic_ns=3_100) is not None


def test_stall_detector_reports_writer_failure_once_even_without_pending_queue() -> None:
    detector = PersistenceStallDetector(stall_timeout_ns=100)
    failed = snapshot(
        status=PersistenceWriterStatus.FAILED,
        accepted=2,
        completed=1,
    )

    signal = detector.observe(failed, now_monotonic_ns=5)

    assert signal is not None
    assert signal.reason_code == "PERSISTENCE_FAILED"
    assert signal.outstanding == 1
    assert detector.observe(failed, now_monotonic_ns=6) is None


@pytest.mark.parametrize(
    "invalid",
    [
        snapshot(accepted=0, completed=1),
        replace(snapshot(), capacity=0),
        replace(snapshot(), pending=6),
    ],
)
def test_stall_detector_rejects_inconsistent_snapshots(
    invalid: PersistenceQueueSnapshot,
) -> None:
    with pytest.raises(ValueError, match="inconsistent"):
        PersistenceStallDetector(stall_timeout_ns=1).observe(
            invalid,
            now_monotonic_ns=1,
        )


def test_stall_detector_rejects_monotonic_regression() -> None:
    detector = PersistenceStallDetector(stall_timeout_ns=10)
    assert detector.observe(snapshot(), now_monotonic_ns=20) is None

    with pytest.raises(ValueError, match="backwards"):
        detector.observe(snapshot(), now_monotonic_ns=19)


def test_journal_health_detector_prioritizes_unwritable_and_latches_faults() -> None:
    detector = JournalHealthFaultDetector()
    low_disk = JournalHealth(
        JournalHealthStatus.DEGRADED,
        True,
        1,
        1,
        0,
        0,
        ("LOW_DISK_SPACE",),
    )
    unwritable = replace(low_disk, writable=False, reason_codes=("JOURNAL_BUSY",))
    healthy = replace(
        low_disk,
        status=JournalHealthStatus.HEALTHY,
        reason_codes=(),
    )

    first = detector.observe(low_disk, now_monotonic_ns=1)
    assert first is not None
    assert first.kind is RuntimeFaultKind.JOURNAL_DISK_PRESSURE
    assert detector.observe(low_disk, now_monotonic_ns=2) is None
    second = detector.observe(unwritable, now_monotonic_ns=3)
    assert second is not None
    assert second.kind is RuntimeFaultKind.JOURNAL_UNWRITABLE
    assert detector.observe(healthy, now_monotonic_ns=4) is None
    assert detector.observe(low_disk, now_monotonic_ns=5) is not None


@pytest.mark.parametrize("now", [-1, True])
def test_journal_health_detector_rejects_invalid_time(now: int) -> None:
    health = JournalHealth(JournalHealthStatus.HEALTHY, True, 1, 1, 0, 0)
    with pytest.raises(ValueError, match="now_monotonic_ns"):
        JournalHealthFaultDetector().observe(health, now_monotonic_ns=now)


@pytest.mark.parametrize(
    "value",
    [
        RuntimeFaultReport(
            RuntimeFaultKind.JOURNAL_UNWRITABLE,
            RuntimeFaultComponent.PERSISTENCE,
            0,
            "JOURNAL_UNWRITABLE",
        ),
        PersistenceStallSignal("PERSISTENCE_STALLED", 1, 1, 1, 0),
    ],
)
def test_fault_values_are_immutable(value: object) -> None:
    with pytest.raises(FrozenInstanceError):
        if isinstance(value, RuntimeFaultReport):
            value.detail_code = "CHANGED"  # type: ignore[misc]
        else:
            assert isinstance(value, PersistenceStallSignal)
            value.reason_code = "CHANGED"  # type: ignore[misc]
