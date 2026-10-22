from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import cast

import pytest

from auto_trading_system.apps.trade_runtime.bootstrap import runtime_capability_for_profile
from auto_trading_system.config import EnvironmentProfile
from auto_trading_system.runtime import (
    BoundedEventIngress,
    DecisionEventLoop,
    DecisionLoopError,
    PublishOutcome,
    QueueOverflowReport,
    RuntimeCapability,
    RuntimeDispatch,
    RuntimeEventSource,
    RuntimeHealth,
    RuntimeRecoveryPermit,
    RuntimeState,
    RuntimeStateMachine,
    TransitionAuthority,
    TransitionDecision,
    TransitionOutcome,
)


@dataclass
class IncrementingClock:
    value: int = 0

    def monotonic_ns(self) -> int:
        self.value += 1
        return self.value


@dataclass
class OwnerGuard:
    calls: int = 0
    failure: RuntimeError | None = None

    def __call__(self) -> None:
        self.calls += 1
        if self.failure is not None:
            raise self.failure


def healthy_runtime() -> RuntimeHealth:
    return RuntimeHealth(
        qmt_connected=True,
        broker_account_ok=True,
        market_healthy=True,
        journal_healthy=True,
        reconciliation_complete=True,
        critical_discrepancy=False,
        unknown_order=False,
        clock_within_limit=True,
        disk_writable=True,
        event_queue_intact=True,
    )


def full_permit() -> RuntimeRecoveryPermit:
    return RuntimeRecoveryPermit(
        reconciliation_id="recon_runtime_0001",
        local_authorized=True,
        central_authorized=True,
    )


def make_machine(
    capability: RuntimeCapability = RuntimeCapability.FULL,
) -> tuple[RuntimeStateMachine, OwnerGuard, IncrementingClock]:
    guard = OwnerGuard()
    clock = IncrementingClock()
    return (
        RuntimeStateMachine(
            capability=capability,
            monotonic_clock=clock,
            owner_guard=guard,
        ),
        guard,
        clock,
    )


def advance_to_ready(machine: RuntimeStateMachine, health: RuntimeHealth | None = None) -> None:
    assert (
        machine.request_transition(
            RuntimeState.CONNECTING,
            authority=TransitionAuthority.LIFECYCLE,
            reason_code="START_CONNECTING",
        ).outcome
        is TransitionOutcome.APPLIED
    )
    assert (
        machine.request_transition(
            RuntimeState.RECONCILING,
            authority=TransitionAuthority.LIFECYCLE,
            reason_code="START_RECONCILING",
        ).outcome
        is TransitionOutcome.APPLIED
    )
    assert (
        machine.request_transition(
            RuntimeState.READY_PAUSED,
            authority=TransitionAuthority.LIFECYCLE,
            reason_code="START_READY_PAUSED",
            health=health or healthy_runtime(),
        ).outcome
        is TransitionOutcome.APPLIED
    )


def activate_auto(machine: RuntimeStateMachine) -> TransitionDecision:
    return machine.request_transition(
        RuntimeState.AUTO,
        authority=TransitionAuthority.RUNTIME,
        reason_code="VALID_PLAN_ACTIVATED",
        health=healthy_runtime(),
        permit=full_permit(),
    )


def overflow_report(*, normal: int, critical: int) -> QueueOverflowReport:
    return QueueOverflowReport(
        dropped_total=normal + critical,
        dropped_normal=normal,
        dropped_critical=critical,
        sources=(RuntimeEventSource.MARKET,),
        first_monotonic_ns=10,
        last_monotonic_ns=20,
    )


def test_initial_state_is_always_booting_without_an_auto_restore_path() -> None:
    machine, guard, _ = make_machine()

    assert machine.state is RuntimeState.BOOTING
    assert machine.transition_sequence == 0
    assert machine.restriction_authority is None
    assert guard.calls == 0


