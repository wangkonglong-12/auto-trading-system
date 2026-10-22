from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from auto_trading_system.apps.watchdog.supervisor import (
    SubprocessRuntimeLauncher,
    WatchdogAction,
    WatchdogPolicy,
    WatchdogSupervisor,
    WatchdogSupervisorError,
)
from auto_trading_system.domain.trading_time import ShanghaiTimestamp
from auto_trading_system.runtime import (
    ExternalHaltMarkerStore,
    RuntimeHeartbeat,
    RuntimeHeartbeatStore,
    RuntimeState,
)


class FakeClock:
    def __init__(self) -> None:
        self.ns = 1_000_000_000
        self.instant = datetime(2026, 9, 4, 14, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

    def now(self) -> ShanghaiTimestamp:
        return ShanghaiTimestamp(self.instant)

    def monotonic_ns(self) -> int:
        return self.ns

    def advance(self, seconds: int) -> None:
        self.ns += seconds * 1_000_000_000
        self.instant += timedelta(seconds=seconds)


class FakeProcess:
    def __init__(self, process_id: int) -> None:
        self._process_id = process_id
        self.exit_code: int | None = None
        self.terminated = False
        self.killed = False
        self.closed = False
        self.wait_times_out = False

    @property
    def process_id(self) -> int:
        return self._process_id

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True
        if not self.wait_times_out:
            self.exit_code = 3

    def wait(self, timeout_seconds: float) -> int:
        assert timeout_seconds > 0
        if self.wait_times_out and not self.killed:
            raise TimeoutError
        self.exit_code = 3
        return self.exit_code

    def kill(self) -> None:
        self.killed = True
        self.exit_code = 3

    def close(self) -> None:
        self.closed = True


class FakeLauncher:
    def __init__(self) -> None:
        self.to_start: list[FakeProcess] = []
        self.attached: dict[int, FakeProcess] = {}
        self.force_halt_values: list[bool] = []
        self.start_error: Exception | None = None

    def attach(self, process_id: int) -> FakeProcess | None:
        return self.attached.get(process_id)

    def start(self, *, force_halt: bool) -> FakeProcess:
        self.force_halt_values.append(force_halt)
        if self.start_error is not None:
            raise self.start_error
        return self.to_start.pop(0)


def policy() -> WatchdogPolicy:
    return WatchdogPolicy(
        poll_interval_seconds=1,
        heartbeat_startup_timeout_seconds=5,
        heartbeat_stale_timeout_seconds=3,
        terminate_grace_seconds=2,
        restart_initial_delay_seconds=1,
        restart_max_delay_seconds=4,
        crash_limit=3,
        crash_window_seconds=20,
        circuit_open_seconds=10,
    )


def stores(tmp_path: Path) -> tuple[RuntimeHeartbeatStore, ExternalHaltMarkerStore]:
    return (
        RuntimeHeartbeatStore.from_storage_root(
            tmp_path, node_id="node-dev-01", account_id="acct_demo_001"
        ),
        ExternalHaltMarkerStore.from_storage_root(
            tmp_path, node_id="node-dev-01", account_id="acct_demo_001"
        ),
    )


def write_heartbeat(
    store: RuntimeHeartbeatStore, clock: FakeClock, *, process_id: int, sequence: int
) -> None:
    store.write(
        RuntimeHeartbeat(
            node_id="node-dev-01",
            account_id="acct_demo_001",
            boot_id="boot_0123456789abcdef0123456789abcdef",
            process_id=process_id,
            sequence=sequence,
            runtime_state=RuntimeState.READY_PAUSED,
            written_at=clock.now(),
        )
    )


def make_supervisor(
    tmp_path: Path,
    clock: FakeClock,
    launcher: FakeLauncher,
    *,
    free_bytes: int = 10_000,
) -> tuple[WatchdogSupervisor, RuntimeHeartbeatStore, ExternalHaltMarkerStore]:
    heartbeats, markers = stores(tmp_path)
    supervisor = WatchdogSupervisor(
        heartbeat_store=heartbeats,
        marker_store=markers,
        launcher=launcher,
        clock=clock,
        monotonic_clock=clock,
        policy=policy(),
        storage_root=tmp_path,
        minimum_free_bytes=100,
        watchdog_process_id=77,
        disk_free_bytes=lambda _path: free_bytes,
    )
    return supervisor, heartbeats, markers


def finish_discovery(supervisor: WatchdogSupervisor, clock: FakeClock) -> None:
    assert supervisor.tick().action is WatchdogAction.DISCOVERING
    clock.advance(5)


def test_watchdog_attaches_to_separately_started_runtime(tmp_path: Path) -> None:
    clock = FakeClock()
    launcher = FakeLauncher()
    process = FakeProcess(321)
    launcher.attached[321] = process
    supervisor, heartbeats, _markers = make_supervisor(tmp_path, clock, launcher)

    assert supervisor.tick().action is WatchdogAction.DISCOVERING
    clock.advance(1)
    write_heartbeat(heartbeats, clock, process_id=321, sequence=1)
    attached = supervisor.tick()
    clock.advance(1)
    write_heartbeat(heartbeats, clock, process_id=321, sequence=2)
    healthy = supervisor.tick()

    assert attached.action is WatchdogAction.ATTACHED
    assert attached.restart_generation == 0
    assert healthy.action is WatchdogAction.HEALTHY
    assert launcher.force_halt_values == []
    supervisor.stop()
    assert process.closed is True
    assert process.terminated is False


def test_stale_heartbeat_is_latched_then_runtime_restarts_forced_safe(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    launcher = FakeLauncher()
    first = FakeProcess(401)
    second = FakeProcess(402)
    launcher.to_start.extend([first, second])
    supervisor, heartbeats, markers = make_supervisor(tmp_path, clock, launcher)
    finish_discovery(supervisor, clock)

    assert supervisor.tick().action is WatchdogAction.STARTED
    clock.advance(1)
    write_heartbeat(heartbeats, clock, process_id=401, sequence=1)
    assert supervisor.tick().action is WatchdogAction.HEALTHY
    clock.advance(3)
    scheduled = supervisor.tick()

    assert scheduled.action is WatchdogAction.RESTART_SCHEDULED
    assert first.terminated is True
    marker = markers.read()
    assert marker is not None
    assert "RUNTIME_HEARTBEAT_STALE" in marker.reason_codes
    clock.advance(1)
    restarted = supervisor.tick()
    assert restarted.action is WatchdogAction.STARTED
    assert restarted.restart_generation == 2
    assert launcher.force_halt_values == [False, True]


def test_heartbeat_start_timeout_uses_kill_after_grace_timeout(tmp_path: Path) -> None:
    clock = FakeClock()
    launcher = FakeLauncher()
    process = FakeProcess(501)
    process.wait_times_out = True
    launcher.to_start.append(process)
    supervisor, _heartbeats, markers = make_supervisor(tmp_path, clock, launcher)
    finish_discovery(supervisor, clock)
    supervisor.tick()
    clock.advance(5)

    result = supervisor.tick()

    assert result.action is WatchdogAction.RESTART_SCHEDULED
    assert process.terminated and process.killed and process.closed
    marker = markers.read()
    assert marker is not None
    assert "RUNTIME_HEARTBEAT_START_TIMEOUT" in marker.reason_codes


def test_three_runtime_exits_open_circuit_then_allow_safe_retry(tmp_path: Path) -> None:
    clock = FakeClock()
    launcher = FakeLauncher()
    processes = [FakeProcess(601 + index) for index in range(4)]
    launcher.to_start.extend(processes)
    supervisor, _heartbeats, markers = make_supervisor(tmp_path, clock, launcher)
    finish_discovery(supervisor, clock)

    for expected_index in range(3):
        assert supervisor.tick().action is WatchdogAction.STARTED
        processes[expected_index].exit_code = 9
        result = supervisor.tick()
        if expected_index < 2:
            assert result.action is WatchdogAction.RESTART_SCHEDULED
            clock.advance(2**expected_index)
        else:
            assert result.action is WatchdogAction.CIRCUIT_OPEN

    assert supervisor.tick().action is WatchdogAction.CIRCUIT_OPEN
    marker = markers.read()
    assert marker is not None
    assert marker.reason_codes == ("RUNTIME_EXITED", "RUNTIME_CRASH_LOOP")
    clock.advance(10)
    assert supervisor.tick().action is WatchdogAction.STARTED
    assert launcher.force_halt_values == [False, True, True, True]


def test_low_disk_latches_halt_without_killing_healthy_runtime(tmp_path: Path) -> None:
    clock = FakeClock()
    launcher = FakeLauncher()
    process = FakeProcess(701)
    launcher.attached[701] = process
    supervisor, heartbeats, markers = make_supervisor(tmp_path, clock, launcher, free_bytes=10)
    assert supervisor.tick().action is WatchdogAction.DISCOVERING
    clock.advance(1)
    write_heartbeat(heartbeats, clock, process_id=701, sequence=1)
    assert supervisor.tick().action is WatchdogAction.ATTACHED
    clock.advance(1)
    write_heartbeat(heartbeats, clock, process_id=701, sequence=2)

    result = supervisor.tick()

    assert result.action is WatchdogAction.HALT_LATCHED
    assert process.terminated is False
    marker = markers.read()
    assert marker is not None
    assert marker.reason_codes == ("WATCHDOG_DISK_SPACE_LOW",)
    assert marker.occurrence_count == 1


def test_launch_failure_is_latched_and_backed_off(tmp_path: Path) -> None:
    clock = FakeClock()
    launcher = FakeLauncher()
    launcher.start_error = OSError("unavailable")
    supervisor, _heartbeats, markers = make_supervisor(tmp_path, clock, launcher)
    finish_discovery(supervisor, clock)

    result = supervisor.tick()

    assert result.action is WatchdogAction.RESTART_SCHEDULED
    marker = markers.read()
    assert marker is not None
    assert marker.reason_codes == ("RUNTIME_LAUNCH_FAILED",)
    assert dict(marker.details) == {"error_type": "OSError"}


def test_corrupt_halt_marker_stops_automatic_launch(tmp_path: Path) -> None:
    clock = FakeClock()
    launcher = FakeLauncher()
    supervisor, _heartbeats, markers = make_supervisor(tmp_path, clock, launcher)
    markers.path.parent.mkdir(parents=True, exist_ok=True)
    markers.path.write_text("corrupt", encoding="utf-8")

    with pytest.raises(WatchdogSupervisorError, match="HALT_MARKER_INVALID"):
        supervisor.tick()
    assert launcher.force_halt_values == []


def test_disk_query_failure_latches_once(tmp_path: Path) -> None:
    clock = FakeClock()
    launcher = FakeLauncher()
    heartbeats, markers = stores(tmp_path)

    def fail_disk(_path: Path) -> int:
        raise OSError("disk unavailable")

    supervisor = WatchdogSupervisor(
        heartbeat_store=heartbeats,
        marker_store=markers,
        launcher=launcher,
        clock=clock,
        monotonic_clock=clock,
        policy=policy(),
        storage_root=tmp_path,
        minimum_free_bytes=100,
        watchdog_process_id=77,
        disk_free_bytes=fail_disk,
    )
    assert supervisor.tick().action is WatchdogAction.DISCOVERING
    marker = markers.read()
    assert marker is not None
    assert "WATCHDOG_DISK_QUERY_FAILED" in marker.reason_codes


def test_policy_and_supervisor_reject_invalid_values(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive integers"):
        WatchdogPolicy(0, 1, 1, 1, 1, 1, 2, 1, 1)
    with pytest.raises(ValueError, match="maximum"):
        WatchdogPolicy(1, 1, 1, 1, 2, 1, 2, 1, 1)
    clock = FakeClock()
    launcher = FakeLauncher()
    heartbeats, markers = stores(tmp_path)
    with pytest.raises(ValueError, match="minimum_free_bytes"):
        WatchdogSupervisor(
            heartbeat_store=heartbeats,
            marker_store=markers,
            launcher=launcher,
            clock=clock,
            monotonic_clock=clock,
            policy=policy(),
            storage_root=tmp_path,
            minimum_free_bytes=0,
        )


@pytest.mark.skipif(sys.platform != "win32", reason="production watchdog is Windows-only")
def test_subprocess_launcher_starts_exact_command_and_attaches_on_windows(tmp_path: Path) -> None:
    output_path = tmp_path / "forced.txt"
    script = (
        "import os,pathlib,time;"
        f"pathlib.Path({str(output_path)!r}).write_text("
        "os.environ.get('ATS_WATCHDOG_FORCE_HALT','0'));time.sleep(30)"
    )
    python_executable = str(Path(sys.base_prefix) / "python.exe")
    launcher = SubprocessRuntimeLauncher((python_executable, "-c", script), environment={})
    process = launcher.start(force_halt=True)
    try:
        for _ in range(100):
            # Creation precedes write completion. Wait for the probe payload,
            # preserving the same deadline and exact forced-halt assertion.
            if output_path.exists() and output_path.read_text(encoding="utf-8") == "1":
                break
            import time

            time.sleep(0.01)
        assert output_path.read_text(encoding="utf-8") == "1"
        attached = launcher.attach(process.process_id)
        assert attached is not None
        assert attached.poll() is None
        attached.close()
    finally:
        process.terminate()
        process.wait(5)
        process.close()


def test_subprocess_launcher_rejects_relative_or_empty_command() -> None:
    with pytest.raises(ValueError, match="empty"):
        SubprocessRuntimeLauncher(())
    with pytest.raises(ValueError, match="absolute"):
        SubprocessRuntimeLauncher(("python",))
    assert os.path.isabs(sys.executable)


def test_stopped_supervisor_is_idempotent(tmp_path: Path) -> None:
    clock = FakeClock()
    launcher = FakeLauncher()
    supervisor, _heartbeats, _markers = make_supervisor(tmp_path, clock, launcher)

    supervisor.stop()
    supervisor.stop()

    assert supervisor.tick().action is WatchdogAction.STOPPED


def test_invalid_disk_result_fails_closed(tmp_path: Path) -> None:
    clock = FakeClock()
    launcher = FakeLauncher()
    heartbeats, markers = stores(tmp_path)
    supervisor = WatchdogSupervisor(
        heartbeat_store=heartbeats,
        marker_store=markers,
        launcher=launcher,
        clock=clock,
        monotonic_clock=clock,
        policy=policy(),
        storage_root=tmp_path,
        minimum_free_bytes=1,
        disk_free_bytes=lambda _path: -1,
    )

    with pytest.raises(WatchdogSupervisorError, match="DISK_RESULT_INVALID"):
        supervisor.tick()


def test_supervisor_run_honors_stop_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clock = FakeClock()
    launcher = FakeLauncher()
    supervisor, _heartbeats, _markers = make_supervisor(tmp_path, clock, launcher)
    checks = iter((False, True))
    monkeypatch.setattr(
        "auto_trading_system.apps.watchdog.supervisor.time.sleep", lambda _seconds: None
    )

    supervisor.run(lambda: next(checks))

    assert supervisor.tick().action is WatchdogAction.STOPPED
