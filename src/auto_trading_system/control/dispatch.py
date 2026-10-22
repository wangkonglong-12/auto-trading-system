"""Local-control submission, volatile progress receipts, and durable receipt lookup."""

from __future__ import annotations

import asyncio
import re
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import Protocol, runtime_checkable

from auto_trading_system.control.models import (
    LocalControlError,
    LocalControlRequest,
    LocalControlResult,
    PersistedLocalControlReceipt,
)
from auto_trading_system.control.recovery import project_local_control_receipts
from auto_trading_system.control.service import LocalControlCoordinator
from auto_trading_system.domain import Clock, ShanghaiTimestamp, TradingDate
from auto_trading_system.journal import JournalRecoverySnapshot
from auto_trading_system.runtime import (
    EventPriority,
    PublishOutcome,
    RuntimeEventSource,
    RuntimeHealth,
    RuntimeRecoveryPermit,
    RuntimeState,
)

_ACCOUNT_ID_PATTERN = re.compile(r"acct_[A-Za-z0-9_-]{3,48}")
_COMMAND_ID_PATTERN = re.compile(r"cmd_[A-Za-z0-9_-]{8,64}")
_REASON_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{2,63}")


class LocalControlReceiptStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    PROCESSING = "PROCESSING"
    APPLIED = "APPLIED"
    NOOP = "NOOP"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    UNRESOLVED = "UNRESOLVED"

    @property
    def terminal(self) -> bool:
        return self not in {
            LocalControlReceiptStatus.ACCEPTED,
            LocalControlReceiptStatus.PROCESSING,
        }


@dataclass(frozen=True, slots=True)
class LocalControlCommandReceipt:
    request: LocalControlRequest
    status: LocalControlReceiptStatus
    updated_at: ShanghaiTimestamp
    previous_state: RuntimeState | None = None
    current_state: RuntimeState | None = None
    transition_sequence: int | None = None
    rejection_code: str | None = None
    blockers: tuple[str, ...] = ()
    error_code: str | None = None
    retryable: bool = False
    requires_reconciliation: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.request, LocalControlRequest):
            raise ValueError("request must be a LocalControlRequest")
        if not isinstance(self.status, LocalControlReceiptStatus):
            raise ValueError("status must be a LocalControlReceiptStatus")
        if not isinstance(self.updated_at, ShanghaiTimestamp):
            raise ValueError("updated_at must be a ShanghaiTimestamp")
        if type(self.retryable) is not bool or type(self.requires_reconciliation) is not bool:
            raise ValueError("receipt flags must be booleans")
        if not isinstance(self.blockers, tuple) or not all(
            isinstance(value, str) and _REASON_PATTERN.fullmatch(value) for value in self.blockers
        ):
            raise ValueError("blockers must be immutable reason codes")
        if (
            self.rejection_code is not None
            and _REASON_PATTERN.fullmatch(self.rejection_code) is None
        ):
            raise ValueError("rejection_code has an invalid format")
        if self.error_code is not None and _REASON_PATTERN.fullmatch(self.error_code) is None:
            raise ValueError("error_code has an invalid format")

        if self.status in {
            LocalControlReceiptStatus.ACCEPTED,
            LocalControlReceiptStatus.PROCESSING,
        }:
            if (
                any(
                    value is not None
                    for value in (
                        self.previous_state,
                        self.current_state,
                        self.transition_sequence,
                        self.rejection_code,
                        self.error_code,
                    )
                )
                or self.blockers
            ):
                raise ValueError("non-terminal receipt cannot contain terminal details")
            if self.retryable or self.requires_reconciliation:
                raise ValueError("non-terminal receipt cannot contain failure flags")
        elif self.status in {
            LocalControlReceiptStatus.APPLIED,
            LocalControlReceiptStatus.NOOP,
            LocalControlReceiptStatus.REJECTED,
        }:
            self._validate_transition_receipt()
        elif self.status is LocalControlReceiptStatus.FAILED:
            if self.error_code is None or self.rejection_code is not None:
                raise ValueError("failed receipt requires only an error code")
        elif (
            self.error_code != "LOCAL_CONTROL_UNRESOLVED"
            or not self.requires_reconciliation
            or self.retryable
            or any(
                value is not None
                for value in (
                    self.previous_state,
                    self.current_state,
                    self.transition_sequence,
                    self.rejection_code,
                )
            )
            or self.blockers
        ):
            raise ValueError("unresolved receipt is inconsistent")

    def _validate_transition_receipt(self) -> None:
        if not isinstance(self.previous_state, RuntimeState) or not isinstance(
            self.current_state, RuntimeState
        ):
            raise ValueError("terminal transition receipt requires runtime states")
        if self.error_code is not None or self.retryable or self.requires_reconciliation:
            raise ValueError("terminal transition receipt cannot contain failure details")
        if self.status is LocalControlReceiptStatus.APPLIED:
            if (
                self.transition_sequence is None
                or self.transition_sequence < 1
                or self.rejection_code is not None
                or self.current_state is not self.request.action.target_state
            ):
                raise ValueError("applied receipt is inconsistent")
        elif self.status is LocalControlReceiptStatus.NOOP:
            if (
                self.transition_sequence is not None
                or self.rejection_code is not None
                or self.previous_state is not self.current_state
                or self.current_state is not self.request.action.target_state
            ):
                raise ValueError("noop receipt is inconsistent")
        elif (
            self.transition_sequence is not None
            or self.rejection_code is None
            or self.previous_state is not self.current_state
        ):
            raise ValueError("rejected receipt is inconsistent")


