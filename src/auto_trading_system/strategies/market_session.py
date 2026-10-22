"""Shared installed continuous-session boundary for the current strategy baseline."""

from datetime import datetime, time


def market_open(now: datetime) -> bool:
    return time(9, 30) <= now.time() < time(11, 30) or time(13) <= now.time() < time(14, 55, 30)
