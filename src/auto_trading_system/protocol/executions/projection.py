"""Pure, replayable master-row projection. Receipt order cannot invent a fill."""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import Field

from .models import (
    CancelFailure,
    Candidate,
    Event,
    Identity,
    Model,
    Money,
    OrderFact,
    Price,
    Progress,
    RequestFailure,
    SubmissionFact,
    TradeFact,
)


class Fill(Model):
    business_date: date
    occurred_at: datetime
    fact: TradeFact


class State(Model):
    identity: Identity
    last_seq: int = 0
    name: str | None = None
    phase: str | None = None
    source_run_id: str | None = None
    target_amount: Money | None = None
    owner_plan: str | None = None
    progress: Progress | None = None
    submissions: dict[str, SubmissionFact] = Field(default_factory=dict)
    orders: dict[str, OrderFact] = Field(default_factory=dict)
    trades: dict[str, Fill] = Field(default_factory=dict)
    cancel_errors: dict[str, CancelFailure] = Field(default_factory=dict)
    request_errors: dict[str, RequestFailure] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Summary(Model):
    execution_id: str
    identity: Identity
    name: str | None
    phase: str | None
    source_run_id: str | None
    target_amount: Money | None
    target_quantity: int | None
    buy_status: str
    sell_status: str
    stage: Literal["WAITING", "BUYING", "NOT_BOUGHT", "HOLDING", "SELLING", "CLOSED", "REVIEW"]
    bought_quantity: int
    buy_amount: Money
    buy_average_price: Price | None
    sold_quantity: int
    sell_amount: Money
    sell_average_price: Price | None
    remaining_quantity: int
    available_quantity: int | None = None
    first_buy_at: datetime | None
    last_buy_at: datetime | None
    last_sell_at: datetime | None
    reason_code: str | None
    version: int
    created_at: datetime | None
    updated_at: datetime | None


def apply_event(state: State, event: Event) -> State:
    if event.identity != state.identity or event.seq != state.last_seq + 1:
        raise ValueError("execution identity or event sequence mismatch")
    value = state.model_dump()
    for name in ("submissions", "orders", "trades", "cancel_errors", "request_errors"):
        value[name] = dict(getattr(state, name))
    fact = event.fact
    if isinstance(fact, Candidate):
        if state.phase is None or fact.phase < state.phase:
            value.update(
                name=fact.name,
                phase=fact.phase,
                source_run_id=fact.source_run_id,
                target_amount=fact.target_amount,
            )
        elif state.name is None and fact.name is not None:
            value["name"] = fact.name
    elif isinstance(fact, Progress):
        if fact.target_quantity and state.owner_plan is None:
            value["owner_plan"] = fact.plan_hash
        if value["owner_plan"] in (None, fact.plan_hash):
            value["progress"] = fact
    elif isinstance(fact, SubmissionFact):
        previous = state.submissions.get(fact.client_order_id)
        if previous is not None:
            if previous.model_copy(update={"status": fact.status}) != fact:
                raise ValueError("submission identity changed")
            # Recovery is evidence of a retained intent, never a new acceptance.
            if fact.status == "RECOVERED":
                fact = previous
            if previous.status not in {"PERSISTING", "DISPATCHING"} and fact.status in {
                "PERSISTING",
                "DISPATCHING",
            }:
                fact = previous
        value["submissions"][fact.client_order_id] = fact
    elif isinstance(fact, OrderFact):
        submission = state.submissions.get(fact.client_order_id)
        if (
            submission is None
            or submission.action != "NEW"
            or (submission.side, submission.quantity, Decimal(submission.price))
            != (fact.side, fact.quantity, Decimal(fact.price))
        ):
            raise ValueError("order lacks matching local submission")
        previous_order = state.orders.get(fact.client_order_id)
        if previous_order is not None:
            if (
                previous_order.broker_order_id
                and previous_order.broker_order_id != fact.broker_order_id
            ):
                raise ValueError("broker order identity changed")
            terminal = {"FILLED", "CANCELLED", "REJECTED"}
            if previous_order.status in terminal:
                if (
                    fact.status in terminal
                    and (fact.status, fact.filled_quantity)
                    != (previous_order.status, previous_order.filled_quantity)
                ) or fact.filled_quantity > previous_order.filled_quantity:
                    raise ValueError("terminal order facts changed")
                fact = previous_order
            if fact.filled_quantity < previous_order.filled_quantity:
                fact = previous_order
        value["orders"][fact.client_order_id] = fact
    elif isinstance(fact, TradeFact):
        submission = state.submissions.get(fact.client_order_id)
        if submission is None or submission.action != "NEW" or submission.side != fact.side:
            raise ValueError("trade lacks matching local submission")
        if (fact.side == "BUY" and Decimal(fact.price) > Decimal(submission.price)) or (
            fact.side == "SELL" and Decimal(fact.price) < Decimal(submission.price)
        ):
            raise ValueError("trade violates the original limit price")
        key = f"{event.occurred_at.date()}:{state.identity.symbol[-2:]}:{fact.broker_trade_id}"
        fill = Fill(
            business_date=event.occurred_at.date(), occurred_at=event.occurred_at, fact=fact
        )
        prior = state.trades.get(key)
        if prior is not None and prior != fill:
            raise ValueError("broker trade identity conflict")
        order = state.orders.get(fact.client_order_id)
        if order is not None and order.broker_order_id not in (None, fact.broker_order_id):
            raise ValueError("trade belongs to another broker order")
        value["trades"][key] = fill
        total = sum(
            f.fact.quantity
            for f in value["trades"].values()
            if f.fact.client_order_id == fact.client_order_id
        )
        if total > submission.quantity:
            raise ValueError("fills exceed submitted quantity")
    elif isinstance(fact, CancelFailure):
        submission = state.submissions.get(fact.client_order_id)
        if submission is None or submission.side != fact.side:
            raise ValueError("cancel failure lacks matching order")
        value["cancel_errors"][fact.client_order_id] = fact
    elif isinstance(fact, RequestFailure):
        submission = state.submissions.get(fact.client_order_id)
        if submission is None or (submission.side, submission.action) != (fact.side, fact.action):
            raise ValueError("request failure lacks matching local intent")
        value["request_errors"][fact.client_order_id] = fact
    value.update(
        last_seq=event.seq,
        created_at=state.created_at or event.occurred_at,
        updated_at=max(state.updated_at or event.occurred_at, event.occurred_at),
    )
    result = State.model_validate(value)
    summary(result)  # Cross-event conservation is validated before a transaction can commit.
    if len(result.submissions) > 2048 or len(result.trades) > 10000:
        raise ValueError("execution fact capacity exceeded")
    return result


