"""Display collection is bounded and cannot call the trading control path."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from test_tail_feed import NOW, wire

from auto_trading_system.apps.local_console.trading_process import (
    MAX_BYTES,
    MAX_EVENTS,
    MAX_VISIBLE,
    TradingProcessLog,
)


def status() -> dict[str, Any]:
    return {
        "components": {
            "engine": {
                "availability": "CURRENT",
                "data": {
                    "boot_id": "boot-a",
                    "trade_date": NOW.date().isoformat(),
                    "recovery": "CLEAR",
                    "daily_auto": {"enabled": True, "state": "WAITING", "reason": "WAITING_WINDOW"},
                    "market_session": {"recent_activity": []},
                },
            },
            "agent": {
                "availability": "CURRENT",
                "data": {
                    "invocation_id": "tail-feed-test",
                    "status": "IDLE",
                    "last_result": {"status": "WAITING_PHASES"},
                },
            },
        }
    }


def log(
    root: Path, source: dict[str, Any] | None = None, clock: Any = None, elapsed: Any = None
) -> TradingProcessLog:
    value = source if source is not None else status()
    return TradingProcessLog(
        root,
        node_id="node-a",
        account_id="account-a",
        status=lambda: value,
        clock=clock or (lambda: NOW),
        elapsed=elapsed or (lambda: 0.0),
    )


def selection(root: Path) -> None:
    feed = wire()
    feed["batches"][0]["stocks"] = [
        {"code": "003021.SZ", "name": "兆威机电", "rank": 1},
        {"code": "000001.SZ", "name": "平安银行", "rank": 2},
        {"code": "600580.SH", "name": "卧龙电驱", "rank": 3},
    ]
    path = root / "critical/tail-feed-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"feed": feed, "suspended": False}), encoding="utf-8")


def page() -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        {
            "available": True,
            "coherent": True,
            "trade_date": NOW.date().isoformat(),
            "observed_at": NOW.isoformat(),
            "names": {"003021.SZ": "兆威机电"},
            "submissions": [
                {
                    "client_order_id": "ord_example0001",
                    "symbol": "003021.SZ",
                    "name": "兆威机电",
                    "status": "SDK_ACCEPTED",
                }
            ],
            "orders": [
                {
                    "client_order_id": "ord_example0001",
                    "symbol": "003021.SZ",
                    "owned_by_system": True,
                    "status": "FILLED",
                    "quantity": 100,
                    "filled": 100,
                    "limit_price": "94",
                    "cancel_state": "NONE",
                }
            ],
            "trades": [
                {
                    "trade_id": "trade_example0001",
                    "symbol": "003021.SZ",
                    "side": "BUY",
                    "quantity": 100,
                    "price": "93.9",
                    "owned_by_system": True,
                }
            ],
        },
        {
            "available": True,
            "display_state": "CURRENT",
            "selection_date": NOW.date().isoformat(),
            "rows": [
                {
                    "symbol": "003021.SZ",
                    "name": "兆威机电",
                    "trade_date": NOW.date().isoformat(),
                    "state": "FILLED",
                    "quantity": 100,
                    "average_price": "93.9",
                    "amount": "9390",
                }
            ],
        },
    )


def test_selection_lists_every_stock_and_caches_unchanged_files(
    tmp_path: Path, monkeypatch: Any
) -> None:
    selection(tmp_path)
    history = log(tmp_path)
    reads: list[Path] = []
    read = history._read

    def counted(path: Path, maximum: int) -> dict[str, Any]:
        reads.append(path)
        return read(path, maximum)

    monkeypatch.setattr(history, "_read", counted)
    history.collect()
    first: Any = history.snapshot()
    assert first["stocks"] == [
        {"symbol": "003021.SZ", "name": "兆威机电"},
        {"symbol": "000001.SZ", "name": "平安银行"},
        {"symbol": "600580.SH", "name": "卧龙电驱"},
    ]
    assert any("共3只股票" in e["message"] for e in first["events"])
    assert len([e for e in first["events"] if "批次入选" in e["message"]]) == 3
    history.collect()
    assert history.snapshot() == first
    assert reads.count(history.feed_path) == 1


def test_success_requires_actual_fills_and_has_stock_identity(tmp_path: Path) -> None:
    history = log(tmp_path)
    details, buys = page()
    incomplete = deepcopy(buys)
    incomplete["rows"][0].update(state="PENDING", quantity=0)
    history.offer(details, incomplete)
    history.collect()
    view: Any = history.snapshot()
    assert not any(e["level"] == "SUCCESS" for e in view["events"])
    assert any("全部成交，等待成交核对" in e["message"] for e in view["events"])
    history.offer(details, buys)
    history.collect()
    view = history.snapshot()
    successes = [e for e in view["events"] if e["level"] == "SUCCESS"]
    assert len(successes) == 1 and successes[0]["symbol"] == "003021.SZ"
    assert successes[0]["name"] == "兆威机电" and "9390" in successes[0]["message"]
    assert all(e["name"] == "兆威机电" for e in view["events"] if e["symbol"] == "003021.SZ")


@pytest.mark.parametrize("channel", ["connection", "account"])
def test_short_read_flaps_do_not_displace_trade_events(tmp_path: Path, channel: str) -> None:
    source, tick = status(), [0.0]
    history = log(tmp_path, source, elapsed=lambda: tick[0])
    details, buys = page()
    history.offer(details, buys)
    history.collect()
    original = history.snapshot()["events"]
    for i in range(100):
        tick[0] += 2
        available = i % 2 == 1
        if channel == "connection":
            source["components"]["engine"]["availability"] = (
                "CURRENT" if available else "UNAVAILABLE"
            )
        else:
            details["available"] = available
        history.offer(details, buys)
        history.collect()
        view = history.snapshot()
        assert bool(view["warning"]) is not available
        assert view["events"] == original
    assert any(e["level"] == "SUCCESS" for e in original)


@pytest.mark.parametrize("channel", ["connection", "account"])
def test_read_outage_and_stable_recovery_are_logged_once(tmp_path: Path, channel: str) -> None:
    source, tick = status(), [0.0]
    history = log(tmp_path, source, elapsed=lambda: tick[0])
    details, buys = page()

    def sample(seconds: float, available: bool) -> list[dict[str, Any]]:
        tick[0] = seconds
        if channel == "connection":
            source["components"]["engine"]["availability"] = (
                "CURRENT" if available else "UNAVAILABLE"
            )
        else:
            details["available"] = available
        history.offer(details, buys)
        history.collect()
        return [
            e
            for e in history.snapshot()["events"]
            if "交易引擎状态" in e["message"]
            or ("账户" in e["message"] and "核验" not in e["message"])
        ]

    initial = len(sample(0, True))
    assert len(sample(1, False)) == initial
    assert len(sample(5.9, False)) == initial
    assert len(sample(6, False)) == initial + 1
    assert sample(10, False)[-1]["level"] == "ERROR"
    assert len(sample(12, True)) == initial + 1
    assert "正在确认稳定性" in str(history.snapshot()["warning"])
    assert len(sample(14, False)) == initial + 1
    assert len(sample(20, True)) == initial + 1
    assert len(sample(24.9, True)) == initial + 1
    assert len(sample(25, True)) == initial + 2
    assert sample(26, True)[-1]["level"] == "INFO"
    assert history.snapshot()["warning"] is None
    # A later independent outage still deserves a new alert.
    assert len(sample(27, False)) == initial + 2
    assert len(sample(32, False)) == initial + 3


@pytest.mark.parametrize(
    ("availability", "error", "message"),
    [
        ("UNAVAILABLE", "PROBE_FAILED", "页面暂时无法读取交易引擎状态"),
        ("STALE", None, "交易引擎状态已过期"),
        ("UNAVAILABLE", "PROGRESS_REGRESSED", "交易引擎状态进度异常"),
    ],
)
def test_read_alert_identifies_display_failure_without_claiming_broker_disconnect(
    tmp_path: Path, availability: str, error: str | None, message: str
) -> None:
    source, tick = status(), [0.0]
    source["components"]["engine"].update(availability=availability, error=error)
    history = log(tmp_path, source, elapsed=lambda: tick[0])
    history.collect()
    assert history.snapshot()["events"] == []
    assert message in str(history.snapshot()["warning"])
    tick[0] = 5
    history.collect()
    events = history.snapshot()["events"]
    assert len(events) == 1 and events[0]["level"] == "ERROR"
    assert message in events[0]["message"]
    assert "记录可能不完整" not in str(history.snapshot())


def test_pending_display_read_does_not_delay_real_order_errors(tmp_path: Path) -> None:
    source = status()
    source["components"]["engine"]["availability"] = "UNAVAILABLE"
    history = log(tmp_path, source)
    details, buys = page()
    details["orders"][0].update(cancel_state="FAILED", cancel_reason="QMT_CANCEL_ERROR_1")
    history.offer(details, buys)
    history.collect()
    events = history.snapshot()["events"]
    assert any(e["level"] == "ERROR" and "撤单失败" in e["message"] for e in events)
    assert any(e["level"] == "SUCCESS" for e in events)
    source["components"]["engine"]["availability"] = "CURRENT"
    source["components"]["engine"]["data"]["market_session"]["failure_code"] = "QMT_DISCONNECTED"
    history.collect()
    assert any(
        "行情处理异常" in e["message"] and "QMT_DISCONNECTED" in e["message"]
        for e in history.snapshot()["events"]
    )


def test_confirmed_read_outage_is_not_duplicated_on_console_restart(tmp_path: Path) -> None:
    source, tick = status(), [0.0]
    source["components"]["engine"]["availability"] = "UNAVAILABLE"
    history = log(tmp_path, source, elapsed=lambda: tick[0])
    history.collect()
    tick[0] = 5
    history.collect()
    original = history.snapshot()["events"]
    tick[0] = 0
    restored = log(tmp_path, source, elapsed=lambda: tick[0])
    restored.collect()
    assert restored.snapshot()["events"] == original
    assert restored.snapshot()["warning"]
    source["components"]["engine"]["availability"] = "CURRENT"
    restored.collect()
    assert not any("读取正常" in e["message"] for e in restored.snapshot()["events"])
    tick[0] = 5
    restored.collect()
    assert sum("读取正常" in e["message"] for e in restored.snapshot()["events"]) == 1


def test_new_day_restarts_display_read_confirmation(tmp_path: Path) -> None:
    source, tick, clock = status(), [0.0], [NOW]
    source["components"]["engine"]["availability"] = "UNAVAILABLE"
    history = log(tmp_path, source, lambda: clock[0], lambda: tick[0])
    history.offer({"available": False}, {})
    history.collect()
    assert "账户数据" in str(history.snapshot()["warning"])
    clock[0] += timedelta(days=1)
    tick[0] = 6
    history.collect()
    assert history.snapshot()["events"] == []
    assert "账户数据" not in str(history.snapshot()["warning"])
    tick[0] = 11
    history.collect()
    assert len(history.snapshot()["events"]) == 1


@pytest.mark.parametrize("case", ["stale", "snapshot", "truncated", "incoherent", "unavailable"])
def test_unconfirmed_observation_never_becomes_blue_success(tmp_path: Path, case: str) -> None:
    history = log(tmp_path)
    details, buys = page()
    if case == "stale":
        details["observed_at"] = (NOW - timedelta(seconds=16)).isoformat()
    if case == "snapshot":
        buys["display_state"] = "SNAPSHOT"
    if case == "truncated":
        details["truncated"] = True
    if case == "incoherent":
        details["coherent"] = False
    if case == "unavailable":
        details["available"] = False
    history.offer(details, buys)
    history.collect()
    view: Any = history.snapshot()
    assert not any(e["level"] == "SUCCESS" for e in view["events"])


def test_cancel_errors_are_red_and_raw_private_exceptions_never_render(tmp_path: Path) -> None:
    source = status()
    source["components"]["engine"]["data"]["recovery_error"] = "private credential path"
    history = log(tmp_path, source)
    details, buys = page()
    details["orders"][0].update(cancel_state="FAILED", cancel_reason="QMT_CANCEL_ERROR_1")
    buys["rows"][0].update(state="PARTIAL", quantity=40)
    history.offer(details, buys)
    history.collect()
    view: Any = history.snapshot()
    assert any(
        e["level"] == "ERROR" and e["symbol"] == "003021.SZ" and "撤单失败" in e["message"]
        for e in view["events"]
    )
    assert "private credential" not in json.dumps(view)


def test_bounded_history_and_restore_without_duplicate_events(tmp_path: Path) -> None:
    history = log(tmp_path)
    with history._lock:
        for i in range(MAX_EVENTS + 50):
            history._emit("progress", i, f"进度{i}", NOW, symbol="003021.SZ")
    history.flush()
    saved: Any = history.snapshot()
    assert saved["retained"] == MAX_EVENTS and len(saved["events"]) == MAX_VISIBLE
    assert history.path.stat().st_size < MAX_BYTES
    restored: Any = log(tmp_path).snapshot()
    assert restored["stream_id"] == saved["stream_id"] and restored["events"] == saved["events"]


def test_latest_only_offer_is_nonblocking_and_storage_failure_is_display_only(
    tmp_path: Path, monkeypatch: Any
) -> None:
    history = log(tmp_path)
    details, buys = page()
    # Offering page observations performs no source lookup or disk writes.
    calls: list[str] = []

    def cached_status() -> dict[str, Any]:
        calls.append("cached-status")
        return status()

    history.status = cached_status
    history.offer(details, buys)
    for _ in range(10):
        history.offer(details, buys)
    assert calls == []

    def failed_replace(*args: Any) -> None:
        raise OSError("private storage failure")

    monkeypatch.setattr(
        "auto_trading_system.apps.local_console.trading_process.os.replace", failed_replace
    )
    history.collect()
    view: Any = history.snapshot()
    assert len(calls) == 1 and view["warning"] is not None
    assert any(e["level"] == "SUCCESS" for e in view["events"])
    assert "private storage" not in str(view)
    assert not list(history.path.parent.glob("*.tmp"))


def test_next_day_and_different_account_do_not_reuse_old_history(tmp_path: Path) -> None:
    clock = [NOW]
    history = log(tmp_path, clock=lambda: clock[0])
    details, buys = page()
    history.offer(details, buys)
    history.collect()
    first: Any = history.snapshot()
    other = TradingProcessLog(
        tmp_path, node_id="node-a", account_id="different-account", status=status, clock=lambda: NOW
    )
    assert other.snapshot()["events"] == []
    clock[0] += timedelta(days=1)
    history.collect()
    later: Any = history.snapshot()
    assert later["stream_id"] != first["stream_id"] and later["stocks"] == []
    assert not any(e["level"] == "SUCCESS" for e in later["events"])


@pytest.mark.parametrize("body", ["not JSON", '{"feed": {}}'])
def test_bad_selection_file_cannot_block_other_trade_records(tmp_path: Path, body: str) -> None:
    history = log(tmp_path)
    history.feed_path.parent.mkdir(parents=True)
    history.feed_path.write_text(body, encoding="utf-8")
    history.offer(*page())
    history.collect()
    view: Any = history.snapshot()
    assert any(e["level"] == "SUCCESS" for e in view["events"])
    assert view["warning"] or any("选股记录格式异常" in e["message"] for e in view["events"])


def test_old_activity_does_not_add_yesterdays_stock_to_today(tmp_path: Path) -> None:
    source = status()
    source["components"]["engine"]["data"]["market_session"]["recent_activity"] = [
        {
            "category": "ORDER",
            "state": "FILLED",
            "symbol": "003021.SZ",
            "name": "兆威机电",
            "occurred_at": (NOW - timedelta(days=1)).isoformat(),
        }
    ]
    history = log(tmp_path, source)
    history.collect()
    assert history.snapshot()["stocks"] == []


def test_terminal_unbought_result_is_explicit_and_not_blue(tmp_path: Path) -> None:
    history = log(tmp_path)
    details, buys = page()
    buys["rows"][0].update(state="NOT_BOUGHT", quantity=0, signal_state="WINDOW_CLOSED")
    details.update(orders=[], trades=[], submissions=[])
    history.offer(details, buys)
    history.collect()
    view: Any = history.snapshot()
    assert any(
        e["symbol"] == "003021.SZ" and "买入结果" in e["message"] and "未买入" in e["message"]
        for e in view["events"]
    )
    assert not any(e["level"] == "SUCCESS" for e in view["events"])


def test_closing_reference_is_recorded_once_without_claiming_buy_success(tmp_path: Path) -> None:
    history = log(tmp_path)
    history.closing_path.parent.mkdir(parents=True)
    history.closing_path.write_text(
        json.dumps({"trade_date": NOW.date().isoformat(), "payload_hash": "sha256:" + "a" * 64}),
        encoding="utf-8",
    )
    history.collect()
    history.collect()
    view: Any = history.snapshot()
    assert len([e for e in view["events"] if "封存记录已保存" in e["message"]]) == 1
    assert not any(e["level"] == "SUCCESS" for e in view["events"])


def test_repeated_phase_timestamps_do_not_displace_confirmed_success(tmp_path: Path) -> None:
    source = status()
    market = source["components"]["engine"]["data"]["market_session"]
    clock = [NOW]
    history = log(tmp_path, source, lambda: clock[0])
    history.offer(*page())
    for i in range(100):
        clock[0] = NOW + timedelta(milliseconds=i)
        market["recent_activity"] = [
            {
                "category": "ENTRY_SIGNAL",
                "context": "phase-" + phase,
                "phase": phase,
                "state": state,
                "symbol": "003021.SZ",
                "occurred_at": clock[0].isoformat(),
            }
            for phase, state in [
                ("1440", "FINISHED"),
                ("1445", "DUPLICATE_SIGNAL"),
                ("1450", "DUPLICATE_SIGNAL"),
            ]
        ]
        history.collect()
    events = history.snapshot()["events"]
    assert len([e for e in events if e["level"] == "SUCCESS"]) == 1
    assert len([e for e in events if "本批买入目标执行结束" in e["message"]]) == 1
    assert len([e for e in events if "重复入选" in e["message"]]) == 2
    assert not any("买入目标执行结束，等待" in e["message"] for e in events)
    assert len(events) < 20
    before = history.path.read_bytes()
    history.collect()
    assert history.path.read_bytes() == before
    # New real order changes and errors must remain visible after terminal progress.
    market["recent_activity"] = [
        {
            "category": "ORDER",
            "state": "CANCELLED",
            "symbol": "003021.SZ",
            "detail": "BUY:0/100",
            "occurred_at": NOW.isoformat(),
        },
        {
            "category": "ERROR",
            "state": "TEST_FAILURE",
            "symbol": "003021.SZ",
            "occurred_at": NOW.isoformat(),
        },
    ]
    history.collect()
    assert any(e["level"] == "ERROR" for e in history.snapshot()["events"])
    assert any("未成交委托已撤销" in e["message"] for e in history.snapshot()["events"])
    # Semantic de-duplication survives a console restart.
    restored = log(tmp_path, source, lambda: clock[0])
    count = restored.snapshot()["retained"]
    restored.collect()
    assert restored.snapshot()["retained"] == count


def test_day_end_is_printed_once_and_later_faults_are_still_reported(tmp_path: Path) -> None:
    source = status()
    automatic = source["components"]["engine"]["data"]["daily_auto"]
    automatic.update(state="WAITING", reason="WAITING_NEXT_DAY")
    history = log(tmp_path, source)
    for _ in range(20):
        history.collect()
    assert sum("今日自动买入已结束" in e["message"] for e in history.snapshot()["events"]) == 1
    automatic.update(state="FAULT", reason="ACCOUNT_QUERY_FAILED")
    history.collect()
    assert history.snapshot()["events"][-1]["level"] == "ERROR"


def test_identical_orders_keep_distinct_submission_identity(tmp_path: Path) -> None:
    source = status()
    source["components"]["engine"]["data"]["market_session"]["recent_activity"] = [
        {
            "category": "ORDER",
            "context": client_id,
            "state": "ACKED",
            "symbol": "003021.SZ",
            "detail": "BUY:0/100",
            "occurred_at": NOW.isoformat(),
        }
        for client_id in ["order-a", "order-b"]
    ]
    history = log(tmp_path, source)
    history.collect()
    assert sum("券商已接收委托" in e["message"] for e in history.snapshot()["events"]) == 2


def test_old_spam_is_collapsed_without_dropping_success_errors_or_sequence(tmp_path: Path) -> None:
    history = log(tmp_path)
    history._stock("003021.SZ", "兆威机电")
    for i in range(100):
        history._emit(
            "old-signal", i, "买入目标执行结束，等待实际成交核对", NOW, symbol="003021.SZ"
        )
        history._emit("old-duplicate", i, "重复入选，沿用当天原买入目标", NOW, symbol="003021.SZ")
    history._emit("real-success", True, "买入成功，实际成交100股", NOW, "SUCCESS", "003021.SZ")
    history._emit("real-error", True, "真实异常", NOW, "ERROR", "003021.SZ")
    history.flush()
    source = history.path.read_bytes()
    restored = log(tmp_path)
    events = restored.snapshot()["events"]
    assert len(events) == 4
    assert {e["level"] for e in events} == {"INFO", "SUCCESS", "ERROR"}
    assert not any("等待实际成交核对" in e["message"] for e in events)
    assert restored._seq == history._seq
    assert restored.path.read_bytes() == source
    restored.collect()
    assert restored._seq > history._seq
