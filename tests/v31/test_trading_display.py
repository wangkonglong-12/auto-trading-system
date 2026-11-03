"""Selection-day display rolls at 14:30, independently of broker query dates."""

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from auto_trading_system.apps.local_console.trading_display import TradingDisplay

SESSIONS = [date(2026, 10, 22), date(2026, 10, 23), date(2026, 10, 26), date(2026, 10, 27)]


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, "SELL_MONITORING"),
        ({"enabled": False}, "SELL_BOUND_PAUSED"),
        ({"authorized": False}, "SELL_BOUND_PAUSED"),
        ({"state": "FAILED"}, "SELL_BLOCKED"),
        ({"targets": []}, "SELL_NOT_BOUND"),
        (
            {"targets": [{"lot_id": "lot-a", "symbol": "000001.SZ", "reasons": ["TAKE_PROFIT"]}]},
            "SELL_TRIGGERED",
        ),
    ],
)
def test_retained_buy_rows_show_current_batch_sell_status(
    tmp_path: Path, changes: dict[str, Any], expected: str
) -> None:
    store = display(tmp_path)
    project(store)
    before = store.path.read_bytes()
    details: dict[str, Any] = {
        "available": True,
        "managed_lots": [{"lot_id": "lot-a", "symbol": "000001.SZ", "acquired_on": "2026-10-23"}],
    }
    board = {
        "state": "READY",
        "enabled": True,
        "authorized": True,
        "targets": [{"lot_id": "lot-a", "symbol": "000001.SZ", "reasons": []}],
        **changes,
    }
    engine: dict[str, Any] = {
        "recovery": "CLEAR",
        "halted": False,
        "board_sell": {"restored": True, "boards": {"MAIN": board}},
    }
    result = store.project(
        details, engine, maximum=3, now=datetime.fromisoformat("2026-10-26T10:00:00+08:00")
    )
    assert result["rows"][0]["sell_state"] == expected
    assert store.path.read_bytes() == before
    # Another batch of the same stock must not inherit this row's binding.
    details["managed_lots"][0]["acquired_on"] = "2026-10-22"
    assert (
        store.project(
            details, engine, maximum=3, now=datetime.fromisoformat("2026-10-26T10:00:00+08:00")
        )["rows"][0]["sell_state"]
        == "SELL_UNVERIFIED"
    )
    # An unavailable status must not retain yesterday's running claim.
    assert (
        store.project(
            details, {}, maximum=3, now=datetime.fromisoformat("2026-10-26T10:00:00+08:00")
        )["rows"][0]["sell_state"]
        == "SELL_UNVERIFIED"
    )


def display(root: Path, *, account: str = "account-a") -> TradingDisplay:
    return TradingDisplay(root, node_id="node-a", account_id=account, sessions=SESSIONS)


def project(
    store: TradingDisplay,
    at: str = "2026-10-23T14:46:00+08:00",
    *,
    selection: str | None = "2026-10-23",
    available: bool = True,
    trades: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "available": available,
        "observed_at": at,
        "orders": [],
        "names": {"000001.SZ": "平安银行"},
        "trades": [
            {
                "symbol": "000001.SZ",
                "side": "BUY",
                "owned_by_system": True,
                "quantity": 100,
                "price": "10.01",
                "traded_at": at,
            }
        ]
        if trades
        else [],
        **extra,
    }
    engine: dict[str, Any] = {
        "plan": {"trade_date": selection, "symbols": ["000001.SZ"], "decision_phases": ["1440"]}
        if selection
        else None,
        "market_session": {"entry_targets": [{"symbol": "000001.SZ", "target_quantity": 100}]},
    }
    return store.project(details, engine, maximum=3, now=datetime.fromisoformat(at))


@pytest.mark.parametrize(
    "at",
    [
        "2026-10-23T23:59:59+08:00",
        "2026-10-24T00:00:00+08:00",
        "2026-10-25T14:30:00+08:00",
        "2026-10-26T14:29:59+08:00",
    ],
)
def test_retains_saved_fills_over_weekend_and_restart(tmp_path: Path, at: str) -> None:
    original = project(display(tmp_path))
    result = project(display(tmp_path), at, selection=None, trades=False)
    assert result["rows"] == original["rows"]
    assert result["completed"] == 1
    assert result["display_state"] == "SNAPSHOT"
    assert result["selection_date"] == "2026-10-23"
    assert result["expires_at"] == "2026-10-26T14:30:00+08:00"
    assert result["decision_phases"] == ["1440"]


