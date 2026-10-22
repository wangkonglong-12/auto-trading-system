from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest

from auto_trading_system.domain import SHANGHAI_TIMEZONE, ShanghaiTimestamp
from auto_trading_system.monitoring.qmt_readonly import (
    QmtAccountStatus,
    QmtProbeConfig,
    QmtProbeOutcome,
    QmtReadOnlyAdapterError,
    QmtSignal,
    QmtSignalKind,
    QmtSignalSink,
    QmtStatusQuery,
)
from auto_trading_system.monitoring.qmt_snapshot import (
    QmtDatasetObservation,
    QmtFieldShape,
    QmtSnapshotDataset,
    QmtSnapshotResultKind,
    run_qmt_snapshot_probe,
    write_qmt_snapshot_report,
)

ACCOUNT_ID = "A123456789"


@dataclass
class FakeClock:
    nanoseconds: int = 0
    wall_time: datetime = datetime(2026, 9, 4, 16, 0, tzinfo=SHANGHAI_TIMEZONE)

    def now(self) -> ShanghaiTimestamp:
        return ShanghaiTimestamp(self.wall_time)

    def monotonic_ns(self) -> int:
        self.nanoseconds += 1_000_000
        return self.nanoseconds


def field_shape(name: str, **overrides: object) -> QmtFieldShape:
    values: dict[str, object] = {
        "name": name,
        "required": True,
        "observed_types": ("int",),
        "missing_count": 0,
        "none_count": 0,
        "invalid_count": 0,
        "non_finite_count": 0,
        "negative_count": 0,
    }
    values.update(overrides)
    return QmtFieldShape(**values)  # type: ignore[arg-type]


def observation(
    dataset: QmtSnapshotDataset,
    *,
    result_kind: QmtSnapshotResultKind | None = None,
    fields: tuple[QmtFieldShape, ...] | None = None,
) -> QmtDatasetObservation:
    is_asset = dataset is QmtSnapshotDataset.ASSET
    kind = result_kind or (QmtSnapshotResultKind.OBJECT if is_asset else QmtSnapshotResultKind.LIST)
    return QmtDatasetObservation(
        dataset=dataset,
        result_kind=kind,
        record_count=1,
        fields=fields or (field_shape("account_type"),),
    )


@dataclass
class FakeSnapshotClient:
    datasets: list[QmtDatasetObservation]
    statuses: list[QmtStatusQuery] = field(
        default_factory=lambda: [QmtStatusQuery(1, (QmtAccountStatus(2, 0),))]
    )
    connect_result: int = 0
    subscribe_result: int = 0
    unsubscribe_result: int = 0
    sdk_version: str | None = "fixture-snapshot"
    signal_after_subscribe: QmtSignal | None = None
    signal_during_asset: QmtSignal | None = None
    query_failure: QmtReadOnlyAdapterError | None = None
    unsubscribe_failure: Exception | None = None
    stop_failure: Exception | None = None
    calls: list[str] = field(default_factory=list)
    sink: QmtSignalSink | None = None

    def register_signal_sink(self, sink: QmtSignalSink) -> None:
        self.calls.append("register")
        self.sink = sink

    def start(self) -> None:
        self.calls.append("start")

    def connect(self) -> int:
        self.calls.append("connect")
        return self.connect_result

    def subscribe(self) -> int:
        self.calls.append("subscribe")
        if self.signal_after_subscribe is not None:
            assert self.sink is not None
            self.sink(self.signal_after_subscribe)
        return self.subscribe_result

    def query_account_statuses(self) -> QmtStatusQuery:
        self.calls.append("status")
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def _query(self, name: str) -> QmtDatasetObservation:
        self.calls.append(name)
        if self.query_failure is not None:
            raise self.query_failure
        if name == "asset" and self.signal_during_asset is not None:
            assert self.sink is not None
            self.sink(self.signal_during_asset)
        return self.datasets.pop(0)

    def query_asset(self) -> QmtDatasetObservation:
        return self._query("asset")

    def query_positions(self) -> QmtDatasetObservation:
        return self._query("positions")

    def query_orders(self) -> QmtDatasetObservation:
        return self._query("orders")

    def query_trades(self) -> QmtDatasetObservation:
        return self._query("trades")

    def unsubscribe(self) -> int:
        self.calls.append("unsubscribe")
        if self.unsubscribe_failure is not None:
            raise self.unsubscribe_failure
        return self.unsubscribe_result

    def stop(self) -> None:
        self.calls.append("stop")
        if self.stop_failure is not None:
            raise self.stop_failure


