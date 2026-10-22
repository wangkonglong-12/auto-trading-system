"""Storage-only maintenance operations; runtime owns locks, paths and manifests.

Driver exceptions are translated here so application entrypoints never depend
on APSW. These functions are used only under the stopped-node maintenance lease.
"""

from __future__ import annotations

import os
from pathlib import Path
from time import monotonic

import apsw
from pydantic import Field

from auto_trading_system.journal.critical_models import Submission
from auto_trading_system.journal.critical_schema import APPLICATION_ID, SCHEMA_SQL, SCHEMA_VERSION
from auto_trading_system.protocol.v2.base import WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest, parse_bytes


class JournalProof(WireModel):
    storage_generation: str = Field(min_length=1, max_length=96, description="SQLite内部创建代际")
    submission_count: int = Field(ge=0, description="已核验J1记录数")
    last_record_seq: int = Field(ge=0, description="已核验J1最大序号")
    evidence_count: int = Field(ge=0, description="已核验回报、授权和检查点记录数")


def inspect_journal(path: Path, account_id: str) -> JournalProof:
    """Validate a standalone snapshot; caller validates path and byte limits."""
    try:
        return _inspect_journal(path, account_id)
    except apsw.Error as exc:
        raise ValueError("backup Journal storage validation failed") from exc


def _inspect_journal(path: Path, account_id: str) -> JournalProof:
    """Read-only structural, foreign-key and canonical payload validation."""
    db = apsw.Connection(str(path), flags=apsw.SQLITE_OPEN_READONLY)
    try:
        db.pragma("trusted_schema", False)
        if db.pragma("journal_mode") != "delete":
            raise ValueError("backup Journal must be standalone without WAL dependencies")
        if (
            db.pragma("application_id") != APPLICATION_ID
            or db.pragma("user_version") != SCHEMA_VERSION
        ):
            raise ValueError("backup Journal schema identity differs")
        # Compare the exact installed DDL, including constraints, not just names.
        reference = apsw.Connection(":memory:")
        try:
            reference.execute(SCHEMA_SQL)
            query = (
                "SELECT type,name,tbl_name,sql FROM sqlite_schema "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            if list(db.execute(query)) != list(reference.execute(query)):
                raise ValueError("backup Journal schema structure differs")
        finally:
            reference.close()
        if db.pragma("quick_check") != "ok" or next(db.execute("PRAGMA foreign_key_check"), None):
            raise ValueError("backup Journal integrity failed")
        meta = list(
            db.execute("SELECT schema_version,journal_generation,account_id FROM store_meta")
        )
        if len(meta) != 1 or meta[0][0] != SCHEMA_VERSION or meta[0][2] != account_id:
            raise ValueError("backup Journal account differs")
        count, high, evidence = 0, 0, 0
        targets: dict[str, tuple[int, str]] = {}
        for seq, order_id, sha, body, action_id in db.execute(
            "SELECT record_seq,client_order_id,payload_hash,payload_json,action_id "
            "FROM submission_records ORDER BY record_seq"
        ):
            encoded = str(body).encode()
            item = Submission.model_validate_json(encoded)
            if (
                digest(encoded) != sha
                or canonical_bytes(parse_bytes(encoded)) != encoded
                or item.account_id != account_id
                or item.client_order_id != order_id
                or item.target.action_id != action_id
                or int(seq) <= high
            ):
                raise ValueError("backup Journal submission binding differs")
            target = (item.target.target_remaining, item.target.rule_id)
            prior = targets.get(str(action_id))
            if prior and (target[0] > prior[0] or target[1] != prior[1]):
                raise ValueError("backup Journal target history widened")
            targets[str(action_id)] = target
            count, high = count + 1, int(seq)
            if count > 100_000:
                raise ValueError("backup Journal exceeds submission verification budget")
        stored_targets: dict[str, tuple[int, str]] = {}
        for key, remaining, rule in db.execute(
            "SELECT action_id,target_remaining,rule_id FROM action_targets"
        ):
            if len(stored_targets) >= 100_000:
                raise ValueError("backup Journal exceeds target verification budget")
            stored_targets[str(key)] = (int(remaining), str(rule))
        if stored_targets != targets:
            raise ValueError("backup Journal target projection differs")
        for table in ("broker_facts", "control_records", "recovery_checkpoints"):
            for sha, body in db.execute(f"SELECT payload_hash,payload_json FROM {table}"):
                encoded = str(body).encode()
                if digest(encoded) != sha or canonical_bytes(parse_bytes(encoded)) != encoded:
                    raise ValueError("backup Journal evidence digest differs")
                evidence += 1
        return JournalProof(
            storage_generation=str(meta[0][1]),
            submission_count=count,
            last_record_seq=high,
            evidence_count=evidence,
        )
    finally:
        db.close()


def copy_journal(path: Path, destination: Path, *, max_bytes: int) -> None:
    """Copy committed WAL through Backup API and flush a standalone new file."""
    try:
        _copy_journal(path, destination, max_bytes=max_bytes)
    except apsw.Error as exc:
        raise ValueError("backup Journal storage copy failed") from exc


def _copy_journal(path: Path, destination: Path, *, max_bytes: int) -> None:
    # Exclusive creation prevents this storage adapter from overwriting any file.
    with destination.open("xb"):
        pass
    source = apsw.Connection(str(path), flags=apsw.SQLITE_OPEN_READONLY)
    try:
        if int(source.pragma("page_count")) * int(source.pragma("page_size")) > max_bytes:
            raise ValueError("backup Journal exceeds byte budget")
        target = apsw.Connection(str(destination))
        try:
            deadline = monotonic() + 60
            with target.backup("main", source, "main") as backup:
                while not backup.done:
                    if monotonic() >= deadline:
                        raise ValueError("backup database copy exceeded time budget")
                    backup.step(128)
            target.pragma("journal_mode", "delete")
            target.pragma("synchronous", "full")
        finally:
            target.close()
    finally:
        source.close()
    with destination.open("r+b") as complete:
        os.fsync(complete.fileno())
