"""Broker gateway errors with explicit uncertainty semantics."""

from __future__ import annotations


class BrokerGatewayError(RuntimeError):
    """Base class for broker-port failures."""


class BrokerNotStartedError(BrokerGatewayError):
    """Raised when an operation is attempted before gateway startup."""


class DuplicateClientOrderError(BrokerGatewayError):
    """Raised when a client order id is submitted more than once."""


class UnknownOrderError(BrokerGatewayError):
    """Raised when an order cannot be identified by the broker adapter."""


class OrderNotCancelableError(BrokerGatewayError):
    """Raised when a cancel is requested for a terminal order."""


class BrokerQueryError(BrokerGatewayError):
    """Raised when a query did not produce authoritative broker facts."""


class BrokerTimeoutError(BrokerGatewayError):
    """Timeout whose operation outcome may or may not be known."""

    def __init__(self, operation: str, *, outcome_unknown: bool) -> None:
        qualifier = "outcome unknown" if outcome_unknown else "not applied"
        super().__init__(f"broker {operation} timed out ({qualifier})")
        self.operation = operation
        self.outcome_unknown = outcome_unknown
