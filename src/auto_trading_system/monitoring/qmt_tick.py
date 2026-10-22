"""QMT-003 read-only tick probe and privacy-safe evidence aggregation."""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from auto_trading_system.domain import SecurityCode, SystemClock
from auto_trading_system.monitoring.qmt_readonly import (
    QmtProbeClock,
    QmtProbeOutcome,
    QmtProbePhaseEvidence,
    QmtReadOnlyAdapterError,
    write_immutable_probe_payload,
)

_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{16}$")
_MAX_SYMBOLS = 15
_MAX_ACCEPTED_SAMPLES = 100_000


class QmtTickScenario(StrEnum):
    ACTIVE = "ACTIVE"
    HALTED = "HALTED"
    LIMIT_UP = "LIMIT_UP"
    LIMIT_DOWN = "LIMIT_DOWN"
    CLOSED = "CLOSED"

    @property
    def requires_callback(self) -> bool:
        return self in {self.ACTIVE, self.LIMIT_UP, self.LIMIT_DOWN}


class QmtTickSource(StrEnum):
    SNAPSHOT = "SNAPSHOT"
    CALLBACK = "CALLBACK"


class QmtTimestampUnit(StrEnum):
    SECONDS = "SECONDS"
    MILLISECONDS = "MILLISECONDS"
    MICROSECONDS = "MICROSECONDS"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class QmtTickFieldSample:
    """One field reduced to structural facts with its value removed."""

    name: str
    required: bool
    observed_type: str
    valid: bool
    none: bool = False
    non_finite_count: int = 0
    negative_count: int = 0
    sequence_length: int | None = None
    element_types: tuple[str, ...] = ()
    invalid_element_count: int = 0

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ValueError("QMT tick field name is invalid")
        if self.observed_type not in {
            "bool",
            "float",
            "int",
            "list",
            "missing",
            "none",
            "other",
            "str",
            "tuple",
        }:
            raise ValueError("QMT tick field type is invalid")
        if self.none != (self.observed_type == "none"):
            raise ValueError("QMT tick none marker is inconsistent")
        for value in (
            self.non_finite_count,
            self.negative_count,
            self.invalid_element_count,
        ):
            if type(value) is not int or value < 0:
                raise ValueError("QMT tick field counters are invalid")
        if self.sequence_length is not None and (
            type(self.sequence_length) is not int or self.sequence_length < 0
        ):
            raise ValueError("QMT tick sequence length is invalid")
        if tuple(sorted(set(self.element_types))) != self.element_types:
            raise ValueError("QMT tick element types must be unique and sorted")


@dataclass(frozen=True, slots=True)
class QmtTickSample:
    """Sanitized facts from one SDK tick record."""

    symbol_fingerprint: str
    source: QmtTickSource
    fields: tuple[QmtTickFieldSample, ...]
    timestamp_unit: QmtTimestampUnit | None
    volume_multiplier_candidate: int | None
    stock_status_code: int | None
    order_book_depth_mismatch: bool

    def __post_init__(self) -> None:
        if _FINGERPRINT_PATTERN.fullmatch(self.symbol_fingerprint) is None:
            raise ValueError("QMT tick symbol fingerprint is invalid")
        if not self.fields or len({field.name for field in self.fields}) != len(self.fields):
            raise ValueError("QMT tick sample fields must be a non-empty unique tuple")
        if self.volume_multiplier_candidate is not None and (
            type(self.volume_multiplier_candidate) is not int
            or not 1 <= self.volume_multiplier_candidate <= 10_000
        ):
            raise ValueError("QMT tick volume multiplier is invalid")
        if self.stock_status_code is not None and type(self.stock_status_code) is not int:
            raise TypeError("QMT tick stock status must be an integer")

    @property
    def structurally_valid(self) -> bool:
        return not self.order_book_depth_mismatch and all(field.valid for field in self.fields)


QmtTickSink = Callable[[QmtTickSample], None]
QmtTickErrorSink = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class QmtTickSnapshotBatch:
    samples: tuple[QmtTickSample, ...]
    missing_symbol_count: int
    unexpected_symbol_count: int

    def __post_init__(self) -> None:
        if any(sample.source is not QmtTickSource.SNAPSHOT for sample in self.samples):
            raise ValueError("QMT tick snapshot batch can contain snapshot samples only")
        for value in (self.missing_symbol_count, self.unexpected_symbol_count):
            if type(value) is not int or value < 0:
                raise ValueError("QMT tick batch counters are invalid")


