"""Nonblocking engine observations -> one bounded file worker -> agent handoff."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from functools import partial
from threading import BoundedSemaphore
from typing import TypeVar

from auto_trading_system.config.node_v2 import NodeProcessConfig
from auto_trading_system.gateways.broker.models import (
    BrokerCancelError,
    BrokerEvent,
    BrokerOrder,
    BrokerOrderIdentity,
    BrokerOrderUpdate,
    BrokerRequestError,
    BrokerTrade,
)
from auto_trading_system.journal.critical_models import Submission
from auto_trading_system.protocol.executions.models import (
    Batch,
    CancelFailure,
    Candidate,
    Draft,
    Fact,
    Identity,
    OrderFact,
    Progress,
    Receipt,
    RequestFailure,
    SubmissionFact,
    TradeFact,
    body_hash,
)
from auto_trading_system.protocol.tail_picks import TailPicks
from auto_trading_system.protocol.v2.plans import PlanEnvelope
from auto_trading_system.reconcile.position_lots import lot_id
from auto_trading_system.strategies.entry_state import EntryTarget
from auto_trading_system.sync.execution_store import ExecutionStore

T = TypeVar("T")


class ExecutionReporter:
    def __init__(self, config: NodeProcessConfig, halt: Callable[[str], object]) -> None:
        self.config, self.halt = config, halt
        self.failed = False
        self._error: Exception | None = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="execution-report-file")
        self._slots = BoundedSemaphore(1024)
        self._seen: dict[str, str] = {}
        try:
            self.store = self.request(
                lambda: ExecutionStore(
                    config.data_root / "jobs/.execution-reporting.json",
                    scope=self.scope,
                    node_id=config.node_id,
                )
            ).result(timeout=5)
        except Exception:
            self.close()
            raise
        for stream in self.store.state.streams.values():
            if (
                stream.identity.account_id,
                stream.identity.strategy_id,
                stream.identity.environment,
            ) != (config.account_id, config.strategy_id, self.environment):
                self.close()
                raise ValueError("retained execution reporting scope differs")

    @property
    def environment(self) -> str:
        return "REAL" if self.config.broker == "QMT_TRADING" else "TEST"

    @property
    def scope(self) -> str:
        from auto_trading_system.protocol.v2.canonical import digest

        return digest(self.config.scope())

    def request(self, function: Callable[[], T]) -> Future[T]:
        if self.failed or not self._slots.acquire(blocking=False):
            self.failed = True
            self.halt("EXECUTION_REPORTING_UNAVAILABLE")
            raise ValueError("execution reporting unavailable") from self._error

        def run() -> T:
            try:
                return function()
            except Exception as error:
                self.failed = True
                self._error = error
                self.halt("EXECUTION_REPORTING_PERSISTENCE_FAILED")
                raise
            finally:
                self._slots.release()

        return self._pool.submit(run)

    def close(self) -> None:
        self._pool.shutdown(wait=True)

    def _identity(self, day: object, symbol: str) -> Identity:
        return Identity.model_validate(
            {
                "account_id": self.config.account_id,
                "strategy_id": self.config.strategy_id,
                "selection_date": day,
                "symbol": symbol,
                "environment": self.environment,
            }
        )

    def _append(
        self, identity: Identity, fact: Fact, now: datetime, source: str | None = None
    ) -> None:
        # Source tokens contain identifiers/codes only. SDK free text is deliberately absent.
        from auto_trading_system.protocol.v2.canonical import digest

        source_key = digest(source.encode()) if source else body_hash(fact)
        self.store.append(
            Draft(identity=identity, source_key=source_key, occurred_at=now, fact=fact)
        )

    def candidates(
        self, plans: tuple[PlanEnvelope, ...], names: dict[str, str | None], now: datetime
    ) -> None:
        for plan in plans:
            phase = plan.payload.source.decision_phase
            if phase is None:
                continue
            for item in plan.payload.allocations:
                key = ":".join(
                    (
                        "candidate",
                        str(plan.payload.trade_date),
                        phase,
                        plan.payload.source.source_run_id,
                        item.symbol,
                    )
                )
                if key in self._seen:
                    continue
                fact = Candidate(
                    kind="CANDIDATE",
                    phase=phase,
                    source_run_id=plan.payload.source.source_run_id,
                    name=names.get(item.symbol),
                    target_amount=f"{Decimal(item.target_value):.2f}",
                )
                identity = self._identity(plan.payload.trade_date, item.symbol)
                self.request(partial(self._append, identity, fact, now, key))
                self._seen[key] = "seen"

    def tail_candidates(
        self,
        feed: TailPicks,
        target_amount: str,
        now: datetime,
        *,
        filtered_plans: tuple[PlanEnvelope, ...] = (),
    ) -> None:
        from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest

        plans = {
            p.payload.source.decision_phase: p
            for p in filtered_plans
            if p.payload.trade_date == feed.trade_date
        }
        for batch in feed.batches:
            if batch.status != "READY":
                continue
            for stock in batch.stocks:
                key = (
                    f"candidate:{feed.trade_date}:{batch.phase}:{batch.source_run_id}:{stock.code}"
                )
                assert batch.source_run_id is not None
                fact = Candidate(
                    kind="CANDIDATE",
                    phase=batch.phase,
                    source_run_id=batch.source_run_id,
                    name=stock.name,
                    target_amount=f"{Decimal(target_amount):.2f}",
                )
                identity = self._identity(feed.trade_date, stock.code)
                if key not in self._seen:
                    self.request(partial(self._append, identity, fact, now, key))
                    self._seen[key] = "seen"
                plan = plans.get(batch.phase)
                if (
                    plan is None
                    or plan.payload.source.source_run_id != batch.source_run_id
                    or plan.payload.source.source_hash
                    != digest(canonical_bytes(batch.model_dump(mode="json")))
                    or any(a.symbol == stock.code for a in plan.payload.allocations)
                ):
                    continue
                skipped = Progress(
                    kind="PROGRESS",
                    plan_hash=plan.envelope.payload_hash,
                    status="BOARD_NOT_ALLOWED",
                    target_quantity=0,
                )
                skip_key = f"board-filter:{plan.envelope.payload_hash}:{stock.code}"
                if skip_key not in self._seen:

                    def record(
                        identity: Identity = identity,
                        skipped: Progress = skipped,
                        skip_key: str = skip_key,
                    ) -> None:
                        # Never mark an execution with an existing strategy order as excluded.
                        if identity not in self.store.state.orders.values():
                            self._append(identity, skipped, now, skip_key)

                    self.request(record)
                    self._seen[skip_key] = "seen"

    def submission(self, item: Submission, status: str, now: datetime) -> None:
        key = "submission:" + item.client_order_id + ":" + status
        if key in self._seen:
            return

        def record() -> None:
            if item.side == "BUY":
                identity = self._identity(item.trade_date, item.symbol)
            else:
                if item.sell_binding is None:
                    raise ValueError("cannot report an unbound strategy sale")
                identities = {
                    self.store.state.lots[a.lot_id].execution_id: self.store.state.lots[a.lot_id]
                    for a in item.sell_binding.allocations
                }
                if len(identities) != 1:
                    raise ValueError("sale spans independent executions; split before reporting")
                identity = next(iter(identities.values()))
            original = item.original_client_order_id
            if original is not None:
                identity = self.store.state.orders[original]
            acquired_lot = (
                lot_id(item) if item.side == "BUY" and item.action_type == "NEW" else None
            )
            self.store.bind(order_id=item.client_order_id, identity=identity, lot_id=acquired_lot)
            fact = SubmissionFact.model_validate(
                {
                    "kind": "SUBMISSION",
                    "client_order_id": item.client_order_id,
                    "side": item.side,
                    "action": item.action_type,
                    "original_client_order_id": original,
                    "quantity": item.quantity,
                    "price": item.limit_price,
                    "status": status,
                    "lot_id": acquired_lot,
                }
            )
            self._append(identity, fact, now, key)

        self.request(record)
        self._seen[key] = "seen"

    def progress(
        self,
        plan: PlanEnvelope,
        targets: tuple[EntryTarget, ...],
        states: tuple[tuple[str, str], ...],
        now: datetime,
    ) -> None:
        by_symbol = {t.symbol: t for t in targets}
        for symbol, status in states:
            target = by_symbol.get(symbol)
            fact = Progress(
                kind="PROGRESS",
                plan_hash=plan.envelope.payload_hash,
                status=status,
                target_quantity=target.target_quantity if target else None,
            )
            key = plan.envelope.payload_hash + ":" + symbol + ":" + body_hash(fact)
            if key in self._seen:
                continue
            identity = self._identity(plan.payload.trade_date, symbol)
            self.request(partial(self._append, identity, fact, now, key))
            self._seen[key] = "seen"

    def broker(self, event: BrokerEvent, submission: Submission | None) -> None:
        if isinstance(event, BrokerOrderIdentity):
            return  # Private identity evidence is not a financial execution event.
        if submission is None:
            return  # Account observations do not establish strategy ownership.
        self.submission(
            submission,
            "RECOVERED",
            event.order.updated_at.value
            if isinstance(event, BrokerOrderUpdate)
            else event.traded_at.value
            if isinstance(event, BrokerTrade)
            else event.occurred_at.value,
        )

        def record() -> None:
            identity = self.store.state.orders[submission.client_order_id]
            fact: Fact
            if isinstance(event, BrokerOrderUpdate):
                order = event.order
                status = {
                    "CREATED": "ACTIVE",
                    "ACCEPTED": "ACTIVE",
                    "PENDING": "ACTIVE",
                    "CANCEL_PENDING": "ACTIVE",
                    "SUBMITTING": "ACTIVE",
                    "ACKED": "ACTIVE",
                    "PARTIAL_FILL_CANCEL_PENDING": "PARTIAL",
                    "PARTIALLY_FILLED_CANCELLED": "CANCELLED",
                    "PARTIALLY_FILLED": "PARTIAL",
                    "FILLED": "FILLED",
                    "CANCELLED": "CANCELLED",
                    "REJECTED": "REJECTED",
                }.get(order.status.value, "UNKNOWN")
                fact = OrderFact.model_validate(
                    {
                        "kind": "ORDER",
                        "client_order_id": order.client_order_id,
                        "broker_order_id": order.broker_order_id,
                        "side": order.side.value,
                        "quantity": order.order_quantity.value,
                        "filled_quantity": order.filled_quantity.value,
                        "price": format(order.limit_price.amount, "f"),
                        "status": status,
                        "reason_code": order.status_reason,
                    }
                )
                self._append(
                    identity,
                    fact,
                    order.updated_at.value,
                    "order:" + order.client_order_id + ":" + body_hash(fact),
                )
            elif isinstance(event, BrokerTrade):
                fact = TradeFact.model_validate(
                    {
                        "kind": "TRADE",
                        "client_order_id": event.client_order_id,
                        "broker_order_id": event.broker_order_id,
                        "broker_trade_id": event.trade_id,
                        "side": event.side.value,
                        "quantity": event.quantity.value,
                        "price": format(event.price.amount, "f"),
                        "amount": f"{event.notional.amount:.2f}",
                    }
                )
                self._append(
                    identity,
                    fact,
                    event.traded_at.value,
                    f"trade:{event.traded_at.value.date()}:{identity.symbol[-2:]}:{event.trade_id}",
                )
            elif isinstance(event, BrokerCancelError):
                fact = CancelFailure.model_validate(
                    {
                        "kind": "CANCEL_ERROR",
                        "client_order_id": event.client_order_id,
                        "broker_order_id": event.broker_order_id,
                        "side": submission.side,
                        "reason_code": event.error_code,
                    }
                )
                self._append(
                    identity,
                    fact,
                    event.occurred_at.value,
                    "cancel-error:"
                    + event.client_order_id
                    + ":"
                    + event.error_code
                    + ":"
                    + event.occurred_at.value.isoformat(),
                )
            elif isinstance(event, BrokerRequestError):
                fact = RequestFailure.model_validate(
                    {
                        "kind": "REQUEST_ERROR",
                        "client_order_id": event.client_order_id,
                        "action": event.action_type,
                        "side": submission.side,
                        "reason_code": event.reason_code,
                    }
                )
                self._append(
                    identity,
                    fact,
                    event.occurred_at.value,
                    "request-error:" + event.client_order_id + ":" + event.reason_code,
                )

        self.request(record)

    def query_facts(
        self,
        orders: tuple[BrokerOrder, ...],
        trades: tuple[BrokerTrade, ...],
        known: dict[str, Submission],
    ) -> None:
        for order in orders:
            key = "query-order:" + order.client_order_id
            signature = str(
                (order.status, order.filled_quantity, order.broker_order_id, order.status_reason)
            )
            if self._seen.get(key) != signature:
                self.broker(BrokerOrderUpdate(order), known.get(order.client_order_id))
                self._seen[key] = signature
        for trade in trades:
            key = "query-trade:" + str(trade)
            if key not in self._seen:
                self.broker(trade, known.get(trade.client_order_id))
                self._seen[key] = "seen"

    def batch(self) -> Batch | None:
        return self.request(self.store.batch).result(timeout=3)

    def acknowledge(self, batch: Batch, receipt: Receipt) -> None:
        self.request(lambda: self.store.acknowledge(batch, receipt)).result(timeout=3)
