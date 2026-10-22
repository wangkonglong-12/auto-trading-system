"""V2 supervision: deterministic faults and real Windows owned-process recovery."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from typing import Any

import pytest
import test_node_processes as node_helpers
import win32api
import win32con
import win32event
import win32job
import win32process
from pydantic import JsonValue
from test_node_processes import eventually, rpc, running_engine

from auto_trading_system.apps.node_process.__main__ import main
from auto_trading_system.apps.trade_runtime.engine_simulation import DEMO_NOW
from auto_trading_system.config.node_v2 import NodeProcessConfig, SupervisionPolicy
from auto_trading_system.control.ipc_protocol import IPCError, RPCClient
from auto_trading_system.control.windows_pipe import exchange
from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.protocol.v2.plans import PlanEnvelope
from auto_trading_system.runtime.node_supervisor import (
    NodeSupervisor,
    reset_supervision,
    run_supervisor,
    supervisor_guard,
)
from auto_trading_system.runtime.owned_process import OwnedProcess, child_environment
from auto_trading_system.runtime.supervision_state import SupervisionStore
from auto_trading_system.sync.artifact_files import ArtifactFiles

node_fixture = node_helpers.node
Node = tuple[NodeProcessConfig, Path, PlanEnvelope]


class FakeChild:
    def __init__(self, pid: int) -> None:
        self.process_id = pid
        self.code: int | None = None
        self.terminated = False
        self.closed = False

    def poll(self) -> int | None:
        return self.code

    def owns_process(self, process_id: int) -> bool:
        return process_id == self.process_id

    def terminate(self) -> None:
        self.terminated = True
        self.code = 3

    def kill(self) -> None:
        self.terminate()

    def wait(self, timeout_seconds: float) -> int:
        assert timeout_seconds > 0
        assert self.code is not None
        return self.code

    def close(self) -> None:
        self.closed = True


class Rig:
    def __init__(self, config: NodeProcessConfig) -> None:
        self.now = 0.0
        self.free = 10**12
        self.config = config
        self.store = SupervisionStore(config)
        self.children: list[FakeChild] = []
        self.response: dict[str, JsonValue] = {}
        self.unavailable = False
        self.supervisor = NodeSupervisor(
            config,
            self.store,
            self.launch,
            self.probe,
            elapsed=lambda: self.now,
            free_bytes=lambda: self.free,
        )

    def launch(self, run_id: str) -> FakeChild:
        assert self.store.read().active_run == run_id
        child = FakeChild(1000 + len(self.children))
        self.children.append(child)
        self.response = {
            "component": "engine",
            "broker": "MOCK",
            "real_trading_enabled": False,
            "node_id": self.config.node_id,
            "account_id": self.config.account_id,
            "node_generation": self.config.node_generation,
            "process_id": child.process_id,
            "supervision_id": run_id,
            "boot_id": "boot-" + run_id,
            "owner_sequence": 1,
            "journal_healthy": True,
            "halted": True,
        }
        return child

    def probe(self) -> dict[str, JsonValue]:
        if self.unavailable:
            raise IPCError("PIPE_UNAVAILABLE")
        return self.response


@pytest.fixture
def rig(node_fixture: Node) -> Rig:
    return Rig(node_fixture[0])


def test_owner_progress_only_and_no_repeated_heartbeat_refresh(rig: Rig) -> None:
    assert rig.supervisor.tick()["action"] == "STARTING"
    assert rig.supervisor.tick()["action"] == "HEALTHY"
    rig.now = 9
    assert rig.supervisor.tick()["action"] == "HEALTHY"
    rig.response["owner_sequence"] = 2
    rig.supervisor.tick()
    rig.now = 18
    assert rig.supervisor.tick()["action"] == "HEALTHY"
    rig.now = 19
    assert rig.supervisor.tick()["action"] == "BACKOFF"
    assert rig.store.read().reason == "OWNER_STALE"
    assert rig.children[0].terminated and rig.children[0].closed


def test_live_profile_health_check_accepts_matching_live_capability(rig: Rig) -> None:
    rig.supervisor.config = rig.config.model_copy(update={"broker": "QMT_TRADING"})
    rig.supervisor.tick()
    rig.response["broker"] = "QMT_TRADING"
    rig.response["real_trading_enabled"] = True
    assert rig.supervisor.tick()["action"] == "HEALTHY"
    rig.response["real_trading_enabled"] = False
    assert rig.supervisor.tick()["action"] == "BLOCKED"


@pytest.mark.parametrize(
    "field,value",
    [
        ("component", "agent"),
        ("broker", "LIVE"),
        ("real_trading_enabled", True),
        ("node_id", "foreign-node"),
        ("account_id", "foreign-account"),
        ("node_generation", 2),
        ("process_id", 2000),
        ("process_id", True),
        ("supervision_id", "old-run"),
        ("owner_sequence", True),
        ("owner_sequence", 0),
        ("owner_sequence", "2"),
        ("boot_id", ""),
        ("journal_healthy", False),
    ],
)
def test_identity_and_health_fail_closed(rig: Rig, field: str, value: JsonValue) -> None:
    rig.supervisor.tick()
    rig.response[field] = value
    assert rig.supervisor.tick()["action"] == "BLOCKED"
    assert rig.children[0].terminated
    rig.now += 1000
    rig.supervisor.tick()
    assert len(rig.children) == 1


@pytest.mark.parametrize("field,value", [("boot_id", "replacement"), ("owner_sequence", 1)])
def test_boot_change_or_sequence_regression_blocks(rig: Rig, field: str, value: JsonValue) -> None:
    rig.supervisor.tick()
    rig.response["owner_sequence"] = 2
    rig.supervisor.tick()
    rig.response[field] = value
    assert rig.supervisor.tick()["action"] == "BLOCKED"


def test_startup_and_pipe_failure_do_not_refresh_timer(rig: Rig) -> None:
    rig.supervisor.tick()
    rig.unavailable = True
    rig.now = 29
    assert rig.supervisor.tick()["action"] == "AWAITING_PROGRESS"
    rig.now = 30
    assert rig.supervisor.tick()["reason"] == "CHILD_START_TIMEOUT"


def test_probe_latency_counts_towards_timeout(rig: Rig) -> None:
    rig.supervisor.tick()
    rig.supervisor.tick()

    def slow() -> dict[str, JsonValue]:
        rig.now += 11
        raise IPCError("PIPE_TIMEOUT")

    rig.supervisor.probe = slow
    assert rig.supervisor.tick()["reason"] == "OWNER_STALE"


def test_restart_backoff_and_durable_circuit(rig: Rig) -> None:
    for index in range(3):
        rig.now = 100.0 * index
        assert rig.supervisor.tick()["action"] == "STARTING"
        rig.children[-1].code = 91
        snapshot = rig.supervisor.tick()
        assert snapshot["failures"] == index + 1
        if index < 2:
            assert snapshot["action"] == "BACKOFF"
            rig.supervisor.tick()
            assert len(rig.children) == index + 1
    assert snapshot["action"] == "BLOCKED"
    fresh = Rig(rig.config)
    assert fresh.supervisor.tick()["action"] == "BLOCKED"
    assert not fresh.children


def test_interrupted_monitor_consumes_persisted_launch(rig: Rig) -> None:
    rig.supervisor.tick()
    fresh = Rig(rig.config)
    assert fresh.supervisor.state.failures == 1
    assert fresh.supervisor.state.reason == "SUPERVISOR_INTERRUPTED"
    assert fresh.store.read().active_run is None
    assert fresh.supervisor.tick()["action"] == "BACKOFF"


def test_clean_child_stop_does_not_restart(rig: Rig) -> None:
    rig.supervisor.tick()
    rig.children[0].code = 0
    assert rig.supervisor.tick()["action"] == "STOPPED"
    rig.now = 1000
    rig.supervisor.tick()
    assert len(rig.children) == 1 and rig.store.read().active_run is None
    assert not rig.children[0].terminated


def test_normal_monitor_stop_owns_only_child(rig: Rig) -> None:
    rig.supervisor.tick()
    rig.supervisor.stop()
    assert rig.children[0].terminated and rig.children[0].closed
    assert rig.store.read().failures == 0 and rig.store.read().active_run is None
    assert rig.supervisor.tick()["action"] == "STOPPED"


@pytest.mark.parametrize("timeout", [False, True])
def test_graceful_stop_binds_run_and_has_owned_fallback(rig: Rig, timeout: bool) -> None:
    rig.supervisor.tick()
    calls: list[str] = []

    def request_stop(run_id: str) -> None:
        calls.append(run_id)
        if timeout:
            raise IPCError("PIPE_TIMEOUT")
        rig.children[0].code = 0

    rig.supervisor.request_stop = request_stop
    rig.supervisor.stop()
    assert calls == [rig.supervisor.run_id]
    assert rig.children[0].terminated is timeout
    assert rig.children[0].closed


def test_fault_stops_child_before_persistence(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.supervisor.tick()
    write = rig.store.write

    def checked(state: Any) -> None:
        assert rig.children[0].terminated and rig.children[0].closed
        write(state)

    monkeypatch.setattr(rig.store, "write", checked)
    rig.free = 0
    rig.supervisor.tick()


@pytest.mark.parametrize("running", [False, True])
def test_low_disk_blocks_and_stops_owned_child(rig: Rig, running: bool) -> None:
    if running:
        rig.supervisor.tick()
    rig.free = 0
    assert rig.supervisor.tick()["action"] == "BLOCKED"
    assert rig.store.read().reason == "DISK_SPACE_LOW"
    assert not rig.children or rig.children[0].terminated


def test_disk_query_failure_is_not_healthy(rig: Rig) -> None:
    def failed() -> int:
        raise OSError("injected")

    rig.supervisor.free_bytes = failed
    assert rig.supervisor.tick()["reason"] == "DISK_CHECK_FAILED"
    assert not rig.children


def test_launch_failure_is_counted(rig: Rig) -> None:
    def failed(_: str) -> FakeChild:
        raise OSError("injected")

    rig.supervisor.launch = failed
    assert rig.supervisor.tick()["reason"] == "CHILD_LAUNCH_FAILED"
    assert rig.store.read().active_run is None and rig.store.read().failures == 1


def test_state_write_failure_never_starts_child(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def failed(*_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(rig.store, "write", failed)
    with pytest.raises(OSError):
        rig.supervisor.tick()
    assert not rig.children


def test_failed_fault_record_still_stops_child_and_keeps_inflight(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig.supervisor.tick()

    def failed(*_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(rig.store, "write", failed)
    rig.free = 0
    with pytest.raises(OSError):
        rig.supervisor.tick()
    assert rig.children[0].terminated and rig.children[0].closed
    assert rig.store.read().active_run
    rig.supervisor.stop()
    assert rig.store.read().active_run


def test_abort_latches_instead_of_clean_shutdown(rig: Rig) -> None:
    rig.supervisor.tick()
    rig.supervisor.abort()
    rig.supervisor.stop()
    assert rig.store.read().blocked and rig.store.read().reason == "SUPERVISOR_FAILED"


@pytest.mark.parametrize("data", [b"{", b"{}", b"x" * 8193])
def test_corrupt_monitor_state_rejected(node_fixture: Node, data: bytes) -> None:
    store = SupervisionStore(node_fixture[0])
    store.path.write_bytes(data)
    with pytest.raises(ValueError):
        Rig(node_fixture[0])


def test_cross_node_scope_rejected(rig: Rig) -> None:
    state = rig.store.read().model_copy(update={"scope_hash": "sha256:" + "f" * 64})
    with pytest.raises(ValueError):
        rig.store.write(state)
    rig.store.path.write_bytes(canonical_bytes(state.model_dump(mode="json")))
    with pytest.raises(ValueError):
        rig.store.read()


def test_manual_reset_archives_previous_state_and_requires_stopped_node(rig: Rig) -> None:
    rig.free = 0
    rig.supervisor.tick()
    prior = rig.store.read()
    with supervisor_guard(rig.config), pytest.raises(RuntimeError):
        reset_supervision(rig.config)
    reset_supervision(rig.config)
    current = rig.store.read()
    assert not current.blocked and current.failures == 0
    assert current.previous_state_hash
    saved = ArtifactFiles(rig.config.data_root / "artifacts").read(current.previous_state_hash)
    assert saved == canonical_bytes(prior.model_dump(mode="json"))


def test_existing_engine_not_adopted_or_signalled(node_fixture: Node) -> None:
    config, path, _ = node_fixture
    with running_engine(config, lambda: DEMO_NOW) as client:
        old = client.invoke("STATUS")["boot_id"]
        with pytest.raises(RuntimeError):
            run_supervisor(config, path)
        with pytest.raises(RuntimeError):
            reset_supervision(config)
        assert client.invoke("STATUS")["boot_id"] == old


@pytest.mark.parametrize("timeout", [0, -1, 8])
def test_probe_timeout_validation(timeout: float) -> None:
    with pytest.raises(ValueError):
        exchange(r"\\.\pipe\AutoTrading.v2." + "a" * 32 + ".control", b"a", timeout_seconds=timeout)


@pytest.mark.parametrize(
    "field,value",
    [
        ("failure_limit", 0),
        ("failure_limit", 11),
        ("stale_seconds", 0),
        ("minimum_free_bytes", 0),
        ("startup_seconds", 1000),
    ],
)
def test_supervision_policy_bounds(field: str, value: int) -> None:
    with pytest.raises(ValueError):
        SupervisionPolicy.model_validate({field: value})


def test_owner_status_progress_and_mock_safety(node_fixture: Node) -> None:
    config = node_fixture[0]
    with running_engine(config, lambda: DEMO_NOW) as client:
        first = client.invoke("STATUS")
        sequence = first["owner_sequence"]
        assert type(sequence) is int
        eventually(lambda: int(str(client.invoke("STATUS")["owner_sequence"])) > sequence)
        assert first["halted"] and not first["real_trading_enabled"]
        assert first["supervision_id"] is None


def test_owned_job_close_terminates_child() -> None:
    child = OwnedProcess(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        environment=child_environment("test"),
    )
    handle = win32api.OpenProcess(win32con.SYNCHRONIZE, False, child.process_id)
    try:
        assert child.poll() is None
        assert child.owns_process(child.process_id)
        assert not child.owns_process(os.getpid())
        assert not child.owns_process(-1)
        assert not child.owns_process(0x7FFFFFFE)
        with pytest.raises(TimeoutError):
            child.wait(0.01)
        child.close()
        assert win32event.WaitForSingleObject(handle, 5000) == win32event.WAIT_OBJECT_0
        child.close()
    finally:
        handle.Close()
        child.close()


def test_exit_259_is_not_confused_with_still_running() -> None:
    child = OwnedProcess(
        [sys.executable, "-c", "import sys; sys.exit(259)"], environment=child_environment("test")
    )
    try:
        assert child.wait(5) == 259
        assert child.poll() == 259
    finally:
        child.close()


def test_job_assignment_failure_never_resumes_child(monkeypatch: pytest.MonkeyPatch) -> None:
    resumed: list[object] = []
    created: list[Any] = []
    original = win32process.CreateProcess

    def create(*args: Any) -> Any:
        result = original(*args)
        created.append(win32api.OpenProcess(win32con.SYNCHRONIZE, False, result[2]))
        return result

    def fail(*_: object) -> None:
        raise OSError("injected assignment failure")

    monkeypatch.setattr(win32process, "CreateProcess", create)
    monkeypatch.setattr(win32job, "AssignProcessToJobObject", fail)
    monkeypatch.setattr(win32process, "ResumeThread", resumed.append)
    try:
        with pytest.raises(OSError):
            OwnedProcess(
                [sys.executable, "-c", "import time; time.sleep(120)"],
                environment=child_environment("test"),
            )
        assert not resumed
        assert win32event.WaitForSingleObject(created[0], 5000) == win32event.WAIT_OBJECT_0
    finally:
        for handle in created:
            handle.Close()


@contextmanager
def actual_supervisor(
    config: NodeProcessConfig, path: Path
) -> Iterator[tuple[subprocess.Popen[bytes], RPCClient]]:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "auto_trading_system.apps.node_process",
            "--config",
            str(path),
            "supervise",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    client = rpc(config, "operator", "supervisor")
    try:
        yield process, client
    finally:
        if process.poll() is None:
            try:
                client.invoke("STOP")
                process.wait(12)
            except (IPCError, subprocess.TimeoutExpired):
                process.kill()
                process.wait(10)


def healthy_child(process: subprocess.Popen[bytes], client: RPCClient) -> dict[str, JsonValue]:
    deadline = monotonic() + 15
    last: dict[str, JsonValue] = {}
    while monotonic() < deadline:
        assert process.poll() is None, last
        try:
            last = client.invoke("STATUS")
        except IPCError:
            sleep(0.05)
            continue
        if last["action"] == "HEALTHY":
            return last
        sleep(0.05)
    raise AssertionError(last)


def test_actual_child_kill_restarts_paused_and_new_boot(node_fixture: Node) -> None:
    config, path, _ = node_fixture
    with actual_supervisor(config, path) as (process, client):
        initial = healthy_child(process, client)
        pid = initial["child_process_id"]
        assert type(pid) is int
        handle = win32api.OpenProcess(win32con.PROCESS_TERMINATE, False, pid)
        try:
            win32process.TerminateProcess(handle, 91)
        finally:
            handle.Close()
        eventually(lambda: client.invoke("STATUS")["failures"] == 1)
        current = healthy_child(process, client)
        assert current["child_boot_id"] != initial["child_boot_id"]
        status = current["child_status"]
        assert isinstance(status, dict) and status["halted"] is True
        assert status["real_trading_enabled"] is False
        assert status["recovery"] != "AUTHORIZED"


def test_actual_monitor_kill_kills_owned_child_and_counts_interruption(node_fixture: Node) -> None:
    config, path, _ = node_fixture
    with actual_supervisor(config, path) as (process, client):
        status = healthy_child(process, client)
        pid = status["child_process_id"]
        assert type(pid) is int
        handle = win32api.OpenProcess(win32con.SYNCHRONIZE, False, pid)
        try:
            process.kill()
            process.wait(5)
            assert win32event.WaitForSingleObject(handle, 5000) == win32event.WAIT_OBJECT_0
        finally:
            handle.Close()
    assert SupervisionStore(config).read().active_run
    with actual_supervisor(config, path) as (process, client):
        restarted = healthy_child(process, client)
        assert restarted["failures"] == 1


def test_actual_engine_stop_is_intentional(node_fixture: Node) -> None:
    config, path, _ = node_fixture
    with actual_supervisor(config, path) as (process, client):
        healthy_child(process, client)
        rpc(config, "operator", "control").invoke("STOP")
        assert process.wait(15) == 0
    assert SupervisionStore(config).read().failures == 0


def test_actual_frozen_owner_is_detected_despite_responsive_pipe(node_fixture: Node) -> None:
    config, path, _ = node_fixture
    policy = config.supervision.model_copy(update={"stale_seconds": 3})
    config = config.model_copy(update={"supervision": policy})
    path.write_bytes(canonical_bytes(config.model_dump(mode="json")))
    client = rpc(config, "operator", "control")

    def launch(run_id: str) -> OwnedProcess:
        return OwnedProcess(
            [
                sys.executable,
                str(Path(__file__).with_name("supervisor_stalled_engine.py")),
                str(path),
            ],
            environment=child_environment(run_id),
        )

    supervisor = NodeSupervisor(
        config, SupervisionStore(config), launch, lambda: client.invoke("STATUS")
    )
    try:
        supervisor.tick()
        until = monotonic() + 15
        while monotonic() < until and supervisor.state.failures == 0:
            supervisor.tick()
            sleep(0.05)
        assert supervisor.sequence == 1  # Initial published copy; the owner never advances.
        assert supervisor.state.reason == "OWNER_STALE"
        assert supervisor.child is None
    finally:
        supervisor.stop()


def test_second_supervisor_and_unauthorized_actions_are_rejected(node_fixture: Node) -> None:
    config, path, _ = node_fixture
    with actual_supervisor(config, path) as (process, client):
        before = healthy_child(process, client)
        with pytest.raises(RuntimeError):
            run_supervisor(config, path)
        with pytest.raises(IPCError):
            client.invoke("ARM")
        with pytest.raises(IPCError):
            client.invoke("STATUS", {"unexpected": True})
        with pytest.raises(IPCError):
            rpc(config, "operator", "control").invoke(
                "SUPERVISOR_STOP", {"supervision_id": "foreign-run"}
            )
        after = healthy_child(process, client)
        assert before["child_boot_id"] == after["child_boot_id"]


def test_reset_cli_requires_interactive_exact_confirmation(
    node_fixture: Node,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, path, _ = node_fixture
    arguments = ["--config", str(path), "reset-supervision"]
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert main(arguments) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "INTERACTIVE_OPERATOR_REQUIRED"
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "wrong")
    assert main(arguments) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "CONFIRMATION_MISMATCH"
    monkeypatch.setattr("builtins.input", lambda _: "RESET SUPERVISION " + config.account_id)
    assert main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["supervision_reset"] is True


def test_supervisor_composition_in_current_process(node_fixture: Node) -> None:
    config, path, _ = node_fixture
    ready, stop = Event(), Event()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            run_supervisor(config, path, stop=stop, ready=lambda _: ready.set())
        except BaseException as exc:
            errors.append(exc)
            ready.set()

    thread = Thread(target=run)
    thread.start()
    client = rpc(config, "operator", "supervisor")
    try:
        assert ready.wait(8) and not errors
        eventually(lambda: client.invoke("STATUS")["action"] == "HEALTHY", seconds=15)
        with pytest.raises(IPCError):
            client.invoke("STATUS", {"unexpected": True})
        assert client.invoke("STOP")["stop_requested"] is True
    finally:
        stop.set()
        thread.join(15)
    assert not thread.is_alive() and not errors
    assert SupervisionStore(config).read().active_run is None


def test_configuration_change_is_rejected_before_monitor_start(node_fixture: Node) -> None:
    config, path, _ = node_fixture
    path.write_bytes(
        canonical_bytes(config.model_copy(update={"mock_cash": "0.00"}).model_dump(mode="json"))
    )
    with pytest.raises(ValueError, match="configuration changed"):
        run_supervisor(config, path)


def test_unexpected_monitor_error_persists_block(
    node_fixture: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, path, _ = node_fixture

    def fail(_: NodeSupervisor) -> dict[str, JsonValue]:
        raise RuntimeError("injected monitor error")

    monkeypatch.setattr(NodeSupervisor, "tick", fail)
    with pytest.raises(RuntimeError, match="injected monitor error"):
        run_supervisor(config, path)
    assert SupervisionStore(config).read().blocked


@pytest.mark.parametrize("command", [[], ["relative-python"], [sys.executable, "invalid\x00arg"]])
def test_owned_process_rejects_invalid_command(command: list[str]) -> None:
    with pytest.raises(ValueError):
        OwnedProcess(command, environment={})


def test_job_name_collision_fails_without_modifying_existing_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(win32api, "GetLastError", lambda: 183)
    with pytest.raises(RuntimeError, match="collision"):
        OwnedProcess([sys.executable, "-c", "pass"], environment={})


def test_state_fsync_failure_keeps_old_file_and_cleans_own_temporary(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.store.write(rig.store.read())
    before = rig.store.path.read_bytes()

    def fail(_: int) -> None:
        raise OSError("injected fsync failure")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError):
        rig.store.write(rig.store.read().model_copy(update={"failures": 1}))
    assert rig.store.path.read_bytes() == before
    assert not list(rig.store.path.parent.glob(".supervision-*"))
