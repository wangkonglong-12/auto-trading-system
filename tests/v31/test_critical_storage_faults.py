"""Real SQLite/process fault checks, restricted to pytest-owned temporary files.

These are process-crash and logical-capacity tests. They do not simulate loss of
power, controller caches, damaged hardware, or the target QMT machine's antivirus.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from time import monotonic

import apsw
import pytest

from auto_trading_system.journal.apsw_store import APSWCriticalStore
from auto_trading_system.journal.critical_models import CriticalStoreError, Submission
from auto_trading_system.journal.critical_writer import CriticalJournalOwner
from auto_trading_system.protocol.v2.canonical import canonical_bytes

# The child exits itself; no global process lookup or kill command is involved.
# Exiting in the commit hook interrupts a transaction before successful COMMIT.
_CRASH_CHILD = """
import os
import sys
from pathlib import Path
from auto_trading_system.journal.apsw_store import APSWCriticalStore
from auto_trading_system.journal.critical_models import Submission

item = Submission.model_validate_json(sys.stdin.read())
store = APSWCriticalStore(Path(sys.argv[1]), item.account_id)
store.open()
if sys.argv[2] == 'before_commit':
    store._db().set_commit_hook(lambda: os._exit(74))
store.commit_submission(item)
os._exit(73)
"""


@pytest.mark.parametrize("crash_point", ["before_commit", "after_commit"])
def test_process_exit_never_leaves_half_a_j1(
    tmp_path: Path, submission: Submission, crash_point: str
) -> None:
    path = tmp_path / "crash-test.db"
    initial = APSWCriticalStore(path, submission.account_id)
    initial.open()
    initial.close()
    result = subprocess.run(
        [sys.executable, "-c", _CRASH_CHILD, str(path), crash_point],
        input=submission.model_dump_json(),
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == (74 if crash_point == "before_commit" else 73), result.stderr

    recovered = APSWCriticalStore(path, submission.account_id)
    recovered.open()
    try:
        records = recovered.read_submissions()
        targets = recovered._db().execute("SELECT COUNT(*) FROM action_targets").fetchone()
        expected = 0 if crash_point == "before_commit" else 1
        assert len(records) == expected
        assert targets == (expected,)
        if records:
            assert records[0].submission == submission
            # Durable recovery evidence never becomes permission to resubmit.
            assert not recovered.commit_submission(submission).fresh
    finally:
        recovered.close()


def test_long_reader_defers_checkpoint_without_blocking_j1(
    tmp_path: Path, submission: Submission
) -> None:
    path = tmp_path / "reader-test.db"
    store = APSWCriticalStore(path, submission.account_id)
    store.open()
    reader = apsw.Connection(str(path), flags=apsw.SQLITE_OPEN_READONLY)
    try:
        store.commit_submission(submission)
        reader.execute("BEGIN")
        assert reader.execute("SELECT COUNT(*) FROM submission_records").fetchone() == (1,)
        second = submission.model_copy(update={"client_order_id": "ord_second0001"})
        assert store.commit_submission(second).fresh
        assert reader.execute("SELECT COUNT(*) FROM submission_records").fetchone() == (1,)
        log_pages, completed_pages = store.checkpoint()
        assert log_pages > completed_pages >= 0
        reader.execute("ROLLBACK")
        log_pages, completed_pages = store.checkpoint()
        assert log_pages == completed_pages
        assert len(store.read_submissions()) == 2
    finally:
        if not reader.get_autocommit():
            reader.execute("ROLLBACK")
        reader.close()
        store.close()


def test_sqlite_capacity_exhaustion_latches_owner_and_rolls_back(
    tmp_path: Path, submission: Submission
) -> None:
    path = tmp_path / "capacity-test.db"

    class LimitedStore(APSWCriticalStore):
        def open(self) -> None:
            super().open()
            # SQLITE_FULL is triggered without filling the user's physical disk.
            db = self._db()
            db.pragma("max_page_count", db.pragma("page_count"))

    owner = CriticalJournalOwner(lambda: LimitedStore(path, submission.account_id))
    owner.start()
    try:
        body = canonical_bytes({"source": "isolated-test", "payload": "x" * 60_000})
        future = owner.request(
            lambda store: store.append_evidence("broker_facts", "large-test-fact", body),
            deadline=monotonic() + 2,
        )
        with pytest.raises(apsw.FullError):
            future.result(timeout=3)
        with pytest.raises(CriticalStoreError, match="UNAVAILABLE"):
            owner.commit(submission, deadline=monotonic() + 2).result(timeout=3)
        assert not owner.healthy
    finally:
        assert owner.close()

    recovered = APSWCriticalStore(path, submission.account_id)
    recovered.open()
    try:
        assert recovered.read_submissions() == ()
        assert recovered._db().execute("SELECT COUNT(*) FROM broker_facts").fetchone() == (0,)
    finally:
        recovered.close()


def test_corrupt_submission_is_not_released_as_recovery_evidence(
    tmp_path: Path, submission: Submission
) -> None:
    store = APSWCriticalStore(tmp_path / "corrupt-test.db", submission.account_id)
    store.open()
    try:
        store.commit_submission(submission)
        # Logical corruption is deterministic and confined to this temporary DB.
        store._db().execute("UPDATE submission_records SET payload_json='{}'")
        with pytest.raises(CriticalStoreError, match="PAYLOAD_CORRUPT"):
            store.read_submissions()
    finally:
        store.close()
