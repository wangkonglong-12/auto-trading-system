from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import metadata
from types import SimpleNamespace

import pytest

from auto_trading_system.domain import SecurityCode
from auto_trading_system.gateways.market.qmt import (
    CiccQmtTickProbeClient,
    XtdataBindings,
    inspect_tick_record,
    load_xtdata_bindings,
)
from auto_trading_system.monitoring.qmt_readonly import QmtReadOnlyAdapterError
from auto_trading_system.monitoring.qmt_tick import (
    QmtTickSample,
    QmtTickSource,
    QmtTimestampUnit,
)

SYMBOL = SecurityCode.parse("600000.SH")


def tick(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "time": 1_725_438_600_000,
        "lastPrice": 10.1,
        "open": 10.0,
        "high": 10.2,
        "low": 9.9,
        "lastClose": 9.95,
        "amount": 1_000_000.0,
        "volume": 1_000,
        "pvolume": 100_000,
        "stockStatus": 13,
        "askPrice": [10.1, 10.2, 10.3, 10.4, 10.5],
        "bidPrice": [10.0, 9.9, 9.8, 9.7, 9.6],
        "askVol": [10, 20, 30, 40, 50],
        "bidVol": [50, 40, 30, 20, 10],
    }
    values.update(changes)
    return values


@dataclass
class FakeXtdata:
    subscription_result: object = 17
    snapshot_result: object = field(default_factory=lambda: {SYMBOL.to_wire(): tick()})
    subscribe_failure: Exception | None = None
    snapshot_failure: Exception | None = None
    unsubscribe_failure: Exception | None = None
    callbacks: list[Callable[[object], None]] = field(default_factory=list)
    calls: list[object] = field(default_factory=list)

    def subscribe_quote(self, symbol: str, **kwargs: object) -> object:
        self.calls.append(("subscribe", symbol, kwargs["period"], kwargs["count"]))
        if self.subscribe_failure is not None:
            raise self.subscribe_failure
        callback = kwargs["callback"]
        assert callable(callback)
        self.callbacks.append(callback)
        return self.subscription_result

    def get_full_tick(self, symbols: list[str]) -> object:
        self.calls.append(("snapshot", symbols))
        if self.snapshot_failure is not None:
            raise self.snapshot_failure
        return self.snapshot_result

    def unsubscribe_quote(self, subscription_id: int) -> None:
        self.calls.append(("unsubscribe", subscription_id))
        if self.unsubscribe_failure is not None:
            raise self.unsubscribe_failure

    def bindings(self) -> XtdataBindings:
        return XtdataBindings(
            self.subscribe_quote,
            self.unsubscribe_quote,
            self.get_full_tick,
            "fixture-sdk",
        )


def test_inspect_tick_reduces_values_to_field_shapes() -> None:
    observation = inspect_tick_record(SYMBOL.to_wire(), tick(), QmtTickSource.SNAPSHOT)

    assert observation.structurally_valid
    assert observation.timestamp_unit is QmtTimestampUnit.MILLISECONDS
    assert observation.volume_multiplier_candidate == 100
    assert observation.stock_status_code == 13
    assert observation.order_book_depth_mismatch is False
    assert {field.name for field in observation.fields} >= {
        "time",
        "lastPrice",
        "volume",
        "pvolume",
        "askPrice",
        "bidVol",
    }
    assert all("10.1" not in repr(field) for field in observation.fields)


@pytest.mark.parametrize(("metadata_version", "expected"), [("2.0", "2.0"), (None, "1.0")])
def test_dynamic_bindings_validate_api_and_resolve_version(
    monkeypatch: pytest.MonkeyPatch, metadata_version: str | None, expected: str
) -> None:
    package = SimpleNamespace(__version__="1.0")
    data = SimpleNamespace(
        subscribe_quote=lambda *args, **kwargs: None,
        unsubscribe_quote=lambda *args, **kwargs: None,
        get_full_tick=lambda *args, **kwargs: None,
    )

    def import_module(name: str) -> object:
        return package if name == "xtquant" else data

    def version(_: str) -> str:
        if metadata_version is None:
            raise metadata.PackageNotFoundError
        return metadata_version

    monkeypatch.setattr(importlib, "import_module", import_module)
    monkeypatch.setattr(metadata, "version", version)

    bindings = load_xtdata_bindings()

    assert bindings.sdk_version == expected


def test_dynamic_bindings_fail_closed_for_missing_or_incompatible_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(_: str) -> object:
        raise ModuleNotFoundError("secret")

    monkeypatch.setattr(importlib, "import_module", missing)
    with pytest.raises(QmtReadOnlyAdapterError, match="XTDATA_IMPORT_FAILED"):
        load_xtdata_bindings()

    package = SimpleNamespace(__version__="1.0")
    data = SimpleNamespace(
        subscribe_quote=None,
        unsubscribe_quote=lambda *_: None,
        get_full_tick=lambda *_: None,
    )
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: package if name == "xtquant" else data,
    )
    with pytest.raises(QmtReadOnlyAdapterError, match="XTDATA_API_INCOMPATIBLE"):
        load_xtdata_bindings()


