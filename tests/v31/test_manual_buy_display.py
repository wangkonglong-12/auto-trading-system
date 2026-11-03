from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from auto_trading_system.apps.local_console.trading_display import TradingDisplay


def display(root: Any) -> Any:
    return TradingDisplay(
        root,
        node_id="node-a",
        account_id="account-a",
        sessions=[date(2026, 10, 23), date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28)],
    )


def project(
    store: TradingDisplay,
    at: str = "2026-10-26T10:00:00+08:00",
    status: str = "ACKED",
    filled: int = 0,
    available: bool = True,
) -> Any:
    day = at[:10]
    details: dict[str, Any] = {
        "available": available,
        "observed_at": at,
        "trade_date": day,
        "names": {"000001.SZ": "平安银行"},
        "submissions": [
            {
                "client_order_id": "ord_manual0001",
                "symbol": "000001.SZ",
                "source": "MANUAL",
                "quantity": 100,
                "limit_price": "10",
                "trade_date": day,
                "name": "平安银行",
                "submitted_at": at,
                "status": "SDK_ACCEPTED",
            }
        ],
        "orders": [
            {
                "client_order_id": "ord_manual0001",
                "symbol": "000001.SZ",
                "side": "BUY",
                "owned_by_system": True,
                "quantity": 100,
                "filled": filled,
                "status": status,
            }
        ],
        "trades": [
            {
                "client_order_id": "ord_manual0001",
                "symbol": "000001.SZ",
                "side": "BUY",
                "owned_by_system": True,
                "quantity": filled,
                "price": "9.99",
                "traded_at": at,
            }
        ]
        if filled
        else [],
    }
    return store.project(
        details,
        {"trade_date": day, "recovery": "CLEAR", "plan": None},
        maximum=3,
        now=datetime.fromisoformat(at),
    )


def test_manual_without_selection_shows_before_1430_and_survives_rollover(tmp_path: Path) -> None:
    store = display(tmp_path)
    first = project(store)
    assert first["rows"][0]["source"] == "MANUAL" and first["rows"][0]["state"] == "PENDING"
    assert first["rows"][0]["bought_at"] is None
    assert first["today_summary"]["remaining"] == 2
    after = project(store, "2026-10-26T14:30:00+08:00", "FILLED", 100)
    assert after["rows"][0]["state"] == "FILLED"
    assert after["rows"][0]["average_price"] == "9.9900"
    assert after["rows"][0]["sell_state"] == "RULES_NOT_CONFIGURED"
    assert after["today_summary"]["completed"] == 1
    restarted = project(display(tmp_path), "2026-10-26T15:30:00+08:00", available=False)
    assert restarted["rows"][0]["state"] == "FILLED"
    assert restarted["rows"][0]["is_snapshot"] is True
    assert restarted["today_summary"]["remaining"] is None


def test_same_symbol_on_different_days_is_not_merged_and_today_counts_ignore_yesterday(
    tmp_path: Path,
) -> None:
    store = display(tmp_path)
    project(store, status="FILLED", filled=100)
    next_day = project(store, "2026-10-27T10:00:00+08:00")
    assert len(next_day["rows"]) == 2
    assert {r["trade_date"] for r in next_day["rows"]} == {"2026-10-26", "2026-10-27"}
    assert next_day["today_summary"]["completed"] == 0
    assert next_day["today_summary"]["remaining"] == 2
    rolled = project(store, "2026-10-27T14:30:00+08:00")
    assert len(rolled["rows"]) == 1 and rolled["rows"][0]["trade_date"] == "2026-10-27"


@pytest.mark.parametrize(
    "broker,filled,expected,occupied",
    [
        ("REJECTED", 0, "REJECTED", 0),
        ("CANCELLED", 0, "CANCELLED", 0),
        ("PARTIALLY_FILLED_CANCELLED", 40, "PARTIALLY_FILLED_CANCELLED", 1),
        ("PARTIALLY_FILLED", 40, "PARTIAL", 1),
    ],
)
def test_manual_status_comes_from_orders_without_strategy_target(
    tmp_path: Path, broker: str, filled: int, expected: str, occupied: int
) -> None:
    result = project(display(tmp_path), status=broker, filled=filled)
    assert result["rows"][0]["state"] == expected
    assert result["today_summary"]["occupied"] == occupied


def test_pending_intent_is_visible_before_broker_returns_order(tmp_path: Path) -> None:
    from auto_trading_system.apps.local_console.buy_dashboard import buy_dashboard

    data: dict[str, Any] = {
        "available": True,
        "trade_date": "2026-10-26",
        "submissions": [
            {
                "client_order_id": "ord_manual0001",
                "symbol": "000001.SZ",
                "source": "MANUAL",
                "name": "平安银行",
                "quantity": 100,
                "status": "PERSISTING",
            }
        ],
        "orders": [],
        "trades": [],
    }
    result: Any = buy_dashboard(data, {}, maximum=3)
    assert result["rows"][0]["state"] == "PENDING"
    assert result["rows"][0]["amount"] is None
    assert result["remaining"] == 2
