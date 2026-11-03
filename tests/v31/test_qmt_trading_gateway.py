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
from auto_trading_system.protocol.qmt_recovery import QmtOrderRecoveryBinding

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


def make_gateway(
    tmp_path: Path,
    *,
    order_recovery_bindings: tuple[QmtOrderRecoveryBinding, ...] = (),
) -> tuple[CiccQmtTradingGateway, FakeTrader]:
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
        order_recovery_bindings=order_recovery_bindings,
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


@pytest.mark.parametrize("status", [54, 57])
def test_yesterdays_external_zero_fill_terminal_orders_do_not_break_today(
    tmp_path: Path, status: int
) -> None:
    gateway, trader = make_gateway(tmp_path)
    gateway.start(lambda _: None)
    try:
        trader.orders = [
            SimpleNamespace(
                account_id=ACCOUNT,
                account_type=2,
                order_id=9,
                order_remark="external",
                strategy_name="manual",
                order_status=status,
                order_volume=100,
                traded_volume=0,
                stock_code="000001.SZ",
                order_type=23,
                price=10.0,
                order_time=int((NOW - timedelta(days=1)).timestamp()),
            )
        ]
        assert gateway.query_orders() == ()
        assert gateway.query_trades() == ()
        assert gateway.read_account().observed_order_ids == ()
        assert trader.calls == []
        cast(Any, trader.orders[0]).order_time = 1
        with pytest.raises(QmtCommandError, match="QMT_TIMESTAMP_OUTSIDE_TRADE_DATE"):
            gateway.query_orders()
    finally:
        gateway.stop()


@pytest.mark.parametrize(
    "status,filled,strategy,offset",
    [
        (50, 0, "manual", -1),
        (55, 40, "manual", -1),
        (56, 100, "manual", -1),
        (54, 0, "personal-auto-trading", -1),
        (57, 0, "manual", 1),
    ],
)
def test_historical_system_active_filled_and_future_orders_still_fail_closed(
    tmp_path: Path, status: int, filled: int, strategy: str, offset: int
) -> None:
    gateway, trader = make_gateway(tmp_path)
    gateway.start(lambda _: None)
    try:
        trader.orders = [
            SimpleNamespace(
                account_id=ACCOUNT,
                account_type=2,
                order_id=9,
                order_remark=REMARK,
                strategy_name=strategy,
                order_status=status,
                order_volume=100,
                traded_volume=filled,
                stock_code="000001.SZ",
                order_type=23,
                price=10.0,
                order_time=int((NOW + timedelta(days=offset)).timestamp()),
            )
        ]
        with pytest.raises(QmtCommandError, match="QMT_TIMESTAMP_OUTSIDE_TRADE_DATE"):
            gateway.query_orders()
        assert trader.calls == []
    finally:
        gateway.stop()


def test_historical_trades_are_never_discarded(tmp_path: Path) -> None:
    gateway, trader = make_gateway(tmp_path)
    gateway.start(lambda _: None)
    try:
        trader.trades = [
            SimpleNamespace(
                account_id=ACCOUNT,
                account_type=2,
                traded_id="old-fill",
                order_id=9,
                stock_code="000001.SZ",
                order_type=23,
                traded_price=10.0,
                traded_volume=100,
                traded_amount=1000.0,
                order_remark="external",
                strategy_name="manual",
                traded_time=int((NOW - timedelta(days=1)).timestamp()),
            )
        ]
        with pytest.raises(QmtCommandError, match="QMT_TIMESTAMP_OUTSIDE_TRADE_DATE"):
            gateway.query_trades()
        assert trader.calls == []
    finally:
        gateway.stop()


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


@pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL])
@pytest.mark.parametrize("source", ["sequence", "remark", "unmapped", "conflict"])
def test_error_callbacks_keep_halt_and_emit_only_verified_owned_requests(
    tmp_path: Path, side: OrderSide, source: str
) -> None:
    from dataclasses import replace

    from auto_trading_system.gateways.broker.models import BrokerRequestError

    gateway, trader = make_gateway(tmp_path)
    events: list[Any] = []
    gateway.start(events.append)
    gateway.submit_order(replace(command(), side=side))
    if source == "conflict":
        gateway._remarks["otherremark"] = "ord_otheridentifier"
    data = SimpleNamespace(account_id=ACCOUNT, error_id=-123, error_msg="private SDK account text")
    if source in ("sequence", "conflict"):
        data.seq = 71
    if source == "remark":
        data.order_remark = REMARK
    if source == "conflict":
        data.order_remark = "otherremark"
    cast(Any, trader.callback).on_order_error(data)
    with pytest.raises(QmtCommandError, match="QMT_CALLBACK_ERROR"):
        gateway.query_orders()
    if source in ("sequence", "remark"):
        assert len(events) == 1 and isinstance(events[0], BrokerRequestError)
        assert events[0].client_order_id == command().client_order_id
        assert events[0].reason_code == "QMT_ORDER_ERROR_NEG_123"
        assert "private SDK" not in repr(events[0])
    else:
        assert events == []
    gateway.stop()


@pytest.mark.parametrize("source", ["sequence", "order_id"])
def test_cancel_error_keeps_original_order_binding_and_all_callbacks(
    tmp_path: Path, source: str
) -> None:
    from auto_trading_system.gateways.broker.models import BrokerCancelError

    gateway, trader = make_gateway(tmp_path)
    events: list[Any] = []
    gateway.start(events.append)
    gateway.cancel_order(CancelOrder(command().client_order_id, "81234"))
    data = SimpleNamespace(account_id=ACCOUNT, error_id=1, error_msg="private SDK account text")
    if source == "sequence":
        data.seq = 72
    else:
        data.order_id = 81234
    cast(Any, trader.callback).on_cancel_error(data)
    cast(Any, trader.callback).on_cancel_error(data)
    with pytest.raises(QmtCommandError, match="QMT_CALLBACK_ERROR"):
        gateway.query_orders()
    assert len(events) == 2 and all(isinstance(e, BrokerCancelError) for e in events)
    assert all(
        e.client_order_id == command().client_order_id and e.broker_order_id == "81234"
        for e in events
    )
    assert all("private SDK" not in repr(e) for e in events)
    gateway.stop()


def test_request_tracking_capacity_and_sequence_conflict_remain_fail_closed(tmp_path: Path) -> None:
    from dataclasses import replace

    gateway, trader = make_gateway(tmp_path)
    gateway.start(lambda _: None)
    gateway.submit_order(command())
    different = replace(
        command(), client_order_id="ord_abcdefghijklmnopqrst", remark_token="abcdefghijklmnopqrst"
    )
    with pytest.raises(QmtCommandError, match="QMT_REQUEST_SEQUENCE_CONFLICT"):
        gateway.submit_order(different)
    gateway._requests = dict.fromkeys(range(2048), ("ord_request0001", "NEW"))
    before = len(trader.calls)
    with pytest.raises(QmtCommandError, match="QMT_REQUEST_CAPACITY"):
        gateway.submit_order(command())
    with pytest.raises(QmtCommandError, match="QMT_REQUEST_CAPACITY"):
        gateway.cancel_order(CancelOrder(command().client_order_id, "81234"))
    assert len(trader.calls) == before
    gateway.stop()


@pytest.mark.parametrize(
    "sequence,order_id,expected",
    [
        (72, 81234, "ord_cancel_first"),
        (73, 81234, "ord_cancel_second"),
        (None, 81234, None),
        (999, 81234, None),
        (73, 99999, None),
    ],
)
def test_cancel_retry_errors_are_bound_to_the_exact_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sequence: int | None,
    order_id: int,
    expected: str | None,
) -> None:
    gateway, trader = make_gateway(tmp_path)
    events: list[Any] = []
    gateway.start(events.append)
    gateway.cancel_order(CancelOrder(command().client_order_id, "81234", "ord_cancel_first"))
    monkeypatch.setattr(trader, "cancel_order_stock_async", lambda *a: 73)
    gateway.cancel_order(CancelOrder(command().client_order_id, "81234", "ord_cancel_second"))
    data = SimpleNamespace(account_id=ACCOUNT, order_id=order_id, error_id=1, error_msg="private")
    if sequence is not None:
        data.seq = sequence
    cast(Any, trader.callback).on_cancel_error(data)
    with pytest.raises(QmtCommandError, match="QMT_CALLBACK_ERROR"):
        gateway.query_orders()
    assert [e.client_order_id for e in events] == ([expected] if expected else [])
    gateway.stop()
