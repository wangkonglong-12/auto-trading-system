from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from auto_trading_system.apps.trade_runtime.bootstrap import build_instance_guard
from auto_trading_system.config import load_runtime_configuration
from auto_trading_system.runtime import (
    AccountInstanceGuard,
    InstanceAlreadyRunningError,
    InstanceGuardError,
    WindowsNamedMutexBackend,
    create_account_instance_guard,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config" / "v1" / "runtime.read-only.example.json"


class FakeLockBackend:
    def __init__(self) -> None:
        self.held: dict[str, object] = {}
        self.released: list[object] = []

    def acquire(self, name: str, *, account_fingerprint: str) -> object:
        if name in self.held:
            raise InstanceAlreadyRunningError(account_fingerprint)
        token = object()
        self.held[name] = token
        return token

    def release(self, token: object) -> None:
        name = next(name for name, current in self.held.items() if current is token)
        del self.held[name]
        self.released.append(token)


class EmptyTokenBackend(FakeLockBackend):
    def acquire(self, name: str, *, account_fingerprint: str) -> object:
        return None


class FailingReleaseBackend(FakeLockBackend):
    def release(self, token: object) -> None:
        raise InstanceGuardError("TEST_RELEASE_FAILED", "injected failure")


class FakeKernel32:
    def __init__(
        self, *, create_result: int = 1, release_result: int = 1, close_result: int = 1
    ) -> None:
        self.create_result = create_result
        self.release_result = release_result
        self.close_result = close_result

    def CreateMutexW(self, security: object, initial_owner: int, name: str) -> int:
        ctypes.set_last_error(5 if self.create_result == 0 else 0)
        return self.create_result

    def ReleaseMutex(self, handle: object) -> int:
        ctypes.set_last_error(6 if self.release_result == 0 else 0)
        return self.release_result

    def CloseHandle(self, handle: object) -> int:
        ctypes.set_last_error(6 if self.close_result == 0 else 0)
        return self.close_result


def backend_with_api(api: FakeKernel32) -> WindowsNamedMutexBackend:
    backend = object.__new__(WindowsNamedMutexBackend)
    backend._kernel32 = api
    return backend


@pytest.mark.parametrize("account", ["", "abc", "account_01", "账户1234", "A" * 65, 1234])
def test_account_instance_key_is_strict(account: object) -> None:
    with pytest.raises(InstanceGuardError, match="ACCOUNT_INSTANCE_KEY_INVALID"):
        AccountInstanceGuard(account, backend=FakeLockBackend())  # type: ignore[arg-type]


def test_mutex_name_is_stable_case_insensitive_and_redacted() -> None:
    backend = FakeLockBackend()
    first = AccountInstanceGuard("Broker12345", backend=backend)
    second = AccountInstanceGuard("BROKER12345", backend=backend)

    assert first.mutex_name == second.mutex_name
    assert first.mutex_name.startswith("Global\\AutoTrading.Account.")
    assert "Broker12345" not in first.mutex_name
    assert "BROKER12345" not in first.mutex_name
    assert len(first.account_fingerprint) == 12
    assert "Broker12345" not in repr(first)


def test_local_namespace_is_explicit_and_other_names_are_rejected() -> None:
    guard = AccountInstanceGuard("Broker12345", backend=FakeLockBackend(), namespace="Local")
    assert guard.mutex_name.startswith("Local\\")

    with pytest.raises(ValueError, match="namespace must be Global or Local"):
        AccountInstanceGuard("Broker12345", backend=FakeLockBackend(), namespace="Session")


def test_guard_context_owns_and_releases_exactly_once() -> None:
    backend = FakeLockBackend()
    guard = AccountInstanceGuard("Broker12345", backend=backend)

    guard.release()
    with guard as acquired:
        assert acquired is guard
        acquired_state = guard.is_acquired
        assert acquired_state
        assert len(backend.held) == 1
    released_state = guard.is_acquired
    assert not released_state
    assert backend.held == {}
    assert len(backend.released) == 1


def test_same_guard_cannot_acquire_twice() -> None:
    guard = AccountInstanceGuard("Broker12345", backend=FakeLockBackend())
    guard.acquire()
    try:
        with pytest.raises(InstanceGuardError, match="INSTANCE_GUARD_ALREADY_ACQUIRED"):
            guard.acquire()
    finally:
        guard.release()


def test_two_guards_for_same_account_collide_and_different_accounts_do_not() -> None:
    backend = FakeLockBackend()
    first = AccountInstanceGuard("Broker12345", backend=backend)
    duplicate = AccountInstanceGuard("broker12345", backend=backend)
    other = AccountInstanceGuard("Broker54321", backend=backend)

    with first, other:
        with pytest.raises(InstanceAlreadyRunningError) as error:
            duplicate.acquire()
        assert error.value.code == "ACCOUNT_INSTANCE_ALREADY_RUNNING"
        assert "Broker12345" not in str(error.value)


def test_missing_backend_token_fails_closed() -> None:
    guard = AccountInstanceGuard("Broker12345", backend=EmptyTokenBackend())

    with pytest.raises(InstanceGuardError, match="INSTANCE_MUTEX_TOKEN_INVALID"):
        guard.acquire()

    assert not guard.is_acquired


def test_release_failure_clears_local_ownership_state() -> None:
    guard = AccountInstanceGuard("Broker12345", backend=FailingReleaseBackend())
    guard.acquire()

    with pytest.raises(InstanceGuardError, match="TEST_RELEASE_FAILED"):
        guard.release()

    assert not guard.is_acquired


def test_windows_backend_fails_closed_on_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("auto_trading_system.runtime.single_instance.sys.platform", "linux")

    with pytest.raises(InstanceGuardError, match="INSTANCE_GUARD_PLATFORM_UNSUPPORTED"):
        WindowsNamedMutexBackend()


def test_windows_backend_fails_when_win32_api_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("auto_trading_system.runtime.single_instance.sys.platform", "win32")
    monkeypatch.setattr("auto_trading_system.runtime.single_instance.ctypes.WinDLL", None)

    with pytest.raises(InstanceGuardError, match="INSTANCE_GUARD_API_UNAVAILABLE"):
        WindowsNamedMutexBackend()


def test_windows_backend_reports_create_release_and_close_failures() -> None:
    create_failure = backend_with_api(FakeKernel32(create_result=0))
    with pytest.raises(InstanceGuardError, match="INSTANCE_MUTEX_CREATE_FAILED"):
        create_failure.acquire("Global\\test", account_fingerprint="abcdef123456")

    release_failure = backend_with_api(FakeKernel32(release_result=0))
    with pytest.raises(InstanceGuardError, match="INSTANCE_MUTEX_RELEASE_FAILED"):
        release_failure.release(1)

    close_failure = backend_with_api(FakeKernel32(close_result=0))
    with pytest.raises(InstanceGuardError, match="INSTANCE_MUTEX_CLOSE_FAILED"):
        close_failure.release(1)


def test_bootstrap_binds_mutex_to_resolved_broker_account_without_disclosure() -> None:
    broker_account = "ACTUAL123456"
    loaded = load_runtime_configuration(
        CONFIG_PATH,
        environment={
            "ATS_SECRET_NODE_HMAC": "0123456789abcdef0123456789abcdef",
            "ATS_SECRET_BROKER_ACCOUNT": broker_account,
            "ATS_SECRET_OPERATOR_PASSWORD_VERIFIER": (
                "ats-scrypt-v1$32768$8$1$AAECAwQFBgcICQoLDA0ODw$"
                "f81LIxrvonq2ysChD17nLqfS0_FxQ1_5qy8BxpPy-n4"
            ),
        },
    )
    backend = FakeLockBackend()

    guard = build_instance_guard(loaded, backend=backend)

    assert broker_account not in guard.mutex_name
    assert broker_account not in repr(guard)
    with guard:
        assert guard.is_acquired


@pytest.mark.skipif(sys.platform != "win32", reason="requires Win32 named mutex")
def test_real_windows_mutex_blocks_same_process_and_releases() -> None:
    account = f"RT001{uuid4().hex}"
    first = create_account_instance_guard(account)
    duplicate = create_account_instance_guard(account.lower())

    with first, pytest.raises(InstanceAlreadyRunningError):
        duplicate.acquire()
    with duplicate:
        assert duplicate.is_acquired


@pytest.mark.skipif(sys.platform != "win32", reason="requires Win32 named mutex")
def test_real_windows_mutex_blocks_second_process_and_recovers_after_exit(tmp_path: Path) -> None:
    account = f"RT001{uuid4().hex}"
    ready_path = tmp_path / "child-ready"
    stop_path = tmp_path / "child-stop"
    child_code = "\n".join(
        (
            "import sys, time",
            "from pathlib import Path",
            "from auto_trading_system.runtime import create_account_instance_guard",
            "guard = create_account_instance_guard(sys.argv[1])",
            "guard.acquire()",
            "ready_path, stop_path = Path(sys.argv[2]), Path(sys.argv[3])",
            "ready_path.write_text('ready', encoding='ascii')",
            "while not stop_path.exists(): time.sleep(0.02)",
            "guard.release()",
        )
    )
    process = subprocess.Popen(
        [sys.executable, "-c", child_code, account, str(ready_path), str(stop_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready_path.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if not ready_path.exists():
            stdout, stderr = process.communicate(timeout=2)
            pytest.fail(f"child did not acquire mutex: stdout={stdout!r}, stderr={stderr!r}")

        with pytest.raises(InstanceAlreadyRunningError):
            create_account_instance_guard(account).acquire()
        stop_path.write_text("stop", encoding="ascii")
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)

    with create_account_instance_guard(account) as recovered:
        assert recovered.is_acquired


@pytest.mark.skipif(sys.platform != "win32", reason="requires Win32 named mutex")
def test_windows_backend_rejects_invalid_release_token() -> None:
    backend = WindowsNamedMutexBackend()

    with pytest.raises(InstanceGuardError, match="INSTANCE_MUTEX_TOKEN_INVALID"):
        backend.release(None)
