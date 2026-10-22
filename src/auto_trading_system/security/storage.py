"""Secure initialization of the configured runtime storage layout."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from auto_trading_system.security.windows_acl import (
    AclHardeningError,
    DirectoryAclResult,
    WindowsDirectoryAclHardener,
)

_RUNTIME_DIRECTORIES = ("data", "logs", "diagnostics", "ticks", "backups")


class DirectoryAclHardener(Protocol):
    def harden(self, target: Path, *, recursive: bool = True) -> DirectoryAclResult:
        """Restrict an existing directory tree."""


class SecureStorageError(RuntimeError):
    """Stable storage initialization failure without exposing local paths."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class SecureStorageResult:
    directory_count: int
    acl: DirectoryAclResult


def initialize_secure_storage(
    root: Path,
    *,
    hardener: DirectoryAclHardener | None = None,
) -> SecureStorageResult:
    """Create the fixed runtime tree and fail startup unless its DACL is protected."""
    if not isinstance(root, Path) or not root.is_absolute():
        raise SecureStorageError(
            "SECURE_STORAGE_ROOT_INVALID",
            "runtime storage root must be an absolute path",
        )
    if root.is_symlink() or root == Path(root.anchor):
        raise SecureStorageError(
            "SECURE_STORAGE_ROOT_INVALID",
            "runtime storage root must be a dedicated non-root directory",
        )
    if not root.parent.is_dir() or root.parent.is_symlink():
        raise SecureStorageError(
            "SECURE_STORAGE_PARENT_INVALID",
            "runtime storage parent must be an existing physical directory",
        )
    try:
        root.mkdir(exist_ok=True)
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_dir() or resolved_root == Path(resolved_root.anchor):
            raise SecureStorageError(
                "SECURE_STORAGE_ROOT_INVALID",
                "runtime storage root must be a directory",
            )
        for name in _RUNTIME_DIRECTORIES:
            child = resolved_root / name
            if child.is_symlink():
                raise SecureStorageError(
                    "SECURE_STORAGE_CHILD_INVALID",
                    "runtime storage child must not be a symlink",
                )
            child.mkdir(exist_ok=True)
            if not child.is_dir():
                raise SecureStorageError(
                    "SECURE_STORAGE_CHILD_INVALID",
                    "runtime storage child must be a directory",
                )
    except SecureStorageError:
        raise
    except OSError as exc:
        raise SecureStorageError(
            "SECURE_STORAGE_CREATE_FAILED",
            "runtime storage layout could not be created",
        ) from exc

    effective_hardener = WindowsDirectoryAclHardener() if hardener is None else hardener
    try:
        acl_result = effective_hardener.harden(resolved_root, recursive=True)
    except AclHardeningError as exc:
        raise SecureStorageError(
            "SECURE_STORAGE_ACL_FAILED",
            "runtime storage permissions could not be protected",
        ) from exc
    return SecureStorageResult(
        directory_count=len(_RUNTIME_DIRECTORIES) + 1,
        acl=acl_result,
    )
