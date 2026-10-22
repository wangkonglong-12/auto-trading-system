"""QMT command boundary with an isolated fake SDK; never connects to a broker."""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from auto_trading_system.domain import Price, Quantity, SecurityCode
from auto_trading_system.gateways.broker.cicc_qmt import XtquantBindings
from auto_trading_system.gateways.broker.models import CancelOrder, OrderSide, SubmitOrder
from auto_trading_system.gateways.broker.qmt_command_transport import (
    QmtCommandError,
    QmtCommandTransport,
    QmtConstants,
)
from auto_trading_system.monitoring.qmt_readonly import QmtProbeConfig

CN = timezone(timedelta(hours=8))
ACCOUNT = "A123456789"


class FakeTrader:
    def __init__(self) -> None:
        self.callback: object | None = None
        self.calls: list[tuple[object, ...]] = []
        self.status: object = [SimpleNamespace(account_id=ACCOUNT, account_type=2, status=0)]
        self.submit_sequence = 71
        self.orders: object = []
        self.asset: object = SimpleNamespace(account_id=ACCOUNT, account_type=2, cash=10000.0)
        self.positions: object = [
            SimpleNamespace(
                account_id=ACCOUNT,
                account_type=2,
                stock_code="000001.SZ",
                can_use_volume=100,
            )
        ]
        self.trades: object = []

    def register_callback(self, callback: object) -> None:
        self.callback = callback
        self.calls.append(("callback",))

    def start(self) -> None:
        self.calls.append(("start",))

    def connect(self) -> int:
        self.calls.append(("connect",))
        return 0

    def subscribe(self, _: object) -> int:
        self.calls.append(("subscribe",))
        return 0

    def query_account_status(self) -> object:
        return self.status

    def order_stock_async(self, *args: object) -> int:
        self.calls.append(("submit", *args))
        return self.submit_sequence

    def cancel_order_stock_async(self, *args: object) -> int:
        self.calls.append(("cancel", *args))
        return 72

    def query_stock_orders(self, _: object, __: bool) -> object:
        return self.orders

    def query_stock_asset(self, _: object) -> object:
        return self.asset

    def query_stock_positions(self, _: object) -> object:
        return self.positions

    def query_stock_trades(self, _: object) -> object:
        return self.trades

    def stop(self) -> None:
        self.calls.append(("stop",))


def command() -> SubmitOrder:
    return SubmitOrder(
        "ord_01K43TAIL000000001",
        SecurityCode.parse("000001.SZ"),
        OrderSide.SELL,
        Quantity(100),
        Price.parse("12.34"),
        "ATS000000000000000000001",
    )


def transport(tmp_path: Path) -> tuple[QmtCommandTransport, FakeTrader]:
    userdata = tmp_path / "userdata_mini"
    userdata.mkdir()
    (userdata / "up_queue_xtquant").touch()
    trader = FakeTrader()
    bindings = XtquantBindings(
        lambda *_: trader, type("Callback", (), {}), lambda *_: object(), "fake"
    )
    adapter = QmtCommandTransport(
        QmtProbeConfig(userdata, 6101904, "STOCK"),
        ACCOUNT,
        lambda: datetime(2026, 10, 20, 14, 30, tzinfo=CN),
        bindings=bindings,
        constants=QmtConstants(23, 24, 11),
    )
    return adapter, trader


def test_async_submit_cancel_and_callback_are_distinct_facts(tmp_path: Path) -> None:
    adapter, trader = transport(tmp_path)
    with pytest.raises(QmtCommandError, match="QMT_COMMAND_UNAVAILABLE"):
        adapter.submit(command())
    adapter.start()
    adapter.preflight(command())
    assert adapter.submit(command()) == 71  # SDK request seq, never order acceptance.
    submit = next(call for call in trader.calls if call[0] == "submit")
    assert submit[2:] == (
        "000001.SZ",
        24,
        100,
        11,
        12.34,
        "personal-auto-trading",
        command().remark_token,
    )
    assert adapter.cancel(CancelOrder(command().client_order_id, "81234")) == 72
    assert next(call for call in trader.calls if call[0] == "cancel")[2] == 81234
    assert trader.callback is not None
    cast(Any, trader.callback).on_order_stock_async_response(
        SimpleNamespace(
            account_id=ACCOUNT, seq=71, order_id=81234, order_remark=command().remark_token
        )
    )
    facts = adapter.drain()
    assert len(facts) == 1
    assert (facts[0].kind, facts[0].sequence, facts[0].order_id) == ("ORDER_RESPONSE", 71, 81234)
    trader.orders = [
        SimpleNamespace(
            account_id=ACCOUNT,
            account_type=2,
            order_id=81234,
            order_remark=command().remark_token,
            order_status=50,
            order_volume=100,
            traded_volume=0,
            stock_code="000001.SZ",
            order_type=24,
            price=12.34,
        )
    ]
    matched = adapter.query_order(81234, command().remark_token)
    assert matched is not None and matched.status == 50
    with pytest.raises(QmtCommandError, match="QMT_ORDER_QUERY_IDENTITY_CONFLICT"):
        adapter.query_order(81234, "wrong")
    adapter.stop()
    adapter.stop()
    assert trader.calls.count(("stop",)) == 1