def test_optional_fields_are_recorded_as_valid_missing_fields() -> None:
    observation = inspect_tick_record(SYMBOL.to_wire(), tick(), QmtTickSource.CALLBACK)
    fields = {field.name: field for field in observation.fields}

    assert fields["openInt"].observed_type == "missing"
    assert fields["openInt"].valid
    assert fields["transactionNum"].valid


@pytest.mark.parametrize(
    ("changes", "field_name"),
    [
        ({"time": True}, "time"),
        ({"time": 42}, "time"),
        ({"lastPrice": float("nan")}, "lastPrice"),
        ({"volume": -1}, "volume"),
        ({"amount": None}, "amount"),
        ({"askVol": [1, "bad"]}, "askVol"),
        ({"askPrice": [1.0] * 51}, "askPrice"),
    ],
)
def test_invalid_scalar_or_book_fields_are_rejected(
    changes: dict[str, object], field_name: str
) -> None:
    observation = inspect_tick_record(SYMBOL.to_wire(), tick(**changes), QmtTickSource.SNAPSHOT)

    assert not observation.structurally_valid
    assert not next(field for field in observation.fields if field.name == field_name).valid


def test_mismatched_price_and_volume_depth_is_rejected() -> None:
    observation = inspect_tick_record(SYMBOL.to_wire(), tick(askVol=[1, 2]), QmtTickSource.SNAPSHOT)

    assert observation.order_book_depth_mismatch
    assert not observation.structurally_valid


@pytest.mark.parametrize(
    ("timestamp", "unit"),
    [
        (1_725_438_600, QmtTimestampUnit.SECONDS),
        (1_725_438_600_000_000, QmtTimestampUnit.MICROSECONDS),
        (42, QmtTimestampUnit.UNKNOWN),
    ],
)
def test_timestamp_unit_candidates_are_explicit(timestamp: int, unit: QmtTimestampUnit) -> None:
    observation = inspect_tick_record(
        SYMBOL.to_wire(), tick(time=timestamp), QmtTickSource.SNAPSHOT
    )

    assert observation.timestamp_unit is unit


@pytest.mark.parametrize(
    "changes",
    [
        {"askPrice": None},
        {"askPrice": "not-a-sequence"},
        {"askPrice": (10.1, 10.2), "askVol": (1, 2)},
        {"pvolume": float("inf")},
    ],
)
def test_book_container_and_volume_edge_cases_are_bounded(changes: dict[str, object]) -> None:
    observation = inspect_tick_record(SYMBOL.to_wire(), tick(**changes), QmtTickSource.SNAPSHOT)

    assert len(observation.fields) == 17


def test_non_mapping_tick_record_is_rejected_without_repr() -> None:
    with pytest.raises(QmtReadOnlyAdapterError, match="QMT_TICK_RECORD_TYPE_INVALID") as exc_info:
        inspect_tick_record(SYMBOL.to_wire(), "secret-payload", QmtTickSource.SNAPSHOT)

    assert "secret-payload" not in str(exc_info.value)


def test_volume_multiplier_is_only_reported_for_bounded_integral_ratio() -> None:
    assert (
        inspect_tick_record(
            SYMBOL.to_wire(), tick(volume=3, pvolume=10), QmtTickSource.SNAPSHOT
        ).volume_multiplier_candidate
        is None
    )
    assert (
        inspect_tick_record(
            SYMBOL.to_wire(), tick(volume=0, pvolume=0), QmtTickSource.SNAPSHOT
        ).volume_multiplier_candidate
        is None
    )


def test_subscription_accepts_single_and_list_callback_shapes() -> None:
    sdk = FakeXtdata()
    client = CiccQmtTickProbeClient(bindings=sdk.bindings())
    samples: list[QmtTickSample] = []
    errors: list[str] = []

    subscription_id = client.subscribe(SYMBOL, samples.append, errors.append)
    sdk.callbacks[0]({SYMBOL.to_wire(): tick()})
    sdk.callbacks[0]({SYMBOL.to_wire(): [tick(), tick()]})

    assert subscription_id == 17
    assert len(samples) == 3
    assert all(item.source is QmtTickSource.CALLBACK for item in samples)
    assert errors == []
    assert sdk.calls[0] == ("subscribe", SYMBOL.to_wire(), "tick", 0)