def test_startup_requires_connect_reconcile_and_healthy_ready_pause() -> None:
    machine, _, clock = make_machine()

    connect = machine.request_transition(
        RuntimeState.CONNECTING,
        authority=TransitionAuthority.LIFECYCLE,
        reason_code="START_CONNECTING",
    )
    reconcile = machine.request_transition(
        RuntimeState.RECONCILING,
        authority=TransitionAuthority.BROKER_FACT,
        reason_code="BROKER_CONNECTED",
    )
    missing_health = machine.request_transition(
        RuntimeState.READY_PAUSED,
        authority=TransitionAuthority.LIFECYCLE,
        reason_code="RECONCILIATION_FINISHED",
    )
    ready = machine.request_transition(
        RuntimeState.READY_PAUSED,
        authority=TransitionAuthority.LIFECYCLE,
        reason_code="RECONCILIATION_FINISHED",
        health=healthy_runtime(),
    )

    assert [
        connect.transition_sequence,
        reconcile.transition_sequence,
        ready.transition_sequence,
    ] == [
        1,
        2,
        3,
    ]
    assert missing_health.outcome is TransitionOutcome.REJECTED
    assert missing_health.rejection_code == "RECOVERY_HEALTH_REQUIRED"
    assert machine.state is RuntimeState.READY_PAUSED
    assert clock.value == 4


def test_reconciliation_blockers_prevent_ready_paused() -> None:
    machine, _, _ = make_machine()
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

    decision = machine.request_transition(
        RuntimeState.READY_PAUSED,
        authority=TransitionAuthority.LIFECYCLE,
        reason_code="UNSAFE_RECONCILIATION",
        health=replace(healthy_runtime(), critical_discrepancy=True),
    )

    assert decision.rejection_code == "RECOVERY_PRECONDITION_FAILED"
    assert decision.blockers == ("CRITICAL_DISCREPANCY_OPEN",)
    assert machine.state is RuntimeState.RECONCILING


def test_illegal_transition_is_rejected_without_changing_state_or_sequence() -> None:
    machine, guard, _ = make_machine()

    decision = machine.request_transition(
        RuntimeState.AUTO,
        authority=TransitionAuthority.RUNTIME,
        reason_code="SKIP_STARTUP",
        health=healthy_runtime(),
        permit=full_permit(),
    )

    assert decision.outcome is TransitionOutcome.REJECTED
    assert decision.rejection_code == "INVALID_RUNTIME_TRANSITION"
    assert decision.current_state is RuntimeState.BOOTING
    assert decision.transition_sequence is None
    assert machine.transition_sequence == 0
    assert guard.calls == 1


def test_lifecycle_and_shutdown_transitions_require_explicit_authorities() -> None:
    machine, _, _ = make_machine()
    rejected_connect = machine.request_transition(
        RuntimeState.CONNECTING,
        authority=TransitionAuthority.CENTRAL,
        reason_code="REMOTE_CONNECT",
    )
    rejected_stop = machine.request_transition(
        RuntimeState.STOPPING,
        authority=TransitionAuthority.CENTRAL,
        reason_code="REMOTE_STOP",
    )

    assert rejected_connect.rejection_code == "LIFECYCLE_AUTHORITY_REQUIRED"
    assert rejected_stop.rejection_code == "SHUTDOWN_AUTHORITY_REQUIRED"
    assert machine.state is RuntimeState.BOOTING


def test_reconcile_ready_and_reconnect_reject_non_lifecycle_authority() -> None:
    machine, _, _ = make_machine()
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
    ready = machine.request_transition(
        RuntimeState.READY_PAUSED,
        authority=TransitionAuthority.BROKER_FACT,
        reason_code="BROKER_TRIED_READY",
        health=healthy_runtime(),
    )

    assert ready.rejection_code == "LIFECYCLE_AUTHORITY_REQUIRED"

    machine.request_transition(
        RuntimeState.READY_PAUSED,
        authority=TransitionAuthority.LIFECYCLE,
        reason_code="START_READY_PAUSED",
        health=healthy_runtime(),
    )
    reconnect = machine.request_transition(
        RuntimeState.CONNECTING,
        authority=TransitionAuthority.CENTRAL,
        reason_code="CENTRAL_TRIED_RECONNECT",
    )
    assert reconnect.rejection_code == "LIFECYCLE_AUTHORITY_REQUIRED"


def test_same_state_request_is_noop_and_does_not_replace_restriction() -> None:
    machine, _, _ = make_machine()
    advance_to_ready(machine)
    activate_auto(machine)
    pause = machine.request_transition(
        RuntimeState.PAUSE_BUY,
        authority=TransitionAuthority.CENTRAL,
        reason_code="CENTRAL_PAUSE",
    )
    duplicate = machine.request_transition(
        RuntimeState.PAUSE_BUY,
        authority=TransitionAuthority.STRATEGY,
        reason_code="STRATEGY_DUPLICATE",
    )

    assert pause.outcome is TransitionOutcome.APPLIED
    assert duplicate.outcome is TransitionOutcome.NOOP
    assert duplicate.transition_sequence is None
    assert machine.restriction_authority is TransitionAuthority.CENTRAL


