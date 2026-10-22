from __future__ import annotations

import importlib
import importlib.metadata
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from auto_trading_system.gateways.broker.cicc_qmt import (
    CiccQmtReadOnlyClient,
    XtquantBindings,
    load_xtquant_bindings,
)
from auto_trading_system.monitoring.qmt_readonly import (
    QmtProbeConfig,
    QmtReadOnlyAdapterError,
    QmtSignal,
    QmtSignalKind,
)

ACCOUNT_ID = "A123456789"


class FakeCallbackBase:
    pass


@dataclass
class FakeAccount:
    account_id: str
    account_type: str


@dataclass
class FakeStatus:
    account_id: str
    account_type: int
    status: int


class FakeTrader:
    instances: ClassVar[list[FakeTrader]] = []

    def __init__(self, path: str, session_id: int) -> None:
        self.path = path
        self.session_id = session_id
        self.calls: list[str] = []
        self.callback: Any | None = None
        self.connect_result: object = 0
        self.subscribe_result: object = 0
        self.unsubscribe_result: object = 0
        self.status_result: object = [FakeStatus(ACCOUNT_ID, 2, 0)]
        self.__class__.instances.append(self)

    def register_callback(self, callback: object) -> None:
        self.calls.append("register")
        self.callback = callback

    def start(self) -> None:
        self.calls.append("start")

    def connect(self) -> object:
        self.calls.append("connect")
        return self.connect_result

    def subscribe(self, account: FakeAccount) -> object:
        assert account.account_id == ACCOUNT_ID
        self.calls.append("subscribe")
        return self.subscribe_result

    def query_account_status(self) -> object:
        self.calls.append("query")
        return self.status_result

    def unsubscribe(self, account: FakeAccount) -> object:
        assert account.account_id == ACCOUNT_ID
        self.calls.append("unsubscribe")
        return self.unsubscribe_result

    def stop(self) -> None:
        self.calls.append("stop")


@pytest.fixture(autouse=True)
def clear_fake_traders() -> None:
    FakeTrader.instances.clear()


def config(tmp_path: Path) -> QmtProbeConfig:
    userdata = tmp_path / "userdata_mini"
    (userdata / "up_queue_xtquant").mkdir(parents=True)
    return QmtProbeConfig(
        userdata_mini=userdata,
        session_id=131001,
        account_type="STOCK",
        observation_seconds=1,
        query_interval_milliseconds=100,
    )


def bindings() -> XtquantBindings:
    return XtquantBindings(
        trader_type=FakeTrader,
        callback_type=FakeCallbackBase,
        account_type=FakeAccount,
        sdk_version="fixture-1",
    )


def started_client(tmp_path: Path, signals: list[QmtSignal]) -> CiccQmtReadOnlyClient:
    client = CiccQmtReadOnlyClient(config(tmp_path), ACCOUNT_ID, bindings=bindings())
    client.register_signal_sink(signals.append)
    client.start()
    assert client.connect() == 0
    assert client.subscribe() == 0
    return client


def test_adapter_uses_only_readonly_calls_and_sanitizes_target_status(tmp_path: Path) -> None:
    signals: list[QmtSignal] = []
    client = started_client(tmp_path, signals)
    trader = FakeTrader.instances[0]
    trader.status_result = [
        FakeStatus("OTHER1234", 2, 5),
        FakeStatus(ACCOUNT_ID, 2, 0),
    ]

    query = client.query_account_statuses()
    assert client.unsubscribe() == 0
    client.stop()
    client.stop()

    assert query.total_account_count == 2
    assert [(item.account_type, item.status) for item in query.target_statuses] == [(2, 0)]
    assert trader.calls == [
        "register",
        "start",
        "connect",
        "subscribe",
        "query",
        "unsubscribe",
        "stop",
    ]
    assert not hasattr(client, "order_stock")
    assert not hasattr(client, "submit_order")
    assert not hasattr(client, "cancel_order")
    assert ACCOUNT_ID not in repr(client)


def test_shared_fund_id_is_filtered_by_stock_account_type(tmp_path: Path) -> None:
    signals: list[QmtSignal] = []
    client = started_client(tmp_path, signals)
    trader = FakeTrader.instances[0]
    trader.status_result = [
        FakeStatus(ACCOUNT_ID, 7, 1),
        FakeStatus(ACCOUNT_ID, 11, 1),
        FakeStatus(ACCOUNT_ID, 2, 0),
    ]
    assert [
        (item.account_type, item.status) for item in client.query_account_statuses().target_statuses
    ] == [(2, 0)]
    assert trader.callback is not None
    trader.callback.on_account_status(FakeStatus(ACCOUNT_ID, 7, 1))
    trader.callback.on_account_status(FakeStatus(ACCOUNT_ID, 2, 0))
    assert signals == [QmtSignal(QmtSignalKind.ACCOUNT_STATUS, account_type=2, status=0)]


