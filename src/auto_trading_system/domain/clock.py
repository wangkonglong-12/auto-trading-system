"""Injectable wall and monotonic clocks for deterministic trading logic."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Protocol

from auto_trading_system.domain.trading_time import SHANGHAI_TIMEZONE, ShanghaiTimestamp


class Clock(Protocol):
    """Wall-clock port used by business rules and event timestamping."""

    def now(self) -> ShanghaiTimestamp:
        """Return the current instant normalized to Asia/Shanghai."""
        ...


class MonotonicClock(Protocol):
    """Elapsed-time port immune to wall-clock corrections."""

    def monotonic_ns(self) -> int:
        """Return an opaque monotonically increasing nanosecond counter."""
        ...


class SystemClock:
    """Production implementation of both clock ports."""

    def now(self) -> ShanghaiTimestamp:
        return ShanghaiTimestamp(datetime.now(tz=SHANGHAI_TIMEZONE))

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()