class QmtTickReadOnlyClient(Protocol):
    """Only XtData operations authorized for the QMT-003 field probe."""

    @property
    def sdk_version(self) -> str | None: ...

    def subscribe(
        self,
        symbol: SecurityCode,
        sample_sink: QmtTickSink,
        error_sink: QmtTickErrorSink,
    ) -> int: ...

    def get_full_tick(self, symbols: tuple[SecurityCode, ...]) -> QmtTickSnapshotBatch: ...

    def wait_for_callbacks(self, seconds: int) -> None: ...

    def unsubscribe(self, subscription_id: int) -> None: ...


QmtTickClientFactory = Callable[[], QmtTickReadOnlyClient]


@dataclass(frozen=True, slots=True)
class QmtTickProbeConfig:
    symbols: tuple[SecurityCode, ...]
    scenario: QmtTickScenario
    observation_seconds: int = 30

    def __post_init__(self) -> None:
        if not isinstance(self.symbols, tuple) or not 1 <= len(self.symbols) <= _MAX_SYMBOLS:
            raise ValueError("QMT tick probe requires between 1 and 15 symbols")
        wires = tuple(symbol.to_wire() for symbol in self.symbols)
        if len(set(wires)) != len(wires):
            raise ValueError("QMT tick probe symbols must be unique")
        if not isinstance(self.scenario, QmtTickScenario):
            raise TypeError("QMT tick scenario is invalid")
        if type(self.observation_seconds) is not int or not 1 <= self.observation_seconds <= 300:
            raise ValueError("QMT tick observation must be between 1 and 300 seconds")


@dataclass(frozen=True, slots=True)
class QmtTickFieldShape:
    name: str
    required: bool
    observed_types: tuple[str, ...]
    sample_count: int
    missing_count: int
    none_count: int
    invalid_count: int
    non_finite_count: int
    negative_count: int
    minimum_sequence_length: int | None
    maximum_sequence_length: int | None
    element_types: tuple[str, ...]
    invalid_element_count: int

    @property
    def structurally_valid(self) -> bool:
        return self.invalid_count == 0 and (not self.required or self.missing_count == 0)

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "required": self.required,
            "observed_types": list(self.observed_types),
            "sample_count": self.sample_count,
            "missing_count": self.missing_count,
            "none_count": self.none_count,
            "invalid_count": self.invalid_count,
            "non_finite_count": self.non_finite_count,
            "negative_count": self.negative_count,
            "minimum_sequence_length": self.minimum_sequence_length,
            "maximum_sequence_length": self.maximum_sequence_length,
            "element_types": list(self.element_types),
            "invalid_element_count": self.invalid_element_count,
        }


@dataclass(frozen=True, slots=True)
class QmtTickSymbolEvidence:
    symbol_fingerprint: str
    source: QmtTickSource
    sample_count: int
    valid_sample_count: int
    fields: tuple[QmtTickFieldShape, ...]
    timestamp_units: tuple[QmtTimestampUnit, ...]
    volume_multiplier_candidates: tuple[int, ...]
    stock_status_codes: tuple[int, ...]
    order_book_depth_mismatch_count: int

    @property
    def structurally_valid(self) -> bool:
        return self.sample_count > 0 and self.valid_sample_count == self.sample_count

    def to_payload(self) -> dict[str, object]:
        return {
            "symbol_fingerprint": self.symbol_fingerprint,
            "source": self.source.value,
            "sample_count": self.sample_count,
            "valid_sample_count": self.valid_sample_count,
            "structurally_valid": self.structurally_valid,
            "timestamp_units": [unit.value for unit in self.timestamp_units],
            "volume_multiplier_candidates": list(self.volume_multiplier_candidates),
            "stock_status_codes": list(self.stock_status_codes),
            "order_book_depth_mismatch_count": self.order_book_depth_mismatch_count,
            "fields": [field.to_payload() for field in self.fields],
        }


