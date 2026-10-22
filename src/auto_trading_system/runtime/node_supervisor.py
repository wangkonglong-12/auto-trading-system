"""Strategy-independent V2 supervision, managing only an owned Windows child.

Pipe replies are authenticated, but a live pipe thread alone is not health:
only increasing owner_sequence from the expected child/boot counts as progress.
All timeout and retry decisions use elapsed time. Failure budgets survive a
supervisor restart; restarting a process never grants trading authority.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from threading import Event, Lock
from time import monotonic
from uuid import uuid4

from pydantic import JsonValue

from auto_trading_system.config.node_v2 import NodeProcessConfig, load_node_config, role_keys
from auto_trading_system.control.ipc_protocol import IPCError, Request, RPCClient, RPCService
from auto_trading_system.control.windows_pipe import PipeServer, exchange
from auto_trading_system.runtime.node_layout import validate_layout
from auto_trading_system.runtime.owned_process import OwnedChild, OwnedProcess, child_environment
from auto_trading_system.runtime.single_instance import (
    AccountInstanceGuard,
    WindowsNamedMutexBackend,
)
from auto_trading_system.runtime.supervision_state import SupervisionState, SupervisionStore


def supervisor_guard(config: NodeProcessConfig, *, engine: bool = False) -> AccountInstanceGuard:
    key = ("" if engine else "supervisor:") + config.account_id.upper()
    return AccountInstanceGuard(
        hashlib.sha256(key.encode()).hexdigest(), backend=WindowsNamedMutexBackend()
    )


class NodeSupervisor:
    """Single-owner state machine; transport, OS processes and clock are ports."""

    def __init__(
        self,
        config: NodeProcessConfig,
        store: SupervisionStore,
        launch: Callable[[str], OwnedChild],
        probe: Callable[[], dict[str, JsonValue]],
        *,
        elapsed: Callable[[], float] = monotonic,
        free_bytes: Callable[[], int] | None = None,
        request_stop: Callable[[str], None] | None = None,
    ) -> None:
        self.config, self.store, self.launch, self.probe = config, store, launch, probe
        self.elapsed = elapsed
        self.request_stop = request_stop
        self.free_bytes = free_bytes or (lambda: shutil.disk_usage(config.data_root).free)
        self.state = store.read()
        self.child: OwnedChild | None = None
        self.run_id: str | None = None
        self.boot_id: str | None = None
        self.sequence = 0
        self.last_progress = 0.0
        self.next_launch = 0.0
        self.action = "READY"
        self.child_status: dict[str, JsonValue] = {}
        if self.state.active_run:
            self._failure("SUPERVISOR_INTERRUPTED")

    def _save(self, **changes: object) -> None:
        candidate = SupervisionState.model_validate_json(
            self.state.model_copy(update=changes).model_dump_json()
        )
        self.store.write(candidate)
        self.state = candidate

    def _close_child(self) -> None:
        child, self.child = self.child, None
        if child is None:
            return
        try:
            if child.poll() is None:
                child.terminate()
                child.wait(self.config.supervision.stop_seconds)
        finally:
            # Closing the Job handle is the final fail-safe, including on timeout.
            child.close()

    def _failure(self, reason: str, *, block: bool = False) -> None:
        failures = min(100, self.state.failures + 1)
        try:
            # Stop execution before a potentially slow fault-state flush. The
            # previously persisted active_run still covers interruption here.
            try:
                self._close_child()
            finally:
                self._save(
                    failures=failures,
                    active_run=None,
                    reason=reason,
                    blocked=block
                    or self.state.blocked
                    or failures >= self.config.supervision.failure_limit,
                )
        except BaseException:
            self.action = "FAULT"
            raise
        self.action = "BLOCKED" if self.state.blocked else "BACKOFF"
        self.next_launch = self.elapsed() + min(
            30, self.config.supervision.restart_seconds * 2 ** min(failures - 1, 10)
        )

    def _progress(self, status: dict[str, JsonValue]) -> None:
        expected: dict[str, JsonValue] = {
            "component": "engine",
            "broker": self.config.broker,
            "real_trading_enabled": self.config.broker == "QMT_TRADING",
            "node_id": self.config.node_id,
            "account_id": self.config.account_id,
            "node_generation": self.config.node_generation,
            "supervision_id": self.run_id,
        }
        pid = status.get("process_id")
        if (type(pid) is not int or not self.child or not self.child.owns_process(pid)) or any(
            type(status.get(k)) is not type(v) or status.get(k) != v for k, v in expected.items()
        ):
            self._failure("CHILD_IDENTITY_MISMATCH", block=True)
            return
        boot, sequence = status.get("boot_id"), status.get("owner_sequence")
        if (
            not isinstance(boot, str)
            or not boot
            or type(sequence) is not int
            or sequence < 1
            or sequence < self.sequence
            or (self.boot_id is not None and boot != self.boot_id)
        ):
            self._failure("CHILD_PROGRESS_INVALID", block=True)
            return
        if status.get("journal_healthy") is not True:
            self._failure("CHILD_JOURNAL_UNHEALTHY", block=True)
            return
        self.child_status = dict(status)
        self.boot_id = boot
        if sequence > self.sequence:
            self.sequence = sequence
            self.last_progress = self.elapsed()
        self.action = "HEALTHY"  # Process health is distinct from trading permission.

    def tick(self) -> dict[str, JsonValue]:
        if self.action in ("STOPPED", "FAULT"):
            return self.snapshot()
        if self.state.blocked or self.state.failures >= self.config.supervision.failure_limit:
            self.action = "BLOCKED"
            return self.snapshot()
        try:
            free = self.free_bytes()
        except OSError:
            self._failure("DISK_CHECK_FAILED", block=True)
            return self.snapshot()
        if type(free) is not int or free < self.config.supervision.minimum_free_bytes:
            self._failure("DISK_SPACE_LOW", block=True)
            return self.snapshot()
        if self.child is None:
            if self.elapsed() < self.next_launch:
                return self.snapshot()
            self.run_id = uuid4().hex
            self._save(active_run=self.run_id, reason="LAUNCHING")
            try:
                self.child = self.launch(self.run_id)
            except Exception:
                self._failure("CHILD_LAUNCH_FAILED")
                return self.snapshot()
            self.sequence, self.boot_id = 0, None
            self.child_status = {}
            self.last_progress = self.elapsed()
            self.action = "STARTING"
            return self.snapshot()
        code = self.child.poll()
        if code is not None:
            if code == 0:
                self._close_child()
                self._save(active_run=None, reason="CHILD_STOPPED")
                self.action = "STOPPED"  # An explicit engine stop must not resurrect it.
            else:
                self._failure("CHILD_EXITED")
            return self.snapshot()
        try:
            status = self.probe()
        except (IPCError, OSError, ValueError):
            # A bounded failed probe does not refresh the owner progress timer.
            self.action = "AWAITING_PROGRESS"
        else:
            self._progress(status)
        if self.child is not None:
            timeout = (
                self.config.supervision.stale_seconds
                if self.sequence
                else self.config.supervision.startup_seconds
            )
            if self.elapsed() - self.last_progress >= timeout:
                self._failure("OWNER_STALE" if self.sequence else "CHILD_START_TIMEOUT")
        return self.snapshot()

    def stop(self) -> None:
        if self.child and self.child.poll() is None and self.run_id and self.request_stop:
            try:
                self.request_stop(self.run_id)
                self.child.wait(self.config.supervision.stop_seconds)
            except (IPCError, OSError, TimeoutError):
                pass  # Graceful exit is bounded; only our Job is the fallback.
        self._close_child()
        # A failed durable write must keep the previous in-flight evidence.
        if self.action != "FAULT":
            self._save(active_run=None)
        self.action = "STOPPED"

    def abort(self) -> None:
        """An unexpected monitor failure must not look like a clean shutdown."""
        self._failure("SUPERVISOR_FAILED", block=True)

    def snapshot(self) -> dict[str, JsonValue]:
        return {
            "component": "supervisor",
            "action": self.action,
            "child_process_id": self.child.process_id if self.child else None,
            "supervision_id": self.run_id,
            "child_boot_id": self.boot_id,
            "failures": self.state.failures,
            "blocked": self.state.blocked,
            "reason": self.state.reason,
            "child_status": dict(self.child_status),
            "child_status_current": self.child is not None and self.action == "HEALTHY",
            "real_trading_enabled": False,
        }


def reset_supervision(config: NodeProcessConfig) -> None:
    """Maintenance only; both supervisor and engine must be stopped first."""
    validate_layout(config)
    with supervisor_guard(config), supervisor_guard(config, engine=True):
        SupervisionStore(config).reset()


def run_supervisor(
    config: NodeProcessConfig,
    config_path: Path,
    *,
    stop: Event | None = None,
    ready: Callable[[dict[str, JsonValue]], None] = lambda _: None,
) -> None:
    validate_layout(config)
    path = config_path.resolve(strict=True)
    if load_node_config(path) != config:
        raise ValueError("supervisor configuration changed")
    stopped = stop or Event()
    with supervisor_guard(config):
        # Never adopt or signal an existing standalone engine, even with a valid PID.
        with supervisor_guard(config, engine=True):
            pass
        key = role_keys(config, ("operator",))["operator"]
        client = RPCClient(
            lambda body: exchange(config.pipe_name("control"), body, timeout_seconds=0.5),
            "operator",
            key,
        )

        def launch(run_id: str) -> OwnedChild:
            if load_node_config(path) != config:
                raise ValueError("supervisor configuration changed")
            desktop_logging = bool(os.environ.get("ATS_DESKTOP_LOG_DIRECTORY"))
            module_arguments = (
                ["-m", "auto_trading_system.apps.desktop", "--node"]
                if desktop_logging
                else ["-m", "auto_trading_system.apps.node_process"]
            )
            return OwnedProcess(
                [
                    str(Path(sys.executable).resolve()),
                    *module_arguments,
                    "--config",
                    str(path),
                    "engine",
                ],
                environment=child_environment(run_id),
            )

        supervisor = NodeSupervisor(
            config,
            SupervisionStore(config),
            launch,
            lambda: client.invoke("STATUS"),
            request_stop=lambda run_id: _request_child_stop(client, run_id),
        )
        lock = Lock()
        published = supervisor.snapshot()

        def handle(request: Request) -> dict[str, JsonValue]:
            if request.payload:
                raise IPCError("INVALID_ARGUMENTS")
            if request.action == "STOP":
                stopped.set()
                return {"stop_requested": True}
            if request.action == "STATUS":
                with lock:
                    return dict(published)
            raise IPCError("ACTION_DENIED")

        service = RPCService(
            uuid4().hex,
            {"operator": key},
            {"operator": frozenset({"HELLO", "STATUS", "STOP"})},
            handle,
        )
        server = PipeServer(config.pipe_name("supervisor"), config.service_sid, service.handle)
        try:
            ready(published)
            while not stopped.is_set():
                if not server.healthy:
                    raise IPCError("SUPERVISOR_PIPE_FAILED")
                current = supervisor.tick()
                with lock:
                    published = current
                if current["action"] == "STOPPED":
                    break
                stopped.wait(config.supervision.poll_seconds)
        except BaseException:
            supervisor.abort()
            raise
        finally:
            try:
                supervisor.stop()
            finally:
                server.close()


def _request_child_stop(client: RPCClient, run_id: str) -> None:
    client.invoke("SUPERVISOR_STOP", {"supervision_id": run_id})
