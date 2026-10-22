"""One idempotent daily upload attempt, invoked by the Windows agent job.

Every attempt checks the remote receipt again, including after prior local
success. This repairs a backend restored to an earlier database backup. No
SQLite connection, trading permission or per-trade network outbox is involved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.protocol.v2.daily_sync import AppliedReceipt, DailyManifest
from auto_trading_system.sync.artifact_files import ArtifactFiles
from auto_trading_system.sync.daily_files import FrozenBatch


class DailyTransport(Protocol):
    def receipt(self, batch_id: str) -> AppliedReceipt | None: ...
    def create(self, manifest: DailyManifest) -> None: ...
    def missing_parts(self, batch_id: str) -> tuple[str, ...]: ...
    def put_part(self, batch_id: str, part_id: str, body: bytes) -> None: ...
    def complete(self, batch_id: str, manifest_hash: str) -> None: ...


class RetryableSyncError(RuntimeError):
    """Timeout, overload or transport failure; retry the same immutable batch."""


class PermanentSyncError(RuntimeError):
    """Identity/schema/content conflict; operator attention instead of blind retries."""


@dataclass(frozen=True, slots=True)
class UploadResult:
    status: str
    receipt_artifact_hash: str | None = None


def upload_once(batch: FrozenBatch, files: ArtifactFiles, remote: DailyTransport) -> UploadResult:
    manifest = batch.manifest
    try:
        receipt = remote.receipt(manifest.batch_id)
        if receipt is None:
            remote.create(manifest)
            descriptions = {part.part_id: part for part in manifest.parts}
            for part_id in remote.missing_parts(manifest.batch_id):
                if part_id not in descriptions:
                    raise PermanentSyncError("server requested an unknown part")
                part = descriptions[part_id]
                body = files.read(part.part_hash)
                if len(body) != part.uncompressed_bytes:
                    raise PermanentSyncError("local part byte count mismatch")
                remote.put_part(manifest.batch_id, part_id, body)
            remote.complete(manifest.batch_id, manifest.manifest_hash)
            receipt = remote.receipt(manifest.batch_id)
        if receipt is None:
            return UploadResult("PENDING")
        receipt.check_manifest(manifest)
        artifact_hash = files.put(canonical_bytes(receipt.model_dump(mode="json")))
        return UploadResult("SYNCED", artifact_hash)
    except RetryableSyncError:
        return UploadResult("PENDING")
    except (PermanentSyncError, ValueError):
        return UploadResult("NEEDS_ATTENTION")
    except OSError:
        # Local file failure is visible to the agent and never changes engine permission.
        return UploadResult("LOCAL_STORAGE_ERROR")
