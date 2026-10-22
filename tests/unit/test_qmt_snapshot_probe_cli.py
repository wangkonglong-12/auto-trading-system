from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest

from auto_trading_system.apps.qmt_snapshot_probe.__main__ import run
from auto_trading_system.domain import SHANGHAI_TIMEZONE, ShanghaiTimestamp
from auto_trading_system.monitoring.qmt_readonly import (
    QmtAccountStatus,
    QmtProbeConfig,
    QmtSignalSink,
    QmtStatusQuery,
)
from auto_trading_system.monitoring.qmt_snapshot import (
    QmtDatasetObservation,
    QmtFieldShape,
    QmtSnapshotDataset,
    QmtSnapshotResultKind,
)
from auto_trading_system.runtime import InstanceAlreadyRunningError, InstanceGuardError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "v1" / "runtime.read-only.example.json"
ACCOUNT_ID = "A123456789"


@dataclass
class FakeClock:
    nanoseconds: int = 0
    wall_time: datetime = datetime(2026, 9, 4, 16, 30, tzinfo=SHANGHAI_TIMEZONE)

    def now(self) -> ShanghaiTimestamp:
        return ShanghaiTimestamp(self.wall_time)

    def monotonic_ns(self) -> int:
        self.nanoseconds += 1_000_000
        return self.nanoseconds


@dataclass
class FakeGuard:
    acquire_failure: InstanceGuardError | None = None
    release_failure: InstanceGuardError | None = None
    calls: list[str] = field(default_factory=list)

    def acquire(self) -> None:
        self.calls.append("acquire")
        if self.acquire_failure is not None:
            raise self.acquire_failure

    def release(self) -> None:
        self.calls.append("release")
        if self.release_failure is not None:
            raise self.release_failure


def shape() -> QmtFieldShape:
    return QmtFieldShape("account_type", True, ("int",), 0, 0, 0, 0, 0)


@dataclass
class PassingClient:
    sdk_version: str | None = "fixture"
    calls: list[str] = field(default_factory=list)

    def register_signal_sink(self, sink: QmtSignalSink) -> None:
        assert callable(sink)
        self.calls.append("register")

    def start(self) -> None:
        self.calls.append("start")

    def connect(self) -> int:
        self.calls.append("connect")
        return 0

    def subscribe(self) -> int:
        self.calls.append("subscribe")
        return 0

    def query_account_statuses(self) -> QmtStatusQuery:
        self.calls.append("status")
        return QmtStatusQuery(1, (QmtAccountStatus(2, 0),))

    def _query(self, dataset: QmtSnapshotDataset) -> QmtDatasetObservation:
        self.calls.append(dataset.value.casefold())
        return QmtDatasetObservation(
            dataset,
            QmtSnapshotResultKind.OBJECT
            if dataset is QmtSnapshotDataset.ASSET
            else QmtSnapshotResultKind.LIST,
            1,
            (shape(),),
        )

    def query_asset(self) -> QmtDatasetObservation:
        return self._query(QmtSnapshotDataset.ASSET)

    def query_positions(self) -> QmtDatasetObservation:
        return self._query(QmtSnapshotDataset.POSITIONS)

    def query_orders(self) -> QmtDatasetObservation:
        return self._query(QmtSnapshotDataset.ORDERS)

    def query_trades(self) -> QmtDatasetObservation:
        return self._query(QmtSnapshotDataset.TRADES)

    def unsubscribe(self) -> int:
        self.calls.append("unsubscribe")
        return 0

    def stop(self) -> None:
        self.calls.append("stop")


