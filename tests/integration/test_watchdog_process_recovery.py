from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from auto_trading_system.apps.watchdog.supervisor import (
    SubprocessRuntimeLauncher,
    WatchdogAction,
    WatchdogPolicy,
    WatchdogSupervisor,
)
from auto_trading_system.domain.trading_time import ShanghaiTimestamp
from auto_trading_system.runtime import ExternalHaltMarkerStore, RuntimeHeartbeatStore


class IntegrationClock:
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


def _wait_for_action(
    supervisor: WatchdogSupervisor,
    expected: WatchdogAction,
    *,
    attempts: int = 200,
) -> None:
    for _ in range(attempts):
        if supervisor.tick().action is expected:
            return
        time.sleep(0.01)
    raise AssertionError(f"watchdog did not reach {expected}")


def test_real_child_crash_is_relaunched_with_forced_halt(tmp_path: Path) -> None:
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "watchdog_runtime_child.py"
    project_root = Path(__file__).resolve().parents[2]
    virtual_environment_packages = Path(sys.prefix) / "Lib" / "site-packages"
    launch_log = tmp_path / "launches.txt"
    clock = IntegrationClock()
    heartbeat_store = RuntimeHeartbeatStore.from_storage_root(
        tmp_path, node_id="node-dev-01", account_id="acct_demo_001"
    )
    marker_store = ExternalHaltMarkerStore.from_storage_root(
        tmp_path, node_id="node-dev-01", account_id="acct_demo_001"
    )
    supervisor = WatchdogSupervisor(
        heartbeat_store=heartbeat_store,
        marker_store=marker_store,
        launcher=SubprocessRuntimeLauncher(
            (
                str(Path(sys.base_prefix) / "python.exe"),
                str(fixture),
                str(tmp_path),
                str(launch_log),
            ),
            environment={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    (str(project_root / "src"), str(virtual_environment_packages))
                ),
            },
        ),
        clock=clock,
        monotonic_clock=clock,
        policy=WatchdogPolicy(
            poll_interval_seconds=1,
            heartbeat_startup_timeout_seconds=1,
            heartbeat_stale_timeout_seconds=1,
            terminate_grace_seconds=2,
            restart_initial_delay_seconds=1,
            restart_max_delay_seconds=2,
            crash_limit=3,
            crash_window_seconds=30,
            circuit_open_seconds=10,
        ),
        storage_root=tmp_path,
        minimum_free_bytes=1,
    )

    try:
        assert supervisor.tick().action is WatchdogAction.DISCOVERING
        clock.advance(1)
        assert supervisor.tick().action is WatchdogAction.STARTED
        _wait_for_action(supervisor, WatchdogAction.RESTART_SCHEDULED)
        clock.advance(1)
        assert supervisor.tick().action is WatchdogAction.STARTED
        _wait_for_action(supervisor, WatchdogAction.HEALTHY)

        assert launch_log.read_text(encoding="utf-8").splitlines() == ["0", "1"]
        marker = marker_store.read()
        assert marker is not None
        assert marker.reason_codes == ("RUNTIME_EXITED",)
        heartbeat = heartbeat_store.read()
        assert heartbeat is not None
        assert heartbeat.heartbeat.runtime_state.value == "HALT"
    finally:
        supervisor.stop()