def config(tmp_path: Path) -> QmtProbeConfig:
    userdata = (tmp_path / "userdata_mini").resolve()
    (userdata / "up_queue_xtquant").mkdir(parents=True)
    return QmtProbeConfig(userdata, 131002, "STOCK")


def all_datasets() -> list[QmtDatasetObservation]:
    return [observation(dataset) for dataset in QmtSnapshotDataset]


def factory_for(client: FakeSnapshotClient):  # type: ignore[no-untyped-def]
    def factory(probe_config: QmtProbeConfig, account_id: str) -> FakeSnapshotClient:
        assert probe_config.session_id == 131002
        assert account_id == ACCOUNT_ID
        return client

    return factory


def test_probe_queries_all_datasets_only_while_account_is_ok_and_omits_values(
    tmp_path: Path,
) -> None:
    client = FakeSnapshotClient(all_datasets())

    report = run_qmt_snapshot_probe(
        config(tmp_path),
        account_id=ACCOUNT_ID,
        config_hash="sha256:config",
        client_factory=factory_for(client),
        clock=FakeClock(),
    )

    assert report.outcome is QmtProbeOutcome.PASS
    assert report.failure_code is None
    assert report.status_query_count == 5
    assert [item.dataset for item in report.datasets] == list(QmtSnapshotDataset)
    assert client.calls == [
        "register",
        "start",
        "connect",
        "subscribe",
        "status",
        "asset",
        "status",
        "positions",
        "status",
        "orders",
        "status",
        "trades",
        "status",
        "unsubscribe",
        "stop",
    ]
    rendered = json.dumps(report.to_payload(), ensure_ascii=False)
    assert ACCOUNT_ID not in rendered
    assert str(tmp_path) not in rendered
    assert report.to_payload()["privacy"]["financial_values_omitted"] is True  # type: ignore[index]


@pytest.mark.parametrize(
    ("dataset", "kind"),
    [
        (QmtSnapshotDataset.ASSET, QmtSnapshotResultKind.ASSET_NONE),
        (QmtSnapshotDataset.POSITIONS, QmtSnapshotResultKind.AMBIGUOUS_NONE),
    ],
)
def test_none_is_indeterminate_but_remaining_readonly_queries_are_collected(
    tmp_path: Path,
    dataset: QmtSnapshotDataset,
    kind: QmtSnapshotResultKind,
) -> None:
    datasets = all_datasets()
    index = list(QmtSnapshotDataset).index(dataset)
    datasets[index] = QmtDatasetObservation(dataset, kind, 0, ())
    client = FakeSnapshotClient(datasets)

    report = run_qmt_snapshot_probe(
        config(tmp_path),
        account_id=ACCOUNT_ID,
        config_hash="sha256:config",
        client_factory=factory_for(client),
        clock=FakeClock(),
    )

    assert report.outcome is QmtProbeOutcome.BLOCKED
    assert report.failure_code == "QMT_SNAPSHOT_QUERY_INDETERMINATE"
    assert len(report.datasets) == 4
    assert client.calls[-2:] == ["unsubscribe", "stop"]


def test_invalid_field_shape_fails_closed_before_later_queries(tmp_path: Path) -> None:
    invalid_asset = observation(
        QmtSnapshotDataset.ASSET,
        fields=(field_shape("cash", invalid_count=1, observed_types=("bool",)),),
    )
    client = FakeSnapshotClient([invalid_asset, *all_datasets()[1:]])

    report = run_qmt_snapshot_probe(
        config(tmp_path),
        account_id=ACCOUNT_ID,
        config_hash="sha256:config",
        client_factory=factory_for(client),
        clock=FakeClock(),
    )

    assert report.outcome is QmtProbeOutcome.ERROR
    assert report.failure_code == "QMT_SNAPSHOT_FIELDS_INVALID"
    assert "positions" not in client.calls
    assert client.calls[-2:] == ["unsubscribe", "stop"]


