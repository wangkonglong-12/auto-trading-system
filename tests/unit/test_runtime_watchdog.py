from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from auto_trading_system.apps.trade_runtime.bootstrap import build_runtime_watchdog_boundary
from auto_trading_system.config import RuntimeConfig
from auto_trading_system.domain.trading_time import ShanghaiTimestamp
from auto_trading_system.runtime import (
    ExternalHaltMarkerStore,
    RuntimeCapability,
    RuntimeHeartbeat,
    RuntimeHeartbeatStore,
    RuntimeState,
    RuntimeStateMachine,
    RuntimeWatchdogBridge,
    TransitionAuthority,
    WatchdogArtifactError,
    enforce_external_halt_at_startup,
)


class MutableClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 9, 4, 14, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

    def now(self) -> ShanghaiTimestamp:
        return ShanghaiTimestamp(self.current)

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


def heartbeat_store(tmp_path: Path) -> RuntimeHeartbeatStore:
    return RuntimeHeartbeatStore.from_storage_root(
        tmp_path,
        node_id="node-dev-01",
        account_id="acct_demo_001",
    )


def marker_store(tmp_path: Path) -> ExternalHaltMarkerStore:
    return ExternalHaltMarkerStore.from_storage_root(
        tmp_path,
        node_id="node-dev-01",
        account_id="acct_demo_001",
    )


def make_heartbeat(clock: MutableClock, *, sequence: int = 1) -> RuntimeHeartbeat:
    return RuntimeHeartbeat(
        node_id="node-dev-01",
        account_id="acct_demo_001",
        boot_id="boot_0123456789abcdef0123456789abcdef",
        process_id=1234,
        sequence=sequence,
        runtime_state=RuntimeState.READY_PAUSED,
        written_at=clock.now(),
    )


def test_runtime_heartbeat_round_trip_is_atomic_and_fingerprinted(tmp_path: Path) -> None:
    store = heartbeat_store(tmp_path)
    clock = MutableClock()

    written = store.write(make_heartbeat(clock))
    loaded = store.read()

    assert loaded == written
    assert loaded is not None
    assert loaded.heartbeat.runtime_state is RuntimeState.READY_PAUSED
    assert loaded.fingerprint.startswith("sha256:")
    assert not list(store.path.parent.glob("*.tmp"))


def test_heartbeat_store_rejects_wrong_identity_and_invalid_fields(tmp_path: Path) -> None:
    store = heartbeat_store(tmp_path)
    clock = MutableClock()
    wrong = RuntimeHeartbeat(
        node_id="node-other-01",
        account_id="acct_demo_001",
        boot_id="boot_0123456789abcdef0123456789abcdef",
        process_id=1234,
        sequence=1,
        runtime_state=RuntimeState.HALT,
        written_at=clock.now(),
    )
    with pytest.raises(WatchdogArtifactError, match="WATCHDOG_IDENTITY_MISMATCH"):
        store.write(wrong)

    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text('{"artifact_version":"2.0"}', encoding="utf-8")
    with pytest.raises(WatchdogArtifactError, match="FIELDS_INVALID"):
        store.read()


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not-json",
        b"[]",
        b'{"x":1,"x":2}',
        b"x" * 65_537,
    ],
    ids=("empty", "non-json", "array-root", "duplicate-key", "oversized"),
)
def test_heartbeat_store_rejects_malformed_or_oversized_artifact(
    tmp_path: Path, payload: bytes
) -> None:
    store = heartbeat_store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(payload)

    with pytest.raises(WatchdogArtifactError):
        store.read()


def test_external_halt_marker_latches_identity_and_accumulates_reasons(tmp_path: Path) -> None:
    store = marker_store(tmp_path)
    clock = MutableClock()

    first = store.record(
        reason_code="RUNTIME_EXITED",
        detected_at=clock.now(),
        watchdog_process_id=77,
        runtime_process_id=1234,
        restart_generation=1,
        details={"exit_code": 9},
    )
    clock.advance(2)
    second = store.record(
        reason_code="RUNTIME_CRASH_LOOP",
        detected_at=clock.now(),
        watchdog_process_id=77,
        runtime_process_id=None,
        restart_generation=3,
        details={"crash_count": 3},
    )

    assert second.marker_id == first.marker_id
    assert second.reason_codes == ("RUNTIME_EXITED", "RUNTIME_CRASH_LOOP")
    assert second.occurrence_count == 2
    assert second.first_detected_at == first.first_detected_at
    assert store.read() == second
    value = json.loads(store.path.read_text(encoding="utf-8"))
    assert value["details"] == {"crash_count": "3"}