def test_callback_copies_only_target_status_and_disconnect(tmp_path: Path) -> None:
    signals: list[QmtSignal] = []
    started_client(tmp_path, signals)
    callback = FakeTrader.instances[0].callback
    assert callback is not None

    callback.on_account_status(FakeStatus("OTHER1234", 2, 5))
    callback.on_account_status(FakeStatus(ACCOUNT_ID, 2, 1))
    callback.on_disconnected()

    assert signals == [
        QmtSignal(QmtSignalKind.ACCOUNT_STATUS, account_type=2, status=1),
        QmtSignal(QmtSignalKind.DISCONNECTED),
    ]


def test_callback_converts_malformed_payload_to_stable_signal(tmp_path: Path) -> None:
    signals: list[QmtSignal] = []
    started_client(tmp_path, signals)
    callback = FakeTrader.instances[0].callback
    assert callback is not None

    callback.on_account_status(SimpleNamespace(account_id=ACCOUNT_ID, status="0"))

    assert signals == [QmtSignal(QmtSignalKind.INVALID_ACCOUNT_STATUS)]


@pytest.mark.parametrize(
    ("status_result", "expected_code"),
    [
        (None, "QMT_STATUS_QUERY_NONE"),
        ({}, "QMT_STATUS_QUERY_TYPE_INVALID"),
        ([SimpleNamespace(account_id=ACCOUNT_ID, account_type=2)], "QMT_STATUS_PAYLOAD_INVALID"),
        (
            [SimpleNamespace(account_id=ACCOUNT_ID, account_type=2, status=True)],
            "QMT_STATUS_PAYLOAD_INVALID",
        ),
        (
            [FakeStatus(ACCOUNT_ID, 2, 0), FakeStatus(ACCOUNT_ID, 2, 5)],
            "QMT_STATUS_TARGET_DUPLICATE",
        ),
    ],
)
def test_status_query_rejects_ambiguous_or_malformed_sdk_results(
    tmp_path: Path,
    status_result: object,
    expected_code: str,
) -> None:
    client = started_client(tmp_path, [])
    FakeTrader.instances[0].status_result = status_result

    with pytest.raises(QmtReadOnlyAdapterError) as exc_info:
        client.query_account_statuses()

    assert exc_info.value.code == expected_code


def test_integer_return_contract_rejects_bool_and_wrong_call_order(tmp_path: Path) -> None:
    client = CiccQmtReadOnlyClient(config(tmp_path), ACCOUNT_ID, bindings=bindings())
    with pytest.raises(QmtReadOnlyAdapterError, match="QMT_CONNECT_ORDER_INVALID"):
        client.connect()
    client.register_signal_sink(lambda _: None)
    client.start()
    FakeTrader.instances[0].connect_result = True

    with pytest.raises(QmtReadOnlyAdapterError, match="QMT_CONNECT_RESULT_INVALID"):
        client.connect()


def test_binding_loader_resolves_official_types_and_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = {
        "xtquant": SimpleNamespace(__version__="fallback"),
        "xtquant.xttrader": SimpleNamespace(
            XtQuantTrader=FakeTrader,
            XtQuantTraderCallback=FakeCallbackBase,
        ),
        "xtquant.xttype": SimpleNamespace(StockAccount=FakeAccount),
    }
    monkeypatch.setattr(importlib, "import_module", lambda name: modules[name])
    monkeypatch.setattr(importlib.metadata, "version", lambda _: "2026.08")

    loaded = load_xtquant_bindings()

    assert loaded.trader_type is FakeTrader
    assert loaded.callback_type is FakeCallbackBase
    assert loaded.account_type is FakeAccount
    assert loaded.sdk_version == "2026.08"


def test_binding_loader_returns_stable_error_without_import_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_path = "C:/private/account-A123456789"

    def fail_import(_: str) -> object:
        raise ModuleNotFoundError(secret_path)

    monkeypatch.setattr(importlib, "import_module", fail_import)

    with pytest.raises(QmtReadOnlyAdapterError) as exc_info:
        load_xtquant_bindings()

    assert exc_info.value.code == "XTQUANT_IMPORT_FAILED"
    assert secret_path not in str(exc_info.value)
