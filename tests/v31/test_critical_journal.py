from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from time import monotonic

import apsw
import pytest

from auto_trading_system.journal.apsw_store import APSWCriticalStore
from auto_trading_system.journal.critical_models import (
    CommitEvidence,
    CriticalStoreError,
    Submission,
)
from auto_trading_system.journal.critical_schema import SCHEMA_SQL
from auto_trading_system.journal.critical_writer import CriticalJournalOwner, open_critical_journal


def test_single_j1_duplicate_conflict_recovery_and_backup(
    tmp_path: Path, submission: Submission
) -> None:
    store = APSWCriticalStore(tmp_path / "critical.db", submission.account_id)
    store.open()
    try:
        first = store.commit_submission(submission)
        assert first.fresh and first.record_seq == 1
        assert not store.commit_submission(submission).fresh
        with pytest.raises(CriticalStoreError, match="CONTENT_CONFLICT"):
            store.commit_submission(submission.model_copy(update={"quantity": 90}))
        assert store.read_submissions()[0].submission == submission
        assert store.read_submissions(1) == ()
        with pytest.raises(ValueError):
            store.read_submissions(limit=201)
        backup = tmp_path / "backup.db"
        assert store.backup(backup).startswith("sha256:")
        with pytest.raises(CriticalStoreError, match="TARGET_EXISTS"):
            store.backup(backup)
        assert len(store.checkpoint()) == 2
    finally:
        store.close()
    restored = APSWCriticalStore(backup, submission.account_id)
    restored.open()
    try:
        assert restored.read_submissions()[0].submission == submission
    finally:
        restored.close()


def test_store_rejects_foreign_db_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "user-data.db"
    connection = apsw.Connection(str(path))
    connection.execute("CREATE TABLE user_business (id INTEGER PRIMARY KEY)")
    connection.close()
    original = path.read_bytes()
    with pytest.raises(CriticalStoreError, match="FOREIGN_DATABASE"):
        APSWCriticalStore(path, "simulation-account").open()
    assert path.read_bytes() == original


def test_owner_and_account_are_enforced(tmp_path: Path, submission: Submission) -> None:
    store = APSWCriticalStore(tmp_path / "critical.db", submission.account_id)
    store.open()
    try:
        with (
            ThreadPoolExecutor(max_workers=1) as executor,
            pytest.raises(CriticalStoreError, match="WRONG_OWNER"),
        ):
            executor.submit(store.read_submissions).result()
        with pytest.raises(CriticalStoreError, match="ACCOUNT_MISMATCH"):
            store.commit_submission(submission.model_copy(update={"account_id": "wrong"}))
    finally:
        store.close()
    with pytest.raises(CriticalStoreError, match="ACCOUNT_MISMATCH"):
        APSWCriticalStore(tmp_path / "critical.db", "wrong").open()


def test_fact_idempotency_and_tightening_target(tmp_path: Path, submission: Submission) -> None:
    store = APSWCriticalStore(tmp_path / "critical.db", submission.account_id)
    store.open()
    try:
        assert store.append_evidence("broker_facts", "trade-1", b'{"quantity":100}')
        assert not store.append_evidence("broker_facts", "trade-1", b'{"quantity":100}')
        with pytest.raises(CriticalStoreError, match="FACT_CONTENT_CONFLICT"):
            store.append_evidence("broker_facts", "trade-1", b'{"quantity":200}')
        store.commit_submission(submission)
        widened = submission.model_copy(
            update={
                "client_order_id": "ord_r310000002",
                "target": submission.target.model_copy(update={"target_remaining": 200}),
            }
        )
        with pytest.raises(CriticalStoreError, match="TARGET_CONFLICT"):
            store.commit_submission(widened)
        assert len(store.read_submissions()) == 1
    finally:
        store.close()


def test_all_new_database_columns_have_comments() -> None:
    columns = [line for line in SCHEMA_SQL.splitlines() if line.startswith("    ")]
    assert len(columns) == 27
    assert all(" -- " in column and len(column.split(" -- ")[1]) > 5 for column in columns)
    for forbidden in ("CREATE TABLE outbox", "CREATE TABLE trade_plans", "CREATE TABLE ticks"):
        assert forbidden not in SCHEMA_SQL


def test_lock_wait_latches_failure_and_never_changes_existing_records(
    tmp_path: Path,
    submission: Submission,
) -> None:
    path = tmp_path / "critical.db"
    owner = open_critical_journal(path, submission.account_id)
    lock = apsw.Connection(str(path))
    lock.execute("BEGIN IMMEDIATE")
    try:
        future = owner.commit(submission, deadline=monotonic() + 1)
        with pytest.raises(apsw.BusyError):
            future.result(timeout=2)
        # The consumer must also fence a ready exception; no retry uses a new ID.
        assert future.done()
    finally:
        lock.execute("ROLLBACK")
        lock.close()
        assert owner.close()


def test_late_commit_remains_evidence_and_close_is_bounded(
    tmp_path: Path,
    submission: Submission,
) -> None:
    entered, release = Event(), Event()

    class SlowStore(APSWCriticalStore):
        def commit_submission(self, item: Submission) -> CommitEvidence:
            entered.set()
            assert release.wait(3)
            return super().commit_submission(item)

    owner = CriticalJournalOwner(lambda: SlowStore(tmp_path / "critical.db", submission.account_id))
    owner.start()
    future = owner.commit(submission, deadline=monotonic() + 0.25)
    assert entered.wait(1)
    assert not owner.close(timeout=0.01)
    release.set()
    assert future.result(timeout=2).fresh
    assert owner.close()


def test_queue_overflow_expiry_and_lifecycle(tmp_path: Path, submission: Submission) -> None:
    entered, release = Event(), Event()
    owner = CriticalJournalOwner(
        lambda: APSWCriticalStore(tmp_path / "critical.db", submission.account_id),
        capacity=1,
    )
    owner.start()
    with pytest.raises(CriticalStoreError, match="ALREADY_STARTED"):
        owner.start()

    def wait_for_release(store: object) -> bool:
        entered.set()
        return release.wait(3)

    try:
        running = owner.request(wait_for_release, deadline=monotonic() + 1)
        assert entered.wait(1)
        queued = owner.commit(submission, deadline=monotonic() + 1)
        rejected = owner.commit(submission, deadline=monotonic() + 1)
        with pytest.raises(CriticalStoreError, match="QUEUE_FULL"):
            rejected.result()
        assert not owner.healthy
        release.set()
        assert running.result(timeout=1)
        with pytest.raises(CriticalStoreError, match="UNAVAILABLE"):
            queued.result(timeout=1)
    finally:
        release.set()
        assert owner.close()


def test_expired_mailbox_work_does_not_write(tmp_path: Path, submission: Submission) -> None:
    owner = open_critical_journal(tmp_path / "critical.db", submission.account_id)
    try:
        with pytest.raises(CriticalStoreError, match="EXPIRED"):
            owner.commit(submission, deadline=monotonic() - 1).result(timeout=1)
        assert owner.recovery_page().result(timeout=1) == ()
    finally:
        assert owner.close()
