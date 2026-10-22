"""CICC MiniQMT adapter for the QMT-002 read-only snapshot probe."""

from __future__ import annotations

import hmac
import math
from dataclasses import dataclass

from auto_trading_system.gateways.broker.cicc_qmt import (
    CiccQmtReadOnlyClient,
    XtquantBindings,
)
from auto_trading_system.monitoring.qmt_readonly import (
    QmtProbeConfig,
    QmtReadOnlyAdapterError,
)
from auto_trading_system.monitoring.qmt_snapshot import (
    QmtDatasetObservation,
    QmtFieldShape,
    QmtSnapshotDataset,
    QmtSnapshotResultKind,
)

_MAX_DATASET_RECORDS = 10_000


@dataclass(frozen=True, slots=True)
class _FieldSpec:
    name: str
    value_type: type[object]
    required: bool = True
    identity: bool = False


_ASSET_FIELDS = (
    _FieldSpec("account_type", int),
    _FieldSpec("account_id", str),
    _FieldSpec("cash", float),
    _FieldSpec("frozen_cash", float),
    _FieldSpec("market_value", float),
    _FieldSpec("total_asset", float),
    _FieldSpec("fetch_balance", float, required=False),
    _FieldSpec("current_balance", float, required=False),
)

_POSITION_FIELDS = (
    _FieldSpec("account_type", int),
    _FieldSpec("account_id", str),
    _FieldSpec("stock_code", str, identity=True),
    _FieldSpec("volume", int),
    _FieldSpec("can_use_volume", int),
    _FieldSpec("open_price", float),
    _FieldSpec("market_value", float),
    _FieldSpec("frozen_volume", int),
    _FieldSpec("on_road_volume", int),
    _FieldSpec("yesterday_volume", int),
    _FieldSpec("avg_price", float),
)

_ORDER_FIELDS = (
    _FieldSpec("account_type", int),
    _FieldSpec("account_id", str),
    _FieldSpec("stock_code", str),
    _FieldSpec("order_id", int, identity=True),
    _FieldSpec("order_sysid", str),
    _FieldSpec("order_time", int),
    _FieldSpec("order_type", int),
    _FieldSpec("order_volume", int),
    _FieldSpec("price_type", int),
    _FieldSpec("price", float),
    _FieldSpec("traded_volume", int),
    _FieldSpec("traded_price", float),
    _FieldSpec("order_status", int),
    _FieldSpec("status_msg", str),
    _FieldSpec("strategy_name", str),
    _FieldSpec("order_remark", str),
    _FieldSpec("direction", int),
    _FieldSpec("offset_flag", int),
)

_TRADE_FIELDS = (
    _FieldSpec("account_type", int),
    _FieldSpec("account_id", str),
    _FieldSpec("stock_code", str),
    _FieldSpec("order_type", int),
    _FieldSpec("traded_id", str, identity=True),
    _FieldSpec("traded_time", int),
    _FieldSpec("traded_price", float),
    _FieldSpec("traded_volume", int),
    _FieldSpec("traded_amount", float),
    _FieldSpec("order_id", int),
    _FieldSpec("order_sysid", str),
    _FieldSpec("strategy_name", str),
    _FieldSpec("order_remark", str),
    _FieldSpec("direction", int),
    _FieldSpec("offset_flag", int),
)