class LocalControlSubmissionFailureKind(StrEnum):
    CONFLICT = "CONFLICT"
    NOT_FOUND = "NOT_FOUND"
    UNAVAILABLE = "UNAVAILABLE"


class LocalControlSubmissionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        kind: LocalControlSubmissionFailureKind,
    ) -> None:
        if _REASON_PATTERN.fullmatch(code) is None:
            raise ValueError("code has an invalid format")
        if not isinstance(kind, LocalControlSubmissionFailureKind):
            raise ValueError("kind must be a LocalControlSubmissionFailureKind")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.detail = message
        self.kind = kind


class LocalControlReceiptRegistry:
    """Thread-safe bounded progress cache; durable results remain in LocalJournal."""

    def __init__(self, *, capacity: int = 1024) -> None:
        if type(capacity) is not int or not 1 <= capacity <= 100_000:
            raise ValueError("capacity must be between 1 and 100000")
        self._capacity = capacity
        self._receipts: OrderedDict[str, LocalControlCommandReceipt] = OrderedDict()
        self._lock = Lock()

    def reserve(
        self,
        request: LocalControlRequest,
        *,
        accepted_at: ShanghaiTimestamp,
    ) -> tuple[LocalControlCommandReceipt, bool]:
        if not isinstance(request, LocalControlRequest):
            raise ValueError("request must be a LocalControlRequest")
        if not isinstance(accepted_at, ShanghaiTimestamp):
            raise ValueError("accepted_at must be a ShanghaiTimestamp")
        with self._lock:
            existing = self._receipts.get(request.command_id)
            if existing is not None:
                if existing.request != request:
                    raise LocalControlSubmissionError(
                        "LOCAL_CONTROL_IDEMPOTENCY_CONFLICT",
                        "command identity already belongs to different content",
                        kind=LocalControlSubmissionFailureKind.CONFLICT,
                    )
                return existing, False
            self._make_room()
            receipt = LocalControlCommandReceipt(
                request=request,
                status=LocalControlReceiptStatus.ACCEPTED,
                updated_at=accepted_at,
            )
            self._receipts[request.command_id] = receipt
            return receipt, True

    def get(self, command_id: str) -> LocalControlCommandReceipt | None:
        _validate_command_id(command_id)
        with self._lock:
            return self._receipts.get(command_id)

    def mark_processing(
        self, request: LocalControlRequest, *, updated_at: ShanghaiTimestamp
    ) -> LocalControlCommandReceipt:
        receipt = LocalControlCommandReceipt(
            request=request,
            status=LocalControlReceiptStatus.PROCESSING,
            updated_at=updated_at,
        )
        return self._replace(request, receipt)

    def mark_result(
        self,
        result: LocalControlResult,
    ) -> LocalControlCommandReceipt:
        status = LocalControlReceiptStatus(result.outcome.value)
        receipt = LocalControlCommandReceipt(
            request=result.request,
            status=status,
            updated_at=result.decided_at,
            previous_state=result.previous_state,
            current_state=result.current_state,
            transition_sequence=result.transition_sequence,
            rejection_code=result.rejection_code,
            blockers=result.blockers,
        )
        return self._replace(result.request, receipt)

    def mark_failed(
        self,
        request: LocalControlRequest,
        *,
        updated_at: ShanghaiTimestamp,
        error_code: str,
        current_state: RuntimeState | None,
        retryable: bool,
        requires_reconciliation: bool,
    ) -> LocalControlCommandReceipt:
        receipt = LocalControlCommandReceipt(
            request=request,
            status=LocalControlReceiptStatus.FAILED,
            updated_at=updated_at,
            current_state=current_state,
            error_code=error_code,
            retryable=retryable,
            requires_reconciliation=requires_reconciliation,
        )
        return self._replace(request, receipt)

    def discard_accepted(self, request: LocalControlRequest) -> None:
        with self._lock:
            existing = self._receipts.get(request.command_id)
            if (
                existing is not None
                and existing.request == request
                and existing.status is LocalControlReceiptStatus.ACCEPTED
            ):
                del self._receipts[request.command_id]

    def _replace(
        self,
        request: LocalControlRequest,
        receipt: LocalControlCommandReceipt,
    ) -> LocalControlCommandReceipt:
        with self._lock:
            existing = self._receipts.get(request.command_id)
            if existing is None or existing.request != request:
                raise LocalControlSubmissionError(
                    "LOCAL_CONTROL_RECEIPT_NOT_RESERVED",
                    "command receipt was not reserved before dispatch",
                    kind=LocalControlSubmissionFailureKind.UNAVAILABLE,
                )
            self._receipts[request.command_id] = receipt
            self._receipts.move_to_end(request.command_id)
            return receipt

    def _make_room(self) -> None:
        if len(self._receipts) < self._capacity:
            return
        for command_id, receipt in tuple(self._receipts.items()):
            if receipt.status.terminal:
                del self._receipts[command_id]
                return
        raise LocalControlSubmissionError(
            "LOCAL_CONTROL_RECEIPT_CAPACITY_EXHAUSTED",
            "all local-control receipt slots are active",
            kind=LocalControlSubmissionFailureKind.UNAVAILABLE,
        )


