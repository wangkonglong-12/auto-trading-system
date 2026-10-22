"""Live QMT gateway mapping with an isolated fake SDK; no broker connection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from auto_trading_system.domain import Price, Quantity, SecurityCode
from auto_trading_system.gateways.broker.cicc_qmt import XtquantBindings
from auto_trading_system.gateways.broker.cicc_qmt_trading_gateway import CiccQmtTradingGateway
from auto_trading_system.gateways.broker.models import (
    BrokerOrderStatus,
    CancelOrder,
    OrderSide,
    SubmitOrder,
)
from auto_trading_system.gateways.broker.qmt_command_transport import (
    QmtCommandError,
    QmtConstants,
)
from auto_trading_system.monitoring.qmt_readonly import QmtProbeConfig

CN = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 20, 14, 30, tzinfo=CN)
ACCOUNT = "A123456789"
REMARK = "0123456789abcdefghij"


class FakeTrader:
    def __init__(self) -> None:
        self.callback: object | None = None
        self.orders: list[object] = []
        self.trades: list[object] = []
        self.positions: list[object] = [
            SimpleNamespace(
                account_id=ACCOUNT,
                account_type=2,
                stock_code="000001.SZ",
                volume=100,
                can_use_volume=100,
                frozen_volume=0,
                market_value=1234.0,
                avg_price=10.0,
            )
        ]
        self.asset = SimpleNamespace(
            account_id=ACCOUNT,
            account_type=2,
            cash=10000.0,
            frozen_cash=0.0,
            market_value=1234.0,
            total_asset=11234.0,
        )
        self.calls: list[tuple[object, ...]] = []

    def register_callback(self, callback: object) -> None:
        self.callback = callback

    def start(self) -> None: ...

    def connect(self) -> int:
        return 0

    def subscribe(self, _: object) -> int:
        return 0

    def query_account_status(self) -> list[object]:
        return [SimpleNamespace(account_id=ACCOUNT, account_type=2, status=0)]

    def query_stock_asset(self, _: object) -> object:
        return self.asset

    def query_stock_positions(self, _: object) -> list[object]:
        return self.positions

    def query_stock_orders(self, _: object, __: bool) -> list[object]:
        return self.orders

    def query_stock_trades(self, _: object) -> list[object]:
        return self.trades

    def order_stock_async(self, *args: object) -> int:
        self.calls.append(("submit", *args))
        return 71

    def cancel_order_stock_async(self, *args: object) -> int:
        self.calls.append(("cancel", *args))
        return 72

    def stop(self) -> None: ...


def make_gateway(tmp_path: Path) -> tuple[CiccQmtTradingGateway, FakeTrader]:
    userdata = tmp_path / "userdata_mini"
    userdata.mkdir()
    (userdata / "up_queue_xtquant").touch()
    trader = FakeTrader()
    bindings = XtquantBindings(
        lambda *_: trader,
        type("Callback", (), {}),
        lambda *_: object(),
        "fake",
    )
    gateway = CiccQmtTradingGateway(
        QmtProbeConfig(userdata, 6101904, "STOCK"),
        ACCOUNT,
        "qmt-0123456789abcdef",
        lambda: NOW,
        bindings=bindings,
        constants=QmtConstants(23, 24, 11),
    )
    return gateway, trader


def command() -> SubmitOrder:
    return SubmitOrder(
        "ord_" + REMARK,
        SecurityCode.parse("000001.SZ"),
        OrderSide.SELL,
        Quantity(100),
        Price.parse("12.34"),
        REMARK,
    )


def test_submit_cancel_and_query_reconcile_to_broker_models(tmp_path: Path) -> None:
    gateway, trader = make_gateway(tmp_path)
    gateway.start(lambda _: None)
    receipt = gateway.submit_order(command())
    assert receipt.request_seq == 71
    trader.orders = [
        SimpleNamespace(
            account_id=ACCOUNT,
            account_type=2,
            order_id=81234,
            order_remark=REMARK,
            strategy_name="personal-auto-trading",
            order_status=55,
            order_volume=100,
            traded_volume=40,
            stock_code="000001.SZ",
            order_type=24,
            price=12.34,
            order_time=int(NOW.timestamp()),
        )
    ]
    trader.trades = [
        SimpleNamespace(
            account_id=ACCOUNT,
            account_type=2,
            traded_id="T0001",
            order_id=81234,
            stock_code="000001.SZ",
            order_type=24,
            traded_price=12.34,
            traded_volume=40,
            traded_amount=493.6,
            order_remark=REMARK,
            strategy_name="personal-auto-trading",
            traded_time=int(NOW.timestamp()),
        )
    ]
    orders = gateway.query_orders()
    trades = gateway.query_trades()
    assert len(orders) == 1 and orders[0].client_order_id == command().client_order_id
    assert orders[0].status is BrokerOrderStatus.PARTIALLY_FILLED
    assert orders[0].remaining_quantity.value == 60
    assert len(trades) == 1 and trades[0].trade_id == "trade_T0001"
    assert trades[0].client_order_id == command().client_order_id
    account = gateway.read_account()
    assert account.coherent and account.observed_order_ids == (command().client_order_id,)
    assert account.positions[0].cost_amount == "1000.00"
    assert gateway.cancel_order(CancelOrder(command().client_order_id, "81234")).request_seq == 72
    gateway.stop()


def test_identity_mismatch_and_external_orders_fail_closed_or_stay_unowned(tmp_path: Path) -> None:
    gateway, trader = make_gateway(tmp_path)
    gateway.start(lambda _: None)
    with pytest.raises(QmtCommandError, match="QMT_ACCOUNT_ORDER_WINDOW_MISSING"):
        gateway.read_account()
    bad = command()
    with pytest.raises(QmtCommandError, match="QMT_ORDER_ID_REMARK_MISMATCH"):
        gateway.submit_order(
            SubmitOrder(
                "ord_wrongidentifier",
                bad.symbol,
                bad.side,
                bad.quantity,
                bad.limit_price,
                bad.remark_token,
            )
        )
    trader.orders = [
        SimpleNamespace(
            account_id=ACCOUNT,
            account_type=2,
            order_id=9,
            order_remark="manual",
            strategy_name="manual",
            order_status=50,
            order_volume=100,
            traded_volume=0,
            stock_code="000001.SZ",
            order_type=24,
            price=12.34,
            order_time=int(NOW.timestamp()),
        )
    ]
    order = gateway.query_orders()[0]
    assert order.client_order_id.startswith("ord_external_")
    assert order.client_order_id != "ord_manual"
    assert gateway.find_order_by_client_id(order.client_order_id) == order
    assert gateway.find_order_by_client_id("ord_missing0000") is None
    trader.trades = [
        SimpleNamespace(
            account_id=ACCOUNT,
            account_type=2,
            traded_id="不规范成交编号",
            order_id=9,
            stock_code="000001.SZ",
            order_type=24,
            traded_price=12.34,
            traded_volume=10,
            traded_amount=123.4,
            order_remark="manual",
            strategy_name="manual",
            traded_time=int(NOW.timestamp()),
        )
    ]
    assert gateway.query_trades()[0].trade_id.startswith("trade_qmt_")
    cast(Any, trader.orders)[0].order_time = int((NOW - timedelta(days=1)).timestamp())
    with pytest.raises(QmtCommandError, match="QMT_TIMESTAMP_OUTSIDE_TRADE_DATE"):
        gateway.query_orders()
    gateway.stop()
