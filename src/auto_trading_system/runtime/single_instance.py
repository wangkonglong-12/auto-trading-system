"""Account-scoped process ownership using a Windows named mutex."""

from __future__ import annotations

import ctypes
import hashlib
import re
import sys
from typing import Any, Protocol

ERROR_ALREADY_EXISTS = 183
MUTEX_PREFIX = "AutoTrading.Account"


class InstanceGuardError(RuntimeError):
    """Fail-closed instance ownership error without account disclosure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class InstanceAlreadyRunningError(InstanceGuardError):
    """Raised when another process already owns the account mutex."""

    def __init__(self, account_fingerprint: str) -> None:
        super().__init__(
            "ACCOUNT_INSTANCE_ALREADY_RUNNING",
            f"another runtime owns account fingerprint {account_fingerprint}",
        )
        self.account_fingerprint = account_fingerprint


class InstanceLockBackend(Protocol):
    """Minimal OS-lock adapter used by the lifecycle composition root."""

    def acquire(self, name: str, *, account_fingerprint: str) -> object:
        """Acquire exclusive ownership or raise InstanceGuardError."""
        ...

    def release(self, token: object) -> None:
        """Release and close a previously acquired ownership token."""
        ...


class WindowsNamedMutexBackend:
    """Win32 CreateMutex implementation with immediate collision detection."""

    __slots__ = ("_kernel32",)

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise InstanceGuardError(
                "INSTANCE_GUARD_PLATFORM_UNSUPPORTED",
                "Windows named mutexes require win32",
            )
        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:  # pragma: no cover - defensive on a malformed Windows runtime
            raise InstanceGuardError(
                "INSTANCE_GUARD_API_UNAVAILABLE",
                "Win32 synchronization API is unavailable",
            )
        kernel32 = win_dll("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel32.ReleaseMutex.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        self._kernel32: Any = kernel32

    def acquire(self, name: str, *, account_fingerprint: str) -> object:
        ctypes.set_last_error(0)
        handle = self._kernel32.CreateMutexW(None, 1, name)
        if not handle:
            error_code = ctypes.get_last_error()
            raise InstanceGuardError(
                "INSTANCE_MUTEX_CREATE_FAILED",
                f"Win32 error {error_code}",
            )
        error_code = ctypes.get_last_error()
        if error_code == ERROR_ALREADY_EXISTS:
            self._kernel32.CloseHandle(handle)
            raise InstanceAlreadyRunningError(account_fingerprint)
        return int(handle)

    def release(self, token: object) -> None:
        if type(token) is not int or token == 0:
            raise InstanceGuardError("INSTANCE_MUTEX_TOKEN_INVALID", "mutex token is invalid")
        handle = ctypes.c_void_p(token)
        release_succeeded = bool(self._kernel32.ReleaseMutex(handle))
        release_error = 0 if release_succeeded else ctypes.get_last_error()
        close_succeeded = bool(self._kernel32.CloseHandle(handle))
        close_error = 0 if close_succeeded else ctypes.get_last_error()
        if not release_succeeded:
            raise InstanceGuardError(
                "INSTANCE_MUTEX_RELEASE_FAILED",
                f"Win32 error {release_error}",
            )
        if not close_succeeded:
            raise InstanceGuardError(
                "INSTANCE_MUTEX_CLOSE_FAILED",
                f"Win32 error {close_error}",
            )


class AccountInstanceGuard:
    """Own exactly one OS lock for a normalized brokerage account identity."""

    __slots__ = ("_account_fingerprint", "_backend", "_mutex_name", "_token")

    def __init__(
        self,
        account_key: str,
        *,
        backend: InstanceLockBackend,
        namespace: str = "Global",
    ) -> None:
        normalized = _normalize_account_key(account_key)
        if namespace not in {"Global", "Local"}:
            raise ValueError("namespace must be Global or Local")
        digest = hashlib.sha256(normalized.encode("ascii")).hexdigest()
        self._account_fingerprint = digest[:12]
        self._mutex_name = f"{namespace}\\{MUTEX_PREFIX}.{digest}"
        self._backend = backend
        self._token: object | None = None

    @property
    def account_fingerprint(self) -> str:
        return self._account_fingerprint

    @property
    def mutex_name(self) -> str:
        """Return the non-secret digest-based diagnostics name."""
        return self._mutex_name

    @property
    def is_acquired(self) -> bool:
        return self._token is not None

    def acquire(self) -> None:
        if self._token is not None:
            raise InstanceGuardError(
                "INSTANCE_GUARD_ALREADY_ACQUIRED",
                "this guard already owns the account mutex",
            )
        token = self._backend.acquire(
            self._mutex_name,
            account_fingerprint=self._account_fingerprint,
        )
        if token is None:
            raise InstanceGuardError(
                "INSTANCE_MUTEX_TOKEN_INVALID",
                "lock backend returned no ownership token",
            )
        self._token = token

    def release(self) -> None:
        token = self._token
        if token is None:
            return
        self._token = None
        self._backend.release(token)

    def __enter__(self) -> AccountInstanceGuard:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.release()

    def __repr__(self) -> str:
        return (
            "AccountInstanceGuard("
            f"account_fingerprint={self._account_fingerprint!r}, acquired={self.is_acquired})"
        )


def _normalize_account_key(account_key: str) -> str:
    if (
        not isinstance(account_key, str)
        or not 4 <= len(account_key) <= 64
        or re.fullmatch(r"[A-Za-z0-9]+", account_key) is None
    ):
        raise InstanceGuardError(
            "ACCOUNT_INSTANCE_KEY_INVALID",
            "broker account key must be 4-64 ASCII letters or digits",
        )
    return account_key.upper()


def create_account_instance_guard(
    account_key: str,
    *,
    namespace: str = "Global",
) -> AccountInstanceGuard:
    """Create the production account guard backed by a Win32 named mutex."""
    return AccountInstanceGuard(
        account_key,
        backend=WindowsNamedMutexBackend(),
        namespace=namespace,
    )
