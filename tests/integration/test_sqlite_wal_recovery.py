from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from auto_trading_system.domain import TradingDate
from auto_trading_system.journal.sqlite import SQLiteJournalSettings, SQLiteLocalJournal

PROJECT_ROOT = Path(__file__).parents[2]
ACCOUNT_ID = "acct_demo_001"


def child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    source_path = str(PROJECT_ROOT / "src")
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = source_path if not existing else source_path + os.pathsep + existing
    return environment


def test_committed_wal_survives_process_crash_and_uncommitted_work_rolls_back(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "journal.db").resolve()
    committed_child = """
import os
import sys
from pathlib import Path
from auto_trading_system.journal import CheckpointRecord, JournalTransaction, PutCheckpoint
from auto_trading_system.journal.sqlite import SQLiteJournalSettings, SQLiteLocalJournal

path = Path(sys.argv[1])
journal = SQLiteLocalJournal(SQLiteJournalSettings(path, sys.argv[2], minimum_free_bytes=0))
journal.open()
journal.commit(JournalTransaction(
    "txn_crash_commit_0001",
    (PutCheckpoint(CheckpointRecord("runtime", "crash_probe", b"committed", 1)),),
))
os._exit(0)
"""
    subprocess.run(
        [sys.executable, "-c", committed_child, str(database_path), ACCOUNT_ID],
        cwd=PROJECT_ROOT,
        env=child_environment(),
        check=True,
        capture_output=True,
        timeout=15,
    )

    uncommitted_child = """
import os
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute("PRAGMA journal_mode = WAL")
connection.execute("BEGIN IMMEDIATE")
connection.execute(
    "INSERT INTO checkpoints(namespace, key, value, revision) VALUES (?, ?, ?, ?)",
    ("runtime", "uncommitted_probe", b"not-committed", 1),
)
os._exit(0)
"""
    subprocess.run(
        [sys.executable, "-c", uncommitted_child, str(database_path)],
        cwd=PROJECT_ROOT,
        env=child_environment(),
        check=True,
        capture_output=True,
        timeout=15,
    )

    journal = SQLiteLocalJournal(
        SQLiteJournalSettings(database_path, ACCOUNT_ID, minimum_free_bytes=0)
    )
    opened = journal.open()
    assert opened.created is False
    recovery = journal.load_recovery(
        account_id=ACCOUNT_ID, trade_date=TradingDate.parse("2026-09-04")
    )
    assert [(item.key, item.value, item.revision) for item in recovery.checkpoints] == [
        ("crash_probe", b"committed", 1)
    ]
    assert journal.health().last_commit_sequence == 1
    journal.close()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