def test_preflight_rejects_ambiguous_facts_and_insufficient_shares(tmp_path: Path) -> None:
    adapter, trader = transport(tmp_path)
    adapter.start()
    trader.orders = None
    with pytest.raises(QmtCommandError, match="QMT_PREFLIGHT_QUERY_UNKNOWN"):
        adapter.preflight(command())
    trader.orders = []
    trader.positions = []
    with pytest.raises(QmtCommandError, match="QMT_PREFLIGHT_SHARES_INSUFFICIENT"):
        adapter.preflight(command())
    adapter.stop()


def test_fail_closed_on_account_status_disconnection_and_unknown_result(tmp_path: Path) -> None:
    adapter, trader = transport(tmp_path)
    adapter.start()
    trader.status = [SimpleNamespace(account_id=ACCOUNT, account_type=2, status=5)]
    with pytest.raises(QmtCommandError, match="QMT_ACCOUNT_NOT_READY"):
        adapter.submit(command())
    trader.status = [SimpleNamespace(account_id=ACCOUNT, account_type=2, status=0)]
    trader.submit_sequence = -1
    with pytest.raises(QmtCommandError, match="QMT_SUBMIT_REJECTED_BY_SDK"):
        adapter.submit(command())
    trader.submit_sequence = 73
    assert trader.callback is not None
    cast(Any, trader.callback).on_disconnected()
    with pytest.raises(QmtCommandError, match="QMT_COMMAND_UNAVAILABLE"):
        adapter.submit(command())
    with pytest.raises(QmtCommandError, match="QMT_COMMAND_DISCONNECTED"):
        adapter.drain()
    with pytest.raises(QmtCommandError, match="QMT_COMMAND_UNAVAILABLE"):
        adapter.submit(command())
    adapter.stop()


def test_callback_account_mismatch_and_bad_cancel_id_fail_closed(tmp_path: Path) -> None:
    adapter, trader = transport(tmp_path)
    adapter.start()
    with pytest.raises(QmtCommandError, match="QMT_CANCEL_ORDER_ID_INVALID"):
        adapter.cancel(CancelOrder(command().client_order_id, "manual-order"))
    assert trader.callback is not None
    cast(Any, trader.callback).on_stock_trade(SimpleNamespace(account_id="OTHER", order_id=8))
    with pytest.raises(QmtCommandError, match="QMT_CALLBACK_ACCOUNT_MISMATCH"):
        adapter.drain()
    adapter.stop()


def test_constants_and_query_shapes_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(QmtCommandError, match="QMT_CONSTANTS_INVALID"):
        QmtConstants(0, 24, 11)
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda _: SimpleNamespace(STOCK_BUY=23, STOCK_SELL=24, FIX_PRICE=11),
    )
    assert QmtConstants.from_sdk() == QmtConstants(23, 24, 11)
    monkeypatch.setattr(importlib, "import_module", lambda _: SimpleNamespace())
    with pytest.raises(QmtCommandError, match="QMT_CONSTANTS_UNAVAILABLE"):
        QmtConstants.from_sdk()

    adapter, trader = transport(tmp_path)
    adapter.start()
    trader.orders = [
        SimpleNamespace(
            account_id=ACCOUNT,
            account_type=2,
            order_id=1,
            order_remark=command().remark_token,
            order_status=50,
            order_volume=100,
            traded_volume=0,
            stock_code="000001.SZ",
            order_type=24,
            price=12.34,
        )
    ]
    with pytest.raises(QmtCommandError, match="QMT_ORDER_QUERY_INVALID"):
        adapter.query_orders()
    trader.trades = [SimpleNamespace(account_id="OTHER", account_type=2)]
    with pytest.raises(QmtCommandError, match="QMT_TRADE_QUERY_IDENTITY_CONFLICT"):
        adapter.query_trades()
    trader.positions = [
        SimpleNamespace(
            account_id=ACCOUNT,
            account_type=2,
            stock_code="000001.SZ",
            volume=100,
            can_use_volume=101,
            frozen_volume=0,
            market_value=1000.0,
            avg_price=10.0,
        )
    ]
    with pytest.raises(QmtCommandError, match="QMT_ACCOUNT_QUERY_INVALID"):
        adapter.query_account()
    adapter.stop()
