"""Order sizing, price protection and fail-closed execution services."""

from auto_trading_system.execution.hard_safety_guard import (
    CompositeOrderExecutionGuard,
    HardSafetyOrderExecutionGuard,
    HardSafetyViolation,
)
from auto_trading_system.execution.order_intent_gate import (
    BrokerOrderSubmitter,
    OrderExecutionGuard,
    OrderIntentCommitter,
    OrderIntentGateError,
    OrderIntentGateResult,
    OrderIntentGateStatus,
    OrderIntentPersistenceGate,
    OrderIntentSubmission,
    OrderSubmissionUnresolvedError,
    UnresolvedSubmissionStage,
)
from auto_trading_system.execution.permission import (
    OrderExecutionPermissionDenied,
    RuntimeOrderExecutionGuard,
)

__all__ = [
    "BrokerOrderSubmitter",
    "CompositeOrderExecutionGuard",
    "HardSafetyOrderExecutionGuard",
    "HardSafetyViolation",
    "OrderExecutionGuard",
    "OrderExecutionPermissionDenied",
    "OrderIntentCommitter",
    "OrderIntentGateError",
    "OrderIntentGateResult",
    "OrderIntentGateStatus",
    "OrderIntentPersistenceGate",
    "OrderIntentSubmission",
    "OrderSubmissionUnresolvedError",
    "RuntimeOrderExecutionGuard",
    "UnresolvedSubmissionStage",
]
