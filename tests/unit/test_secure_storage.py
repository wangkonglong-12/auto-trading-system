from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from auto_trading_system.security import (
    AclHardeningError,
    DirectoryAclResult,
    SecureStorageError,
    initialize_secure_storage,
)


@dataclass
class RecordingHardener:
    target: Path | None = None
    recursive: bool | None = None
    fail: bool = False

    def harden(self, target: Path, *, recursive: bool = True) -> DirectoryAclResult:
        self.target = target
        self.recursive = recursive
        if self.fail:
            raise AclHardeningError("ACL_HARDENING_FAILED", "deliberate failure")
        return DirectoryAclResult(recursive=recursive, protected_principal_count=2)


def test_initializer_creates_fixed_layout_then_hardens_entire_tree(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    hardener = RecordingHardener()

    result = initialize_secure_storage(root, hardener=hardener)

    assert result.directory_count == 6
    assert hardener.target == root.resolve()
    assert hardener.recursive is True
    assert {item.name for item in root.iterdir()} == {
        "data",
        "logs",
        "diagnostics",
        "ticks",
        "backups",
    }


def test_initializer_fails_closed_when_acl_cannot_be_applied(tmp_path: Path) -> None:
    root = tmp_path / "runtime"

    with pytest.raises(SecureStorageError) as exc_info:
        initialize_secure_storage(root, hardener=RecordingHardener(fail=True))

    assert exc_info.value.code == "SECURE_STORAGE_ACL_FAILED"
    assert str(root) not in str(exc_info.value)


def test_initializer_rejects_broad_and_non_absolute_roots(tmp_path: Path) -> None:
    for root in (Path("relative"), Path(tmp_path.anchor)):
        with pytest.raises(SecureStorageError, match="SECURE_STORAGE_ROOT_INVALID"):
            initialize_secure_storage(root, hardener=RecordingHardener())
