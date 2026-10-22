from datetime import date
from pathlib import Path

from auto_trading_system.runtime.daily_risk_file import (
    DailyRiskBaseline,
    load_daily_risk_baseline,
    save_daily_risk_baseline,
)


def test_daily_risk_baseline_is_create_once_and_scoped(tmp_path: Path) -> None:
    path = tmp_path / "daily-risk.json"
    first = DailyRiskBaseline(
        account_id="qmt-account",
        trade_date=date(2026, 10, 20),
        total_equity="100000.00",
    )
    changed = first.model_copy(update={"total_equity": "90000.00"})

    assert save_daily_risk_baseline(path, first) == first
    assert save_daily_risk_baseline(path, changed) == first
    assert (
        load_daily_risk_baseline(path, account_id="qmt-account", trade_date=date(2026, 10, 20))
        == first
    )
    assert (
        load_daily_risk_baseline(path, account_id="qmt-account", trade_date=date(2026, 10, 21))
        is None
    )

    next_day = DailyRiskBaseline(
        account_id="qmt-account",
        trade_date=date(2026, 10, 21),
        total_equity="91000.00",
    )
    assert save_daily_risk_baseline(path, next_day) == next_day
    assert (
        load_daily_risk_baseline(path, account_id="qmt-account", trade_date=date(2026, 10, 21))
        == next_day
    )
