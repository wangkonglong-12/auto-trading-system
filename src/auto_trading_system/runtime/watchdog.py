"""Durable Runtime heartbeat and fail-closed external HALT protocol."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import threading
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from auto_trading_system.domain.clock import Clock
from auto_trading_system.domain.trading_time import ShanghaiTimestamp
from auto_trading_system.runtime.lifecycle import LifecyclePhase, RuntimeComponent
from auto_trading_system.runtime.state_machine import (
    RuntimeState,
    RuntimeStateMachine,
    TransitionAuthority,
    TransitionDecision,
)

_ARTIFACT_VERSION = "1.0"
_MAX_ARTIFACT_BYTES = 65_536
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,95}$")
_REASON_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_DETAIL_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
WATCHDOG_FORCE_HALT_ENVIRONMENT = "ATS_WATCHDOG_FORCE_HALT"


class WatchdogArtifactError(RuntimeError):
    """Stable error raised for an unsafe or unavailable watchdog artifact."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _validate_identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")


def _parse_json_object(raw: bytes, *, artifact_name: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise WatchdogArtifactError(
                    "WATCHDOG_ARTIFACT_DUPLICATE_KEY",
                    f"{artifact_name} contains duplicate keys",
                )
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except WatchdogArtifactError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WatchdogArtifactError(
            "WATCHDOG_ARTIFACT_INVALID",
            f"{artifact_name} is not valid UTF-8 JSON",
        ) from exc
    if not isinstance(value, dict):
        raise WatchdogArtifactError(
            "WATCHDOG_ARTIFACT_INVALID",
            f"{artifact_name} root must be an object",
        )
    return value


def _expect_exact_keys(value: Mapping[str, Any], expected: frozenset[str], name: str) -> None:
    if frozenset(value) != expected:
        raise WatchdogArtifactError(
            "WATCHDOG_ARTIFACT_FIELDS_INVALID",
            f"{name} fields do not match protocol version {_ARTIFACT_VERSION}",
        )


def _require_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise WatchdogArtifactError(
            "WATCHDOG_ARTIFACT_FIELD_INVALID",
            f"{key} must be a string",
        )
    return item


def _require_int(value: Mapping[str, Any], key: str) -> int:
    item = value.get(key)
    if type(item) is not int:
        raise WatchdogArtifactError(
            "WATCHDOG_ARTIFACT_FIELD_INVALID",
            f"{key} must be an integer",
        )
    return item


def _normalize_details(details: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    if len(details) > 16:
        raise ValueError("watchdog details cannot contain more than 16 fields")
    normalized: list[tuple[str, str]] = []
    for key, value in sorted(details.items()):
        if not isinstance(key, str) or _DETAIL_KEY_PATTERN.fullmatch(key) is None:
            raise ValueError("watchdog detail key is invalid")
        if type(value) not in {str, int, bool}:
            raise ValueError("watchdog detail value must be a string, integer, or boolean")
        rendered = str(value).lower() if type(value) is bool else str(value)
        if not rendered or len(rendered) > 256:
            raise ValueError("watchdog detail value length is invalid")
        normalized.append((key, rendered))
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class RuntimeHeartbeat:
    """Small liveness lease written by one Runtime boot."""

    node_id: str
    account_id: str
    boot_id: str
    process_id: int
    sequence: int
    runtime_state: RuntimeState
    written_at: ShanghaiTimestamp

    def __post_init__(self) -> None:
        _validate_identifier(self.node_id, "node_id")
        _validate_identifier(self.account_id, "account_id")
        if re.fullmatch(r"boot_[a-f0-9]{32}", self.boot_id) is None:
            raise ValueError("boot_id is invalid")
        if type(self.process_id) is not int or self.process_id < 1:
            raise ValueError("process_id must be a positive integer")
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("sequence must be a positive integer")
        if not isinstance(self.runtime_state, RuntimeState):
            raise ValueError("runtime_state must be a RuntimeState")
        if not isinstance(self.written_at, ShanghaiTimestamp):
            raise ValueError("written_at must be a ShanghaiTimestamp")

    def to_mapping(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "artifact_version": _ARTIFACT_VERSION,
            "boot_id": self.boot_id,
            "node_id": self.node_id,
            "process_id": self.process_id,
            "runtime_state": self.runtime_state.value,
            "sequence": self.sequence,
            "written_at": self.written_at.to_wire(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Self:
        _expect_exact_keys(
            value,
            frozenset(
                {
                    "account_id",
                    "artifact_version",
                    "boot_id",
                    "node_id",
                    "process_id",
                    "runtime_state",
                    "sequence",
                    "written_at",
                }
            ),
            "runtime heartbeat",
        )
        if _require_string(value, "artifact_version") != _ARTIFACT_VERSION:
            raise WatchdogArtifactError(
                "WATCHDOG_ARTIFACT_VERSION_UNSUPPORTED",
                "runtime heartbeat version is unsupported",
            )
        try:
            return cls(
                node_id=_require_string(value, "node_id"),
                account_id=_require_string(value, "account_id"),
                boot_id=_require_string(value, "boot_id"),
                process_id=_require_int(value, "process_id"),
                sequence=_require_int(value, "sequence"),
                runtime_state=RuntimeState(_require_string(value, "runtime_state")),
                written_at=ShanghaiTimestamp.parse(_require_string(value, "written_at")),
            )
        except ValueError as exc:
            raise WatchdogArtifactError(
                "WATCHDOG_ARTIFACT_FIELD_INVALID",
                "runtime heartbeat contains an invalid field",
            ) from exc


@dataclass(frozen=True, slots=True)
class HeartbeatSnapshot:
    heartbeat: RuntimeHeartbeat
    fingerprint: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"sha256:[a-f0-9]{64}", self.fingerprint) is None:
            raise ValueError("heartbeat fingerprint is invalid")


@dataclass(frozen=True, slots=True)
class ExternalHaltMarker:
    """Latched evidence requiring a safe Runtime start and operator recovery."""

    marker_id: str
    node_id: str
    account_id: str
    reason_codes: tuple[str, ...]
    first_detected_at: ShanghaiTimestamp
    last_detected_at: ShanghaiTimestamp
    occurrence_count: int
    watchdog_process_id: int
    runtime_process_id: int | None
    restart_generation: int
    details: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if re.fullmatch(r"halt_[a-f0-9]{32}", self.marker_id) is None:
            raise ValueError("marker_id is invalid")
        _validate_identifier(self.node_id, "node_id")
        _validate_identifier(self.account_id, "account_id")
        if not self.reason_codes or len(self.reason_codes) > 16:
            raise ValueError("reason_codes length is invalid")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("reason_codes must be unique")
        if any(_REASON_PATTERN.fullmatch(reason) is None for reason in self.reason_codes):
            raise ValueError("reason_code is invalid")
        if not isinstance(self.first_detected_at, ShanghaiTimestamp) or not isinstance(
            self.last_detected_at, ShanghaiTimestamp
        ):
            raise ValueError("detection timestamps are invalid")
        if self.last_detected_at < self.first_detected_at:
            raise ValueError("last detection cannot precede first detection")
        if type(self.occurrence_count) is not int or self.occurrence_count < 1:
            raise ValueError("occurrence_count must be positive")
        if type(self.watchdog_process_id) is not int or self.watchdog_process_id < 1:
            raise ValueError("watchdog_process_id must be positive")
        if self.runtime_process_id is not None and (
            type(self.runtime_process_id) is not int or self.runtime_process_id < 1
        ):
            raise ValueError("runtime_process_id must be positive when present")
        if type(self.restart_generation) is not int or self.restart_generation < 0:
            raise ValueError("restart_generation must be non-negative")
        _normalize_details(dict(self.details))

    def to_mapping(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "artifact_version": _ARTIFACT_VERSION,
            "details": dict(self.details),
            "first_detected_at": self.first_detected_at.to_wire(),
            "last_detected_at": self.last_detected_at.to_wire(),
            "marker_id": self.marker_id,
            "node_id": self.node_id,
            "occurrence_count": self.occurrence_count,
            "reason_codes": list(self.reason_codes),
            "restart_generation": self.restart_generation,
            "runtime_process_id": self.runtime_process_id,
            "watchdog_process_id": self.watchdog_process_id,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Self:
        _expect_exact_keys(
            value,
            frozenset(
                {
                    "account_id",
                    "artifact_version",
                    "details",
                    "first_detected_at",
                    "last_detected_at",
                    "marker_id",
                    "node_id",
                    "occurrence_count",
                    "reason_codes",
                    "restart_generation",
                    "runtime_process_id",
                    "watchdog_process_id",
                }
            ),
            "external HALT marker",
        )
        if _require_string(value, "artifact_version") != _ARTIFACT_VERSION:
            raise WatchdogArtifactError(
                "WATCHDOG_ARTIFACT_VERSION_UNSUPPORTED",
                "external HALT marker version is unsupported",
            )
        reasons = value.get("reason_codes")
        details = value.get("details")
        runtime_process_id = value.get("runtime_process_id")
        if not isinstance(reasons, list) or any(not isinstance(item, str) for item in reasons):
            raise WatchdogArtifactError(
                "WATCHDOG_ARTIFACT_FIELD_INVALID", "reason_codes must be a string array"
            )
        if not isinstance(details, dict) or any(not isinstance(key, str) for key in details):
            raise WatchdogArtifactError(
                "WATCHDOG_ARTIFACT_FIELD_INVALID", "details must be an object"
            )
        if runtime_process_id is not None and type(runtime_process_id) is not int:
            raise WatchdogArtifactError(
                "WATCHDOG_ARTIFACT_FIELD_INVALID",
                "runtime_process_id must be an integer or null",
            )
        try:
            return cls(
                marker_id=_require_string(value, "marker_id"),
                node_id=_require_string(value, "node_id"),
                account_id=_require_string(value, "account_id"),
                reason_codes=tuple(reasons),
                first_detected_at=ShanghaiTimestamp.parse(
                    _require_string(value, "first_detected_at")
                ),
                last_detected_at=ShanghaiTimestamp.parse(
                    _require_string(value, "last_detected_at")
                ),
                occurrence_count=_require_int(value, "occurrence_count"),
                watchdog_process_id=_require_int(value, "watchdog_process_id"),
                runtime_process_id=runtime_process_id,
                restart_generation=_require_int(value, "restart_generation"),
                details=_normalize_details(details),
            )
        except ValueError as exc:
            raise WatchdogArtifactError(
                "WATCHDOG_ARTIFACT_FIELD_INVALID",
                "external HALT marker contains an invalid field",
            ) from exc


class _AtomicJsonArtifact:
    def __init__(self, path: Path, *, name: str) -> None:
        if not isinstance(path, Path) or not path.is_absolute():
            raise ValueError("watchdog artifact path must be absolute")
        self._path = path
        self._name = name

    @property
    def path(self) -> Path:
        return self._path

    def read_bytes(self) -> bytes | None:
        try:
            if not self._path.exists():
                return None
            if self._path.is_symlink():
                raise WatchdogArtifactError(
                    "WATCHDOG_ARTIFACT_SYMLINK_REJECTED",
                    f"{self._name} cannot be a symbolic link",
                )
            size = self._path.stat().st_size
            if size < 1 or size > _MAX_ARTIFACT_BYTES:
                raise WatchdogArtifactError(
                    "WATCHDOG_ARTIFACT_SIZE_INVALID",
                    f"{self._name} size is invalid",
                )
            return self._path.read_bytes()
        except WatchdogArtifactError:
            raise
        except OSError as exc:
            raise WatchdogArtifactError(
                "WATCHDOG_ARTIFACT_READ_FAILED",
                f"{self._name} cannot be read",
            ) from exc

    def write_mapping(self, value: Mapping[str, object]) -> bytes:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if not payload or len(payload) > _MAX_ARTIFACT_BYTES:
            raise WatchdogArtifactError(
                "WATCHDOG_ARTIFACT_SIZE_INVALID",
                f"{self._name} serialized size is invalid",
            )
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self._path.with_name(
                f".{self._path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
            )
            descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, self._path)
            finally:
                with suppress(OSError):
                    temp_path.unlink(missing_ok=True)
        except OSError as exc:
            raise WatchdogArtifactError(
                "WATCHDOG_ARTIFACT_WRITE_FAILED",
                f"{self._name} cannot be written atomically",
            ) from exc
        return payload


class RuntimeHeartbeatStore:
    """Atomic heartbeat file scoped to one configured node and account."""

    def __init__(self, path: Path, *, node_id: str, account_id: str) -> None:
        _validate_identifier(node_id, "node_id")
        _validate_identifier(account_id, "account_id")
        self._artifact = _AtomicJsonArtifact(path, name="runtime heartbeat")
        self._node_id = node_id
        self._account_id = account_id

    @classmethod
    def from_storage_root(
        cls, storage_root: Path, *, node_id: str, account_id: str
    ) -> RuntimeHeartbeatStore:
        return cls(
            storage_root / "runtime" / "runtime-heartbeat.json",
            node_id=node_id,
            account_id=account_id,
        )

    @property
    def path(self) -> Path:
        return self._artifact.path

    def write(self, heartbeat: RuntimeHeartbeat) -> HeartbeatSnapshot:
        if heartbeat.node_id != self._node_id or heartbeat.account_id != self._account_id:
            raise WatchdogArtifactError(
                "WATCHDOG_IDENTITY_MISMATCH",
                "runtime heartbeat identity does not match this store",
            )
        payload = self._artifact.write_mapping(heartbeat.to_mapping())
        return HeartbeatSnapshot(heartbeat, f"sha256:{hashlib.sha256(payload).hexdigest()}")

    def read(self) -> HeartbeatSnapshot | None:
        payload = self._artifact.read_bytes()
        if payload is None:
            return None
        heartbeat = RuntimeHeartbeat.from_mapping(
            _parse_json_object(payload, artifact_name="runtime heartbeat")
        )
        if heartbeat.node_id != self._node_id or heartbeat.account_id != self._account_id:
            raise WatchdogArtifactError(
                "WATCHDOG_IDENTITY_MISMATCH",
                "runtime heartbeat identity does not match configured node and account",
            )
        return HeartbeatSnapshot(heartbeat, f"sha256:{hashlib.sha256(payload).hexdigest()}")


class ExternalHaltMarkerStore:
    """Thread-safe, append-in-place latch for watchdog HALT reasons."""

    def __init__(self, path: Path, *, node_id: str, account_id: str) -> None:
        _validate_identifier(node_id, "node_id")
        _validate_identifier(account_id, "account_id")
        self._artifact = _AtomicJsonArtifact(path, name="external HALT marker")
        self._node_id = node_id
        self._account_id = account_id
        self._lock = threading.Lock()

    @classmethod
    def from_storage_root(
        cls, storage_root: Path, *, node_id: str, account_id: str
    ) -> ExternalHaltMarkerStore:
        return cls(
            storage_root / "runtime" / "external-halt.json",
            node_id=node_id,
            account_id=account_id,
        )

    @property
    def path(self) -> Path:
        return self._artifact.path

    def read(self) -> ExternalHaltMarker | None:
        payload = self._artifact.read_bytes()
        if payload is None:
            return None
        marker = ExternalHaltMarker.from_mapping(
            _parse_json_object(payload, artifact_name="external HALT marker")
        )
        if marker.node_id != self._node_id or marker.account_id != self._account_id:
            raise WatchdogArtifactError(
                "WATCHDOG_IDENTITY_MISMATCH",
                "external HALT marker identity does not match configured node and account",
            )
        return marker

    def record(
        self,
        *,
        reason_code: str,
        detected_at: ShanghaiTimestamp,
        watchdog_process_id: int,
        runtime_process_id: int | None,
        restart_generation: int,
        details: Mapping[str, object] | None = None,
    ) -> ExternalHaltMarker:
        if _REASON_PATTERN.fullmatch(reason_code) is None:
            raise ValueError("reason_code is invalid")
        normalized_details = _normalize_details(details or {})
        with self._lock:
            existing = self.read()
            if existing is None:
                marker = ExternalHaltMarker(
                    marker_id=f"halt_{secrets.token_hex(16)}",
                    node_id=self._node_id,
                    account_id=self._account_id,
                    reason_codes=(reason_code,),
                    first_detected_at=detected_at,
                    last_detected_at=detected_at,
                    occurrence_count=1,
                    watchdog_process_id=watchdog_process_id,
                    runtime_process_id=runtime_process_id,
                    restart_generation=restart_generation,
                    details=normalized_details,
                )
            else:
                reasons = existing.reason_codes
                if reason_code not in reasons:
                    reasons = (*reasons, reason_code)
                marker = ExternalHaltMarker(
                    marker_id=existing.marker_id,
                    node_id=existing.node_id,
                    account_id=existing.account_id,
                    reason_codes=reasons,
                    first_detected_at=existing.first_detected_at,
                    last_detected_at=max(existing.last_detected_at, detected_at),
                    occurrence_count=existing.occurrence_count + 1,
                    watchdog_process_id=watchdog_process_id,
                    runtime_process_id=runtime_process_id,
                    restart_generation=restart_generation,
                    details=normalized_details,
                )
            self._artifact.write_mapping(marker.to_mapping())
            return marker


def enforce_external_halt_at_startup(
    *,
    state_machine: RuntimeStateMachine,
    marker_store: ExternalHaltMarkerStore,
    force_halt_environment: Mapping[str, str] | None = None,
) -> TransitionDecision | None:
    """Apply the watchdog's persisted latch before Runtime connects to QMT."""
    marker = marker_store.read()
    environment = os.environ if force_halt_environment is None else force_halt_environment
    forced = environment.get(WATCHDOG_FORCE_HALT_ENVIRONMENT) == "1"
    if marker is None and not forced:
        return None
    reason = "WATCHDOG_EXTERNAL_HALT" if marker is not None else "WATCHDOG_FORCED_SAFE_RESTART"
    return state_machine.request_transition(
        RuntimeState.HALT,
        authority=TransitionAuthority.WATCHDOG,
        reason_code=reason,
    )


class RuntimeWatchdogBridge:
    """Lifecycle worker publishing heartbeats and observing a new HALT latch."""

    def __init__(
        self,
        *,
        heartbeat_store: RuntimeHeartbeatStore,
        marker_store: ExternalHaltMarkerStore,
        clock: Clock,
        node_id: str,
        account_id: str,
        runtime_state: Callable[[], RuntimeState],
        halt_sink: Callable[[ExternalHaltMarker], Awaitable[None]],
        interval_seconds: int,
        process_id: int | None = None,
        boot_id: str | None = None,
    ) -> None:
        _validate_identifier(node_id, "node_id")
        _validate_identifier(account_id, "account_id")
        if type(interval_seconds) is not int or not 1 <= interval_seconds <= 10:
            raise ValueError("interval_seconds must be between 1 and 10")
        resolved_process_id = os.getpid() if process_id is None else process_id
        if type(resolved_process_id) is not int or resolved_process_id < 1:
            raise ValueError("process_id must be positive")
        resolved_boot_id = f"boot_{secrets.token_hex(16)}" if boot_id is None else boot_id
        if re.fullmatch(r"boot_[a-f0-9]{32}", resolved_boot_id) is None:
            raise ValueError("boot_id is invalid")
        if not callable(runtime_state) or not callable(halt_sink):
            raise ValueError("runtime_state and halt_sink must be callable")
        self._heartbeat_store = heartbeat_store
        self._marker_store = marker_store
        self._clock = clock
        self._node_id = node_id
        self._account_id = account_id
        self._runtime_state = runtime_state
        self._halt_sink = halt_sink
        self._interval_seconds = interval_seconds
        self._process_id = resolved_process_id
        self._boot_id = resolved_boot_id
        self._sequence = 0
        self._delivered_marker_ids: set[str] = set()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._started_once = False

    async def start(self) -> None:
        if self._started_once:
            raise WatchdogArtifactError(
                "WATCHDOG_BRIDGE_ALREADY_STARTED", "runtime watchdog bridge already started"
            )
        self._started_once = True
        await self._publish_once()
        self._task = asyncio.create_task(self._run(), name="runtime-watchdog-bridge")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop_event.set()
        await task
        self._task = None

    def definition(self) -> RuntimeComponent:
        return RuntimeComponent(
            name="watchdog-bridge",
            phase=LifecyclePhase.WORKER,
            start=self.start,
            stop=self.stop,
        )

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval_seconds)
                return
            except TimeoutError:
                await self._publish_once()

    async def _publish_once(self) -> None:
        marker = self._marker_store.read()
        if marker is not None and marker.marker_id not in self._delivered_marker_ids:
            await self._halt_sink(marker)
            self._delivered_marker_ids.add(marker.marker_id)
        self._sequence += 1
        self._heartbeat_store.write(
            RuntimeHeartbeat(
                node_id=self._node_id,
                account_id=self._account_id,
                boot_id=self._boot_id,
                process_id=self._process_id,
                sequence=self._sequence,
                runtime_state=self._runtime_state(),
                written_at=self._clock.now(),
            )
        )
