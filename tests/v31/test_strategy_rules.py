from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime

import pytest

from auto_trading_system.domain import Money, Price
from auto_trading_system.strategies.entry_sizing import BuyRule, EntryCapacity, size_entry
from auto_trading_system.strategies.exit_v3 import (
    ExitLot,
    ExitObservation,
    ExitState,
    evaluate_exit,
)


def lot(open_price: str = "10") -> ExitLot:
    # Friday -> Monday comes from the supplied verified calendar, not +1 day.
    return ExitLot(
        "lot-1",
        1000,
        date(2026, 9, 4),
        date(2026, 9, 7),
        Price.parse("10"),
        Price.parse("10"),
        Price.parse(open_price),
    )


def tick(price: str, time: str = "09:30:00", received_ns: int = 1) -> ExitObservation:
    return ExitObservation(
        datetime.fromisoformat(f"2026-09-07T{time}+08:00"),
        received_ns,
        Price.parse(price),
        True,
        True,
    )


def test_high_open_target_is_half_initial_once_not_half_remaining() -> None:
    first = evaluate_exit(lot("10.3"), ExitState(1000), tick("10.3"))
    assert first.state.target_remaining == 500 and "OPEN_REDUCE" in first.reasons
    after_restart = evaluate_exit(lot("10.3"), first.state, tick("10.3", received_ns=2))
    assert after_restart.state.target_remaining == 500 and not after_restart.reasons


def test_profit_rules_combine_to_one_tighter_target() -> None:
    result = evaluate_exit(lot("10.3"), ExitState(1000), tick("10.7"))
    assert result.state.target_remaining == 0
    assert result.reasons == ("OPEN_REDUCE", "TAKE_PROFIT")


def test_peak_requires_three_seconds_and_path_gap_keeps_time_exit_available() -> None:
    state = evaluate_exit(lot(), ExitState(1000), tick("10.4")).state
    state = evaluate_exit(lot(), state, tick("10.29", received_ns=1_000_000_001)).state
    early = evaluate_exit(lot(), state, tick("10.29", received_ns=3_999_999_999))
    assert early.state.target_remaining == 1000
    complete = evaluate_exit(lot(), early.state, tick("10.29", received_ns=4_000_000_001))
    assert complete.state.target_remaining == 0 and complete.reasons == ("PEAK_EXIT",)
    gap = evaluate_exit(
        lot(), state, replace(tick("10.29", received_ns=4_000_000_001), path_complete=False)
    )
    assert not gap.state.path_complete and gap.state.target_remaining == 1000
    timed = evaluate_exit(lot(), gap.state, tick("10.29", "10:30:00", received_ns=5_000_000_001))
    assert timed.state.target_remaining == 0 and "TIME_1030" in timed.reasons


@pytest.mark.parametrize(
    ("price", "when", "reason"),
    [
        ("10.34", "10:30:00", "TIME_1030"),
        ("9.8", "11:00:00", "TIME_1100"),
        ("10.5", "14:30:00", "FINAL_EXIT"),
    ],
)
def test_time_exit_boundaries(price: str, when: str, reason: str) -> None:
    result = evaluate_exit(lot(), ExitState(1000), tick(price, when))
    assert result.state.target_remaining == 0 and reason in result.reasons


def test_t0_stale_and_market_close_do_not_start_orders() -> None:
    for observation in (
        replace(tick("10"), wall_time=datetime.fromisoformat("2026-09-04T14:30:00+08:00")),
        replace(tick("10"), fresh=False),
        tick("10", "14:55:30"),
        tick("10", "09:29:59"),
    ):
        assert not evaluate_exit(lot(), ExitState(1000), observation).can_start_child_order


def test_sizing_respects_volume_depth_cash_and_board_specific_minimum() -> None:
    capacity = EntryCapacity(Money.parse("10000"), Money.parse("5"), 1000, 10000, 3000, True)
    standard = BuyRule(100, 100, 1000000, Price.parse("0.01"))
    result = size_entry(Price.parse("10.001"), Price.parse("10.1"), standard, capacity)
    assert result.quantity == 300 and result.price == Price.parse("10.01")
    star = BuyRule(200, 1, 100000, Price.parse("0.01"))
    result = size_entry(
        Price.parse("10"), Price.parse("10"), star, replace(capacity, visible_ask_quantity=2050)
    )
    assert result.quantity == 205
    rejected = size_entry(
        Price.parse("10"), Price.parse("10"), star, replace(capacity, visible_ask_quantity=1990)
    )
    assert rejected.reason == "SKIPPED_MIN_ORDER"
    assert size_entry(Price.parse("10"), Price.parse("9"), star, capacity).reason == "PRICE_CEILING"
    assert (
        size_entry(
            Price.parse("10"), Price.parse("10"), star, replace(capacity, path_complete=False)
        ).reason
        == "MARKET_PATH_UNCERTAIN"
    )
