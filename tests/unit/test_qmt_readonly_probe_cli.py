from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest

from auto_trading_system.apps.qmt_readonly_probe.__main__ import run
from auto_trading_system.domain import SHANGHAI_TIMEZONE, ShanghaiTimestamp
from auto_trading_system.monitoring.qmt_readonly import (
    QmtAccountStatus,
    QmtProbeConfig,
    QmtSignalSink,
    QmtStatusQuery,
)
from auto_trading_system.runtime import InstanceAlreadyRunningError, InstanceGuardError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "v1" / "runtime.read-only.example.json"
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
        self.calls.append("query")
        return QmtStatusQuery(1, (QmtAccountStatus(account_type=2, status=0),))

    def unsubscribe(self) -> int:
        self.calls.append("unsubscribe")
        return 0

    def stop(self) -> None:
        self.calls.append("stop")


@dataclass
class FakeInstanceGuard:
    calls: list[str] = field(default_factory=list)
    acquire_failure: InstanceGuardError | None = None
    release_failure: InstanceGuardError | None = None

    def acquire(self) -> None:
        self.calls.append("acquire")
        if self.acquire_failure is not None:
            raise self.acquire_failure

    def release(self) -> None:
        self.calls.append("release")
        if self.release_failure is not None:
            raise self.release_failure


def write_config(tmp_path: Path, *, profile: str = "READ_ONLY") -> Path:
    payload = copy.deepcopy(json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8")))
    userdata = tmp_path / "userdata_mini"
    (userdata / "up_queue_xtquant").mkdir(parents=True)
    payload["profile"] = profile
    payload["qmt"]["userdata_mini"] = str(userdata)
    path = (tmp_path / f"runtime-{profile.casefold()}.json").resolve()
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_cli_resolves_only_broker_credential_and_writes_safe_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = write_config(tmp_path)
    output_path = (tmp_path / "qmt-probe.json").resolve()
    client = PassingClient()
    guard = FakeInstanceGuard()
    clock = FakeClock()

    result = run(
        [
            "--config",
            str(config_path),
            "--output",
            str(output_path),
            "--observation-seconds",
            "1",
            "--query-interval-milliseconds",
            "100",
        ],
        environment={"ATS_SECRET_BROKER_ACCOUNT": ACCOUNT_ID},
        client_factory=lambda config, account: _assert_factory_inputs(
            config,
            account,
            client,
        ),
        instance_guard_factory=lambda account: _assert_guard_account(account, guard),
        clock=clock,
        wait=clock.wait,
    )

    assert result == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["outcome"] == "PASS"
    assert summary["exit_code"] == 0
    contents = output_path.read_text(encoding="utf-8")
    report = json.loads(contents)
    assert report["schema"] == "qmt-readonly-probe/v1"
    assert report["collection_mode"] == "READ_ONLY"
    assert ACCOUNT_ID not in contents
    assert str(config_path) not in contents
    assert client.calls == [
        "register",
        "start",
        "connect",
        "subscribe",
        "query",
        "unsubscribe",
        "stop",
    ]
    assert guard.calls == ["acquire", "release"]


def _assert_factory_inputs(
    config: QmtProbeConfig,
    account_id: str,
    client: PassingClient,
) -> PassingClient:
    assert config.account_type == "STOCK"
    assert config.observation_seconds == 1
    assert account_id == ACCOUNT_ID
    return client


def _assert_guard_account(account_id: str, guard: FakeInstanceGuard) -> FakeInstanceGuard:
    assert account_id == ACCOUNT_ID
    return guard


def test_cli_refuses_qmt_access_when_account_mutex_is_owned(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = write_config(tmp_path)
    output_path = (tmp_path / "mutex-blocked.json").resolve()
    guard = FakeInstanceGuard(acquire_failure=InstanceAlreadyRunningError("0123456789ab"))

    result = run(
        ["--config", str(config_path), "--output", str(output_path)],
        environment={"ATS_SECRET_BROKER_ACCOUNT": ACCOUNT_ID},
        client_factory=lambda *_: pytest.fail("QMT client must not be created"),
        instance_guard_factory=lambda _: guard,
    )

    captured = capsys.readouterr()
    assert result == 2
    assert captured.err.strip() == "ACCOUNT_INSTANCE_ALREADY_RUNNING"
    assert ACCOUNT_ID not in captured.out + captured.err
    assert guard.calls == ["acquire"]
    assert not output_path.exists()


def test_cli_releases_account_mutex_before_publishing_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = write_config(tmp_path)
    output_path = (tmp_path / "mutex-release-failed.json").resolve()
    guard = FakeInstanceGuard(
        release_failure=InstanceGuardError("INSTANCE_MUTEX_RELEASE_FAILED", "injected")
    )

    result = run(
        ["--config", str(config_path), "--output", str(output_path)],
        environment={"ATS_SECRET_BROKER_ACCOUNT": ACCOUNT_ID},
        client_factory=lambda *_: PassingClient(),
        instance_guard_factory=lambda _: guard,
        clock=FakeClock(),
        wait=lambda _: None,
    )

    captured = capsys.readouterr()
    assert result == 3
    assert captured.err.strip() == "INSTANCE_MUTEX_RELEASE_FAILED"
    assert guard.calls == ["acquire", "release"]
    assert not output_path.exists()


def test_cli_rejects_non_readonly_profile_before_secret_or_qmt_access(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = write_config(tmp_path, profile="PAPER")
    output_path = (tmp_path / "rejected.json").resolve()

    result = run(
        ["--config", str(config_path), "--output", str(output_path)],
        environment={},
        client_factory=lambda *_: pytest.fail("QMT client must not be created"),
    )

    assert result == 2
    assert capsys.readouterr().err.strip() == "QMT_PROBE_PROFILE_NOT_READ_ONLY"
    assert not output_path.exists()


@pytest.mark.parametrize("account_id", ["", "A-123", "账户1234"])
def test_cli_rejects_invalid_account_credential_without_leaking_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    account_id: str,
) -> None:
    config_path = write_config(tmp_path)
    output_path = (tmp_path / "invalid-account.json").resolve()

    result = run(
        ["--config", str(config_path), "--output", str(output_path)],
        environment={"ATS_SECRET_BROKER_ACCOUNT": account_id},
        client_factory=lambda *_: pytest.fail("QMT client must not be created"),
    )

    captured = capsys.readouterr()
    assert result == 2
    if account_id:
        assert account_id not in captured.out + captured.err
    assert not output_path.exists()


def test_cli_never_overwrites_an_existing_field_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = write_config(tmp_path)
    output_path = (tmp_path / "existing.json").resolve()
    output_path.write_text("preserve this evidence", encoding="utf-8")

    result = run(
        ["--config", str(config_path), "--output", str(output_path)],
        environment={},
        client_factory=lambda *_: pytest.fail("QMT client must not be created"),
    )

    assert result == 3
    assert capsys.readouterr().err.strip() == "QMT_PROBE_OUTPUT_EXISTS"
    assert output_path.read_text(encoding="utf-8") == "preserve this evidence"


def test_cli_requires_absolute_config_and_output_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(["--config", "relative.json", "--output", str(tmp_path / "out.json")]) == 2
    assert capsys.readouterr().err.strip() == "QMT_PROBE_CONFIG_PATH_INVALID"

    config_path = write_config(tmp_path)
    assert run(["--config", str(config_path), "--output", "relative.json"]) == 3
    assert capsys.readouterr().err.strip() == "QMT_PROBE_OUTPUT_INVALID"
