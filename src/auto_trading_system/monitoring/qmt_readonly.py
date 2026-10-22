"""Read-only QMT field probe orchestration and immutable evidence output."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from auto_trading_system.domain import Clock, MonotonicClock, SystemClock

_MAX_EVIDENCE_ITEMS = 256
_MAX_REPORT_BYTES = 1_048_576
_ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_ACCOUNT_STATUS_NAMES = {
    -1: "INVALID",
    0: "OK",
    1: "WAITING_LOGIN",
    2: "LOGGING_IN",
    3: "FAIL",
    4: "INITIALIZING",
    5: "CORRECTING",
    6: "CLOSED",
    7: "ASSIST_CONNECTION_FAILED",
    8: "DISABLED_BY_SYSTEM",
    9: "DISABLED_BY_USER",
}


class QmtReadOnlyAdapterError(RuntimeError):
    """Stable adapter failure without SDK values or account identifiers."""

    def __init__(self, code: str, message: str) -> None:
        if not isinstance(code, str) or _ERROR_CODE_PATTERN.fullmatch(code) is None:
            code = "QMT_ADAPTER_ERROR_INVALID"
            message = "adapter supplied an invalid error code"
        super().__init__(f"{code}: {message}")
        self.code = code


class QmtProbeOutputError(RuntimeError):
    """Stable evidence output failure without local content or paths."""

    def __init__(self, code: str, message: str) -> None:
        if not isinstance(code, str) or _ERROR_CODE_PATTERN.fullmatch(code) is None:
            code = "QMT_OUTPUT_ERROR_INVALID"
            message = "output boundary supplied an invalid error code"
        super().__init__(f"{code}: {message}")
        self.code = code


class QmtSignalKind(StrEnum):
    ACCOUNT_STATUS = "ACCOUNT_STATUS"
    DISCONNECTED = "DISCONNECTED"
    INVALID_ACCOUNT_STATUS = "INVALID_ACCOUNT_STATUS"


class QmtProbeOutcome(StrEnum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class QmtAccountStatus:
    """One target-account status with the raw account identifier removed."""

    account_type: int
    status: int

    def __post_init__(self) -> None:
        if type(self.account_type) is not int or type(self.status) is not int:
            raise TypeError("QMT account status fields must be integers")


@dataclass(frozen=True, slots=True)
class QmtStatusQuery:
    """Sanitized result of one SDK-wide account status query."""

    total_account_count: int
    target_statuses: tuple[QmtAccountStatus, ...]

    def __post_init__(self) -> None:
        if type(self.total_account_count) is not int or self.total_account_count < 0:
            raise ValueError("QMT account count must be a non-negative integer")
        if len(self.target_statuses) > self.total_account_count:
            raise ValueError("target status count cannot exceed total account count")


@dataclass(frozen=True, slots=True)
class QmtSignal:
    """Minimal immutable callback copy accepted by the probe collector."""

    kind: QmtSignalKind
    account_type: int | None = None
    status: int | None = None

    def __post_init__(self) -> None:
        if self.kind is QmtSignalKind.ACCOUNT_STATUS:
            if type(self.account_type) is not int or type(self.status) is not int:
                raise TypeError("account status signal requires integer fields")
        elif self.account_type is not None or self.status is not None:
            raise ValueError("non-status signal cannot contain account status fields")


QmtSignalSink = Callable[[QmtSignal], None]


class QmtReadOnlyClient(Protocol):
    """Only the XtQuant operations authorized for the QMT-001 field probe."""

    @property
    def sdk_version(self) -> str | None: ...

    def register_signal_sink(self, sink: QmtSignalSink) -> None: ...

    def start(self) -> None: ...

    def connect(self) -> int: ...

    def subscribe(self) -> int: ...

    def query_account_statuses(self) -> QmtStatusQuery: ...

    def unsubscribe(self) -> int: ...

    def stop(self) -> None: ...


class QmtProbeClock(Clock, MonotonicClock, Protocol):
    """Combined wall and elapsed-time clock required by field evidence."""


QmtReadOnlyClientFactory = Callable[["QmtProbeConfig", str], QmtReadOnlyClient]


@dataclass(frozen=True, slots=True)
class QmtProbeConfig:
    """Validated field-probe parameters with no account identifier."""

    userdata_mini: Path
    session_id: int
    account_type: str
    observation_seconds: int = 30
    query_interval_milliseconds: int = 1_000

    def __post_init__(self) -> None:
        if not isinstance(self.userdata_mini, Path) or not self.userdata_mini.is_absolute():
            raise ValueError("userdata_mini must be an absolute path")
        if self.userdata_mini.name.casefold() != "userdata_mini":
            raise ValueError("userdata_mini path must end with userdata_mini")
        if not self.userdata_mini.is_dir():
            raise ValueError("userdata_mini directory does not exist")
        if not (self.userdata_mini / "up_queue_xtquant").exists():
            raise ValueError("function trading queue does not exist")
        if type(self.session_id) is not int or not 1 <= self.session_id <= 2_147_483_647:
            raise ValueError("session_id is outside the supported range")
        if self.account_type != "STOCK":
            raise ValueError("the initial field probe supports STOCK accounts only")
        if type(self.observation_seconds) is not int or not 1 <= self.observation_seconds <= 300:
            raise ValueError("observation_seconds must be between 1 and 300")
        if (
            type(self.query_interval_milliseconds) is not int
            or not 100 <= self.query_interval_milliseconds <= 5_000
            or self.query_interval_milliseconds > self.observation_seconds * 1_000
        ):
            raise ValueError("query interval is outside the supported observation window")


@dataclass(frozen=True, slots=True)
class QmtProbePhaseEvidence:
    phase: str
    elapsed_milliseconds: int
    result_code: int | None = None

    def to_payload(self) -> dict[str, object]:
        result: dict[str, object] = {
            "phase": self.phase,
            "elapsed_milliseconds": self.elapsed_milliseconds,
        }
        if self.result_code is not None:
            result["result_code"] = self.result_code
        return result


@dataclass(frozen=True, slots=True)
class QmtProbeStatusEvidence:
    source: str
    elapsed_milliseconds: int
    account_type: int
    status: int

    def to_payload(self) -> dict[str, object]:
        return {
            "source": self.source,
            "elapsed_milliseconds": self.elapsed_milliseconds,
            "account_type": self.account_type,
            "status": self.status,
            "status_name": _ACCOUNT_STATUS_NAMES.get(self.status, "UNMAPPED"),
        }


@dataclass(frozen=True, slots=True)
class QmtProbeReport:
    """Complete sanitized QMT-001 evidence payload."""

    started_at: str
    completed_at: str
    duration_milliseconds: int
    outcome: QmtProbeOutcome
    failure_code: str | None
    primary_failure_code: str | None
    cleanup_failure_codes: tuple[str, ...]
    config_hash: str
    account_fingerprint: str
    masked_account: str
    userdata_mini_fingerprint: str
    session_id: int
    account_type: str
    sdk_version: str | None
    connect_result: int | None
    subscribe_result: int | None
    unsubscribe_result: int | None
    status_query_count: int
    maximum_reported_account_count: int
    disconnect_callback_count: int
    invalid_callback_count: int
    phase_evidence: tuple[QmtProbePhaseEvidence, ...]
    status_evidence: tuple[QmtProbeStatusEvidence, ...]
    evidence_truncated: bool

    @property
    def exit_code(self) -> int:
        if self.outcome is QmtProbeOutcome.PASS:
            return 0
        if self.outcome is QmtProbeOutcome.BLOCKED:
            return 2
        return 3

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": "qmt-readonly-probe/v1",
            "collection_mode": "READ_ONLY",
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_milliseconds": self.duration_milliseconds,
            "outcome": self.outcome.value,
            "failure_code": self.failure_code,
            "primary_failure_code": self.primary_failure_code,
            "cleanup_failure_codes": list(self.cleanup_failure_codes),
            "config_hash": self.config_hash,
            "target_account": {
                "fingerprint": self.account_fingerprint,
                "masked": self.masked_account,
                "type": self.account_type,
            },
            "qmt": {
                "userdata_mini_fingerprint": self.userdata_mini_fingerprint,
                "session_id": self.session_id,
                "sdk_version": self.sdk_version,
                "connect_result": self.connect_result,
                "subscribe_result": self.subscribe_result,
                "unsubscribe_result": self.unsubscribe_result,
            },
            "status": {
                "query_count": self.status_query_count,
                "maximum_reported_account_count": self.maximum_reported_account_count,
                "disconnect_callback_count": self.disconnect_callback_count,
                "invalid_callback_count": self.invalid_callback_count,
                "observations": [item.to_payload() for item in self.status_evidence],
            },
            "phases": [item.to_payload() for item in self.phase_evidence],
            "evidence_truncated": self.evidence_truncated,
            "privacy": {
                "full_broker_account_omitted": True,
                "userdata_path_omitted": True,
                "sdk_objects_omitted": True,
                "exception_messages_omitted": True,
            },
        }


class _EvidenceCollector:
    def __init__(self, *, clock: MonotonicClock, started_ns: int) -> None:
        self._clock = clock
        self._started_ns = started_ns
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._statuses: list[QmtProbeStatusEvidence] = []
        self._last_by_source: dict[str, tuple[int, int]] = {}
        self._disconnect_count = 0
        self._invalid_count = 0
        self._truncated = False

    def elapsed_milliseconds(self) -> int:
        return max(0, (self._clock.monotonic_ns() - self._started_ns) // 1_000_000)

    def accept_signal(self, signal: QmtSignal) -> None:
        should_wake = False
        with self._lock:
            if signal.kind is QmtSignalKind.DISCONNECTED:
                self._disconnect_count += 1
                should_wake = True
            elif signal.kind is QmtSignalKind.INVALID_ACCOUNT_STATUS:
                self._invalid_count += 1
                should_wake = True
            else:
                assert signal.account_type is not None
                assert signal.status is not None
                self._append_status_locked(
                    source="CALLBACK",
                    account_type=signal.account_type,
                    status=signal.status,
                )
        if should_wake:
            self._wake.set()

    def accept_query_status(self, status: QmtAccountStatus) -> None:
        with self._lock:
            self._append_status_locked(
                source="QUERY",
                account_type=status.account_type,
                status=status.status,
            )

    def _append_status_locked(self, *, source: str, account_type: int, status: int) -> None:
        current = (account_type, status)
        if self._last_by_source.get(source) == current:
            return
        self._last_by_source[source] = current
        if len(self._statuses) >= _MAX_EVIDENCE_ITEMS:
            self._truncated = True
            return
        self._statuses.append(
            QmtProbeStatusEvidence(
                source=source,
                elapsed_milliseconds=self.elapsed_milliseconds(),
                account_type=account_type,
                status=status,
            )
        )

    def wait(self, timeout_seconds: float) -> None:
        self._wake.wait(timeout_seconds)
        self._wake.clear()

    def snapshot(self) -> tuple[tuple[QmtProbeStatusEvidence, ...], int, int, bool]:
        with self._lock:
            return (
                tuple(self._statuses),
                self._disconnect_count,
                self._invalid_count,
                self._truncated,
            )


def _masked_account(value: str) -> str:
    revealed = min(4, max(0, len(value) - 4))
    if revealed == 0:
        return "*" * len(value)
    return f"{'*' * (len(value) - revealed)}{value[-revealed:]}"


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def run_qmt_readonly_probe(
    config: QmtProbeConfig,
    *,
    account_id: str,
    config_hash: str,
    client_factory: QmtReadOnlyClientFactory,
    clock: QmtProbeClock | None = None,
    wait: Callable[[float], None] | None = None,
) -> QmtProbeReport:
    """Execute only connect, account subscribe, status query and cleanup operations."""
    effective_clock = SystemClock() if clock is None else clock
    started_at = effective_clock.now().to_wire()
    started_ns = effective_clock.monotonic_ns()
    userdata_mini_fingerprint = _fingerprint(
        str(config.userdata_mini.resolve(strict=False)).casefold()
    )
    collector = _EvidenceCollector(clock=effective_clock, started_ns=started_ns)
    effective_wait = collector.wait if wait is None else wait
    phases: list[QmtProbePhaseEvidence] = []
    evidence_truncated = False

    def record_phase(phase: str, result_code: int | None = None) -> None:
        nonlocal evidence_truncated
        if len(phases) >= _MAX_EVIDENCE_ITEMS:
            evidence_truncated = True
            return
        phases.append(
            QmtProbePhaseEvidence(
                phase=phase,
                elapsed_milliseconds=collector.elapsed_milliseconds(),
                result_code=result_code,
            )
        )

    client: QmtReadOnlyClient | None = None
    sdk_version: str | None = None
    subscribed = False
    connect_result: int | None = None
    subscribe_result: int | None = None
    unsubscribe_result: int | None = None
    status_query_count = 0
    maximum_reported_account_count = 0
    outcome = QmtProbeOutcome.ERROR
    failure_code: str | None = "QMT_PROBE_NOT_COMPLETED"
    cleanup_failures: list[str] = []

    try:
        client = client_factory(config, account_id)
        sdk_version = client.sdk_version
        client.register_signal_sink(collector.accept_signal)
        record_phase("CALLBACK_REGISTERED")

        client.start()
        record_phase("TRADER_STARTED")

        connect_result = client.connect()
        record_phase("CONNECT_RETURNED", connect_result)
        if connect_result != 0:
            outcome = QmtProbeOutcome.BLOCKED
            failure_code = "QMT_CONNECT_FAILED"
        else:
            subscribe_result = client.subscribe()
            record_phase("SUBSCRIBE_RETURNED", subscribe_result)
            if subscribe_result != 0:
                outcome = QmtProbeOutcome.BLOCKED
                failure_code = "QMT_SUBSCRIBE_FAILED"
            else:
                subscribed = True
                deadline_ns = (
                    effective_clock.monotonic_ns() + config.observation_seconds * 1_000_000_000
                )
                saw_target_status = False
                while True:
                    _, disconnects, invalid_callbacks, _ = collector.snapshot()
                    if disconnects:
                        outcome = QmtProbeOutcome.BLOCKED
                        failure_code = "QMT_DISCONNECTED"
                        break
                    if invalid_callbacks:
                        outcome = QmtProbeOutcome.ERROR
                        failure_code = "QMT_CALLBACK_PAYLOAD_INVALID"
                        break

                    query = client.query_account_statuses()
                    status_query_count += 1
                    maximum_reported_account_count = max(
                        maximum_reported_account_count,
                        query.total_account_count,
                    )
                    record_phase("ACCOUNT_STATUS_QUERY_RETURNED", query.total_account_count)
                    for status in query.target_statuses:
                        saw_target_status = True
                        collector.accept_query_status(status)

                    _, disconnects, invalid_callbacks, _ = collector.snapshot()
                    if disconnects:
                        outcome = QmtProbeOutcome.BLOCKED
                        failure_code = "QMT_DISCONNECTED"
                        break
                    if invalid_callbacks:
                        outcome = QmtProbeOutcome.ERROR
                        failure_code = "QMT_CALLBACK_PAYLOAD_INVALID"
                        break
                    if any(status.status == 0 for status in query.target_statuses):
                        outcome = QmtProbeOutcome.PASS
                        failure_code = None
                        break

                    now_ns = effective_clock.monotonic_ns()
                    if now_ns >= deadline_ns:
                        outcome = QmtProbeOutcome.BLOCKED
                        failure_code = (
                            "QMT_ACCOUNT_NOT_OK" if saw_target_status else "QMT_ACCOUNT_NOT_FOUND"
                        )
                        break
                    remaining_seconds = (deadline_ns - now_ns) / 1_000_000_000
                    interval_seconds = config.query_interval_milliseconds / 1_000
                    effective_wait(min(interval_seconds, remaining_seconds))
    except QmtReadOnlyAdapterError as exc:
        outcome = QmtProbeOutcome.ERROR
        failure_code = exc.code
    except Exception:
        outcome = QmtProbeOutcome.ERROR
        failure_code = "QMT_PROBE_UNEXPECTED_FAILURE"
    finally:
        primary_failure_code = failure_code
        if client is not None and subscribed:
            try:
                unsubscribe_result = client.unsubscribe()
                record_phase("UNSUBSCRIBE_RETURNED", unsubscribe_result)
                if unsubscribe_result != 0:
                    cleanup_failures.append("QMT_UNSUBSCRIBE_FAILED")
            except QmtReadOnlyAdapterError as exc:
                cleanup_failures.append(exc.code)
            except Exception:
                cleanup_failures.append("QMT_UNSUBSCRIBE_UNEXPECTED_FAILURE")
        if client is not None:
            try:
                client.stop()
                record_phase("TRADER_STOPPED")
            except QmtReadOnlyAdapterError as exc:
                cleanup_failures.append(exc.code)
            except Exception:
                cleanup_failures.append("QMT_STOP_UNEXPECTED_FAILURE")
        if cleanup_failures:
            outcome = QmtProbeOutcome.ERROR
            failure_code = "QMT_CLEANUP_FAILED"

    statuses, disconnect_count, invalid_count, collector_truncated = collector.snapshot()
    completed_at = effective_clock.now().to_wire()
    duration_milliseconds = collector.elapsed_milliseconds()
    return QmtProbeReport(
        started_at=started_at,
        completed_at=completed_at,
        duration_milliseconds=duration_milliseconds,
        outcome=outcome,
        failure_code=failure_code,
        primary_failure_code=primary_failure_code if cleanup_failures else None,
        cleanup_failure_codes=tuple(cleanup_failures),
        config_hash=config_hash,
        account_fingerprint=_fingerprint(account_id),
        masked_account=_masked_account(account_id),
        userdata_mini_fingerprint=userdata_mini_fingerprint,
        session_id=config.session_id,
        account_type=config.account_type,
        sdk_version=sdk_version,
        connect_result=connect_result,
        subscribe_result=subscribe_result,
        unsubscribe_result=unsubscribe_result,
        status_query_count=status_query_count,
        maximum_reported_account_count=maximum_reported_account_count,
        disconnect_callback_count=disconnect_count,
        invalid_callback_count=invalid_count,
        phase_evidence=tuple(phases),
        status_evidence=statuses,
        evidence_truncated=(evidence_truncated or collector_truncated),
    )


def write_qmt_probe_report(
    destination: Path,
    report: QmtProbeReport,
    *,
    forbidden_values: Iterable[str] = (),
) -> str:
    """Atomically create one immutable report and return its SHA-256."""
    return write_immutable_probe_payload(
        destination,
        report.to_payload(),
        forbidden_values=forbidden_values,
    )


def write_immutable_probe_payload(
    destination: Path,
    payload_value: Mapping[str, object],
    *,
    forbidden_values: Iterable[str] = (),
) -> str:
    """Atomically create one bounded JSON evidence file and return its SHA-256."""
    validate_qmt_probe_destination(destination)

    payload = (
        json.dumps(
            payload_value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if len(payload) > _MAX_REPORT_BYTES:
        raise QmtProbeOutputError("QMT_PROBE_OUTPUT_TOO_LARGE", "report exceeds 1 MiB")
    for value in forbidden_values:
        if value and (value.encode("utf-8") in payload or value.encode("utf-16-le") in payload):
            raise QmtProbeOutputError(
                "QMT_PROBE_REDACTION_FAILED",
                "a protected value remained in the report",
            )

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=".qmt-readonly-probe-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.link(temporary_path, destination)
        except FileExistsError as exc:
            raise QmtProbeOutputError(
                "QMT_PROBE_OUTPUT_EXISTS",
                "report destination already exists",
            ) from exc
        temporary_path.unlink()
        temporary_path = None
        return hashlib.sha256(payload).hexdigest()
    except QmtProbeOutputError:
        raise
    except OSError as exc:
        raise QmtProbeOutputError(
            "QMT_PROBE_OUTPUT_WRITE_FAILED",
            "report could not be written",
        ) from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def validate_qmt_probe_destination(destination: Path) -> None:
    """Reject an unsafe or non-immutable destination before opening a QMT session."""
    if not isinstance(destination, Path) or not destination.is_absolute():
        raise QmtProbeOutputError(
            "QMT_PROBE_OUTPUT_INVALID",
            "report destination must be an absolute path",
        )
    if destination.suffix.casefold() != ".json":
        raise QmtProbeOutputError(
            "QMT_PROBE_OUTPUT_INVALID",
            "report destination must use the .json extension",
        )
    if not destination.parent.is_dir() or destination.parent.is_symlink():
        raise QmtProbeOutputError(
            "QMT_PROBE_OUTPUT_INVALID",
            "report destination parent is invalid",
        )
    if destination.exists():
        raise QmtProbeOutputError(
            "QMT_PROBE_OUTPUT_EXISTS",
            "report destination already exists",
        )
