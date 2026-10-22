from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest

from auto_trading_system.domain import SHANGHAI_TIMEZONE, ShanghaiTimestamp
from auto_trading_system.monitoring.qmt_readonly import (
    QmtAccountStatus,
    QmtProbeConfig,
    QmtProbeOutcome,
    QmtProbeOutputError,
    QmtReadOnlyAdapterError,
    QmtSignal,
    QmtSignalKind,
    QmtSignalSink,
    QmtStatusQuery,
    run_qmt_readonly_probe,
    write_qmt_probe_report,
)

ACCOUNT_ID = "A123456789"


@dataclass
class FakeClock:
    nanoseconds: int = 0
    wall_time: datetime = datetime(2026, 9, 4, 14, 30, tzinfo=SHANGHAI_TIMEZONE)

    def now(self) -> ShanghaiTimestamp:
        return ShanghaiTimestamp(self.wall_time)

    def monotonic_ns(self) -> int:
        return self.nanoseconds

    def wait(self, seconds: float) -> None:
        self.nanoseconds += round(seconds * 1_000_000_000)


@dataclass
class FakeReadOnlyClient:
    query_results: list[QmtStatusQuery | Exception]
    connect_result: int = 0
    subscribe_result: int = 0
    unsubscribe_result: int = 0
    sdk_version: str | None = "1.0.fixture"
    signal_after_subscribe: QmtSignal | None = None
    stop_failure: QmtReadOnlyAdapterError | None = None
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
        self.calls.append("query")
        result = self.query_results.pop(0) if len(self.query_results) > 1 else self.query_results[0]
        if isinstance(result, Exception):
            raise result
        return result

    def unsubscribe(self) -> int:
        self.calls.append("unsubscribe")
        return self.unsubscribe_result

    def stop(self) -> None:
        self.calls.append("stop")
        if self.stop_failure is not None:
            raise self.stop_failure


def probe_config(tmp_path: Path, **overrides: object) -> QmtProbeConfig:
    userdata = tmp_path / "userdata_mini"
    (userdata / "up_queue_xtquant").mkdir(parents=True)
    values: dict[str, object] = {
        "userdata_mini": userdata,
        "session_id": 131001,
        "account_type": "STOCK",
        "observation_seconds": 2,
        "query_interval_milliseconds": 500,
    }
    values.update(overrides)
    return QmtProbeConfig(**values)  # type: ignore[arg-type]


def factory_for(client: FakeReadOnlyClient) -> Callable[[QmtProbeConfig, str], FakeReadOnlyClient]:
    def factory(config: QmtProbeConfig, account_id: str) -> FakeReadOnlyClient:
        assert config.account_type == "STOCK"
        assert account_id == ACCOUNT_ID
        return client

    return factory


