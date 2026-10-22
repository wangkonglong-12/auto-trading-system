from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from auto_trading_system.domain import Price
from auto_trading_system.strategies.exit_v3 import (
    ExitLot,
    ExitObservation,
    ExitState,
    evaluate_exit,
)
from auto_trading_system.strategies.sell_rules import SellRules

CN = timezone(timedelta(hours=8))


def lot(cost: str = "10") -> ExitLot:
    return ExitLot(
        "lot-1",
        100,
        date(2026, 10, 19),
        date(2026, 10, 20),
        Price.parse(cost),
        Price.parse("10"),
        Price.parse("10"),
    )


def quote(price: str, at: str, ns: int = 1) -> ExitObservation:
    hour, minute = map(int, at.split(":"))
    return ExitObservation(
        datetime(2026, 10, 20, hour, minute, tzinfo=CN), ns, Price.parse(price), True, True
    )


RULES = SellRules(take_profit_pct=Decimal("5"), stop_loss_pct=Decimal("3"), sell_by=time(14, 30))


@pytest.mark.parametrize(
    ("price", "time", "reason"),
    [
        ("10.50", "10:00", "TAKE_PROFIT"),
        ("9.70", "10:00", "STOP_LOSS"),
        ("10.00", "14:30", "SELL_BY_TIME"),
    ],
)
def test_three_rules_use_lot_buy_cost(price: str, time: str, reason: str) -> None:
    result = evaluate_exit(lot(), ExitState(100), quote(price, time), RULES)
    assert result.reasons == (reason,)
    assert result.state.target_remaining == 0
    replay = evaluate_exit(lot(), result.state, quote(price, time, 2), RULES)
    assert replay.reasons == ()
    assert replay.state.target_remaining == 0


def test_cost_is_per_lot_and_simultaneous_rules_have_one_target() -> None:
    same_price = quote("10.50", "14:30")
    result = evaluate_exit(lot("10"), ExitState(100), same_price, RULES)
    assert result.reasons == ("TAKE_PROFIT", "SELL_BY_TIME")
    assert result.state.target_remaining == 0
    other = evaluate_exit(lot("11"), ExitState(100), same_price, RULES)
    assert other.reasons == ("STOP_LOSS", "SELL_BY_TIME")


def test_invalid_rules_fail_closed() -> None:
    with pytest.raises(ValueError):
        SellRules(take_profit_pct=Decimal("0"), stop_loss_pct=Decimal("3"), sell_by=time(14, 30))
    with pytest.raises(ValueError):
        SellRules(take_profit_pct=Decimal("5"), stop_loss_pct=Decimal("3"), sell_by=time(14, 55))
    with pytest.raises(ValueError):
        SellRules(take_profit_pct=Decimal("5"), stop_loss_pct=Decimal("3"), sell_by=time(14, 30, 1))
    with pytest.raises(ValueError):
        SellRules(take_profit_pct=Decimal("5"), stop_loss_pct=Decimal("3"), sell_by=time(12))