@runtime_checkable
class LocalControlRecoveryReader(Protocol):
    async def load_recovery(
        self, *, account_id: str, trade_date: TradingDate
    ) -> JournalRecoverySnapshot: ...


class JournalLocalControlReceiptReader:
    def __init__(
        self,
        reader: LocalControlRecoveryReader,
        *,
        account_id: str,
        clock: Clock,
    ) -> None:
        if not isinstance(reader, LocalControlRecoveryReader):
            raise ValueError("reader must implement LocalControlRecoveryReader")
        if _ACCOUNT_ID_PATTERN.fullmatch(account_id) is None:
            raise ValueError("account_id has an invalid format")
        if not callable(getattr(clock, "now", None)):
            raise ValueError("clock must implement Clock")
        self._reader = reader
        self._account_id = account_id
        self._clock = clock

    async def find(self, command_id: str) -> PersistedLocalControlReceipt | None:
        _validate_command_id(command_id)
        snapshot = await self._reader.load_recovery(
            account_id=self._account_id,
            trade_date=self._clock.now().trading_date,
        )
        receipts = project_local_control_receipts(
            snapshot.controls,
            account_id=self._account_id,
        )
        return next(
            (receipt for receipt in receipts if receipt.request.command_id == command_id),
            None,
        )


@runtime_checkable
class LocalControlReceiptReader(Protocol):
    async def find(self, command_id: str) -> PersistedLocalControlReceipt | None: ...


