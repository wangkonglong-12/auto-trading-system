"""Blocking LocalJournal port owned exclusively by the Persistence Writer."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from auto_trading_system.domain import ShanghaiTimestamp, TradingDate
from auto_trading_system.journal.models import (
    JournalCommitResult,
    JournalHealth,
    JournalRecoverySnapshot,
    JournalTransaction,
    MaintenanceRequest,
    MaintenanceResult,
    OutboxItem,
)


class JournalStatus(StrEnum):
    NEW = "NEW"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


class LocalJournalError(RuntimeError):
    """Stable storage error; adapter details remain in protected diagnostics."""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(f"{code}: {message}")
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", code) is None:
            raise ValueError("journal error code is invalid")
        if type(retryable) is not bool:
            raise ValueError("retryable must be a boolean")
        self.code = code
        self.retryable = retryable


class JournalConflictError(LocalJournalError):
    def __init__(self, record_type: str, record_id: str) -> None:
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", record_type) is None:
            raise ValueError("record_type is invalid")
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{3,95}", record_id) is None:
            raise ValueError("record_id is invalid")
        super().__init__(
            "JOURNAL_IDEMPOTENCY_CONFLICT",
            "an existing identity has different immutable content",
            retryable=False,
        )
        self.record_type = record_type
        self.record_id = record_id


@dataclass(frozen=True, slots=True)
class JournalOpenResult:
    schema_version: int
    journal_id: str
    created: bool

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ValueError("schema_version must be a positive integer")
        if re.fullmatch(r"journal_[0-9a-f]{32}", self.journal_id) is None:
            raise ValueError("journal_id is invalid")
        if type(self.created) is not bool:
            raise ValueError("created must be a boolean")


@runtime_checkable
class LocalJournal(Protocol):
    """Storage-neutral contract called only by one dedicated Persistence Writer."""

    @property
    def status(self) -> JournalStatus: ...

    def open(self) -> JournalOpenResult: ...

    def commit(self, transaction: JournalTransaction) -> JournalCommitResult: ...

    def load_recovery(
        self, *, account_id: str, trade_date: TradingDate
    ) -> JournalRecoverySnapshot: ...

    def fetch_outbox(
        self, *, limit: int, available_at: ShanghaiTimestamp
    ) -> tuple[OutboxItem, ...]: ...

    def health(self) -> JournalHealth: ...

    def maintain(self, request: MaintenanceRequest) -> MaintenanceResult: ...

    def close(self) -> None: ...