def test_clears_at_boundary_until_new_selection_then_resets_fills(tmp_path: Path) -> None:
    store = display(tmp_path)
    project(store)
    for selection in (None, "2026-10-23"):
        result = project(store, "2026-10-26T14:30:00+08:00", selection=selection)
        assert result["rows"] == [] and result["completed"] is None
        assert result["decision_phases"] == []
        assert result["display_state"] == "WAITING_SELECTION"
    result = project(store, "2026-10-26T14:40:00+08:00", selection="2026-10-26", trades=False)
    assert result["selection_date"] == "2026-10-26"
    assert result["completed"] == 0
    assert result["rows"] == []
    assert result["display_state"] == "CURRENT"


def test_no_selection_does_not_expose_owned_broker_buys(tmp_path: Path) -> None:
    result = project(display(tmp_path), selection=None)
    assert result["rows"] == [] and result["completed"] is None


def test_before_1430_does_not_show_same_day_plan(tmp_path: Path) -> None:
    result = project(display(tmp_path), "2026-10-23T14:29:59+08:00")
    assert result["rows"] == []
    result = project(display(tmp_path), "2026-10-23T06:30:00+00:00")
    assert result["selection_date"] == "2026-10-23"
    assert len(result["rows"]) == 1


def test_empty_ready_selection_is_saved(tmp_path: Path) -> None:
    store = display(tmp_path)
    now = datetime.fromisoformat("2026-10-23T14:40:00+08:00")
    result = store.project(
        {"available": True, "observed_at": now.isoformat()},
        {"plan": {"trade_date": "2026-10-23", "symbols": [], "decision_phases": ["1440"]}},
        maximum=3,
        now=now,
    )
    assert result["rows"] == [] and result["completed"] == 0
    result = project(display(tmp_path), "2026-10-26T09:30:00+08:00", selection=None)
    assert result["rows"] == [] and result["completed"] == 0
    assert result["display_state"] == "SNAPSHOT"


@pytest.mark.parametrize(
    "extra",
    [
        {"available": False},
        {"truncated": True},
        {"observed_at": "2026-10-22T14:46:00+08:00"},
        {"observed_at": "invalid"},
    ],
)
def test_failed_or_stale_queries_do_not_overwrite_saved_facts(tmp_path: Path, extra: Any) -> None:
    store = display(tmp_path)
    project(store)
    before = store.path.read_bytes()
    result = project(store, trades=False, **extra)
    assert result["rows"][0]["amount"] == "1001.00"
    assert result["display_state"] == "SNAPSHOT"
    assert store.path.read_bytes() == before


def test_account_isolation_and_corrupt_cache(tmp_path: Path) -> None:
    store = display(tmp_path)
    project(store)
    assert project(display(tmp_path, account="other"), selection=None)["rows"] == []
    store.path.write_text("corrupt", encoding="utf-8")
    assert project(store, selection=None)["rows"] == []


def test_holiday_uses_explicit_calendar_and_does_not_guess_next_day(tmp_path: Path) -> None:
    store = TradingDisplay(
        tmp_path,
        node_id="node-a",
        account_id="account-a",
        sessions=[
            date(2026, 10, 23),
            date(2026, 11, 2),
            date(2026, 11, 3),
        ],
    )
    assert project(store)["expires_at"] == "2026-11-02T14:30:00+08:00"
    assert project(store, "2026-10-28T15:00:00+08:00", selection=None)["completed"] == 1
    result = project(store, "2026-11-03T14:30:00+08:00")
    assert result["rows"] == [] and result["display_state"] == "WAITING_SELECTION"
    assert result["calendar_pending"] is True


def test_save_failure_reported_without_claiming_durable_cache(
    tmp_path: Path, monkeypatch: Any
) -> None:
    store = display(tmp_path)
    monkeypatch.setattr(store, "_save", lambda _: False)
    result = project(store)
    assert result["display_state"] == "CURRENT" and result["snapshot_saved"] is False


def test_no_snapshot_and_failed_query_does_not_claim_zero_fills(tmp_path: Path) -> None:
    result = project(display(tmp_path), available=False)
    assert result["completed"] is None and result["available"] is False
    assert result["rows"][0]["state"] == "UNAVAILABLE"


def test_delayed_old_period_query_cannot_overwrite_new_snapshot(tmp_path: Path) -> None:
    store = display(tmp_path)
    project(store, "2026-10-26T14:46:00+08:00", selection="2026-10-26", trades=False)
    before = store.path.read_bytes()
    assert project(store)["rows"] == []
    assert store.path.read_bytes() == before


def test_stale_preview_calendar_is_refreshed_from_qmt_without_changing_config(
    tmp_path: Path,
) -> None:
    configured = [date(2026, 10, 21), date(2026, 10, 22)]
    calls: list[date] = []

    def read_calendar(day: date) -> list[date]:
        calls.append(day)
        return SESSIONS

    store = TradingDisplay(
        tmp_path,
        node_id="node-a",
        account_id="account-a",
        sessions=configured,
        calendar_reader=read_calendar,
    )
    result = project(store)
    assert result["completed"] == 1
    assert result["expires_at"] == "2026-10-26T14:30:00+08:00"
    assert result["calendar_pending"] is False
    project(store)
    assert calls == [date(2026, 10, 23)]
    assert configured == [date(2026, 10, 21), date(2026, 10, 22)]


