from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from auto_trading_system.domain import (
    SHANGHAI_TIMEZONE,
    Clock,
    DomainValueError,
    MonotonicClock,
    ShanghaiTimestamp,
    SystemClock,
    TradingDate,
)


@pytest.mark.parametrize("text", ["2026-01-01", "2026-02-28", "2028-02-29"])
def test_trading_date_parses_strict_iso_calendar_date(text: str) -> None:
    trading_date = TradingDate.parse(text)

    assert trading_date.to_wire() == text
    assert str(trading_date) == text


@pytest.mark.parametrize("text", ["", "2026-1-01", "2026-01-1", "2026/01/01", "2026-02-29"])
def test_trading_date_rejects_invalid_or_noncanonical_values(text: str) -> None:
    with pytest.raises(DomainValueError, match="INVALID_TRADING_DATE"):
        TradingDate.parse(text)


def test_trading_date_constructor_rejects_datetime_subclass() -> None:
    with pytest.raises(DomainValueError, match="INVALID_TRADING_DATE_TYPE"):
        TradingDate(datetime(2026, 1, 1))  # noqa: DTZ001 - deliberately test rejection


def test_trading_date_combines_naive_local_time_in_shanghai() -> None:
    timestamp = TradingDate(date(2026, 9, 3)).at(time(14, 50, 0))

    assert timestamp.to_wire() == "2026-09-03T14:50:00+08:00"
    assert timestamp.trading_date == TradingDate(date(2026, 9, 3))
    assert timestamp.value.tzinfo is SHANGHAI_TIMEZONE


@pytest.mark.parametrize(
    "local_time",
    [time(14, 50, tzinfo=UTC), "14:50:00"],
)
def test_trading_date_rejects_nonlocal_time(local_time: object) -> None:
    with pytest.raises(DomainValueError, match="INVALID_LOCAL_TIME"):
        TradingDate(date(2026, 9, 3)).at(local_time)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-09-03T14:50:00+08:00", "2026-09-03T14:50:00+08:00"),
        ("2026-09-03T14:50:00.123456+08:00", "2026-09-03T14:50:00.123456+08:00"),
    ],
)
def test_shanghai_timestamp_parses_explicit_protocol_offset(text: str, expected: str) -> None:
    assert ShanghaiTimestamp.parse(text).to_wire() == expected


@pytest.mark.parametrize(
    ("text", "error_code"),
    [
        ("not-a-time", "INVALID_TIMESTAMP"),
        ("2026-09-03T14:50:00", "INVALID_TIMEZONE_OFFSET"),
        ("2026-09-03T06:50:00Z", "INVALID_TIMEZONE_OFFSET"),
        ("2026-09-03T15:50:00+09:00", "INVALID_TIMEZONE_OFFSET"),
        ("2026-09-03 14:50:00+08:00", "INVALID_TIMESTAMP"),
        ("2026-09-03T14:50:00+0800", "INVALID_TIMESTAMP"),
        ("2026-09-03T14:50:00.1234567+08:00", "INVALID_TIMESTAMP"),
    ],
)
def test_shanghai_timestamp_wire_parser_enforces_offset(text: str, error_code: str) -> None:
    with pytest.raises(DomainValueError, match=error_code):
        ShanghaiTimestamp.parse(text)


def test_shanghai_timestamp_rejects_naive_datetime() -> None:
    with pytest.raises(DomainValueError, match="NAIVE_TIMESTAMP"):
        ShanghaiTimestamp(
            datetime(2026, 9, 3, 14, 50)  # noqa: DTZ001 - deliberately test rejection
        )


def test_shanghai_timestamp_rejects_non_string_wire_value() -> None:
    with pytest.raises(DomainValueError, match="INVALID_TIMESTAMP"):
        ShanghaiTimestamp.parse(1)  # type: ignore[arg-type]


def test_shanghai_timestamp_normalizes_aware_internal_instant() -> None:
    timestamp = ShanghaiTimestamp.from_instant(datetime(2026, 9, 3, 6, 50, tzinfo=UTC))

    assert timestamp.to_wire() == "2026-09-03T14:50:00+08:00"
    assert timestamp.value.tzinfo is SHANGHAI_TIMEZONE
    assert str(timestamp) == timestamp.to_wire()


def test_system_clock_satisfies_wall_and_monotonic_ports() -> None:
    clock = SystemClock()
    wall_clock: Clock = clock
    monotonic_clock: MonotonicClock = clock

    before = monotonic_clock.monotonic_ns()
    now = wall_clock.now()
    after = monotonic_clock.monotonic_ns()

    assert now.value.tzinfo is SHANGHAI_TIMEZONE
    assert before <= after