def test_same_state_higher_priority_restriction_is_latched_without_fake_transition() -> None:
    machine, _, _ = make_machine()
    advance_to_ready(machine)
    activate_auto(machine)
    machine.request_transition(
        RuntimeState.HALT,
        authority=TransitionAuthority.CENTRAL,
        reason_code="CENTRAL_HALT",
    )

    local_halt = machine.request_transition(
        RuntimeState.HALT,
        authority=TransitionAuthority.LOCAL_OPERATOR,
        reason_code="LOCAL_EMERGENCY_HALT",
    )

    assert local_halt.outcome is TransitionOutcome.NOOP
    assert local_halt.transition_sequence is None
    assert machine.restriction_authority is TransitionAuthority.LOCAL_OPERATOR


@pytest.mark.parametrize(
    ("field", "expected_blocker"),
    [
        ("qmt_connected", "QMT_DISCONNECTED"),
        ("broker_account_ok", "BROKER_ACCOUNT_NOT_OK"),
        ("market_healthy", "MARKET_DATA_UNHEALTHY"),
        ("journal_healthy", "JOURNAL_UNAVAILABLE"),
        ("reconciliation_complete", "RECONCILIATION_REQUIRED"),
        ("clock_within_limit", "CLOCK_SKEW_UNSAFE"),
        ("disk_writable", "DISK_UNWRITABLE"),
        ("event_queue_intact", "EVENT_QUEUE_INCOMPLETE"),
    ],
)
def test_auto_rejects_each_unhealthy_positive_precondition(
    field: str, expected_blocker: str
) -> None:
    machine, _, _ = make_machine()
    advance_to_ready(machine)
    unhealthy = replace(healthy_runtime(), **{field: False})

    decision = machine.request_transition(
        RuntimeState.AUTO,
        authority=TransitionAuthority.RUNTIME,
        reason_code="AUTO_REQUEST",
        health=unhealthy,
        permit=full_permit(),
    )

    assert decision.rejection_code == "AUTO_HEALTH_PRECONDITION_FAILED"
    assert expected_blocker in decision.blockers
    assert machine.state is RuntimeState.READY_PAUSED


@pytest.mark.parametrize(
    ("field", "expected_blocker"),
    [
        ("critical_discrepancy", "CRITICAL_DISCREPANCY_OPEN"),
        ("unknown_order", "UNKNOWN_ORDER_OPEN"),
    ],
)
def test_auto_rejects_each_open_negative_precondition(field: str, expected_blocker: str) -> None:
    machine, _, _ = make_machine()
    advance_to_ready(machine)
    unhealthy = replace(healthy_runtime(), **{field: True})

    decision = machine.request_transition(
        RuntimeState.AUTO,
        authority=TransitionAuthority.RUNTIME,
        reason_code="AUTO_REQUEST",
        health=unhealthy,
        permit=full_permit(),
    )

    assert expected_blocker in decision.blockers


@pytest.mark.parametrize(
    ("health", "permit", "expected_code"),
    [
        (None, None, "AUTO_HEALTH_REQUIRED"),
        (healthy_runtime(), None, "AUTO_PERMIT_REQUIRED"),
        (
            healthy_runtime(),
            RuntimeRecoveryPermit("recon_runtime_0001", False, True),
            "LOCAL_AUTO_AUTHORIZATION_REQUIRED",
        ),
        (
            healthy_runtime(),
            RuntimeRecoveryPermit("recon_runtime_0001", True, False),
            "CENTRAL_AUTO_AUTHORIZATION_REQUIRED",
        ),
    ],
)
def test_auto_requires_explicit_health_and_dual_authorization(
    health: RuntimeHealth | None,
    permit: RuntimeRecoveryPermit | None,
    expected_code: str,
) -> None:
    machine, _, _ = make_machine()
    advance_to_ready(machine)

    decision = machine.request_transition(
        RuntimeState.AUTO,
        authority=TransitionAuthority.RUNTIME,
        reason_code="AUTO_REQUEST",
        health=health,
        permit=permit,
    )

    assert decision.rejection_code == expected_code