@pytest.mark.parametrize(
    ("connect_result", "subscribe_result", "expected_code", "absent_call"),
    [
        (-1, 0, "QMT_SNAPSHOT_CONNECT_FAILED", "subscribe"),
        (0, -1, "QMT_SNAPSHOT_SUBSCRIBE_FAILED", "status"),
    ],
)
def test_connect_and_subscribe_failures_never_query_account_data(
    tmp_path: Path,
    connect_result: int,
    subscribe_result: int,
    expected_code: str,
    absent_call: str,
) -> None:
    client = FakeSnapshotClient(
        all_datasets(),
        connect_result=connect_result,
        subscribe_result=subscribe_result,
    )

    report = run_qmt_snapshot_probe(
        config(tmp_path),
        account_id=ACCOUNT_ID,
        config_hash="sha256:config",
        client_factory=factory_for(client),
        clock=FakeClock(),
    )

    assert report.outcome is QmtProbeOutcome.BLOCKED
    assert report.failure_code == expected_code
    assert absent_call not in client.calls
    assert "asset" not in client.calls


@pytest.mark.parametrize(
    ("signal", "expected_code", "expected_outcome"),
    [
        (
            QmtSignal(QmtSignalKind.INVALID_ACCOUNT_STATUS),
            "QMT_SNAPSHOT_CALLBACK_INVALID",
            QmtProbeOutcome.ERROR,
        ),
        (
            QmtSignal(QmtSignalKind.ACCOUNT_STATUS, account_type=2, status=5),
            "QMT_SNAPSHOT_ACCOUNT_NOT_OK",
            QmtProbeOutcome.BLOCKED,
        ),
    ],
)
def test_callback_failure_blocks_before_snapshot_queries(
    tmp_path: Path,
    signal: QmtSignal,
    expected_code: str,
    expected_outcome: QmtProbeOutcome,
) -> None:
    client = FakeSnapshotClient(all_datasets(), signal_after_subscribe=signal)

    report = run_qmt_snapshot_probe(
        config(tmp_path),
        account_id=ACCOUNT_ID,
        config_hash="sha256:config",
        client_factory=factory_for(client),
        clock=FakeClock(),
    )

    assert report.outcome is expected_outcome
    assert report.failure_code == expected_code
    assert "asset" not in client.calls


def test_duplicate_target_account_status_is_a_technical_error(tmp_path: Path) -> None:
    duplicate = QmtStatusQuery(
        2,
        (QmtAccountStatus(2, 0), QmtAccountStatus(2, 0)),
    )
    client = FakeSnapshotClient(all_datasets(), statuses=[duplicate])

    report = run_qmt_snapshot_probe(
        config(tmp_path),
        account_id=ACCOUNT_ID,
        config_hash="sha256:config",
        client_factory=factory_for(client),
        clock=FakeClock(),
    )

    assert report.outcome is QmtProbeOutcome.ERROR
    assert report.failure_code == "QMT_SNAPSHOT_ACCOUNT_STATUS_INVALID"
    assert "asset" not in client.calls


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([QmtStatusQuery(0, ())], "QMT_SNAPSHOT_ACCOUNT_NOT_FOUND"),
        ([QmtStatusQuery(1, (QmtAccountStatus(2, 5),))], "QMT_SNAPSHOT_ACCOUNT_NOT_OK"),
    ],
)
def test_account_must_be_present_and_ok_before_first_snapshot_query(
    tmp_path: Path,
    statuses: list[QmtStatusQuery],
    expected: str,
) -> None:
    client = FakeSnapshotClient(all_datasets(), statuses=statuses)

    report = run_qmt_snapshot_probe(
        config(tmp_path),
        account_id=ACCOUNT_ID,
        config_hash="sha256:config",
        client_factory=factory_for(client),
        clock=FakeClock(),
    )

    assert report.outcome is QmtProbeOutcome.BLOCKED
    assert report.failure_code == expected
    assert "asset" not in client.calls