def test_external_halt_marker_rejects_tamper_and_unsafe_details(tmp_path: Path) -> None:
    store = marker_store(tmp_path)
    clock = MutableClock()
    with pytest.raises(ValueError, match="detail key"):
        store.record(
            reason_code="RUNTIME_EXITED",
            detected_at=clock.now(),
            watchdog_process_id=1,
            runtime_process_id=None,
            restart_generation=0,
            details={"Bad-Key": "value"},
        )

    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps(
            {
                "account_id": "acct_other_001",
                "artifact_version": "1.0",
                "details": {},
                "first_detected_at": clock.now().to_wire(),
                "last_detected_at": clock.now().to_wire(),
                "marker_id": "halt_0123456789abcdef0123456789abcdef",
                "node_id": "node-dev-01",
                "occurrence_count": 1,
                "reason_codes": ["RUNTIME_EXITED"],
                "restart_generation": 0,
                "runtime_process_id": None,
                "watchdog_process_id": 1,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(WatchdogArtifactError, match="IDENTITY_MISMATCH"):
        store.read()


def test_external_halt_startup_gate_forces_halt_but_never_auto(tmp_path: Path) -> None:
    store = marker_store(tmp_path)
    clock = MutableClock()
    machine = RuntimeStateMachine(
        capability=RuntimeCapability.FULL,
        monotonic_clock=type("Clock", (), {"monotonic_ns": lambda self: 10})(),
        owner_guard=lambda: None,
    )
    store.record(
        reason_code="RUNTIME_EXITED",
        detected_at=clock.now(),
        watchdog_process_id=77,
        runtime_process_id=1234,
        restart_generation=1,
    )

    decision = enforce_external_halt_at_startup(
        state_machine=machine,
        marker_store=store,
        force_halt_environment={},
    )

    assert decision is not None
    assert decision.current_state is RuntimeState.HALT
    assert decision.authority is TransitionAuthority.WATCHDOG
    assert machine.state is RuntimeState.HALT


def test_environment_only_startup_gate_is_fail_safe_and_absence_is_noop(tmp_path: Path) -> None:
    store = marker_store(tmp_path)
    machine = RuntimeStateMachine(
        capability=RuntimeCapability.FULL,
        monotonic_clock=type("Clock", (), {"monotonic_ns": lambda self: 10})(),
        owner_guard=lambda: None,
    )
    assert (
        enforce_external_halt_at_startup(
            state_machine=machine,
            marker_store=store,
            force_halt_environment={},
        )
        is None
    )

    decision = enforce_external_halt_at_startup(
        state_machine=machine,
        marker_store=store,
        force_halt_environment={"ATS_WATCHDOG_FORCE_HALT": "1"},
    )
    assert decision is not None
    assert decision.reason_code == "WATCHDOG_FORCED_SAFE_RESTART"


def test_runtime_watchdog_bridge_publishes_and_delivers_latch(tmp_path: Path) -> None:
    async def scenario() -> None:
        heartbeats = heartbeat_store(tmp_path)
        markers = marker_store(tmp_path)
        clock = MutableClock()
        marker = markers.record(
            reason_code="WATCHDOG_DISK_SPACE_LOW",
            detected_at=clock.now(),
            watchdog_process_id=77,
            runtime_process_id=1234,
            restart_generation=0,
        )
        delivered: list[str] = []

        async def halt_sink(value: Any) -> None:
            delivered.append(value.marker_id)

        bridge = RuntimeWatchdogBridge(
            heartbeat_store=heartbeats,
            marker_store=markers,
            clock=clock,
            node_id="node-dev-01",
            account_id="acct_demo_001",
            runtime_state=lambda: RuntimeState.HALT,
            halt_sink=halt_sink,
            interval_seconds=1,
            process_id=1234,
            boot_id="boot_0123456789abcdef0123456789abcdef",
        )
        assert bridge.definition().name == "watchdog-bridge"
        await bridge.start()
        await asyncio.sleep(1.05)
        await bridge.stop()

        assert delivered == [marker.marker_id]
        snapshot = heartbeats.read()
        assert snapshot is not None
        assert snapshot.heartbeat.sequence == 2
        assert snapshot.heartbeat.runtime_state is RuntimeState.HALT
        with pytest.raises(WatchdogArtifactError, match="ALREADY_STARTED"):
            await bridge.start()

    asyncio.run(scenario())


def test_watchdog_artifact_constructor_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        RuntimeHeartbeatStore(Path("relative.json"), node_id="node-dev-01", account_id="acct_a")
    with pytest.raises(ValueError, match="boot_id"):
        RuntimeHeartbeat(
            node_id="node-dev-01",
            account_id="acct_demo_001",
            boot_id="bad",
            process_id=1,
            sequence=1,
            runtime_state=RuntimeState.HALT,
            written_at=MutableClock().now(),
        )


def test_runtime_composition_applies_forced_safe_start_before_building_bridge(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    payload = json.loads(
        (project_root / "config" / "v1" / "runtime.read-only.example.json").read_text(
            encoding="utf-8"
        )
    )
    payload["storage"]["root"] = str(tmp_path)
    settings = RuntimeConfig.model_validate(payload)
    machine = RuntimeStateMachine(
        capability=RuntimeCapability.OBSERVE_ONLY,
        monotonic_clock=type("Clock", (), {"monotonic_ns": lambda self: 10})(),
        owner_guard=lambda: None,
    )

    async def halt_sink(_marker: Any) -> None:
        return None

    boundary = build_runtime_watchdog_boundary(
        settings,
        machine,
        clock=MutableClock(),
        boot_id="boot_0123456789abcdef0123456789abcdef",
        halt_sink=halt_sink,
        process_id=1234,
        environment={"ATS_WATCHDOG_FORCE_HALT": "1"},
    )

    assert boundary.startup_decision is not None
    assert machine.state is RuntimeState.HALT
    assert boundary.bridge.definition().name == "watchdog-bridge"
