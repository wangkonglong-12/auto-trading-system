"""CICC MiniQMT XtData adapter restricted to the QMT-003 read-only probe."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import math
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from auto_trading_system.domain import SecurityCode
from auto_trading_system.monitoring.qmt_readonly import QmtReadOnlyAdapterError
from auto_trading_system.monitoring.qmt_tick import (
    QmtTickErrorSink,
    QmtTickFieldSample,
    QmtTickSample,
    QmtTickSink,
    QmtTickSnapshotBatch,
    QmtTickSource,
    QmtTimestampUnit,
)

_MAX_CALLBACK_BATCH = 256
_MAX_BOOK_DEPTH = 50


@dataclass(frozen=True, slots=True)
class XtdataBindings:
    subscribe_quote: Callable[..., Any]
    unsubscribe_quote: Callable[..., Any]
    get_full_tick: Callable[..., Any]
    sdk_version: str | None
    get_instrument_detail: Callable[..., Any] | None = None


def load_xtdata_bindings() -> XtdataBindings:
    """Load the three XtData functions allowed by the QMT-003 probe."""
    try:
        package_module = importlib.import_module("xtquant")
        data_module = importlib.import_module("xtquant.xtdata")
        subscribe_quote = data_module.subscribe_quote
        unsubscribe_quote = data_module.unsubscribe_quote
        get_full_tick = data_module.get_full_tick
        get_instrument_detail = getattr(data_module, "get_instrument_detail", None)
    except (AttributeError, ImportError, ModuleNotFoundError) as exc:
        raise QmtReadOnlyAdapterError(
            "XTDATA_IMPORT_FAILED", "required XtData probe functions are unavailable"
        ) from exc
    if not all(callable(item) for item in (subscribe_quote, unsubscribe_quote, get_full_tick)):
        raise QmtReadOnlyAdapterError(
            "XTDATA_API_INCOMPATIBLE", "required XtData probe functions are incompatible"
        )
    try:
        raw_version: object = importlib.metadata.version("xtquant")
    except importlib.metadata.PackageNotFoundError:
        raw_version = getattr(package_module, "__version__", None)
    sdk_version = (
        raw_version
        if isinstance(raw_version, str) and 0 < len(raw_version) <= 64 and raw_version.isprintable()
        else None
    )
    return XtdataBindings(
        subscribe_quote,
        unsubscribe_quote,
        get_full_tick,
        sdk_version,
        get_instrument_detail,
    )


@dataclass(frozen=True, slots=True)
class _ScalarSpec:
    name: str
    kind: str
    required: bool = True
    timestamp: bool = False


_SCALAR_FIELDS = (
    _ScalarSpec("time", "int", timestamp=True),
    _ScalarSpec("lastPrice", "numeric"),
    _ScalarSpec("open", "numeric"),
    _ScalarSpec("high", "numeric"),
    _ScalarSpec("low", "numeric"),
    _ScalarSpec("lastClose", "numeric"),
    _ScalarSpec("amount", "numeric"),
    _ScalarSpec("volume", "numeric"),
    _ScalarSpec("pvolume", "numeric"),
    _ScalarSpec("stockStatus", "int"),
    _ScalarSpec("openInt", "numeric", required=False),
    _ScalarSpec("lastSettlementPrice", "numeric", required=False),
    _ScalarSpec("transactionNum", "int", required=False),
)
_BOOK_FIELDS = ("askPrice", "bidPrice", "askVol", "bidVol")


class CiccQmtTickProbeClient:
    """Expose subscription, latest snapshot, wait and unsubscribe only."""

    def __init__(self, *, bindings: XtdataBindings | None = None) -> None:
        self._bindings = load_xtdata_bindings() if bindings is None else bindings
        self._subscriptions: dict[int, str] = {}
        self._callbacks: dict[int, Callable[[object], None]] = {}
        self._symbols: set[str] = set()
        self._wait_event = threading.Event()

    def __repr__(self) -> str:
        return "CiccQmtTickProbeClient(<protected>)"

    @property
    def sdk_version(self) -> str | None:
        return self._bindings.sdk_version

    def subscribe(
        self,
        symbol: SecurityCode,
        sample_sink: QmtTickSink,
        error_sink: QmtTickErrorSink,
    ) -> int:
        wire = symbol.to_wire()
        if wire in self._symbols or not callable(sample_sink) or not callable(error_sink):
            raise QmtReadOnlyAdapterError(
                "QMT_TICK_SUBSCRIBE_ORDER_INVALID", "XtData subscribe order is invalid"
            )

        def callback(payload: object) -> None:
            try:
                for sample in _inspect_callback_payload(wire, payload):
                    sample_sink(sample)
            except Exception:
                error_sink("QMT_TICK_CALLBACK_INVALID")

        try:
            result = self._bindings.subscribe_quote(
                wire,
                period="tick",
                count=0,
                callback=callback,
            )
        except Exception as exc:
            raise QmtReadOnlyAdapterError(
                "QMT_TICK_SUBSCRIBE_FAILED", "XtData tick subscription failed"
            ) from exc
        if type(result) is not int:
            raise QmtReadOnlyAdapterError(
                "QMT_TICK_SUBSCRIBE_RESULT_INVALID", "XtData returned an invalid subscription id"
            )
        if result > 0:
            if result in self._subscriptions:
                raise QmtReadOnlyAdapterError(
                    "QMT_TICK_SUBSCRIPTION_ID_DUPLICATE",
                    "XtData reused an active subscription id",
                )
            self._subscriptions[result] = wire
            self._callbacks[result] = callback
            self._symbols.add(wire)
        return result

    def get_full_tick(self, symbols: tuple[SecurityCode, ...]) -> QmtTickSnapshotBatch:
        wires = tuple(symbol.to_wire() for symbol in symbols)
        if not wires or any(wire not in self._symbols for wire in wires):
            raise QmtReadOnlyAdapterError(
                "QMT_TICK_SNAPSHOT_ORDER_INVALID", "XtData snapshot query order is invalid"
            )
        try:
            result = self._bindings.get_full_tick(list(wires))
        except Exception as exc:
            raise QmtReadOnlyAdapterError(
                "QMT_TICK_SNAPSHOT_QUERY_FAILED", "XtData full-tick query failed"
            ) from exc
        if not isinstance(result, Mapping):
            raise QmtReadOnlyAdapterError(
                "QMT_TICK_SNAPSHOT_RESULT_INVALID", "XtData full-tick result is not a mapping"
            )
        requested = set(wires)
        returned = {key for key in result if isinstance(key, str)}
        samples = [
            inspect_tick_record(wire, result[wire], QmtTickSource.SNAPSHOT)
            for wire in wires
            if wire in result
        ]
        return QmtTickSnapshotBatch(
            samples=tuple(samples),
            missing_symbol_count=len(requested - returned),
            unexpected_symbol_count=sum(1 for key in result if key not in requested),
        )

    def wait_for_callbacks(self, seconds: int) -> None:
        if type(seconds) is not int or seconds < 1 or not self._subscriptions:
            raise QmtReadOnlyAdapterError(
                "QMT_TICK_WAIT_ORDER_INVALID", "XtData callback wait order is invalid"
            )
        self._wait_event.wait(seconds)

    def unsubscribe(self, subscription_id: int) -> None:
        if type(subscription_id) is not int or subscription_id not in self._subscriptions:
            raise QmtReadOnlyAdapterError(
                "QMT_TICK_UNSUBSCRIBE_ORDER_INVALID", "XtData unsubscribe order is invalid"
            )
        try:
            self._bindings.unsubscribe_quote(subscription_id)
        except Exception as exc:
            raise QmtReadOnlyAdapterError(
                "QMT_TICK_UNSUBSCRIBE_FAILED", "XtData unsubscribe failed"
            ) from exc
        wire = self._subscriptions.pop(subscription_id)
        self._callbacks.pop(subscription_id)
        self._symbols.remove(wire)


def _inspect_callback_payload(expected_symbol: str, payload: object) -> tuple[QmtTickSample, ...]:
    if not isinstance(payload, Mapping) or set(payload) != {expected_symbol}:
        raise ValueError("callback symbol set is invalid")
    value = payload[expected_symbol]
    if isinstance(value, Mapping):
        records: Sequence[object] = (value,)
    elif isinstance(value, (list, tuple)):
        records = value
    else:
        raise ValueError("callback record container is invalid")
    if not 1 <= len(records) <= _MAX_CALLBACK_BATCH:
        raise ValueError("callback batch is outside the bounded range")
    return tuple(
        inspect_tick_record(expected_symbol, record, QmtTickSource.CALLBACK) for record in records
    )


def inspect_tick_record(
    symbol_wire: str,
    record: object,
    source: QmtTickSource,
) -> QmtTickSample:
    """Reduce a raw SDK tick to non-sensitive field facts."""
    if not isinstance(record, Mapping):
        raise QmtReadOnlyAdapterError(
            "QMT_TICK_RECORD_TYPE_INVALID", "XtData tick record is not a mapping"
        )
    fields = [_inspect_scalar(record, spec) for spec in _SCALAR_FIELDS]
    fields.extend(_inspect_book(record, name) for name in _BOOK_FIELDS)

    timestamp = record.get("time")
    timestamp_unit = _timestamp_unit(timestamp) if type(timestamp) is int else None
    volume_multiplier = _volume_multiplier(record.get("volume"), record.get("pvolume"))
    status = record.get("stockStatus")
    stock_status = status if type(status) is int else None
    lengths = {field.name: field.sequence_length for field in fields if field.name in _BOOK_FIELDS}
    depth_mismatch = (
        lengths["askPrice"] is not None
        and lengths["askVol"] is not None
        and lengths["askPrice"] != lengths["askVol"]
    ) or (
        lengths["bidPrice"] is not None
        and lengths["bidVol"] is not None
        and lengths["bidPrice"] != lengths["bidVol"]
    )
    return QmtTickSample(
        symbol_fingerprint=hashlib.sha256(symbol_wire.encode("ascii")).hexdigest()[:16],
        source=source,
        fields=tuple(fields),
        timestamp_unit=timestamp_unit,
        volume_multiplier_candidate=volume_multiplier,
        stock_status_code=stock_status,
        order_book_depth_mismatch=depth_mismatch,
    )


def _inspect_scalar(record: Mapping[object, object], spec: _ScalarSpec) -> QmtTickFieldSample:
    if spec.name not in record:
        return QmtTickFieldSample(
            spec.name,
            spec.required,
            "missing",
            not spec.required,
        )
    value = record[spec.name]
    observed_type = _observed_type(value)
    if value is None:
        return QmtTickFieldSample(spec.name, spec.required, "none", False, none=True)
    valid_type = type(value) is int if spec.kind == "int" else type(value) in {int, float}
    numeric_value: int | float | None = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric_value = value
    non_finite = int(isinstance(numeric_value, float) and not math.isfinite(numeric_value))
    negative = int(numeric_value is not None and numeric_value < 0)
    valid = valid_type and not non_finite and not negative
    if spec.timestamp and type(value) is int and _timestamp_unit(value) is QmtTimestampUnit.UNKNOWN:
        valid = False
    return QmtTickFieldSample(
        name=spec.name,
        required=spec.required,
        observed_type=observed_type,
        valid=valid,
        non_finite_count=non_finite,
        negative_count=negative,
    )


def _inspect_book(record: Mapping[object, object], name: str) -> QmtTickFieldSample:
    if name not in record:
        return QmtTickFieldSample(name, True, "missing", False)
    value = record[name]
    observed_type = _observed_type(value)
    if value is None:
        return QmtTickFieldSample(name, True, "none", False, none=True)
    if not isinstance(value, (list, tuple)):
        return QmtTickFieldSample(name, True, observed_type, False)
    element_types = tuple(sorted({_observed_type(item) for item in value}))
    non_finite = sum(type(item) is float and not math.isfinite(item) for item in value)
    negative = sum(type(item) in {int, float} and item < 0 for item in value)
    invalid_elements = sum(type(item) not in {int, float} for item in value)
    valid = (
        len(value) <= _MAX_BOOK_DEPTH
        and invalid_elements == 0
        and non_finite == 0
        and negative == 0
    )
    return QmtTickFieldSample(
        name=name,
        required=True,
        observed_type=observed_type,
        valid=valid,
        non_finite_count=non_finite,
        negative_count=negative,
        sequence_length=len(value),
        element_types=element_types,
        invalid_element_count=invalid_elements,
    )


def _observed_type(value: object) -> str:
    if value is None:
        return "none"
    if type(value) is bool:
        return "bool"
    if type(value) is int:
        return "int"
    if type(value) is float:
        return "float"
    if type(value) is str:
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, tuple):
        return "tuple"
    return "other"


def _timestamp_unit(value: int) -> QmtTimestampUnit:
    if 1_000_000_000 <= value < 10_000_000_000:
        return QmtTimestampUnit.SECONDS
    if 1_000_000_000_000 <= value < 10_000_000_000_000:
        return QmtTimestampUnit.MILLISECONDS
    if 1_000_000_000_000_000 <= value < 10_000_000_000_000_000:
        return QmtTimestampUnit.MICROSECONDS
    return QmtTimestampUnit.UNKNOWN


def _volume_multiplier(volume: object, pvolume: object) -> int | None:
    if type(volume) not in {int, float} or type(pvolume) not in {int, float}:
        return None
    numeric_volume = cast(int | float, volume)
    numeric_pvolume = cast(int | float, pvolume)
    if (
        not math.isfinite(float(numeric_volume))
        or not math.isfinite(float(numeric_pvolume))
        or numeric_volume <= 0
    ):
        return None
    try:
        ratio = Decimal(str(numeric_pvolume)) / Decimal(str(numeric_volume))
    except (InvalidOperation, ZeroDivisionError):
        return None
    if ratio != ratio.to_integral_value():
        return None
    candidate = int(ratio)
    return candidate if 1 <= candidate <= 10_000 else None
