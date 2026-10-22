"""Bounded in-memory V2 receiver for offline transport/recovery tests only.

Not a production MySQL replacement. Production endpoint authentication, durable
staging, financial projection and resource budgets are separate work packages.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import RLock

from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest, parse_bytes
from auto_trading_system.protocol.v2.daily_sync import (
    AppliedReceipt,
    DailyManifest,
    DailyRecord,
    Dataset,
)
from auto_trading_system.protocol.v2.records import validate_record
from auto_trading_system.sync.daily_upload import PermanentSyncError


@dataclass
class _Batch:
    manifest: DailyManifest
    parts: dict[str, bytes] = field(default_factory=dict)
    receipt: AppliedReceipt | None = None


class InMemoryDailyReceiver:
    def __init__(
        self, *, capacity: int = 32, before_apply: Callable[[], None] = lambda: None
    ) -> None:
        self._lock = RLock()
        self._capacity = capacity
        self._before_apply = before_apply
        self._batches: dict[str, _Batch] = {}
        self._current: dict[tuple[str, str], str] = {}

    def receipt(self, batch_id: str) -> AppliedReceipt | None:
        with self._lock:
            batch = self._batches.get(batch_id)
            return batch.receipt if batch else None

    def create(self, manifest: DailyManifest) -> None:
        with self._lock:
            old = self._batches.get(manifest.batch_id)
            if old:
                if old.manifest.manifest_hash != manifest.manifest_hash:
                    raise PermanentSyncError("batch id conflicts with existing content")
                return
            if len(self._batches) >= self._capacity:
                raise PermanentSyncError("simulation receiver capacity reached")
            if any(
                item.manifest.account_id == manifest.account_id
                and item.manifest.trade_date == manifest.trade_date
                and item.manifest.revision == manifest.revision
                for item in self._batches.values()
            ):
                raise PermanentSyncError("day revision already has a different batch")
            self._batches[manifest.batch_id] = _Batch(manifest)

    def missing_parts(self, batch_id: str) -> tuple[str, ...]:
        with self._lock:
            batch = self._batches[batch_id]
            return tuple(p.part_id for p in batch.manifest.parts if p.part_id not in batch.parts)

    def put_part(self, batch_id: str, part_id: str, body: bytes) -> None:
        with self._lock:
            batch = self._batches[batch_id]
            descriptions = {part.part_id: part for part in batch.manifest.parts}
            description = descriptions.get(part_id)
            if description is None:
                raise PermanentSyncError("unknown part")
            if len(body) != description.uncompressed_bytes or digest(body) != description.part_hash:
                raise PermanentSyncError("part size/hash mismatch")
            records = parse_bytes(body)
            if not isinstance(records, list) or len(records) != description.record_count:
                raise PermanentSyncError("part record count mismatch")
            for record in records:
                DailyRecord.model_validate_json(canonical_bytes(record))
            old = batch.parts.get(part_id)
            if old is not None and old != body:
                raise PermanentSyncError("part id content conflict")
            batch.parts[part_id] = body

    def complete(self, batch_id: str, manifest_hash: str) -> None:
        with self._lock:
            batch = self._batches[batch_id]
            manifest = batch.manifest
            if manifest.manifest_hash != manifest_hash:
                raise PermanentSyncError("manifest mismatch")
            if batch.receipt is not None:
                return
            if self.missing_parts(batch_id):
                raise PermanentSyncError("parts are missing")
            seen: set[tuple[str, str]] = set()
            for part in manifest.parts:
                records = parse_bytes(batch.parts[part.part_id])
                assert isinstance(records, list)
                for raw in records:
                    item = DailyRecord.model_validate_json(canonical_bytes(raw))
                    key = (part.dataset, item.record_id)
                    if key in seen:
                        raise PermanentSyncError("duplicate business record across parts")
                    seen.add(key)
            day = (manifest.account_id, manifest.trade_date.isoformat())
            previous_id = self._current.get(day)
            if previous_id != manifest.supersedes_batch_id:
                raise PermanentSyncError("correction does not supersede current day revision")
            if (
                previous_id is not None
                and manifest.revision != self._batches[previous_id].manifest.revision + 1
            ):
                raise PermanentSyncError("day revisions must advance by one")
            # Fault injection here proves readers continue to see the previous
            # complete revision. Production implements this boundary in MySQL.
            self._before_apply()
            batch.receipt = AppliedReceipt(
                batch_id=batch_id,
                account_id=manifest.account_id,
                trade_date=manifest.trade_date,
                revision=manifest.revision,
                manifest_hash=manifest.manifest_hash,
                business_status="COMPLETE"
                if all(item.status == "COMPLETE" for item in manifest.dataset_status)
                else "INCOMPLETE",
                financial_status="PROVISIONAL",
                applied_at=datetime.now(UTC),
            )
            self._current[day] = batch_id

    def current(self, account_id: str, trade_date: str) -> AppliedReceipt | None:
        with self._lock:
            batch_id = self._current.get((account_id, trade_date))
            return self.receipt(batch_id) if batch_id else None

    def records(
        self, account_id: str, trade_date: str, dataset: Dataset
    ) -> tuple[DailyRecord, ...]:
        """Read only the applied revision; pending fragments stay invisible.

        This test-only query verifies uploaded business facts rather than merely
        checking a receipt. Production queries belong to the quant application.
        """
        with self._lock:
            batch_id = self._current.get((account_id, trade_date))
            if batch_id is None:
                return ()
            batch = self._batches[batch_id]
            result: list[DailyRecord] = []
            for part in batch.manifest.parts:
                if part.dataset == dataset:
                    values = parse_bytes(batch.parts[part.part_id])
                    assert isinstance(values, list)
                    for value in values:
                        record = DailyRecord.model_validate_json(canonical_bytes(value))
                        validate_record(
                            dataset,
                            record.data,
                            account_id=account_id,
                            trade_date=batch.manifest.trade_date,
                        )
                        result.append(record)
            return tuple(result)