@dataclass(frozen=True, slots=True)
class QmtTickProbeReport:
    started_at: str
    completed_at: str
    duration_milliseconds: int
    outcome: QmtProbeOutcome
    failure_code: str | None
    primary_failure_code: str | None
    cleanup_failure_codes: tuple[str, ...]
    config_hash: str
    scenario: QmtTickScenario
    observation_seconds: int
    sdk_version: str | None
    requested_symbol_count: int
    subscription_count: int
    snapshot_missing_symbol_count: int
    snapshot_unexpected_symbol_count: int
    invalid_callback_count: int
    accepted_sample_count: int
    ignored_sample_count: int
    unsubscribe_attempt_count: int
    phases: tuple[QmtProbePhaseEvidence, ...]
    symbols: tuple[QmtTickSymbolEvidence, ...]

    @property
    def exit_code(self) -> int:
        if self.outcome is QmtProbeOutcome.PASS:
            return 0
        if self.outcome is QmtProbeOutcome.BLOCKED:
            return 2
        return 3

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": "qmt-tick-probe/v1",
            "collection_mode": "READ_ONLY",
            "work_item": "QMT-003",
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_milliseconds": self.duration_milliseconds,
            "outcome": self.outcome.value,
            "failure_code": self.failure_code,
            "primary_failure_code": self.primary_failure_code,
            "cleanup_failure_codes": list(self.cleanup_failure_codes),
            "config_hash": self.config_hash,
            "scenario": self.scenario.value,
            "observation_seconds": self.observation_seconds,
            "qmt": {
                "sdk_version": self.sdk_version,
                "requested_symbol_count": self.requested_symbol_count,
                "subscription_count": self.subscription_count,
                "unsubscribe_attempt_count": self.unsubscribe_attempt_count,
            },
            "snapshot": {
                "missing_symbol_count": self.snapshot_missing_symbol_count,
                "unexpected_symbol_count": self.snapshot_unexpected_symbol_count,
            },
            "callbacks": {
                "invalid_count": self.invalid_callback_count,
                "accepted_sample_count": self.accepted_sample_count,
                "ignored_sample_count": self.ignored_sample_count,
            },
            "symbols": [symbol.to_payload() for symbol in self.symbols],
            "phases": [phase.to_payload() for phase in self.phases],
            "privacy": {
                "full_broker_account_omitted": True,
                "security_codes_replaced_by_fingerprints": True,
                "prices_amounts_and_volumes_omitted": True,
                "raw_tick_payloads_omitted": True,
                "userdata_path_omitted": True,
                "exception_messages_omitted": True,
            },
        }


class _FieldAccumulator:
    def __init__(self, name: str, required: bool) -> None:
        self.name = name
        self.required = required
        self.observed_types: set[str] = set()
        self.sample_count = 0
        self.missing_count = 0
        self.none_count = 0
        self.invalid_count = 0
        self.non_finite_count = 0
        self.negative_count = 0
        self.minimum_length: int | None = None
        self.maximum_length: int | None = None
        self.element_types: set[str] = set()
        self.invalid_element_count = 0

    def accept(self, field: QmtTickFieldSample) -> None:
        self.sample_count += 1
        self.observed_types.add(field.observed_type)
        self.missing_count += int(field.observed_type == "missing")
        self.none_count += int(field.none)
        self.invalid_count += int(not field.valid)
        self.non_finite_count += field.non_finite_count
        self.negative_count += field.negative_count
        if field.sequence_length is not None:
            self.minimum_length = (
                field.sequence_length
                if self.minimum_length is None
                else min(self.minimum_length, field.sequence_length)
            )
            self.maximum_length = (
                field.sequence_length
                if self.maximum_length is None
                else max(self.maximum_length, field.sequence_length)
            )
        self.element_types.update(field.element_types)
        self.invalid_element_count += field.invalid_element_count

    def freeze(self) -> QmtTickFieldShape:
        return QmtTickFieldShape(
            name=self.name,
            required=self.required,
            observed_types=tuple(sorted(self.observed_types)),
            sample_count=self.sample_count,
            missing_count=self.missing_count,
            none_count=self.none_count,
            invalid_count=self.invalid_count,
            non_finite_count=self.non_finite_count,
            negative_count=self.negative_count,
            minimum_sequence_length=self.minimum_length,
            maximum_sequence_length=self.maximum_length,
            element_types=tuple(sorted(self.element_types)),
            invalid_element_count=self.invalid_element_count,
        )


