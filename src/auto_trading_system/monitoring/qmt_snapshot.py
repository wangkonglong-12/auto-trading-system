"""QMT-002 read-only account-query field probe and privacy-safe evidence."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from auto_trading_system.domain import SystemClock
from auto_trading_system.monitoring.qmt_readonly import (
    QmtAccountStatus,
    QmtProbeClock,
    QmtProbeConfig,
    QmtProbeOutcome,
    QmtProbePhaseEvidence,
    QmtProbeStatusEvidence,
    QmtReadOnlyAdapterError,
    QmtReadOnlyClient,
    QmtSignal,
    QmtSignalKind,
    write_immutable_probe_payload,
)

_FIELD_NAME_MAXIMUM = 64
_MAX_DATASET_RECORDS = 10_000
_MAX_STATUS_EVIDENCE = 256


class QmtSnapshotDataset(StrEnum):
    ASSET = "ASSET"
    POSITIONS = "POSITIONS"
    ORDERS = "ORDERS"
    TRADES = "TRADES"


class QmtSnapshotResultKind(StrEnum):
    OBJECT = "OBJECT"
    LIST = "LIST"
    EMPTY_LIST = "EMPTY_LIST"
    ASSET_NONE = "ASSET_NONE"
    AMBIGUOUS_NONE = "AMBIGUOUS_NONE"


@dataclass(frozen=True, slots=True)
class QmtFieldShape:
    """Aggregate field metadata with every source value removed."""

    name: str
    required: bool
    observed_types: tuple[str, ...]
    missing_count: int
    none_count: int
    invalid_count: int
    non_finite_count: int
    negative_count: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or not 1 <= len(self.name) <= _FIELD_NAME_MAXIMUM
            or not self.name.replace("_", "").isalnum()
        ):
            raise ValueError("QMT field name is invalid")
        if not isinstance(self.required, bool):
            raise TypeError("QMT required marker must be boolean")
        allowed_types = {"bool", "float", "int", "none", "other", "str"}
        if (
            not isinstance(self.observed_types, tuple)
            or tuple(sorted(set(self.observed_types))) != self.observed_types
            or any(item not in allowed_types for item in self.observed_types)
        ):
            raise ValueError("QMT observed field types are invalid")
        for value in (
            self.missing_count,
            self.none_count,
            self.invalid_count,
            self.non_finite_count,
            self.negative_count,
        ):
            if type(value) is not int or value < 0:
                raise ValueError("QMT field counters must be non-negative integers")

    @property
    def structurally_valid(self) -> bool:
        return (not self.required or self.missing_count == 0) and self.invalid_count == 0

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "required": self.required,
            "observed_types": list(self.observed_types),
            "missing_count": self.missing_count,
            "none_count": self.none_count,
            "invalid_count": self.invalid_count,
            "non_finite_count": self.non_finite_count,
            "negative_count": self.negative_count,
        }


@dataclass(frozen=True, slots=True)
class QmtDatasetObservation:
    """One query result reduced to field shape and aggregate counters."""

    dataset: QmtSnapshotDataset
    result_kind: QmtSnapshotResultKind
    record_count: int
    fields: tuple[QmtFieldShape, ...]
    duplicate_identity_count: int = 0

    def __post_init__(self) -> None:
        if type(self.record_count) is not int or not 0 <= self.record_count <= _MAX_DATASET_RECORDS:
            raise ValueError("QMT snapshot record count is outside the bounded range")
        if type(self.duplicate_identity_count) is not int or self.duplicate_identity_count < 0:
            raise ValueError("QMT duplicate identity count is invalid")
        if not isinstance(self.fields, tuple) or len({field.name for field in self.fields}) != len(
            self.fields
        ):
            raise ValueError("QMT snapshot fields must be a unique tuple")
        if self.record_count > 0 and not self.fields:
            raise ValueError("non-empty QMT observations require field evidence")
        if self.dataset is QmtSnapshotDataset.ASSET:
            if self.result_kind not in {
                QmtSnapshotResultKind.OBJECT,
                QmtSnapshotResultKind.ASSET_NONE,
            }:
                raise ValueError("asset observation has an incompatible result kind")
        elif self.result_kind not in {
            QmtSnapshotResultKind.LIST,
            QmtSnapshotResultKind.EMPTY_LIST,
            QmtSnapshotResultKind.AMBIGUOUS_NONE,
        }:
            raise ValueError("list observation has an incompatible result kind")
        if (
            self.result_kind
            in {
                QmtSnapshotResultKind.ASSET_NONE,
                QmtSnapshotResultKind.AMBIGUOUS_NONE,
                QmtSnapshotResultKind.EMPTY_LIST,
            }
            and self.record_count != 0
        ):
            raise ValueError("empty QMT result kinds cannot contain records")
        if self.result_kind is QmtSnapshotResultKind.OBJECT and self.record_count != 1:
            raise ValueError("QMT object observation must contain exactly one record")

    @property
    def structurally_valid(self) -> bool:
        return self.duplicate_identity_count == 0 and all(
            field.structurally_valid for field in self.fields
        )

    @property
    def is_indeterminate(self) -> bool:
        return self.result_kind in {
            QmtSnapshotResultKind.ASSET_NONE,
            QmtSnapshotResultKind.AMBIGUOUS_NONE,
        }

    def to_payload(self) -> dict[str, object]:
        return {
            "dataset": self.dataset.value,
            "result_kind": self.result_kind.value,
            "record_count": self.record_count,
            "duplicate_identity_count": self.duplicate_identity_count,
            "structurally_valid": self.structurally_valid,
            "fields": [field.to_payload() for field in self.fields],
        }


class QmtSnapshotReadOnlyClient(QmtReadOnlyClient, Protocol):
    """QMT-001 lifecycle plus the four queries authorized by QMT-002."""

    def query_asset(self) -> QmtDatasetObservation: ...

    def query_positions(self) -> QmtDatasetObservation: ...

    def query_orders(self) -> QmtDatasetObservation: ...

    def query_trades(self) -> QmtDatasetObservation: ...


QmtSnapshotClientFactory = Callable[[QmtProbeConfig, str], QmtSnapshotReadOnlyClient]


@dataclass(frozen=True, slots=True)
class QmtSnapshotProbeReport:
    started_at: str
    completed_at: str
    duration_milliseconds: int
    outcome: QmtProbeOutcome
    failure_code: str | None
    primary_failure_code: str | None
    cleanup_failure_codes: tuple[str, ...]
    config_hash: str
    account_fingerprint: str
    userdata_mini_fingerprint: str
    session_id: int
    account_type: str
    sdk_version: str | None
    connect_result: int | None
    subscribe_result: int | None
    unsubscribe_result: int | None
    status_query_count: int
    phases: tuple[QmtProbePhaseEvidence, ...]
    statuses: tuple[QmtProbeStatusEvidence, ...]
    datasets: tuple[QmtDatasetObservation, ...]
    disconnect_callback_count: int
    invalid_callback_count: int
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
            "schema": "qmt-snapshot-probe/v1",
            "collection_mode": "READ_ONLY",
            "work_item": "QMT-002",
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_milliseconds": self.duration_milliseconds,
            "outcome": self.outcome.value,
            "failure_code": self.failure_code,
            "primary_failure_code": self.primary_failure_code,
            "cleanup_failure_codes": list(self.cleanup_failure_codes),
            "config_hash": self.config_hash,
            "target_account_fingerprint": self.account_fingerprint,
            "qmt": {
                "userdata_mini_fingerprint": self.userdata_mini_fingerprint,
                "session_id": self.session_id,
                "account_type": self.account_type,
                "sdk_version": self.sdk_version,
                "connect_result": self.connect_result,
                "subscribe_result": self.subscribe_result,
                "unsubscribe_result": self.unsubscribe_result,
            },
            "status": {
                "query_count": self.status_query_count,
                "disconnect_callback_count": self.disconnect_callback_count,
                "invalid_callback_count": self.invalid_callback_count,
                "observations": [item.to_payload() for item in self.statuses],
            },
            "datasets": [item.to_payload() for item in self.datasets],
            "phases": [item.to_payload() for item in self.phases],
            "evidence_truncated": self.evidence_truncated,
            "privacy": {
                "full_broker_account_omitted": True,
                "security_codes_omitted": True,
                "order_and_trade_identifiers_omitted": True,
                "financial_values_omitted": True,
                "userdata_path_omitted": True,
                "sdk_objects_omitted": True,
                "exception_messages_omitted": True,
            },
        }


class _SignalCollector:
    def __init__(self, *, clock: QmtProbeClock, started_ns: int) -> None:
        self._clock = clock
        self._started_ns = started_ns
        self._lock = threading.Lock()
        self._disconnects = 0
        self._invalid = 0
        self._latest_status: tuple[int, int] | None = None
        self._statuses: list[QmtProbeStatusEvidence] = []
        self._truncated = False

    def elapsed_milliseconds(self) -> int:
        return max(0, (self._clock.monotonic_ns() - self._started_ns) // 1_000_000)

    def accept(self, signal: QmtSignal) -> None:
        with self._lock:
            if signal.kind is QmtSignalKind.DISCONNECTED:
                self._disconnects += 1
            elif signal.kind is QmtSignalKind.INVALID_ACCOUNT_STATUS:
                self._invalid += 1
            else:
                assert signal.account_type is not None and signal.status is not None
                current = (signal.account_type, signal.status)
                self._latest_status = current
                if (
                    not self._statuses
                    or (
                        self._statuses[-1].account_type,
                        self._statuses[-1].status,
                    )
                    != current
                ):
                    if len(self._statuses) >= _MAX_STATUS_EVIDENCE:
                        self._truncated = True
                    else:
                        self._statuses.append(
                            QmtProbeStatusEvidence(
                                source="CALLBACK",
                                elapsed_milliseconds=self.elapsed_milliseconds(),
                                account_type=signal.account_type,
                                status=signal.status,
                            )
                        )

    def accept_query(self, status: QmtAccountStatus) -> None:
        with self._lock:
            if len(self._statuses) >= _MAX_STATUS_EVIDENCE:
                self._truncated = True
            else:
                self._statuses.append(
                    QmtProbeStatusEvidence(
                        source="QUERY",
                        elapsed_milliseconds=self.elapsed_milliseconds(),
                        account_type=status.account_type,
                        status=status.status,
                    )
                )

    def failure_code(self) -> str | None:
        with self._lock:
            if self._disconnects:
                return "QMT_SNAPSHOT_DISCONNECTED"
            if self._invalid:
                return "QMT_SNAPSHOT_CALLBACK_INVALID"
            if self._latest_status is not None and self._latest_status[1] != 0:
                return "QMT_SNAPSHOT_ACCOUNT_NOT_OK"
            return None

    def snapshot(self) -> tuple[tuple[QmtProbeStatusEvidence, ...], int, int, bool]:
        with self._lock:
            return tuple(self._statuses), self._disconnects, self._invalid, self._truncated


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def run_qmt_snapshot_probe(
    config: QmtProbeConfig,
    *,
    account_id: str,
    config_hash: str,
    client_factory: QmtSnapshotClientFactory,
    clock: QmtProbeClock | None = None,
) -> QmtSnapshotProbeReport:
    """Query four datasets only after an active target account is confirmed OK."""
    effective_clock = SystemClock() if clock is None else clock
    started_at = effective_clock.now().to_wire()
    started_ns = effective_clock.monotonic_ns()
    collector = _SignalCollector(clock=effective_clock, started_ns=started_ns)
    path_fingerprint = _fingerprint(str(config.userdata_mini.resolve(strict=False)).casefold())
    phases: list[QmtProbePhaseEvidence] = []
    datasets: list[QmtDatasetObservation] = []
    client: QmtSnapshotReadOnlyClient | None = None
    sdk_version: str | None = None
    subscribed = False
    connect_result: int | None = None
    subscribe_result: int | None = None
    unsubscribe_result: int | None = None
    status_query_count = 0
    outcome = QmtProbeOutcome.ERROR
    failure_code: str | None = "QMT_SNAPSHOT_NOT_COMPLETED"
    cleanup_failures: list[str] = []

    def phase(name: str, result: int | None = None) -> None:
        phases.append(
            QmtProbePhaseEvidence(
                phase=name,
                elapsed_milliseconds=collector.elapsed_milliseconds(),
                result_code=result,
            )
        )

    def require_account_ok() -> bool:
        nonlocal status_query_count, outcome, failure_code
        assert client is not None
        callback_failure = collector.failure_code()
        if callback_failure is not None:
            outcome = (
                QmtProbeOutcome.BLOCKED
                if callback_failure != "QMT_SNAPSHOT_CALLBACK_INVALID"
                else QmtProbeOutcome.ERROR
            )
            failure_code = callback_failure
            return False
        status_query = client.query_account_statuses()
        status_query_count += 1
        phase("ACCOUNT_STATUS_QUERY_RETURNED", status_query.total_account_count)
        for status in status_query.target_statuses:
            collector.accept_query(status)
        if not status_query.target_statuses:
            outcome = QmtProbeOutcome.BLOCKED
            failure_code = "QMT_SNAPSHOT_ACCOUNT_NOT_FOUND"
            return False
        if len(status_query.target_statuses) != 1:
            outcome = QmtProbeOutcome.ERROR
            failure_code = "QMT_SNAPSHOT_ACCOUNT_STATUS_INVALID"
            return False
        if status_query.target_statuses[0].status != 0:
            outcome = QmtProbeOutcome.BLOCKED
            failure_code = "QMT_SNAPSHOT_ACCOUNT_NOT_OK"
            return False
        return True

    try:
        client = client_factory(config, account_id)
        sdk_version = client.sdk_version
        client.register_signal_sink(collector.accept)
        phase("CALLBACK_REGISTERED")
        client.start()
        phase("TRADER_STARTED")
        connect_result = client.connect()
        phase("CONNECT_RETURNED", connect_result)
        if connect_result != 0:
            outcome = QmtProbeOutcome.BLOCKED
            failure_code = "QMT_SNAPSHOT_CONNECT_FAILED"
        else:
            subscribe_result = client.subscribe()
            phase("SUBSCRIBE_RETURNED", subscribe_result)
            if subscribe_result != 0:
                outcome = QmtProbeOutcome.BLOCKED
                failure_code = "QMT_SNAPSHOT_SUBSCRIBE_FAILED"
            else:
                subscribed = True
                queries = (
                    ("ASSET_QUERY_RETURNED", client.query_asset),
                    ("POSITIONS_QUERY_RETURNED", client.query_positions),
                    ("ORDERS_QUERY_RETURNED", client.query_orders),
                    ("TRADES_QUERY_RETURNED", client.query_trades),
                )
                if require_account_ok():
                    outcome = QmtProbeOutcome.PASS
                    failure_code = None
                    for query_phase, query in queries:
                        observation = query()
                        datasets.append(observation)
                        phase(query_phase, observation.record_count)
                        if not observation.structurally_valid:
                            outcome = QmtProbeOutcome.ERROR
                            failure_code = "QMT_SNAPSHOT_FIELDS_INVALID"
                            break
                        if observation.is_indeterminate:
                            outcome = QmtProbeOutcome.BLOCKED
                            failure_code = "QMT_SNAPSHOT_QUERY_INDETERMINATE"
                        if not require_account_ok():
                            break
    except QmtReadOnlyAdapterError as exc:
        outcome = QmtProbeOutcome.ERROR
        failure_code = exc.code
    except Exception:
        outcome = QmtProbeOutcome.ERROR
        failure_code = "QMT_SNAPSHOT_UNEXPECTED_FAILURE"
    finally:
        primary_failure_code = failure_code
        if client is not None and subscribed:
            try:
                unsubscribe_result = client.unsubscribe()
                phase("UNSUBSCRIBE_RETURNED", unsubscribe_result)
                if unsubscribe_result != 0:
                    cleanup_failures.append("QMT_SNAPSHOT_UNSUBSCRIBE_FAILED")
            except QmtReadOnlyAdapterError as exc:
                cleanup_failures.append(exc.code)
            except Exception:
                cleanup_failures.append("QMT_SNAPSHOT_UNSUBSCRIBE_UNEXPECTED_FAILURE")
        if client is not None:
            try:
                client.stop()
                phase("TRADER_STOPPED")
            except QmtReadOnlyAdapterError as exc:
                cleanup_failures.append(exc.code)
            except Exception:
                cleanup_failures.append("QMT_SNAPSHOT_STOP_UNEXPECTED_FAILURE")
        if cleanup_failures:
            outcome = QmtProbeOutcome.ERROR
            failure_code = "QMT_SNAPSHOT_CLEANUP_FAILED"

    statuses, disconnects, invalid, evidence_truncated = collector.snapshot()
    return QmtSnapshotProbeReport(
        started_at=started_at,
        completed_at=effective_clock.now().to_wire(),
        duration_milliseconds=collector.elapsed_milliseconds(),
        outcome=outcome,
        failure_code=failure_code,
        primary_failure_code=primary_failure_code if cleanup_failures else None,
        cleanup_failure_codes=tuple(cleanup_failures),
        config_hash=config_hash,
        account_fingerprint=_fingerprint(account_id),
        userdata_mini_fingerprint=path_fingerprint,
        session_id=config.session_id,
        account_type=config.account_type,
        sdk_version=sdk_version,
        connect_result=connect_result,
        subscribe_result=subscribe_result,
        unsubscribe_result=unsubscribe_result,
        status_query_count=status_query_count,
        phases=tuple(phases),
        statuses=statuses,
        datasets=tuple(datasets),
        disconnect_callback_count=disconnects,
        invalid_callback_count=invalid,
        evidence_truncated=evidence_truncated,
    )


def write_qmt_snapshot_report(
    destination: Path,
    report: QmtSnapshotProbeReport,
    *,
    forbidden_values: tuple[str, ...] = (),
) -> str:
    return write_immutable_probe_payload(
        destination,
        report.to_payload(),
        forbidden_values=forbidden_values,
    )