def test_remote_or_strategy_request_cannot_directly_enter_auto() -> None:
    machine, _, _ = make_machine()
    advance_to_ready(machine)

    for authority in (TransitionAuthority.CENTRAL, TransitionAuthority.STRATEGY):
        decision = machine.request_transition(
            RuntimeState.AUTO,
            authority=authority,
            reason_code="REMOTE_AUTO_REQUEST",
            health=healthy_runtime(),
            permit=full_permit(),
        )
        assert decision.rejection_code == "REMOTE_AUTO_RESUME_REQUIRES_LOCAL_DECISION"


@pytest.mark.parametrize(
    ("capability", "target", "expected_code"),
    [
        (
            RuntimeCapability.OBSERVE_ONLY,
            RuntimeState.AUTO,
            "EXECUTION_CAPABILITY_FORBIDS_AUTO",
        ),
        (
            RuntimeCapability.EXIT_ONLY,
            RuntimeState.AUTO,
            "EXECUTION_CAPABILITY_FORBIDS_AUTO",
        ),
        (
            RuntimeCapability.OBSERVE_ONLY,
            RuntimeState.SELL_ONLY,
            "EXECUTION_CAPABILITY_FORBIDS_SELL",
        ),
    ],
)
def test_deployment_capability_is_a_hard_upper_bound(
    capability: RuntimeCapability, target: RuntimeState, expected_code: str
) -> None:
    machine, _, _ = make_machine(capability)
    advance_to_ready(machine)

    decision = machine.request_transition(
        target,
        authority=TransitionAuthority.RUNTIME,
        reason_code="CAPABILITY_TEST",
        health=healthy_runtime(),
        permit=full_permit(),
    )

    assert decision.rejection_code == expected_code


def test_exit_only_capability_can_enter_sell_only() -> None:
    machine, _, _ = make_machine(RuntimeCapability.EXIT_ONLY)
    advance_to_ready(machine)

    decision = machine.request_transition(
        RuntimeState.SELL_ONLY,
        authority=TransitionAuthority.RUNTIME,
        reason_code="EXIT_PROFILE_ACTIVE",
    )

    assert decision.outcome is TransitionOutcome.APPLIED
    assert machine.state is RuntimeState.SELL_ONLY


@pytest.mark.parametrize(
    ("profile", "capability"),
    [
        (EnvironmentProfile.READ_ONLY, RuntimeCapability.OBSERVE_ONLY),
        (EnvironmentProfile.PAPER, RuntimeCapability.FULL),
        (EnvironmentProfile.SHADOW, RuntimeCapability.FULL),
        (EnvironmentProfile.ASSISTED, RuntimeCapability.FULL),
        (EnvironmentProfile.LIVE_EXIT_ONLY, RuntimeCapability.EXIT_ONLY),
        (EnvironmentProfile.LIVE, RuntimeCapability.FULL),
    ],
)
def test_deployment_profile_maps_to_explicit_runtime_capability(
    profile: EnvironmentProfile, capability: RuntimeCapability
) -> None:
    assert runtime_capability_for_profile(profile) is capability


def test_profile_and_machine_reject_untyped_capability_inputs() -> None:
    with pytest.raises(ValueError, match="profile"):
        runtime_capability_for_profile(cast(EnvironmentProfile, "LIVE"))
    with pytest.raises(ValueError, match="capability"):
        RuntimeStateMachine(
            capability=cast(RuntimeCapability, "FULL"),
            monotonic_clock=IncrementingClock(),
            owner_guard=OwnerGuard(),
        )


def test_central_restriction_cannot_be_cleared_by_runtime_or_strategy() -> None:
    machine, _, _ = make_machine()
    advance_to_ready(machine)
    assert activate_auto(machine).outcome is TransitionOutcome.APPLIED
    machine.request_transition(
        RuntimeState.PAUSE_BUY,
        authority=TransitionAuthority.CENTRAL,
        reason_code="CENTRAL_RISK_PAUSE",
    )

    for authority in (TransitionAuthority.RUNTIME, TransitionAuthority.STRATEGY):
        decision = machine.request_transition(
            RuntimeState.AUTO,
            authority=authority,
            reason_code="LOW_PRIORITY_RESUME",
            health=healthy_runtime(),
            permit=full_permit(),
        )
        expected = (
            "HIGHER_PRIORITY_RESTRICTION_ACTIVE"
            if authority is TransitionAuthority.RUNTIME
            else "REMOTE_AUTO_RESUME_REQUIRES_LOCAL_DECISION"
        )
        assert decision.rejection_code == expected
    assert machine.state is RuntimeState.PAUSE_BUY