def test_missing_future_calendar_does_not_block_todays_official_selection(tmp_path: Path) -> None:
    store = TradingDisplay(
        tmp_path,
        node_id="node-a",
        account_id="account-a",
        sessions=[date(2026, 10, 21), date(2026, 10, 22)],
        calendar_reader=lambda _: (),
    )
    result = project(store)
    assert result["completed"] == 1 and result["calendar_pending"] is True
    assert result["selection_date"] == "2026-10-23"
    assert result["expires_at"] is None
    result = project(store, "2026-10-24T09:00:00+08:00")
    assert result["completed"] == 1 and result["display_state"] == "SNAPSHOT"


def test_historical_qmt_calendar_supplies_expiry_on_next_confirmed_session(tmp_path: Path) -> None:
    def historical(day: date) -> list[date]:
        return [item for item in SESSIONS if item <= day]

    store = TradingDisplay(
        tmp_path, node_id="node-a", account_id="account-a", sessions=[], calendar_reader=historical
    )
    assert project(store)["completed"] == 1
    weekend = project(store, "2026-10-25T15:00:00+08:00", selection=None)
    assert weekend["completed"] == 1 and weekend["expires_at"] is None
    morning = project(store, "2026-10-26T09:30:00+08:00", selection=None)
    assert morning["completed"] == 1
    assert morning["expires_at"] == "2026-10-26T14:30:00+08:00"
    rollover = project(store, "2026-10-26T14:30:00+08:00", selection=None)
    assert rollover["rows"] == [] and rollover["decision_phases"] == []


def test_calendar_that_lags_before_open_retries_on_same_day(tmp_path: Path) -> None:
    visible_days = [date(2026, 10, 22), date(2026, 10, 23)]
    store = TradingDisplay(
        tmp_path,
        node_id="node-a",
        account_id="account-a",
        sessions=[],
        calendar_reader=lambda _: visible_days,
    )
    project(store)
    assert project(store, "2026-10-26T08:00:00+08:00", selection=None)["completed"] == 1
    visible_days.append(date(2026, 10, 26))
    result = project(store, "2026-10-26T14:30:00+08:00", selection=None)
    assert result["rows"] == []


def test_calendar_retries_are_bounded_and_recover(tmp_path: Path) -> None:
    responses: list[Any] = [OSError("unavailable"), [], SESSIONS]

    def read_calendar(_: date) -> list[date]:
        value = responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return list(value)

    store = TradingDisplay(
        tmp_path,
        node_id="node-a",
        account_id="account-a",
        sessions=[],
        calendar_reader=read_calendar,
    )
    assert project(store)["completed"] == 1
    project(store, "2026-10-23T14:46:30+08:00")
    assert len(responses) == 2
    project(store, "2026-10-23T14:47:00+08:00")
    result = project(store, "2026-10-23T14:48:00+08:00")
    assert responses == [] and result["calendar_pending"] is False
    assert result["expires_at"] == "2026-10-26T14:30:00+08:00"


def test_legacy_snapshot_excludes_pure_candidates_without_rewriting_saved_facts(
    tmp_path: Path,
) -> None:
    import json

    store = display(tmp_path)
    project(store)
    saved = json.loads(store.path.read_text(encoding="utf-8"))
    saved["buys"]["rows"][0].pop("has_trade_activity", None)
    saved["buys"]["rows"].append(
        {
            "symbol": "000568.SZ",
            "source": "AUTO",
            "state": "NOT_BOUGHT",
            "signal_state": "POSITION_CAPACITY",
            "quantity": 0,
            "amount": None,
        }
    )
    store.path.write_text(json.dumps(saved), encoding="utf-8")
    original = store.path.read_bytes()
    result = project(display(tmp_path), "2026-10-26T09:30:00+08:00", selection=None, trades=False)
    assert [r["symbol"] for r in result["rows"]] == ["000001.SZ"]
    assert result["display_state"] == "SNAPSHOT"
    assert store.path.read_bytes() == original


def test_candidate_only_snapshot_stays_empty_after_restart(tmp_path: Path) -> None:
    first = project(display(tmp_path), trades=False)
    assert first["rows"] == [] and first["not_bought"] == 1
    later = project(display(tmp_path), "2026-10-26T09:30:00+08:00", selection=None, trades=False)
    assert later["rows"] == [] and later["not_bought"] == 1
