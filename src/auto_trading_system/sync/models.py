"""Typed delivery, pressure, and run-result values for the Durable Outbox."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from auto_trading_system.domain import ShanghaiTimestamp
from auto_trading_system.journal import MAX_STORED_SEQUENCE

MAX_OUTBOX_ATTEMPTS = 2_147_483_647
_CONTENT_HASH_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_REASON_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{2,63}")


def _require_reason(value: str, field: str) -> None:
    if not isinstance(value, str) or _REASON_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} has an invalid format")


class OutboxDeliveryDisposition(StrEnum):
    ACKNOWLEDGE = "ACKNOWLEDGE"
    RETRY = "RETRY"
    DEAD_LETTER = "DEAD_LETTER"


@dataclass(frozen=True, slots=True)
class OutboxDeliveryReceipt:
    outbox_id: int
    content_hash: str
    disposition: OutboxDeliveryDisposition
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if type(self.outbox_id) is not int or not 1 <= self.outbox_id <= MAX_STORED_SEQUENCE:
            raise ValueError("outbox_id must be a positive 64-bit integer")
        if (
            not isinstance(self.content_hash, str)
            or _CONTENT_HASH_PATTERN.fullmatch(self.content_hash) is None
        ):
            raise ValueError("content_hash has an invalid format")
        if not isinstance(self.disposition, OutboxDeliveryDisposition):
            raise ValueError("disposition must be an OutboxDeliveryDisposition")
        if self.disposition is OutboxDeliveryDisposition.ACKNOWLEDGE:
            if self.reason_code is not None:
                raise ValueError("acknowledgement cannot contain a reason_code")
        elif self.reason_code is None:
            raise ValueError("retry and dead letter receipts require a reason_code")
        else:
            _require_reason(self.reason_code, "reason_code")


class OutboxPressureLevel(StrEnum):
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class OutboxPressureSignal:
    level: OutboxPressureLevel
    pending: int
    high_watermark: int
    critical_watermark: int
    observed_at: ShanghaiTimestamp

    def __post_init__(self) -> None:
        if not isinstance(self.level, OutboxPressureLevel):
            raise ValueError("level must be an OutboxPressureLevel")
        if type(self.pending) is not int or not 0 <= self.pending <= MAX_STORED_SEQUENCE:
            raise ValueError("pending must be a non-negative 64-bit integer")
        if (
            type(self.high_watermark) is not int
            or type(self.critical_watermark) is not int
            or self.high_watermark < 1
            or self.critical_watermark <= self.high_watermark
            or self.critical_watermark > MAX_STORED_SEQUENCE
        ):
            raise ValueError("watermarks are invalid")
        if not isinstance(self.observed_at, ShanghaiTimestamp):
            raise ValueError("observed_at must be a ShanghaiTimestamp")
        expected = OutboxPressureLevel.NORMAL
        if self.pending >= self.critical_watermark:
            expected = OutboxPressureLevel.CRITICAL
        elif self.pending >= self.high_watermark:
            expected = OutboxPressureLevel.HIGH
        if self.level is not expected:
            raise ValueError("level does not match pending and watermarks")

    @property
    def blocks_new_buys(self) -> bool:
        return self.level is not OutboxPressureLevel.NORMAL


@dataclass(frozen=True, slots=True)
class OutboxWorkerSettings:
    batch_size: int = 50
    retry_base_seconds: int = 2
    retry_max_seconds: int = 300
    high_watermark: int = 50_000
    critical_watermark: int = 100_000

    def __post_init__(self) -> None:
        if type(self.batch_size) is not int or not 1 <= self.batch_size <= 50:
            raise ValueError("batch_size must be between 1 and 50")
        if type(self.retry_base_seconds) is not int or not 1 <= self.retry_base_seconds <= 300:
            raise ValueError("retry_base_seconds must be between 1 and 300")
        if (
            type(self.retry_max_seconds) is not int
            or not self.retry_base_seconds <= self.retry_max_seconds <= 86_400
        ):
            raise ValueError("retry_max_seconds must be between the base delay and 86400")
        if (
            type(self.high_watermark) is not int
            or type(self.critical_watermark) is not int
            or self.high_watermark < 1
            or self.critical_watermark <= self.high_watermark
            or self.critical_watermark > MAX_STORED_SEQUENCE
        ):
            raise ValueError("critical_watermark must exceed a positive high_watermark")

    def retry_delay_seconds(self, attempts: int) -> int:
        if type(attempts) is not int or not 1 <= attempts <= MAX_OUTBOX_ATTEMPTS:
            raise ValueError("attempts must be a positive 32-bit integer")
        exponent = min(attempts - 1, 62)
        return min(self.retry_base_seconds * (1 << exponent), self.retry_max_seconds)

    def pressure_level(self, pending: int) -> OutboxPressureLevel:
        if type(pending) is not int or not 0 <= pending <= MAX_STORED_SEQUENCE:
            raise ValueError("pending must be a non-negative 64-bit integer")
        if pending >= self.critical_watermark:
            return OutboxPressureLevel.CRITICAL
        if pending >= self.high_watermark:
            return OutboxPressureLevel.HIGH
        return OutboxPressureLevel.NORMAL


class OutboxRunStatus(StrEnum):
    IDLE = "IDLE"
    APPLIED = "APPLIED"
    TRANSPORT_FAILURE_RESCHEDULED = "TRANSPORT_FAILURE_RESCHEDULED"


@dataclass(frozen=True, slots=True)
class OutboxRunResult:
    status: OutboxRunStatus
    fetched: int
    acknowledged: int
    retried: int
    dead_lettered: int
    pending_before: int
    pending_after: int
    pressure_level: OutboxPressureLevel
    commit_sequence: int | None = None
    delivery_error_code: str | None = None
    delivery_error_retryable: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, OutboxRunStatus):
            raise ValueError("status must be an OutboxRunStatus")
        counts = (
            self.fetched,
            self.acknowledged,
            self.retried,
            self.dead_lettered,
            self.pending_before,
            self.pending_after,
        )
        if any(type(value) is not int or not 0 <= value <= MAX_STORED_SEQUENCE for value in counts):
            raise ValueError("outbox run counts must be non-negative 64-bit integers")
        if self.fetched > 50:
            raise ValueError("fetched cannot exceed the maximum batch size")
        if self.acknowledged + self.retried + self.dead_lettered != self.fetched:
            raise ValueError("delivery outcome counts must equal fetched")
        if not isinstance(self.pressure_level, OutboxPressureLevel):
            raise ValueError("pressure_level must be an OutboxPressureLevel")
        if self.status is OutboxRunStatus.IDLE:
            if self.fetched != 0 or self.commit_sequence is not None:
                raise ValueError("idle result cannot contain delivery work")
        elif type(self.commit_sequence) is not int or not 1 <= self.commit_sequence <= (
            MAX_STORED_SEQUENCE
        ):
            raise ValueError("applied result requires a positive commit_sequence")
        if self.status is OutboxRunStatus.TRANSPORT_FAILURE_RESCHEDULED:
            if self.retried != self.fetched or self.delivery_error_code is None:
                raise ValueError("transport failure must reschedule the complete batch")
        elif self.delivery_error_code is not None or self.delivery_error_retryable is not None:
            raise ValueError("only a transport failure can include delivery error details")
        if self.delivery_error_code is not None:
            _require_reason(self.delivery_error_code, "delivery_error_code")
            if type(self.delivery_error_retryable) is not bool:
                raise ValueError("delivery_error_retryable must be a boolean")


class OutboxDeliveryError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        _require_reason(code, "code")
        if type(retryable) is not bool:
            raise ValueError("retryable must be a boolean")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.retryable = retryable


class OutboxWorkerError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        _require_reason(code, "code")
        if type(retryable) is not bool:
            raise ValueError("retryable must be a boolean")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.retryable = retryable