def test_only_local_operator_can_clear_even_when_other_authority_has_higher_priority() -> None:
    machine, _, _ = make_machine()
    advance_to_ready(machine)
    activate_auto(machine)
    machine.request_transition(
        RuntimeState.SELL_ONLY,
        authority=TransitionAuthority.CENTRAL,
        reason_code="CENTRAL_SELL_ONLY",
    )

    hard_safety_resume = machine.request_transition(
        RuntimeState.PAUSE_BUY,
        authority=TransitionAuthority.HARD_SAFETY,
        reason_code="HARD_SAFETY_TRIED_RESUME",
        health=healthy_runtime(),
        permit=full_permit(),
    )
    local_without_permit = machine.request_transition(
        RuntimeState.PAUSE_BUY,
        authority=TransitionAuthority.LOCAL_OPERATOR,
        reason_code="LOCAL_RESUME_WITHOUT_PERMIT",
        health=healthy_runtime(),
    )

    assert hard_safety_resume.rejection_code == "LOCAL_RECOVERY_DECISION_REQUIRED"
    assert local_without_permit.rejection_code == "LOCAL_RECOVERY_AUTHORIZATION_REQUIRED"


def test_local_operator_can_clear_restriction_only_after_safe_recovery() -> None:
    machine, _, _ = make_machine()
    advance_to_ready(machine)
    activate_auto(machine)
    machine.request_transition(
        RuntimeState.HALT,
        authority=TransitionAuthority.LOCAL_OPERATOR,
        reason_code="LOCAL_EMERGENCY_HALT",
    )

    remote = machine.request_transition(
        RuntimeState.READY_PAUSED,
        authority=TransitionAuthority.CENTRAL,
        reason_code="REMOTE_HALT_CLEAR",
        health=healthy_runtime(),
        permit=full_permit(),
    )
    no_health = machine.request_transition(
        RuntimeState.READY_PAUSED,
        authority=TransitionAuthority.LOCAL_OPERATOR,
        reason_code="LOCAL_HALT_CLEAR",
        permit=full_permit(),
    )
    unhealthy = machine.request_transition(
        RuntimeState.READY_PAUSED,
        authority=TransitionAuthority.LOCAL_OPERATOR,
        reason_code="LOCAL_HALT_CLEAR",
        health=replace(healthy_runtime(), unknown_order=True),
        permit=full_permit(),
    )
    recovered = machine.request_transition(
        RuntimeState.READY_PAUSED,
        authority=TransitionAuthority.LOCAL_OPERATOR,
        reason_code="LOCAL_HALT_CLEAR",
        health=healthy_runtime(),
        permit=full_permit(),
    )

    assert remote.rejection_code == "HIGHER_PRIORITY_RESTRICTION_ACTIVE"
    assert no_health.rejection_code == "RECOVERY_HEALTH_REQUIRED"
    assert unhealthy.rejection_code == "RECOVERY_PRECONDITION_FAILED"
    assert unhealthy.blockers == ("UNKNOWN_ORDER_OPEN",)
    assert recovered.outcome is TransitionOutcome.APPLIED
    assert machine.state is RuntimeState.READY_PAUSED
    assert machine.restriction_authority is None


def test_more_restrictive_request_applies_but_preserves_higher_authority_owner() -> None:
    machine, _, _ = make_machine()
    advance_to_ready(machine)
    activate_auto(machine)
    machine.request_transition(
        RuntimeState.PAUSE_BUY,
        authority=TransitionAuthority.HARD_SAFETY,
        reason_code="HARD_LIMIT_REACHED",
    )

    halt = machine.request_transition(
        RuntimeState.HALT,
        authority=TransitionAuthority.CENTRAL,
        reason_code="CENTRAL_EMERGENCY_HALT",
    )

    assert halt.outcome is TransitionOutcome.APPLIED
    assert machine.state is RuntimeState.HALT
    assert machine.restriction_authority is TransitionAuthority.HARD_SAFETY


def test_normal_queue_overflow_pauses_auto_and_does_not_loosen_safer_state() -> None:
    machine, _, _ = make_machine()
    advance_to_ready(machine)
    activate_auto(machine)

    paused = machine.degrade_for_queue_overflow(overflow_report(normal=3, critical=0))
    already_safe = machine.degrade_for_queue_overflow(overflow_report(normal=1, critical=0))

    assert paused.outcome is TransitionOutcome.APPLIED
    assert paused.current_state is RuntimeState.PAUSE_BUY
    assert paused.reason_code == "QUEUE_NORMAL_OVERFLOW"
    assert already_safe.outcome is TransitionOutcome.NOOP
    assert machine.state is RuntimeState.PAUSE_BUY


