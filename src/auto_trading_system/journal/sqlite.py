"""SQLite/WAL LocalJournal adapter; this is the only module allowed to import sqlite3."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, cast
from uuid import uuid4

from auto_trading_system.domain import (
    Clock,
    Money,
    Price,
    Quantity,
    SecurityCode,
    ShanghaiTimestamp,
    SystemClock,
    TradingDate,
)
from auto_trading_system.journal.models import (
    MAX_STORED_SEQUENCE,
    AcknowledgeOutbox,
    AppendDomainEvent,
    BrokerOrderLink,
    BrokerTradeRecord,
    CacheTradePlan,
    CanonicalJsonDocument,
    CheckpointRecord,
    CommitOutcome,
    DeadLetterOutbox,
    EnqueueOutbox,
    EventDraft,
    JournalCommitResult,
    JournalHealth,
    JournalHealthStatus,
    JournalOperation,
    JournalRecoverySnapshot,
    JournalTransaction,
    LocalControlRecord,
    MaintenanceKind,
    MaintenanceRequest,
    MaintenanceResult,
    OrderIntentRecord,
    OutboxItem,
    OutboxKind,
    OutboxState,
    PersistedEvent,
    PutCheckpoint,
    PutLocalControl,
    PutOrderIntent,
    RecordBrokerTrade,
    RescheduleOutbox,
    StoreCommand,
    UpsertBrokerOrderLink,
)
from auto_trading_system.journal.port import (
    JournalConflictError,
    JournalOpenResult,
    JournalStatus,
    LocalJournalError,
)
from auto_trading_system.protocol.canonical_json import (
    calculate_event_hash,
    canonical_json_bytes,
)
from auto_trading_system.protocol.events import validate_domain_event_semantics

LATEST_SCHEMA_VERSION = 1
APPLICATION_ID = 0x4154534A  # ASCII "ATSJ"
MAX_OUTBOX_BATCH = 500
_ACCOUNT_ID_PATTERN = re.compile(r"acct_[A-Za-z0-9_-]{3,48}")

_SCHEMA_V1 = (
    """
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY CHECK (version > 0),
        checksum TEXT NOT NULL,
        applied_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE journal_meta (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        journal_id TEXT NOT NULL UNIQUE,
        account_id TEXT NOT NULL,
        commit_sequence INTEGER NOT NULL CHECK (commit_sequence >= 0),
        created_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE committed_transactions (
        transaction_id TEXT PRIMARY KEY,
        request_hash TEXT NOT NULL,
        commit_sequence INTEGER NOT NULL UNIQUE,
        result_body BLOB NOT NULL,
        committed_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE boot_sequences (
        boot_id TEXT PRIMARY KEY,
        last_local_seq INTEGER NOT NULL CHECK (last_local_seq > 0)
    ) STRICT
    """,
    """
    CREATE TABLE trade_plans (
        plan_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        body BLOB NOT NULL,
        cached_at TEXT NOT NULL
    ) STRICT
    """,
    "CREATE INDEX ix_trade_plans_recovery ON trade_plans(account_id, trade_date, plan_id)",
    """
    CREATE TABLE runtime_commands (
        command_id TEXT PRIMARY KEY,
        command_seq INTEGER NOT NULL CHECK (command_seq > 0),
        account_id TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        body BLOB NOT NULL,
        stored_at TEXT NOT NULL,
        UNIQUE(account_id, command_seq)
    ) STRICT
    """,
    """
    CREATE INDEX ix_runtime_commands_recovery
    ON runtime_commands(account_id, trade_date, command_seq)
    """,
    """
    CREATE TABLE order_intents (
        intent_id TEXT PRIMARY KEY,
        client_order_id TEXT NOT NULL UNIQUE,
        plan_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
        quantity INTEGER NOT NULL CHECK (quantity > 0),
        limit_price TEXT NOT NULL,
        max_notional TEXT NOT NULL,
        remark_token TEXT NOT NULL,
        execution_policy TEXT NOT NULL,
        created_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE INDEX ix_order_intents_recovery
    ON order_intents(account_id, trade_date, client_order_id)
    """,
    """
    CREATE TABLE broker_order_links (
        client_order_id TEXT PRIMARY KEY,
        remark_token TEXT NOT NULL,
        request_seq INTEGER,
        broker_order_id TEXT,
        broker_order_sysid TEXT,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(client_order_id) REFERENCES order_intents(client_order_id)
    ) STRICT
    """,
    "CREATE INDEX ix_broker_order_id ON broker_order_links(broker_order_id)",
    "CREATE INDEX ix_broker_order_sysid ON broker_order_links(broker_order_sysid)",
    "CREATE INDEX ix_broker_order_remark ON broker_order_links(remark_token)",
    """
    CREATE TABLE broker_trades (
        account_id TEXT NOT NULL,
        trade_id TEXT NOT NULL,
        client_order_id TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        traded_at TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        body BLOB NOT NULL,
        PRIMARY KEY(account_id, trade_id),
        FOREIGN KEY(client_order_id) REFERENCES order_intents(client_order_id)
    ) STRICT
    """,
    """
    CREATE INDEX ix_broker_trades_recovery
    ON broker_trades(account_id, trade_date, traded_at, trade_id)
    """,
    """
    CREATE TABLE domain_events (
        event_id TEXT PRIMARY KEY,
        boot_id TEXT NOT NULL,
        local_seq INTEGER NOT NULL CHECK (local_seq > 0),
        account_id TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        event_type TEXT NOT NULL,
        draft_hash TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        body BLOB NOT NULL,
        recorded_at TEXT NOT NULL,
        commit_sequence INTEGER NOT NULL CHECK (commit_sequence > 0),
        UNIQUE(boot_id, local_seq)
    ) STRICT
    """,
    "CREATE INDEX ix_domain_events_recovery ON domain_events(account_id, trade_date, event_id)",
    """
    CREATE TABLE outbox (
        outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('PENDING', 'ACKED', 'DEAD_LETTER')),
        document_id TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        body BLOB NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        available_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        terminal_at TEXT,
        last_error_reason TEXT,
        terminal_reason TEXT,
        UNIQUE(kind, document_id)
    ) STRICT
    """,
    "CREATE INDEX ix_outbox_pending ON outbox(state, available_at, outbox_id)",
    """
    CREATE TABLE local_controls (
        control_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        created_at TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        body BLOB NOT NULL
    ) STRICT
    """,
    "CREATE INDEX ix_local_controls_recovery ON local_controls(account_id, created_at, control_id)",
    """
    CREATE TABLE checkpoints (
        namespace TEXT NOT NULL,
        key TEXT NOT NULL,
        value BLOB NOT NULL,
        revision INTEGER NOT NULL CHECK (revision > 0),
        PRIMARY KEY(namespace, key)
    ) STRICT
    """,
)

_MIGRATION_V1_CHECKSUM = (
    "sha256:"
    + hashlib.sha256(
        "\n".join(statement.strip() for statement in _SCHEMA_V1).encode("utf-8")
    ).hexdigest()
)

_REQUIRED_SCHEMA_COLUMNS = {
    "schema_migrations": frozenset({"version", "checksum", "applied_at"}),
    "journal_meta": frozenset(
        {"singleton", "journal_id", "account_id", "commit_sequence", "created_at"}
    ),
    "committed_transactions": frozenset(
        {"transaction_id", "request_hash", "commit_sequence", "result_body", "committed_at"}
    ),
    "boot_sequences": frozenset({"boot_id", "last_local_seq"}),
    "trade_plans": frozenset(
        {"plan_id", "account_id", "trade_date", "content_hash", "body", "cached_at"}
    ),
    "runtime_commands": frozenset(
        {
            "command_id",
            "command_seq",
            "account_id",
            "trade_date",
            "content_hash",
            "body",
            "stored_at",
        }
    ),
    "order_intents": frozenset(
        {
            "intent_id",
            "client_order_id",
            "plan_id",
            "account_id",
            "trade_date",
            "symbol",
            "side",
            "quantity",
            "limit_price",
            "max_notional",
            "remark_token",
            "execution_policy",
            "created_at",
        }
    ),
    "broker_order_links": frozenset(
        {
            "client_order_id",
            "remark_token",
            "request_seq",
            "broker_order_id",
            "broker_order_sysid",
            "updated_at",
        }
    ),
    "broker_trades": frozenset(
        {
            "account_id",
            "trade_id",
            "client_order_id",
            "trade_date",
            "traded_at",
            "content_hash",
            "body",
        }
    ),
    "domain_events": frozenset(
        {
            "event_id",
            "boot_id",
            "local_seq",
            "account_id",
            "trade_date",
            "event_type",
            "draft_hash",
            "content_hash",
            "body",
            "recorded_at",
            "commit_sequence",
        }
    ),
    "outbox": frozenset(
        {
            "outbox_id",
            "kind",
            "state",
            "document_id",
            "content_hash",
            "body",
            "attempts",
            "available_at",
            "created_at",
            "terminal_at",
            "last_error_reason",
            "terminal_reason",
        }
    ),
    "local_controls": frozenset(
        {"control_id", "account_id", "reason_code", "created_at", "content_hash", "body"}
    ),
    "checkpoints": frozenset({"namespace", "key", "value", "revision"}),
}
_REQUIRED_INDEXES = frozenset(
    {
        "ix_trade_plans_recovery",
        "ix_runtime_commands_recovery",
        "ix_order_intents_recovery",
        "ix_broker_order_id",
        "ix_broker_order_sysid",
        "ix_broker_order_remark",
        "ix_broker_trades_recovery",
        "ix_domain_events_recovery",
        "ix_outbox_pending",
        "ix_local_controls_recovery",
    }
)


@dataclass(frozen=True, slots=True)
class SQLiteJournalSettings:
    database_path: Path
    account_id: str
    busy_timeout_ms: int = 3000
    wal_autocheckpoint_pages: int = 1000
    minimum_free_bytes: int = 512 * 1024 * 1024
    maximum_wal_bytes: int = 256 * 1024 * 1024
    maximum_pending_outbox: int = 100_000

    def __post_init__(self) -> None:
        if not isinstance(self.database_path, Path) or not self.database_path.is_absolute():
            raise ValueError("database_path must be absolute")
        if (
            not isinstance(self.account_id, str)
            or _ACCOUNT_ID_PATTERN.fullmatch(self.account_id) is None
        ):
            raise ValueError("account_id has an invalid format")
        if type(self.busy_timeout_ms) is not int or not 1 <= self.busy_timeout_ms <= 60_000:
            raise ValueError("busy_timeout_ms must be between 1 and 60000")
        if (
            type(self.wal_autocheckpoint_pages) is not int
            or not 1 <= self.wal_autocheckpoint_pages <= 1_000_000
        ):
            raise ValueError("wal_autocheckpoint_pages must be between 1 and 1000000")
        if type(self.minimum_free_bytes) is not int or self.minimum_free_bytes < 0:
            raise ValueError("minimum_free_bytes must be non-negative")
        if type(self.maximum_wal_bytes) is not int or self.maximum_wal_bytes < 1:
            raise ValueError("maximum_wal_bytes must be positive")
        if type(self.maximum_pending_outbox) is not int or self.maximum_pending_outbox < 1:
            raise ValueError("maximum_pending_outbox must be positive")


class SQLiteLocalJournal:
    """Synchronous SQLite adapter owned by exactly one Persistence Writer thread."""

    def __init__(self, settings: SQLiteJournalSettings, *, clock: Clock | None = None) -> None:
        if not isinstance(settings, SQLiteJournalSettings):
            raise ValueError("settings must be SQLiteJournalSettings")
        self._settings = settings
        self._clock = clock or SystemClock()
        self._status = JournalStatus.NEW
        self._connection: sqlite3.Connection | None = None
        self._owner_thread_id: int | None = None
        self._journal_id: str | None = None

    @property
    def status(self) -> JournalStatus:
        return self._status

    def open(self) -> JournalOpenResult:
        if self._status is not JournalStatus.NEW:
            raise LocalJournalError(
                "JOURNAL_NOT_NEW", "journal instance cannot be opened", retryable=False
            )
        self._owner_thread_id = threading.get_ident()
        path = self._settings.database_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                path,
                timeout=self._settings.busy_timeout_ms / 1000,
                isolation_level=None,
                check_same_thread=True,
            )
            connection.row_factory = sqlite3.Row
            self._connection = connection
            self._configure_connection(connection)
            created = self._migrate_and_identify(connection)
            self._verify_runtime_pragmas(connection)
            if self._journal_id is None:
                raise LocalJournalError(
                    "JOURNAL_STORAGE_INVARIANT",
                    "journal metadata is missing",
                    retryable=False,
                )
            result = JournalOpenResult(
                schema_version=LATEST_SCHEMA_VERSION,
                journal_id=self._journal_id,
                created=created,
            )
            self._status = JournalStatus.OPEN
            return result
        except LocalJournalError:
            self._fail_and_close()
            raise
        except (OSError, sqlite3.Error) as exc:
            self._fail_and_close()
            raise self._translate_error(exc, operation="open") from exc
        except (KeyError, TypeError, ValueError) as exc:
            self._fail_and_close()
            raise LocalJournalError(
                "JOURNAL_STORAGE_INVARIANT",
                "journal data violated a storage invariant",
                retryable=False,
            ) from exc

    def commit(self, transaction: JournalTransaction) -> JournalCommitResult:
        connection = self._require_open_owner()
        if not isinstance(transaction, JournalTransaction):
            raise ValueError("transaction must be a JournalTransaction")
        request_hash = _transaction_hash(transaction)
        commit_started = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT request_hash, commit_sequence, result_body
                FROM committed_transactions WHERE transaction_id = ?
                """,
                (transaction.transaction_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise JournalConflictError("TRANSACTION", transaction.transaction_id)
                result = _commit_result_from_bytes(cast(bytes, existing["result_body"]))
                connection.execute("ROLLBACK")
                return JournalCommitResult(
                    transaction_id=result.transaction_id,
                    outcome=CommitOutcome.DUPLICATE,
                    commit_sequence=result.commit_sequence,
                    persisted_events=result.persisted_events,
                )

            committed_at = self._clock.now()
            commit_sequence = self._next_commit_sequence(connection)
            persisted_events: list[PersistedEvent] = []
            persisted_event_ids: set[str] = set()
            for operation in transaction.operations:
                event = self._apply_operation(
                    connection,
                    operation,
                    committed_at=committed_at,
                    commit_sequence=commit_sequence,
                )
                if event is not None and event.event_id not in persisted_event_ids:
                    persisted_events.append(event)
                    persisted_event_ids.add(event.event_id)
            result = JournalCommitResult(
                transaction_id=transaction.transaction_id,
                outcome=CommitOutcome.COMMITTED,
                commit_sequence=commit_sequence,
                persisted_events=tuple(persisted_events),
            )
            connection.execute(
                """
                INSERT INTO committed_transactions(
                    transaction_id, request_hash, commit_sequence, result_body, committed_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    transaction.transaction_id,
                    request_hash,
                    commit_sequence,
                    _commit_result_to_bytes(result),
                    committed_at.to_wire(),
                ),
            )
            connection.execute(
                "UPDATE journal_meta SET commit_sequence = ? WHERE singleton = 1",
                (commit_sequence,),
            )
            commit_started = True
            connection.execute("COMMIT")
            return result
        except JournalConflictError:
            self._rollback(connection)
            raise
        except LocalJournalError as exc:
            self._rollback(connection)
            if exc.code in {"JOURNAL_SEQUENCE_EXHAUSTED", "JOURNAL_STORAGE_INVARIANT"}:
                self._fail_and_close()
            raise
        except sqlite3.Error as exc:
            if commit_started:
                self._fail_and_close()
                raise LocalJournalError(
                    "JOURNAL_COMMIT_OUTCOME_UNKNOWN",
                    "journal commit outcome must be recovered by transaction id",
                    retryable=True,
                ) from exc
            self._rollback(connection)
            translated = self._handle_storage_error(exc, operation="commit")
            raise translated from exc
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            self._rollback(connection)
            self._fail_and_close()
            raise LocalJournalError(
                "JOURNAL_STORAGE_INVARIANT",
                "journal data violated a storage invariant",
                retryable=False,
            ) from exc

    def load_recovery(self, *, account_id: str, trade_date: TradingDate) -> JournalRecoverySnapshot:
        connection = self._require_open_owner()
        if not isinstance(trade_date, TradingDate):
            raise ValueError("trade_date must be a TradingDate")
        self._require_account(account_id)
        try:
            date_text = trade_date.to_wire()
            plans = tuple(
                _document_from_row(row)
                for row in connection.execute(
                    """
                    SELECT plan_id AS document_id, content_hash, body
                    FROM trade_plans
                    WHERE account_id = ? AND trade_date = ? ORDER BY plan_id
                    """,
                    (account_id, date_text),
                )
            )
            commands = tuple(
                _document_from_row(row)
                for row in connection.execute(
                    """
                    SELECT command_id AS document_id, content_hash, body
                    FROM runtime_commands
                    WHERE account_id = ? AND trade_date = ? ORDER BY command_seq
                    """,
                    (account_id, date_text),
                )
            )
            intents = tuple(
                _intent_from_row(row)
                for row in connection.execute(
                    """
                    SELECT * FROM order_intents
                    WHERE account_id = ? AND trade_date = ? ORDER BY client_order_id
                    """,
                    (account_id, date_text),
                )
            )
            links = tuple(
                _link_from_row(row)
                for row in connection.execute(
                    """
                    SELECT link.* FROM broker_order_links AS link
                    JOIN order_intents AS intent USING(client_order_id)
                    WHERE intent.account_id = ? AND intent.trade_date = ?
                    ORDER BY link.client_order_id
                    """,
                    (account_id, date_text),
                )
            )
            trades = tuple(
                _trade_from_row(row)
                for row in connection.execute(
                    """
                    SELECT * FROM broker_trades
                    WHERE account_id = ? AND trade_date = ?
                    ORDER BY traded_at, trade_id
                    """,
                    (account_id, date_text),
                )
            )
            controls = tuple(
                _control_from_row(row)
                for row in connection.execute(
                    """
                    SELECT * FROM local_controls
                    WHERE account_id = ? ORDER BY created_at, control_id
                    """,
                    (account_id,),
                )
            )
            checkpoints = tuple(
                _checkpoint_from_row(row)
                for row in connection.execute("SELECT * FROM checkpoints ORDER BY namespace, key")
            )
            watermark_row = connection.execute(
                """
                SELECT COALESCE(MAX(command_seq), 0) AS high_watermark
                FROM runtime_commands WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()
            return JournalRecoverySnapshot(
                account_id=account_id,
                trade_date=trade_date,
                plans=plans,
                commands=commands,
                order_intents=intents,
                broker_order_links=links,
                broker_trades=trades,
                controls=controls,
                checkpoints=checkpoints,
                command_high_watermark=cast(int, watermark_row["high_watermark"]),
            )
        except sqlite3.Error as exc:
            raise self._handle_storage_error(exc, operation="load_recovery") from exc
        except (KeyError, TypeError, ValueError) as exc:
            self._fail_and_close()
            raise LocalJournalError(
                "JOURNAL_STORAGE_INVARIANT",
                "journal data violated a storage invariant",
                retryable=False,
            ) from exc

    def fetch_outbox(
        self, *, limit: int, available_at: ShanghaiTimestamp
    ) -> tuple[OutboxItem, ...]:
        connection = self._require_open_owner()
        if type(limit) is not int or not 1 <= limit <= MAX_OUTBOX_BATCH:
            raise ValueError(f"limit must be between 1 and {MAX_OUTBOX_BATCH}")
        if not isinstance(available_at, ShanghaiTimestamp):
            raise ValueError("available_at must be a ShanghaiTimestamp")
        try:
            return tuple(
                _outbox_from_row(row)
                for row in connection.execute(
                    """
                    SELECT * FROM outbox
                    WHERE state = 'PENDING' AND available_at <= ?
                    ORDER BY outbox_id LIMIT ?
                    """,
                    (available_at.to_wire(), limit),
                )
            )
        except sqlite3.Error as exc:
            raise self._handle_storage_error(exc, operation="fetch_outbox") from exc
        except (KeyError, TypeError, ValueError) as exc:
            self._fail_and_close()
            raise LocalJournalError(
                "JOURNAL_STORAGE_INVARIANT",
                "journal data violated a storage invariant",
                retryable=False,
            ) from exc

    def health(self) -> JournalHealth:
        if self._status is not JournalStatus.OPEN:
            return JournalHealth(
                status=JournalHealthStatus.UNAVAILABLE,
                writable=False,
                schema_version=0,
                database_bytes=_database_bytes(self._settings.database_path),
                pending_outbox=0,
                last_commit_sequence=0,
                reason_codes=("JOURNAL_NOT_OPEN",),
                wal_bytes=_file_bytes(Path(f"{self._settings.database_path}-wal")),
                free_disk_bytes=_free_disk_bytes(self._settings.database_path.parent),
            )
        connection = self._require_open_owner()
        try:
            version = cast(int, connection.execute("PRAGMA user_version").fetchone()[0])
            pending = cast(
                int,
                connection.execute(
                    "SELECT COUNT(*) FROM outbox WHERE state = 'PENDING'"
                ).fetchone()[0],
            )
            sequence = cast(
                int,
                connection.execute(
                    "SELECT commit_sequence FROM journal_meta WHERE singleton = 1"
                ).fetchone()[0],
            )
            reason_codes: list[str] = []
            free_disk_bytes = shutil.disk_usage(self._settings.database_path.parent).free
            if free_disk_bytes < self._settings.minimum_free_bytes:
                reason_codes.append("LOW_DISK_SPACE")
            wal_bytes = _file_bytes(Path(f"{self._settings.database_path}-wal"))
            if wal_bytes > self._settings.maximum_wal_bytes:
                reason_codes.append("WAL_SIZE_HIGH")
            if pending > self._settings.maximum_pending_outbox:
                reason_codes.append("OUTBOX_BACKLOG_HIGH")
            writable = True
            try:
                self._probe_writable(connection)
            except sqlite3.Error as exc:
                probe_error = self._translate_error(exc, operation="health_probe")
                if not probe_error.retryable:
                    self._fail_and_close()
                    return JournalHealth(
                        status=JournalHealthStatus.UNAVAILABLE,
                        writable=False,
                        schema_version=version,
                        database_bytes=_database_bytes(self._settings.database_path),
                        pending_outbox=pending,
                        last_commit_sequence=sequence,
                        reason_codes=(probe_error.code,),
                        wal_bytes=wal_bytes,
                        free_disk_bytes=free_disk_bytes,
                    )
                writable = False
                reason_codes.append(probe_error.code)
            healthy = not reason_codes
            return JournalHealth(
                status=(JournalHealthStatus.HEALTHY if healthy else JournalHealthStatus.DEGRADED),
                writable=writable,
                schema_version=version,
                database_bytes=_database_bytes(self._settings.database_path),
                pending_outbox=pending,
                last_commit_sequence=sequence,
                reason_codes=tuple(reason_codes),
                wal_bytes=wal_bytes,
                free_disk_bytes=free_disk_bytes,
            )
        except (KeyError, OSError, TypeError, ValueError, sqlite3.Error):
            self._fail_and_close()
            return JournalHealth(
                status=JournalHealthStatus.UNAVAILABLE,
                writable=False,
                schema_version=0,
                database_bytes=_database_bytes(self._settings.database_path),
                pending_outbox=0,
                last_commit_sequence=0,
                reason_codes=("JOURNAL_HEALTH_QUERY_FAILED",),
                wal_bytes=_file_bytes(Path(f"{self._settings.database_path}-wal")),
                free_disk_bytes=_free_disk_bytes(self._settings.database_path.parent),
            )

    def maintain(self, request: MaintenanceRequest) -> MaintenanceResult:
        connection = self._require_open_owner()
        if not isinstance(request, MaintenanceRequest):
            raise ValueError("request must be a MaintenanceRequest")
        try:
            if request.kind is MaintenanceKind.CHECKPOINT:
                row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                completed = cast(int, row[0]) == 0
                page_size = cast(int, connection.execute("PRAGMA page_size").fetchone()[0])
                return MaintenanceResult(
                    kind=request.kind,
                    completed=completed,
                    bytes_written=cast(int, row[2]) * page_size,
                    detail_code=("WAL_CHECKPOINT_COMPLETE" if completed else "WAL_CHECKPOINT_BUSY"),
                )
            if request.kind is MaintenanceKind.INTEGRITY_CHECK:
                rows = connection.execute("PRAGMA quick_check").fetchall()
                completed = len(rows) == 1 and rows[0][0] == "ok"
                if not completed:
                    self._fail_and_close()
                    raise LocalJournalError(
                        "JOURNAL_INTEGRITY_CHECK_FAILED",
                        "journal integrity check failed",
                        retryable=False,
                    )
                return MaintenanceResult(
                    kind=request.kind,
                    completed=completed,
                    bytes_written=0,
                    detail_code=("INTEGRITY_CHECK_OK" if completed else "INTEGRITY_CHECK_FAILED"),
                )
            if request.destination is None:  # pragma: no cover - model protects this
                raise AssertionError("backup destination is required")
            return self._backup(connection, request.destination)
        except LocalJournalError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise self._handle_storage_error(exc, operation="maintain") from exc

    def close(self) -> None:
        if self._status in {JournalStatus.CLOSED, JournalStatus.NEW}:
            self._status = JournalStatus.CLOSED
            return
        if self._connection is None:
            self._status = JournalStatus.CLOSED
            return
        self._require_owner_thread()
        connection = self._connection
        self._connection = None
        try:
            connection.close()
        except sqlite3.Error as exc:
            self._status = JournalStatus.FAILED
            raise self._translate_error(exc, operation="close") from exc
        self._status = JournalStatus.CLOSED

    def _configure_connection(self, connection: sqlite3.Connection) -> None:
        connection.execute(f"PRAGMA busy_timeout = {self._settings.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        mode = cast(str, connection.execute("PRAGMA journal_mode = WAL").fetchone()[0])
        if mode.lower() != "wal":
            raise LocalJournalError(
                "JOURNAL_WAL_UNAVAILABLE", "WAL mode could not be enabled", retryable=False
            )
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute(f"PRAGMA wal_autocheckpoint = {self._settings.wal_autocheckpoint_pages}")

    def _migrate_and_identify(self, connection: sqlite3.Connection) -> bool:
        current_version = cast(int, connection.execute("PRAGMA user_version").fetchone()[0])
        application_id = cast(int, connection.execute("PRAGMA application_id").fetchone()[0])
        if current_version > LATEST_SCHEMA_VERSION:
            raise LocalJournalError(
                "JOURNAL_SCHEMA_TOO_NEW", "journal schema is not supported", retryable=False
            )
        if application_id not in {0, APPLICATION_ID}:
            raise LocalJournalError(
                "JOURNAL_APPLICATION_MISMATCH",
                "database belongs to another application",
                retryable=False,
            )
        if current_version > 0 and application_id != APPLICATION_ID:
            raise LocalJournalError(
                "JOURNAL_APPLICATION_MISMATCH",
                "database does not carry the journal application id",
                retryable=False,
            )
        if current_version == 0:
            user_tables = connection.execute(
                """
                SELECT name FROM sqlite_schema
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            ).fetchall()
            if user_tables:
                raise LocalJournalError(
                    "JOURNAL_APPLICATION_MISMATCH",
                    "database is not an empty journal",
                    retryable=False,
                )
        connection.execute("BEGIN EXCLUSIVE")
        try:
            if current_version == 0:
                for statement in _SCHEMA_V1:
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT INTO schema_migrations(version, checksum, applied_at)
                    VALUES (1, ?, ?)
                    """,
                    (_MIGRATION_V1_CHECKSUM, self._clock.now().to_wire()),
                )
                connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION}")
            migration = connection.execute(
                "SELECT checksum FROM schema_migrations WHERE version = 1"
            ).fetchone()
            if migration is None or migration["checksum"] != _MIGRATION_V1_CHECKSUM:
                raise LocalJournalError(
                    "JOURNAL_MIGRATION_CHECKSUM_MISMATCH",
                    "journal migration history is invalid",
                    retryable=False,
                )
            self._verify_schema_manifest(connection)
            row = connection.execute(
                "SELECT journal_id, account_id FROM journal_meta WHERE singleton = 1"
            ).fetchone()
            created = current_version == 0
            if row is None and not created:
                raise LocalJournalError(
                    "JOURNAL_STORAGE_INVARIANT",
                    "journal metadata is missing",
                    retryable=False,
                )
            if row is None:
                self._journal_id = f"journal_{uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO journal_meta(
                        singleton, journal_id, account_id, commit_sequence, created_at
                    ) VALUES (1, ?, ?, 0, ?)
                    """,
                    (
                        self._journal_id,
                        self._settings.account_id,
                        self._clock.now().to_wire(),
                    ),
                )
            else:
                self._journal_id = cast(str, row["journal_id"])
                if row["account_id"] != self._settings.account_id:
                    raise LocalJournalError(
                        "JOURNAL_ACCOUNT_MISMATCH",
                        "journal belongs to another account",
                        retryable=False,
                    )
            connection.execute("COMMIT")
            return created
        except LocalJournalError:
            self._rollback(connection)
            raise
        except sqlite3.Error as exc:
            self._rollback(connection)
            raise LocalJournalError(
                "JOURNAL_MIGRATION_FAILED",
                "journal schema migration failed",
                retryable=False,
            ) from exc

    def _verify_runtime_pragmas(self, connection: sqlite3.Connection) -> None:
        values = {
            "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
            "synchronous": cast(int, connection.execute("PRAGMA synchronous").fetchone()[0]),
            "foreign_keys": cast(int, connection.execute("PRAGMA foreign_keys").fetchone()[0]),
            "wal_autocheckpoint": cast(
                int, connection.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
            ),
            "busy_timeout": cast(int, connection.execute("PRAGMA busy_timeout").fetchone()[0]),
            "trusted_schema": cast(int, connection.execute("PRAGMA trusted_schema").fetchone()[0]),
            "application_id": cast(int, connection.execute("PRAGMA application_id").fetchone()[0]),
            "user_version": cast(int, connection.execute("PRAGMA user_version").fetchone()[0]),
        }
        if values != {
            "journal_mode": "wal",
            "synchronous": 2,
            "foreign_keys": 1,
            "wal_autocheckpoint": self._settings.wal_autocheckpoint_pages,
            "busy_timeout": self._settings.busy_timeout_ms,
            "trusted_schema": 0,
            "application_id": APPLICATION_ID,
            "user_version": LATEST_SCHEMA_VERSION,
        }:
            raise LocalJournalError(
                "JOURNAL_PRAGMA_MISMATCH",
                "required durability settings are inactive",
                retryable=False,
            )

    def _verify_schema_manifest(self, connection: sqlite3.Connection) -> None:
        for table, required_columns in _REQUIRED_SCHEMA_COLUMNS.items():
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
            actual_columns = {cast(str, row["name"]) for row in rows}
            if not required_columns.issubset(actual_columns):
                raise LocalJournalError(
                    "JOURNAL_SCHEMA_INCOMPLETE",
                    "journal schema is incomplete",
                    retryable=False,
                )
        indexes = {
            cast(str, row["name"])
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'index'")
        }
        if not _REQUIRED_INDEXES.issubset(indexes):
            raise LocalJournalError(
                "JOURNAL_SCHEMA_INCOMPLETE",
                "journal schema is incomplete",
                retryable=False,
            )

    def _probe_writable(self, connection: sqlite3.Connection) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE journal_meta SET commit_sequence = commit_sequence WHERE singleton = 1"
            )
        finally:
            self._rollback(connection)

    def _next_commit_sequence(self, connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT commit_sequence FROM journal_meta WHERE singleton = 1"
        ).fetchone()
        sequence = cast(int, row["commit_sequence"]) + 1
        if sequence > 9_223_372_036_854_775_807:
            raise LocalJournalError(
                "JOURNAL_SEQUENCE_EXHAUSTED", "commit sequence is exhausted", retryable=False
            )
        return sequence

    def _apply_operation(
        self,
        connection: sqlite3.Connection,
        operation: JournalOperation,
        *,
        committed_at: ShanghaiTimestamp,
        commit_sequence: int,
    ) -> PersistedEvent | None:
        if isinstance(operation, CacheTradePlan):
            self._cache_plan(connection, operation, committed_at)
        elif isinstance(operation, StoreCommand):
            self._store_command(connection, operation, committed_at)
        elif isinstance(operation, PutOrderIntent):
            self._put_intent(connection, operation.intent)
        elif isinstance(operation, UpsertBrokerOrderLink):
            self._upsert_link(connection, operation.link)
        elif isinstance(operation, RecordBrokerTrade):
            self._record_trade(connection, operation.trade)
        elif isinstance(operation, AppendDomainEvent):
            return self._append_event(
                connection,
                operation.draft,
                committed_at,
                commit_sequence=commit_sequence,
            )
        elif isinstance(operation, EnqueueOutbox):
            self._enqueue_document(connection, operation.kind, operation.document, committed_at)
        elif isinstance(operation, RescheduleOutbox):
            self._reschedule_outbox(connection, operation)
        elif isinstance(operation, AcknowledgeOutbox):
            self._set_outbox_terminal(
                connection,
                operation.outbox_id,
                operation.content_hash,
                "ACKED",
                None,
                committed_at,
            )
        elif isinstance(operation, DeadLetterOutbox):
            self._set_outbox_terminal(
                connection,
                operation.outbox_id,
                operation.content_hash,
                "DEAD_LETTER",
                operation.reason_code,
                committed_at,
            )
        elif isinstance(operation, PutLocalControl):
            self._put_control(connection, operation.control)
        elif isinstance(operation, PutCheckpoint):
            self._put_checkpoint(connection, operation.checkpoint)
        else:  # pragma: no cover - JournalTransaction validates the union
            raise AssertionError("unsupported journal operation")
        return None

    def _cache_plan(
        self,
        connection: sqlite3.Connection,
        operation: CacheTradePlan,
        committed_at: ShanghaiTimestamp,
    ) -> None:
        payload = operation.document.to_mapping()
        account_id = _document_string(payload, "account_id")
        self._require_account(account_id)
        trade_date = _document_trading_date(payload, "trade_date").to_wire()
        if payload.get("plan_id") != operation.plan_id:
            _invalid_document_scope()
        values = {
            "account_id": account_id,
            "trade_date": trade_date,
            "content_hash": operation.document.content_hash,
            "body": operation.document.body,
            "cached_at": committed_at.to_wire(),
        }
        self._insert_or_compare(
            connection,
            table="trade_plans",
            key_column="plan_id",
            key_value=operation.plan_id,
            values=values,
            immutable_columns=("account_id", "trade_date", "content_hash", "body"),
            record_type="TRADE_PLAN",
        )

    def _store_command(
        self,
        connection: sqlite3.Connection,
        operation: StoreCommand,
        committed_at: ShanghaiTimestamp,
    ) -> None:
        payload = operation.document.to_mapping()
        account_id = _document_string(payload, "account_id")
        self._require_account(account_id)
        issued_at = _document_timestamp(payload, "issued_at")
        if (
            payload.get("command_id") != operation.command_id
            or payload.get("command_seq") != operation.command_seq
        ):
            _invalid_document_scope()
        sequence_owner = connection.execute(
            """
            SELECT command_id FROM runtime_commands
            WHERE account_id = ? AND command_seq = ?
            """,
            (account_id, operation.command_seq),
        ).fetchone()
        if sequence_owner is not None and sequence_owner["command_id"] != operation.command_id:
            raise JournalConflictError("COMMAND_SEQUENCE", f"cmdseq_{operation.command_seq}")
        values = {
            "command_seq": operation.command_seq,
            "account_id": account_id,
            "trade_date": issued_at.trading_date.to_wire(),
            "content_hash": operation.document.content_hash,
            "body": operation.document.body,
            "stored_at": committed_at.to_wire(),
        }
        self._insert_or_compare(
            connection,
            table="runtime_commands",
            key_column="command_id",
            key_value=operation.command_id,
            values=values,
            immutable_columns=("command_seq", "account_id", "trade_date", "content_hash", "body"),
            record_type="COMMAND",
        )

    def _put_intent(self, connection: sqlite3.Connection, intent: OrderIntentRecord) -> None:
        self._require_account(intent.account_id)
        owner = connection.execute(
            "SELECT intent_id FROM order_intents WHERE client_order_id = ?",
            (intent.client_order_id,),
        ).fetchone()
        if owner is not None and owner["intent_id"] != intent.intent_id:
            raise JournalConflictError("CLIENT_ORDER", intent.client_order_id)
        values: dict[str, object] = {
            "client_order_id": intent.client_order_id,
            "plan_id": intent.plan_id,
            "account_id": intent.account_id,
            "trade_date": intent.trade_date.to_wire(),
            "symbol": intent.symbol.to_wire(),
            "side": intent.side.value,
            "quantity": intent.quantity.value,
            "limit_price": intent.limit_price.to_wire(),
            "max_notional": intent.max_notional.to_wire(),
            "remark_token": intent.remark_token,
            "execution_policy": intent.execution_policy,
            "created_at": intent.created_at.to_wire(),
        }
        self._insert_or_compare(
            connection,
            table="order_intents",
            key_column="intent_id",
            key_value=intent.intent_id,
            values=values,
            immutable_columns=tuple(values),
            record_type="ORDER_INTENT",
        )

    def _upsert_link(self, connection: sqlite3.Connection, link: BrokerOrderLink) -> None:
        intent_row = connection.execute(
            "SELECT remark_token FROM order_intents WHERE client_order_id = ?",
            (link.client_order_id,),
        ).fetchone()
        if intent_row is None:
            raise LocalJournalError(
                "JOURNAL_ORDER_INTENT_NOT_FOUND",
                "broker link has no persisted order intent",
                retryable=False,
            )
        if intent_row["remark_token"] != link.remark_token:
            raise JournalConflictError("BROKER_ORDER_LINK", link.client_order_id)
        row = connection.execute(
            "SELECT * FROM broker_order_links WHERE client_order_id = ?",
            (link.client_order_id,),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO broker_order_links(
                    client_order_id, remark_token, request_seq, broker_order_id,
                    broker_order_sysid, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    link.client_order_id,
                    link.remark_token,
                    link.request_seq,
                    link.broker_order_id,
                    link.broker_order_sysid,
                    link.updated_at.to_wire(),
                ),
            )
            return
        if row["remark_token"] != link.remark_token:
            raise JournalConflictError("BROKER_ORDER_LINK", link.client_order_id)
        merged: dict[str, object | None] = {}
        for column in ("request_seq", "broker_order_id", "broker_order_sysid"):
            old = row[column]
            new = getattr(link, column)
            if old is not None and new is not None and old != new:
                raise JournalConflictError("BROKER_ORDER_LINK", link.client_order_id)
            merged[column] = old if old is not None else new
        old_time = ShanghaiTimestamp.parse(cast(str, row["updated_at"]))
        updated_at = max(old_time, link.updated_at).to_wire()
        connection.execute(
            """
            UPDATE broker_order_links
            SET request_seq = ?, broker_order_id = ?, broker_order_sysid = ?, updated_at = ?
            WHERE client_order_id = ?
            """,
            (
                merged["request_seq"],
                merged["broker_order_id"],
                merged["broker_order_sysid"],
                updated_at,
                link.client_order_id,
            ),
        )

    def _record_trade(self, connection: sqlite3.Connection, trade: BrokerTradeRecord) -> None:
        self._require_account(trade.account_id)
        intent_row = connection.execute(
            "SELECT account_id FROM order_intents WHERE client_order_id = ?",
            (trade.client_order_id,),
        ).fetchone()
        if intent_row is None:
            raise LocalJournalError(
                "JOURNAL_ORDER_INTENT_NOT_FOUND",
                "broker trade has no persisted order intent",
                retryable=False,
            )
        if intent_row["account_id"] != trade.account_id:
            raise JournalConflictError("BROKER_TRADE", trade.trade_id)
        values: dict[str, object] = {
            "trade_id": trade.trade_id,
            "client_order_id": trade.client_order_id,
            "trade_date": trade.traded_at.trading_date.to_wire(),
            "traded_at": trade.traded_at.to_wire(),
            "content_hash": trade.document.content_hash,
            "body": trade.document.body,
        }
        self._insert_or_compare(
            connection,
            table="broker_trades",
            key_column="account_id",
            key_value=trade.account_id,
            values=values,
            immutable_columns=tuple(values),
            record_type="BROKER_TRADE",
            extra_key=("trade_id", trade.trade_id),
        )

    def _append_event(
        self,
        connection: sqlite3.Connection,
        draft: EventDraft,
        committed_at: ShanghaiTimestamp,
        *,
        commit_sequence: int,
    ) -> PersistedEvent:
        self._require_account(draft.account_id)
        existing = connection.execute(
            """
            SELECT event_id AS document_id, boot_id, local_seq, draft_hash, content_hash, body
            FROM domain_events WHERE event_id = ?
            """,
            (draft.event_id,),
        ).fetchone()
        if existing is not None:
            document = _document_from_row(existing)
            if existing["draft_hash"] != _event_draft_hash(draft):
                raise JournalConflictError("DOMAIN_EVENT", draft.event_id)
            return PersistedEvent(
                event_id=draft.event_id,
                boot_id=cast(str, existing["boot_id"]),
                local_seq=cast(int, existing["local_seq"]),
                document=document,
            )
        row = connection.execute(
            "SELECT last_local_seq FROM boot_sequences WHERE boot_id = ?",
            (draft.boot_id,),
        ).fetchone()
        local_seq = 1 if row is None else cast(int, row["last_local_seq"]) + 1
        if local_seq > MAX_STORED_SEQUENCE:
            raise LocalJournalError(
                "JOURNAL_SEQUENCE_EXHAUSTED",
                "event sequence is exhausted",
                retryable=False,
            )
        if row is None:
            connection.execute(
                "INSERT INTO boot_sequences(boot_id, last_local_seq) VALUES (?, ?)",
                (draft.boot_id, local_seq),
            )
        else:
            connection.execute(
                "UPDATE boot_sequences SET last_local_seq = ? WHERE boot_id = ?",
                (local_seq, draft.boot_id),
            )
        recorded_at = max(committed_at, draft.occurred_at)
        event = _event_mapping(draft, local_seq=local_seq, recorded_at=recorded_at)
        event_hash = calculate_event_hash(event)
        event["event_hash"] = event_hash
        try:
            validate_domain_event_semantics(event)
        except ValueError as exc:
            raise LocalJournalError(
                "JOURNAL_EVENT_INVALID",
                "domain event violates the upload contract",
                retryable=False,
            ) from exc
        body = canonical_json_bytes(event, exclude=frozenset())
        document = CanonicalJsonDocument(
            document_id=draft.event_id,
            content_hash=event_hash,
            body=body,
        )
        connection.execute(
            """
            INSERT INTO domain_events(
                event_id, boot_id, local_seq, account_id, trade_date, event_type,
                draft_hash, content_hash, body, recorded_at, commit_sequence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                draft.event_id,
                draft.boot_id,
                local_seq,
                draft.account_id,
                draft.trade_date.to_wire(),
                draft.event_type.value,
                _event_draft_hash(draft),
                event_hash,
                body,
                recorded_at.to_wire(),
                commit_sequence,
            ),
        )
        self._enqueue_document(connection, OutboxKind.DOMAIN_EVENT, document, recorded_at)
        return PersistedEvent(
            event_id=draft.event_id,
            boot_id=draft.boot_id,
            local_seq=local_seq,
            document=document,
        )

    def _enqueue_document(
        self,
        connection: sqlite3.Connection,
        kind: OutboxKind,
        document: CanonicalJsonDocument,
        created_at: ShanghaiTimestamp,
    ) -> None:
        row = connection.execute(
            "SELECT content_hash, body FROM outbox WHERE kind = ? AND document_id = ?",
            (kind.value, document.document_id),
        ).fetchone()
        if row is not None:
            if row["content_hash"] != document.content_hash or row["body"] != document.body:
                raise JournalConflictError("OUTBOX_DOCUMENT", document.document_id)
            return
        connection.execute(
            """
            INSERT INTO outbox(
                kind, state, document_id, content_hash, body, attempts,
                available_at, created_at, terminal_at, last_error_reason, terminal_reason
            ) VALUES (?, 'PENDING', ?, ?, ?, 0, ?, ?, NULL, NULL, NULL)
            """,
            (
                kind.value,
                document.document_id,
                document.content_hash,
                document.body,
                created_at.to_wire(),
                created_at.to_wire(),
            ),
        )

    def _reschedule_outbox(
        self, connection: sqlite3.Connection, operation: RescheduleOutbox
    ) -> None:
        row = connection.execute(
            """
            SELECT state, content_hash, attempts, available_at, created_at, last_error_reason
            FROM outbox WHERE outbox_id = ?
            """,
            (operation.outbox_id,),
        ).fetchone()
        if row is None:
            raise LocalJournalError(
                "JOURNAL_OUTBOX_NOT_FOUND", "outbox item does not exist", retryable=False
            )
        if row["content_hash"] != operation.content_hash:
            raise JournalConflictError("OUTBOX_RECEIPT", f"outbox_{operation.outbox_id}")
        if row["state"] != "PENDING":
            raise LocalJournalError(
                "JOURNAL_OUTBOX_ALREADY_TERMINAL",
                "outbox item already has a terminal state",
                retryable=False,
            )
        old_attempts = cast(int, row["attempts"])
        old_available = ShanghaiTimestamp.parse(cast(str, row["available_at"]))
        created_at = ShanghaiTimestamp.parse(cast(str, row["created_at"]))
        exact_retry = (
            operation.attempts == old_attempts
            and operation.available_at == old_available
            and operation.reason_code == row["last_error_reason"]
        )
        if exact_retry:
            return
        if (
            operation.attempts <= old_attempts
            or operation.available_at < old_available
            or operation.available_at < created_at
        ):
            raise JournalConflictError("OUTBOX_RESCHEDULE", f"outbox_{operation.outbox_id}")
        connection.execute(
            """
            UPDATE outbox
            SET attempts = ?, available_at = ?, last_error_reason = ?
            WHERE outbox_id = ?
            """,
            (
                operation.attempts,
                operation.available_at.to_wire(),
                operation.reason_code,
                operation.outbox_id,
            ),
        )

    def _set_outbox_terminal(
        self,
        connection: sqlite3.Connection,
        outbox_id: int,
        content_hash: str,
        state: str,
        reason: str | None,
        terminal_at: ShanghaiTimestamp,
    ) -> None:
        row = connection.execute(
            "SELECT state, content_hash, terminal_reason FROM outbox WHERE outbox_id = ?",
            (outbox_id,),
        ).fetchone()
        if row is None:
            raise LocalJournalError(
                "JOURNAL_OUTBOX_NOT_FOUND", "outbox item does not exist", retryable=False
            )
        if row["content_hash"] != content_hash:
            raise JournalConflictError("OUTBOX_RECEIPT", f"outbox_{outbox_id}")
        if row["state"] == state and row["terminal_reason"] == reason:
            return
        if row["state"] != "PENDING":
            raise LocalJournalError(
                "JOURNAL_OUTBOX_ALREADY_TERMINAL",
                "outbox item already has a terminal state",
                retryable=False,
            )
        connection.execute(
            """
            UPDATE outbox SET state = ?, terminal_reason = ?, terminal_at = ?
            WHERE outbox_id = ?
            """,
            (state, reason, terminal_at.to_wire(), outbox_id),
        )

    def _put_control(self, connection: sqlite3.Connection, control: LocalControlRecord) -> None:
        self._require_account(control.account_id)
        values: dict[str, object] = {
            "account_id": control.account_id,
            "reason_code": control.reason_code,
            "created_at": control.created_at.to_wire(),
            "content_hash": control.document.content_hash,
            "body": control.document.body,
        }
        self._insert_or_compare(
            connection,
            table="local_controls",
            key_column="control_id",
            key_value=control.control_id,
            values=values,
            immutable_columns=tuple(values),
            record_type="LOCAL_CONTROL",
        )

    def _put_checkpoint(self, connection: sqlite3.Connection, checkpoint: CheckpointRecord) -> None:
        row = connection.execute(
            "SELECT value, revision FROM checkpoints WHERE namespace = ? AND key = ?",
            (checkpoint.namespace, checkpoint.key),
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO checkpoints(namespace, key, value, revision) VALUES (?, ?, ?, ?)",
                (checkpoint.namespace, checkpoint.key, checkpoint.value, checkpoint.revision),
            )
            return
        old_revision = cast(int, row["revision"])
        if checkpoint.revision == old_revision and row["value"] == checkpoint.value:
            return
        if checkpoint.revision <= old_revision:
            raise JournalConflictError("CHECKPOINT", checkpoint.key)
        connection.execute(
            "UPDATE checkpoints SET value = ?, revision = ? WHERE namespace = ? AND key = ?",
            (checkpoint.value, checkpoint.revision, checkpoint.namespace, checkpoint.key),
        )

    def _insert_or_compare(
        self,
        connection: sqlite3.Connection,
        *,
        table: str,
        key_column: str,
        key_value: object,
        values: Mapping[str, object],
        immutable_columns: Sequence[str],
        record_type: str,
        extra_key: tuple[str, object] | None = None,
    ) -> None:
        where = f"{key_column} = ?"
        key_values = [key_value]
        insert_values: dict[str, object] = {key_column: key_value, **values}
        record_id = str(key_value)
        if extra_key is not None:
            where += f" AND {extra_key[0]} = ?"
            key_values.append(extra_key[1])
            record_id = str(extra_key[1])
        row = connection.execute(f"SELECT * FROM {table} WHERE {where}", key_values).fetchone()
        if row is not None:
            if any(row[column] != values[column] for column in immutable_columns):
                raise JournalConflictError(record_type, record_id)
            return
        columns = tuple(insert_values)
        placeholders = ", ".join("?" for _ in columns)
        connection.execute(
            f"INSERT INTO {table}({', '.join(columns)}) VALUES ({placeholders})",
            tuple(insert_values[column] for column in columns),
        )

    def _backup(self, connection: sqlite3.Connection, destination: Path) -> MaintenanceResult:
        source = self._settings.database_path.resolve(strict=False)
        target = destination.resolve(strict=False)
        if target in {source, Path(f"{source}-wal"), Path(f"{source}-shm")}:
            raise LocalJournalError(
                "JOURNAL_BACKUP_TARGET_INVALID", "backup target equals journal", retryable=False
            )
        if target.exists():
            raise LocalJournalError(
                "JOURNAL_BACKUP_EXISTS", "backup target already exists", retryable=False
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.partial-{uuid4().hex}")
        backup: sqlite3.Connection | None = None
        try:
            backup = sqlite3.connect(temporary, isolation_level=None)
            connection.backup(backup)
            check = backup.execute("PRAGMA quick_check").fetchall()
            if len(check) != 1 or check[0][0] != "ok":
                raise LocalJournalError(
                    "JOURNAL_BACKUP_VERIFY_FAILED", "backup integrity check failed", retryable=False
                )
            backup.close()
            backup = None
            temporary.rename(target)
        except Exception:
            if backup is not None:
                with suppress(sqlite3.Error):
                    backup.close()
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise
        return MaintenanceResult(
            kind=MaintenanceKind.BACKUP,
            completed=True,
            bytes_written=target.stat().st_size,
            detail_code="BACKUP_COMPLETE",
        )

    def _require_open_owner(self) -> sqlite3.Connection:
        self._require_owner_thread()
        if self._status is not JournalStatus.OPEN or self._connection is None:
            raise LocalJournalError("JOURNAL_NOT_OPEN", "journal is not available", retryable=False)
        return self._connection

    def _require_owner_thread(self) -> None:
        if self._owner_thread_id is not None and threading.get_ident() != self._owner_thread_id:
            raise LocalJournalError(
                "JOURNAL_THREAD_OWNERSHIP_VIOLATION",
                "journal may only be used by its owner",
                retryable=False,
            )

    def _require_account(self, account_id: str) -> None:
        if account_id != self._settings.account_id:
            raise LocalJournalError(
                "JOURNAL_ACCOUNT_MISMATCH",
                "journal operation targets another account",
                retryable=False,
            )

    def _translate_error(self, error: Exception, *, operation: str) -> LocalJournalError:
        sqlite_code = getattr(error, "sqlite_errorcode", None)
        primary_code = sqlite_code & 0xFF if isinstance(sqlite_code, int) else None
        detail = str(error).lower()
        if primary_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or (
            primary_code is None and ("locked" in detail or "busy" in detail)
        ):
            return LocalJournalError(
                "JOURNAL_BUSY", f"journal {operation} is temporarily unavailable", retryable=True
            )
        if primary_code == sqlite3.SQLITE_FULL or "database or disk is full" in detail:
            return LocalJournalError(
                "JOURNAL_DISK_FULL", f"journal {operation} failed", retryable=False
            )
        if primary_code == sqlite3.SQLITE_READONLY or "readonly" in detail or "read-only" in detail:
            return LocalJournalError(
                "JOURNAL_READ_ONLY", f"journal {operation} is not writable", retryable=False
            )
        if primary_code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
            return LocalJournalError(
                "JOURNAL_CORRUPT", f"journal {operation} failed", retryable=False
            )
        if primary_code == sqlite3.SQLITE_IOERR or "disk i/o" in detail:
            return LocalJournalError(
                "JOURNAL_IO_FAILURE", f"journal {operation} failed", retryable=False
            )
        if isinstance(error, sqlite3.IntegrityError):
            return LocalJournalError(
                "JOURNAL_INTEGRITY_FAILURE", f"journal {operation} was rejected", retryable=False
            )
        return LocalJournalError(
            "JOURNAL_STORAGE_FAILURE", f"journal {operation} failed", retryable=False
        )

    def _handle_storage_error(self, error: Exception, *, operation: str) -> LocalJournalError:
        translated = self._translate_error(error, operation=operation)
        if not translated.retryable:
            self._fail_and_close()
        return translated

    def _rollback(self, connection: sqlite3.Connection) -> None:
        if connection.in_transaction:
            with suppress(sqlite3.Error):
                connection.execute("ROLLBACK")

    def _fail_and_close(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            with suppress(sqlite3.Error):
                connection.close()
        self._status = JournalStatus.FAILED


def _database_bytes(path: Path) -> int:
    return _file_bytes(path)


def _file_bytes(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _free_disk_bytes(path: Path) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def _document_string(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        _invalid_document_scope()
    return value


def _document_trading_date(payload: Mapping[str, Any], field: str) -> TradingDate:
    try:
        return TradingDate.parse(_document_string(payload, field))
    except ValueError:
        _invalid_document_scope()


def _document_timestamp(payload: Mapping[str, Any], field: str) -> ShanghaiTimestamp:
    try:
        return ShanghaiTimestamp.parse(_document_string(payload, field))
    except ValueError:
        _invalid_document_scope()


def _invalid_document_scope() -> NoReturn:
    raise LocalJournalError(
        "JOURNAL_DOCUMENT_SCOPE_INVALID",
        "document identity or recovery scope is invalid",
        retryable=False,
    )


def _document_from_row(row: sqlite3.Row) -> CanonicalJsonDocument:
    return CanonicalJsonDocument(
        document_id=cast(str, row["document_id"]),
        content_hash=cast(str, row["content_hash"]),
        body=cast(bytes, row["body"]),
    )


def _intent_from_row(row: sqlite3.Row) -> OrderIntentRecord:
    from auto_trading_system.journal.models import IntentSide

    return OrderIntentRecord(
        intent_id=cast(str, row["intent_id"]),
        client_order_id=cast(str, row["client_order_id"]),
        plan_id=cast(str, row["plan_id"]),
        account_id=cast(str, row["account_id"]),
        trade_date=TradingDate.parse(cast(str, row["trade_date"])),
        symbol=SecurityCode.parse(cast(str, row["symbol"])),
        side=IntentSide(cast(str, row["side"])),
        quantity=Quantity(cast(int, row["quantity"])),
        limit_price=Price.parse(cast(str, row["limit_price"])),
        max_notional=Money.parse(cast(str, row["max_notional"])),
        remark_token=cast(str, row["remark_token"]),
        execution_policy=cast(str, row["execution_policy"]),
        created_at=ShanghaiTimestamp.parse(cast(str, row["created_at"])),
    )


def _link_from_row(row: sqlite3.Row) -> BrokerOrderLink:
    return BrokerOrderLink(
        client_order_id=cast(str, row["client_order_id"]),
        remark_token=cast(str, row["remark_token"]),
        updated_at=ShanghaiTimestamp.parse(cast(str, row["updated_at"])),
        request_seq=cast(int | None, row["request_seq"]),
        broker_order_id=cast(str | None, row["broker_order_id"]),
        broker_order_sysid=cast(str | None, row["broker_order_sysid"]),
    )


def _trade_from_row(row: sqlite3.Row) -> BrokerTradeRecord:
    document = CanonicalJsonDocument(
        document_id=cast(str, row["trade_id"]),
        content_hash=cast(str, row["content_hash"]),
        body=cast(bytes, row["body"]),
    )
    return BrokerTradeRecord(
        trade_id=cast(str, row["trade_id"]),
        client_order_id=cast(str, row["client_order_id"]),
        account_id=cast(str, row["account_id"]),
        traded_at=ShanghaiTimestamp.parse(cast(str, row["traded_at"])),
        document=document,
    )


def _control_from_row(row: sqlite3.Row) -> LocalControlRecord:
    document = CanonicalJsonDocument(
        document_id=cast(str, row["control_id"]),
        content_hash=cast(str, row["content_hash"]),
        body=cast(bytes, row["body"]),
    )
    return LocalControlRecord(
        control_id=cast(str, row["control_id"]),
        account_id=cast(str, row["account_id"]),
        reason_code=cast(str, row["reason_code"]),
        created_at=ShanghaiTimestamp.parse(cast(str, row["created_at"])),
        document=document,
    )


def _checkpoint_from_row(row: sqlite3.Row) -> CheckpointRecord:
    return CheckpointRecord(
        namespace=cast(str, row["namespace"]),
        key=cast(str, row["key"]),
        value=cast(bytes, row["value"]),
        revision=cast(int, row["revision"]),
    )


def _outbox_from_row(row: sqlite3.Row) -> OutboxItem:
    state = OutboxState(cast(str, row["state"]))
    document = CanonicalJsonDocument(
        document_id=cast(str, row["document_id"]),
        content_hash=cast(str, row["content_hash"]),
        body=cast(bytes, row["body"]),
    )
    return OutboxItem(
        outbox_id=cast(int, row["outbox_id"]),
        kind=OutboxKind(cast(str, row["kind"])),
        state=state,
        document=document,
        attempts=cast(int, row["attempts"]),
        available_at=ShanghaiTimestamp.parse(cast(str, row["available_at"])),
        created_at=ShanghaiTimestamp.parse(cast(str, row["created_at"])),
    )


def _event_mapping(
    draft: EventDraft, *, local_seq: int, recorded_at: ShanghaiTimestamp
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "schema_version": "1.0",
        "event_id": draft.event_id,
        "node_id": draft.node_id,
        "account_id": draft.account_id,
        "boot_id": draft.boot_id,
        "local_seq": local_seq,
        "correlation_id": draft.correlation_id,
        "source": draft.source.value,
        "severity": draft.severity.value,
        "event_type": draft.event_type.value,
        "aggregate_type": draft.aggregate_type.value,
        "aggregate_id": draft.aggregate_id,
        "trade_date": draft.trade_date.to_wire(),
        "occurred_at": draft.occurred_at.to_wire(),
        "recorded_at": recorded_at.to_wire(),
        "payload": json.loads(draft.payload.decode("utf-8")),
    }
    if draft.causation_id is not None:
        event["causation_id"] = draft.causation_id
    return event


def _event_draft_hash(draft: EventDraft) -> str:
    payload = _operation_mapping(AppendDomainEvent(draft))
    digest = hashlib.sha256(canonical_json_bytes(payload, exclude=frozenset()))
    return f"sha256:{digest.hexdigest()}"


def _operation_mapping(operation: JournalOperation) -> dict[str, Any]:
    if isinstance(operation, CacheTradePlan):
        return {"type": "CacheTradePlan", "document": _document_mapping(operation.document)}
    if isinstance(operation, StoreCommand):
        return {
            "type": "StoreCommand",
            "command_seq": operation.command_seq,
            "document": _document_mapping(operation.document),
        }
    if isinstance(operation, PutOrderIntent):
        intent = operation.intent
        return {
            "type": "PutOrderIntent",
            "intent_id": intent.intent_id,
            "client_order_id": intent.client_order_id,
            "plan_id": intent.plan_id,
            "account_id": intent.account_id,
            "trade_date": intent.trade_date.to_wire(),
            "symbol": intent.symbol.to_wire(),
            "side": intent.side.value,
            "quantity": intent.quantity.value,
            "limit_price": intent.limit_price.to_wire(),
            "max_notional": intent.max_notional.to_wire(),
            "remark_token": intent.remark_token,
            "execution_policy": intent.execution_policy,
            "created_at": intent.created_at.to_wire(),
        }
    if isinstance(operation, UpsertBrokerOrderLink):
        link = operation.link
        return {
            "type": "UpsertBrokerOrderLink",
            "client_order_id": link.client_order_id,
            "remark_token": link.remark_token,
            "request_seq": link.request_seq,
            "broker_order_id": link.broker_order_id,
            "broker_order_sysid": link.broker_order_sysid,
            "updated_at": link.updated_at.to_wire(),
        }
    if isinstance(operation, RecordBrokerTrade):
        trade = operation.trade
        return {
            "type": "RecordBrokerTrade",
            "trade_id": trade.trade_id,
            "client_order_id": trade.client_order_id,
            "account_id": trade.account_id,
            "traded_at": trade.traded_at.to_wire(),
            "document": _document_mapping(trade.document),
        }
    if isinstance(operation, AppendDomainEvent):
        draft = operation.draft
        return {
            "type": "AppendDomainEvent",
            "event_id": draft.event_id,
            "node_id": draft.node_id,
            "account_id": draft.account_id,
            "boot_id": draft.boot_id,
            "correlation_id": draft.correlation_id,
            "causation_id": draft.causation_id,
            "source": draft.source.value,
            "severity": draft.severity.value,
            "event_type": draft.event_type.value,
            "aggregate_type": draft.aggregate_type.value,
            "aggregate_id": draft.aggregate_id,
            "trade_date": draft.trade_date.to_wire(),
            "occurred_at": draft.occurred_at.to_wire(),
            "payload": json.loads(draft.payload.decode("utf-8")),
        }
    if isinstance(operation, EnqueueOutbox):
        return {
            "type": "EnqueueOutbox",
            "kind": operation.kind.value,
            "document": _document_mapping(operation.document),
        }
    if isinstance(operation, RescheduleOutbox):
        return {
            "type": "RescheduleOutbox",
            "outbox_id": operation.outbox_id,
            "content_hash": operation.content_hash,
            "attempts": operation.attempts,
            "available_at": operation.available_at.to_wire(),
            "reason_code": operation.reason_code,
        }
    if isinstance(operation, AcknowledgeOutbox):
        return {
            "type": "AcknowledgeOutbox",
            "outbox_id": operation.outbox_id,
            "content_hash": operation.content_hash,
        }
    if isinstance(operation, DeadLetterOutbox):
        return {
            "type": "DeadLetterOutbox",
            "outbox_id": operation.outbox_id,
            "content_hash": operation.content_hash,
            "reason_code": operation.reason_code,
        }
    if isinstance(operation, PutLocalControl):
        control = operation.control
        return {
            "type": "PutLocalControl",
            "control_id": control.control_id,
            "account_id": control.account_id,
            "reason_code": control.reason_code,
            "created_at": control.created_at.to_wire(),
            "document": _document_mapping(control.document),
        }
    if isinstance(operation, PutCheckpoint):
        checkpoint = operation.checkpoint
        return {
            "type": "PutCheckpoint",
            "namespace": checkpoint.namespace,
            "key": checkpoint.key,
            "value": base64.b64encode(checkpoint.value).decode("ascii"),
            "revision": checkpoint.revision,
        }
    raise AssertionError("unsupported journal operation")  # pragma: no cover


def _document_mapping(document: CanonicalJsonDocument) -> dict[str, Any]:
    return {
        "document_id": document.document_id,
        "content_hash": document.content_hash,
        "body": document.to_mapping(),
    }


def _transaction_hash(transaction: JournalTransaction) -> str:
    payload = {
        "transaction_id": transaction.transaction_id,
        "operations": [_operation_mapping(operation) for operation in transaction.operations],
    }
    digest = hashlib.sha256(canonical_json_bytes(payload, exclude=frozenset()))
    return f"sha256:{digest.hexdigest()}"


def _commit_result_to_bytes(result: JournalCommitResult) -> bytes:
    return canonical_json_bytes(
        {
            "transaction_id": result.transaction_id,
            "commit_sequence": result.commit_sequence,
            "persisted_events": [
                {
                    "event_id": event.event_id,
                    "boot_id": event.boot_id,
                    "local_seq": event.local_seq,
                    "document": _document_mapping(event.document),
                }
                for event in result.persisted_events
            ],
        },
        exclude=frozenset(),
    )


def _commit_result_from_bytes(body: bytes) -> JournalCommitResult:
    payload = json.loads(body.decode("utf-8"))
    events = tuple(
        PersistedEvent(
            event_id=event["event_id"],
            boot_id=event["boot_id"],
            local_seq=event["local_seq"],
            document=CanonicalJsonDocument.from_mapping(
                event["document"]["document_id"],
                event["document"]["content_hash"],
                event["document"]["body"],
            ),
        )
        for event in payload["persisted_events"]
    )
    return JournalCommitResult(
        transaction_id=payload["transaction_id"],
        outcome=CommitOutcome.COMMITTED,
        commit_sequence=payload["commit_sequence"],
        persisted_events=events,
    )