def write_config(tmp_path: Path, *, profile: str = "READ_ONLY") -> Path:
    payload = copy.deepcopy(json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8")))
    userdata = tmp_path / "userdata_mini"
    (userdata / "up_queue_xtquant").mkdir(parents=True)
    payload["profile"] = profile
    payload["qmt"]["userdata_mini"] = str(userdata)
    path = (tmp_path / f"snapshot-{profile.casefold()}.json").resolve()
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def assert_factory(
    probe_config: QmtProbeConfig,
    account_id: str,
    client: PassingClient,
) -> PassingClient:
    assert probe_config.account_type == "STOCK"
    assert account_id == ACCOUNT_ID
    return client


def test_cli_writes_privacy_safe_report_after_releasing_account_guard(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = write_config(tmp_path)
    output_path = (tmp_path / "snapshot-report.json").resolve()
    client = PassingClient()
    guard = FakeGuard()

    result = run(
        ["--config", str(config_path), "--output", str(output_path)],
        environment={"ATS_SECRET_BROKER_ACCOUNT": ACCOUNT_ID},
        client_factory=lambda config, account: assert_factory(config, account, client),
        instance_guard_factory=lambda account: guard if account == ACCOUNT_ID else pytest.fail(),
        clock=FakeClock(),
    )

    assert result == 0
    assert guard.calls == ["acquire", "release"]
    summary = json.loads(capsys.readouterr().out)
    assert summary["outcome"] == "PASS"
    contents = output_path.read_text(encoding="utf-8")
    assert ACCOUNT_ID not in contents
    assert json.loads(contents)["schema"] == "qmt-snapshot-probe/v1"
    assert client.calls[-2:] == ["unsubscribe", "stop"]


def test_cli_refuses_qmt_when_account_guard_is_owned(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = write_config(tmp_path)
    output_path = (tmp_path / "blocked.json").resolve()
    guard = FakeGuard(acquire_failure=InstanceAlreadyRunningError("0123456789ab"))

    result = run(
        ["--config", str(config_path), "--output", str(output_path)],
        environment={"ATS_SECRET_BROKER_ACCOUNT": ACCOUNT_ID},
        client_factory=lambda *_: pytest.fail("QMT client must not be created"),
        instance_guard_factory=lambda _: guard,
    )

    assert result == 2
    assert capsys.readouterr().err.strip() == "ACCOUNT_INSTANCE_ALREADY_RUNNING"
    assert guard.calls == ["acquire"]
    assert not output_path.exists()


def test_cli_rejects_non_readonly_profile_before_secret_guard_or_qmt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = write_config(tmp_path, profile="PAPER")
    output_path = (tmp_path / "paper.json").resolve()

    result = run(
        ["--config", str(config_path), "--output", str(output_path)],
        environment={},
        client_factory=lambda *_: pytest.fail("QMT client must not be created"),
        instance_guard_factory=lambda _: pytest.fail("guard must not be created"),
    )

    assert result == 2
    assert capsys.readouterr().err.strip() == "QMT_SNAPSHOT_PROFILE_NOT_READ_ONLY"
    assert not output_path.exists()


def test_cli_never_overwrites_existing_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_config(tmp_path)
    output_path = (tmp_path / "existing.json").resolve()
    output_path.write_text("preserve", encoding="utf-8")

    result = run(
        ["--config", str(config_path), "--output", str(output_path)],
        environment={},
        client_factory=lambda *_: pytest.fail("QMT client must not be created"),
    )

    assert result == 3
    assert capsys.readouterr().err.strip() == "QMT_PROBE_OUTPUT_EXISTS"
    assert output_path.read_text(encoding="utf-8") == "preserve"


def test_cli_does_not_publish_when_guard_release_fails(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = write_config(tmp_path)
    output_path = (tmp_path / "release-failed.json").resolve()
    guard = FakeGuard(
        release_failure=InstanceGuardError("INSTANCE_MUTEX_RELEASE_FAILED", "injected")
    )

    result = run(
        ["--config", str(config_path), "--output", str(output_path)],
        environment={"ATS_SECRET_BROKER_ACCOUNT": ACCOUNT_ID},
        client_factory=lambda *_: PassingClient(),
        instance_guard_factory=lambda _: guard,
        clock=FakeClock(),
    )

    assert result == 3
    assert capsys.readouterr().err.strip() == "INSTANCE_MUTEX_RELEASE_FAILED"
    assert not output_path.exists()
