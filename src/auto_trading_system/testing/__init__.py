"""Deterministic test adapters that are never selected by production composition."""

from auto_trading_system.testing.central_api import (
    EventBatchScenario,
    MockCentralDisconnectedError,
    MockCentralNetworkFault,
    MockCentralService,
    MockCentralTimeoutError,
    MockResponseFault,
    ReceiptOutcome,
)

__all__ = [
    "EventBatchScenario",
    "MockCentralDisconnectedError",
    "MockCentralNetworkFault",
    "MockCentralService",
    "MockCentralTimeoutError",
    "MockResponseFault",
    "ReceiptOutcome",
]
