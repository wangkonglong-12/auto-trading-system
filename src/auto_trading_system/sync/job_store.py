"""Agent-owned file job index, separate from engine critical SQLite storage.

One account/node agent holds an OS mutex before reading or changing this index.
An index write uses fsync plus same-volume atomic replacement. Immutable plan,
batch and receipt bodies remain content-addressed in ArtifactFiles.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field

from auto_trading_system.protocol.v2.base import Sha256, WireId, WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest, parse_bytes
from auto_trading_system.runtime.single_instance import InstanceLockBackend


class SyncJob(WireModel):
    job_id: WireId
    kind: Literal["PLAN_RECEIPT", "DAILY"]
    artifact_hash: Sha256
    state: Literal["PENDING", "SYNCED", "NEEDS_ATTENTION", "LOCAL_STORAGE_ERROR"] = "PENDING"
    attempts: int = Field(default=0, ge=0)
    updated_at: AwareDatetime
    receipt_hash: Sha256 | None = None
    reason_code: WireId = "NEW"


class RetryCursor(WireModel):
    after_job_id: WireId = Field(description="上次完成本地结果落盘的任务ID；下轮从后继任务开始")


_FILENAME = re.compile(r"^[a-f0-9]{64}\.json$")


class JobStore:
    def __init__(
        self,
        root: Path,
        *,
        node_id: str,
        account_id: str,
        backend: InstanceLockBackend,
        maximum_jobs: int = 4000,
    ) -> None:
        if not 1 <= maximum_jobs <= 10000:
            raise ValueError("invalid agent index capacity")
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._backend = backend
        self._scope = canonical_bytes({"node_id": node_id, "account_id": account_id, "version": 2})
        scope = digest(self._scope)[7:]
        self._mutex = "Global\\AutoTrading.Agent." + scope
        self._token: object | None = None
        self._capacity = maximum_jobs

    def __enter__(self) -> JobStore:
        if self._token is not None:
            raise RuntimeError("agent lease is already held")
        self._token = self._backend.acquire(self._mutex, account_fingerprint=self._mutex[-12:])
        if self._token is None:
            raise RuntimeError("agent lock backend returned no token")
        try:
            path = self.root / ".agent-scope.json"
            if path.is_symlink():
                raise ValueError("agent scope symlinks are not allowed")
            try:
                with path.open("xb") as output:
                    output.write(self._scope)
                    output.flush()
                    os.fsync(output.fileno())
            except FileExistsError:
                with path.open("rb") as source:
                    if source.read(513) != self._scope:
                        raise ValueError(
                            "agent directory belongs to a different node or account"
                        ) from None
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        token, self._token = self._token, None
        if token is not None:
            self._backend.release(token)

    def require_lease(self) -> None:
        if self._token is None:
            raise RuntimeError("agent index requires exclusive process ownership")

    def _path(self, job_id: str) -> Path:
        path = self.root / (digest(job_id.encode())[7:] + ".json")
        if path.is_symlink():
            raise ValueError("agent job symlinks are not allowed")
        return path

    def _read(self, path: Path) -> SyncJob:
        if path.is_symlink():
            raise ValueError("agent job symlinks are not allowed")
        with path.open("rb") as source:
            value = parse_bytes(source.read(32769), max_bytes=32768)
        if not isinstance(value, dict) or set(value) != {"body", "hash"}:
            raise ValueError("invalid agent job envelope")
        body = canonical_bytes(value["body"])
        if digest(body) != value["hash"]:
            raise ValueError("agent job checksum mismatch")
        job = SyncJob.model_validate_json(body)
        if self._path(job.job_id) != path:
            raise ValueError("agent job filename identity mismatch")
        return job

    def list(self) -> tuple[SyncJob, ...]:
        self.require_lease()
        with os.scandir(self.root) as entries:
            paths = list(
                islice(
                    (Path(item.path) for item in entries if _FILENAME.fullmatch(item.name)),
                    self._capacity + 1,
                )
            )
        if len(paths) > self._capacity:
            raise ValueError("agent job index exceeds capacity; retention review required")
        return tuple(sorted((self._read(path) for path in paths), key=lambda job: job.job_id))

    def register(self, job: SyncJob) -> SyncJob:
        self.require_lease()
        path = self._path(job.job_id)
        if path.exists():
            prior = self._read(path)
            if (prior.kind, prior.artifact_hash) != (job.kind, job.artifact_hash):
                raise ValueError("job id conflicts with immutable artifact")
            return prior
        if len(self.list()) >= self._capacity:
            raise ValueError("agent job index is full; preserve pending data")
        if job.state != "PENDING" or job.attempts != 0:
            raise ValueError("new jobs must start pending without attempts")
        self._write(job)
        return job

    def get(self, job_id: str) -> SyncJob | None:
        """Read a predecessor under the same account lease as the retry worker."""
        self.require_lease()
        try:
            return self._read(self._path(job_id))
        except FileNotFoundError:
            return None

    def retry_cursor(self, lane: Literal["all", "pending", "synced"] = "all") -> str | None:
        """Persisted round-robin order is independent of clock adjustments."""
        self.require_lease()
        path = self.root / f".retry-{lane}-cursor.json"
        if path.is_symlink():
            raise ValueError("retry cursor links are invalid")
        try:
            with path.open("rb") as source:
                value = parse_bytes(source.read(4097), max_bytes=4096)
        except FileNotFoundError:
            return None
        if not isinstance(value, dict) or set(value) != {"body", "hash"}:
            raise ValueError("invalid retry cursor envelope")
        body = canonical_bytes(value["body"])
        if digest(body) != value["hash"]:
            raise ValueError("retry cursor checksum mismatch")
        return RetryCursor.model_validate_json(body).after_job_id

    def advance_retry_cursor(
        self, job_id: str, lane: Literal["all", "pending", "synced"] = "all"
    ) -> None:
        self.require_lease()
        body = RetryCursor(after_job_id=job_id).model_dump(mode="json")
        self._replace(
            self.root / f".retry-{lane}-cursor.json",
            canonical_bytes({"body": body, "hash": digest(canonical_bytes(body))}),
        )

    def result(
        self,
        job: SyncJob,
        *,
        state: Literal["PENDING", "SYNCED", "NEEDS_ATTENTION", "LOCAL_STORAGE_ERROR"],
        now: datetime,
        reason: str,
        receipt_hash: str | None = None,
    ) -> SyncJob:
        self.require_lease()
        prior = self._read(self._path(job.job_id))
        if prior != job:
            raise ValueError("stale agent job version")
        if state == "SYNCED" and receipt_hash is None:
            raise ValueError("successful sync requires a verified receipt artifact")
        values = job.model_dump(mode="json")
        values.update(
            state=state,
            attempts=job.attempts + 1,
            updated_at=now.isoformat(),
            receipt_hash=receipt_hash,
            reason_code=reason,
        )
        updated = SyncJob.model_validate_json(canonical_bytes(values))
        self._write(updated)
        return updated

    def _write(self, job: SyncJob) -> None:
        body = job.model_dump(mode="json")
        data = canonical_bytes({"body": body, "hash": digest(canonical_bytes(body))})
        self._replace(self._path(job.job_id), data)

    def _replace(self, path: Path, data: bytes) -> None:
        if path.is_symlink():
            raise ValueError("agent index links are invalid")
        temporary = self.root / (".pending-" + uuid4().hex)
        try:
            with temporary.open("xb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            # Remove only this call's exact scratch file; retained job records
            # and immutable artifacts are never pruned by a synchronization job.
            temporary.unlink(missing_ok=True)
