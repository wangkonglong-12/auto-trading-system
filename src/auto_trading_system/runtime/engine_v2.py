"""Single-owner V2 execution kernel: recovery, durable approvals and J1 dispatch.

All public state-changing methods except broker_event()/emergency_halt() run on
the owner thread. poll() never waits on unfinished futures. This kernel has no
HTTP, QMT import or database connection; the offline application wires a Mock
gateway, account reader and explicit local approvals. Real broker admission and
the local authenticated IPC adapter remain separate release work.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from queue import Empty, Full, Queue
from threading import Lock, get_ident
from time import monotonic
from typing import Literal
from uuid import uuid4

from pydantic import Field, JsonValue

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
    BrokerOrderUpdate,
    BrokerTrade,
)
from auto_trading_system.gateways.broker.port import BrokerGateway
from auto_trading_system.gateways.broker.query_validation import (
    validate_order_query_result,
    validate_trade_query_result,
)
from auto_trading_system.journal.checkpoint_blobs import (
    read_checkpoint,
    write_checkpoint,
)
from auto_trading_system.journal.critical_models import RecoveryRecord, Submission
from auto_trading_system.journal.critical_writer import CriticalJournalOwner, CriticalStorePort
from auto_trading_system.orders.projection import OrderProjection, OrderView
from auto_trading_system.protocol.v2.api import PlanLookup, PlanReceipt
from auto_trading_system.protocol.v2.base import WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.protocol.v2.daily_sync import (
    LocalControlRequest,
    LocalControlResult,
    SessionGrant,
)
from auto_trading_system.protocol.v2.records import MoneyText
from auto_trading_system.reconcile.journal_scan import JournalScan
from auto_trading_system.reconcile.portfolio_checkpoint import (
    OpeningReference,
    PortfolioCheckpoint,
    closing_key,
    load_opening,
)
from auto_trading_system.reconcile.position_lots import (
    ExchangeCalendar,
    PortfolioProjection,
    PositionLot,
)
from auto_trading_system.runtime.day_snapshot import DaySnapshot
from auto_trading_system.runtime.execution_scope import execution_scopes
from auto_trading_system.runtime.memory_state import ReservationBook
from auto_trading_system.runtime.observation_v2 import (
    CheckedObservation,
    check_empty_observation,
    check_observation,
)
from auto_trading_system.runtime.recovery_v2 import (
    AccountFact,
    BrokerRecoverySnapshot,
    RecoveryReport,
    reconcile,
    recovery_evidence,
)
from auto_trading_system.strategies.entry_state import EntryTarget
from auto_trading_system.strategies.exit_context import ExitSessionContext
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
        self._calendar = calendar
        self._opening_reference = opening_reference
        self.exit_context = exit_context
        self._exit_state_guard = exit_state_guard
        self._entry_state_guard = entry_state_guard
        self._entry_target_reader = entry_target_reader
        self.live_exit_gate: object | None = None
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
        self._opening_read: Future[bytes | None] | None = None
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
        self._grant_generation = 0
        self._confirmed: str | None = None
        self._report: RecoveryReport | None = None
        self._broker_snapshot: BrokerRecoverySnapshot | None = None
        self._risk_baseline_equity = (
            Decimal(risk_baseline_equity) if risk_baseline_equity is not None else None
        )
        self._records: list[RecoveryRecord] = []
        self._known: dict[str, Submission] = {}
        self._book = ReservationBook()
        self._page: Future[tuple[RecoveryRecord, ...]] | None = None
        self._query: Future[BrokerRecoverySnapshot] | None = None
        self._checkpoint: Future[bool] | None = None
        self._recovery_deadline = 0.0
        self._recovery_epoch = 0
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
    ) -> None:
        """Attach the live entry state guard once, before a session is armed."""
        self._owner_only()
        if (
            self._entry_state_guard is not None
            or self._entry_target_reader is not None
            or self._grant is not None
        ):
            raise ValueError("entry runtime cannot be replaced in this boot")
        self._entry_state_guard = guard
        self._entry_target_reader = target_reader

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

    def _now(self) -> datetime:
        now = self._clock()
        if now.utcoffset() is None:
            raise ValueError("engine clock must be timezone aware")
        return now.astimezone(CN)

    def broker_event(self, event: BrokerEvent) -> None:
        """Immutable gateway values enter a bounded queue; callback performs no SQL."""
        with self._event_lock:
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

    def _drain_events(self) -> None:
        for _ in range(100):
            try:
                event = self._inbox.get_nowait()
            except Empty:
                break
            try:
                if isinstance(event, BrokerOrderUpdate):
                    order = event.order
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
                elif isinstance(event, BrokerCancelError):
                    # The typed reason code is enough here; arbitrary SDK text
                    # never enters local logs or trusted control responses.
                    self._tighten("CANCEL_REQUIRES_RECONCILIATION")
                    continue
                else:
                    raise ValueError("unrecognized broker callback")
                body = canonical_bytes(record.model_dump(mode="json"))
                self._save_audit("broker_facts", "fact-" + digest(body)[7:], body)
            except Exception:
                self._tighten("BROKER_FACT_INVALID")

    def emergency_halt(self, reason: str = "LOCAL_HALT") -> None:
        """May be invoked on an authenticated emergency thread; never waits on I/O."""
        self.permission.halt(reason)

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
        receipt = self.plans.apply(lookup, artifact_hash)
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
        self._records = []
        self._recovery_deadline = monotonic() + 30
        self._recovery_epoch = self.permission.snapshot().epoch
        self._scan = None
        if self._opening_reference is not None:
            key = self._opening_reference.key
            self._opening_read = self.journal.request(
                lambda store: read_checkpoint(store, key),
                deadline=self._recovery_deadline,
            )
        else:
            self._scan = JournalScan(
                account_id=self.account_id, trade_date=self.trade_date, checkpoint=None
            )
            self._page = self.journal.recovery_page()

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
        if not any((self._page, self._query, self._checkpoint, self._opening_read)):
            return
        if (
            monotonic() >= self._recovery_deadline
            or self.permission.snapshot().epoch != self._recovery_epoch
        ):
            self._page = self._query = self._checkpoint = None
            self._opening_read = None
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
                    self._query = self.dispatcher.observe(self._capture)
            if self._opening_read is not None and self._opening_read.done():
                body = self._opening_read.result()
                self._opening_read = None
                assert self._opening_reference is not None
                checkpoint = load_opening(
                    body,
                    self._opening_reference,
                    account_id=self.account_id,
                    trade_date=self.trade_date,
                    calendar=self._calendar,
                )
                self._opening = checkpoint.lots
                self._scan = JournalScan(
                    account_id=self.account_id, trade_date=self.trade_date, checkpoint=checkpoint
                )
                self._page = self.journal.recovery_page()
            if self._query and self._query.done():
                snapshot = self._query.result()
                self._query = None
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
                self._checkpoint = self.journal.request(
                    lambda store: write_checkpoint(store, report.report_id, body),
                    deadline=self._recovery_deadline,
                )
            if self._checkpoint and self._checkpoint.done():
                if not self._checkpoint.result():
                    raise ValueError("recovery checkpoint was not newly created")
                self._checkpoint = None
                assert self._report is not None
                self._known = {r.submission.client_order_id: r.submission for r in self._records}
                assert self._broker_snapshot is not None
                self._orders = {o.client_order_id: o for o in self._broker_snapshot.orders}
                if self._report.status == "CLEAR":
                    checked = self._check_observation(self._broker_snapshot, self._order_state)
                    self._order_state, self._portfolio = checked.orders, checked.portfolio
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
        except Exception:
            self._page = self._query = self._checkpoint = None
            self._opening_read = None
            self._report = None
            self._tighten("RECOVERY_FAILED")

    @property
    def _opening_hash(self) -> str | None:
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
            or any((self._page, self._query, self._checkpoint, self._opening_read))
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
        scopes = execution_scopes(self.plans.plans.loaded(), self.exit_context)
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
            raise ValueError("plan ownership required for account projection")
        if len({scope.strategy_id for scope in scopes}) != 1:
            raise ValueError("entry and exit scopes require the same strategy owner")
        scope = scopes[0]
        if self.exit_context is not None:
            opening = {lot.lot_id: lot for lot in self._opening}
            if any(
                item.lot_id not in opening
                or opening[item.lot_id].symbol != item.symbol
                or opening[item.lot_id].strategy_id != scope.strategy_id
                for item in self.exit_context.lots
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
            not self._grant
            or self.permission.snapshot().halted
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
        scopes = execution_scopes(self.plans.plans.loaded(), self.exit_context)
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
            or grant.mode != self.limits.mode
            or grant.recovery_report_hash != self._confirmed
            or grant.session_generation <= self._grant_generation
            or any(scope.trade_date != self.trade_date for scope in scopes)
            or not relevant
            or not grant.valid_from
            <= now
            < grant.valid_until
            <= min(scope.valid_until for scope in relevant)
            or not set(grant.directions) <= allowed_directions
            or grant.max_positions > self.limits.max_positions
            or Decimal(grant.max_order_value) > Decimal(self.limits.max_order_value)
            or Decimal(grant.max_buy_value)
            > min(
                Decimal(self.limits.max_buy_value),
                Decimal(buy_scope.max_buy_value) if buy_scope is not None else Decimal(0),
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
                if self.exit_context is None:
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
        grant, loaded, snapshot = self._grant, self.plans.plans.loaded(), self._broker_snapshot
        scopes = execution_scopes(loaded, self.exit_context)
        scope = next(
            (
                value
                for value in scopes
                if (submission.side == "BUY" and value.kind == "ENTRY_PLAN")
                or (submission.side == "SELL" and value.kind == "EXIT_SESSION")
            ),
            None,
        )
        if scope is None and submission.side == "SELL" and self.exit_context is None:
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
            or submission.quantity > self.limits.max_order_quantity
            or Decimal(submission.maximum_amount) > Decimal(grant.max_order_value)
        ):
            return False
        if submission.action_type == "CANCEL":
            # A second cancel cannot race an unresolved prior cancellation.
            # Rechecking the same pending J1 excludes its own request ID.
            if any(
                s.action_type == "CANCEL"
                and s.original_client_order_id == submission.original_client_order_id
                and s.client_order_id != submission.client_order_id
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
        if submission.side == "SELL":
            if self.exit_context is not None and (
                not self.exit_context.valid_from <= now < self.exit_context.stop_new_at
                or submission.quantity > self.exit_context.max_order_quantity
                or self._exit_state_guard is None
                or not self._exit_state_guard(submission)
            ):
                return False
            if self.exit_context is not None:
                binding = submission.sell_binding
                if binding is None or len(binding.allocations) != 1:
                    return False
                lot_id = binding.allocations[0].lot_id
                policy = next((p for p in self.exit_context.lots if p.lot_id == lot_id), None)
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
                    rule_id=self.exit_context.rule_id if self.exit_context else EXIT_RULE,
                )
                and self._risk_guard(submission)
            )
        if loaded is None:
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
            (a for a in loaded.payload.allocations if a.symbol == submission.symbol), None
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
            > Decimal(allocation.max_value)
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
        return len(symbols) <= grant.max_positions and self._risk_guard(submission)

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

    def entry_resources(self, symbol: str) -> tuple[Decimal, Decimal, int]:
        """Current cash/amount ceiling and remaining symbol slots, without granting authority."""
        self._owner_only()
        loaded, grant, broker = self.plans.plans.loaded(), self._grant, self._broker_snapshot
        if loaded is None or grant is None or broker is None:
            return Decimal(0), Decimal(0), 0
        allocation = next((a for a in loaded.payload.allocations if a.symbol == symbol), None)
        if allocation is None:
            return Decimal(0), Decimal(0), 0
        exposures = self.entry_exposures()
        symbols = {p.symbol for p in broker.account.positions if p.quantity} | {
            e.symbol for e in exposures
        }
        cash = min(
            self._book.available_cash(broker.account.observation()).amount,
            Decimal(grant.max_buy_value) - sum((e.amount for e in exposures), Decimal(0)),
            Decimal(allocation.max_value)
            - sum((e.amount for e in exposures if e.symbol == symbol), Decimal(0)),
        )
        slots = grant.max_positions - len(symbols - {symbol})
        return max(Decimal(0), cash), Decimal(grant.max_order_value), max(0, slots)

    def exit_inputs(self) -> tuple[PortfolioProjection, AccountFact | None, tuple[Submission, ...]]:
        """Immutable same-owner inputs for the D1 strategy driver; never grant permission."""
        self._owner_only()
        return (
            self._portfolio,
            self._broker_snapshot.account if self._broker_snapshot else None,
            tuple(self._known.values()),
        )

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
            self._order_state.register(submission)
            self._coordinator.begin(submission, valid_until=valid_until)
        except Exception:
            self._tighten("SUBMISSION_BEGIN_FAILED")
            raise
        self._version += 1
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
            and Decimal(self.limits.max_daily_loss) > 0
            and self._risk_baseline_equity - Decimal(self._broker_snapshot.account.total_equity)
            >= Decimal(self.limits.max_daily_loss)
        ):
            self._tighten("MAX_DAILY_LOSS_REACHED")
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
        self._notice_permission()
        self._poll_observation()
        if (
            self._grant
            and not self.permission.snapshot().halted
            and self._observation is None
            and self._observed is None
            and not self._pending
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

    def operator_cancel_order(self, client_order_id: str) -> dict[str, JsonValue]:
        """Persist and dispatch one cancel for a system-owned active order."""
        self._owner_only()
        original = self._known.get(client_order_id)
        order = self._orders.get(client_order_id)
        if (
            original is None
            or original.action_type != "NEW"
            or order is None
            or order.broker_order_id is None
            or order.is_terminal
        ):
            raise ValueError("ORDER_NOT_CANCELABLE")
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
        self.submit(cancel, valid_until=monotonic() + 0.25)
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
        return self.dispatcher.observe(self._capture)

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
        return {
            "available": True,
            "observed_at": account.observed_at.isoformat(),
            "cash": account.cash,
            "cash_frozen": account.cash_frozen,
            "market_value": account.market_value,
            "total_equity": account.total_equity,
            "coherent": account.coherent,
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
                    "limit_price": str(order.limit_price.amount),
                    "reason": order.status_reason,
                    "owned_by_system": order.client_order_id in self._known,
                    "cancelable": order.client_order_id in self._known
                    and order.broker_order_id is not None
                    and not order.is_terminal,
                }
                for order in observed.orders[:100]
            ],
            "trades": [
                {
                    "trade_id": trade.trade_id,
                    "symbol": str(trade.symbol),
                    "side": trade.side.value,
                    "quantity": trade.quantity.value,
                    "price": str(trade.price.amount),
                    "traded_at": trade.traded_at.value.isoformat(),
                }
                for trade in observed.trades[:100]
            ],
            "truncated": any(
                len(rows) > 100 for rows in (account.positions, observed.orders, observed.trades)
            ),
        }
