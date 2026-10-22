from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from auto_trading_system.security.windows_acl import (
    AclHardeningError,
    CommandResult,
    WindowsDirectoryAclHardener,
)

USER_SID = "S-1-5-21-111-222-333-1001"


@dataclass
class RecordingRunner:
    acl_return_code: int = 0
    calls: list[tuple[tuple[str, ...], int]] = field(default_factory=list)

    def run(self, arguments: tuple[str, ...], *, timeout_seconds: int) -> CommandResult:
        self.calls.append((arguments, timeout_seconds))
        if arguments[0] == "whoami.exe":
            return CommandResult(0, f'"DOMAIN\\runtime","{USER_SID}"\n')
        return CommandResult(self.acl_return_code, "sensitive local path and user name")


def test_hardener_uses_sid_only_argument_vector_and_removes_broad_grants(
    tmp_path: Path,
) -> None:
    target = tmp_path / "runtime"
    target.mkdir()
    runner = RecordingRunner()
    hardener = WindowsDirectoryAclHardener(runner=runner)

    result = hardener.harden(target)

    assert result.recursive is True
    assert result.protected_principal_count == 2
    assert runner.calls[0][0] == ("whoami.exe", "/user", "/fo", "csv", "/nh")
    command = runner.calls[1][0]
    assert command[0] == "icacls.exe"
    assert command[1] == str(target.resolve())
    assert "/inheritance:r" in command
    assert "/remove:g" in command
    assert f"*{USER_SID}:(OI)(CI)F" in command
    assert "*S-1-5-18:(OI)(CI)F" in command
    assert command[-2:] == ("/T", "/Q")


def test_acl_command_failure_is_stable_and_omits_command_output(tmp_path: Path) -> None:
    target = tmp_path / "runtime"
    target.mkdir()
    runner = RecordingRunner(acl_return_code=5)

    with pytest.raises(AclHardeningError) as exc_info:
        WindowsDirectoryAclHardener(runner=runner).harden(target)

    assert exc_info.value.code == "ACL_HARDENING_FAILED"
    assert "sensitive local path" not in str(exc_info.value)
    assert str(target) not in str(exc_info.value)


@pytest.mark.parametrize(
    "target_factory",
    [lambda path: Path("relative"), lambda path: Path(path.anchor)],
)
def test_hardener_rejects_relative_and_filesystem_root_targets(
    tmp_path: Path,
    target_factory: Callable[[Path], Path],
) -> None:
    with pytest.raises(AclHardeningError) as exc_info:
        WindowsDirectoryAclHardener(runner=RecordingRunner()).harden(target_factory(tmp_path))

    assert exc_info.value.code == "ACL_TARGET_INVALID"