class _SymbolAccumulator:
    def __init__(self, fingerprint: str, source: QmtTickSource) -> None:
        self.fingerprint = fingerprint
        self.source = source
        self.sample_count = 0
        self.valid_sample_count = 0
        self.fields: dict[str, _FieldAccumulator] = {}
        self.timestamp_units: set[QmtTimestampUnit] = set()
        self.volume_multipliers: set[int] = set()
        self.stock_statuses: set[int] = set()
        self.depth_mismatch_count = 0

    def accept(self, sample: QmtTickSample) -> None:
        self.sample_count += 1
        self.valid_sample_count += int(sample.structurally_valid)
        if sample.timestamp_unit is not None:
            self.timestamp_units.add(sample.timestamp_unit)
        if sample.volume_multiplier_candidate is not None:
            self.volume_multipliers.add(sample.volume_multiplier_candidate)
        if sample.stock_status_code is not None:
            self.stock_statuses.add(sample.stock_status_code)
        self.depth_mismatch_count += int(sample.order_book_depth_mismatch)
        for field in sample.fields:
            accumulator = self.fields.setdefault(
                field.name, _FieldAccumulator(field.name, field.required)
            )
            if accumulator.required != field.required:
                raise ValueError("QMT tick field required marker changed between samples")
            accumulator.accept(field)

    def freeze(self) -> QmtTickSymbolEvidence:
        return QmtTickSymbolEvidence(
            symbol_fingerprint=self.fingerprint,
            source=self.source,
            sample_count=self.sample_count,
            valid_sample_count=self.valid_sample_count,
            fields=tuple(self.fields[name].freeze() for name in sorted(self.fields)),
            timestamp_units=tuple(sorted(self.timestamp_units, key=lambda unit: unit.value)),
            volume_multiplier_candidates=tuple(sorted(self.volume_multipliers)),
            stock_status_codes=tuple(sorted(self.stock_statuses)),
            order_book_depth_mismatch_count=self.depth_mismatch_count,
        )


class _TickCollector:
    def __init__(self, symbols: tuple[SecurityCode, ...]) -> None:
        self._lock = threading.Lock()
        self._accepted = 0
        self._ignored = 0
        self._invalid_callbacks = 0
        self._known_fingerprints = {fingerprint_symbol(symbol) for symbol in symbols}
        self._accumulators = {
            (fingerprint, source): _SymbolAccumulator(fingerprint, source)
            for fingerprint in self._known_fingerprints
            for source in QmtTickSource
        }

    def accept(self, sample: QmtTickSample) -> None:
        with self._lock:
            if self._accepted >= _MAX_ACCEPTED_SAMPLES:
                self._ignored += 1
                return
            accumulator = self._accumulators.get((sample.symbol_fingerprint, sample.source))
            if accumulator is None:
                self._invalid_callbacks += 1
                return
            try:
                accumulator.accept(sample)
            except Exception:
                self._invalid_callbacks += 1
                return
            self._accepted += 1
            if sample.source is QmtTickSource.CALLBACK and not sample.structurally_valid:
                self._invalid_callbacks += 1

    def reject(self, code: str) -> None:
        del code
        with self._lock:
            self._invalid_callbacks += 1

    def snapshot(self) -> tuple[tuple[QmtTickSymbolEvidence, ...], int, int, int]:
        with self._lock:
            evidence = tuple(
                self._accumulators[key].freeze()
                for key in sorted(self._accumulators, key=lambda item: (item[0], item[1].value))
            )
            return evidence, self._invalid_callbacks, self._accepted, self._ignored


def fingerprint_symbol(symbol: SecurityCode) -> str:
    return hashlib.sha256(symbol.to_wire().encode("ascii")).hexdigest()[:16]


