from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from auto_trading_system.gateways.broker.cicc_qmt import XtquantBindings
from auto_trading_system.gateways.broker.cicc_qmt_snapshot import CiccQmtSnapshotClient
from auto_trading_system.monitoring.qmt_readonly import QmtProbeConfig, QmtReadOnlyAdapterError
from auto_trading_system.monitoring.qmt_snapshot import (
    QmtSnapshotDataset,
    QmtSnapshotResultKind,
)

ACCOUNT_ID = "A123456789"


class FakeCallback:
    pass


class FakeAccount:
    def __init__(self, account_id: str, account_type: str) -> None:
        self.account_id = account_id
        self.account_type = account_type


class FakeTrader:
    def __init__(self, path: str, session_id: int) -> None:
        self.path = path
        self.session_id = session_id
        self.callback: object | None = None
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.asset_result: object = asset()
        self.positions_result: object = [position()]
        self.orders_result: object = [order()]
        self.trades_result: object = [trade()]
        self.raise_method: str | None = None

    def register_callback(self, callback: object) -> None:
        self.callback = callback

    def start(self) -> None:
        pass

    def connect(self) -> int:
        return 0

    def subscribe(self, account: FakeAccount) -> int:
        del account
        return 0

    def unsubscribe(self, account: FakeAccount) -> int:
        del account
        return 0

    def stop(self) -> None:
        pass

    def query_stock_asset(self, account: FakeAccount) -> object:
        return self._result("query_stock_asset", self.asset_result, account)

    def query_stock_positions(self, account: FakeAccount) -> object:
        return self._result("query_stock_positions", self.positions_result, account)

    def query_stock_orders(self, account: FakeAccount, cancelable_only: bool) -> object:
        return self._result("query_stock_orders", self.orders_result, account, cancelable_only)

    def query_stock_trades(self, account: FakeAccount) -> object:
        return self._result("query_stock_trades", self.trades_result, account)

    def _result(self, name: str, result: object, *arguments: object) -> object:
        self.calls.append((name, arguments))
        if self.raise_method == name:
            raise RuntimeError(f"private failure for {ACCOUNT_ID}")
        return result


