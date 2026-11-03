"""Single-owner V2 execution kernel: recovery, durable approvals and J1 dispatch.

All public state-changing methods except broker_event()/emergency_halt() run on
the owner thread. poll() never waits on unfinished futures. This kernel has no
HTTP, QMT import or database connection; the offline application wires a Mock
gateway, account reader and explicit local approvals. Real broker admission and
the local authenticated IPC adapter remain separate release work.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from queue import Empty, Full, Queue
from threading import Lock, get_ident
from time import monotonic
from typing import TYPE_CHECKING, Literal, cast
from uuid import uuid4

from pydantic import Field, JsonValue, field_validator

if TYPE_CHECKING:
    from auto_trading_system.runtime.execution_reporting import ExecutionReporter
    from auto_trading_system.runtime.tail_feed import LocalTailFeed

from auto_trading_system.control.approval_v2 import ApprovalPort
from auto_trading_system.execution.dispatch_boundary import DispatchPermission, SDKDispatcher
from auto_trading_system.execution.entry_budget import BuyExposure, buy_exposures
from auto_trading_system.execution.plan_control import EnginePlanControl
from auto_trading_system.execution.sell_policy import EXIT_RULE, allows_sell
from auto_trading_system.execution.submission_coordinator import (
    SubmissionCoordinator,
    SubmissionStatus,
)
from auto_trading_system.gateways.broker.models import (
    BrokerCancelError,
    BrokerEvent,
    BrokerOrder,
    BrokerOrderIdentity,
    BrokerOrderStatus,
    BrokerOrderUpdate,
    BrokerRequestError,
    BrokerTrade,
)
from auto_trading_system.gateways.broker.port import BrokerGateway
from auto_trading_system.gateways.broker.query_validation import (
    validate_order_query_result,
    validate_trade_query_result,
)
from auto_trading_system.journal.checkpoint_blobs import (
    write_checkpoint,
)
from auto_trading_system.journal.critical_models import RecoveryRecord, Submission
from auto_trading_system.journal.critical_writer import CriticalJournalOwner, CriticalStorePort
from auto_trading_system.orders.projection import OrderProjection, OrderView
from auto_trading_system.protocol.qmt_recovery import QmtOrderRecoveryBinding
from auto_trading_system.protocol.v2.api import PlanLookup, PlanReceipt
from auto_trading_system.protocol.v2.base import WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.daily_sync import (
    LocalControlRequest,
    LocalControlResult,
    SessionGrant,
)
from auto_trading_system.protocol.v2.plans import Allocation, PlanPayload
from auto_trading_system.protocol.v2.records import MoneyText
from auto_trading_system.reconcile.historical_opening import LoadedOpening, load_recovery_opening
from auto_trading_system.reconcile.journal_scan import JournalScan
from auto_trading_system.reconcile.portfolio_checkpoint import (
    OpeningReference,
    PortfolioCheckpoint,
    closing_key,
)
from auto_trading_system.reconcile.position_lots import (
    ExchangeCalendar,
    PortfolioProjection,
    PositionLot,
)
from auto_trading_system.runtime.cancel_state import CancelFailure, failure_key, load_failures
from auto_trading_system.runtime.day_snapshot import DaySnapshot
from auto_trading_system.runtime.execution_scope import ExecutionScope, execution_scopes
from auto_trading_system.runtime.memory_state import ReservationBook
from auto_trading_system.runtime.observation_v2 import (
    CheckedObservation,
    check_empty_observation,
    check_observation,
)
from auto_trading_system.runtime.qmt_order_identities import (
    identity_key,
    load_order_identities,
    order_identity,
)
from auto_trading_system.runtime.recovery_v2 import (
    AccountFact,
    BrokerRecoverySnapshot,
    RecoveryReport,
    reconcile,
    recovery_evidence,
)
from auto_trading_system.strategies.entry_state import EntryPolicy, EntryTarget
from auto_trading_system.strategies.exit_context import ExitSessionContext
from auto_trading_system.strategies.trading_settings import (
    TradingBoard,
    TradingSettings,
    stock_board,
)
from auto_trading_system.sync.daily_export import export_order, export_trade

CN = timezone(timedelta(hours=8))


class EngineLimits(WireModel):
    """Installed local ceiling, not a permission supplied by a remote plan."""

    mode: Literal["PAPER", "SHADOW", "LIVE"] = "PAPER"
    max_buy_value: MoneyText
    max_order_value: MoneyText
    max_positions: int = Field(ge=0, le=15)
    max_order_quantity: int = Field(ge=1, le=2**31 - 1)
    max_daily_loss: MoneyText = Field(default="0", exclude=True)
    max_symbol_buy_value: MoneyText | None = Field(default=None, exclude_if=lambda v: v is None)
    max_total_position_pct: MoneyText | None = Field(default=None, exclude_if=lambda v: v is None)
    max_daily_symbols: int | None = Field(default=None, ge=1, le=15, exclude_if=lambda v: v is None)
    allowed_boards: tuple[TradingBoard, ...] = Field(default=("MAIN",), max_length=3, exclude=True)

    @field_validator("max_total_position_pct")
    @classmethod
    def valid_position_pct(cls, value: str | None) -> str | None:
        if value is not None and not Decimal("0") < Decimal(value) <= Decimal("100"):
            raise ValueError("position percentage must be greater than zero and at most 100")
        return value


@dataclass(frozen=True)
class EngineSnapshot:
    state: str
    state_version: int
    permission_epoch: int
    halted: bool
    in_flight: tuple[str, ...]
    recovery: RecoveryReport | None
    grant_id: str | None
    submissions: tuple[SubmissionStatus, ...]
    orders: tuple[OrderView, ...]
    lots: tuple[PositionLot, ...]
    observation_version: int
    observation_pending: bool
    account_observed_at: datetime | None
    recovery_scanned_records: int
    retained_recovery_records: int


@dataclass
class _Control:
    request: LocalControlRequest
    expected_epoch: int
    event_version: int
    deadline: float
    write: Future[bool]


class EngineKernel:
    def __init__(
        self,
        *,
        account_id: str,
        boot_id: str,
        trade_date: date,
        journal: CriticalJournalOwner,
        dispatcher: SDKDispatcher,
        permission: DispatchPermission,
        plans: EnginePlanControl,
        approvals: ApprovalPort,
        account_reader: Callable[[], AccountFact],
        clock: Callable[[], datetime],
        limits: EngineLimits,
        risk_guard: Callable[[Submission], bool],
        calendar: ExchangeCalendar,
        opening_reference: OpeningReference | None = None,
        exit_context: ExitSessionContext | None = None,
        exit_state_guard: Callable[[Submission], bool] | None = None,
        entry_state_guard: Callable[[Submission], bool] | None = None,
        entry_target_reader: Callable[[], tuple[EntryTarget, ...]] | None = None,
        risk_baseline_equity: MoneyText | None = None,
    ) -> None:
        if plans.account_id != account_id or plans.boot_id != boot_id:
            raise ValueError("engine plan scope mismatch")
        self._owner = get_ident()
        self.account_id, self.boot_id, self.trade_date = account_id, boot_id, trade_date
        self.journal, self.dispatcher, self.permission = journal, dispatcher, permission
        self.plans, self.approvals = plans, approvals
        self._account_reader, self._clock = account_reader, clock
        self.limits, self._risk_guard = limits, risk_guard
        # SELL retains its installed ceilings; live BUY settings have a separate lifecycle.
        self.buy_limits = limits
        self.buy_settings: TradingSettings | None = None
        self.buy_settings_version: int | None = None
        self.buy_enabled = True
        self.buy_settings_error: str | None = None
        self._buy_grant: SessionGrant | None = None
        self._calendar = calendar
        self._opening_reference = opening_reference
        self.exit_context = exit_context
        self._exit_state_guard = exit_state_guard
        self.board_exit_contexts: dict[str, ExitSessionContext] = {}
        self._board_exit_attached: set[str] = set()
        self._board_exit_guards: dict[str, Callable[[Submission], bool]] = {}
        self._entry_state_guard = entry_state_guard
        self._entry_target_reader = entry_target_reader
        self._entry_policy_reader: Callable[[], tuple[EntryPolicy, ...]] | None = None
        self.live_exit_gate: object | None = None
        self.local_tail_feed: LocalTailFeed | None = None
        self.execution_reporter: ExecutionReporter | None = None
        self._next_execution_capture = 0.0
        if exit_context is not None:
            exit_context = ExitSessionContext.model_validate_json(exit_context.model_dump_json())
            if (
                exit_state_guard is None
                or opening_reference is None
                or exit_context.account_id != account_id
                or exit_context.trade_date != trade_date
                or exit_context.opening_hash != opening_reference.payload_hash
                or exit_context.calendar_hash
                != digest(canonical_bytes(calendar.model_dump(mode="json")))
            ):
                raise ValueError("exit context must bind this engine and verified opening/calendar")
            self.exit_context = exit_context
        self._opening: tuple[PositionLot, ...] = ()
        self._scan: JournalScan | None = None
        self._opening_read: Future[LoadedOpening] | None = None
        self._opening_proof: bytes | None = None
        self._sealed = False
        self._order_state = OrderProjection()
        self._portfolio = PortfolioProjection((), frozenset(), (), ())
        self._observation: Future[BrokerRecoverySnapshot] | None = None
        self._observed: CheckedObservation | None = None
        self._observation_epoch = 0
        self._observation_deadline = 0.0
        self._next_observation = monotonic() + 2
        self._observation_version = 0
        self._event_lock = Lock()
        self._event_version = 0
        self._inbox: Queue[BrokerEvent] = Queue(maxsize=1024)
        self._overflow = False
        self._orders: dict[str, BrokerOrder] = {}
        self._last_now = self._now()
        self._state = "STARTUP_PAUSED"
        self._version = 1
        self._epoch = permission.snapshot().epoch
        self._grant: SessionGrant | None = None
        self._board_exit_grants: dict[str, SessionGrant] = {}
        self._grant_generation = 0
        self._confirmed: str | None = None
        self._report: RecoveryReport | None = None
        self._broker_snapshot: BrokerRecoverySnapshot | None = None
        self._risk_baseline_equity = (
            Decimal(risk_baseline_equity) if risk_baseline_equity is not None else None
        )
        self._records: list[RecoveryRecord] = []
        self._known: dict[str, Submission] = {}
        self._manual_permits: dict[str, tuple[Submission, float]] = {}
        self.manual_sell_locks: set[str] = set()
        self.market_data_failure: str | None = None
        self._identity_saved: dict[str, str] = {}
        self._identity_read: Future[tuple[QmtOrderRecoveryBinding, ...]] | None = None
        self._cancel_permits: dict[str, tuple[Submission, float, BrokerRecoverySnapshot]] = {}
        self._cancel_failures: dict[str, CancelFailure] = {}
        self._cancel_failure_pending: dict[str, tuple[CancelFailure, Future[bool], float]] = {}
        self._cancel_failure_attempted: set[str] = set()
        self._manual_watching_until = 0.0
        self._book = ReservationBook()
        self._page: Future[tuple[RecoveryRecord, ...]] | None = None
        self._query: Future[BrokerRecoverySnapshot] | None = None
        self._checkpoint: Future[tuple[bool, dict[str, CancelFailure]]] | None = None
        self._recovery_deadline = 0.0
        self._recovery_epoch = 0
        self._recovery_stage = "RECOVERY"
        self._recovery_error: str | None = None
        self._pending: _Control | None = None
        self._requests: dict[str, tuple[str, LocalControlResult]] = {}
        self._audit: list[tuple[Future[bool], float]] = []
        self._coordinator = SubmissionCoordinator(journal, dispatcher, permission, self._allows)

    def _owner_only(self) -> None:
        if get_ident() != self._owner:
            raise RuntimeError("engine state has a single owner")

    def install_entry_runtime(
        self,
        guard: Callable[[Submission], bool],
        target_reader: Callable[[], tuple[EntryTarget, ...]],
        policy_reader: Callable[[], tuple[EntryPolicy, ...]] | None = None,
    ) -> None:
        """Attach the live entry state guard once, before a session is armed."""
        self._owner_only()
        if (
            self._entry_state_guard is not None
            or self._entry_target_reader is not None
            or (self._grant is not None and "BUY" in self._grant.directions)
        ):
            raise ValueError("entry runtime cannot be replaced in this boot")
        self._entry_state_guard = guard
        self._entry_target_reader = target_reader
        self._entry_policy_reader = policy_reader

    def install_exit_runtime(self, guard: Callable[[Submission], bool]) -> None:
        """Attach the live exit state guard once, before a session is armed."""
        self._owner_only()
        if (
            self.exit_context is None
            or self._exit_state_guard is not None
            or self._grant is not None
        ):
            raise ValueError("exit runtime cannot be replaced in this boot")
        self._exit_state_guard = guard

    def install_board_exit(
        self,
        context: ExitSessionContext,
        guard: Callable[[Submission], bool],
        *,
        restoring: bool = False,
    ) -> None:
        """Register a frozen board scope; registration never grants trading authority."""
        self._owner_only()
        context = ExitSessionContext.model_validate_json(context.model_dump_json())
        if context.board in self.board_exit_contexts:
            if (
                self.board_exit_contexts[context.board] != context
                or context.payload_hash in self._board_exit_attached
            ):
                raise ValueError("board context cannot be replaced")
            self._board_exit_guards[context.payload_hash] = guard
            self._board_exit_attached.add(context.payload_hash)
            return
        if (
            self.exit_context is not None
            or context.board is None
            or context.account_id != self.account_id
            or context.trade_date != self.trade_date
            or (not restoring and context.opening_hash != self._opening_hash)
            or context.calendar_hash
            != digest(canonical_bytes(self._calendar.model_dump(mode="json")))
            or context.board in self.board_exit_contexts
            or any(
                a.lot_id == b.lot_id
                for current in self.board_exit_contexts.values()
                for a in current.lots
                for b in context.lots
            )
        ):
            raise ValueError("board exit scope conflicts with installed ownership")
        opening = {lot.lot_id: lot for lot in self._opening}
        if not restoring and any(
            p.lot_id not in opening
            or opening[p.lot_id].symbol != p.symbol
            or opening[p.lot_id].strategy_id != context.strategy_id
            for p in context.lots
        ):
            raise ValueError("board exit requires verified opening lots")
        self.board_exit_contexts[context.board] = context
        self._board_exit_guards[context.payload_hash] = guard
        if not restoring:
            self._board_exit_attached.add(context.payload_hash)

    def retain_board_sell_grant(self, context_hash: str) -> None:
        """Retain only an already durably authorized SELL grant for an installed board."""
        self._owner_only()
        grant = self._grant
        if (
            context_hash not in self._board_exit_guards
            or grant is None
            or "SELL" not in grant.directions
            or self.permission.snapshot().halted
            or not grant.valid_from <= self._now() < grant.valid_until
        ):
            raise ValueError("BOARD_SELL_AUTHORIZATION_REQUIRED")
        self._board_exit_grants[context_hash] = grant

    def exit_context_for(self, payload_hash: str) -> ExitSessionContext | None:
        contexts = (
            *self.board_exit_contexts.values(),
            *((self.exit_context,) if self.exit_context else ()),
        )
        return next((c for c in contexts if c.payload_hash == payload_hash), None)

    def _now(self) -> datetime:
        now = self._clock()
        if now.utcoffset() is None:
            raise ValueError("engine clock must be timezone aware")
        return now.astimezone(CN)

    def broker_event(self, event: BrokerEvent) -> None:
        """Immutable gateway values enter a bounded queue; callback performs no SQL."""
        with self._event_lock:
            if not isinstance(event, BrokerOrderIdentity):
                self._event_version += 1
            try:
                self._inbox.put_nowait(event)
            except Full:
                self._overflow = True
                self.permission.halt("BROKER_EVENT_OVERFLOW")

    def _events(self) -> int:
        with self._event_lock:
            return self._event_version

    def _save_audit(
        self, table: Literal["broker_facts", "control_records"], key: str, body: bytes
    ) -> None:
        if len(self._audit) >= 1024:
            self._tighten("CRITICAL_FACT_CAPACITY")
            return
        deadline = monotonic() + 0.25
        self._audit.append(
            (
                self.journal.request(
                    lambda store: store.append_evidence(table, key, body),
                    deadline=deadline,
                ),
                deadline,
            )
        )

    def _save_order_identity(self, order: BrokerOrder) -> None:
        original = self._known.get(order.client_order_id)
        if original is None:
            return
        body = order_identity(original, order)
        if body is None:
            return
        fingerprint = digest(body)
        previous = self._identity_saved.get(order.client_order_id)
        if previous == fingerprint:
            return
        if previous is not None:
            raise ValueError("QMT_ORDER_IDENTITY_CONFLICT")
        self._identity_saved[order.client_order_id] = fingerprint
        self._save_audit("broker_facts", identity_key(order.client_order_id), body)

    def _record_cancel_failure(
        self, identity: str, broker_id: str | None, reason: str, occurred_at: datetime
    ) -> None:
        item = self._known.get(identity)
        if item is not None and item.action_type == "NEW":
            # Legacy callbacks contain only the original order ID. Never assign
            # an ambiguous late callback to a later retry.
            candidates = [
                s
                for s in self._known.values()
                if s.action_type == "CANCEL"
                and s.original_client_order_id == identity
                and s.original_order_id == broker_id
            ]
            item = candidates[0] if len(candidates) == 1 else None
        if (
            item is None
            or item.action_type != "CANCEL"
            or (broker_id is not None and item.original_order_id != broker_id)
            or item.client_order_id in self._cancel_failures
            or item.client_order_id in self._cancel_failure_pending
            or item.client_order_id in self._cancel_failure_attempted
            or occurred_at.date() != self.trade_date
            or occurred_at > self._now()
        ):
            return
        if len(self._audit) >= 1024:
            self._tighten("CRITICAL_FACT_CAPACITY")
            return
        failure = CancelFailure(
            cancel_request_id=item.client_order_id,
            submission_hash=digest(canonical_bytes(item.model_dump(mode="json"))),
            reason_code=reason,
            occurred_at=occurred_at,
        )
        body = canonical_bytes(failure.model_dump(mode="json"))
        deadline = monotonic() + 0.25
        future = self.journal.request(
            lambda store: store.append_evidence(
                "broker_facts", failure_key(item.client_order_id), body
            ),
            deadline=deadline,
        )
        self._cancel_failure_pending[item.client_order_id] = (failure, future, deadline)
        self._cancel_failure_attempted.add(item.client_order_id)
        self._audit.append((future, deadline))

    def _poll_cancel_failures(self) -> None:
        for identity, (failure, future, deadline) in tuple(self._cancel_failure_pending.items()):
            if not future.done():
                continue
            del self._cancel_failure_pending[identity]
            try:
                future.result()
                if monotonic() < deadline and self.journal.healthy:
                    self._cancel_failures[identity] = failure
            except Exception:
                self._tighten("CANCEL_OUTCOME_PERSISTENCE_FAILED")
        for status in self._coordinator.snapshot():
            item = self._known.get(status.client_order_id)
            if (
                item is not None
                and item.action_type == "CANCEL"
                and status.status == "NOT_DISPATCHED"
            ):
                self._record_cancel_failure(
                    item.client_order_id,
                    item.original_order_id,
                    "CANCEL_NOT_DISPATCHED",
                    self._now(),
                )

    def _cancel_blocks(self, item: Submission) -> bool:
        return item.client_order_id not in self._cancel_failures

    def _cancel_state(self, order: BrokerOrder) -> tuple[str, str | None]:
        requests = [
            s
            for s in self._known.values()
            if s.action_type == "CANCEL" and s.original_client_order_id == order.client_order_id
        ]
        if order.is_terminal:
            return (
                "CONFIRMED"
                if order.status
                in {BrokerOrderStatus.CANCELLED, BrokerOrderStatus.PARTIALLY_FILLED_CANCELLED}
                else "NONE"
            ), None
        if order.status in {
            BrokerOrderStatus.CANCEL_PENDING,
            BrokerOrderStatus.PARTIAL_FILL_CANCEL_PENDING,
        }:
            return "PENDING", None
        blocking = [s for s in requests if self._cancel_blocks(s)]
        if blocking:
            statuses = {s.client_order_id: s.status for s in self._coordinator.snapshot()}
            unknown = any(
                s.client_order_id in self._cancel_failure_attempted
                or statuses.get(s.client_order_id)
                not in {"PERSISTING", "DISPATCHING", "SDK_ACCEPTED"}
                for s in blocking
            )
            return ("UNKNOWN" if unknown else "PENDING"), None
        if requests:
            return "FAILED", self._cancel_failures[requests[-1].client_order_id].reason_code
        return "NONE", None

    def _drain_events(self) -> None:
        for _ in range(100):
            try:
                event = self._inbox.get_nowait()
            except Empty:
                break
            try:
                if isinstance(event, BrokerOrderIdentity):
                    self._save_order_identity(event.order)
                    continue
                if isinstance(event, BrokerOrderUpdate):
                    order = event.order
                    self._save_order_identity(order)
                    self._orders[order.client_order_id] = self._order_state.observe_order(order)
                    record = export_order(
                        order,
                        account_id=self.account_id,
                        trade_date=self.trade_date,
                        observed_at=order.updated_at.value,
                    )
                elif isinstance(event, BrokerTrade):
                    self._order_state.observe_trade(event)
                    record = export_trade(
                        event,
                        account_id=self.account_id,
                        trade_date=self.trade_date,
                        observed_at=event.traded_at.value,
                        origin="UNKNOWN",
                    )
                elif isinstance(event, BrokerRequestError):
                    self._tighten("BROKER_REQUEST_REQUIRES_RECONCILIATION")
                    if event.action_type == "CANCEL":
                        self._record_cancel_failure(
                            event.client_order_id, None, event.reason_code, event.occurred_at.value
                        )
                    failure_body = canonical_bytes(
                        {
                            "kind": "REQUEST_ERROR",
                            "client_order_id": event.client_order_id,
                            "action": event.action_type,
                            "reason_code": event.reason_code,
                            "occurred_at": event.occurred_at.value.isoformat(),
                        }
                    )
                    self._save_audit(
                        "broker_facts", "request-error-" + digest(failure_body)[7:], failure_body
                    )
                    if self.execution_reporter is not None:
                        self.execution_reporter.broker(
                            event, self._known.get(event.client_order_id)
                        )
                    continue
                elif isinstance(event, BrokerCancelError):
                    # The typed reason code is enough here; arbitrary SDK text
                    # never enters local logs or trusted control responses.
                    self._tighten("CANCEL_REQUIRES_RECONCILIATION")
                    self._record_cancel_failure(
                        event.client_order_id,
                        event.broker_order_id,
                        event.error_code,
                        event.occurred_at.value,
                    )
                    if self.execution_reporter is not None:
                        self.execution_reporter.broker(
                            event, self._known.get(event.client_order_id)
                        )
                    continue
                else:
                    raise ValueError("unrecognized broker callback")
                body = canonical_bytes(record.model_dump(mode="json"))
                self._save_audit("broker_facts", "fact-" + digest(body)[7:], body)
                if self.execution_reporter is not None:
                    reported = (
                        BrokerOrderUpdate(self._orders[event.order.client_order_id])
                        if isinstance(event, BrokerOrderUpdate)
                        else event
                    )
                    self.execution_reporter.broker(
                        reported,
                        self._known.get(
                            reported.order.client_order_id
                            if isinstance(reported, BrokerOrderUpdate)
                            else reported.client_order_id
                        ),
                    )
            except Exception:
                self._tighten("BROKER_FACT_INVALID")

    def emergency_halt(self, reason: str = "LOCAL_HALT") -> None:
        """May be invoked on an authenticated emergency thread; never waits on I/O."""
        self.permission.halt(reason)

    def current_buy_grant(self) -> SessionGrant | None:
        grant = self._grant
        if grant is None or "BUY" not in grant.directions:
            return None
        return (
            self._buy_grant
            if self._buy_grant and self._buy_grant.grant_id == grant.grant_id
            else grant
        )

    def entry_allocations(
        self, plan: PlanPayload, settings: TradingSettings | None = None
    ) -> tuple[Allocation, ...]:
        """Local execution amendments retain the signed original selection identity."""
        settings = settings or self.buy_settings
        feed = self.local_tail_feed
        if settings is None or feed is None:
            return plan.allocations
        stocks = None
        if (
            feed.snapshot is not None
            and plan.source.algorithm_version == "local-tail-feed-adapter-v1"
        ):
            for batch in feed.snapshot.batches:
                if (
                    batch.phase == plan.source.decision_phase
                    and batch.status == "READY"
                    and digest(canonical_bytes(batch.model_dump(mode="json")))
                    == plan.source.source_hash
                ):
                    stocks = batch.stocks
                    break
        if stocks is None:
            return tuple(
                a.model_copy(
                    update={
                        "target_value": settings.buy.max_symbol_buy_value,
                        "max_value": settings.buy.max_symbol_buy_value,
                        "max_qty": settings.advanced.max_order_quantity
                        * (settings.buy.max_retries + 1),
                    }
                )
                for a in plan.allocations
            )
        return tuple(
            Allocation(
                allocation_id=f"local-{stock.rank}",
                symbol=stock.code,
                rank=stock.rank,
                target_value=settings.buy.max_symbol_buy_value,
                max_value=settings.buy.max_symbol_buy_value,
                max_qty=settings.advanced.max_order_quantity * (settings.buy.max_retries + 1),
            )
            for stock in stocks
        )

    def _tighten(self, reason: str) -> None:
        self.permission.halt(reason)
        self._notice_permission()

    def _notice_permission(self) -> None:
        permission = self.permission.snapshot()
        if permission.epoch != self._epoch:
            self._epoch = permission.epoch
            self._version += 1
            if permission.halted:
                self._grant = None
                self._confirmed = None
                self._state = "HALTED"

    def apply(self, lookup: PlanLookup, artifact_hash: str | None) -> PlanReceipt:
        self._owner_only()
        self.poll()
        if self._pending:
            raise ValueError("control persistence is pending")
        try:
            receipt = self.plans.apply(lookup, artifact_hash)
        except (OSError, RuntimeError):
            self.emergency_halt("PLAN_ACCEPTANCE_PERSISTENCE_FAILED")
            raise
        self._notice_permission()
        self._version += 1
        return receipt

    def start_recovery(self) -> None:
        self._owner_only()
        self.poll()
        if self._pending or any(
            (
                self._page,
                self._query,
                self._checkpoint,
                self._opening_read,
                self._identity_read,
                self._observation,
                self._observed,
            )
        ):
            raise ValueError("recovery or control is already pending")
        if not self.dispatcher.idle or any(
            s.status in ("PERSISTING", "DISPATCHING") for s in self._coordinator.snapshot()
        ):
            raise ValueError("wait for actual SDK and Journal work to settle")
        self._tighten("RECOVERY_REQUESTED")
        self._state = "RECOVERING"
        self._report, self._broker_snapshot = None, None
        self._recovery_error = None
        self._recovery_stage = "RECOVERY_JOURNAL_SCAN"
        self._records = []
        self._recovery_deadline = monotonic() + 30
        self._recovery_epoch = self.permission.snapshot().epoch
        self._scan = None
        self._opening_proof = None
        self._recovery_stage = "RECOVERY_OPENING_REFERENCE"
        self._opening_read = self.journal.request(
            lambda store: load_recovery_opening(
                store,
                self._opening_reference,
                account_id=self.account_id,
                today=self.trade_date,
                calendar=self._calendar,
            ),
            deadline=self._recovery_deadline,
        )

    def _capture(self, broker: BrokerGateway) -> BrokerRecoverySnapshot:
        start = self._events()
        orders = validate_order_query_result(broker.query_orders())
        trades = validate_trade_query_result(broker.query_trades())
        account = self._account_reader()
        end = self._events()
        return BrokerRecoverySnapshot(
            orders, trades, account, end, start == end and not self._overflow
        )

    def _poll_recovery(self) -> None:
        if not any(
            (self._page, self._query, self._checkpoint, self._opening_read, self._identity_read)
        ):
            return
        if (
            monotonic() >= self._recovery_deadline
            or self.permission.snapshot().epoch != self._recovery_epoch
        ):
            self._page = self._query = self._checkpoint = None
            self._opening_read = None
            self._identity_read = None
            self._report = None
            self._tighten("RECOVERY_DEADLINE_OR_PERMISSION_CHANGED")
            return
        try:
            if self._page and self._page.done():
                page = self._page.result()
                self._page = None
                assert self._scan is not None
                if page:
                    self._scan.consume(page)
                if len(page) == 200:
                    self._page = self.journal.recovery_page(page[-1].record_seq)
                else:
                    self._scan.finish()
                    self._records = self._scan.current
                    self._recovery_stage = "RECOVERY_BROKER_QUERY"
                    self._identity_read = self.journal.request(
                        lambda store: load_order_identities(store, tuple(self._records)),
                        deadline=self._recovery_deadline,
                    )
            if self._identity_read is not None and self._identity_read.done():
                identities = self._identity_read.result()
                self._identity_read = None

                def capture(broker: BrokerGateway) -> BrokerRecoverySnapshot:
                    restore = getattr(broker, "restore_order_identities", None)
                    if callable(restore):
                        restore(identities)
                    return self._capture(broker)

                self._query = self.dispatcher.observe(capture)
            if self._opening_read is not None and self._opening_read.done():
                loaded_opening = self._opening_read.result()
                self._opening_read = None
                if loaded_opening.error is not None:
                    raise ValueError(loaded_opening.error)
                checkpoint = loaded_opening.baseline
                self._opening_proof = loaded_opening.proof
                self._opening = checkpoint.lots if checkpoint is not None else ()
                self._scan = JournalScan(
                    account_id=self.account_id, trade_date=self.trade_date, checkpoint=checkpoint
                )
                self._recovery_stage = "RECOVERY_JOURNAL_SCAN"
                self._page = self.journal.recovery_page()
            if self._query and self._query.done():
                snapshot = self._query.result()
                self._query = None
                self._recovery_stage = "RECOVERY_RECONCILE"
                report = reconcile(
                    tuple(self._records),
                    snapshot,
                    account_id=self.account_id,
                    boot_id=self.boot_id,
                    trade_date=self.trade_date,
                    now=self._now(),
                    report_id="recovery-" + uuid4().hex,
                    opening=self._opening,
                    opening_hash=self._opening_hash,
                    allow_unmanaged=self.limits.mode == "LIVE",
                )
                body = canonical_bytes(
                    {
                        "report": report.model_dump(mode="json"),
                        "evidence": recovery_evidence(
                            tuple(self._records), snapshot, opening_hash=self._opening_hash
                        ),
                    }
                )
                self._report, self._broker_snapshot = report, snapshot
                self._recovery_stage = "RECOVERY_CHECKPOINT_WRITE"
                records = tuple(self._records)
                opening_proof = self._opening_proof

                def save_recovery(
                    store: CriticalStorePort,
                ) -> tuple[bool, dict[str, CancelFailure]]:
                    if opening_proof is not None:
                        write_checkpoint(
                            store, "recovered-opening:" + digest(opening_proof)[7:], opening_proof
                        )
                    return write_checkpoint(store, report.report_id, body), load_failures(
                        store, records
                    )

                self._checkpoint = self.journal.request(
                    save_recovery,
                    deadline=self._recovery_deadline,
                )
            if self._checkpoint and self._checkpoint.done():
                saved, failures = self._checkpoint.result()
                if not saved:
                    raise ValueError("recovery checkpoint was not newly created")
                self._cancel_failures = failures
                self._checkpoint = None
                assert self._report is not None
                self._known = {r.submission.client_order_id: r.submission for r in self._records}
                assert self._broker_snapshot is not None
                self._orders = {o.client_order_id: o for o in self._broker_snapshot.orders}
                if self._report.status == "CLEAR":
                    self._recovery_stage = "RECOVERY_ACCOUNT_PROJECTION"
                    checked = self._check_observation(self._broker_snapshot, self._order_state)
                    self._order_state, self._portfolio = checked.orders, checked.portfolio
                    for order in self._broker_snapshot.orders:
                        self._save_order_identity(order)
                    if (
                        self._risk_baseline_equity is None
                        and self._broker_snapshot.account.total_equity is not None
                    ):
                        self._risk_baseline_equity = Decimal(
                            self._broker_snapshot.account.total_equity
                        )
                    self._observation_version += 1
                    # Durable checkpoint + terminal order/trade/account evidence
                    # may release reservations, never old send-attempt tombstones.
                    for record in self._records:
                        key = record.submission.client_order_id
                        self.permission.resolve_terminal(key, confirmed=True)
                        self._book.resolve_terminal(key, reconciled=True)
                self._state = (
                    "REVIEW_REQUIRED" if self._report.status == "CLEAR" else "RECOVERY_BLOCKED"
                )
                self._version += 1
        except Exception as exc:
            self._page = self._query = self._checkpoint = None
            self._identity_read = None
            self._opening_read = None
            self._report = None
            code = exc.args[0] if exc.args else None
            self._recovery_error = (
                code
                if isinstance(code, str)
                and re.fullmatch(r"(?:QMT_|RECOVERY_HISTORY_)[A-Z0-9_]{1,56}", code)
                else self._recovery_stage + "_FAILED"
            )
            self._tighten("RECOVERY_FAILED")

    def recovery_error(self) -> str | None:
        """A fixed diagnostic code; exception messages and broker identifiers stay private."""
        self._owner_only()
        return self._recovery_error

    @property
    def _opening_hash(self) -> str | None:
        if self._opening_proof is not None:
            return digest(self._opening_proof)
        return self._opening_reference.payload_hash if self._opening_reference else None

    def risk_baseline_equity(self) -> str | None:
        self._owner_only()
        return (
            format(self._risk_baseline_equity, "f")
            if self._risk_baseline_equity is not None
            else None
        )

    def capture_day(self) -> DaySnapshot:
        """Freeze a fresh durable end-of-day observation, including blocked reports.

        This read-only internal port runs on the engine owner. Packaging/upload
        happens on the agent worker. Missing evidence is never an empty day and
        a BLOCKED recovery is exportable evidence, not permission to trade.
        """
        self._owner_only()
        self.poll()
        report, broker = self._report, self._broker_snapshot
        if (
            report is None
            or broker is None
            or self._scan is None
            or self._pending
            or any(
                (self._page, self._query, self._checkpoint, self._opening_read, self._identity_read)
            )
            or not self.permission.snapshot().halted
            or not self.critical_idle
            or not self.dispatcher.idle
            or not (
                self._state in ("REVIEW_REQUIRED", "RECOVERY_BLOCKED", "READY_PAUSED")
                or self._sealed
            )
            or (
                report.status == "CLEAR"
                and set(self._known) != {r.submission.client_order_id for r in self._records}
            )
            or report.event_version != self._events()
            or not 0 <= (self._now() - report.observed_at).total_seconds() <= 30
            or self._now().date() != self.trade_date
            or self._now().time() < time(15)
        ):
            raise ValueError("daily export requires fresh durable paused closing recovery")
        return DaySnapshot(
            entry_targets=self._entry_target_reader() if self._entry_target_reader else (),
            entry_plans=self.plans.plans.accepted()
            if self.plans.plans.tail_history is not None
            else (),
            entry_policies=self._entry_policy_reader() if self._entry_policy_reader else (),
            report=report,
            broker=broker,
            submissions=tuple(self._records),
            lots=self._portfolio.lots if report.status == "CLEAR" else (),
            plan=self.plans.plans.loaded(),
            opening_hash=self._opening_hash,
            journal_high_watermark=self._scan.prefix.high_watermark,
        )

    def seal_portfolio(self) -> Future[OpeningReference]:
        """After close, seal only a freshly reconciled, paused day; never enable trading.

        This is an owner-only offline composition port, not an unauthenticated
        IPC endpoint. The caller polls the returned Future outside the engine.
        Failed/late persistence leaves this boot permanently sealed and paused.
        """
        self._owner_only()
        self.poll()
        if (
            self._sealed
            or self._pending
            or not self.permission.snapshot().halted
            or not self._report_current()
            or not self.dispatcher.idle
            or not self.critical_idle
            or self._now().time() < time(15)
            or self._now().date() != self.trade_date
        ):
            raise ValueError("closing requires a fresh terminal paused recovery after 15:00")
        assert self._report is not None
        assert self._broker_snapshot is not None
        self._check_observation(self._broker_snapshot, self._order_state)
        assert self._scan is not None
        checkpoint = PortfolioCheckpoint(
            account_id=self.account_id,
            trade_date=self.trade_date,
            calendar_version=self._calendar.version,
            recovery_report_hash=self._report.payload_hash,
            submission_high_watermark=self._scan.prefix.high_watermark,
            submission_prefix_hash=self._scan.prefix.payload_hash,
            # Settled zero balances remain in historical J1 evidence; the new
            # opening carries only live holdings, bounding future lot projection.
            lots=tuple(lot for lot in self._portfolio.lots if lot.remaining),
        )
        body, reference = checkpoint.body(), checkpoint.reference()
        self._sealed = True
        self._tighten("DAY_SEALED")

        def save(store: CriticalStorePort) -> OpeningReference:
            write_checkpoint(store, closing_key(self.trade_date), body)
            return reference

        return self.journal.request(save, deadline=monotonic() + 0.25)

    def _report_current(self) -> bool:
        return bool(
            self._report
            and self._report.status == "CLEAR"
            and not self._checkpoint
            and not self._audit
            and self._inbox.empty()
            and self._report.event_version == self._events()
            and 0 <= (self._now() - self._report.observed_at).total_seconds() <= 30
        )

    @property
    def critical_idle(self) -> bool:
        """Owner-only readiness, distinct from broker order finality."""
        self._owner_only()
        return (
            self.journal.healthy
            and not self._audit
            and self._inbox.empty()
            and not self._observation
            and not self._observed
        )

    def _check_observation(
        self, snapshot: BrokerRecoverySnapshot, previous: OrderProjection
    ) -> CheckedObservation:
        scopes = self._execution_scopes()
        if not scopes:
            if not self._known and not self._opening:
                return check_empty_observation(
                    snapshot=snapshot,
                    previous=previous,
                    account_id=self.account_id,
                    trade_date=self.trade_date,
                    now=self._now(),
                    event_version=self._events(),
                    allow_unmanaged=self.limits.mode == "LIVE",
                )
            if not self._known and self._opening and self._opening_hash is not None:
                # A verified previous-day portfolio can be checked before today's
                # first selection. It supplies no ENTRY execution scope or grant.
                return check_observation(
                    snapshot=snapshot,
                    previous=previous,
                    submissions=(),
                    account_id=self.account_id,
                    trade_date=self.trade_date,
                    strategy_id=self._opening[0].strategy_id,
                    plan_hash=self._opening_hash,
                    calendar=self._calendar,
                    now=self._now(),
                    event_version=self._events(),
                    opening=self._opening,
                    opening_hash=self._opening_hash,
                    allow_unmanaged=self.limits.mode == "LIVE",
                )
            raise ValueError("plan ownership required for account projection")
        if len({scope.strategy_id for scope in scopes}) != 1:
            raise ValueError("entry and exit scopes require the same strategy owner")
        scope = scopes[0]
        if self.exit_context is not None or self.board_exit_contexts:
            opening = {lot.lot_id: lot for lot in self._opening}
            if any(
                context.opening_hash != self._opening_hash
                for context in self.board_exit_contexts.values()
            ):
                raise ValueError("board context opening proof differs")
            if any(
                item.lot_id not in opening
                or opening[item.lot_id].symbol != item.symbol
                or opening[item.lot_id].strategy_id != scope.strategy_id
                for context in (
                    *self.board_exit_contexts.values(),
                    *((self.exit_context,) if self.exit_context else ()),
                )
                for item in context.lots
            ):
                raise ValueError("exit context has no matching opening ownership")
        return check_observation(
            snapshot=snapshot,
            previous=previous,
            submissions=tuple(self._known.values()),
            account_id=self.account_id,
            trade_date=self.trade_date,
            strategy_id=scope.strategy_id,
            plan_hash=scope.payload_hash,
            context_type=scope.kind,
            calendar=self._calendar,
            now=self._now(),
            event_version=self._events(),
            opening=self._opening,
            opening_hash=self._opening_hash,
            accepted_scopes=frozenset((item.payload_hash, item.kind) for item in scopes),
            allow_unmanaged=self.limits.mode == "LIVE",
        )

    def refresh_account(self) -> None:
        """Start a bounded SDK query, never wait on it or raise trading permission."""
        self._owner_only()
        if (
            ((not self._grant or self.permission.snapshot().halted) and not self._watch_manual())
            or self._pending
            or any(
                (
                    self._page,
                    self._query,
                    self._checkpoint,
                    self._opening_read,
                    self._observation,
                    self._observed,
                )
            )
            or not self.dispatcher.idle
            or any(s.status in ("PERSISTING", "DISPATCHING") for s in self._coordinator.snapshot())
        ):
            raise ValueError("account observation is unavailable or busy")
        self._observation_epoch = self.permission.snapshot().epoch
        self._observation_deadline = monotonic() + 2
        self._observation = self.dispatcher.observe(self._capture)

    def _poll_observation(self) -> None:
        if self._observation is None and self._observed is None:
            return
        if (
            monotonic() >= self._observation_deadline
            or self._observation_epoch != self.permission.snapshot().epoch
        ):
            self._observation, self._observed = None, None
            self._tighten("ACCOUNT_OBSERVATION_EXPIRED")
            return
        try:
            if self._observation is not None and self._observation.done():
                snapshot = self._observation.result()
                self._observation = None
                checked = self._check_observation(snapshot, self._order_state)
                # Queries repair missing callbacks. Persist only new/changed
                # broker facts; periodic account snapshots stay in memory.
                old_orders = {o.client_order_id: o for o in self._order_state.orders()}
                old_trades = set(self._order_state.trades())
                for order in snapshot.orders:
                    self._save_order_identity(order)
                    if old_orders.get(order.client_order_id) != order:
                        record = export_order(
                            order,
                            account_id=self.account_id,
                            trade_date=self.trade_date,
                            observed_at=order.updated_at.value,
                        )
                        body = canonical_bytes(record.model_dump(mode="json"))
                        self._save_audit("broker_facts", "fact-" + digest(body)[7:], body)
                for trade in snapshot.trades:
                    if trade not in old_trades:
                        record_trade = export_trade(
                            trade,
                            account_id=self.account_id,
                            trade_date=self.trade_date,
                            observed_at=trade.traded_at.value,
                            origin="UNKNOWN",
                        )
                        body = canonical_bytes(record_trade.model_dump(mode="json"))
                        self._save_audit("broker_facts", "fact-" + digest(body)[7:], body)
                self._observed = checked
            if self._observed is not None and not self._audit:
                checked = self._observed
                self._observed = None
                # Recheck after asynchronous writes. HALT/event changes may
                # have happened while the repaired broker facts were saved.
                if (
                    not self.journal.healthy
                    or self._observation_epoch != self.permission.snapshot().epoch
                    or monotonic() >= self._observation_deadline
                ):
                    raise ValueError("observation persistence failed")
                self._check_observation(checked.snapshot, self._order_state)
                self._broker_snapshot = checked.snapshot
                self._order_state, self._portfolio = checked.orders, checked.portfolio
                self._orders = {o.client_order_id: o for o in checked.orders.orders()}
                for item in self._known.values():
                    origin = (
                        item.client_order_id
                        if item.action_type == "NEW"
                        else item.original_client_order_id
                    )
                    if origin in checked.terminal_ids:
                        self._book.resolve_terminal(item.client_order_id, reconciled=True)
                        self.permission.resolve_terminal(item.client_order_id, confirmed=True)
                self._observation_version += 1
                self._version += 1
                self._next_observation = monotonic() + 2
        except Exception:
            self._observation, self._observed = None, None
            self._tighten("ACCOUNT_OBSERVATION_FAILED")

    def control(self, request: LocalControlRequest) -> LocalControlResult:
        self._owner_only()
        self.poll()
        body_hash = digest(canonical_bytes(request.model_dump(mode="json")))
        old = self._requests.get(request.request_id)
        if old:
            if old[0] != body_hash:
                raise ValueError("control request ID content conflict")
            return old[1]
        if len(self._requests) >= 256:
            # HALT remains available even when a session's reply cache is full.
            if request.action in ("HALT", "PAUSE"):
                self._tighten("LOCAL_HALT")
            raise ValueError("control reply capacity exhausted")
        if request.engine_boot_id != self.boot_id:
            return self._result(request, "REJECTED", "WRONG_ENGINE_BOOT")
        if request.action in ("HALT", "PAUSE"):
            self._tighten("LOCAL_" + request.action)
            body = canonical_bytes(request.model_dump(mode="json"))
            self._save_audit("control_records", request.request_id, body)
            return self._result(request, "APPLIED", "PERMISSION_NARROWED_AUDIT_PENDING")
        if request.action == "SNAPSHOT":
            return self._result(request, "APPLIED", "SNAPSHOT_AVAILABLE")
        if (
            request.deadline <= self._now()
            or request.expected_state_version != self._version
            or self._pending is not None
        ):
            return self._result(request, "REJECTED", "STALE_OR_PENDING_CONTROL")
        if request.action == "REQUEST_RECOVERY":
            try:
                self.start_recovery()
            except ValueError:
                return self._result(request, "REJECTED", "RECOVERY_BUSY")
            return self._result(request, "APPLIED", "RECOVERY_STARTED")
        if request.action not in ("CONFIRM_RECOVERY", "ARM", "RESUME"):
            return self._result(request, "REJECTED", "ACTION_NOT_IMPLEMENTED")
        try:
            self.preview_control(request)
            actor = self.approvals.consume(request)
            body = canonical_bytes(
                {
                    "request": request.model_dump(mode="json"),
                    "actor": actor,
                    "permission_epoch": self._epoch,
                    "observed_at": self._now().isoformat(),
                }
            )
            deadline = monotonic() + min(0.25, (request.deadline - self._now()).total_seconds())
            future = self.journal.request(
                lambda store: store.append_evidence("control_records", request.request_id, body),
                deadline=deadline,
            )
            self._pending = _Control(request, self._epoch, self._events(), deadline, future)
            self._state = "CONTROL_PERSISTING"
            self._version += 1
            return self._result(request, "PENDING", "CONTROL_PERSISTING")
        except ValueError:
            return self._result(request, "REJECTED", "AUTHORIZATION_PREREQUISITE_FAILED")

    def preview_control(self, request: LocalControlRequest) -> None:
        """Read-only prerequisite check; neither authenticates nor creates permission.

        The local adapter uses this on the owner before showing an exact proposal.
        control() still checks everything again when a human confirms it.
        """
        self._owner_only()
        if (
            request.engine_boot_id != self.boot_id
            or request.expected_state_version != self._version
            or request.deadline <= self._now()
            or self._pending is not None
            or not self.journal.healthy
            or not self._report_current()
        ):
            raise ValueError("control preview prerequisites are not current")
        if request.action == "CONFIRM_RECOVERY":
            assert self._report
            if (
                request.artifact_hash != self._report.payload_hash
                or not self.permission.snapshot().halted
            ):
                raise ValueError("recovery preview requires a paused matching report")
        elif request.action in ("ARM", "RESUME") and request.grant:
            self._validate_grant(request.grant)
        else:
            raise ValueError("unsupported preview action")

    @property
    def next_session_generation(self) -> int:
        self._owner_only()
        return self._grant_generation + 1

    def control_result(self, request_id: str) -> LocalControlResult | None:
        """Query an existing receipt without replaying or minting a control request."""
        self._owner_only()
        value = self._requests.get(request_id)
        return value[1] if value else None

    def _validate_grant(self, grant: SessionGrant) -> None:
        limits = self.buy_limits if "BUY" in grant.directions else self.limits
        if (
            "BUY" in grant.directions
            and self.plans.plans.tail_history is not None
            and self.plans.plans.loaded() is None
        ):
            raise ValueError("no active tail phase is available")
        scopes = self._execution_scopes()
        if not scopes or not self._report or self._confirmed != self._report.payload_hash:
            raise ValueError("confirmed recovery and plan required")
        now = self._now()
        allowed_directions = {direction for scope in scopes for direction in scope.directions}
        relevant = tuple(
            scope for scope in scopes if set(scope.directions).intersection(grant.directions)
        )
        buy_scope = next((scope for scope in scopes if scope.kind == "ENTRY_PLAN"), None)
        if (
            self._sealed
            or grant.account_id != self.account_id
            or grant.engine_boot_id != self.boot_id
            or grant.mode != limits.mode
            or grant.recovery_report_hash != self._confirmed
            or grant.session_generation <= self._grant_generation
            or any(scope.trade_date != self.trade_date for scope in scopes)
            or not relevant
            or not grant.valid_from
            <= now
            < grant.valid_until
            <= min(scope.valid_until for scope in relevant)
            or not set(grant.directions) <= allowed_directions
            or grant.max_positions > limits.max_positions
            or Decimal(grant.max_order_value) > Decimal(limits.max_order_value)
            or Decimal(grant.max_buy_value)
            > min(
                Decimal(limits.max_buy_value),
                Decimal(limits.max_buy_value)
                if self.buy_settings is not None and self.local_tail_feed is not None
                else Decimal(buy_scope.max_buy_value)
                if buy_scope is not None
                else Decimal(0),
            )
            or (
                self.exit_context is not None
                and (
                    now < self.exit_context.valid_from
                    or Decimal(grant.max_order_value) > Decimal(self.exit_context.max_order_value)
                )
            )
            or not self.permission.snapshot().halted
        ):
            raise ValueError("grant exceeds installed ceiling or frozen session")

    def _poll_control(self) -> None:
        pending = self._pending
        if not pending:
            return
        reason = "CONTROL_STALE"
        valid = (
            monotonic() < pending.deadline
            and self.journal.healthy
            and pending.expected_epoch == self.permission.snapshot().epoch
            and pending.event_version == self._events()
            and self._report_current()
            and pending.request.deadline > self._now()
        )
        if valid and not pending.write.done():
            return
        self._pending = None
        try:
            if not valid or not pending.write.result():
                raise ValueError("late or duplicate control commit")
            if pending.request.action == "CONFIRM_RECOVERY":
                self._confirmed = pending.request.artifact_hash
                self._state = "READY_PAUSED"
            else:
                grant = pending.request.grant
                assert grant
                self._validate_grant(grant)
                if self.exit_context is None and not self.board_exit_contexts:
                    self.plans.mark_armed()
                self._epoch = self.permission.allow_after_durable_grant(pending.expected_epoch)
                self._grant, self._grant_generation = grant, grant.session_generation
                self._state = "ARMED"
            self._version += 1
            self._result(pending.request, "APPLIED", "DURABLE_CONTROL_APPLIED")
            return
        except Exception:
            if valid:
                reason = "CONTROL_PERSISTENCE_FAILED"
        self._tighten(reason)
        self._result(pending.request, "REJECTED", reason)

    def _result(
        self,
        request: LocalControlRequest,
        status: Literal["APPLIED", "REJECTED", "PENDING"],
        reason: str,
    ) -> LocalControlResult:
        permission = self.permission.snapshot()
        result = LocalControlResult(
            request_id=request.request_id,
            engine_boot_id=self.boot_id,
            status=status,
            state_version=self._version,
            permission_epoch=permission.epoch,
            reason_code=reason,
            observed_at=self._now(),
            in_flight_order_ids=permission.in_flight[:32],
        )
        self._requests[request.request_id] = (
            digest(canonical_bytes(request.model_dump(mode="json"))),
            result,
        )
        return result

    def _allows(self, submission: Submission) -> bool:
        if (
            submission.action_type == "CANCEL"
            and submission.client_order_id in self._cancel_permits
        ):
            return self._allows_operator_cancel(submission)
        if self.execution_reporter is not None and self.execution_reporter.failed:
            return False
        if (
            submission.action_type == "NEW"
            and submission.symbol in self.manual_sell_locks
            and submission.context_type != "MANUAL_EXIT"
        ):
            return False
        if submission.context_type in {"MANUAL_ENTRY", "MANUAL_EXIT"}:
            return self._allows_manual(submission)
        grant, loaded, snapshot = self._grant, self.plans.plans.loaded(), self._broker_snapshot
        if submission.side == "BUY":
            grant = self.current_buy_grant()
            if submission.action_type == "NEW" and (
                not self.buy_enabled or self.buy_settings_error
            ):
                return False
            loaded = next(
                (
                    p
                    for p in self.plans.plans.accepted()
                    if p.envelope.payload_hash == submission.plan_hash
                ),
                None,
            )
        if submission.side == "SELL":
            grant = self._board_exit_grants.get(submission.plan_hash, grant)
        scopes = self._execution_scopes()
        scope = next(
            (
                value
                for value in scopes
                if (
                    submission.side == "BUY"
                    and value.kind == "ENTRY_PLAN"
                    and value.payload_hash == submission.plan_hash
                )
                or (
                    submission.side == "SELL"
                    and value.kind == "EXIT_SESSION"
                    and value.payload_hash == submission.plan_hash
                )
            ),
            None,
        )
        if (
            scope is None
            and submission.side == "SELL"
            and self.exit_context is None
            and not self.board_exit_contexts
        ):
            scope = next((value for value in scopes if value.kind == "ENTRY_PLAN"), None)
        now = self._now()
        if not grant or not scope or not snapshot or self.limits.mode not in {"PAPER", "LIVE"}:
            return False
        permission = self.permission.snapshot()
        if (
            permission.halted
            or self._pending
            or self._audit
            or self._observation is not None
            or self._observed is not None
            or not self._inbox.empty()
            or not self.journal.healthy
            or submission.engine_boot_id != self.boot_id
            or submission.permission_epoch != permission.epoch
            or submission.account_id != self.account_id
            or submission.trade_date != self.trade_date
            or submission.plan_hash != scope.payload_hash
            or submission.context_type != scope.kind
            or submission.side not in grant.directions
            or not grant.valid_from <= now < grant.valid_until
            or now.date() != self.trade_date
            or (
                submission.action_type == "NEW"
                and submission.quantity
                > (
                    self.buy_limits.max_order_quantity
                    if submission.side == "BUY"
                    else self.limits.max_order_quantity
                )
            )
            or (
                submission.action_type == "NEW"
                and Decimal(submission.maximum_amount) > Decimal(grant.max_order_value)
            )
        ):
            return False
        if submission.action_type == "CANCEL":
            # A second cancel cannot race an unresolved prior cancellation.
            # Rechecking the same pending J1 excludes its own request ID.
            if any(
                s.action_type == "CANCEL"
                and s.original_client_order_id == submission.original_client_order_id
                and s.client_order_id != submission.client_order_id
                and self._cancel_blocks(s)
                for s in self._known.values()
            ):
                return False
            original = self._known.get(submission.original_client_order_id or "")
            broker_order = self._orders.get(submission.original_client_order_id or "")
            return bool(
                original
                and original.action_type == "NEW"
                and broker_order
                and not broker_order.is_terminal
                and broker_order.broker_order_id == submission.original_order_id
                and submission.target == original.target
                and submission.sell_binding == original.sell_binding
                and submission.entry_binding == original.entry_binding
                and submission.maximum_amount == original.maximum_amount
                and submission.plan_hash == original.plan_hash
                and (
                    str(broker_order.symbol),
                    broker_order.side.value,
                    broker_order.order_quantity.value,
                )
                == (original.symbol, original.side, original.quantity)
                and (
                    submission.symbol,
                    submission.side,
                    submission.quantity,
                    submission.limit_price,
                )
                == (original.symbol, original.side, original.quantity, original.limit_price)
                and now < scope.cancel_by
            )
        if (
            snapshot.event_version != self._events()
            or not snapshot.account.coherent
            or not 0 <= (now - snapshot.account.observed_at).total_seconds() <= 5
        ):
            return False
        if submission.side == "BUY" and not self.buy_board_allowed(submission.symbol):
            return False
        if submission.side == "SELL":
            exit_context = self.exit_context_for(submission.plan_hash)
            state_guard = self._board_exit_guards.get(submission.plan_hash, self._exit_state_guard)
            if exit_context is not None and (
                not exit_context.valid_from <= now < exit_context.stop_new_at
                or submission.quantity > exit_context.max_order_quantity
                or Decimal(submission.limit_price) * submission.quantity
                > Decimal(exit_context.max_order_value)
                or state_guard is None
                or not state_guard(submission)
            ):
                return False
            if exit_context is not None:
                binding = submission.sell_binding
                if binding is None or len(binding.allocations) != 1:
                    return False
                lot_id = binding.allocations[0].lot_id
                policy = next((p for p in exit_context.lots if p.lot_id == lot_id), None)
                lot = next((p for p in self._portfolio.lots if p.lot_id == lot_id), None)
                if (
                    policy is None
                    or lot is None
                    or (
                        submission.quantity % policy.sell_step != 0
                        and not (
                            policy.allow_odd_liquidation
                            and submission.target.target_remaining == 0
                            and submission.quantity == lot.remaining
                            and lot.reserved_quantity == 0
                        )
                    )
                ):
                    return False
            position = next(
                (p for p in snapshot.account.positions if p.symbol == submission.symbol), None
            )
            return bool(
                time(9, 30) <= now.time() < time(14, 55, 30)
                and position is not None
                and Decimal(submission.limit_price) * submission.quantity
                <= Decimal(grant.max_order_value)
                and allows_sell(
                    submission,
                    portfolio=self._portfolio,
                    strategy_id=scope.strategy_id,
                    opening_hash=self._opening_hash,
                    as_of=self.trade_date,
                    broker_sellable=position.sellable,
                    reflected_order_ids=frozenset(snapshot.account.observed_order_ids),
                    prior=tuple(self._known.values()),
                    historical_targets=tuple(self._scan.targets.values()) if self._scan else (),
                    rule_id=exit_context.rule_id if exit_context else EXIT_RULE,
                )
                and self._risk_guard(submission)
            )
        if loaded is None:
            return False
        if submission.action_type == "NEW" and self.manual_symbol_claimed(submission.symbol):
            return False
        if (self._entry_state_guard is not None and not self._entry_state_guard(submission)) or (
            submission.entry_binding is not None
            and submission.entry_binding.state_hash is not None
            and self._entry_state_guard is None
        ):
            return False
        if (
            not loaded.payload.execution_window.entry_not_before
            <= now
            < loaded.payload.execution_window.stop_new_buy_at
        ):
            return False
        allocation = next(
            (a for a in self.entry_allocations(loaded.payload) if a.symbol == submission.symbol),
            None,
        )
        if (
            not allocation
            or submission.quantity > allocation.max_qty
            or submission.symbol in self._portfolio.locked_symbols
        ):
            return False
        if any(
            item.action_type == "NEW"
            and item.side == "BUY"
            and item.symbol == submission.symbol
            and item.entry_binding != submission.entry_binding
            and self.buy_settings is None
            for item in self._known.values()
        ):
            # A later child cannot remove/change the first child's frozen target.
            return False
        if submission.entry_binding is not None and (
            submission.entry_binding.plan_id != loaded.payload.plan_id
            or submission.entry_binding.allocation_id != allocation.allocation_id
            or submission.entry_binding.target_quantity > allocation.max_qty
        ):
            return False
        others = [
            s
            for key, s in self._known.items()
            if key != submission.client_order_id
            and s.action_type == "NEW"
            and s.side == "BUY"
            and s.trade_date == self.trade_date
        ]
        amount = Decimal(submission.maximum_amount)
        exposures = tuple(
            e for e in self.entry_exposures() if e.client_order_id != submission.client_order_id
        )
        if (
            self.buy_limits.max_daily_symbols is not None
            and len({e.symbol for e in exposures if e.quantity > 0} | {submission.symbol})
            > self.buy_limits.max_daily_symbols
        ):
            return False
        if submission.entry_binding is not None and (
            sum(e.quantity for e in exposures if e.symbol == submission.symbol)
            + submission.quantity
            > submission.entry_binding.target_quantity
        ):
            return False
        if (
            sum((e.amount for e in exposures), Decimal(0)) + amount > Decimal(grant.max_buy_value)
            or sum(
                (e.amount for e in exposures if e.symbol == submission.symbol),
                Decimal(0),
            )
            + amount
            > min(
                Decimal(allocation.max_value),
                Decimal(self.buy_limits.max_symbol_buy_value)
                if self.buy_limits.max_symbol_buy_value is not None
                else Decimal(allocation.max_value),
            )
            or amount > self._position_buy_capacity(exclude_id=submission.client_order_id)
            or sum(e.quantity for e in exposures if e.symbol == submission.symbol)
            + submission.quantity
            > allocation.max_qty
        ):
            return False
        symbols = (
            {p.symbol for p in snapshot.account.positions if p.quantity}
            | {s.symbol for s in others}
            | {submission.symbol}
        )
        if self.buy_limits.max_daily_symbols is not None:
            symbols = (
                self._position_symbols()
                | {e.symbol for e in exposures if e.quantity > 0}
                | {submission.symbol}
            )
        return len(symbols) <= grant.max_positions and self._risk_guard(submission)

    def _position_buy_capacity(self, *, exclude_id: str | None = None) -> Decimal:
        """Fresh account market value plus broker and unobserved local BUY commitments.

        Filled shares already belong to account market value. Only remaining
        shares of observed orders are added; unobserved reservations retain their
        full amount until a coherent observation explains them. Manual orders
        count too. A candidate's own reservation is excluded on the J1 recheck.
        """
        pct, broker = self.buy_limits.max_total_position_pct, self._broker_snapshot
        if pct is None:
            return Decimal(0) if self.buy_limits.mode == "LIVE" else Decimal("Infinity")
        if (
            broker is None
            or not broker.stable
            or not broker.account.coherent
            or broker.event_version != self._events()
            or not 0 <= (self._now() - broker.account.observed_at).total_seconds() <= 5
            or broker.account.total_equity is None
            or broker.account.market_value is None
            or Decimal(broker.account.total_equity) <= 0
        ):
            return Decimal(0)
        orders = {o.client_order_id: o for o in broker.orders}
        reserved: set[str] = set()
        pending = Decimal(0)
        for item in self._book.snapshot():
            if item.side != "BUY" or item.client_order_id == exclude_id:
                continue
            reserved.add(item.client_order_id)
            order = orders.get(item.client_order_id)
            if (
                order is not None
                and not order.is_terminal
                and item.client_order_id in broker.account.observed_order_ids
            ):
                fee = max(
                    Decimal(0),
                    Decimal(item.maximum_amount) - Decimal(item.limit_price) * item.quantity,
                )
                pending += order.limit_price.amount * order.remaining_quantity.value + fee
            else:
                pending += Decimal(item.maximum_amount)
        pending += sum(
            (
                o.limit_price.amount * o.remaining_quantity.value
                for o in broker.orders
                if o.side.value == "BUY"
                and not o.is_terminal
                and o.client_order_id not in reserved
                and o.client_order_id != exclude_id
            ),
            Decimal(0),
        )
        return max(
            Decimal(0),
            Decimal(broker.account.total_equity) * Decimal(pct) / 100
            - Decimal(broker.account.market_value)
            - pending,
        )

    def buy_order_exceeds_limits(self, item: Submission) -> bool:
        if self.buy_settings is None:
            return False
        limits = self.buy_limits
        exposures = self.entry_exposures()
        return bool(
            not self.buy_board_allowed(item.symbol)
            or item.quantity > limits.max_order_quantity
            or Decimal(item.maximum_amount) > Decimal(limits.max_order_value)
            or sum((e.amount for e in exposures), Decimal(0)) > Decimal(limits.max_buy_value)
            or sum((e.amount for e in exposures if e.symbol == item.symbol), Decimal(0))
            > Decimal(limits.max_symbol_buy_value or limits.max_buy_value)
            or len({e.symbol for e in exposures if e.quantity})
            > (limits.max_daily_symbols or limits.max_positions)
            or self._position_buy_capacity() <= 0
        )

    def entry_exposures(self) -> tuple[BuyExposure, ...]:
        """Use durable reconciled observations; callback terminal status alone releases nothing."""
        self._owner_only()
        snapshot = self._broker_snapshot
        return buy_exposures(
            tuple(self._known.values()),
            snapshot.orders if snapshot else (),
            snapshot.trades if snapshot else (),
            frozenset(s.client_order_id for s in self._book.snapshot()),
            state_guard_installed=self._entry_state_guard is not None,
        )

    def _execution_scopes(self) -> tuple[ExecutionScope, ...]:
        scopes: list[ExecutionScope] = []
        for plan in self.plans.plans.accepted():
            scopes.extend(execution_scopes(plan, None))
        scopes.extend(execution_scopes(None, self.exit_context))
        for context in self.board_exit_contexts.values():
            scopes.extend(execution_scopes(None, context))
        manual = {**self._known, **{k: v[0] for k, v in self._manual_permits.items()}}
        for item in manual.values():
            binding = item.manual_binding or item.manual_sell_binding
            if binding is not None:
                scopes.append(
                    ExecutionScope(
                        "MANUAL_EXIT" if item.manual_sell_binding else "MANUAL_ENTRY",
                        item.plan_hash,
                        binding.strategy_id,
                        item.trade_date,
                        datetime.combine(item.trade_date, time(15), CN),
                        datetime.combine(item.trade_date, time(15), CN),
                        ("SELL",) if item.manual_sell_binding else ("BUY",),
                        self.limits.max_buy_value,
                    )
                )
        return tuple(scopes)

    def manual_symbol_claimed(self, symbol: str) -> bool:
        # A canceled manual purchase must not silently hand the stock back to
        # an automatic owner. A fresh manual confirmation can retry zero fills.
        return any(
            s.symbol == symbol and s.action_type == "NEW" and s.manual_binding is not None
            for s in self._known.values()
        )

    def _watch_manual(self) -> bool:
        return self._state in {"REVIEW_REQUIRED", "READY_PAUSED", "ARMED", "EXECUTING"} and (
            self._manual_watching_until > monotonic()
            or any(
                s.manual_binding is not None or s.manual_sell_binding is not None
                for s in self._known.values()
            )
        )

    def check_manual_ready(self, side: Literal["BUY", "SELL"] = "BUY") -> None:
        """Local confirmation may authorize one purchase, never release a safety halt."""
        self._owner_only()
        self._notice_permission()
        if (
            self.limits.mode not in {"LIVE", "PAPER"}
            or self.market_data_failure is not None
            or self._state not in {"REVIEW_REQUIRED", "READY_PAUSED", "ARMED", "EXECUTING"}
            or self._report is None
            or self._report.status != "CLEAR"
            or self._sealed
            or self._pending
            or not self.journal.healthy
            or any(
                (self._page, self._query, self._checkpoint, self._opening_read, self._identity_read)
            )
            or self._now().date() != self.trade_date
            or self.trade_date not in self._calendar.sessions
        ):
            raise ValueError("MANUAL_" + side + "_NOT_READY")
        if side == "BUY":
            self._calendar.next_session(self.trade_date)

    def stage_manual_observation(
        self,
        snapshot: BrokerRecoverySnapshot,
        side: Literal["BUY", "SELL"] = "BUY",
    ) -> None:
        self.check_manual_ready(side)
        if self._observation is not None or self._observed is not None:
            raise ValueError("ACCOUNT_QUERY_BUSY")
        self._check_observation(snapshot, self._order_state)
        self._manual_watching_until = monotonic() + 40
        self._observation_epoch = self.permission.snapshot().epoch
        self._observation_deadline = monotonic() + 2
        self._observation = Future()
        self._observation.set_result(snapshot)
        self._poll_observation()

    def authorize_manual(self, item: Submission, *, valid_until: float) -> None:
        self.check_manual_ready(item.side)
        if (
            item.manual_binding is None and item.manual_sell_binding is None
        ) or item.client_order_id in self._known:
            raise ValueError("MANUAL_BUY_ALREADY_SUBMITTED")
        self._manual_permits = {k: v for k, v in self._manual_permits.items() if v[1] > monotonic()}
        self._manual_permits[item.client_order_id] = (item, valid_until)
        self.permission.allow_once(item.client_order_id, item.permission_epoch, valid_until)

    def _allows_manual(self, item: Submission) -> bool:
        now, broker = self._now(), self._broker_snapshot
        binding = item.manual_binding or item.manual_sell_binding
        permit = self._manual_permits.get(item.client_order_id)
        try:
            self.check_manual_ready(item.side)
        except ValueError:
            return False
        if (
            binding is None
            or permit is None
            or permit[0] != item
            or permit[1] <= monotonic()
            or not self.permission.allows(item.client_order_id, item.permission_epoch)
            or item.engine_boot_id != self.boot_id
            or item.account_id != self.account_id
            or item.trade_date != self.trade_date
            or broker is None
            or self._audit
            or self._observation is not None
            or self._observed is not None
            or not self._inbox.empty()
            or not broker.stable
            or not broker.account.coherent
            or broker.event_version != self._events()
            or not 0 <= (now - broker.account.observed_at).total_seconds() <= 5
            or (
                item.action_type == "NEW"
                and not 0 <= (now - binding.confirmed_at).total_seconds() < 30
            )
        ):
            return False
        if item.action_type == "CANCEL":
            original = self._known.get(item.original_client_order_id or "")
            order = self._orders.get(item.original_client_order_id or "")
            return bool(
                original
                and original.manual_binding == binding
                and order
                and not order.is_terminal
                and order.broker_order_id == item.original_order_id
                and now.time() < time(15)
                and not any(
                    s.action_type == "CANCEL"
                    and s.original_client_order_id == item.original_client_order_id
                    and s.client_order_id != item.client_order_id
                    and self._cancel_blocks(s)
                    for s in self._known.values()
                )
            )
        if item.manual_sell_binding is not None:
            return self._allows_manual_sell(item)
        if self.buy_settings_error is not None:
            return False
        exposures = tuple(
            e for e in self.entry_exposures() if e.client_order_id != item.client_order_id
        )

        amount = Decimal(item.maximum_amount)
        symbols = {e.symbol for e in exposures if e.quantity > 0} | {item.symbol}
        return bool(
            (time(9, 30) <= now.time() < time(11, 30) or time(13) <= now.time() < time(14, 55))
            and stock_board(item.symbol) in self.buy_limits.allowed_boards
            and item.symbol not in self._portfolio.locked_symbols
            and not any(e.symbol == item.symbol and e.quantity > 0 for e in exposures)
            and item.quantity <= self.buy_limits.max_order_quantity
            and amount <= Decimal(self.buy_limits.max_order_value)
            and amount + sum((e.amount for e in exposures), Decimal(0))
            <= Decimal(self.buy_limits.max_buy_value)
            and amount + sum((e.amount for e in exposures if e.symbol == item.symbol), Decimal(0))
            <= Decimal(self.buy_limits.max_symbol_buy_value or self.buy_limits.max_buy_value)
            and amount <= self._position_buy_capacity(exclude_id=item.client_order_id)
            and len(symbols) <= (self.buy_limits.max_daily_symbols or self.buy_limits.max_positions)
            and len(self._position_symbols() | symbols) <= self.buy_limits.max_positions
            and self._risk_guard(item)
            and (
                self.buy_limits.mode != "LIVE"
                or (
                    self._risk_baseline_equity is not None
                    and broker.account.total_equity is not None
                    and Decimal(self.buy_limits.max_daily_loss) > 0
                    and self._risk_baseline_equity - Decimal(broker.account.total_equity)
                    < Decimal(self.buy_limits.max_daily_loss)
                )
            )
        )

    def _allows_manual_sell(self, item: Submission) -> bool:
        binding, broker, now = item.manual_sell_binding, self._broker_snapshot, self._now()
        if (
            binding is None
            or broker is None
            or item.sell_binding is None
            or self._opening_hash is None
        ):
            return False
        if not (time(9, 30) <= now.time() < time(11, 30) or time(13) <= now.time() < time(14, 55)):
            return False
        if (
            item.quantity > self.limits.max_order_quantity
            or len(item.sell_binding.allocations) != 1
        ):
            return False
        lot = next(
            (
                owned_lot
                for owned_lot in self._portfolio.lots
                if owned_lot.lot_id == item.sell_binding.allocations[0].lot_id
            ),
            None,
        )
        if lot is None or (
            item.quantity % binding.quantity_step != 0
            and not (
                item.quantity == lot.remaining
                and lot.reserved_quantity == 0
                and item.target.target_remaining == 0
            )
        ):
            return False
        views = {v.client_order_id: v.phase for v in self._order_state.views()}
        if any(
            old.action_type == "NEW"
            and old.symbol == item.symbol
            and old.client_order_id != item.client_order_id
            and views.get(old.client_order_id) != "TERMINAL_CONFIRMED"
            and (
                old.manual_sell_binding is None
                or old.manual_sell_binding.operation_id != binding.operation_id
            )
            for old in self._known.values()
        ):
            return False
        return allows_sell(
            item,
            portfolio=self._portfolio,
            strategy_id=binding.strategy_id,
            opening_hash=self._opening_hash,
            as_of=self.trade_date,
            broker_sellable=next(
                (p.sellable for p in broker.account.positions if p.symbol == item.symbol), 0
            ),
            reflected_order_ids=frozenset(o.client_order_id for o in broker.orders),
            prior=tuple(self._known.values()),
            historical_targets=tuple(self._scan.targets.values()) if self._scan else (),
            rule_id="manual-sell-v1",
        )

    def entry_symbol_claimed(self, symbol: str, plan_hash: str) -> bool | None:
        self._owner_only()
        if self.manual_symbol_claimed(symbol):
            return True
        if self.plans.plans.tail_history is None or self._entry_target_reader is None:
            return False
        try:
            return any(
                t.symbol == symbol and t.target_quantity > 0 and t.plan_hash != plan_hash
                for t in self._entry_target_reader()
            )
        except ValueError:
            # Another phase is still restoring or durably reserving a target.
            return None

    def _position_symbols(self) -> set[str]:
        """Tail holding slots count owned lots; account cash remains broker-wide."""
        if self.plans.plans.tail_history is not None:
            strategies = {scope.strategy_id for scope in self._execution_scopes()}
            return {
                lot.symbol
                for lot in self._portfolio.lots
                if lot.remaining and lot.strategy_id in strategies
            }
        snapshot = self._broker_snapshot
        return {p.symbol for p in snapshot.account.positions if p.quantity} if snapshot else set()

    def buy_board_allowed(self, symbol: str) -> bool:
        return (
            self.plans.plans.tail_history is None
            or stock_board(symbol) in self.buy_limits.allowed_boards
        )

    def has_buy_session(self) -> bool:
        """A SELL-only session must not freeze zero-budget entry targets."""
        self._owner_only()
        grant = self.current_buy_grant()
        return bool(
            self.buy_enabled
            and self.buy_settings_error is None
            and grant
            and "BUY" in grant.directions
            and grant.valid_from <= self._now() < grant.valid_until
        )

    def entry_resources(
        self, symbol: str, plan_hash: str | None = None
    ) -> tuple[Decimal, Decimal, int]:
        """Current cash/amount ceiling and remaining symbol slots, without granting authority."""
        self._owner_only()
        if not self.buy_board_allowed(symbol):
            return Decimal(0), Decimal(0), 0
        loaded, grant, broker = (
            self.plans.plans.loaded(),
            self.current_buy_grant(),
            self._broker_snapshot,
        )
        if plan_hash is not None:
            loaded = next(
                (p for p in self.plans.plans.accepted() if p.envelope.payload_hash == plan_hash),
                None,
            )
        if loaded is None or grant is None or broker is None:
            return Decimal(0), Decimal(0), 0
        allocation = next(
            (a for a in self.entry_allocations(loaded.payload) if a.symbol == symbol), None
        )
        if allocation is None:
            return Decimal(0), Decimal(0), 0
        exposures = self.entry_exposures()
        symbols = self._position_symbols() | {
            e.symbol
            for e in exposures
            if self.buy_limits.max_daily_symbols is None or e.quantity > 0
        }
        cash = min(
            self._book.available_cash(broker.account.observation()).amount,
            self._position_buy_capacity(),
            Decimal(grant.max_buy_value) - sum((e.amount for e in exposures), Decimal(0)),
            Decimal(allocation.max_value)
            - sum((e.amount for e in exposures if e.symbol == symbol), Decimal(0)),
            (
                Decimal(self.buy_limits.max_symbol_buy_value)
                - sum((e.amount for e in exposures if e.symbol == symbol), Decimal(0))
                if self.buy_limits.max_symbol_buy_value is not None
                else Decimal("Infinity")
            ),
        )
        slots = grant.max_positions - len(symbols - {symbol})
        if self.buy_limits.max_daily_symbols is not None:
            daily_symbols = {e.symbol for e in exposures if e.quantity > 0}
            slots = min(slots, self.buy_limits.max_daily_symbols - len(daily_symbols - {symbol}))
        return max(Decimal(0), cash), Decimal(grant.max_order_value), max(0, slots)

    def exit_inputs(self) -> tuple[PortfolioProjection, AccountFact | None, tuple[Submission, ...]]:
        """Immutable same-owner inputs for the D1 strategy driver; never grant permission."""
        self._owner_only()
        return (
            self._portfolio,
            self._broker_snapshot.account if self._broker_snapshot else None,
            tuple(self._known.values()),
        )

    def disposable_market_symbols(self) -> frozenset[str]:
        """Positive full-sale proof, never infer a sale from an empty query alone."""
        self._owner_only()
        broker = self._broker_snapshot
        if (
            broker is None
            or self._report is None
            or self._report.status != "CLEAR"
            or not broker.stable
            or not broker.account.coherent
            or not 0 <= (self._now() - broker.account.observed_at).total_seconds() <= 5
            or broker.event_version != self._events()
            or self._observation is not None
            or self._observed is not None
            or self._audit
            or self._page is not None
            or self._query is not None
            or self._checkpoint is not None
            or self._opening_read is not None
            or self._portfolio.unowned_trade_ids
            or any(
                s.status in {"PERSISTING", "DISPATCHING", "UNCERTAIN", "PERSISTENCE_UNCERTAIN"}
                for s in self._coordinator.snapshot()
            )
        ):
            return frozenset()
        lots = self._portfolio.lots
        sold = {
            lot.symbol
            for lot in lots
            if lot.bought_quantity > 0 and lot.sold_quantity == lot.bought_quantity
        }
        protected = set(self._portfolio.locked_symbols)
        protected.update(lot.symbol for lot in lots if lot.remaining or lot.reserved_quantity)
        protected.update(p.symbol for p in broker.account.positions if p.quantity)
        protected.update(s for s, q in self._portfolio.unmanaged_positions if q)
        protected.update(r.symbol for r in self._portfolio.sell_reservations)
        protected.update(str(o.symbol) for o in broker.orders if not o.is_terminal)
        views = {view.client_order_id: view for view in self._order_state.views()}
        for item in self._known.values():
            view = views.get(item.client_order_id)
            if item.action_type == "NEW" and (view is None or view.phase != "TERMINAL_CONFIRMED"):
                protected.add(item.symbol)
        for plan in self.plans.plans.accepted():
            if self._now() < plan.payload.execution_window.cancel_by:
                protected.update(a.symbol for a in plan.payload.allocations)
        return frozenset(sold - protected)

    def submit(self, submission: Submission, *, valid_until: float) -> None:
        self._owner_only()
        self.poll()
        submission = Submission.model_validate_json(submission.model_dump_json())
        if submission.client_order_id in self._known:
            raise ValueError("recovered or attempted order ID cannot be replayed")
        try:
            allowed = self._allows(submission)
        except Exception:
            self._tighten("SUBMISSION_GUARD_FAILED")
            raise
        if not allowed:
            raise ValueError("engine submission gate rejected")
        if submission.action_type == "NEW":
            assert self._broker_snapshot
            self._book.reserve(submission, self._broker_snapshot.account.observation())
        self._known[submission.client_order_id] = submission
        try:
            if self.execution_reporter is not None:
                self.execution_reporter.submission(submission, "PERSISTING", self._now())
            self._order_state.register(submission)
            self._coordinator.begin(submission, valid_until=valid_until)
        except Exception:
            self._tighten("SUBMISSION_BEGIN_FAILED")
            if self.execution_reporter is not None and not self.execution_reporter.failed:
                self.execution_reporter.submission(submission, "BEGIN_FAILED", self._now())
            raise
        self._version += 1
        if submission.manual_binding is None and submission.manual_sell_binding is None:
            self._state = "EXECUTING"

    def poll(self) -> EngineSnapshot:
        self._owner_only()
        now = self._now()
        if now < self._last_now or now.date() != self.trade_date:
            self._tighten("CLOCK_OR_TRADING_DAY_CHANGED")
        self._last_now = now
        self._notice_permission()
        self._drain_events()
        if not self.journal.healthy and not self.permission.snapshot().halted:
            self._tighten("CRITICAL_JOURNAL_FAILED")
        if self._grant and now >= self._grant.valid_until:
            self._tighten("SESSION_GRANT_EXPIRED")
        if (
            self.limits.mode == "LIVE"
            and self._grant is not None
            and self._broker_snapshot is not None
            and self._broker_snapshot.account.total_equity is not None
            and self._risk_baseline_equity is not None
            and Decimal(self.buy_limits.max_daily_loss) > 0
            and self._risk_baseline_equity - Decimal(self._broker_snapshot.account.total_equity)
            >= Decimal(self.buy_limits.max_daily_loss)
        ):
            self.buy_settings_error = "MAX_DAILY_LOSS_REACHED"
        elif self.buy_settings_error == "MAX_DAILY_LOSS_REACHED":
            self.buy_settings_error = None
        remaining = []
        for future, deadline in self._audit:
            if not future.done():
                remaining.append((future, deadline))
                if monotonic() >= deadline:
                    self._tighten("CRITICAL_FACT_TIMEOUT")
            else:
                try:
                    future.result()
                except Exception:
                    self._tighten("CONTROL_AUDIT_FAILED")
        self._audit = remaining
        self._poll_recovery()
        self._poll_control()
        self._coordinator.poll()
        self._poll_cancel_failures()
        self._notice_permission()
        self._poll_observation()
        if self.execution_reporter is not None:
            try:
                statuses = {s.client_order_id: s.status for s in self._coordinator.snapshot()}
                for key, item in self._known.items():
                    self.execution_reporter.submission(item, statuses.get(key, "RECOVERED"), now)
                if monotonic() >= self._next_execution_capture:
                    feed = self.local_tail_feed
                    if feed is not None and feed.snapshot is not None:
                        self.execution_reporter.tail_candidates(
                            feed.snapshot,
                            feed.settings.buy.max_symbol_buy_value,
                            now,
                            filtered_plans=self.plans.plans.accepted(),
                        )
                    else:
                        self.execution_reporter.candidates(self.plans.plans.accepted(), {}, now)
                    self.execution_reporter.query_facts(
                        self._order_state.orders(), self._order_state.trades(), self._known
                    )
                    self._next_execution_capture = monotonic() + 0.5
            except Exception:
                self._tighten("EXECUTION_REPORTING_FAILED")
        if (
            ((self._grant and not self.permission.snapshot().halted) or self._watch_manual())
            and self._observation is None
            and self._observed is None
            and not self._pending
            and not any(
                key not in self._known and deadline > monotonic()
                for key, (_, deadline) in self._manual_permits.items()
            )
            and self.dispatcher.idle
            and not any(
                s.status in ("PERSISTING", "DISPATCHING") for s in self._coordinator.snapshot()
            )
            and self._broker_snapshot
            and (
                monotonic() >= self._next_observation
                or self._broker_snapshot.event_version != self._events()
            )
        ):
            self.refresh_account()
        return self.snapshot()

    def snapshot(self) -> EngineSnapshot:
        self._owner_only()
        p = self.permission.snapshot()
        return EngineSnapshot(
            self._state,
            self._version,
            p.epoch,
            p.halted,
            p.in_flight,
            self._report,
            self._grant.grant_id if self._grant else None,
            self._coordinator.snapshot(),
            self._order_state.views(),
            self._portfolio.lots,
            self._observation_version,
            self._observation is not None or self._observed is not None,
            self._broker_snapshot.account.observed_at if self._broker_snapshot else None,
            self._scan.prefix.count if self._scan else 0,
            len(self._records),
        )

    def broker_account_summary(self) -> dict[str, JsonValue] | None:
        """Expose bounded broker facts to the local status publisher, never values or IDs."""
        self._owner_only()
        observed = self._broker_snapshot
        if observed is None:
            return None
        return {
            "observed_at": observed.account.observed_at.isoformat(),
            "position_count": len(observed.account.positions),
            "order_count": len(observed.orders),
            "trade_count": len(observed.trades),
            "coherent_for_recovery": observed.account.coherent,
        }

    def _allows_operator_cancel(self, item: Submission) -> bool:
        permit = self._cancel_permits.get(item.client_order_id)
        if permit is None:
            return False
        expected, deadline, observed = permit
        if (
            item != expected
            or item.action_type != "CANCEL"
            or deadline <= monotonic()
            or item.engine_boot_id != self.boot_id
            or item.account_id != self.account_id
            or item.trade_date != self.trade_date
            or self._now().date() != self.trade_date
            or self._now().time() >= time(15)
            or self.limits.mode not in {"LIVE", "PAPER"}
            or self._state
            not in {
                "HALTED",
                "REVIEW_REQUIRED",
                "READY_PAUSED",
                "ARMED",
                "EXECUTING",
                "RECOVERY_BLOCKED",
            }
            or self._report is None
            or not self.journal.healthy
            or self._pending
            or self._audit
            or any(
                (
                    self._page,
                    self._query,
                    self._checkpoint,
                    self._opening_read,
                    self._observation,
                    self._observed,
                )
            )
            or not self._inbox.empty()
            or not self.permission.allows(item.client_order_id, item.permission_epoch)
        ):
            return False
        self._check_observation(observed, self._order_state)
        original = self._known.get(item.original_client_order_id or "")
        order = next(
            (o for o in observed.orders if o.client_order_id == item.original_client_order_id), None
        )
        return bool(
            original
            and original.action_type == "NEW"
            and order
            and order.remaining_quantity.value > 0
            and order.status in {BrokerOrderStatus.ACKED, BrokerOrderStatus.PARTIALLY_FILLED}
            and order.broker_order_id == item.original_order_id
            and item.plan_hash == original.plan_hash
            and item.context_type == original.context_type
            and (
                item.symbol,
                item.side,
                item.quantity,
                item.limit_price,
                item.target,
                item.entry_binding,
                item.sell_binding,
                item.manual_binding,
                item.maximum_amount,
            )
            == (
                original.symbol,
                original.side,
                original.quantity,
                original.limit_price,
                original.target,
                original.entry_binding,
                original.sell_binding,
                original.manual_binding,
                original.maximum_amount,
            )
            and not any(
                s.action_type == "CANCEL"
                and self._cancel_blocks(s)
                and s.original_client_order_id == original.client_order_id
                and s.client_order_id != item.client_order_id
                for s in self._known.values()
            )
        )

    def request_cancel_snapshot(self, client_order_id: str) -> Future[BrokerRecoverySnapshot]:
        self._owner_only()
        original = self._known.get(client_order_id)
        if original is None or original.action_type != "NEW":
            raise ValueError("ORDER_NOT_CANCELABLE")
        if not self.dispatcher.idle or not self.critical_idle:
            raise ValueError("ACCOUNT_QUERY_BUSY")
        return self.dispatcher.observe(self._capture)

    def operator_cancel_order(
        self, client_order_id: str, observation: BrokerRecoverySnapshot | None = None
    ) -> dict[str, JsonValue]:
        """Persist and dispatch one cancel for a system-owned active order."""
        self._owner_only()
        original = self._known.get(client_order_id)
        observed = observation if observation is not None else self._broker_snapshot
        order = (
            next((o for o in observed.orders if o.client_order_id == client_order_id), None)
            if observed
            else None
        )
        if (
            original is None
            or original.action_type != "NEW"
            or order is None
            or order.broker_order_id is None
            or order.is_terminal
            or self._cancel_state(order)[0] not in {"NONE", "FAILED"}
        ):
            raise ValueError("ORDER_NOT_CANCELABLE")
        # A recovery blocked by an active order has not installed its broker
        # projection, but its original immutable J1 intent is still known.
        self._order_state.register(original)
        cancel_id = "ord_" + uuid4().hex[:20]
        cancel = original.model_copy(
            update={
                "client_order_id": cancel_id,
                "action_type": "CANCEL",
                "original_order_id": order.broker_order_id,
                "original_client_order_id": client_order_id,
                "remark_token": uuid4().hex[:20],
                "engine_boot_id": self.boot_id,
                "permission_epoch": self.permission.snapshot().epoch,
            }
        )
        assert observed is not None
        deadline = monotonic() + 2
        self._cancel_permits = {k: v for k, v in self._cancel_permits.items() if v[1] > monotonic()}
        if len(self._cancel_permits) >= 32:
            raise ValueError("CONSOLE_BUSY")
        self._cancel_permits[cancel_id] = (cancel, deadline, observed)
        self.permission.allow_once(cancel_id, cancel.permission_epoch, deadline)
        try:
            self.submit(cancel, valid_until=deadline)
        except ValueError:
            if cancel_id not in self._known:
                self._cancel_permits.pop(cancel_id, None)
                raise ValueError("ORDER_CANCEL_NOT_READY") from None
            raise ValueError("ORDER_CANCEL_RESULT_UNKNOWN") from None
        return {
            "status": "REQUESTED",
            "cancel_request_id": cancel_id,
            "original_client_order_id": client_order_id,
        }

    def request_operator_snapshot(self) -> Future[BrokerRecoverySnapshot]:
        """Read-only SDK query for the page; never changes recovery or authority."""
        self._owner_only()
        if not self.dispatcher.idle:
            raise ValueError("ACCOUNT_QUERY_BUSY")
        plan_symbols = tuple(
            a.symbol for plan in self.plans.plans.accepted() for a in plan.payload.allocations
        )

        def capture_display(broker: BrokerGateway) -> BrokerRecoverySnapshot:
            snapshot = self._capture(broker)
            reader = getattr(broker, "read_display_names", None)
            if not callable(reader):
                return snapshot
            symbols = tuple(
                dict.fromkeys(
                    (
                        *plan_symbols,
                        *(p.symbol for p in snapshot.account.positions),
                        *(str(o.symbol) for o in snapshot.orders),
                        *(str(t.symbol) for t in snapshot.trades),
                    )
                )
            )
            names = reader(symbols)
            return replace(snapshot, display_names=tuple(names.items()))

        return self.dispatcher.observe(capture_display)

    def operator_trade_details(
        self, observation: BrokerRecoverySnapshot | None = None
    ) -> dict[str, JsonValue]:
        """Bounded broker facts for an authenticated local operator view.

        An absent observation stays absent; local projections are never presented
        as a successful broker query. This method runs only on the engine owner.
        """
        self._owner_only()
        observed = observation if observation is not None else self._broker_snapshot
        if observed is None:
            return {"available": False, "positions": [], "orders": [], "trades": []}
        account = observed.account
        if observation is not None and (
            account.account_id != self.account_id
            or not 0 <= (self._now() - account.observed_at).total_seconds() <= 5
        ):
            raise ValueError("ACCOUNT_QUERY_FAILED")
        statuses = {s.client_order_id: s.status for s in self._coordinator.snapshot()}
        buys = [s for s in self._known.values() if s.action_type == "NEW" and s.side == "BUY"]
        cancel_states = {o.client_order_id: self._cancel_state(o) for o in observed.orders[:100]}
        details: dict[str, JsonValue] = {
            "available": True,
            "trade_date": self.trade_date.isoformat(),
            "observed_at": account.observed_at.isoformat(),
            "cash": account.cash,
            "cash_frozen": account.cash_frozen,
            "market_value": account.market_value,
            "total_equity": account.total_equity,
            "coherent": account.coherent,
            "names": dict(observed.display_names),
            "submissions": [
                {
                    "client_order_id": item.client_order_id,
                    "symbol": item.symbol,
                    "source": "MANUAL" if item.manual_binding is not None else "AUTO",
                    "trade_date": item.trade_date.isoformat(),
                    "quantity": item.quantity,
                    "limit_price": item.limit_price,
                    "name": item.manual_binding.name if item.manual_binding else None,
                    "submitted_at": item.manual_binding.confirmed_at.isoformat()
                    if item.manual_binding
                    else None,
                    "status": statuses.get(item.client_order_id, "RECOVERED"),
                }
                for item in buys[:100]
            ],
            "managed_lots": [
                {
                    "lot_id": lot.lot_id,
                    "symbol": lot.symbol,
                    "quantity": lot.remaining,
                    "cost_price": lot.cost_price,
                    "acquired_on": lot.trade_date_acquired.isoformat(),
                    "eligible_sell_date": lot.eligible_sell_date.isoformat(),
                    "sell_state": "RULES_NOT_CONFIGURED",
                }
                for lot in self._portfolio.lots[:100]
                if lot.remaining
            ],
            "positions": [
                {
                    "symbol": position.symbol,
                    "quantity": position.quantity,
                    "sellable": position.sellable,
                    "frozen": position.frozen,
                    "market_value": position.market_value,
                }
                for position in account.positions[:100]
            ],
            "orders": [
                {
                    "client_order_id": order.client_order_id,
                    "symbol": str(order.symbol),
                    "side": order.side.value,
                    "status": order.status.value,
                    "quantity": order.order_quantity.value,
                    "filled": order.filled_quantity.value,
                    "remaining": order.remaining_quantity.value,
                    "limit_price": format(order.limit_price.amount, "f"),
                    "reason": order.status_reason,
                    "owned_by_system": order.client_order_id in self._known,
                    "cancel_state": cancel_states[order.client_order_id][0],
                    "cancel_reason": cancel_states[order.client_order_id][1],
                    "cancel_requested": order.client_order_id in self._known
                    and not order.is_terminal
                    and cancel_states[order.client_order_id][0] in {"PENDING", "UNKNOWN"},
                    "cancelable": order.client_order_id in self._known
                    and order.broker_order_id is not None
                    and not order.is_terminal
                    and order.remaining_quantity.value > 0
                    and order.status
                    in {BrokerOrderStatus.ACKED, BrokerOrderStatus.PARTIALLY_FILLED}
                    and cancel_states[order.client_order_id][0] in {"NONE", "FAILED"},
                }
                for order in observed.orders[:100]
            ],
            "trades": [
                {
                    "trade_id": trade.trade_id,
                    "client_order_id": trade.client_order_id,
                    "owned_by_system": trade.client_order_id in self._known,
                    "symbol": str(trade.symbol),
                    "side": trade.side.value,
                    "quantity": trade.quantity.value,
                    "price": format(trade.price.amount, "f"),
                    "amount": format(trade.notional.amount, "f"),
                    "traded_at": trade.traded_at.value.isoformat(),
                }
                for trade in observed.trades[:100]
            ],
            "truncated": any(
                len(rows) > 100
                for rows in (
                    account.positions,
                    observed.orders,
                    observed.trades,
                    buys,
                    self._portfolio.lots,
                )
            ),
        }
        from auto_trading_system.runtime.manual_sell_view import sell_positions

        try:
            checked = self._check_observation(observed, self._order_state)
            try:
                self.check_manual_ready("SELL")
                sale_ready = True
            except ValueError:
                sale_ready = False
            details["sell_positions"] = sell_positions(
                checked.portfolio,
                observed,
                now=self._now(),
                ready=sale_ready,
                locks=self.manual_sell_locks,
                owned_sell_ids=frozenset(
                    s.client_order_id
                    for s in self._known.values()
                    if s.action_type == "NEW" and s.side == "SELL"
                ),
            )
        except ValueError:
            details["sell_positions"] = []
        sale_submissions: list[JsonValue] = [
            cast(
                JsonValue,
                {
                    "client_order_id": s.client_order_id,
                    "symbol": s.symbol,
                    "name": s.manual_sell_binding.name,
                    "operation_id": s.manual_sell_binding.operation_id,
                    "quantity": s.quantity,
                    "limit_price": s.limit_price,
                    "status": statuses.get(s.client_order_id, "RECOVERED"),
                },
            )
            for s in self._known.values()
            if s.action_type == "NEW" and s.manual_sell_binding is not None
        ][:100]
        details["sell_submissions"] = sale_submissions
        # Leave room for authenticated RPC headers inside its 64 KiB frame.
        # Truncated facts are display-only and cannot claim complete statistics.
        while len(canonical_bytes(details)) > 48 * 1024:
            candidates = [
                (key, value)
                for key, value in details.items()
                if isinstance(value, (list, dict)) and value
            ]
            if not candidates:
                raise ValueError("ACCOUNT_QUERY_FAILED")
            key, value = max(candidates, key=lambda pair: len(pair[1]))
            details[key] = (
                value[: len(value) // 2]
                if isinstance(value, list)
                else dict(list(value.items())[: len(value) // 2])
            )
            details["truncated"] = True
        return details