def run_qmt_tick_probe(
    config: QmtTickProbeConfig,
    *,
    config_hash: str,
    client_factory: QmtTickClientFactory,
    clock: QmtProbeClock | None = None,
) -> QmtTickProbeReport:
    """Collect bounded snapshot/callback field evidence without retaining tick values."""
    effective_clock = SystemClock() if clock is None else clock
    started_at = effective_clock.now().to_wire()
    started_ns = effective_clock.monotonic_ns()
    collector = _TickCollector(config.symbols)
    phases: list[QmtProbePhaseEvidence] = []
    client: QmtTickReadOnlyClient | None = None
    sdk_version: str | None = None
    subscription_ids: list[int] = []
    snapshot_missing = 0
    snapshot_unexpected = 0
    unsubscribe_attempts = 0
    cleanup_failures: list[str] = []
    outcome = QmtProbeOutcome.ERROR
    failure_code: str | None = "QMT_TICK_NOT_COMPLETED"

    def elapsed_milliseconds() -> int:
        return max(0, (effective_clock.monotonic_ns() - started_ns) // 1_000_000)

    def phase(name: str, result: int | None = None) -> None:
        phases.append(QmtProbePhaseEvidence(name, elapsed_milliseconds(), result))

    try:
        client = client_factory()
        sdk_version = client.sdk_version
        phase("CLIENT_CREATED")
        for symbol in config.symbols:
            subscription_id = client.subscribe(symbol, collector.accept, collector.reject)
            phase("SUBSCRIBE_RETURNED", subscription_id)
            if type(subscription_id) is not int or subscription_id <= 0:
                outcome = QmtProbeOutcome.BLOCKED
                failure_code = "QMT_TICK_SUBSCRIBE_FAILED"
                break
            if subscription_id in subscription_ids:
                outcome = QmtProbeOutcome.ERROR
                failure_code = "QMT_TICK_SUBSCRIPTION_ID_DUPLICATE"
                break
            subscription_ids.append(subscription_id)
        else:
            batch = client.get_full_tick(config.symbols)
            snapshot_missing = batch.missing_symbol_count
            snapshot_unexpected = batch.unexpected_symbol_count
            for sample in batch.samples:
                collector.accept(sample)
            phase("SNAPSHOT_RETURNED", len(batch.samples))
            if snapshot_unexpected:
                outcome = QmtProbeOutcome.ERROR
                failure_code = "QMT_TICK_SNAPSHOT_UNEXPECTED_SYMBOL"
            elif snapshot_missing:
                outcome = QmtProbeOutcome.BLOCKED
                failure_code = "QMT_TICK_SNAPSHOT_MISSING"
            else:
                client.wait_for_callbacks(config.observation_seconds)
                phase("OBSERVATION_COMPLETED")
                symbols, invalid_callbacks, _, _ = collector.snapshot()
                snapshot_evidence = tuple(
                    item for item in symbols if item.source is QmtTickSource.SNAPSHOT
                )
                callback_evidence = tuple(
                    item for item in symbols if item.source is QmtTickSource.CALLBACK
                )
                if invalid_callbacks or any(
                    item.sample_count and not item.structurally_valid for item in callback_evidence
                ):
                    outcome = QmtProbeOutcome.ERROR
                    failure_code = "QMT_TICK_FIELDS_INVALID"
                elif any(not item.structurally_valid for item in snapshot_evidence):
                    outcome = QmtProbeOutcome.ERROR
                    failure_code = "QMT_TICK_SNAPSHOT_INVALID"
                elif config.scenario.requires_callback and any(
                    item.sample_count == 0 for item in callback_evidence
                ):
                    outcome = QmtProbeOutcome.BLOCKED
                    failure_code = "QMT_TICK_CALLBACK_MISSING"
                else:
                    outcome = QmtProbeOutcome.PASS
                    failure_code = None
    except QmtReadOnlyAdapterError as exc:
        outcome = QmtProbeOutcome.ERROR
        failure_code = exc.code
    except Exception:
        outcome = QmtProbeOutcome.ERROR
        failure_code = "QMT_TICK_UNEXPECTED_FAILURE"
    finally:
        primary_failure_code = failure_code
        if client is not None:
            for subscription_id in reversed(subscription_ids):
                unsubscribe_attempts += 1
                try:
                    client.unsubscribe(subscription_id)
                    phase("UNSUBSCRIBE_COMPLETED", subscription_id)
                except QmtReadOnlyAdapterError as exc:
                    cleanup_failures.append(exc.code)
                except Exception:
                    cleanup_failures.append("QMT_TICK_UNSUBSCRIBE_UNEXPECTED_FAILURE")
        if cleanup_failures:
            outcome = QmtProbeOutcome.ERROR
            failure_code = "QMT_TICK_CLEANUP_FAILED"

    symbols, invalid_callbacks, accepted_samples, ignored_samples = collector.snapshot()
    return QmtTickProbeReport(
        started_at=started_at,
        completed_at=effective_clock.now().to_wire(),
        duration_milliseconds=elapsed_milliseconds(),
        outcome=outcome,
        failure_code=failure_code,
        primary_failure_code=primary_failure_code if cleanup_failures else None,
        cleanup_failure_codes=tuple(cleanup_failures),
        config_hash=config_hash,
        scenario=config.scenario,
        observation_seconds=config.observation_seconds,
        sdk_version=sdk_version,
        requested_symbol_count=len(config.symbols),
        subscription_count=len(subscription_ids),
        snapshot_missing_symbol_count=snapshot_missing,
        snapshot_unexpected_symbol_count=snapshot_unexpected,
        invalid_callback_count=invalid_callbacks,
        accepted_sample_count=accepted_samples,
        ignored_sample_count=ignored_samples,
        unsubscribe_attempt_count=unsubscribe_attempts,
        phases=tuple(phases),
        symbols=symbols,
    )


def write_qmt_tick_report(
    destination: Path,
    report: QmtTickProbeReport,
    *,
    forbidden_values: tuple[str, ...] = (),
) -> str:
    return write_immutable_probe_payload(
        destination,
        report.to_payload(),
        forbidden_values=forbidden_values,
    )