def summary(state: State) -> Summary:
    def totals(side: str) -> tuple[int, Decimal, list[datetime]]:
        fills = [v for v in state.trades.values() if v.fact.side == side]
        return (
            sum(v.fact.quantity for v in fills),
            sum((Decimal(v.fact.amount) for v in fills), Decimal(0)),
            [v.occurred_at for v in fills],
        )

    bought, buy_amount, buy_times = totals("BUY")
    sold, sell_amount, sell_times = totals("SELL")
    if sold > bought:
        raise ValueError("reported sells exceed the linked buy fills")

    def status(side: str, quantity: int) -> tuple[str, str | None]:
        pending: list[str] = []
        reason: str | None = None
        unknown = False
        all_cancelled = True
        failed = False
        for key, intent in state.submissions.items():
            if intent.side != side:
                continue
            order = state.orders.get(intent.original_client_order_id or key)
            if intent.action == "CANCEL":
                request_error = state.request_errors.get(key)
                if request_error is not None and (
                    order is None or order.status not in {"FILLED", "CANCELLED", "REJECTED"}
                ):
                    unknown, reason = True, request_error.reason_code
                if intent.status not in {"SDK_ACCEPTED", "RECOVERED"} and (
                    order is None or order.status not in {"FILLED", "CANCELLED", "REJECTED"}
                ):
                    unknown, reason = True, intent.status
                continue
            if order is None:
                request_error = state.request_errors.get(key)
                pending.append("REJECTED" if request_error else intent.status)
                if request_error is not None:
                    failed, reason = True, request_error.reason_code
                    all_cancelled = False
                    continue
                unknown |= intent.status in {
                    "UNCERTAIN",
                    "PERSISTENCE_UNCERTAIN",
                    "RECOVERED",
                    "RECOVERY_REQUIRED",
                }
                failed |= intent.status in {
                    "BEGIN_FAILED",
                    "PERSISTENCE_FAILED",
                    "EVIDENCE_MISMATCH",
                    "NOT_DISPATCHED",
                }
                if failed or unknown:
                    reason = intent.status
                all_cancelled = False
            else:
                request_error = state.request_errors.get(key)
                pending.append(order.status)
                if request_error is not None and order.status in {"ACTIVE", "PARTIAL", "UNKNOWN"}:
                    unknown, reason = True, request_error.reason_code
                unknown |= order.status == "UNKNOWN"
                failed |= order.status == "REJECTED"
                if order.status in {"REJECTED", "UNKNOWN"}:
                    reason = order.reason_code
                all_cancelled &= order.status == "CANCELLED"
                if order.filled_quantity > sum(
                    f.fact.quantity for f in state.trades.values() if f.fact.client_order_id == key
                ):
                    unknown, reason = True, "WAITING_TRADE_DETAILS"
                if key in state.cancel_errors and order.status not in {
                    "FILLED",
                    "CANCELLED",
                    "REJECTED",
                }:
                    unknown, reason = True, state.cancel_errors[key].reason_code
        if unknown:
            return "UNKNOWN", reason or "RECONCILIATION_REQUIRED"
        if any(
            s in {"ACTIVE", "PARTIAL", "PERSISTING", "DISPATCHING", "SDK_ACCEPTED"} for s in pending
        ):
            return ("PARTIAL" if quantity else "ORDERING"), reason
        target = state.progress.target_quantity if state.progress and side == "BUY" else bought
        if quantity:
            return ("FILLED", None) if target and quantity >= target else ("PARTIAL", reason)
        if failed:
            return "FAILED", reason
        if pending and all_cancelled:
            return "CANCELLED", reason
        if (
            side == "BUY"
            and state.progress
            and state.progress.status
            in {
                "MISSED_ENTRY_WINDOW",
                "SKIPPED_MIN_ORDER",
                "BOARD_NOT_ALLOWED",
                "POSITION_CAPACITY",
                "WINDOW_CLOSED",
                "CHILD_LIMIT",
            }
        ):
            return "SKIPPED", state.progress.status
        return ("WAITING" if side == "BUY" else "NOT_STARTED"), None

    buy_status, buy_reason = status("BUY", bought)
    sell_status, sell_reason = status("SELL", sold)

    def active(side: str) -> bool:
        return any(
            s.side == side
            and s.action == "NEW"
            and (
                (
                    s.client_order_id not in state.orders
                    and s.client_order_id not in state.request_errors
                    and s.status in {"PERSISTING", "DISPATCHING", "SDK_ACCEPTED"}
                )
                or (
                    s.client_order_id in state.orders
                    and state.orders[s.client_order_id].status in {"ACTIVE", "PARTIAL"}
                )
            )
            for s in state.submissions.values()
        )

    stage: Literal["WAITING", "BUYING", "NOT_BOUGHT", "HOLDING", "SELLING", "CLOSED", "REVIEW"]
    if "UNKNOWN" in (buy_status, sell_status):
        stage = "REVIEW"
    elif active("BUY"):
        stage = "BUYING"
    elif bought and bought == sold:
        stage = "CLOSED"
    elif active("SELL") and sold < bought:
        stage = "SELLING"
    elif bought:
        stage = "HOLDING"
    elif buy_status in {"FAILED", "SKIPPED", "CANCELLED"}:
        stage = "NOT_BOUGHT"
    elif buy_status == "ORDERING":
        stage = "BUYING"
    else:
        stage = "WAITING"

    def average(amount: Decimal, quantity: int) -> str | None:
        return (
            format((amount / quantity).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP), "f")
            if quantity
            else None
        )

    return Summary(
        execution_id=state.identity.execution_id,
        identity=state.identity,
        name=state.name,
        phase=state.phase,
        source_run_id=state.source_run_id,
        target_amount=state.target_amount,
        target_quantity=state.progress.target_quantity if state.progress else None,
        buy_status=buy_status,
        sell_status=sell_status,
        stage=stage,
        bought_quantity=bought,
        buy_amount=f"{buy_amount:.2f}",
        buy_average_price=average(buy_amount, bought),
        sold_quantity=sold,
        sell_amount=f"{sell_amount:.2f}",
        sell_average_price=average(sell_amount, sold),
        remaining_quantity=bought - sold,
        first_buy_at=min(buy_times) if buy_times else None,
        last_buy_at=max(buy_times) if buy_times else None,
        last_sell_at=max(sell_times) if sell_times else None,
        reason_code=sell_reason or buy_reason,
        version=state.last_seq,
        created_at=state.created_at,
        updated_at=state.updated_at,
    )