def test_disconnect_during_query_stops_before_next_dataset(tmp_path: Path) -> None:
    client = FakeSnapshotClient(
        all_datasets(),
        signal_during_asset=QmtSignal(QmtSignalKind.DISCONNECTED),
    )

    report = run_qmt_snapshot_probe(
        config(tmp_path),
        account_id=ACCOUNT_ID,
        config_hash="sha256:config",
        client_factory=factory_for(client),
        clock=FakeClock(),
    )

    assert report.outcome is QmtProbeOutcome.BLOCKED
    assert report.failure_code == "QMT_SNAPSHOT_DISCONNECTED"
    assert [item.dataset for item in report.datasets] == [QmtSnapshotDataset.ASSET]
    assert "positions" not in client.calls


def test_cleanup_failure_overrides_primary_result(tmp_path: Path) -> None:
    client = FakeSnapshotClient(
        all_datasets(),
        stop_failure=QmtReadOnlyAdapterError("QMT_STOP_FAILED", "protected"),
    )

    report = run_qmt_snapshot_probe(
        config(tmp_path),
        account_id=ACCOUNT_ID,
        config_hash="sha256:config",
        client_factory=factory_for(client),
        clock=FakeClock(),
    )

    assert report.outcome is QmtProbeOutcome.ERROR
    assert report.failure_code == "QMT_SNAPSHOT_CLEANUP_FAILED"
    assert report.primary_failure_code is None
    assert report.cleanup_failure_codes == ("QMT_STOP_FAILED",)


def test_query_and_unsubscribe_failures_preserve_stable_primary_and_cleanup_codes(
    tmp_path: Path,
) -> None:
    client = FakeSnapshotClient(
        all_datasets(),
        query_failure=QmtReadOnlyAdapterError("QMT_ASSET_QUERY_FAILED", "protected"),
        unsubscribe_failure=RuntimeError("protected"),
    )

    report = run_qmt_snapshot_probe(
        config(tmp_path),
        account_id=ACCOUNT_ID,
        config_hash="sha256:config",
        client_factory=factory_for(client),
        clock=FakeClock(),
    )

    assert report.outcome is QmtProbeOutcome.ERROR
    assert report.failure_code == "QMT_SNAPSHOT_CLEANUP_FAILED"
    assert report.primary_failure_code == "QMT_ASSET_QUERY_FAILED"
    assert report.cleanup_failure_codes == ("QMT_SNAPSHOT_UNSUBSCRIBE_UNEXPECTED_FAILURE",)


def test_snapshot_report_is_immutable_and_redacted(tmp_path: Path) -> None:
    report = run_qmt_snapshot_probe(
        config(tmp_path),
        account_id=ACCOUNT_ID,
        config_hash="sha256:config",
        client_factory=factory_for(FakeSnapshotClient(all_datasets())),
        clock=FakeClock(),
    )
    destination = (tmp_path / "snapshot.json").resolve()

    digest = write_qmt_snapshot_report(
        destination,
        report,
        forbidden_values=(ACCOUNT_ID,),
    )

    assert len(digest) == 64
    assert ACCOUNT_ID not in destination.read_text(encoding="utf-8")
    with pytest.raises(Exception, match="QMT_PROBE_OUTPUT_EXISTS"):
        write_qmt_snapshot_report(destination, report)


def test_shape_models_reject_inconsistent_or_unbounded_evidence() -> None:
    with pytest.raises(ValueError):
        field_shape("bad-field")
    with pytest.raises(ValueError):
        field_shape("cash", observed_types=("str", "int"))
    with pytest.raises(ValueError):
        QmtDatasetObservation(
            QmtSnapshotDataset.ORDERS,
            QmtSnapshotResultKind.EMPTY_LIST,
            1,
            (),
        )
    with pytest.raises(ValueError):
        QmtDatasetObservation(
            QmtSnapshotDataset.TRADES,
            QmtSnapshotResultKind.LIST,
            10_001,
            (),
        )
