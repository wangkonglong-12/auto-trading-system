"""真实 QMT 数值映射必须拒绝未知、跨账户和无法归属的事实。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from auto_trading_system.gateways.broker.cicc_qmt import XtquantBindings
from auto_trading_system.gateways.broker.cicc_qmt_facts import CiccQmtFactsClient
from auto_trading_system.monitoring.qmt_readonly import QmtProbeConfig, QmtReadOnlyAdapterError

ACCOUNT = "A123456789"


class Trader:
    def __init__(self) -> None:
        self.asset: object = SimpleNamespace(
            account_id=ACCOUNT,
            account_type=2,
            cash=123.456,
            frozen_cash=1.0,
            market_value=200.0,
            total_asset=323.456,
        )
        self.positions: object = [
            SimpleNamespace(
                account_id=ACCOUNT,
                account_type=2,
                stock_code="000001.SZ",
                volume=100,
                can_use_volume=90,
                frozen_volume=10,
                market_value=200.0,
            )
        ]
        self.orders: object = []
        self.trades: object = []
        self.called: list[str] = []

    def register_callback(self, _: object) -> None:
        self.called.append("callback")

    def start(self) -> None:
        self.called.append("start")

    def connect(self) -> int:
        return 0

    def subscribe(self, _: object) -> int:
        return 0

    def query_stock_asset(self, _: object) -> object:
        return self.asset

    def query_stock_positions(self, _: object) -> object:
        return self.positions

    def query_stock_orders(self, _: object, __: bool) -> object:
        return self.orders

    def query_stock_trades(self, _: object) -> object:
        return self.trades


def client(tmp_path: Path) -> tuple[CiccQmtFactsClient, Trader]:
    userdata = tmp_path / "userdata_mini"
    userdata.mkdir()
    (userdata / "up_queue_xtquant").touch()
    trader = Trader()
    bindings = XtquantBindings(lambda *_: trader, type("Callback", (), {}), lambda *_: None, "fake")
    result = CiccQmtFactsClient(
        QmtProbeConfig(userdata, 6101904, "STOCK"), ACCOUNT, bindings=bindings
    )
    result.register_signal_sink(lambda _: None)
    result.start()
    assert result.connect() == 0
    assert result.subscribe() == 0
    return result, trader


def test_real_values_map_but_remain_unattributed(tmp_path: Path) -> None:
    reader, trader = client(tmp_path)
    facts = reader.read_facts("qmt-fingerprint", datetime.now(UTC))
    assert facts.account.cash == "123.46"
    assert facts.account.cash_frozen == "1.00"
    assert facts.account.total_equity == "323.46"
    assert facts.account.positions[0].symbol == "000001.SZ"
    assert facts.account.positions[0].sellable == 90
    assert facts.account.coherent is False
    assert facts.account.observed_order_ids == ()
    assert (facts.order_count, facts.trade_count) == (0, 0)
    assert not hasattr(reader, "order_stock")
    assert not hasattr(reader, "submit_order")
    assert not hasattr(reader, "cancel_order")
    assert trader.called == ["callback", "start"]


@pytest.mark.parametrize(
    ("source", "value", "code"),
    [
        ("asset", None, "QMT_FACT_ASSET_NONE"),
        ("asset", SimpleNamespace(account_id="OTHER", account_type=2), "QMT_FACT_ACCOUNT_MISMATCH"),
        ("positions", None, "QMT_FACT_POSITIONS_INVALID"),
        ("orders", None, "QMT_FACT_ORDERS_INVALID"),
        ("trades", None, "QMT_FACT_TRADES_INVALID"),
        (
            "orders",
            [SimpleNamespace(account_id="OTHER", account_type=2)],
            "QMT_FACT_ACCOUNT_MISMATCH",
        ),
    ],
)
def test_unknown_or_cross_account_data_rejected(
    tmp_path: Path, source: str, value: object, code: str
) -> None:
    reader, trader = client(tmp_path)
    setattr(trader, source, value)
    with pytest.raises(QmtReadOnlyAdapterError) as caught:
        reader.read_facts("qmt-fingerprint", datetime.now(UTC))
    assert caught.value.code == code
    assert ACCOUNT not in str(caught.value)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, "100"])
def test_invalid_money_rejected(tmp_path: Path, bad: Any) -> None:
    reader, trader = client(tmp_path)
    cast(Any, trader.asset).cash = bad
    with pytest.raises(QmtReadOnlyAdapterError):
        reader.read_facts("qmt-fingerprint", datetime.now(UTC))


def test_duplicate_or_inconsistent_position_rejected(tmp_path: Path) -> None:
    reader, trader = client(tmp_path)
    assert isinstance(trader.positions, list)
    trader.positions.append(trader.positions[0])
    with pytest.raises(QmtReadOnlyAdapterError, match="QMT_FACT_POSITION_INVALID"):
        reader.read_facts("qmt-fingerprint", datetime.now(UTC))
    trader.positions = [
        SimpleNamespace(
            account_id=ACCOUNT,
            account_type=2,
            stock_code="000001.SZ",
            volume=100,
            can_use_volume=100,
            frozen_volume=1,
            market_value=100.0,
        )
    ]
    with pytest.raises(QmtReadOnlyAdapterError, match="QMT_FACT_POSITION_INVALID"):
        reader.read_facts("qmt-fingerprint", datetime.now(UTC))
