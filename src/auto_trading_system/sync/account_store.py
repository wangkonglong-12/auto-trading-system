"""Private monitoring outbox, separate from the trading Journal. Single agent writer."""

from __future__ import annotations

import os
import sqlite3
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from auto_trading_system.protocol.account_monitoring import (
    CN,
    MAX_BODY,
    Batch,
    Observation,
    Receipt,
    Snapshot,
    Trade,
    fingerprint,
)


def publish_observation(path: Path, observation: Observation) -> str:
    body = observation.model_dump_json().encode()
    if len(body) > MAX_BODY:
        raise ValueError("account observation too large")
    temporary = path.with_name(".account-observation-" + uuid4().hex)
    try:
        with temporary.open("xb") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return fingerprint(observation)


def read_observation(path: Path, expected_hash: str) -> Observation:
    with path.open("rb") as source:
        body = source.read(MAX_BODY + 1)
    if len(body) > MAX_BODY:
        raise ValueError("account observation too large")
    result = Observation.model_validate_json(body)
    if fingerprint(result) != expected_hash:
        raise ValueError("account observation handoff changed")
    return result


class AccountStore:
    def __init__(self, path: Path, node_id: str, account_id: str) -> None:
        if path.is_symlink() or path.resolve() != path.absolute():
            raise ValueError("account outbox requires a private direct path")
        if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
            raise ValueError("account outbox must be a private regular file")
        self.node_id, self.account_id = node_id, account_id
        self.db = sqlite3.connect(path, timeout=2)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1),
              node_id TEXT NOT NULL, account_id TEXT NOT NULL,
              sequence INTEGER NOT NULL, snapshot TEXT);
            CREATE TABLE IF NOT EXISTS fills (identity TEXT PRIMARY KEY, hash TEXT NOT NULL,
              body TEXT NOT NULL, trade_date TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0);
        """)
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO state VALUES(1,?,?,0,NULL)", (node_id, account_id)
            )
        scope = self.db.execute("SELECT node_id,account_id FROM state").fetchone()
        if scope != (node_id, account_id):
            self.close()
            raise ValueError("account outbox scope conflict")

    def close(self) -> None:
        self.db.close()

    def capture(self, observation: Observation) -> None:
        with self.db:
            sequence = self.db.execute("SELECT sequence FROM state").fetchone()[0] + 1
            snapshot = Snapshot(
                sequence=sequence, observation=observation.model_copy(update={"trades": ()})
            )
            for trade in observation.trades:
                hashed = fingerprint(trade)
                prior = self.db.execute(
                    "SELECT hash FROM fills WHERE identity=?", (trade.key,)
                ).fetchone()
                if prior is not None and prior[0] != hashed:
                    raise ValueError("broker trade content conflict")
                self.db.execute(
                    "INSERT OR IGNORE INTO fills(identity,hash,body,trade_date) VALUES(?,?,?,?)",
                    (
                        trade.key,
                        hashed,
                        trade.model_dump_json(),
                        trade.traded_at.astimezone(CN).date().isoformat(),
                    ),
                )
            if self.db.execute("SELECT COUNT(*) FROM fills WHERE sent=0").fetchone()[0] > 50000:
                raise ValueError("account trade outbox capacity exceeded")
            self.db.execute(
                "UPDATE state SET sequence=?,snapshot=? WHERE id=1",
                (sequence, snapshot.model_dump_json()),
            )
            cutoff = observation.captured_at.astimezone(CN).date() - timedelta(days=7)
            self.db.execute(
                "DELETE FROM fills WHERE sent=1 AND trade_date<?", (cutoff.isoformat(),)
            )

    def batch(self) -> Batch | None:
        body = self.db.execute("SELECT snapshot FROM state").fetchone()[0]
        if body is None:
            return None
        trades = tuple(
            Trade.model_validate_json(row[0])
            for row in self.db.execute(
                "SELECT body FROM fills WHERE sent=0 ORDER BY trade_date,identity LIMIT 1000"
            )
        )
        return Batch(
            node_id=self.node_id,
            account_id=self.account_id,
            snapshot=Snapshot.model_validate_json(body),
            trades=trades,
        )

    def acknowledge(self, batch: Batch, receipt: Receipt) -> None:
        if (batch.node_id, batch.account_id) != (self.node_id, self.account_id) or (
            receipt.batch_hash != fingerprint(batch)
        ):
            raise ValueError("account upload not confirmed")
        with self.db:
            for trade in batch.trades:
                self.db.execute(
                    "UPDATE fills SET sent=1 WHERE identity=? AND hash=?",
                    (trade.key, fingerprint(trade)),
                )