def test_probe_requires_query_status_ok_and_records_ordered_sanitized_evidence(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    client = FakeReadOnlyClient(
        query_results=[
            QmtStatusQuery(1, (QmtAccountStatus(account_type=2, status=1),)),
            QmtStatusQuery(1, (QmtAccountStatus(account_type=2, status=0),)),
        ],
        signal_after_subscribe=QmtSignal(
            kind=QmtSignalKind.ACCOUNT_STATUS,
            account_type=2,
            status=1,
        ),
    )

    report = run_qmt_readonly_probe(
        probe_config(tmp_path),
        account_id=ACCOUNT_ID,
        config_hash="sha256:config",
        client_factory=factory_for(client),
        clock=clock,
        wait=clock.wait,
    )

    assert report.outcome is QmtProbeOutcome.PASS
    assert report.failure_code is None
    assert report.exit_code == 0
    assert report.status_query_count == 2
    assert client.calls == [
        "register",
        "start",
        "connect",
        "subscribe",
        "query",
        "query",
        "unsubscribe",
        "stop",
    ]
    payload = report.to_payload()
    assert [item["status_name"] for item in payload["status"]["observations"]] == [  # type: ignore[index]
        "WAITING_LOGIN",
        "WAITING_LOGIN",
        "OK",
    ]
    rendered = json.dumps(payload, ensure_ascii=False)
    assert ACCOUNT_ID not in rendered
    assert str(tmp_path) not in rendered
    assert payload["target_account"]["masked"].endswith("6789")  # type: ignore[index]


@pytest.mark.parametrize(
    ("client", "failure_code", "expected_calls"),
    [
        (
            FakeReadOnlyClient(query_results=[QmtStatusQuery(0, ())], connect_result=-1),
            "QMT_CONNECT_FAILED",
            ["register", "start", "connect", "stop"],
        ),
        (
            FakeReadOnlyClient(query_results=[QmtStatusQuery(0, ())], subscribe_result=-1),
            "QMT_SUBSCRIBE_FAILED",
            ["register", "start", "connect", "subscribe", "stop"],
        ),
    ],
)
def test_probe_fails_closed_before_status_query(
    tmp_path: Path,
    client: FakeReadOnlyClient,
    failure_code: str,
    expected_calls: list[str],
) -> None:
    report = run_qmt_readonly_probe(
        probe_config(tmp_path),
        account_id=ACCOUNT_ID,
        config_hash="sha256:config",
        client_factory=factory_for(client),
        clock=FakeClock(),
        wait=lambda _: None,
    )

    assert report.outcome is QmtProbeOutcome.BLOCKED
    assert report.failure_code == failure_code
    assert report.exit_code == 2
    assert client.calls == expected_calls


@pytest.mark.parametrize(
    ("query", "expected_code"),
    [
        (
            QmtStatusQuery(1, (QmtAccountStatus(account_type=2, status=5),)),
            "QMT_ACCOUNT_NOT_OK",
        ),
        (QmtStatusQuery(1, ()), "QMT_ACCOUNT_NOT_FOUND"),
    ],
)
def test_probe_times_out_with_distinct_target_account_conclusions(
    tmp_path: Path,
    query: QmtStatusQuery,
    expected_code: str,
) -> None:
    clock = FakeClock()
    client = FakeReadOnlyClient(query_results=[query])

    report = run_qmt_readonly_probe(
        probe_config(tmp_path, observation_seconds=1),
        account_id=ACCOUNT_ID,
        config_hash="sha256:config",
        client_factory=factory_for(client),
        clock=clock,
        wait=clock.wait,
    )

    assert report.outcome is QmtProbeOutcome.BLOCKED
    assert report.failure_code == expected_code
    assert report.status_query_count == 3
    assert client.calls[-2:] == ["unsubscribe", "stop"]


@pytest.mark.parametrize(
    ("signal", "expected_code"),
    [
        (QmtSignal(QmtSignalKind.DISCONNECTED), "QMT_DISCONNECTED"),
        (QmtSignal(QmtSignalKind.INVALID_ACCOUNT_STATUS), "QMT_CALLBACK_PAYLOAD_INVALID"),
    ],
)
def test_probe_rejects_disconnect_or_invalid_callback_before_query(
    tmp_path: Path,
    signal: QmtSignal,
    expected_code: str,
) -> None:
    client = FakeReadOnlyClient(
        query_results=[QmtStatusQuery(1, (QmtAccountStatus(2, 0),))],
        signal_after_subscribe=signal,
    )

    report = run_qmt_readonly_probe(
        probe_config(tmp_path),
        account_id=ACCOUNT_ID,
        config_hash="sha256:config",
        client_factory=factory_for(client),
        clock=FakeClock(),
        wait=lambda _: None,
    )

    assert report.failure_code == expected_code
    assert "query" not in client.calls
    assert client.calls[-2:] == ["unsubscribe", "stop"]


def test_adapter_failure_is_stable_and_cleanup_failure_overrides_primary_result(
    tmp_path: Path,
) -> None:
    client = FakeReadOnlyClient(
        query_results=[QmtReadOnlyAdapterError("QMT_STATUS_QUERY_NONE", "secret detail")],
        stop_failure=QmtReadOnlyAdapterError("QMT_STOP_FAILED", "secret detail"),
    )

    report = run_qmt_readonly_probe(
        probe_config(tmp_path),
        account_id=ACCOUNT_ID,
        config_hash="sha256:config",
        client_factory=factory_for(client),
        clock=FakeClock(),
        wait=lambda _: None,
    )

    assert report.outcome is QmtProbeOutcome.ERROR
    assert report.failure_code == "QMT_CLEANUP_FAILED"
    assert report.primary_failure_code == "QMT_STATUS_QUERY_NONE"
    assert report.cleanup_failure_codes == ("QMT_STOP_FAILED",)
    assert "secret detail" not in json.dumps(report.to_payload())


def test_report_writer_is_atomic_immutable_and_scans_protected_values(tmp_path: Path) -> None:
    clock = FakeClock()
    report = run_qmt_readonly_probe(
        probe_config(tmp_path),
        account_id=ACCOUNT_ID,
        config_hash="sha256:config",
        client_factory=factory_for(
            FakeReadOnlyClient(query_results=[QmtStatusQuery(1, (QmtAccountStatus(2, 0),))])
        ),
        clock=clock,
        wait=clock.wait,
    )
    destination = (tmp_path / "probe.json").resolve()

    digest = write_qmt_probe_report(
        destination,
        report,
        forbidden_values=(ACCOUNT_ID,),
    )

    contents = destination.read_bytes()
    assert digest == hashlib.sha256(contents).hexdigest()
    assert ACCOUNT_ID.encode() not in contents
    assert json.loads(contents)["outcome"] == "PASS"
    with pytest.raises(QmtProbeOutputError, match="QMT_PROBE_OUTPUT_EXISTS"):
        write_qmt_probe_report(destination, report)
    assert destination.read_bytes() == contents
    with pytest.raises(QmtProbeOutputError, match="QMT_PROBE_OUTPUT_INVALID"):
        write_qmt_probe_report(Path("relative.json"), report)


def test_minimum_length_account_is_fully_masked(tmp_path: Path) -> None:
    account_id = "A123"
    report = run_qmt_readonly_probe(
        probe_config(tmp_path),
        account_id=account_id,
        config_hash="sha256:config",
        client_factory=lambda *_: FakeReadOnlyClient(
            query_results=[QmtStatusQuery(1, (QmtAccountStatus(2, 0),))]
        ),
        clock=FakeClock(),
        wait=lambda _: None,
    )

    target = report.to_payload()["target_account"]
    assert isinstance(target, dict)
    assert target["masked"] == "****"
    assert account_id not in json.dumps(report.to_payload())


@pytest.mark.parametrize(
    "changes",
    [
        {"session_id": 0},
        {"account_type": "CREDIT"},
        {"observation_seconds": 0},
        {"query_interval_milliseconds": 99},
        {"query_interval_milliseconds": 2_001},
    ],
)
def test_probe_configuration_rejects_unsafe_boundaries(
    tmp_path: Path,
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        probe_config(tmp_path, **changes)
