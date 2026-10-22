"""Daily correction durability and bounded retry tests; no real account/network."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from pydantic import JsonValue
from test_agent_jobs import NOW, Remote, system
from test_v2_files_sync import sample_batch

from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest, parse_bytes
from auto_trading_system.protocol.v2.daily_sync import DailyManifest
from auto_trading_system.sync.artifact_files import ArtifactFiles
from auto_trading_system.sync.daily_catalog import DailyCatalog, closing_slot
from auto_trading_system.sync.daily_files import FrozenBatch, load_daily
from auto_trading_system.sync.daily_upload import RetryableSyncError


def catalog_at(root: Path, files: ArtifactFiles) -> DailyCatalog:
    root.mkdir(exist_ok=True)
    return DailyCatalog(
        root, files, node_id="simulation-node", account_id="simulation-account", generation=1
    )


def altered(files: ArtifactFiles, batch: FrozenBatch, **updates: Any) -> FrozenBatch:
    value = batch.manifest.model_dump(mode="json", exclude={"manifest_hash"})
    value.update(updates)
    value["manifest_hash"] = digest(canonical_bytes(value))
    manifest = DailyManifest.model_validate_json(canonical_bytes(value))
    return FrozenBatch(manifest, files.put(canonical_bytes(manifest.model_dump(mode="json"))))


def test_append_chain_discovery_and_exact_republish(tmp_path: Path) -> None:
    files = ArtifactFiles(tmp_path / "artifacts")
    catalog = catalog_at(tmp_path / "catalog", files)
    batches = tuple(sample_batch(files, revision=i, count=i) for i in range(1, 4))
    assert catalog.get(NOW.date()) is None
    for batch in batches:
        assert catalog.publish(batch) == batch
    assert catalog.get(NOW.date()) == batches[-1]
    assert tuple(catalog.discover()) == batches
    for batch in reversed(batches):
        assert catalog.publish(batch) == batch
    assert {p.name for p in catalog.root.iterdir()} == {
        "2026-09-07.json",
        "2026-09-07.r0002.json",
        "2026-09-07.r0003.json",
    }
    assert tuple(catalog_at(catalog.root, files).discover()) == batches


@pytest.mark.parametrize(
    "field,value",
    [
        ("supersedes_batch_id", "foreign-batch"),
        ("revision", 3),
        ("journal_generation", "other-journal"),
        ("last_record_seq", 0),
        ("node_generation", 2),
        ("node_id", "other-node"),
        ("data_as_of", "2026-09-07T15:29:00+08:00"),
    ],
)
def test_invalid_successor_preserves_old_reference(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    files = ArtifactFiles(tmp_path / "artifacts")
    catalog = catalog_at(tmp_path / "catalog", files)
    first = catalog.publish(sample_batch(files, count=0))
    second = altered(files, sample_batch(files, revision=2, count=0), **{field: value})
    with pytest.raises(ValueError):
        catalog.publish(second)
    assert catalog.get(NOW.date()) == first
    assert tuple(catalog.discover()) == (first,)


def test_missing_base_gap_and_changed_revision_are_not_overwritten(tmp_path: Path) -> None:
    files = ArtifactFiles(tmp_path / "artifacts")
    catalog = catalog_at(tmp_path / "catalog", files)
    second = sample_batch(files, revision=2, count=1)
    with pytest.raises(ValueError, match="predecessor"):
        catalog.publish(second)
    path = catalog.root / "2026-09-07.r0002.json"
    path.write_bytes(canonical_bytes({"artifact_hash": second.artifact_hash}))
    with pytest.raises(ValueError, match="first revision"):
        catalog.get(NOW.date())
    first = sample_batch(files, count=1)
    (catalog.root / "2026-09-07.json").write_bytes(
        canonical_bytes({"artifact_hash": first.artifact_hash})
    )
    with pytest.raises(ValueError, match="different frozen"):
        catalog.publish(sample_batch(files, revision=2, count=2))
    assert catalog.get(NOW.date()) == second


@pytest.mark.parametrize("published", [False, True])
def test_link_failure_before_or_after_commit_is_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    published: bool,
) -> None:
    files = ArtifactFiles(tmp_path / "artifacts")
    catalog = catalog_at(tmp_path / "catalog", files)
    first = catalog.publish(sample_batch(files, count=1))
    second = sample_batch(files, revision=2, count=2)
    original = os.link

    def fail(source: Path, target: Path) -> None:
        if published:
            original(source, target)
        raise OSError("injected publication interruption")

    with monkeypatch.context() as patch:
        patch.setattr(os, "link", fail)
        with pytest.raises(OSError):
            catalog.publish(second)
    assert catalog.get(NOW.date()) == (second if published else first)
    assert not tuple(catalog.root.glob(".pending-*"))
    assert catalog.publish(second) == second
    assert tuple(catalog.discover()) == (first, second)


@pytest.mark.parametrize("conflicting", [False, True])
def test_concurrent_publication_is_identical_or_explicit_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    conflicting: bool,
) -> None:
    files = ArtifactFiles(tmp_path / "artifacts")
    catalog = catalog_at(tmp_path / "catalog", files)
    catalog.publish(sample_batch(files, count=0))
    first = sample_batch(files, revision=2, count=1)
    second = sample_batch(files, revision=2, count=2) if conflicting else first
    barrier, original = Barrier(2), os.link

    def simultaneous(source: Path, target: Path) -> None:
        barrier.wait(timeout=5)
        original(source, target)

    monkeypatch.setattr(os, "link", simultaneous)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(catalog.publish, b) for b in (first, second)]
    failures = [future.exception() for future in results if future.exception()]
    assert len(failures) == int(conflicting)
    assert all(isinstance(error, ValueError) for error in failures)
    assert catalog.get(NOW.date()) in (first, second)


@pytest.mark.parametrize("filename", ["2026-09-07.r0001.json", "2026-09-07.r0033.json"])
def test_invalid_revision_filename_fails_closed(tmp_path: Path, filename: str) -> None:
    files = ArtifactFiles(tmp_path / "artifacts")
    catalog = catalog_at(tmp_path / "catalog", files)
    (catalog.root / filename).write_bytes(b"{}")
    with pytest.raises(ValueError, match="reference revision"):
        tuple(catalog.discover())


def test_full_part_validation_detects_duplicate_and_invalid_payload(tmp_path: Path) -> None:
    files = ArtifactFiles(tmp_path / "artifacts")
    batch = sample_batch(files, count=201)
    first, second = batch.manifest.parts
    values = parse_bytes(files.read(first.part_hash))
    assert isinstance(values, list)
    replacements: tuple[JsonValue, ...] = (
        [values[0]],
        [{"record_id": "other", "data": {}}],
        [],
    )
    for replacement in replacements:
        body = canonical_bytes(replacement)
        description = second.model_dump(mode="json")
        description.update(part_hash=files.put(body), uncompressed_bytes=len(body))
        invalid = altered(files, batch, parts=[first.model_dump(mode="json"), description])
        with pytest.raises(ValueError):
            load_daily(files, invalid.artifact_hash)


def test_long_chain_progress_with_budget_one_and_remote_restore(tmp_path: Path) -> None:
    agent, _, remote, _ = system(tmp_path)
    catalog = catalog_at(tmp_path / "catalog", agent.files)
    for revision in range(1, 13):
        catalog.publish(sample_batch(agent.files, revision=revision, count=1))
    with agent.store:
        # Registration order is intentionally reversed. Retries must neither
        # recurse forever nor assume hash-based batch IDs sort by revision.
        for batch in reversed(tuple(catalog.discover())):
            agent.register_daily(batch)
        for receiver in (remote, Remote(remote.fixed)):
            agent.remote = receiver
            for _ in range(160):
                results = agent.retry_retained(maximum_jobs=1)
                assert len(results) == 1
                assert results[0].state in ("PENDING", "SYNCED")
                if receiver.receipt("batch-12"):
                    break
            assert receiver.receipt("batch-12") is not None
            current = receiver.current(agent.account_id, NOW.date().isoformat())
            assert current is not None and current.revision == 12


def test_local_io_failure_retries_without_resetting_immutable_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, _, _, _ = system(tmp_path)
    with agent.store:
        original = agent.register_daily(sample_batch(agent.files, count=1))
        read = agent.files.read

        def fail(content_hash: str, *, maximum_bytes: int = 512 * 1024) -> bytes:
            raise OSError("temporary unavailable disk")

        monkeypatch.setattr(agent.files, "read", fail)
        failed = agent.retry_retained()[0]
        assert failed.state == "LOCAL_STORAGE_ERROR"
        monkeypatch.setattr(agent.files, "read", read)
        recovered = agent.retry_retained()[0]
        assert recovered.state == "SYNCED" and recovered.attempts == 2
        assert recovered.artifact_hash == original.artifact_hash


def test_lost_receipt_and_pending_predecessor_keep_original_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, _, remote, _ = system(tmp_path)
    with agent.store:
        first = agent.register_daily(sample_batch(agent.files, count=1))
        second = agent.register_daily(sample_batch(agent.files, revision=2, count=2))
        pending = agent.run_job(second)
        assert pending.reason_code == "PREDECESSOR_PENDING"
        assert remote.receipt(second.job_id) is None
        complete = remote.complete

        def lost(batch_id: str, manifest_hash: str) -> None:
            complete(batch_id, manifest_hash)
            raise RetryableSyncError("ack lost after APPLIED")

        monkeypatch.setattr(remote, "complete", lost)
        assert agent.run_job(first).state == "PENDING"
        monkeypatch.setattr(remote, "complete", complete)
        assert agent.run_job(pending).state == "SYNCED"
        assert [job.state for job in agent.retry_retained()] == ["SYNCED", "SYNCED"]


@pytest.mark.parametrize("state", ["missing", "NEEDS_ATTENTION", "mismatch"])
def test_bad_predecessor_does_not_upload_child(tmp_path: Path, state: str) -> None:
    agent, _, remote, _ = system(tmp_path)
    with agent.store:
        if state != "missing":
            first = agent.register_daily(sample_batch(agent.files, count=0))
            if state == "NEEDS_ATTENTION":
                agent.store.result(first, state="NEEDS_ATTENTION", now=NOW, reason="CONFLICT")
        batch = sample_batch(agent.files, revision=2, count=0)
        if state == "mismatch":
            batch = altered(agent.files, batch, journal_generation="foreign")
        child = agent.register_daily(batch)
        result = agent.run_job(child)
        assert result.state == ("PENDING" if state == "NEEDS_ATTENTION" else "NEEDS_ATTENTION")
        assert remote.receipt(child.job_id) is None


@pytest.mark.parametrize("minute", [30, 31, 44, 45, 59])
def test_closing_slot_floors_fifteen_minutes(minute: int) -> None:
    instant = NOW.replace(hour=15, minute=minute)
    assert closing_slot(instant) == instant.replace(minute=minute // 15 * 15, second=0)
    assert closing_slot(instant.astimezone()) == closing_slot(instant)


def test_closing_slot_boundaries() -> None:
    assert closing_slot(NOW.replace(hour=18, minute=0, second=45)).minute == 0
    for instant in (
        NOW.replace(tzinfo=None),
        NOW,
        NOW.replace(hour=18, minute=1),
        NOW.replace(hour=15, minute=30) - timedelta(microseconds=1),
    ):
        with pytest.raises(ValueError):
            closing_slot(instant)


def test_retry_cursor_survives_restart_clock_rollback_and_failed_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, _, _, _ = system(tmp_path)
    with agent.store:
        for revision in range(1, 4):
            agent.register_daily(sample_batch(agent.files, revision=revision, count=0))
        assert agent.retry_retained(maximum_jobs=1)[0].job_id == "batch-1"
    agent.clock = lambda: NOW - timedelta(days=3)
    with agent.store:
        assert agent.retry_retained(maximum_jobs=1)[0].job_id == "batch-2"
        with monkeypatch.context() as patch:

            def fail(job_id: str, lane: str = "all") -> None:
                raise OSError("cursor write failed")

            patch.setattr(agent.store, "advance_retry_cursor", fail)
            with pytest.raises(OSError, match="cursor write failed"):
                agent.retry_retained(maximum_jobs=1)
        assert agent.store.retry_cursor() == "batch-2"
        assert agent.retry_retained(maximum_jobs=1)[0].job_id == "batch-3"
        assert agent.retry_retained(maximum_jobs=1)[0].job_id == "batch-1"
        (agent.store.root / ".retry-all-cursor.json").write_bytes(b"{}")
        with pytest.raises(ValueError, match="cursor envelope"):
            agent.retry_retained(maximum_jobs=1)


def test_pending_upload_has_priority_and_history_keeps_reserved_audit_slot(tmp_path: Path) -> None:
    agent, _, _, _ = system(tmp_path)
    with agent.store:
        for revision in range(1, 10):
            job = agent.register_daily(sample_batch(agent.files, revision=revision, count=0))
            assert agent.run_job(job).state == "SYNCED"
        agent.register_daily(sample_batch(agent.files, revision=10, count=1))
        results = agent.retry_retained(maximum_jobs=2)
        assert results[0].job_id == "batch-10" and results[0].state == "SYNCED"
        assert results[1].job_id != "batch-10"