def test_critical_queue_overflow_always_halts() -> None:
    machine, _, _ = make_machine()
    advance_to_ready(machine)
    activate_auto(machine)

    decision = machine.degrade_for_queue_overflow(overflow_report(normal=0, critical=1))

    assert decision.outcome is TransitionOutcome.APPLIED
    assert decision.current_state is RuntimeState.HALT
    assert decision.reason_code == "QUEUE_CRITICAL_OVERFLOW"
    assert machine.restriction_authority is TransitionAuthority.HARD_SAFETY


def test_any_overflow_during_startup_halts_fail_closed() -> None:
    machine, _, _ = make_machine()

    decision = machine.degrade_for_queue_overflow(overflow_report(normal=1, critical=0))

    assert decision.current_state is RuntimeState.HALT
    assert decision.reason_code == "QUEUE_CRITICAL_OVERFLOW"


def test_reconnect_path_returns_only_to_ready_paused() -> None:
    machine, _, _ = make_machine()
    advance_to_ready(machine)
    activate_auto(machine)
    disconnected = machine.request_transition(
        RuntimeState.CONNECTING,
        authority=TransitionAuthority.BROKER_FACT,
        reason_code="QMT_DISCONNECTED",
    )
    reconciling = machine.request_transition(
        RuntimeState.RECONCILING,
        authority=TransitionAuthority.BROKER_FACT,
        reason_code="QMT_RECONNECTED",
    )
    ready = machine.request_transition(
        RuntimeState.READY_PAUSED,
        authority=TransitionAuthority.LIFECYCLE,
        reason_code="RECONNECT_RECONCILED",
        health=healthy_runtime(),
    )

    assert [disconnected.current_state, reconciling.current_state, ready.current_state] == [
        RuntimeState.CONNECTING,
        RuntimeState.RECONCILING,
        RuntimeState.READY_PAUSED,
    ]
    assert machine.state is RuntimeState.READY_PAUSED


def test_reconnect_keeps_existing_restriction_until_local_recovery() -> None:
    machine, _, _ = make_machine()
    advance_to_ready(machine)
    activate_auto(machine)
    machine.request_transition(
        RuntimeState.PAUSE_BUY,
        authority=TransitionAuthority.CENTRAL,
        reason_code="CENTRAL_RISK_PAUSE",
    )

    connecting = machine.request_transition(
        RuntimeState.CONNECTING,
        authority=TransitionAuthority.BROKER_FACT,
        reason_code="QMT_DISCONNECTED",
    )
    machine.request_transition(
        RuntimeState.RECONCILING,
        authority=TransitionAuthority.BROKER_FACT,
        reason_code="QMT_RECONNECTED",
    )
    machine.request_transition(
        RuntimeState.READY_PAUSED,
        authority=TransitionAuthority.LIFECYCLE,
        reason_code="RECONNECT_RECONCILED",
        health=healthy_runtime(),
    )
    runtime_resume = activate_auto(machine)

    assert connecting.outcome is TransitionOutcome.APPLIED
    assert machine.restriction_authority is TransitionAuthority.CENTRAL
    assert runtime_resume.rejection_code == "HIGHER_PRIORITY_RESTRICTION_ACTIVE"


def test_stopping_and_stopped_are_terminal_and_never_resume_auto() -> None:
    machine, _, _ = make_machine()
    stopping = machine.request_transition(
        RuntimeState.STOPPING,
        authority=TransitionAuthority.LIFECYCLE,
        reason_code="PROCESS_STOP_REQUESTED",
    )
    stopped = machine.request_transition(
        RuntimeState.STOPPED,
        authority=TransitionAuthority.LIFECYCLE,
        reason_code="PROCESS_STOPPED",
    )
    resume = machine.request_transition(
        RuntimeState.AUTO,
        authority=TransitionAuthority.LOCAL_OPERATOR,
        reason_code="INVALID_RESUME",
        health=healthy_runtime(),
        permit=full_permit(),
    )

    assert stopping.outcome is TransitionOutcome.APPLIED
    assert stopped.outcome is TransitionOutcome.APPLIED
    assert resume.rejection_code == "INVALID_RUNTIME_TRANSITION"
    assert machine.state is RuntimeState.STOPPED