def asset(**changes: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "account_type": 2,
        "account_id": ACCOUNT_ID,
        "cash": 100.0,
        "frozen_cash": 0.0,
        "market_value": 200.0,
        "total_asset": 300.0,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def position(**changes: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "account_type": 2,
        "account_id": ACCOUNT_ID,
        "stock_code": "000001.SZ",
        "volume": 100,
        "can_use_volume": 100,
        "open_price": 10.0,
        "market_value": 1000.0,
        "frozen_volume": 0,
        "on_road_volume": 0,
        "yesterday_volume": 100,
        "avg_price": 10.0,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def order(**changes: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "account_type": 2,
        "account_id": ACCOUNT_ID,
        "stock_code": "000001.SZ",
        "order_id": 123,
        "order_sysid": "SYS123",
        "order_time": 143000,
        "order_type": 23,
        "order_volume": 100,
        "price_type": 11,
        "price": 10.0,
        "traded_volume": 0,
        "traded_price": 0.0,
        "order_status": 50,
        "status_msg": "accepted",
        "strategy_name": "ATS",
        "order_remark": "ATS123",
        "direction": 0,
        "offset_flag": 0,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def trade(**changes: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "account_type": 2,
        "account_id": ACCOUNT_ID,
        "stock_code": "000001.SZ",
        "order_type": 23,
        "traded_id": "TRADE123",
        "traded_time": 143100,
        "traded_price": 10.0,
        "traded_volume": 100,
        "traded_amount": 1000.0,
        "order_id": 123,
        "order_sysid": "SYS123",
        "strategy_name": "ATS",
        "order_remark": "ATS123",
        "direction": 0,
        "offset_flag": 0,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def probe_config(tmp_path: Path) -> QmtProbeConfig:
    userdata = tmp_path / "userdata_mini"
    (userdata / "up_queue_xtquant").mkdir(parents=True)
    return QmtProbeConfig(userdata, 131001, "STOCK")


def create_client(
    tmp_path: Path,
    *,
    start: bool = True,
) -> tuple[CiccQmtSnapshotClient, FakeTrader]:
    instances: list[FakeTrader] = []

    def make_trader(path: str, session_id: int) -> FakeTrader:
        trader = FakeTrader(path, session_id)
        instances.append(trader)
        return trader

    bindings = XtquantBindings(make_trader, FakeCallback, FakeAccount, "fixture")
    client = CiccQmtSnapshotClient(probe_config(tmp_path), ACCOUNT_ID, bindings=bindings)
    if start:
        client.register_signal_sink(lambda _: None)
        client.start()
        assert client.connect() == 0
        assert client.subscribe() == 0
    return client, instances[0]


def field(observation: Any, name: str) -> Any:
    return next(item for item in observation.fields if item.name == name)


def test_four_queries_return_only_aggregate_field_shapes(tmp_path: Path) -> None:
    client, trader = create_client(tmp_path)

    observations = (
        client.query_asset(),
        client.query_positions(),
        client.query_orders(),
        client.query_trades(),
    )

    assert [item.dataset for item in observations] == list(QmtSnapshotDataset)
    assert [item.result_kind for item in observations] == [
        QmtSnapshotResultKind.OBJECT,
        QmtSnapshotResultKind.LIST,
        QmtSnapshotResultKind.LIST,
        QmtSnapshotResultKind.LIST,
    ]
    assert all(item.record_count == 1 and item.structurally_valid for item in observations)
    assert [name for name, _ in trader.calls] == [
        "query_stock_asset",
        "query_stock_positions",
        "query_stock_orders",
        "query_stock_trades",
    ]
    assert trader.calls[2][1][1] is False
    rendered = repr(observations)
    for protected in (ACCOUNT_ID, "000001.SZ", "SYS123", "TRADE123", "1000.0"):
        assert protected not in rendered


@pytest.mark.parametrize("query_name", ["positions", "orders", "trades"])
def test_empty_list_is_distinct_from_ambiguous_none(tmp_path: Path, query_name: str) -> None:
    client, trader = create_client(tmp_path)
    setattr(trader, f"{query_name}_result", [])
    query = getattr(client, f"query_{query_name}")
    empty = query()
    setattr(trader, f"{query_name}_result", None)
    ambiguous = query()

    assert empty.result_kind is QmtSnapshotResultKind.EMPTY_LIST
    assert ambiguous.result_kind is QmtSnapshotResultKind.AMBIGUOUS_NONE
    assert empty.record_count == ambiguous.record_count == 0
    assert empty.fields == ambiguous.fields == ()


def test_asset_none_has_unambiguous_failure_kind(tmp_path: Path) -> None:
    client, trader = create_client(tmp_path)
    trader.asset_result = None

    observation = client.query_asset()

    assert observation.result_kind is QmtSnapshotResultKind.ASSET_NONE
    assert observation.is_indeterminate


def test_optional_balance_fields_report_presence_without_requiring_them(tmp_path: Path) -> None:
    client, trader = create_client(tmp_path)

    absent = client.query_asset()
    trader.asset_result = asset(fetch_balance=80.0, current_balance=90.0)
    present = client.query_asset()

    assert absent.structurally_valid
    assert field(absent, "fetch_balance").required is False
    assert field(absent, "fetch_balance").missing_count == 1
    assert field(present, "fetch_balance").observed_types == ("float",)
    assert field(present, "current_balance").missing_count == 0


def test_unknown_sdk_value_type_is_not_misreported_as_string(tmp_path: Path) -> None:
    client, trader = create_client(tmp_path)
    trader.asset_result = asset(cash=object())

    observation = client.query_asset()

    assert field(observation, "cash").observed_types == ("other",)
    assert field(observation, "cash").invalid_count == 1


def test_negative_numeric_value_is_counted_and_rejected(tmp_path: Path) -> None:
    client, trader = create_client(tmp_path)
    trader.positions_result = [position(volume=-1)]

    observation = client.query_positions()

    assert field(observation, "volume").negative_count == 1
    assert field(observation, "volume").invalid_count == 1
    assert not observation.structurally_valid


def test_missing_bool_nan_and_wrong_account_are_counted_without_values(tmp_path: Path) -> None:
    client, trader = create_client(tmp_path)
    malformed = asset(account_id="OTHER1234", cash=True, market_value=float("nan"))
    delattr(malformed, "total_asset")
    trader.asset_result = malformed

    observation = client.query_asset()

    assert not observation.structurally_valid
    assert field(observation, "account_id").invalid_count == 1
    assert field(observation, "cash").observed_types == ("bool",)
    assert field(observation, "cash").invalid_count == 1
    assert field(observation, "market_value").non_finite_count == 1
    assert field(observation, "market_value").invalid_count == 1
    assert field(observation, "total_asset").missing_count == 1
    rendered = repr(observation)
    assert "OTHER1234" not in rendered
    assert "nan" not in rendered.lower()


@pytest.mark.parametrize(
    ("dataset", "records"),
    [
        ("positions", [position(), position()]),
        ("orders", [order(), order(stock_code="000002.SZ")]),
        ("trades", [trade(), trade(stock_code="000002.SZ")]),
    ],
)
def test_duplicate_dataset_identity_is_counted(
    tmp_path: Path,
    dataset: str,
    records: list[SimpleNamespace],
) -> None:
    client, trader = create_client(tmp_path)
    setattr(trader, f"{dataset}_result", records)

    observation = getattr(client, f"query_{dataset}")()

    assert observation.duplicate_identity_count == 1
    assert not observation.structurally_valid


@pytest.mark.parametrize("dataset", ["positions", "orders", "trades"])
def test_list_query_rejects_wrong_container_and_record_limit(
    tmp_path: Path,
    dataset: str,
) -> None:
    client, trader = create_client(tmp_path)
    setattr(trader, f"{dataset}_result", ())
    with pytest.raises(QmtReadOnlyAdapterError, match=f"QMT_{dataset.upper()}_RESULT_TYPE_INVALID"):
        getattr(client, f"query_{dataset}")()

    setattr(trader, f"{dataset}_result", [object()] * 10_001)
    with pytest.raises(QmtReadOnlyAdapterError, match=f"QMT_{dataset.upper()}_RESULT_TOO_LARGE"):
        getattr(client, f"query_{dataset}")()


def test_asset_rejects_container_instead_of_treating_it_as_sdk_object(tmp_path: Path) -> None:
    client, trader = create_client(tmp_path)
    trader.asset_result = {}

    with pytest.raises(QmtReadOnlyAdapterError, match="QMT_ASSET_RESULT_TYPE_INVALID"):
        client.query_asset()


@pytest.mark.parametrize(
    ("query_name", "method_name", "error_code"),
    [
        ("asset", "query_stock_asset", "QMT_ASSET_QUERY_FAILED"),
        ("positions", "query_stock_positions", "QMT_POSITIONS_QUERY_FAILED"),
        ("orders", "query_stock_orders", "QMT_ORDERS_QUERY_FAILED"),
        ("trades", "query_stock_trades", "QMT_TRADES_QUERY_FAILED"),
    ],
)
def test_sdk_query_failure_has_stable_sanitized_error(
    tmp_path: Path,
    query_name: str,
    method_name: str,
    error_code: str,
) -> None:
    client, trader = create_client(tmp_path)
    trader.raise_method = method_name

    with pytest.raises(QmtReadOnlyAdapterError) as exc_info:
        getattr(client, f"query_{query_name}")()

    assert exc_info.value.code == error_code
    assert ACCOUNT_ID not in str(exc_info.value)


def test_snapshot_queries_require_subscribed_lifecycle_and_fail_after_stop(tmp_path: Path) -> None:
    client, _ = create_client(tmp_path, start=False)
    with pytest.raises(QmtReadOnlyAdapterError, match="QMT_SNAPSHOT_QUERY_ORDER_INVALID"):
        client.query_asset()

    client.register_signal_sink(lambda _: None)
    client.start()
    assert client.connect() == 0
    assert client.subscribe() == 0
    client.stop()

    with pytest.raises(QmtReadOnlyAdapterError, match="QMT_SNAPSHOT_QUERY_ORDER_INVALID"):
        client.query_positions()