def test_subscription_rejects_duplicate_symbol_and_duplicate_active_id() -> None:
    sdk = FakeXtdata()
    client = CiccQmtTickProbeClient(bindings=sdk.bindings())
    client.subscribe(SYMBOL, lambda _: None, lambda _: None)
    with pytest.raises(QmtReadOnlyAdapterError, match="QMT_TICK_SUBSCRIBE_ORDER_INVALID"):
        client.subscribe(SYMBOL, lambda _: None, lambda _: None)

    other = SecurityCode.parse("000001.SZ")
    with pytest.raises(QmtReadOnlyAdapterError, match="QMT_TICK_SUBSCRIPTION_ID_DUPLICATE"):
        client.subscribe(other, lambda _: None, lambda _: None)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"000001.SZ": tick()},
        {SYMBOL.to_wire(): []},
        {SYMBOL.to_wire(): "bad"},
    ],
)
def test_callback_never_raises_and_reports_invalid_shape(payload: object) -> None:
    sdk = FakeXtdata()
    client = CiccQmtTickProbeClient(bindings=sdk.bindings())
    errors: list[str] = []
    client.subscribe(SYMBOL, lambda _: pytest.fail("sample must not be accepted"), errors.append)

    sdk.callbacks[0](payload)

    assert errors == ["QMT_TICK_CALLBACK_INVALID"]


def test_snapshot_reports_missing_and_unexpected_symbols_without_raw_values() -> None:
    other = SecurityCode.parse("000001.SZ")
    sdk = FakeXtdata(
        subscription_result=17,
        snapshot_result={SYMBOL.to_wire(): tick(), "999999.SH": tick()},
    )
    client = CiccQmtTickProbeClient(bindings=sdk.bindings())
    client.subscribe(SYMBOL, lambda _: None, lambda _: None)
    sdk.subscription_result = 18
    client.subscribe(other, lambda _: None, lambda _: None)

    result = client.get_full_tick((SYMBOL, other))

    assert len(result.samples) == 1
    assert result.missing_symbol_count == 1
    assert result.unexpected_symbol_count == 1
    assert other.to_wire() not in repr(result)


@pytest.mark.parametrize(
    ("sdk", "operation", "code"),
    [
        (FakeXtdata(subscription_result=True), "subscribe", "QMT_TICK_SUBSCRIBE_RESULT_INVALID"),
        (
            FakeXtdata(subscribe_failure=RuntimeError("secret")),
            "subscribe",
            "QMT_TICK_SUBSCRIBE_FAILED",
        ),
        (
            FakeXtdata(snapshot_result=None),
            "snapshot",
            "QMT_TICK_SNAPSHOT_RESULT_INVALID",
        ),
        (
            FakeXtdata(snapshot_failure=RuntimeError("secret")),
            "snapshot",
            "QMT_TICK_SNAPSHOT_QUERY_FAILED",
        ),
    ],
)
def test_sdk_failures_map_to_stable_codes(sdk: FakeXtdata, operation: str, code: str) -> None:
    client = CiccQmtTickProbeClient(bindings=sdk.bindings())
    if operation == "subscribe":
        with pytest.raises(QmtReadOnlyAdapterError, match=code):
            client.subscribe(SYMBOL, lambda _: None, lambda _: None)
    else:
        client.subscribe(SYMBOL, lambda _: None, lambda _: None)
        with pytest.raises(QmtReadOnlyAdapterError, match=code):
            client.get_full_tick((SYMBOL,))


def test_unsubscribe_is_exactly_once_and_maps_sdk_failure() -> None:
    sdk = FakeXtdata()
    client = CiccQmtTickProbeClient(bindings=sdk.bindings())
    client.subscribe(SYMBOL, lambda _: None, lambda _: None)
    client.unsubscribe(17)

    with pytest.raises(QmtReadOnlyAdapterError, match="QMT_TICK_UNSUBSCRIBE_ORDER_INVALID"):
        client.unsubscribe(17)

    failing_sdk = FakeXtdata(unsubscribe_failure=RuntimeError("secret"))
    failing_client = CiccQmtTickProbeClient(bindings=failing_sdk.bindings())
    failing_client.subscribe(SYMBOL, lambda _: None, lambda _: None)
    with pytest.raises(QmtReadOnlyAdapterError, match="QMT_TICK_UNSUBSCRIBE_FAILED"):
        failing_client.unsubscribe(17)


def test_snapshot_requires_an_active_subscription() -> None:
    client = CiccQmtTickProbeClient(bindings=FakeXtdata().bindings())

    with pytest.raises(QmtReadOnlyAdapterError, match="QMT_TICK_SNAPSHOT_ORDER_INVALID"):
        client.get_full_tick((SYMBOL,))


def test_callback_wait_requires_subscription_and_uses_bounded_seconds() -> None:
    client = CiccQmtTickProbeClient(bindings=FakeXtdata().bindings())
    with pytest.raises(QmtReadOnlyAdapterError, match="QMT_TICK_WAIT_ORDER_INVALID"):
        client.wait_for_callbacks(0)

    client.subscribe(SYMBOL, lambda _: None, lambda _: None)
    waited: list[int] = []
    client._wait_event = SimpleNamespace(wait=waited.append)  # type: ignore[assignment]
    client.wait_for_callbacks(3)
    assert waited == [3]


def test_client_repr_never_exposes_symbols() -> None:
    client = CiccQmtTickProbeClient(bindings=FakeXtdata().bindings())
    client.subscribe(SYMBOL, lambda _: None, lambda _: None)

    assert repr(client) == "CiccQmtTickProbeClient(<protected>)"
    assert SYMBOL.to_wire() not in repr(client)
