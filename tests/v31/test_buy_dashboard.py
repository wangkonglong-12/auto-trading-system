"""UI counts must be supported by real, strategy-owned broker fills."""

from copy import deepcopy
from typing import Any

import pytest

from auto_trading_system.apps.local_console.buy_dashboard import buy_dashboard, transaction_rows


def sample() -> tuple[dict[str, Any], dict[str, Any]]:
    details = {
        "available": True,
        "observed_at": "2026-10-23T14:46:00+08:00",
        "names": {"000001.SZ": "平安银行"},
        "orders": [
            {"symbol": "000002.SZ", "side": "BUY", "status": "ACKED", "owned_by_system": True}
        ],
        "trades": [
            {
                "symbol": "000001.SZ",
                "side": "BUY",
                "owned_by_system": True,
                "quantity": 100,
                "price": "10.01",
                "amount": "1001.00",
                "traded_at": "2026-10-23T14:40:03+08:00",
            },
            {
                "symbol": "000001.SZ",
                "side": "BUY",
                "owned_by_system": True,
                "quantity": 200,
                "price": "10.03",
                "amount": "2006.00",
                "traded_at": "2026-10-23T14:40:04+08:00",
            },
            {
                "symbol": "600000.SH",
                "side": "BUY",
                "owned_by_system": False,
                "quantity": 100,
                "price": "9",
                "traded_at": "2026-10-23T14:40:01+08:00",
            },
        ],
    }
    engine = {
        "plan": {"symbols": ["000001.SZ", "000002.SZ", "000003.SZ"]},
        "market_session": {
            "entry_targets": [{"symbol": "000001.SZ", "target_quantity": 300}],
            "entry_progress": [{"symbol": "000003.SZ", "state": "WAITING_MARKET"}],
        },
    }
    return details, engine


def test_counts_unique_symbols_and_uses_weighted_actual_fill_price() -> None:
    details, engine = sample()
    result = buy_dashboard(details, engine, maximum=3)
    assert (result["completed"], result["pending"], result["not_bought"], result["remaining"]) == (
        1,
        1,
        1,
        1,
    )
    rows: Any = result["rows"]
    assert len(rows) == 3
    assert rows[0]["average_price"] == "10.0233"
    assert rows[0]["amount"] == "3007.00"
    assert rows[0]["bought_at"].endswith("14:40:03+08:00")
    assert rows[0]["name"] == "平安银行"


def test_partial_fill_occupies_slot_but_does_not_count_as_completed() -> None:
    details, engine = sample()
    details["trades"] = details["trades"][:1]
    result = buy_dashboard(details, engine, maximum=3)
    assert result["completed"] == 0
    assert result["partial"] == 1
    assert result["occupied"] == 2


@pytest.mark.parametrize("field", ["available", "truncated"])
def test_unavailable_or_incomplete_query_never_claims_zero_or_complete(field: str) -> None:
    details, engine = sample()
    details[field] = field == "truncated"
    result = buy_dashboard(details, engine, maximum=3)
    assert result["available"] is False
    assert result["completed"] is None and result["remaining"] is None
    rows: Any = result["rows"]
    assert all(row["amount"] is None and row["state"] == "UNAVAILABLE" for row in rows)


def test_zero_is_displayed_only_after_successful_empty_query_and_no_fake_name() -> None:
    result = buy_dashboard({"available": True, "orders": [], "trades": []}, {}, maximum=None)
    assert result["completed"] == 0 and result["remaining"] is None
    assert result["rows"] == []


def test_projection_does_not_mutate_broker_facts() -> None:
    details, engine = sample()
    before = deepcopy((details, engine))
    buy_dashboard(details, engine, maximum=1)
    assert (details, engine) == before


def test_transaction_rows_exclude_candidates_without_changing_selection_counts() -> None:
    details, engine = sample()
    engine["market_session"]["entry_targets"].append(
        {"symbol": "000003.SZ", "target_quantity": 0, "reason": "POSITION_CAPACITY"}
    )
    result = buy_dashboard(details, engine, maximum=1)
    assert [row["symbol"] for row in transaction_rows(result["rows"])] == ["000001.SZ", "000002.SZ"]
    assert result["not_bought"] == 1 and result["occupied"] == 2


@pytest.mark.parametrize("status", ["CANCELLED", "REJECTED"])
def test_zero_fill_automatic_order_is_a_transaction(status: str) -> None:
    details = {
        "available": True,
        "orders": [
            {"symbol": "000001.SZ", "side": "BUY", "status": status, "owned_by_system": True}
        ],
    }
    result = buy_dashboard(details, {}, maximum=1)
    rows = transaction_rows(result["rows"])
    assert len(rows) == 1 and rows[0]["state"] == status and rows[0]["quantity"] == 0


def test_unconfirmed_submission_stays_visible_without_broker_order() -> None:
    result = buy_dashboard(
        {
            "available": True,
            "submissions": [
                {"symbol": "000001.SZ", "client_order_id": "ord_uncertain", "status": "UNCERTAIN"}
            ],
        },
        {},
        maximum=1,
    )
    assert transaction_rows(result["rows"])[0]["state"] == "UNKNOWN"


@pytest.mark.parametrize("status", ["CANCELLED", "REJECTED"])
def test_zero_fill_manual_terminal_counts_once_as_not_bought(status: str) -> None:
    details: dict[str, Any] = {
        "available": True,
        "orders": [
            {
                "client_order_id": "ord_manual0001",
                "symbol": "003021.SZ",
                "side": "BUY",
                "status": status,
                "owned_by_system": True,
                "filled": 0,
            },
            {
                "client_order_id": "ord_manual0002",
                "symbol": "003021.SZ",
                "side": "BUY",
                "status": status,
                "owned_by_system": True,
                "filled": 0,
            },
        ],
        "submissions": [
            {
                "client_order_id": "ord_manual0001",
                "symbol": "003021.SZ",
                "source": "MANUAL",
                "quantity": 100,
            },
            {
                "client_order_id": "ord_manual0002",
                "symbol": "003021.SZ",
                "source": "MANUAL",
                "quantity": 100,
            },
        ],
        "trades": [],
    }
    result = buy_dashboard(details, {}, maximum=3)
    assert result["not_bought"] == 1 and result["occupied"] == 0
    rows = result["rows"]
    assert isinstance(rows, list) and len(rows) == 1
