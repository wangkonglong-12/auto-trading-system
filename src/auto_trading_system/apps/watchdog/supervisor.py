"""Deterministic process supervision with heartbeat and crash-loop safeguards."""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, Self

from auto_trading_system.config.models import WatchdogSettings
from auto_trading_system.domain.clock import Clock, MonotonicClock
from auto_trading_system.runtime.watchdog import (
    WATCHDOG_FORCE_HALT_ENVIRONMENT,
    ExternalHaltMarker,
    ExternalHaltMarkerStore,
    HeartbeatSnapshot,
    RuntimeHeartbeatStore,
    WatchdogArtifactError,
)

_NANOSECONDS_PER_SECOND = 1_000_000_000


class WatchdogSupervisorError(RuntimeError):
    """Fail-closed supervisor error with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class WatchdogAction(StrEnum):
    DISCOVERING = "DISCOVERING"
    ATTACHED = "ATTACHED"
    STARTED = "STARTED"
    AWAITING_HEARTBEAT = "AWAITING_HEARTBEAT"
    HEALTHY = "HEALTHY"
    RESTART_SCHEDULED = "RESTART_SCHEDULED"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    HALT_LATCHED = "HALT_LATCHED"
    STOPPED = "STOPPED"


class WatchedProcess(Protocol):
    @property
    def process_id(self) -> int: ...

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout_seconds: float) -> int: ...

    def kill(self) -> None: ...

    def close(self) -> None: ...


class RuntimeProcessLauncher(Protocol):
    def attach(self, process_id: int) -> WatchedProcess | None: ...

    def start(self, *, force_halt: bool) -> WatchedProcess: ...


@dataclass(frozen=True, slots=True)
class WatchdogPolicy:
    poll_interval_seconds: int
    heartbeat_startup_timeout_seconds: int
    heartbeat_stale_timeout_seconds: int
    terminate_grace_seconds: int
    restart_initial_delay_seconds: int
    restart_max_delay_seconds: int
    crash_limit: int
    crash_window_seconds: int
    circuit_open_seconds: int

    @classmethod
    def from_settings(cls, settings: WatchdogSettings) -> Self:
        return cls(
            poll_interval_seconds=settings.poll_interval_seconds,
            heartbeat_startup_timeout_seconds=settings.heartbeat_startup_timeout_seconds,
            heartbeat_stale_timeout_seconds=settings.heartbeat_stale_timeout_seconds,
            terminate_grace_seconds=settings.terminate_grace_seconds,
            restart_initial_delay_seconds=settings.restart_initial_delay_seconds,
            restart_max_delay_seconds=settings.restart_max_delay_seconds,
            crash_limit=settings.crash_limit,
            crash_window_seconds=settings.crash_window_seconds,
            circuit_open_seconds=settings.circuit_open_seconds,
        )

    def __post_init__(self) -> None:
        values = (
            self.poll_interval_seconds,
            self.heartbeat_startup_timeout_seconds,
            self.heartbeat_stale_timeout_seconds,
            self.terminate_grace_seconds,
            self.restart_initial_delay_seconds,
            self.restart_max_delay_seconds,
            self.crash_limit,
            self.crash_window_seconds,
            self.circuit_open_seconds,
        )
        if any(type(value) is not int or value < 1 for value in values):
            raise ValueError("watchdog policy values must be positive integers")
        if self.restart_max_delay_seconds < self.restart_initial_delay_seconds:
            raise ValueError("restart maximum delay cannot be below initial delay")
        if self.crash_limit < 2:
            raise ValueError("crash_limit must be at least two")


@dataclass(frozen=True, slots=True)
class WatchdogSnapshot:
    action: WatchdogAction
    runtime_process_id: int | None
    restart_generation: int
    recent_crash_count: int
    heartbeat_observed: bool
    next_launch_monotonic_ns: int
    circuit_open_until_monotonic_ns: int
    active_halt_marker_id: str | None


class WatchdogSupervisor:
    """One-step-testable supervisor; elapsed-time decisions use only monotonic time."""

    def __init__(
        self,
        *,
        heartbeat_store: RuntimeHeartbeatStore,
        marker_store: ExternalHaltMarkerStore,
        launcher: RuntimeProcessLauncher,
        clock: Clock,
        monotonic_clock: MonotonicClock,
        policy: WatchdogPolicy,
        storage_root: Path,
        minimum_free_bytes: int,
        watchdog_process_id: int | None = None,
        disk_free_bytes: Callable[[Path], int] | None = None,
    ) -> None:
        if not isinstance(storage_root, Path) or not storage_root.is_absolute():
            raise ValueError("storage_root must be an absolute path")
        if type(minimum_free_bytes) is not int or minimum_free_bytes < 1:
            raise ValueError("minimum_free_bytes must be positive")
        resolved_process_id = os.getpid() if watchdog_process_id is None else watchdog_process_id
        if type(resolved_process_id) is not int or resolved_process_id < 1:
            raise ValueError("watchdog_process_id must be positive")
        self._heartbeat_store = heartbeat_store
        self._marker_store = marker_store
        self._launcher = launcher
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._policy = policy
        self._storage_root = storage_root
        self._minimum_free_bytes = minimum_free_bytes
        self._watchdog_process_id = resolved_process_id
        self._disk_free_bytes = disk_free_bytes or self._default_disk_free_bytes
        self._runtime: WatchedProcess | None = None
        self._runtime_owned = False
        self._restart_generation = 0
        self._recent_crashes: deque[int] = deque()
        self._next_launch_ns = 0
        self._circuit_open_until_ns = 0
        self._launched_ns = 0
        self._last_heartbeat_progress_ns = 0
        self._last_heartbeat_fingerprint: str | None = None
        self._heartbeat_observed = False
        self._discovery_started_ns: int | None = None
        self._discovery_fingerprint: str | None = None
        self._discovery_complete = False
        self._disk_low_reported = False
        self._stopped = False

    @staticmethod
    def _default_disk_free_bytes(path: Path) -> int:
        return shutil.disk_usage(path).free

    def tick(self) -> WatchdogSnapshot:
        if self._stopped:
            return self._snapshot(WatchdogAction.STOPPED)
        now_ns = self._monotonic_clock.monotonic_ns()
        disk_latched = self._inspect_disk()

        runtime = self._runtime
        if runtime is None:
            discovery = self._discover_existing_runtime(now_ns)
            if discovery is not None:
                return self._snapshot(discovery)
            if now_ns < self._circuit_open_until_ns:
                return self._snapshot(WatchdogAction.CIRCUIT_OPEN)
            if now_ns < self._next_launch_ns:
                return self._snapshot(WatchdogAction.RESTART_SCHEDULED)
            marker = self._read_marker_fail_closed()
            try:
                runtime = self._launcher.start(force_halt=marker is not None)
            except Exception as exc:
                self._register_failure(
                    now_ns,
                    reason_code="RUNTIME_LAUNCH_FAILED",
                    runtime_process_id=None,
                    details={"error_type": type(exc).__name__},
                )
                return self._snapshot(self._scheduled_action(now_ns))
            self._runtime = runtime
            self._runtime_owned = True
            self._restart_generation += 1
            self._launched_ns = now_ns
            self._last_heartbeat_progress_ns = now_ns
            self._heartbeat_observed = False
            self._last_heartbeat_fingerprint = self._safe_heartbeat_fingerprint()
            return self._snapshot(WatchdogAction.STARTED)

        exit_code = runtime.poll()
        if exit_code is not None:
            process_id = runtime.process_id
            runtime.close()
            self._runtime = None
            self._runtime_owned = False
            self._register_failure(
                now_ns,
                reason_code="RUNTIME_EXITED",
                runtime_process_id=process_id,
                details={"exit_code": exit_code},
            )
            return self._snapshot(self._scheduled_action(now_ns))

        heartbeat = self._read_heartbeat_for_monitoring(runtime.process_id)
        if heartbeat is not None and heartbeat.fingerprint != self._last_heartbeat_fingerprint:
            self._last_heartbeat_fingerprint = heartbeat.fingerprint
            self._last_heartbeat_progress_ns = now_ns
            self._heartbeat_observed = True

        timeout_seconds = (
            self._policy.heartbeat_stale_timeout_seconds
            if self._heartbeat_observed
            else self._policy.heartbeat_startup_timeout_seconds
        )
        elapsed_ns = now_ns - self._last_heartbeat_progress_ns
        if elapsed_ns >= timeout_seconds * _NANOSECONDS_PER_SECOND:
            reason_code = (
                "RUNTIME_HEARTBEAT_STALE"
                if self._heartbeat_observed
                else "RUNTIME_HEARTBEAT_START_TIMEOUT"
            )
            process_id = runtime.process_id
            self._record_marker(
                reason_code=reason_code,
                runtime_process_id=process_id,
                details={"timeout_seconds": timeout_seconds},
            )
            self._stop_runtime(runtime)
            self._runtime = None
            self._runtime_owned = False
            self._register_crash(now_ns)
            return self._snapshot(self._scheduled_action(now_ns))

        if not self._heartbeat_observed:
            return self._snapshot(WatchdogAction.AWAITING_HEARTBEAT)
        return self._snapshot(
            WatchdogAction.HALT_LATCHED if disk_latched else WatchdogAction.HEALTHY
        )

    def run(self, stop_requested: Callable[[], bool]) -> None:
        if not callable(stop_requested):
            raise ValueError("stop_requested must be callable")
        try:
            while not stop_requested():
                self.tick()
                time.sleep(self._policy.poll_interval_seconds)
        finally:
            self.stop()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        runtime = self._runtime
        self._runtime = None
        runtime_owned = self._runtime_owned
        self._runtime_owned = False
        if runtime is not None and runtime_owned:
            self._stop_runtime(runtime)
        elif runtime is not None:
            runtime.close()

    def _discover_existing_runtime(self, now_ns: int) -> WatchdogAction | None:
        if self._discovery_complete:
            return None
        if self._discovery_started_ns is None:
            self._discovery_started_ns = now_ns
            self._discovery_fingerprint = self._safe_heartbeat_fingerprint()
            return WatchdogAction.DISCOVERING

        snapshot = self._read_heartbeat_lenient()
        if snapshot is not None and snapshot.fingerprint != self._discovery_fingerprint:
            try:
                attached = self._launcher.attach(snapshot.heartbeat.process_id)
            except Exception as exc:
                self._record_marker(
                    reason_code="RUNTIME_ATTACH_FAILED",
                    runtime_process_id=snapshot.heartbeat.process_id,
                    details={"error_type": type(exc).__name__},
                )
                attached = None
            if attached is not None and attached.poll() is None:
                self._runtime = attached
                self._runtime_owned = False
                self._last_heartbeat_fingerprint = snapshot.fingerprint
                self._last_heartbeat_progress_ns = now_ns
                self._launched_ns = now_ns
                self._heartbeat_observed = True
                self._discovery_complete = True
                return WatchdogAction.ATTACHED
            if attached is not None:
                attached.close()
            self._discovery_fingerprint = snapshot.fingerprint

        elapsed_ns = now_ns - self._discovery_started_ns
        if elapsed_ns < self._policy.heartbeat_startup_timeout_seconds * _NANOSECONDS_PER_SECOND:
            return WatchdogAction.DISCOVERING
        self._discovery_complete = True
        return None

    def _inspect_disk(self) -> bool:
        try:
            free_bytes = self._disk_free_bytes(self._storage_root)
        except OSError as exc:
            if not self._disk_low_reported:
                self._record_marker(
                    reason_code="WATCHDOG_DISK_QUERY_FAILED",
                    runtime_process_id=(
                        self._runtime.process_id if self._runtime is not None else None
                    ),
                    details={"error_type": type(exc).__name__},
                )
            self._disk_low_reported = True
            return True
        if type(free_bytes) is not int or free_bytes < 0:
            raise WatchdogSupervisorError(
                "WATCHDOG_DISK_RESULT_INVALID", "disk monitor returned an invalid byte count"
            )
        if free_bytes >= self._minimum_free_bytes:
            return False
        if not self._disk_low_reported:
            self._record_marker(
                reason_code="WATCHDOG_DISK_SPACE_LOW",
                runtime_process_id=self._runtime.process_id if self._runtime is not None else None,
                details={
                    "free_bytes": free_bytes,
                    "minimum_free_bytes": self._minimum_free_bytes,
                },
            )
            self._disk_low_reported = True
        return True

    def _read_heartbeat_lenient(self) -> HeartbeatSnapshot | None:
        try:
            return self._heartbeat_store.read()
        except WatchdogArtifactError as exc:
            self._record_marker(
                reason_code="RUNTIME_HEARTBEAT_INVALID",
                runtime_process_id=self._runtime.process_id if self._runtime is not None else None,
                details={"artifact_error": exc.code},
            )
            return None

    def _read_heartbeat_for_monitoring(self, process_id: int) -> HeartbeatSnapshot | None:
        snapshot = self._read_heartbeat_lenient()
        if snapshot is None or snapshot.heartbeat.process_id != process_id:
            return None
        return snapshot

    def _safe_heartbeat_fingerprint(self) -> str | None:
        snapshot = self._read_heartbeat_lenient()
        return snapshot.fingerprint if snapshot is not None else None

    def _read_marker_fail_closed(self) -> ExternalHaltMarker | None:
        try:
            return self._marker_store.read()
        except WatchdogArtifactError as exc:
            raise WatchdogSupervisorError(
                "WATCHDOG_HALT_MARKER_INVALID",
                f"external HALT marker is unsafe ({exc.code})",
            ) from exc

    def _register_failure(
        self,
        now_ns: int,
        *,
        reason_code: str,
        runtime_process_id: int | None,
        details: Mapping[str, object],
    ) -> None:
        self._record_marker(
            reason_code=reason_code,
            runtime_process_id=runtime_process_id,
            details=details,
        )
        self._register_crash(now_ns)

    def _register_crash(self, now_ns: int) -> None:
        cutoff_ns = now_ns - self._policy.crash_window_seconds * _NANOSECONDS_PER_SECOND
        while self._recent_crashes and self._recent_crashes[0] < cutoff_ns:
            self._recent_crashes.popleft()
        self._recent_crashes.append(now_ns)
        if len(self._recent_crashes) >= self._policy.crash_limit:
            self._circuit_open_until_ns = (
                now_ns + self._policy.circuit_open_seconds * _NANOSECONDS_PER_SECOND
            )
            self._next_launch_ns = self._circuit_open_until_ns
            self._record_marker(
                reason_code="RUNTIME_CRASH_LOOP",
                runtime_process_id=None,
                details={"crash_count": len(self._recent_crashes)},
            )
            return
        exponent = max(0, len(self._recent_crashes) - 1)
        delay = min(
            self._policy.restart_initial_delay_seconds * (2**exponent),
            self._policy.restart_max_delay_seconds,
        )
        self._next_launch_ns = now_ns + delay * _NANOSECONDS_PER_SECOND

    def _scheduled_action(self, now_ns: int) -> WatchdogAction:
        if now_ns < self._circuit_open_until_ns:
            return WatchdogAction.CIRCUIT_OPEN
        return WatchdogAction.RESTART_SCHEDULED

    def _record_marker(
        self,
        *,
        reason_code: str,
        runtime_process_id: int | None,
        details: Mapping[str, object],
    ) -> ExternalHaltMarker:
        try:
            return self._marker_store.record(
                reason_code=reason_code,
                detected_at=self._clock.now(),
                watchdog_process_id=self._watchdog_process_id,
                runtime_process_id=runtime_process_id,
                restart_generation=self._restart_generation,
                details=details,
            )
        except (OSError, ValueError, WatchdogArtifactError) as exc:
            raise WatchdogSupervisorError(
                "WATCHDOG_HALT_MARKER_WRITE_FAILED",
                "external HALT evidence could not be persisted; automatic restart stopped",
            ) from exc

    def _stop_runtime(self, runtime: WatchedProcess) -> None:
        try:
            runtime.terminate()
            runtime.wait(float(self._policy.terminate_grace_seconds))
        except TimeoutError:
            runtime.kill()
            try:
                runtime.wait(float(self._policy.terminate_grace_seconds))
            except TimeoutError as exc:
                raise WatchdogSupervisorError(
                    "WATCHDOG_RUNTIME_STOP_FAILED", "Runtime could not be terminated"
                ) from exc
        finally:
            runtime.close()

    def _snapshot(self, action: WatchdogAction) -> WatchdogSnapshot:
        marker = self._read_marker_fail_closed()
        return WatchdogSnapshot(
            action=action,
            runtime_process_id=self._runtime.process_id if self._runtime is not None else None,
            restart_generation=self._restart_generation,
            recent_crash_count=len(self._recent_crashes),
            heartbeat_observed=self._heartbeat_observed,
            next_launch_monotonic_ns=self._next_launch_ns,
            circuit_open_until_monotonic_ns=self._circuit_open_until_ns,
            active_halt_marker_id=marker.marker_id if marker is not None else None,
        )


class _PopenProcess:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process

    @property
    def process_id(self) -> int:
        return self._process.pid

    def poll(self) -> int | None:
        return self._process.poll()

    def terminate(self) -> None:
        self._process.terminate()

    def wait(self, timeout_seconds: float) -> int:
        try:
            return self._process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError from exc

    def kill(self) -> None:
        self._process.kill()

    def close(self) -> None:
        return None


class _WindowsAttachedProcess:
    _SYNCHRONIZE = 0x00100000
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _WAIT_TIMEOUT = 258

    def __init__(self, process_id: int) -> None:
        if sys.platform != "win32":
            raise WatchdogSupervisorError(
                "WATCHDOG_ATTACH_PLATFORM_UNSUPPORTED", "Runtime attach requires Windows"
            )
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.TerminateProcess.restype = ctypes.c_int
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        access = (
            self._SYNCHRONIZE | self._PROCESS_TERMINATE | self._PROCESS_QUERY_LIMITED_INFORMATION
        )
        handle = kernel32.OpenProcess(access, 0, process_id)
        if not handle:
            raise WatchdogSupervisorError(
                "WATCHDOG_RUNTIME_ATTACH_FAILED",
                f"Win32 error {ctypes.get_last_error()}",
            )
        self._kernel32 = kernel32
        self._handle = int(handle)
        self._process_id = process_id

    @property
    def process_id(self) -> int:
        return self._process_id

    def poll(self) -> int | None:
        code = ctypes.c_uint32()
        if not self._kernel32.GetExitCodeProcess(ctypes.c_void_p(self._handle), ctypes.byref(code)):
            raise WatchdogSupervisorError(
                "WATCHDOG_RUNTIME_POLL_FAILED", f"Win32 error {ctypes.get_last_error()}"
            )
        return None if code.value == self._STILL_ACTIVE else int(code.value)

    def terminate(self) -> None:
        if not self._kernel32.TerminateProcess(ctypes.c_void_p(self._handle), 3):
            raise WatchdogSupervisorError(
                "WATCHDOG_RUNTIME_TERMINATE_FAILED", f"Win32 error {ctypes.get_last_error()}"
            )

    def wait(self, timeout_seconds: float) -> int:
        milliseconds = min(round(timeout_seconds * 1000), 0xFFFFFFFE)
        result = self._kernel32.WaitForSingleObject(ctypes.c_void_p(self._handle), milliseconds)
        if result == self._WAIT_TIMEOUT:
            raise TimeoutError
        polled = self.poll()
        return self._STILL_ACTIVE if polled is None else polled

    def kill(self) -> None:
        self.terminate()

    def close(self) -> None:
        handle = self._handle
        if handle == 0:
            return
        self._handle = 0
        self._kernel32.CloseHandle(ctypes.c_void_p(handle))


class SubprocessRuntimeLauncher:
    """Launch an exact argv without a shell and attach to a heartbeat PID on Windows."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if not command:
            raise ValueError("runtime command cannot be empty")
        executable = Path(command[0])
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("runtime executable must be an existing absolute file")
        if any(not isinstance(argument, str) or "\x00" in argument for argument in command):
            raise ValueError("runtime command arguments must be NUL-free strings")
        self._command = tuple(command)
        self._environment = dict(os.environ if environment is None else environment)

    def attach(self, process_id: int) -> WatchedProcess | None:
        try:
            return _WindowsAttachedProcess(process_id)
        except WatchdogSupervisorError as exc:
            if exc.code == "WATCHDOG_RUNTIME_ATTACH_FAILED":
                return None
            raise

    def start(self, *, force_halt: bool) -> WatchedProcess:
        environment = dict(self._environment)
        if force_halt:
            environment[WATCHDOG_FORCE_HALT_ENVIRONMENT] = "1"
        else:
            environment.pop(WATCHDOG_FORCE_HALT_ENVIRONMENT, None)
        creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        process = subprocess.Popen(
            self._command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            shell=False,
            creationflags=creation_flags,
        )
        return _PopenProcess(process)
