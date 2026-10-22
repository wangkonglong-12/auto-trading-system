"""Windows DACL hardening for the dedicated runtime storage tree."""

from __future__ import annotations

import csv
import io
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_SID_PATTERN = re.compile(r"^S-1-(?:[0-9]+-){1,14}[0-9]+$")
_SYSTEM_SID = "S-1-5-18"
_BROAD_GRANT_SIDS = (
    "S-1-1-0",  # Everyone
    "S-1-5-11",  # Authenticated Users
    "S-1-5-32-544",  # Administrators
    "S-1-5-32-545",  # Users
)


class AclHardeningError(RuntimeError):
    """Stable ACL failure that omits usernames, paths and command output."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class CommandResult:
    return_code: int
    stdout: str


class CommandRunner(Protocol):
    def run(self, arguments: tuple[str, ...], *, timeout_seconds: int) -> CommandResult:
        """Execute an argument-vector command without a shell."""


class UserSidProvider(Protocol):
    def current_user_sid(self) -> str:
        """Return the current process user's SID."""


class SubprocessCommandRunner:
    def run(self, arguments: tuple[str, ...], *, timeout_seconds: int) -> CommandResult:
        try:
            completed = subprocess.run(
                arguments,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AclHardeningError(
                "ACL_COMMAND_FAILED",
                "Windows security command could not be executed",
            ) from exc
        return CommandResult(completed.returncode, completed.stdout)


class WhoAmIUserSidProvider:
    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    def current_user_sid(self) -> str:
        result = self._runner.run(
            ("whoami.exe", "/user", "/fo", "csv", "/nh"),
            timeout_seconds=10,
        )
        if result.return_code != 0:
            raise AclHardeningError(
                "ACL_PRINCIPAL_LOOKUP_FAILED",
                "current Windows principal could not be resolved",
            )
        rows = list(csv.reader(io.StringIO(result.stdout.strip())))
        if len(rows) != 1 or len(rows[0]) < 2:
            raise AclHardeningError(
                "ACL_PRINCIPAL_LOOKUP_FAILED",
                "current Windows principal response is invalid",
            )
        sid = rows[0][-1].strip()
        if _SID_PATTERN.fullmatch(sid) is None:
            raise AclHardeningError(
                "ACL_PRINCIPAL_LOOKUP_FAILED",
                "current Windows principal response is invalid",
            )
        return sid


@dataclass(frozen=True, slots=True)
class DirectoryAclResult:
    """Non-sensitive result facts for audit events."""

    recursive: bool
    protected_principal_count: int


class WindowsDirectoryAclHardener:
    """Restrict one dedicated tree to its service user and LocalSystem."""

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        sid_provider: UserSidProvider | None = None,
    ) -> None:
        effective_runner = SubprocessCommandRunner() if runner is None else runner
        self._runner = effective_runner
        self._sid_provider = (
            WhoAmIUserSidProvider(effective_runner) if sid_provider is None else sid_provider
        )

    def harden(self, target: Path, *, recursive: bool = True) -> DirectoryAclResult:
        if os.name != "nt" and isinstance(self._runner, SubprocessCommandRunner):
            raise AclHardeningError(
                "ACL_PLATFORM_UNSUPPORTED",
                "Windows directory ACLs are unavailable on this platform",
            )
        if type(recursive) is not bool:
            raise AclHardeningError("ACL_REQUEST_INVALID", "recursive flag must be boolean")
        resolved = self._validate_target(target)
        user_sid = self._sid_provider.current_user_sid()
        if _SID_PATTERN.fullmatch(user_sid) is None:
            raise AclHardeningError(
                "ACL_PRINCIPAL_LOOKUP_FAILED",
                "current Windows principal response is invalid",
            )
        arguments = [
            "icacls.exe",
            str(resolved),
            "/inheritance:r",
            "/remove:g",
            *(f"*{sid}" for sid in _BROAD_GRANT_SIDS),
            "/grant:r",
            f"*{user_sid}:(OI)(CI)F",
            f"*{_SYSTEM_SID}:(OI)(CI)F",
        ]
        if recursive:
            arguments.append("/T")
        arguments.append("/Q")
        result = self._runner.run(tuple(arguments), timeout_seconds=30)
        if result.return_code != 0:
            raise AclHardeningError(
                "ACL_HARDENING_FAILED",
                "runtime storage permissions could not be applied",
            )
        return DirectoryAclResult(recursive=recursive, protected_principal_count=2)

    @staticmethod
    def _validate_target(target: Path) -> Path:
        if not isinstance(target, Path) or not target.is_absolute():
            raise AclHardeningError(
                "ACL_TARGET_INVALID",
                "ACL target must be an absolute directory",
            )
        if target.is_symlink():
            raise AclHardeningError("ACL_TARGET_INVALID", "ACL target must not be a symlink")
        try:
            resolved = target.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise AclHardeningError(
                "ACL_TARGET_INVALID",
                "ACL target cannot be resolved",
            ) from exc
        if not resolved.is_dir() or resolved == Path(resolved.anchor):
            raise AclHardeningError(
                "ACL_TARGET_INVALID",
                "ACL target must be a dedicated non-root directory",
            )
        return resolved