class _FieldAccumulator:
    __slots__ = (
        "_spec",
        "invalid_count",
        "missing_count",
        "negative_count",
        "non_finite_count",
        "none_count",
        "observed_types",
    )

    def __init__(self, spec: _FieldSpec) -> None:
        self._spec = spec
        self.observed_types: set[str] = set()
        self.missing_count = 0
        self.none_count = 0
        self.invalid_count = 0
        self.non_finite_count = 0
        self.negative_count = 0

    def accept_missing(self) -> None:
        self.missing_count += 1

    def accept(self, value: object) -> None:
        observed_type = _observed_type(value)
        self.observed_types.add(observed_type)
        if value is None:
            self.none_count += 1
            self.invalid_count += 1
            return
        if type(value) is not self._spec.value_type:
            self.invalid_count += 1
            return
        invalid = False
        if self._spec.identity and (
            (isinstance(value, str) and (not value or not value.isprintable()))
            or (type(value) is int and value <= 0)
        ):
            invalid = True
        if isinstance(value, float) and not math.isfinite(value):
            self.non_finite_count += 1
            invalid = True
        if isinstance(value, (int, float)) and value < 0:
            self.negative_count += 1
            invalid = True
        if invalid:
            self.invalid_count += 1

    def freeze(self) -> QmtFieldShape:
        return QmtFieldShape(
            name=self._spec.name,
            required=self._spec.required,
            observed_types=tuple(sorted(self.observed_types)),
            missing_count=self.missing_count,
            none_count=self.none_count,
            invalid_count=self.invalid_count,
            non_finite_count=self.non_finite_count,
            negative_count=self.negative_count,
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
    # The public model deliberately exposes only a closed set of type labels.
    return "other"


class CiccQmtSnapshotClient(CiccQmtReadOnlyClient):
    """Add only four account snapshot queries to the QMT-001 lifecycle."""

    def __init__(
        self,
        config: QmtProbeConfig,
        account_id: str,
        *,
        bindings: XtquantBindings | None = None,
    ) -> None:
        super().__init__(config, account_id, bindings=bindings)

    def __repr__(self) -> str:
        return "CiccQmtSnapshotClient(<protected>)"

    def query_asset(self) -> QmtDatasetObservation:
        result = self._query_sdk("query_stock_asset", "ASSET")
        if result is None:
            return QmtDatasetObservation(
                dataset=QmtSnapshotDataset.ASSET,
                result_kind=QmtSnapshotResultKind.ASSET_NONE,
                record_count=0,
                fields=(),
            )
        if _is_invalid_asset_container(result):
            raise QmtReadOnlyAdapterError(
                "QMT_ASSET_RESULT_TYPE_INVALID",
                "XtQuant asset query returned an incompatible result",
            )
        fields, duplicate_count = self._observe_records((result,), _ASSET_FIELDS)
        return QmtDatasetObservation(
            dataset=QmtSnapshotDataset.ASSET,
            result_kind=QmtSnapshotResultKind.OBJECT,
            record_count=1,
            fields=fields,
            duplicate_identity_count=duplicate_count,
        )

    def query_positions(self) -> QmtDatasetObservation:
        return self._query_list_dataset(
            method_name="query_stock_positions",
            dataset=QmtSnapshotDataset.POSITIONS,
            specs=_POSITION_FIELDS,
        )

    def query_orders(self) -> QmtDatasetObservation:
        return self._query_list_dataset(
            method_name="query_stock_orders",
            dataset=QmtSnapshotDataset.ORDERS,
            specs=_ORDER_FIELDS,
            arguments=(self._account, False),
        )

    def query_trades(self) -> QmtDatasetObservation:
        return self._query_list_dataset(
            method_name="query_stock_trades",
            dataset=QmtSnapshotDataset.TRADES,
            specs=_TRADE_FIELDS,
        )

    def _query_list_dataset(
        self,
        *,
        method_name: str,
        dataset: QmtSnapshotDataset,
        specs: tuple[_FieldSpec, ...],
        arguments: tuple[object, ...] | None = None,
    ) -> QmtDatasetObservation:
        result = self._query_sdk(
            method_name,
            dataset.value,
            *(arguments if arguments is not None else (self._account,)),
        )
        if result is None:
            return QmtDatasetObservation(
                dataset=dataset,
                result_kind=QmtSnapshotResultKind.AMBIGUOUS_NONE,
                record_count=0,
                fields=(),
            )
        if not isinstance(result, list):
            raise QmtReadOnlyAdapterError(
                f"QMT_{dataset.value}_RESULT_TYPE_INVALID",
                "XtQuant snapshot query returned an incompatible container",
            )
        if len(result) > _MAX_DATASET_RECORDS:
            raise QmtReadOnlyAdapterError(
                f"QMT_{dataset.value}_RESULT_TOO_LARGE",
                "XtQuant snapshot query exceeded the bounded record count",
            )
        if not result:
            return QmtDatasetObservation(
                dataset=dataset,
                result_kind=QmtSnapshotResultKind.EMPTY_LIST,
                record_count=0,
                fields=(),
            )
        fields, duplicate_count = self._observe_records(result, specs)
        return QmtDatasetObservation(
            dataset=dataset,
            result_kind=QmtSnapshotResultKind.LIST,
            record_count=len(result),
            fields=fields,
            duplicate_identity_count=duplicate_count,
        )

    def _query_sdk(self, method_name: str, dataset_name: str, *arguments: object) -> object:
        if not self._subscribed or self._stopped:
            raise QmtReadOnlyAdapterError(
                "QMT_SNAPSHOT_QUERY_ORDER_INVALID",
                "XtQuant snapshot query lifecycle order is invalid",
            )
        if method_name == "query_stock_asset" and not arguments:
            arguments = (self._account,)
        try:
            method = getattr(self._trader, method_name)
            return method(*arguments)
        except Exception as exc:
            raise QmtReadOnlyAdapterError(
                f"QMT_{dataset_name}_QUERY_FAILED",
                "XtQuant snapshot query failed",
            ) from exc

    def _observe_records(
        self,
        records: tuple[object, ...] | list[object],
        specs: tuple[_FieldSpec, ...],
    ) -> tuple[tuple[QmtFieldShape, ...], int]:
        accumulators = {spec.name: _FieldAccumulator(spec) for spec in specs}
        identity_spec = next((spec for spec in specs if spec.identity), None)
        seen_identities: set[int | str] = set()
        duplicate_count = 0

        for record in records:
            for spec in specs:
                try:
                    value = getattr(record, spec.name)
                except Exception:
                    accumulators[spec.name].accept_missing()
                    continue
                accumulators[spec.name].accept(value)

                if spec.name == "account_id" and (
                    isinstance(value, str) and not hmac.compare_digest(value, self._account_id)
                ):
                    accumulators[spec.name].invalid_count += 1
                if (
                    identity_spec is spec
                    and type(value) in (int, str)
                    and not isinstance(value, bool)
                ):
                    identity = value
                    if identity in seen_identities:
                        duplicate_count += 1
                    else:
                        seen_identities.add(identity)

        return tuple(accumulators[spec.name].freeze() for spec in specs), duplicate_count


def _is_invalid_asset_container(result: object) -> bool:
    return result is None or isinstance(
        result,
        (bool, int, float, str, bytes, list, tuple, dict, set),
    )
