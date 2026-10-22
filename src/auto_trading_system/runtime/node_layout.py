"""Explicit new-directory initialization and fail-closed Windows ACL checks.

Initialization never adopts an existing directory or changes its ACL. Each
process checks the immutable scope marker before opening its own resources.
"""

from __future__ import annotations

import os
from pathlib import Path

import win32security

from auto_trading_system.config.node_v2 import NodeProcessConfig
from auto_trading_system.control.windows_pipe import current_sid


def initialize_layout(config: NodeProcessConfig) -> None:
    if current_sid() != config.service_sid:
        raise ValueError("initialize as the configured service user")
    root = config.data_root
    create_private_directory(root, config.service_sid)
    with (root / "node-scope.json").open("xb") as target:
        target.write(config.scope())
        target.flush()
        os.fsync(target.fileno())
    for name in ("critical", "artifacts", "daily-catalog", "jobs", "logs"):
        (root / name).mkdir()
    validate_layout(config)


def create_private_directory(root: Path, sid: str) -> None:
    """Create a new private directory; never adopt or re-ACL an existing path."""
    if current_sid() != sid or not root.is_absolute() or root.resolve() != root.absolute():
        raise ValueError("private directory requires the current user and a direct absolute path")
    # Never grant ourselves rights on a caller-supplied existing collection.
    root.mkdir(parents=False, exist_ok=False)
    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{sid})",
        win32security.SDDL_REVISION_1,
    )
    win32security.SetFileSecurity(
        str(root),
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        descriptor,
    )


def _private_acl(path: Path, sid: str) -> None:
    if path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError("node paths must not traverse links")
    descriptor = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
    acl = descriptor.GetSecurityDescriptorDacl()
    if acl is None or acl.GetAceCount() == 0:
        raise ValueError("node path requires explicit access control")
    for index in range(acl.GetAceCount()):
        ace = acl.GetAce(index)
        if ace[0][0] != win32security.ACCESS_ALLOWED_ACE_TYPE:
            raise ValueError("unsupported node access rule")
        if win32security.ConvertSidToStringSid(ace[2]) not in (sid, "S-1-5-18"):
            raise ValueError("node data is accessible to an unapproved identity")


def validate_layout(config: NodeProcessConfig) -> None:
    # Restoration writes this fence BEFORE any data. A failed/partial restore
    # cannot be adopted by engine, agent, supervisor or init after our locks exit.
    fence = config.data_root / "restore-pending.json"
    if fence.exists() or fence.is_symlink():
        raise ValueError("RESTORE_REVIEW_REQUIRED")
    if current_sid() != config.service_sid:
        raise ValueError("process service identity differs from configuration")
    for name in ("", "node-scope.json", "critical", "artifacts", "daily-catalog", "jobs", "logs"):
        _private_acl(config.data_root / name, config.service_sid)
    with (config.data_root / "node-scope.json").open("rb") as source:
        if source.read(2049) != config.scope():
            raise ValueError("node directory scope differs from configuration")
