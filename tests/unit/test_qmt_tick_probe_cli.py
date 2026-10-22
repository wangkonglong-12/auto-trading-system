from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest

from auto_trading_system.apps.qmt_tick_probe import __main__ as tick_cli
from auto_trading_system.apps.qmt_tick_probe.__main__ import run
from auto_trading_system.domain import SHANGHAI_TIMEZONE, SecurityCode, ShanghaiTimestamp
from auto_trading_system.gateways.market.qmt import inspect_tick_record
from auto_trading_system.monitoring.qmt_tick import (
    QmtTickErrorSink,
    QmtTickSink,
    QmtTickSnapshotBatch,
    QmtTickSource,
)
from auto_trading_system.runtime import InstanceAlreadyRunningError, InstanceGuardError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "v1" / "runtime.read-only.example.json"
ACCOUNT_ID = "A123456789"
SYMBOL = SecurityCode.parse("600000.SH")


def tick() -> dict[str, object]:
    return {
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
        "askPrice": [10.1] * 5,
        "bidPrice": [10.0] * 5,
        "askVol": [10] * 5,
        "bidVol": [20] * 5,
    }


@dataclass
class FakeClock:
    nanoseconds: int = 0
    wall_time: datetime = datetime(2026, 9, 4, 14, 50, tzinfo=SHANGHAI_TIMEZONE)

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


@dataclass
class PassingClient:
    sdk_version: str | None = "fixture"
    sample_sink: QmtTickSink | None = None
    error_sink: QmtTickErrorSink | None = None
    calls: list[str] = field(default_factory=list)

    def subscribe(
        self,
        symbol: SecurityCode,
        sample_sink: QmtTickSink,
        error_sink: QmtTickErrorSink,
    ) -> int:
        assert symbol == SYMBOL
        self.sample_sink = sample_sink
        self.error_sink = error_sink
        self.calls.append("subscribe")
        return 7

    def get_full_tick(self, symbols: tuple[SecurityCode, ...]) -> QmtTickSnapshotBatch:
        assert symbols == (SYMBOL,)
        self.calls.append("snapshot")
        return QmtTickSnapshotBatch(
            (inspect_tick_record(SYMBOL.to_wire(), tick(), QmtTickSource.SNAPSHOT),),
            0,
            0,
        )

    def wait_for_callbacks(self, seconds: int) -> None:
        assert seconds == 1
        assert self.sample_sink is not None
        self.calls.append("wait")
        self.sample_sink(inspect_tick_record(SYMBOL.to_wire(), tick(), QmtTickSource.CALLBACK))

    def unsubscribe(self, subscription_id: int) -> None:
        assert subscription_id == 7
        self.calls.append("unsubscribe")


