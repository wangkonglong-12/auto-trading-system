"""Stopped-node backup and fenced restoration into a fresh private directory.

The account and supervisor guards prevent restart while the agent lease freezes
its file index. SQLite Backup API incorporates WAL; copying the live main file
is never used. A pinned manifest digest binds verification to the chosen backup.
Neither backup nor restoration activates a node or restores trading permission.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from auto_trading_system.config.node_v2 import NodeProcessConfig
from auto_trading_system.journal.apsw_backup import JournalProof, copy_journal
from auto_trading_system.journal.apsw_backup import inspect_journal as inspect_storage
from auto_trading_system.protocol.v2.base import Sha256, WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest, parse_bytes
from auto_trading_system.runtime.node_layout import create_private_directory, validate_layout
from auto_trading_system.runtime.node_supervisor import supervisor_guard
from auto_trading_system.runtime.single_instance import WindowsNamedMutexBackend
from auto_trading_system.sync.job_store import JobStore

MAX_FILES = 16000
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_ALLOWED = re.compile(
    r"(?:critical/(?:journal-v2\.db|supervision-state\.json|paper-broker\.json|plan-recovery\.json)"
    r"|artifacts/[a-f0-9]{64}\.json"
    r"|daily-catalog/\d{4}-\d{2}-\d{2}(?:\.r\d{4})?\.json"
    r"|jobs/(?:[a-f0-9]{64}|\.agent-scope|\.retry-(?:all|pending|synced)-cursor)\.json)"
)


class BackupFile(WireModel):
    path: str = Field(description="产品白名单内的相对路径，固定正斜杠")
    byte_length: int = Field(ge=0, le=MAX_FILE_BYTES, description="文件字节数")
    sha256: Sha256 = Field(description="文件完整内容摘要")

    @model_validator(mode="after")
    def allowed(self) -> BackupFile:
        if not _ALLOWED.fullmatch(self.path):
            raise ValueError("backup path outside product allowlist")
        return self


class BackupManifest(WireModel):
    schema_major: Literal[1] = Field(default=1, description="维护备份清单主版本")
    scope_hash: Sha256 = Field(description="节点/账户/部署代际/配置Journal代际/SID范围摘要")
    created_at: AwareDatetime = Field(description="备份完成时间，UTC")
    journal: JournalProof = Field(description="一致性数据库的结构和正文校验证据")
    files: tuple[BackupFile, ...] = Field(
        min_length=1, max_length=MAX_FILES, description="完整文件目录"
    )

    @model_validator(mode="after")
    def bounded(self) -> BackupManifest:
        names = [item.path for item in self.files]
        if len(set(names)) != len(names) or "critical/journal-v2.db" not in names:
            raise ValueError("backup requires one Journal and unique file paths")
        if sum(item.byte_length for item in self.files) > MAX_TOTAL_BYTES:
            raise ValueError("backup exceeds total byte budget")
        return self


def _regular(path: Path) -> None:
    info = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        or path.resolve() != path.absolute()
    ):
        raise ValueError("backup files must be direct regular files without links")


def _directory(path: Path) -> None:
    info = path.stat(follow_symlinks=False)
    if (
        not path.is_absolute()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        or path.resolve() != path.absolute()
    ):
        raise ValueError("backup directory cannot traverse links")


def _new_destination(destination: Path, source: Path) -> None:
    if (
        not destination.is_absolute()
        or destination.exists()
        or destination.is_symlink()
        or destination.resolve() != destination.absolute()
        or destination.is_relative_to(source)
        or source.is_relative_to(destination)
    ):
        raise ValueError("destination must be a new separate directory")
    _directory(destination.parent)


def _write(path: Path, body: bytes) -> None:
    with path.open("xb") as output:
        output.write(body)
        output.flush()
        os.fsync(output.fileno())


def _file_proof(path: Path, relative: str) -> BackupFile:
    _regular(path)
    length = path.stat().st_size
    if length > MAX_FILE_BYTES:
        raise ValueError("backup file exceeds byte budget")
    sha, actual = hashlib.sha256(), 0
    with path.open("rb") as source:
        while body := source.read(1024 * 1024):
            actual += len(body)
            if actual > length:
                raise ValueError("backup file grew during verification")
            sha.update(body)
    if actual != length:
        raise ValueError("backup file shrank during verification")
    return BackupFile(path=relative, byte_length=length, sha256="sha256:" + sha.hexdigest())


def _copy(source: Path, target: Path, expected: BackupFile) -> None:
    _regular(source)
    current = hashlib.sha256()
    length = 0
    with source.open("rb") as input_file, target.open("xb") as output:
        while body := input_file.read(1024 * 1024):
            length += len(body)
            if length > expected.byte_length:
                raise ValueError("backup source changed during copy")
            current.update(body)
            output.write(body)
        output.flush()
        os.fsync(output.fileno())
    if length != expected.byte_length or "sha256:" + current.hexdigest() != expected.sha256:
        raise ValueError("backup content changed during copy")


def _inventory(root: Path, *, bundle: bool) -> list[str]:
    result: list[str] = []
    _directory(root)
    if bundle and {item.name for item in root.iterdir()} != {
        "manifest.json",
        "critical",
        "artifacts",
        "daily-catalog",
        "jobs",
    }:
        raise ValueError("unexpected backup root entry")
    for folder in ("critical", "artifacts", "daily-catalog", "jobs"):
        directory = root / folder
        _directory(directory)
        with os.scandir(directory) as entries:
            for entry in entries:
                relative = folder + "/" + entry.name
                if not bundle and (
                    entry.name.startswith(".pending-")
                    or relative in ("critical/journal-v2.db-wal", "critical/journal-v2.db-shm")
                ):
                    continue
                if not _ALLOWED.fullmatch(relative):
                    raise ValueError("unexpected file in protected backup scope")
                _regular(Path(entry.path))
                result.append(relative)
                if len(result) > MAX_FILES:
                    raise ValueError("backup file count exceeds budget")
    return sorted(result)


def inspect_journal(path: Path, account_id: str) -> JournalProof:
    """Validate file boundaries before delegating database checks to storage."""
    _regular(path)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Journal exceeds maintenance budget")
    return inspect_storage(path, account_id)


@contextmanager
def _maintenance(config: NodeProcessConfig) -> Iterator[None]:
    validate_layout(config)
    with (
        supervisor_guard(config),
        supervisor_guard(config, engine=True),
        JobStore(
            config.data_root / "jobs",
            node_id=config.node_id,
            account_id=config.account_id,
            backend=WindowsNamedMutexBackend(),
        ),
    ):
        yield


def create_backup(config: NodeProcessConfig, destination: Path) -> str:
    """Return a manifest digest only after all data and verification are durable."""
    _new_destination(destination, config.data_root)
    with _maintenance(config):
        names = _inventory(config.data_root, bundle=False)
        if "critical/journal-v2.db" not in names:
            raise ValueError("Journal is required before backup")
        # Preflight all byte budgets before creating a partial output directory.
        if sum((config.data_root / name).stat().st_size for name in names) > MAX_TOTAL_BYTES:
            raise ValueError("backup exceeds total byte budget")
        create_private_directory(destination, config.service_sid)
        for folder in ("critical", "artifacts", "daily-catalog", "jobs"):
            (destination / folder).mkdir()
        path = config.data_root / "critical/journal-v2.db"
        copy_journal(path, destination / "critical/journal-v2.db", max_bytes=MAX_FILE_BYTES)
        for name in names:
            if name != "critical/journal-v2.db":
                _copy(
                    config.data_root / name,
                    destination / name,
                    _file_proof(config.data_root / name, name),
                )
        if _inventory(config.data_root, bundle=False) != names:
            raise ValueError("backup source inventory changed during maintenance")
        proof = inspect_journal(destination / "critical/journal-v2.db", config.account_id)
        manifest = BackupManifest(
            scope_hash=digest(config.scope()),
            created_at=datetime.now(UTC),
            journal=proof,
            files=tuple(_file_proof(destination / name, name) for name in names),
        )
        body = canonical_bytes(manifest.model_dump(mode="json"))
        if len(body) > MAX_MANIFEST_BYTES:
            raise ValueError("backup manifest exceeds budget")
        # This is the sole completion marker. Interrupted directories remain
        # evidence for inspection and are never deleted/reused automatically.
        _write(destination / "manifest.json", body)
        return digest(body)


def verify_backup(config: NodeProcessConfig, bundle: Path, manifest_hash: str) -> BackupManifest:
    _directory(bundle)
    _regular(bundle / "manifest.json")
    with (bundle / "manifest.json").open("rb") as source:
        body = source.read(MAX_MANIFEST_BYTES + 1)
    if len(body) > MAX_MANIFEST_BYTES or digest(body) != manifest_hash:
        raise ValueError("backup manifest does not match pinned digest")
    manifest = BackupManifest.model_validate_json(
        canonical_bytes(parse_bytes(body, max_bytes=MAX_MANIFEST_BYTES))
    )
    if manifest.scope_hash != digest(config.scope()):
        raise ValueError("backup belongs to a different node scope")
    if _inventory(bundle, bundle=True) != sorted(item.path for item in manifest.files):
        raise ValueError("backup file inventory differs")
    for item in manifest.files:
        if _file_proof(bundle / item.path, item.path) != item:
            raise ValueError("backup file digest differs")
    if inspect_journal(bundle / "critical/journal-v2.db", config.account_id) != manifest.journal:
        raise ValueError("backup Journal proof differs")
    return manifest


def restore_candidate(
    config: NodeProcessConfig,
    bundle: Path,
    manifest_hash: str,
    destination: Path,
) -> BackupManifest:
    """Restore a verified candidate, retaining source data and a startup fence."""
    _new_destination(destination, bundle)
    _new_destination(destination, config.data_root)
    # No source runtime directory is required after physical disk loss, but the
    # same supervisor/account locks still prevent a live instance being replaced.
    with supervisor_guard(config), supervisor_guard(config, engine=True):
        manifest = verify_backup(config, bundle, manifest_hash)
        create_private_directory(destination, config.service_sid)
        _write(
            destination / "restore-pending.json",
            canonical_bytes(
                {
                    "manifest_hash": manifest_hash,
                    "scope_hash": manifest.scope_hash,
                    "status": "RESTORE_REVIEW_REQUIRED",
                    "real_trading_enabled": False,
                }
            ),
        )
        for folder in ("critical", "artifacts", "daily-catalog", "jobs", "logs"):
            (destination / folder).mkdir()
        for item in manifest.files:
            _copy(bundle / item.path, destination / item.path, item)
        if (
            inspect_journal(destination / "critical/journal-v2.db", config.account_id)
            != manifest.journal
        ):
            raise ValueError("restored Journal proof differs")
        _write(destination / "node-scope.json", config.scope())
        _write(
            destination / "restore-verified.json",
            canonical_bytes(
                {
                    "manifest_hash": manifest_hash,
                    "verified_at": datetime.now(UTC).isoformat(),
                    "status": "CANDIDATE_VERIFIED",
                    "real_trading_enabled": False,
                }
            ),
        )
        return manifest
