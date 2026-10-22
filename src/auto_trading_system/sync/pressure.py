"""Adapter that publishes Outbox pressure transitions into the decision loop."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from auto_trading_system.runtime import (
    EventPriority,
    PublishOutcome,
    RuntimeEventSource,
    RuntimeState,
    RuntimeStateMachine,
    TransitionAuthority,
    TransitionDecision,
)
from auto_trading_system.sync.models import (
    OutboxPressureLevel,
    OutboxPressureSignal,
    OutboxWorkerError,
)


@runtime_checkable
class PressureIngress(Protocol):
    def publish(
        self,
        payload: OutboxPressureSignal,
        *,
        source: RuntimeEventSource,
        priority: EventPriority = EventPriority.NORMAL,
    ) -> PublishOutcome: ...


class OutboxPressurePublisher:
    """Use reserved critical queue capacity for every pressure-level transition."""

    def __init__(self, ingress: PressureIngress) -> None:
        if not isinstance(ingress, PressureIngress):
            raise ValueError("ingress must implement PressureIngress")
        self._ingress = ingress

    def __call__(self, signal: OutboxPressureSignal) -> None:
        if not isinstance(signal, OutboxPressureSignal):
            raise ValueError("signal must be an OutboxPressureSignal")
        outcome = self._ingress.publish(
            signal,
            source=RuntimeEventSource.SYNC,
            priority=EventPriority.CRITICAL,
        )
        if outcome is not PublishOutcome.ACCEPTED:
            raise OutboxWorkerError(
                "OUTBOX_PRESSURE_SIGNAL_DROPPED",
                "outbox pressure transition did not enter the decision loop",
                retryable=False,
            )


class OutboxPressureDecisionHandler:
    """Apply Outbox pressure inside the decision-loop owner without auto recovery."""

    def __init__(
        self,
        state_machine: RuntimeStateMachine,
        *,
        owner_guard: Callable[[], None],
    ) -> None:
        if not isinstance(state_machine, RuntimeStateMachine):
            raise ValueError("state_machine must be a RuntimeStateMachine")
        if not callable(owner_guard):
            raise ValueError("owner_guard must be callable")
        self._state_machine = state_machine
        self._owner_guard = owner_guard

    def handle(self, signal: OutboxPressureSignal) -> TransitionDecision:
        self._owner_guard()
        if not isinstance(signal, OutboxPressureSignal):
            raise ValueError("signal must be an OutboxPressureSignal")
        current = self._state_machine.state
        target = current
        reason_code = "OUTBOX_PRESSURE_NORMAL"
        if signal.level is OutboxPressureLevel.CRITICAL:
            target = RuntimeState.HALT
            reason_code = "OUTBOX_PRESSURE_CRITICAL"
        elif signal.level is OutboxPressureLevel.HIGH:
            if current is RuntimeState.AUTO:
                target = RuntimeState.PAUSE_BUY
            reason_code = "OUTBOX_PRESSURE_HIGH"
        return self._state_machine.request_transition(
            target,
            authority=TransitionAuthority.HARD_SAFETY,
            reason_code=reason_code,
        )
