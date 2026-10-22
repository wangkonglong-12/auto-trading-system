"""Single-thread APSW adapter for the minimal recovery journal.

No network/SDK callbacks occur inside transactions. A committed duplicate is
returned as evidence only, never as a new dispatch permit. Opening an unrelated
or legacy database fails before changing its journal mode or schema.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import get_ident
from typing import Literal
from uuid import uuid4

import apsw

from auto_trading_system.journal.critical_models import (
    CommitEvidence,
    CriticalStoreError,
    RecoveryRecord,
    Submission,
)
from auto_trading_system.journal.critical_schema import (
    APPLICATION_ID,
    SCHEMA_SQL,
    SCHEMA_VERSION,
)
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest, parse_bytes

EvidenceTable = Literal["broker_facts", "control_records", "recovery_checkpoints"]
_KEY_COLUMNS = {
    "broker_facts": "fact_key",
    "control_records": "control_id",
    "recovery_checkpoints": "checkpoint_id",
}


class APSWCriticalStore:
    """Created, called and closed on one owner thread, including all reads."""

    def __init__(self, path: Path, account_id: str) -> None:
        self._path = path
        self._account_id = account_id
        self._owner = get_ident()
        self._connection: apsw.Connection | None = None

    def _db(self) -> apsw.Connection:
        if get_ident() != self._owner:
            raise CriticalStoreError("JOURNAL_WRONG_OWNER")
        if self._connection is None:
            raise CriticalStoreError("JOURNAL_CLOSED")
        return self._connection

    def open(self) -> None:
        if get_ident() != self._owner or self._connection is not None:
            raise CriticalStoreError("JOURNAL_INVALID_LIFECYCLE")
        # Fail closed on a substituted APSW wheel. The release lock pins the wheel.
        if apsw.apswversion() != "3.53.4.0" or apsw.sqlitelibversion() != "3.53.4":
            raise CriticalStoreError("JOURNAL_UNQUALIFIED_SQLITE_VERSION")
        if self._path.is_symlink():
            raise CriticalStoreError("JOURNAL_SYMLINK_REJECTED")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        exists = self._path.exists()
        flags = apsw.SQLITE_OPEN_READWRITE
        if not exists:
            flags |= apsw.SQLITE_OPEN_CREATE
        db = apsw.Connection(str(self._path), flags=flags)
        self._connection = db
        try:
            if exists:
                # Probe ownership before any mutating PRAGMA, including WAL.
                if db.pragma("application_id") != APPLICATION_ID:
                    raise CriticalStoreError("JOURNAL_FOREIGN_DATABASE")
                if db.pragma("user_version") != SCHEMA_VERSION:
                    raise CriticalStoreError("JOURNAL_UNSUPPORTED_SCHEMA")
                row = db.execute("SELECT account_id FROM store_meta WHERE singleton=1").fetchone()
                if row != (self._account_id,):
                    raise CriticalStoreError("JOURNAL_ACCOUNT_MISMATCH")
            db.set_busy_timeout(100)
            if db.pragma("journal_mode", "wal") != "wal":
                raise CriticalStoreError("JOURNAL_WAL_UNAVAILABLE")
            db.pragma("synchronous", "full")
            db.pragma("foreign_keys", True)
            db.pragma("wal_autocheckpoint", 0)
            if not exists:
                with _transaction(db):
                    db.execute(SCHEMA_SQL)
                    db.execute(
                        "INSERT INTO store_meta VALUES (1, ?, ?, ?, ?)",
                        (SCHEMA_VERSION, str(uuid4()), self._account_id, _now()),
                    )
                    db.pragma("application_id", APPLICATION_ID)
                    db.pragma("user_version", SCHEMA_VERSION)
            if db.pragma("quick_check") != "ok":
                raise CriticalStoreError("JOURNAL_INTEGRITY_FAILED")
        except BaseException:
            self.close()
            raise

    def commit_submission(self, submission: Submission) -> CommitEvidence:
        db = self._db()
        submission = Submission.model_validate_json(submission.model_dump_json())
        if submission.account_id != self._account_id:
            raise CriticalStoreError("JOURNAL_ACCOUNT_MISMATCH")
        body = canonical_bytes(submission.model_dump(mode="json"))
        payload_hash = digest(body)
        # Explicit BEGIN IMMEDIATE makes write-lock acquisition part of the small
        # J1 operation. No earlier "prepare" transaction is needed.
        with _transaction(db):
            prior = db.execute(
                "SELECT record_seq, payload_hash FROM submission_records WHERE client_order_id=?",
                (submission.client_order_id,),
            ).fetchone()
            if prior is not None:
                if prior[1] != payload_hash:
                    raise CriticalStoreError("JOURNAL_ID_CONTENT_CONFLICT")
                evidence = CommitEvidence(
                    client_order_id=submission.client_order_id,
                    payload_hash=payload_hash,
                    record_seq=int(prior[0]),
                    fresh=False,
                )
            else:
                target = submission.target
                old_target = db.execute(
                    "SELECT target_remaining, rule_id FROM action_targets WHERE action_id=?",
                    (target.action_id,),
                ).fetchone()
                if old_target is not None and (
                    target.target_remaining > old_target[0] or target.rule_id != old_target[1]
                ):
                    raise CriticalStoreError("JOURNAL_ACTION_TARGET_CONFLICT")
                db.execute(
                    "INSERT INTO action_targets VALUES (?, ?, ?) "
                    "ON CONFLICT(action_id) DO UPDATE "
                    "SET target_remaining=excluded.target_remaining",
                    (target.action_id, target.target_remaining, target.rule_id),
                )
                db.execute(
                    "INSERT INTO submission_records "
                    "(client_order_id,payload_hash,payload_json,action_id,status,committed_at) "
                    "VALUES (?, ?, ?, ?, 'SEND_REQUESTED', ?)",
                    (
                        submission.client_order_id,
                        payload_hash,
                        body.decode(),
                        target.action_id,
                        _now(),
                    ),
                )
                evidence = CommitEvidence(
                    client_order_id=submission.client_order_id,
                    payload_hash=payload_hash,
                    record_seq=db.last_insert_rowid(),
                    fresh=True,
                )
        return evidence

    def append_evidence(self, table: EvidenceTable, key: str, body: bytes) -> bool:
        """Return False for an identical fact. A reused key cannot change history."""
        db = self._db()
        if table not in _KEY_COLUMNS or not key or len(key) > 256:
            raise CriticalStoreError("JOURNAL_EVIDENCE_KEY_INVALID")
        body = canonical_bytes(parse_bytes(body, max_bytes=64 * 1024))
        payload_hash = digest(body)
        column = _KEY_COLUMNS[table]  # Identifiers come only from the closed mapping above.
        fresh = False
        with _transaction(db):
            prior = db.execute(
                f"SELECT payload_hash FROM {table} WHERE {column}=?",
                (key,),
            ).fetchone()
            if prior is not None:
                if prior[0] != payload_hash:
                    raise CriticalStoreError("JOURNAL_FACT_CONTENT_CONFLICT")
            else:
                db.execute(
                    f"INSERT INTO {table} VALUES (?, ?, ?, ?)",
                    (key, payload_hash, body.decode(), _now()),
                )
                fresh = True
        return fresh

    def read_evidence(self, table: EvidenceTable, key: str) -> bytes | None:
        """Read one immutable control/checkpoint fact through the sole owner."""
        if table not in _KEY_COLUMNS or not key or len(key) > 256:
            raise CriticalStoreError("JOURNAL_EVIDENCE_KEY_INVALID")
        row = (
            self._db()
            .execute(
                f"SELECT payload_hash,payload_json FROM {table} WHERE {_KEY_COLUMNS[table]}=?",
                (key,),
            )
            .fetchone()
        )
        if row is None:
            return None
        body = str(row[1]).encode()
        if digest(body) != row[0]:
            raise CriticalStoreError("JOURNAL_PAYLOAD_CORRUPT")
        return canonical_bytes(parse_bytes(body, max_bytes=64 * 1024))

    def read_submissions(self, after_seq: int = 0, limit: int = 200) -> tuple[RecoveryRecord, ...]:
        if after_seq < 0 or not 1 <= limit <= 200:
            raise ValueError("recovery pages require a nonnegative cursor and 1..200 rows")
        rows = self._db().execute(
            "SELECT record_seq,payload_hash,payload_json FROM submission_records "
            "WHERE record_seq>? ORDER BY record_seq LIMIT ?",
            (after_seq, limit),
        )
        records: list[RecoveryRecord] = []
        for seq, payload_hash, body in rows:
            if digest(str(body).encode()) != payload_hash:
                raise CriticalStoreError("JOURNAL_PAYLOAD_CORRUPT")
            records.append(
                RecoveryRecord(
                    record_seq=int(seq),
                    payload_hash=str(payload_hash),
                    submission=Submission.model_validate_json(str(body)),
                )
            )
        return tuple(records)

    def checkpoint(self) -> tuple[int, int]:
        """Caller schedules maintenance only after pending trading work is drained."""
        return self._db().wal_checkpoint(mode=apsw.SQLITE_CHECKPOINT_PASSIVE)

    def backup(self, destination: Path) -> str:
        """Online backup API, never a copy of the live DB without its WAL."""
        db = self._db()
        if destination.exists() or destination.resolve() == self._path.resolve():
            raise CriticalStoreError("JOURNAL_BACKUP_TARGET_EXISTS")
        destination.parent.mkdir(parents=True, exist_ok=True)
        target = apsw.Connection(str(destination))
        try:
            with target.backup("main", db, "main") as backup:
                while not backup.done:
                    backup.step(128)
            if target.pragma("quick_check") != "ok":
                raise CriticalStoreError("JOURNAL_BACKUP_INVALID")
        finally:
            target.close()
        return digest(destination.read_bytes())

    def close(self) -> None:
        if self._connection is not None:
            self._db().close()
            self._connection = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def _transaction(db: apsw.Connection) -> Iterator[None]:
    """Preserve the original failure even when SQLite has already rolled back.

    SQLITE_FULL and some I/O errors abort the entire transaction automatically.
    An unconditional context-manager rollback would hide the storage error with
    "no transaction is active", making fault classification unreliable.
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.execute("COMMIT")
    except BaseException as original:
        if not db.get_autocommit():
            try:
                db.execute("ROLLBACK")
            except Exception as rollback_error:
                # The owner is latched unhealthy by the caller in either case.
                original.add_note(f"Journal rollback also failed: {type(rollback_error).__name__}")
        raise
