"""Explicit stock sales over owned lots; no rule engine, retries, or boot replay."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import time, timedelta
from decimal import Decimal
from time import monotonic
from uuid import uuid4

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from auto_trading_system.control.ipc_protocol import IPCError
from auto_trading_system.execution.sell_policy import exit_action_id
from auto_trading_system.gateways.broker.port import BrokerGateway
from auto_trading_system.journal.critical_models import (
    ActionTarget,
    ManualSellBinding,
    SellAllocation,
    SellBinding,
    Submission,
)
from auto_trading_system.orders.projection import OrderView
from auto_trading_system.protocol.v2.base import WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.runtime.engine_v2 import EngineKernel
from auto_trading_system.runtime.manual_buy import ManualBuyInput, ManualFacts
from auto_trading_system.security.operator_session import AuthenticatedOperatorSession
from auto_trading_system.strategies.entry_state import EntryInstrument

MAX_CHILDREN = 32
RULE = "manual-sell-v1"


class ManualSellInput(ManualBuyInput):
    """Same decimal/stock syntax; sale admission uses owned lots, not buy rules."""


class ManualSellLookup(WireModel):
    operation_id: str = Field(pattern=r"^manual-sell-[a-f0-9]{32}$")


class ManualSellOperation(WireModel):
    operation_id: str = Field(pattern=r"^manual-sell-[a-f0-9]{32}$")
    account_id: str
    actor_id: str
    engine_boot_id: str
    permission_epoch: int
    confirmed_at: AwareDatetime
    symbol: str
    name: str
    quantity: int = Field(gt=0)
    limit_price: str
    children: tuple[Submission, ...] = Field(min_length=1, max_length=MAX_CHILDREN)

    @model_validator(mode="after")
    def exact(self) -> ManualSellOperation:
        if sum(c.quantity for c in self.children) != self.quantity or len(
            {c.client_order_id for c in self.children}
        ) != len(self.children):
            raise ValueError("sale children must uniquely cover confirmed quantity")
        for child in self.children:
            b = child.manual_sell_binding
            if (
                b is None
                or b.operation_id != self.operation_id
                or b.actor_id != self.actor_id
                or (
                    child.account_id,
                    child.symbol,
                    child.side,
                    child.limit_price,
                    child.engine_boot_id,
                    child.permission_epoch,
                )
                != (
                    self.account_id,
                    self.symbol,
                    "SELL",
                    self.limit_price,
                    self.engine_boot_id,
                    self.permission_epoch,
                )
                or child.action_type != "NEW"
                or b.confirmed_at != self.confirmed_at
            ):
                raise ValueError("sale child differs from confirmed operation")
        return self


@dataclass
class _Preview:
    session_key: str
    operation: ManualSellOperation
    expires: float
    confirmed: bool = False


@dataclass
class _Active:
    operation: ManualSellOperation
    expires: float
    saved: Future[bool]
    next_child: int = 0
    query: Future[ManualFacts] | None = None
    stopped: bool = False


def operation_key(identity: str) -> str:
    return "manual-sell:" + identity


def _broker_status(views: dict[str, OrderView], identity: str) -> str | None:
    view = views.get(identity)
    return view.order.status.value if view is not None and view.order is not None else None


class ManualSellController:
    def __init__(
        self,
        engine: EngineKernel,
        *,
        strategy_id: str,
        settings_hash: str,
        instrument_reader: Callable[[str, str], tuple[EntryInstrument, str]],
        settings_current: Callable[[], bool],
    ) -> None:
        self.engine, self.strategy_id = engine, strategy_id
        self.settings_hash, self.instrument_reader = settings_hash, instrument_reader
        self.settings_current = settings_current
        self.previews: dict[str, _Preview] = {}
        self.active: dict[str, _Active] = {}

    def query(self, value: ManualSellInput) -> Future[ManualFacts]:
        engine = self.engine
        engine.check_manual_ready("SELL")
        if (
            not engine.dispatcher.idle
            or not engine.critical_idle
            or any(s.status in {"PERSISTING", "DISPATCHING"} for s in engine.snapshot().submissions)
        ):
            raise IPCError("ACCOUNT_QUERY_BUSY")

        def capture(broker: BrokerGateway) -> ManualFacts:
            rule, name = self.instrument_reader(value.symbol, value.limit_price)
            return ManualFacts(engine._capture(broker), rule, name)

        return engine.dispatcher.observe(capture)

    def query_confirmation(
        self,
        session: AuthenticatedOperatorSession,
        identity: str,
    ) -> Future[ManualFacts] | None:
        preview = self._preview(session, identity)
        if preview.confirmed:
            return None
        op = preview.operation
        return self.query(
            ManualSellInput(symbol=op.symbol, limit_price=op.limit_price, quantity=op.quantity)
        )

    def _preview(self, session: AuthenticatedOperatorSession, identity: str) -> _Preview:
        p = self.previews.get(identity)
        if p is None or p.session_key != session.session_key:
            raise IPCError("MANUAL_SELL_PREVIEW_NOT_FOUND")
        if not p.confirmed and monotonic() >= p.expires:
            raise IPCError("MANUAL_SELL_PREVIEW_EXPIRED")
        return p

    def _build(
        self,
        session: AuthenticatedOperatorSession,
        value: ManualSellInput,
        facts: ManualFacts,
        identity: str,
        ids: tuple[str, ...] = (),
    ) -> ManualSellOperation:
        engine, rule = self.engine, facts.instrument
        engine.check_manual_ready("SELL")
        now = engine._now()
        if not (time(9, 30) <= now.time() < time(11, 30) or time(13) <= now.time() < time(14, 55)):
            raise IPCError("MANUAL_SELL_OUTSIDE_WINDOW")
        if value.symbol in engine.manual_sell_locks or rule.symbol != value.symbol:
            raise IPCError("MANUAL_SELL_BUSY")
        checked = engine._check_observation(facts.account, engine._order_state)
        if any(
            v.phase != "TERMINAL_CONFIRMED"
            and v.order is not None
            and str(v.order.symbol) == value.symbol
            for v in checked.orders.views()
        ):
            raise IPCError("MANUAL_SELL_ORDER_CONFLICT")
        portfolio = checked.portfolio
        available = portfolio.sellable(
            symbol=value.symbol,
            strategy_id=self.strategy_id,
            as_of=engine.trade_date,
            broker_sellable=next(
                (p.sellable for p in facts.account.account.positions if p.symbol == value.symbol), 0
            ),
            reflected_order_ids=frozenset(o.client_order_id for o in facts.account.orders),
        )
        if value.quantity > available or engine._opening_hash is None:
            raise IPCError("MANUAL_SELL_INSUFFICIENT")
        lots = sorted(
            (
                owned_lot
                for owned_lot in portfolio.lots
                if owned_lot.symbol == value.symbol
                and owned_lot.strategy_id == self.strategy_id
                and owned_lot.eligible_sell_date <= engine.trade_date
                and owned_lot.remaining > owned_lot.reserved_quantity
            ),
            key=lambda owned_lot: (owned_lot.trade_date_acquired, owned_lot.lot_id),
        )
        children: list[Submission] = []
        left = value.quantity
        liquidating = value.quantity == available and available == sum(
            owned_lot.remaining - owned_lot.reserved_quantity for owned_lot in lots
        )
        for lot in lots:
            if not left:
                break
            take = min(left, lot.remaining - lot.reserved_quantity)
            if value.quantity % 100 == 0 and not liquidating:
                take = take // 100 * 100
                if not take:
                    continue
            # Preserve existing A-share exit semantics: 100-share steps; the
            # final odd remainder is allowed only when closing that entire lot.
            if take % 100 and (take != lot.remaining or lot.reserved_quantity):
                raise IPCError("MANUAL_SELL_QUANTITY_INVALID")
            remaining = lot.remaining
            while take:
                quantity = min(take, engine.limits.max_order_quantity)
                if quantity < take:
                    quantity = quantity // 100 * 100
                if not quantity or len(children) >= MAX_CHILDREN:
                    raise IPCError("MANUAL_SELL_TOO_MANY_CHILDREN")
                index = len(children)
                client_id = ids[index] if index < len(ids) else "ord_" + uuid4().hex[:20]
                binding = ManualSellBinding(
                    operation_id=identity,
                    strategy_id=self.strategy_id,
                    actor_id=session.actor_id,
                    settings_hash=self.settings_hash,
                    symbol=value.symbol,
                    name=facts.name,
                    quantity=quantity,
                    limit_price=value.limit_price,
                    confirmed_at=now,
                    lower_price=rule.lower_price,
                    upper_price=rule.upper_price,
                    price_tick=rule.price_tick,
                    quantity_step=100,
                )
                remaining -= quantity
                children.append(
                    Submission(
                        client_order_id=client_id,
                        account_id=engine.account_id,
                        trade_date=engine.trade_date,
                        action_type="NEW",
                        context_type="MANUAL_EXIT",
                        plan_hash=digest(canonical_bytes(binding.model_dump(mode="json"))),
                        symbol=value.symbol,
                        side="SELL",
                        quantity=quantity,
                        limit_price=value.limit_price,
                        maximum_amount="0",
                        remark_token=client_id[4:],
                        engine_boot_id=engine.boot_id,
                        permission_epoch=engine.snapshot().permission_epoch,
                        target=ActionTarget(
                            action_id=exit_action_id(
                                engine.account_id, self.strategy_id, lot.lot_id, RULE
                            ),
                            target_remaining=remaining,
                            rule_id=RULE,
                        ),
                        sell_binding=SellBinding(
                            strategy_id=self.strategy_id,
                            opening_hash=engine._opening_hash,
                            allocations=(SellAllocation(lot_id=lot.lot_id, quantity=quantity),),
                        ),
                        manual_sell_binding=binding,
                    )
                )
                take -= quantity
                left -= quantity
        if left:
            raise IPCError("MANUAL_SELL_INSUFFICIENT")
        return ManualSellOperation(
            operation_id=identity,
            account_id=engine.account_id,
            actor_id=session.actor_id,
            engine_boot_id=engine.boot_id,
            permission_epoch=engine.snapshot().permission_epoch,
            confirmed_at=now,
            symbol=value.symbol,
            name=facts.name,
            quantity=value.quantity,
            limit_price=value.limit_price,
            children=tuple(children),
        )

    def preview(
        self, session: AuthenticatedOperatorSession, value: ManualSellInput, facts: ManualFacts
    ) -> dict[str, JsonValue]:
        self.previews = {
            k: p
            for k, p in self.previews.items()
            if (p.confirmed and k in self.active) or p.expires > monotonic()
        }
        if len(self.previews) >= 32:
            raise IPCError("CONSOLE_BUSY")
        operation = self._build(session, value, facts, "manual-sell-" + uuid4().hex)
        if len(canonical_bytes(operation.model_dump(mode="json"))) > 48 * 1024:
            raise IPCError("MANUAL_SELL_TOO_MANY_CHILDREN")
        self.engine.stage_manual_observation(facts.account, "SELL")
        self.previews[operation.operation_id] = _Preview(
            session.session_key, operation, monotonic() + 30
        )
        return {
            "operation_id": operation.operation_id,
            "symbol": operation.symbol,
            "name": operation.name,
            "quantity": operation.quantity,
            "limit_price": operation.limit_price,
            "amount": format(Decimal(value.limit_price) * value.quantity, ".2f"),
            "child_count": len(operation.children),
            "expires_at": (operation.confirmed_at + timedelta(seconds=30)).isoformat(),
        }

    def confirm(
        self, session: AuthenticatedOperatorSession, identity: str, facts: ManualFacts | None
    ) -> dict[str, JsonValue]:
        preview = self._preview(session, identity)
        if preview.confirmed:
            return self.result(session, identity, preview.operation)
        if facts is None:
            raise IPCError("MANUAL_SELL_PREVIEW_STALE")
        old = preview.operation
        value = ManualSellInput(
            symbol=old.symbol, limit_price=old.limit_price, quantity=old.quantity
        )
        current = self._build(
            session, value, facts, identity, tuple(c.client_order_id for c in old.children)
        )
        if current.permission_epoch != old.permission_epoch or [
            (c.sell_binding, c.target) for c in current.children
        ] != [(c.sell_binding, c.target) for c in old.children]:
            raise IPCError("MANUAL_SELL_PREVIEW_STALE")
        self.engine.stage_manual_observation(facts.account, "SELL")
        body = canonical_bytes(current.model_dump(mode="json"))
        deadline = monotonic() + 0.25
        future = self.engine.journal.request(
            lambda store: store.append_evidence("control_records", operation_key(identity), body),
            deadline=deadline,
        )
        self.engine._audit.append((future, deadline))
        preview.confirmed, preview.operation = True, current
        self.engine.manual_sell_locks.add(current.symbol)
        self.active[identity] = _Active(current, preview.expires, future)
        return self.result(session, identity, current)

    def lookup(self, identity: str) -> Future[bytes | None]:
        return self.engine.journal.request(
            lambda store: store.read_evidence("control_records", operation_key(identity)),
            deadline=monotonic() + 2,
        )

    def result(
        self,
        session: AuthenticatedOperatorSession,
        identity: str,
        operation: ManualSellOperation | None,
    ) -> dict[str, JsonValue]:
        if operation is None:
            preview = self.previews.get(identity)
            if preview is not None and preview.confirmed:
                operation = preview.operation
            else:
                self.engine.check_manual_ready("SELL")
                if (
                    preview is not None and preview.expires > monotonic()
                ) or not self.engine.critical_idle:
                    raise IPCError("MANUAL_SELL_RESULT_UNKNOWN")
                return {"operation_id": identity, "status": "NOT_SUBMITTED", "children": []}
        if (
            operation.account_id != self.engine.account_id
            or operation.actor_id != session.actor_id
            or operation.operation_id != identity
        ):
            raise IPCError("MANUAL_SELL_RESULT_UNKNOWN")
        engine = self.engine
        states = {s.client_order_id: s.status for s in engine.snapshot().submissions}
        views = {v.client_order_id: v for v in engine.snapshot().orders}
        known = [c for c in operation.children if c.client_order_id in engine._known]
        current = self.active.get(identity)
        unfinished = (
            current is not None
            and not current.stopped
            and current.next_child < len(operation.children)
        )
        status = (
            "PERSISTING"
            if not known and unfinished
            else "DISPATCHING"
            if unfinished
            else "SUBMITTED"
            if len(known) == len(operation.children)
            else "PARTIAL_SUBMITTED"
            if known
            else "NOT_SUBMITTED"
        )
        if any(
            states.get(c.client_order_id)
            in {
                "UNCERTAIN",
                "PERSISTENCE_UNCERTAIN",
                "PERSISTENCE_FAILED",
                "RECOVERY_REQUIRED",
                "BEGIN_FAILED",
                "EVIDENCE_MISMATCH",
            }
            for c in known
        ):
            status = "UNKNOWN"
        if operation.confirmed_at.date() != engine.trade_date:
            status = "ARCHIVED"
        return {
            "operation_id": identity,
            "symbol": operation.symbol,
            "name": operation.name,
            "quantity": operation.quantity,
            "limit_price": operation.limit_price,
            "status": status,
            "children": [
                {
                    "client_order_id": c.client_order_id,
                    "quantity": c.quantity,
                    "status": _broker_status(views, c.client_order_id)
                    or states.get(c.client_order_id, "NOT_SUBMITTED"),
                    "submitted": c.client_order_id in engine._known,
                }
                for c in operation.children
            ],
        }

    def poll(self) -> None:
        engine = self.engine
        for identity, active in tuple(self.active.items()):
            op = active.operation
            views = {v.client_order_id: v for v in engine.snapshot().orders}
            if active.next_child >= len(op.children) or active.stopped:
                if all(
                    c.client_order_id not in engine._known
                    or (
                        c.client_order_id in views
                        and views[c.client_order_id].phase == "TERMINAL_CONFIRMED"
                    )
                    for c in op.children
                ):
                    engine.manual_sell_locks.discard(op.symbol)
                    self.active.pop(identity, None)
                continue
            if (
                monotonic() >= active.expires
                or engine.snapshot().permission_epoch != op.permission_epoch
                or not engine.journal.healthy
            ):
                active.stopped = True
                continue
            if not active.saved.done() or not engine.critical_idle or not engine.dispatcher.idle:
                continue
            try:
                active.saved.result()
                # An earlier rejection, cancellation or uncertain outcome stops
                # the remaining plan; it never causes replacement orders.
                prior = op.children[: active.next_child]
                states = {s.client_order_id: s.status for s in engine.snapshot().submissions}
                if any(
                    states.get(c.client_order_id) in {"PERSISTING", "DISPATCHING"} for c in prior
                ):
                    continue
                if any(
                    states.get(c.client_order_id) not in {"SDK_ACCEPTED", "RECOVERED"}
                    for c in prior
                ):
                    active.stopped = True
                    continue
                if any(
                    _broker_status(views, c.client_order_id)
                    in {
                        "REJECTED",
                        "CANCEL_PENDING",
                        "PARTIAL_FILL_CANCEL_PENDING",
                        "CANCELLED",
                        "PARTIALLY_FILLED_CANCELLED",
                    }
                    for c in prior
                ):
                    active.stopped = True
                    continue
                value = ManualSellInput(
                    symbol=op.symbol, limit_price=op.limit_price, quantity=op.quantity
                )
                if active.query is None:
                    active.query = self.query(value)
                    continue
                if not active.query.done():
                    continue
                facts = active.query.result()
                active.query = None
                # Install new fills/reservations through the existing durable
                # observation pipeline, then grant exactly one frozen child.
                engine.stage_manual_observation(facts.account, "SELL")
                if not engine.critical_idle or engine._observed is not None:
                    continue
                child = op.children[active.next_child]
                deadline = min(active.expires, monotonic() + 2)
                engine.authorize_manual(child, valid_until=deadline)
                engine.submit(child, valid_until=deadline)
                active.next_child += 1
            except (ValueError, RuntimeError):
                active.stopped = True
