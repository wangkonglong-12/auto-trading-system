"""Market-data gateway errors."""

from __future__ import annotations


class MarketGatewayError(RuntimeError):
    """Base class for market-port failures."""


class MarketNotStartedError(MarketGatewayError):
    """Raised when market data is used before gateway startup."""


class SubscriptionLimitExceeded(MarketGatewayError):
    """Raised when the configured symbol subscription bound is exceeded."""


class MarketDataQueryError(MarketGatewayError):
    """Raised when the latest-tick query did not produce an authoritative result."""


class MarketDataTimeoutError(MarketDataQueryError):
    """Raised when a latest-tick query exceeds its deadline."""
