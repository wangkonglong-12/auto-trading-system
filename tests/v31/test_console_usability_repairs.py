from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from auto_trading_system.apps.desktop.__main__ import failure_message, trading_day_changed
from auto_trading_system.apps.local_console.node_app import (
    SaveTradingSettingsInput,
    _settings_validation_error,
)
from auto_trading_system.protocol.v2.canonical import canonical_bytes


def valid_settings() -> dict[str, Any]:
    return {
        "buy": {
            "order_method": "ASK1_PROTECTED_LIMIT",
            "max_daily_buy_value": "50000",
            "max_symbol_buy_value": "10000",
            "max_order_value": "5000",
            "cancel_after_seconds": 10,
            "max_retries": 3,
        },
        "sell": {"take_profit_pct": "5", "stop_loss_pct": "3", "sell_by": "14:30"},
        "safety": {
            "max_daily_loss": "2000",
            "max_positions": 5,
            "emergency_liquidation_enabled": False,
        },
        "advanced": {
            "max_order_quantity": 10000,
            "max_chase_bps": 30,
            "max_spread_bps": 20,
            "quote_max_age_ms": 1000,
        },
    }


@pytest.mark.parametrize(
    "group,field,value,message",
    [
        ("buy", "max_daily_buy_value", "0", "三个买入金额"),
        ("buy", "max_order_value", "20000", "单笔委托最多 ≤"),
        ("buy", "max_daily_buy_value", 50000, "当日最多买入"),
        ("buy", "max_daily_buy_value", "secret-never-echo", "当日最多买入"),
        ("buy", "cancel_after_seconds", 0, "未成交等待"),
        ("buy", "max_retries", 4, "重新下单次数"),
        ("sell", "take_profit_pct", "0", "止盈涨幅"),
        ("sell", "stop_loss_pct", "100", "止损跌幅"),
        ("sell", "sell_by", "12:00", "最晚卖出时间"),
        ("safety", "max_positions", 0, "都必须大于0"),
        ("safety", "max_daily_loss", "0", "都必须大于0"),
        ("advanced", "max_order_quantity", 99, "单笔最大股数"),
        ("advanced", "max_chase_bps", -1, "最大追价"),
        ("advanced", "max_spread_bps", 1001, "最大买卖价差"),
        ("advanced", "quote_max_age_ms", 99, "行情有效期"),
    ],
)
def test_validation_reports_correct_fields_without_echoing_input(
    group: str, field: str, value: object, message: str
) -> None:
    settings = valid_settings()
    settings[group][field] = value
    with pytest.raises(ValidationError) as captured:
        SaveTradingSettingsInput.model_validate_json(
            canonical_bytes({"expected_version": 0, "settings": settings})
        )
    response = _settings_validation_error(captured.value)
    assert response.status_code == 400
    body = bytes(response.body).decode()
    assert message in body
    assert "secret-never-echo" not in body
    assert '"issues"' in body


def test_rollover_uses_china_date_instead_of_treating_clean_shutdown_as_failure() -> None:
    assert not trading_day_changed(
        date(2026, 10, 21), now=datetime(2026, 10, 21, 15, 59, tzinfo=UTC)
    )
    assert trading_day_changed(date(2026, 10, 21), now=datetime(2026, 10, 21, 16, 0, tzinfo=UTC))
    assert "后台进程已退出" in failure_message(RuntimeError("DESKTOP_CHILD_EXITED"))
    assert "secret-input" not in failure_message(RuntimeError("secret-input"))
