"""Runtime-state permission checked immediately before every broker submission."""

from __future__ import annotations

from collections.abc import Callable

from auto_trading_system.journal import IntentSide, OrderIntentRecord
from auto_trading_system.runtime import RuntimeState

_SELL_STATES = frozenset({RuntimeState.AUTO, RuntimeState.PAUSE_BUY, RuntimeState.SELL_ONLY})


class OrderExecutionPermissionDenied(RuntimeError):
    def __init__(self, state: RuntimeState, side: IntentSide) -> None:
        super().__init__(f"ORDER_EXECUTION_PERMISSION_DENIED: {side.value} is not allowed in state")
        self.code = "ORDER_EXECUTION_PERMISSION_DENIED"
        self.state = state
        self.side = side


class RuntimeOrderExecutionGuard:
    """Read the decision-loop-owned state without caching permission across an await."""

    def __init__(self, state_supplier: Callable[[], RuntimeState]) -> None:
        if not callable(state_supplier):
            raise ValueError("state_supplier must be callable")
        self._state_supplier = state_supplier

    def __call__(self, intent: OrderIntentRecord) -> None:
        if not isinstance(intent, OrderIntentRecord):
            raise ValueError("intent must be an OrderIntentRecord")
        state = self._state_supplier()
        if not isinstance(state, RuntimeState):
            raise ValueError("state_supplier must return RuntimeState")
        allowed = state is RuntimeState.AUTO
        if intent.side is IntentSide.SELL:
            allowed = state in _SELL_STATES
        if not allowed:
            raise OrderExecutionPermissionDenied(state, intent.side)