def test_owner_guard_failure_prevents_all_state_mutation() -> None:
    machine, guard, clock = make_machine()
    guard.failure = RuntimeError("not the decision owner")

    with pytest.raises(RuntimeError, match="not the decision owner"):
        machine.request_transition(
            RuntimeState.CONNECTING,
            authority=TransitionAuthority.LIFECYCLE,
            reason_code="START_CONNECTING",
        )

    assert machine.state is RuntimeState.BOOTING
    assert machine.transition_sequence == 0
    assert clock.value == 0


def test_state_machine_mutation_integrates_with_actual_decision_loop_owner() -> None:
    async def scenario() -> None:
        clock = IncrementingClock()
        ingress = BoundedEventIngress[str](
            normal_capacity=2,
            critical_capacity=1,
            monotonic_clock=clock,
        )
        decisions: list[TransitionDecision] = []
        machine: RuntimeStateMachine

        async def handler(_: RuntimeDispatch[str]) -> None:
            decisions.append(
                machine.request_transition(
                    RuntimeState.CONNECTING,
                    authority=TransitionAuthority.LIFECYCLE,
                    reason_code="START_CONNECTING",
                )
            )

        loop = DecisionEventLoop(ingress=ingress, handler=handler)
        machine = RuntimeStateMachine(
            capability=RuntimeCapability.FULL,
            monotonic_clock=clock,
            owner_guard=loop.assert_owner,
        )
        assert (
            ingress.publish("start", source=RuntimeEventSource.INTERNAL) is PublishOutcome.ACCEPTED
        )
        ingress.close()

        await loop.run()

        assert decisions[0].outcome is TransitionOutcome.APPLIED
        assert machine.state is RuntimeState.CONNECTING
        with pytest.raises(DecisionLoopError, match="DECISION_STATE_OWNER_REQUIRED"):
            machine.request_transition(
                RuntimeState.RECONCILING,
                authority=TransitionAuthority.LIFECYCLE,
                reason_code="INVALID_EXTERNAL_MUTATION",
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("target", "authority", "reason", "message"),
    [
        (cast(RuntimeState, "AUTO"), TransitionAuthority.RUNTIME, "VALID_REASON", "target"),
        (
            RuntimeState.CONNECTING,
            cast(TransitionAuthority, "LIFECYCLE"),
            "VALID_REASON",
            "authority",
        ),
        (RuntimeState.CONNECTING, TransitionAuthority.LIFECYCLE, "bad", "reason_code"),
    ],
)
def test_transition_request_rejects_untyped_or_invalid_input(
    target: RuntimeState,
    authority: TransitionAuthority,
    reason: str,
    message: str,
) -> None:
    machine, _, _ = make_machine()

    with pytest.raises(ValueError, match=message):
        machine.request_transition(target, authority=authority, reason_code=reason)


def test_health_and_permit_reject_silent_type_coercion_or_invalid_id() -> None:
    with pytest.raises(ValueError, match="qmt_connected"):
        replace(healthy_runtime(), qmt_connected=cast(bool, 1))
    with pytest.raises(ValueError, match="reconciliation_id"):
        RuntimeRecoveryPermit("invalid", True, True)
    with pytest.raises(ValueError, match="local_authorized"):
        RuntimeRecoveryPermit("recon_runtime_0001", cast(bool, 1), True)
    with pytest.raises(ValueError, match="central_authorized"):
        RuntimeRecoveryPermit("recon_runtime_0001", True, cast(bool, 1))


def test_invalid_overflow_report_is_rejected_before_state_mutation() -> None:
    machine, _, _ = make_machine()
    report = QueueOverflowReport(
        dropped_total=2,
        dropped_normal=1,
        dropped_critical=0,
        sources=(RuntimeEventSource.MARKET,),
        first_monotonic_ns=1,
        last_monotonic_ns=2,
    )

    with pytest.raises(ValueError, match="dropped events"):
        machine.degrade_for_queue_overflow(report)
    assert machine.state is RuntimeState.BOOTING


