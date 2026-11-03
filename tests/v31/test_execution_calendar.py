import pytest
import test_node_processes as node_helpers

from auto_trading_system.config.execution_calendar import (
    cached_execution_calendar,
    refresh_execution_calendar,
)

node_fixture = node_helpers.node


def test_execution_cache_uses_explicit_future_bounds_and_same_exchange_dates(node_fixture):
    config = node_fixture[0]
    dates = [day.strftime("%Y%m%d") for day in config.calendar.sessions]
    today = config.calendar.sessions[0]
    calls = []

    def reader(market, start, end):
        calls.append((market, start, end))
        return dates

    assert refresh_execution_calendar(config, today, reader)
    cached = cached_execution_calendar(config)
    assert cached.sessions == config.calendar.sessions
    assert calls == [
        (market, f"{today.year - 1}0101", f"{today.year}1231") for market in ("SH", "SZ")
    ]
    assert cached.next_session(today) == config.calendar.next_session(today)
    with pytest.raises(ValueError, match="scope"):
        cached_execution_calendar(config.model_copy(update={"node_id": "another-node"}))


@pytest.mark.parametrize("case", ["disagree", "invalid", "unsorted", "past", "missing_installed"])
def test_bad_calendar_never_replaces_configured_execution_sessions(node_fixture, case):
    config = node_fixture[0]
    today = config.calendar.sessions[0]
    dates = [day.strftime("%Y%m%d") for day in config.calendar.sessions]

    def reader(market, start, end):
        if case == "disagree" and market == "SZ":
            return dates[:-1]
        if case == "invalid":
            return ["bad-date", "20260908"]
        if case == "unsorted":
            return dates[::-1]
        if case == "past":
            return ["20260105", "20260106"]
        if case == "missing_installed":
            return [today.strftime("%Y%m%d"), f"{today.year}1231"]
        return dates

    assert not refresh_execution_calendar(config, today, reader)
    assert cached_execution_calendar(config) == config.calendar
    assert not (config.data_root / "critical/execution-calendar.json").exists()
