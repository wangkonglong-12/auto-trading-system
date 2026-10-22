"""Freeze daily datasets into repeatable, bounded parts and immutable files."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from pydantic import AwareDatetime, Field, JsonValue

from auto_trading_system.protocol.v2.base import WireId, WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest, parse_bytes
from auto_trading_system.protocol.v2.daily_sync import (
    DATASETS,
    DailyManifest,
    DailyRecord,
    Dataset,
    DatasetStatus,
    PartDescription,
)
from auto_trading_system.protocol.v2.records import validate_record
from auto_trading_system.sync.artifact_files import ArtifactFiles


class DailyIdentity(WireModel):
    batch_id: WireId
    node_id: WireId
    node_generation: int = Field(ge=1)
    account_id: WireId
    trade_date: date
    revision: int = Field(ge=1)
    supersedes_batch_id: WireId | None = None
    generated_at: AwareDatetime
    data_as_of: AwareDatetime
    journal_generation: WireId
    last_record_seq: int = Field(ge=0)


@dataclass(frozen=True, slots=True)
class FrozenBatch:
    manifest: DailyManifest
    artifact_hash: str


def freeze_daily(
    identity: DailyIdentity,
    records: Mapping[Dataset, tuple[DailyRecord, ...]],
    statuses: tuple[DatasetStatus, ...],
    files: ArtifactFiles,
) -> FrozenBatch:
    """Input is a fixed observation, never a live iterator over changing state."""
    if set(records) != set(DATASETS):
        raise ValueError("all datasets must be supplied, including empty/unavailable ones")
    parts: list[PartDescription] = []
    for dataset in DATASETS:
        items = records[dataset]
        if len({item.record_id for item in items}) != len(items):
            raise ValueError("record ids must be unique within a dataset")
        pending: list[JsonValue] = []
        part_number = 0

        def publish(pending: list[JsonValue] = pending, dataset: Dataset = dataset) -> None:
            nonlocal part_number
            if not pending:
                return
            body = canonical_bytes(pending)
            part_number += 1
            parts.append(
                PartDescription(
                    part_id=f"{dataset}-{part_number:04d}",
                    dataset=dataset,
                    record_count=len(pending),
                    uncompressed_bytes=len(body),
                    part_hash=files.put(body),
                )
            )
            if len(parts) > 2048:
                raise ValueError("daily revision exceeds part count limit")
            pending.clear()

        for item in items:
            record = validate_record(
                dataset, item.data, account_id=identity.account_id, trade_date=identity.trade_date
            )
            if record.observed_at > identity.data_as_of:
                raise ValueError("record observation is after the frozen daily cutoff")
            value: JsonValue = item.model_dump(mode="json")
            if len(canonical_bytes([value])) > 512 * 1024:
                raise ValueError("one record exceeds the maximum part size")
            if len(pending) == 200 or len(canonical_bytes([*pending, value])) > 512 * 1024:
                publish()
            pending.append(value)
        publish()
    payload = identity.model_dump(mode="json")
    payload.update(
        {
            "schema_major": 2,
            "parts": [part.model_dump(mode="json") for part in parts],
            "dataset_status": [status.model_dump(mode="json") for status in statuses],
        }
    )
    payload["manifest_hash"] = digest(canonical_bytes(payload))
    manifest = DailyManifest.model_validate_json(canonical_bytes(payload))
    if sum(part.uncompressed_bytes for part in manifest.parts) > 16 * 1024 * 1024:
        raise ValueError("daily batch exceeds negotiated 16 MiB budget")
    return FrozenBatch(manifest, files.put(canonical_bytes(manifest.model_dump(mode="json"))))


def load_daily(files: ArtifactFiles, artifact_hash: str) -> FrozenBatch:
    manifest = DailyManifest.model_validate_json(files.read(artifact_hash))
    if sum(part.uncompressed_bytes for part in manifest.parts) > 16 * 1024 * 1024:
        raise ValueError("daily batch exceeds negotiated 16 MiB budget")
    seen: set[tuple[Dataset, str]] = set()
    for part in manifest.parts:
        data = files.read(part.part_hash)
        if len(data) != part.uncompressed_bytes:
            raise ValueError("daily part byte count mismatch")
        values = parse_bytes(data)
        if not isinstance(values, list) or len(values) != part.record_count:
            raise ValueError("daily part record count mismatch")
        for value in values:
            item = DailyRecord.model_validate_json(canonical_bytes(value))
            key = (part.dataset, item.record_id)
            if key in seen:
                raise ValueError("duplicate daily record across parts")
            seen.add(key)
            record = validate_record(
                part.dataset,
                item.data,
                account_id=manifest.account_id,
                trade_date=manifest.trade_date,
            )
            if record.observed_at > manifest.data_as_of:
                raise ValueError("daily record is after the frozen cutoff")
    return FrozenBatch(manifest, artifact_hash)


def check_daily_successor(prior: DailyManifest, current: DailyManifest) -> None:
    """Bind a full replacement to one exact predecessor, with no evidence rewind.

    Journal generation changes require a separate maintenance workflow; a sync
    worker cannot splice another account, node, day or Journal chain.
    """
    scope = ("node_id", "node_generation", "account_id", "trade_date", "journal_generation")
    if (
        any(getattr(prior, key) != getattr(current, key) for key in scope)
        or current.revision != prior.revision + 1
        or current.supersedes_batch_id != prior.batch_id
        or current.data_as_of < prior.data_as_of
        or current.generated_at < prior.generated_at
        or current.last_record_seq < prior.last_record_seq
    ):
        raise ValueError("daily correction predecessor or evidence watermark differs")
