from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest

from auto_trading_system.domain import SHANGHAI_TIMEZONE, SecurityCode, ShanghaiTimestamp
from auto_trading_system.monitoring.qmt_readonly import (
    QmtProbeOutcome,
    QmtReadOnlyAdapterError,
)
from auto_trading_system.monitoring.qmt_tick import (
    QmtTickFieldSample,
    QmtTickProbeConfig,
    QmtTickProbeReport,
    QmtTickSample,
    QmtTickScenario,
    QmtTickSink,
    QmtTickSnapshotBatch,
    QmtTickSource,
    QmtTimestampUnit,
    fingerprint_symbol,
    run_qmt_tick_probe,
    write_qmt_tick_report,
)

SYMBOLS = (SecurityCode.parse("600000.SH"), SecurityCode.parse("000001.SZ"))


@dataclass
class FakeClock:
    nanoseconds: int = 0
    wall_time: datetime = datetime(2026, 9, 4, 14, 50, tzinfo=SHANGHAI_TIMEZONE)

    def now(self) -> ShanghaiTimestamp:
        return ShanghaiTimestamp(self.wall_time)

    def monotonic_ns(self) -> int:
        self.nanoseconds += 1_000_000
        return self.nanoseconds


def sample(symbol: SecurityCode, source: QmtTickSource, *, valid: bool = True) -> QmtTickSample:
    return QmtTickSample(
        symbol_fingerprint=fingerprint_symbol(symbol),
        source=source,
        fields=(QmtTickFieldSample("time", True, "int", valid),),
        timestamp_unit=QmtTimestampUnit.MILLISECONDS,
        volume_multiplier_candidate=100,
        stock_status_code=13,
        order_book_depth_mismatch=False,
    )


def batch(*, missing: int = 0, unexpected: int = 0, valid: bool = True) -> QmtTickSnapshotBatch:
    return QmtTickSnapshotBatch(
        tuple(sample(symbol, QmtTickSource.SNAPSHOT, valid=valid) for symbol in SYMBOLS),
        missing,
        unexpected,
    )


@dataclass
class FakeClient:
    snapshot_batch: QmtTickSnapshotBatch = field(default_factory=batch)
    subscription_results: list[int] = field(default_factory=lambda: [10, 20])
    callback_symbols: tuple[SecurityCode, ...] = SYMBOLS
    callback_valid: bool = True
    callback_error: bool = False
    query_failure: QmtReadOnlyAdapterError | None = None
    unsubscribe_failure: QmtReadOnlyAdapterError | None = None
    sdk_version: str | None = "fixture"
    calls: list[str] = field(default_factory=list)
    sinks: list[QmtTickSink] = field(default_factory=list)
    error_sinks: list[object] = field(default_factory=list)

    def subscribe(self, symbol: SecurityCode, sample_sink: QmtTickSink, error_sink: object) -> int:
        self.calls.append(f"subscribe:{symbol}")
        self.sinks.append(sample_sink)
        self.error_sinks.append(error_sink)
        return self.subscription_results[len(self.sinks) - 1]

    def get_full_tick(self, symbols: tuple[SecurityCode, ...]) -> QmtTickSnapshotBatch:
        self.calls.append("snapshot")
        assert symbols == SYMBOLS
        if self.query_failure is not None:
            raise self.query_failure
        return self.snapshot_batch

    def wait_for_callbacks(self, seconds: int) -> None:
        self.calls.append(f"wait:{seconds}")
        if self.callback_error:
            for sink in self.error_sinks:
                assert callable(sink)
                sink("INJECTED")
            return
        sink = self.sinks[0]
        for symbol in self.callback_symbols:
            sink(sample(symbol, QmtTickSource.CALLBACK, valid=self.callback_valid))

    def unsubscribe(self, subscription_id: int) -> None:
        self.calls.append(f"unsubscribe:{subscription_id}")
        if self.unsubscribe_failure is not None:
            raise self.unsubscribe_failure


def run_probe(
    client: FakeClient, scenario: QmtTickScenario = QmtTickScenario.ACTIVE
) -> QmtTickProbeReport:
    return run_qmt_tick_probe(
        QmtTickProbeConfig(SYMBOLS, scenario, observation_seconds=2),
        config_hash="a" * 64,
        client_factory=lambda: client,
        clock=FakeClock(),
    )