@pytest.mark.parametrize(
    "report",
    [
        QueueOverflowReport(
            dropped_total=1,
            dropped_normal=-1,
            dropped_critical=2,
            sources=(RuntimeEventSource.MARKET,),
            first_monotonic_ns=1,
            last_monotonic_ns=2,
        ),
        QueueOverflowReport(
            dropped_total=1,
            dropped_normal=1,
            dropped_critical=0,
            sources=(RuntimeEventSource.MARKET,),
            first_monotonic_ns=-1,
            last_monotonic_ns=2,
        ),
        QueueOverflowReport(
            dropped_total=1,
            dropped_normal=1,
            dropped_critical=0,
            sources=(RuntimeEventSource.MARKET,),
            first_monotonic_ns=2,
            last_monotonic_ns=1,
        ),
    ],
)
def test_overflow_degradation_rejects_negative_or_reversed_report(
    report: QueueOverflowReport,
) -> None:
    machine, _, _ = make_machine()

    with pytest.raises(ValueError, match="dropped events"):
        machine.degrade_for_queue_overflow(report)


def test_transition_decision_enforces_applied_and_rejected_invariants() -> None:
    with pytest.raises(ValueError, match="positive transition_sequence"):
        TransitionDecision(
            outcome=TransitionOutcome.APPLIED,
            previous_state=RuntimeState.BOOTING,
            requested_state=RuntimeState.CONNECTING,
            current_state=RuntimeState.BOOTING,
            authority=TransitionAuthority.LIFECYCLE,
            reason_code="START_CONNECTING",
            decided_monotonic_ns=1,
        )
    with pytest.raises(ValueError, match="inconsistent fields"):
        TransitionDecision(
            outcome=TransitionOutcome.APPLIED,
            previous_state=RuntimeState.BOOTING,
            requested_state=RuntimeState.BOOTING,
            current_state=RuntimeState.BOOTING,
            authority=TransitionAuthority.LIFECYCLE,
            reason_code="START_CONNECTING",
            decided_monotonic_ns=1,
            transition_sequence=1,
        )
    with pytest.raises(ValueError, match="requires a rejection_code"):
        TransitionDecision(
            outcome=TransitionOutcome.REJECTED,
            previous_state=RuntimeState.BOOTING,
            requested_state=RuntimeState.CONNECTING,
            current_state=RuntimeState.BOOTING,
            authority=TransitionAuthority.LIFECYCLE,
            reason_code="START_CONNECTING",
            decided_monotonic_ns=1,
        )
    with pytest.raises(ValueError, match="cannot have a transition_sequence"):
        TransitionDecision(
            outcome=TransitionOutcome.NOOP,
            previous_state=RuntimeState.BOOTING,
            requested_state=RuntimeState.BOOTING,
            current_state=RuntimeState.BOOTING,
            authority=TransitionAuthority.LIFECYCLE,
            reason_code="START_CONNECTING",
            decided_monotonic_ns=1,
            transition_sequence=1,
        )
    with pytest.raises(ValueError, match="only rejected"):
        TransitionDecision(
            outcome=TransitionOutcome.NOOP,
            previous_state=RuntimeState.BOOTING,
            requested_state=RuntimeState.BOOTING,
            current_state=RuntimeState.BOOTING,
            authority=TransitionAuthority.LIFECYCLE,
            reason_code="START_CONNECTING",
            decided_monotonic_ns=1,
            rejection_code="INVALID_RUNTIME_TRANSITION",
        )
    with pytest.raises(ValueError, match="cannot change current_state"):
        TransitionDecision(
            outcome=TransitionOutcome.REJECTED,
            previous_state=RuntimeState.BOOTING,
            requested_state=RuntimeState.CONNECTING,
            current_state=RuntimeState.CONNECTING,
            authority=TransitionAuthority.LIFECYCLE,
            reason_code="START_CONNECTING",
            decided_monotonic_ns=1,
            rejection_code="INVALID_RUNTIME_TRANSITION",
        )
    with pytest.raises(ValueError, match="noop decision"):
        TransitionDecision(
            outcome=TransitionOutcome.NOOP,
            previous_state=RuntimeState.BOOTING,
            requested_state=RuntimeState.CONNECTING,
            current_state=RuntimeState.BOOTING,
            authority=TransitionAuthority.LIFECYCLE,
            reason_code="START_CONNECTING",
            decided_monotonic_ns=1,
        )
    with pytest.raises(ValueError, match="non-negative"):
        TransitionDecision(
            outcome=TransitionOutcome.NOOP,
            previous_state=RuntimeState.BOOTING,
            requested_state=RuntimeState.CONNECTING,
            current_state=RuntimeState.BOOTING,
            authority=TransitionAuthority.LIFECYCLE,
            reason_code="START_CONNECTING",
            decided_monotonic_ns=-1,
        )