@runtime_checkable
class LocalControlIngress(Protocol):
    def publish(
        self,
        payload: LocalControlRequest,
        *,
        source: RuntimeEventSource,
        priority: EventPriority = EventPriority.NORMAL,
    ) -> PublishOutcome: ...


class LocalControlSubmissionService:
    """Idempotently admit HTTP commands to the critical decision-loop channel."""

    def __init__(
        self,
        *,
        reader: LocalControlReceiptReader,
        ingress: LocalControlIngress,
        registry: LocalControlReceiptRegistry,
        clock: Clock,
    ) -> None:
        if not isinstance(reader, LocalControlReceiptReader):
            raise ValueError("reader must implement LocalControlReceiptReader")
        if not isinstance(ingress, LocalControlIngress):
            raise ValueError("ingress must implement LocalControlIngress")
        if not isinstance(registry, LocalControlReceiptRegistry):
            raise ValueError("registry must be a LocalControlReceiptRegistry")
        if not callable(getattr(clock, "now", None)):
            raise ValueError("clock must implement Clock")
        self._reader = reader
        self._ingress = ingress
        self._registry = registry
        self._clock = clock

    async def submit(self, request: LocalControlRequest) -> LocalControlCommandReceipt:
        if not isinstance(request, LocalControlRequest):
            raise ValueError("request must be a LocalControlRequest")
        cached = self._registry.get(request.command_id)
        if cached is not None:
            if cached.request != request:
                raise LocalControlSubmissionError(
                    "LOCAL_CONTROL_IDEMPOTENCY_CONFLICT",
                    "command identity already belongs to different content",
                    kind=LocalControlSubmissionFailureKind.CONFLICT,
                )
            return cached
        persisted = await self._find_persisted(request.command_id)
        if persisted is not None:
            if persisted.request != request:
                raise LocalControlSubmissionError(
                    "LOCAL_CONTROL_IDEMPOTENCY_CONFLICT",
                    "command identity already belongs to different durable content",
                    kind=LocalControlSubmissionFailureKind.CONFLICT,
                )
            return _receipt_from_persisted(persisted)

        receipt, created = self._registry.reserve(request, accepted_at=self._clock.now())
        if not created:
            return receipt
        try:
            outcome = self._ingress.publish(
                request,
                source=RuntimeEventSource.OPERATOR,
                priority=EventPriority.CRITICAL,
            )
        except Exception as exc:
            self._registry.discard_accepted(request)
            raise LocalControlSubmissionError(
                "LOCAL_CONTROL_EVENT_LOOP_UNAVAILABLE",
                "local-control event publication failed",
                kind=LocalControlSubmissionFailureKind.UNAVAILABLE,
            ) from exc
        if outcome is PublishOutcome.ACCEPTED:
            return receipt
        self._registry.discard_accepted(request)
        code = (
            "LOCAL_CONTROL_EVENT_QUEUE_FULL"
            if outcome is PublishOutcome.OVERFLOW
            else "LOCAL_CONTROL_EVENT_LOOP_UNAVAILABLE"
        )
        raise LocalControlSubmissionError(
            code,
            "local-control event was not accepted",
            kind=LocalControlSubmissionFailureKind.UNAVAILABLE,
        )

    async def get_receipt(self, command_id: str) -> LocalControlCommandReceipt:
        _validate_command_id(command_id)
        cached = self._registry.get(command_id)
        if cached is not None:
            return cached
        persisted = await self._find_persisted(command_id)
        if persisted is None:
            raise LocalControlSubmissionError(
                "LOCAL_CONTROL_RECEIPT_NOT_FOUND",
                "local-control receipt was not found",
                kind=LocalControlSubmissionFailureKind.NOT_FOUND,
            )
        return _receipt_from_persisted(persisted)

    async def _find_persisted(self, command_id: str) -> PersistedLocalControlReceipt | None:
        try:
            return await self._reader.find(command_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise LocalControlSubmissionError(
                "LOCAL_CONTROL_AUDIT_UNAVAILABLE",
                "durable local-control audit could not be read",
                kind=LocalControlSubmissionFailureKind.UNAVAILABLE,
            ) from exc


class LocalControlDecisionHandler:
    """Complete a submitted command inside the sole decision-loop owner task."""

    def __init__(
        self,
        coordinator: LocalControlCoordinator,
        registry: LocalControlReceiptRegistry,
        *,
        clock: Clock,
        health_supplier: Callable[[], RuntimeHealth | None] = lambda: None,
        permit_supplier: Callable[[], RuntimeRecoveryPermit | None] = lambda: None,
    ) -> None:
        if not isinstance(coordinator, LocalControlCoordinator):
            raise ValueError("coordinator must be a LocalControlCoordinator")
        if not isinstance(registry, LocalControlReceiptRegistry):
            raise ValueError("registry must be a LocalControlReceiptRegistry")
        if not callable(getattr(clock, "now", None)):
            raise ValueError("clock must implement Clock")
        if not callable(health_supplier) or not callable(permit_supplier):
            raise ValueError("control fact suppliers must be callable")
        self._coordinator = coordinator
        self._registry = registry
        self._clock = clock
        self._health_supplier = health_supplier
        self._permit_supplier = permit_supplier

    async def handle(self, request: LocalControlRequest) -> LocalControlResult | None:
        if not isinstance(request, LocalControlRequest):
            raise ValueError("request must be a LocalControlRequest")
        self._registry.mark_processing(request, updated_at=self._clock.now())
        try:
            result = await self._coordinator.apply(
                request,
                health=self._health_supplier(),
                permit=self._permit_supplier(),
            )
        except asyncio.CancelledError:
            self._registry.mark_failed(
                request,
                updated_at=self._clock.now(),
                error_code="LOCAL_CONTROL_HANDLER_CANCELLED",
                current_state=None,
                retryable=False,
                requires_reconciliation=True,
            )
            raise
        except LocalControlError as exc:
            self._registry.mark_failed(
                request,
                updated_at=self._clock.now(),
                error_code=exc.code,
                current_state=exc.fail_closed_state,
                retryable=exc.retryable,
                requires_reconciliation=exc.requires_reconciliation,
            )
            return None
        except Exception:
            self._registry.mark_failed(
                request,
                updated_at=self._clock.now(),
                error_code="LOCAL_CONTROL_HANDLER_FAILED",
                current_state=None,
                retryable=False,
                requires_reconciliation=True,
            )
            raise
        self._registry.mark_result(result)
        return result


def _receipt_from_persisted(
    persisted: PersistedLocalControlReceipt,
) -> LocalControlCommandReceipt:
    if not persisted.resolved:
        return LocalControlCommandReceipt(
            request=persisted.request,
            status=LocalControlReceiptStatus.UNRESOLVED,
            updated_at=persisted.request.requested_at,
            error_code="LOCAL_CONTROL_UNRESOLVED",
            requires_reconciliation=True,
        )
    assert persisted.outcome is not None
    assert persisted.decided_at is not None
    return LocalControlCommandReceipt(
        request=persisted.request,
        status=LocalControlReceiptStatus(persisted.outcome.value),
        updated_at=persisted.decided_at,
        previous_state=persisted.previous_state,
        current_state=persisted.current_state,
        transition_sequence=persisted.transition_sequence,
        rejection_code=persisted.rejection_code,
        blockers=persisted.blockers,
    )


def _validate_command_id(command_id: str) -> None:
    if not isinstance(command_id, str) or _COMMAND_ID_PATTERN.fullmatch(command_id) is None:
        raise ValueError("command_id has an invalid format")