def test_active_probe_collects_snapshot_callback_shapes_and_cleans_up(tmp_path: Path) -> None:
    client = FakeClient()

    report = run_probe(client)

    assert report.outcome is QmtProbeOutcome.PASS
    assert report.exit_code == 0
    assert report.accepted_sample_count == 4
    assert report.invalid_callback_count == 0
    assert report.subscription_count == 2
    assert client.calls[-2:] == ["unsubscribe:20", "unsubscribe:10"]
    callback_evidence = [item for item in report.symbols if item.source is QmtTickSource.CALLBACK]
    assert all(item.sample_count == 1 and item.structurally_valid for item in callback_evidence)
    assert all(item.volume_multiplier_candidates == (100,) for item in callback_evidence)

    destination = tmp_path / "tick-report.json"
    digest = write_qmt_tick_report(
        destination,
        report,
        forbidden_values=tuple(symbol.to_wire() for symbol in SYMBOLS),
    )
    contents = destination.read_text(encoding="utf-8")
    assert len(digest) == 64
    assert all(symbol.to_wire() not in contents for symbol in SYMBOLS)
    assert json.loads(contents)["schema"] == "qmt-tick-probe/v1"


def test_active_probe_blocks_when_one_symbol_has_no_callback() -> None:
    report = run_probe(FakeClient(callback_symbols=SYMBOLS[:1]))

    assert report.outcome is QmtProbeOutcome.BLOCKED
    assert report.failure_code == "QMT_TICK_CALLBACK_MISSING"
    assert report.exit_code == 2


@pytest.mark.parametrize("scenario", [QmtTickScenario.HALTED, QmtTickScenario.CLOSED])
def test_non_streaming_scenarios_allow_no_callback(scenario: QmtTickScenario) -> None:
    report = run_probe(FakeClient(callback_symbols=()), scenario)

    assert report.outcome is QmtProbeOutcome.PASS


def test_missing_snapshot_blocks_without_waiting() -> None:
    client = FakeClient(snapshot_batch=batch(missing=1))

    report = run_probe(client)

    assert report.outcome is QmtProbeOutcome.BLOCKED
    assert report.failure_code == "QMT_TICK_SNAPSHOT_MISSING"
    assert not any(call.startswith("wait:") for call in client.calls)


def test_unexpected_snapshot_symbol_is_an_error() -> None:
    report = run_probe(FakeClient(snapshot_batch=batch(unexpected=1)))

    assert report.outcome is QmtProbeOutcome.ERROR
    assert report.failure_code == "QMT_TICK_SNAPSHOT_UNEXPECTED_SYMBOL"


@pytest.mark.parametrize(
    ("client", "failure_code"),
    [
        (FakeClient(callback_valid=False), "QMT_TICK_FIELDS_INVALID"),
        (FakeClient(callback_error=True), "QMT_TICK_FIELDS_INVALID"),
        (FakeClient(snapshot_batch=batch(valid=False)), "QMT_TICK_SNAPSHOT_INVALID"),
    ],
)
def test_invalid_callback_or_fields_fail_closed(client: FakeClient, failure_code: str) -> None:
    report = run_probe(client)

    assert report.outcome is QmtProbeOutcome.ERROR
    assert report.failure_code == failure_code


def test_subscription_failure_stops_before_snapshot_and_cleans_successes() -> None:
    client = FakeClient(subscription_results=[10, -1])

    report = run_probe(client)

    assert report.outcome is QmtProbeOutcome.BLOCKED
    assert report.failure_code == "QMT_TICK_SUBSCRIBE_FAILED"
    assert "snapshot" not in client.calls
    assert client.calls[-1] == "unsubscribe:10"


def test_duplicate_subscription_id_is_an_error() -> None:
    report = run_probe(FakeClient(subscription_results=[10, 10]))

    assert report.outcome is QmtProbeOutcome.ERROR
    assert report.failure_code == "QMT_TICK_SUBSCRIPTION_ID_DUPLICATE"
    assert report.unsubscribe_attempt_count == 1


def test_adapter_failure_is_stable_and_cleanup_failure_preserves_primary() -> None:
    report = run_probe(
        FakeClient(
            query_failure=QmtReadOnlyAdapterError("QMT_TICK_QUERY_INJECTED", "secret"),
            unsubscribe_failure=QmtReadOnlyAdapterError("QMT_TICK_UNSUBSCRIBE_INJECTED", "secret"),
        )
    )

    assert report.outcome is QmtProbeOutcome.ERROR
    assert report.failure_code == "QMT_TICK_CLEANUP_FAILED"
    assert report.primary_failure_code == "QMT_TICK_QUERY_INJECTED"
    assert report.cleanup_failure_codes == (
        "QMT_TICK_UNSUBSCRIBE_INJECTED",
        "QMT_TICK_UNSUBSCRIBE_INJECTED",
    )


@pytest.mark.parametrize(
    "config",
    [
        lambda: QmtTickProbeConfig((), QmtTickScenario.ACTIVE),
        lambda: QmtTickProbeConfig((SYMBOLS[0], SYMBOLS[0]), QmtTickScenario.ACTIVE),
        lambda: QmtTickProbeConfig(SYMBOLS, QmtTickScenario.ACTIVE, 0),
    ],
)
def test_probe_configuration_rejects_unsafe_bounds(config: object) -> None:
    with pytest.raises(ValueError):
        assert callable(config)
        config()