def write_config(tmp_path: Path, *, profile: str = "READ_ONLY") -> Path:
    payload = copy.deepcopy(json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8")))
    userdata = tmp_path / "userdata_mini"
    (userdata / "up_queue_xtquant").mkdir(parents=True)
    payload["profile"] = profile
    payload["qmt"]["userdata_mini"] = str(userdata)
    path = (tmp_path / f"tick-{profile.casefold()}.json").resolve()
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def arguments(config_path: Path, output_path: Path, symbol: str = SYMBOL.to_wire()) -> list[str]:
    return [
        "--config",
        str(config_path),
        "--output",
        str(output_path),
        "--symbol",
        symbol,
        "--scenario",
        "ACTIVE",
        "--observation-seconds",
        "1",
    ]


def test_cli_writes_private_report_only_after_account_guard_release(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_config(tmp_path)
    output_path = (tmp_path / "tick-report.json").resolve()
    guard = FakeGuard()
    client = PassingClient()

    result = run(
        arguments(config_path, output_path),
        environment={"ATS_SECRET_BROKER_ACCOUNT": ACCOUNT_ID},
        client_factory=lambda: client,
        instance_guard_factory=lambda account: guard if account == ACCOUNT_ID else pytest.fail(),
        clock=FakeClock(),
    )

    assert result == 0
    assert guard.calls == ["acquire", "release"]
    assert client.calls[-1] == "unsubscribe"
    summary = json.loads(capsys.readouterr().out)
    assert summary["outcome"] == "PASS"
    contents = output_path.read_text(encoding="utf-8")
    assert ACCOUNT_ID not in contents
    assert SYMBOL.to_wire() not in contents
    assert json.loads(contents)["work_item"] == "QMT-003"


def test_cli_refuses_qmt_when_account_guard_is_owned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_config(tmp_path)
    output_path = (tmp_path / "blocked.json").resolve()
    guard = FakeGuard(acquire_failure=InstanceAlreadyRunningError("0123456789ab"))

    result = run(
        arguments(config_path, output_path),
        environment={"ATS_SECRET_BROKER_ACCOUNT": ACCOUNT_ID},
        client_factory=lambda: pytest.fail("XtData client must not be created"),
        instance_guard_factory=lambda _: guard,
    )

    assert result == 2
    assert capsys.readouterr().err.strip() == "ACCOUNT_INSTANCE_ALREADY_RUNNING"
    assert not output_path.exists()


def test_cli_rejects_profile_and_symbol_before_guard_or_xtdata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paper_config = write_config(tmp_path, profile="PAPER")
    output_path = (tmp_path / "paper.json").resolve()

    result = run(
        arguments(paper_config, output_path),
        environment={},
        client_factory=lambda: pytest.fail("XtData client must not be created"),
        instance_guard_factory=lambda _: pytest.fail("guard must not be created"),
    )
    assert result == 2
    assert capsys.readouterr().err.strip() == "QMT_TICK_PROFILE_NOT_READ_ONLY"

    readonly_config = write_config(tmp_path / "second")
    result = run(
        arguments(readonly_config, (tmp_path / "invalid.json").resolve(), "bad-symbol"),
        environment={"ATS_SECRET_BROKER_ACCOUNT": ACCOUNT_ID},
        client_factory=lambda: pytest.fail("XtData client must not be created"),
        instance_guard_factory=lambda _: pytest.fail("guard must not be created"),
    )
    assert result == 2
    assert capsys.readouterr().err.strip() == "QMT_TICK_INPUT_INVALID"


def test_cli_never_overwrites_existing_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_config(tmp_path)
    output_path = (tmp_path / "existing.json").resolve()
    output_path.write_text("preserve", encoding="utf-8")

    result = run(arguments(config_path, output_path), environment={})

    assert result == 3
    assert capsys.readouterr().err.strip() == "QMT_PROBE_OUTPUT_EXISTS"
    assert output_path.read_text(encoding="utf-8") == "preserve"


def test_cli_rejects_relative_config_and_missing_secret_before_xtdata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_path = (tmp_path / "relative.json").resolve()
    result = run(
        arguments(Path("relative.json"), output_path),
        client_factory=lambda: pytest.fail("XtData client must not be created"),
    )
    assert result == 2
    assert capsys.readouterr().err.strip() == "QMT_TICK_CONFIG_PATH_INVALID"

    config_path = write_config(tmp_path / "missing-secret")
    result = run(
        arguments(config_path, (tmp_path / "missing-secret.json").resolve()),
        environment={},
        client_factory=lambda: pytest.fail("XtData client must not be created"),
    )
    assert result == 2
    assert capsys.readouterr().err.strip() == "CONFIG_SECRET_LOAD_FAILED"


def test_cli_maps_guard_and_probe_boundary_failures_to_stable_codes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = write_config(tmp_path)
    output_path = (tmp_path / "guard-failed.json").resolve()

    result = run(
        arguments(config_path, output_path),
        environment={"ATS_SECRET_BROKER_ACCOUNT": ACCOUNT_ID},
        client_factory=lambda: pytest.fail("XtData client must not be created"),
        instance_guard_factory=lambda _: (_ for _ in ()).throw(RuntimeError("secret")),
    )
    assert result == 3
    assert capsys.readouterr().err.strip() == "QMT_TICK_INSTANCE_GUARD_FAILED"

    guard = FakeGuard()
    monkeypatch.setattr(
        tick_cli,
        "run_qmt_tick_probe",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("secret")),
    )
    result = run(
        arguments(config_path, (tmp_path / "probe-failed.json").resolve()),
        environment={"ATS_SECRET_BROKER_ACCOUNT": ACCOUNT_ID},
        client_factory=PassingClient,
        instance_guard_factory=lambda _: guard,
    )
    assert result == 3
    assert guard.calls == ["acquire", "release"]
    assert capsys.readouterr().err.strip() == "QMT_TICK_UNEXPECTED_FAILURE"


def test_cli_does_not_publish_when_guard_release_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_config(tmp_path)
    output_path = (tmp_path / "release-failed.json").resolve()
    guard = FakeGuard(
        release_failure=InstanceGuardError("INSTANCE_MUTEX_RELEASE_FAILED", "injected")
    )

    result = run(
        arguments(config_path, output_path),
        environment={"ATS_SECRET_BROKER_ACCOUNT": ACCOUNT_ID},
        client_factory=PassingClient,
        instance_guard_factory=lambda _: guard,
        clock=FakeClock(),
    )

    assert result == 3
    assert capsys.readouterr().err.strip() == "INSTANCE_MUTEX_RELEASE_FAILED"
    assert not output_path.exists()
