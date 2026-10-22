"""Bounded single-owner order projection; receipts and broker facts stay separate.

Fills may precede order callbacks. A terminal status is only a *candidate* final
state until its cumulative quantity matches uniquely identified trades. Query
replacement is validated on a fresh projection so failure cannot partially
overwrite the last trusted view.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
from threading import get_ident

from auto_trading_system.gateways.broker.models import BrokerOrder, BrokerOrderStatus, BrokerTrade
from auto_trading_system.gateways.broker.query_validation import (
    validate_order_query_result,
    validate_trade_query_result,
)
from auto_trading_system.journal.critical_models import Submission

S = BrokerOrderStatus
_RANK = {
    S.SUBMITTING: 0,
    S.ACKED: 1,
    S.PARTIALLY_FILLED: 2,
    S.CANCEL_PENDING: 3,
    S.PARTIAL_FILL_CANCEL_PENDING: 3,
}


def validate_order(order: BrokerOrder) -> None:
    """Reject impossible status/quantity combinations before monotonic merging."""
    filled, total = order.filled_quantity.value, order.order_quantity.value
    if (
        (
            order.status in (S.SUBMITTING, S.ACKED, S.CANCEL_PENDING, S.CANCELLED, S.REJECTED)
            and filled != 0
        )
        or (
            order.status
            in (S.PARTIALLY_FILLED, S.PARTIAL_FILL_CANCEL_PENDING, S.PARTIALLY_FILLED_CANCELLED)
            and not 0 < filled < total
        )
        or (order.status == S.FILLED and filled != total)
    ):
        raise ValueError("order status and quantity conflict")


def _identity(order: BrokerOrder) -> tuple[object, ...]:
    return (
        order.client_order_id,
        order.symbol,
        order.side,
        order.order_quantity,
        order.limit_price,
    )


def merge_order(prior: BrokerOrder | None, incoming: BrokerOrder) -> BrokerOrder:
    validate_order(incoming)
    if prior is None:
        return incoming
    if _identity(prior) != _identity(incoming) or (
        prior.broker_order_id is not None
        and incoming.broker_order_id is not None
        and prior.broker_order_id != incoming.broker_order_id
    ):
        raise ValueError("broker order identity conflict")
    if prior.is_terminal:
        if (
            incoming.is_terminal
            and (prior.status, prior.filled_quantity) != (incoming.status, incoming.filled_quantity)
        ) or incoming.filled_quantity > prior.filled_quantity:
            raise ValueError("broker terminal facts conflict")
        return prior
    if incoming.is_terminal:
        if incoming.filled_quantity < prior.filled_quantity:
            raise ValueError("terminal order loses confirmed fills")
        return incoming
    if incoming.status in (S.UNKNOWN, S.ERROR_HOLD):
        # Preserve already observed fill totals while surfacing uncertainty.
        return replace(
            prior,
            status=incoming.status,
            status_reason=incoming.status_reason,
            broker_status_code=incoming.broker_status_code,
        )
    if prior.status in (S.UNKNOWN, S.ERROR_HOLD):
        return prior  # Only an authoritative query can clear this uncertainty.
    if incoming.filled_quantity < prior.filled_quantity:
        return prior
    if (
        incoming.filled_quantity == prior.filled_quantity
        and _RANK[incoming.status] < _RANK[prior.status]
    ):
        return prior
    if prior.broker_order_id is not None and incoming.broker_order_id is None:
        return prior
    return incoming


def trade_key(trade: BrokerTrade) -> tuple[str, str, str]:
    """Account is supplied by the enclosing projection; day/market scope the ID."""
    return (trade.traded_at.value.date().isoformat(), trade.symbol.market.value, trade.trade_id)


@dataclass(frozen=True)
class OrderView:
    client_order_id: str
    order: BrokerOrder | None
    traded_quantity: int
    effective_filled_quantity: int
    phase: str
    cancel_request_ids: tuple[str, ...]


class OrderProjection:
    def __init__(self, *, capacity: int = 2048, trade_capacity: int = 10000) -> None:
        if not 1 <= capacity <= 2048 or not 1 <= trade_capacity <= 10000:
            raise ValueError("order projection capacity invalid")
        self._owner = get_ident()
        self._capacity, self._trade_capacity = capacity, trade_capacity
        self._submissions: dict[str, Submission] = {}
        self._cancellations: dict[str, Submission] = {}
        self._orders: dict[str, BrokerOrder] = {}
        self._trades: dict[tuple[str, str, str], BrokerTrade] = {}
        self._totals: dict[str, int] = {}

    def _check(self) -> None:
        if get_ident() != self._owner:
            raise RuntimeError("order projection has a single owner")

    def register(self, submission: Submission) -> None:
        self._check()
        if submission.action_type == "CANCEL":
            if submission.client_order_id in self._submissions:
                raise ValueError("cancel ID conflicts with original order")
            if submission.original_client_order_id not in self._submissions:
                raise ValueError("cancel requires a registered original order")
            prior_cancel = self._cancellations.get(submission.client_order_id)
            if prior_cancel is not None and prior_cancel != submission:
                raise ValueError("cancel request identity conflict")
            if (
                prior_cancel is None
                and len(self._submissions) + len(self._cancellations) >= self._capacity
            ):
                raise ValueError("order projection capacity exhausted")
            self._cancellations[submission.client_order_id] = submission
            return
        key = submission.client_order_id
        prior = self._submissions.get(key)
        if prior is not None and prior != submission:
            raise ValueError("submission identity conflict")
        self._check_capacity(key)
        self._submissions[key] = submission

    def _check_capacity(self, key: str) -> None:
        keys = self._submissions.keys() | self._orders.keys() | self._totals.keys()
        if key not in keys and len(keys) + len(self._cancellations) >= self._capacity:
            raise ValueError("order projection capacity exhausted")

    def observe_order(self, order: BrokerOrder) -> BrokerOrder:
        self._check()
        self._check_capacity(order.client_order_id)
        if order.broker_order_id is not None and any(
            o.client_order_id != order.client_order_id
            and o.broker_order_id == order.broker_order_id
            for o in self._orders.values()
        ):
            raise ValueError("broker order ID belongs to another local order")
        submission = self._submissions.get(order.client_order_id)
        if submission and (
            submission.symbol,
            submission.side,
            submission.quantity,
            Decimal(submission.limit_price),
        ) != (
            str(order.symbol),
            order.side.value,
            order.order_quantity.value,
            order.limit_price.amount,
        ):
            raise ValueError("order differs from local submission")
        merged = merge_order(self._orders.get(order.client_order_id), order)
        total = self._totals.get(order.client_order_id, 0)
        if total > merged.order_quantity.value or (
            merged.is_terminal and total > merged.filled_quantity.value
        ):
            raise ValueError("order contradicts observed trades")
        for trade in self._trades.values():
            if trade.client_order_id == order.client_order_id:
                self._trade_binding(trade, merged, submission)
        self._orders[order.client_order_id] = merged
        return merged

    @staticmethod
    def _trade_binding(
        trade: BrokerTrade, order: BrokerOrder | None, submission: Submission | None
    ) -> None:
        if order and (
            (trade.symbol, trade.side) != (order.symbol, order.side)
            or (
                order.broker_order_id is not None and trade.broker_order_id != order.broker_order_id
            )
        ):
            raise ValueError("trade and order identity conflict")
        if submission and (str(trade.symbol), trade.side.value, trade.traded_at.value.date()) != (
            submission.symbol,
            submission.side,
            submission.trade_date,
        ):
            raise ValueError("trade and submission identity conflict")
        limit = (
            Decimal(submission.limit_price)
            if submission
            else order.limit_price.amount
            if order
            else None
        )
        if limit is not None and (
            (trade.side.value == "BUY" and trade.price.amount > limit)
            or (trade.side.value == "SELL" and trade.price.amount < limit)
        ):
            raise ValueError("trade violates limit price")

    def observe_trade(self, trade: BrokerTrade) -> bool:
        self._check()
        key = trade_key(trade)
        previous = self._trades.get(key)
        if previous is not None:
            if previous != trade:
                raise ValueError("trade identity content conflict")
            return False
        if len(self._trades) >= self._trade_capacity:
            raise ValueError("trade projection capacity exhausted")
        self._check_capacity(trade.client_order_id)
        if trade.notional.amount != (trade.price.amount * trade.quantity.value).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        ):
            raise ValueError("trade amount mismatch")
        order = self._orders.get(trade.client_order_id)
        submission = self._submissions.get(trade.client_order_id)
        self._trade_binding(trade, order, submission)
        total = self._totals.get(trade.client_order_id, 0) + trade.quantity.value
        maximum = (
            submission.quantity if submission else order.order_quantity.value if order else None
        )
        if (maximum is not None and total > maximum) or (
            order and order.is_terminal and total > order.filled_quantity.value
        ):
            raise ValueError("trades exceed confirmed order quantity")
        self._trades[key], self._totals[trade.client_order_id] = trade, total
        return True

    def orders(self) -> tuple[BrokerOrder, ...]:
        self._check()
        return tuple(self._orders.values())

    def trades(self) -> tuple[BrokerTrade, ...]:
        self._check()
        return tuple(self._trades.values())

    def views(self) -> tuple[OrderView, ...]:
        self._check()
        views = []
        for key in sorted(self._submissions.keys() | self._orders.keys() | self._totals.keys()):
            order, total = self._orders.get(key), self._totals.get(key, 0)
            if order is None:
                phase = "WAITING_ORDER"
            elif order.status in (S.UNKNOWN, S.ERROR_HOLD):
                phase = "UNRESOLVED"
            elif order.is_terminal:
                phase = (
                    "TERMINAL_CONFIRMED"
                    if total == order.filled_quantity.value
                    else "WAITING_TRADES"
                )
            else:
                phase = "ACTIVE"
            views.append(
                OrderView(
                    key,
                    order,
                    total,
                    max(total, order.filled_quantity.value if order else 0),
                    phase,
                    tuple(
                        sorted(
                            cancel.client_order_id
                            for cancel in self._cancellations.values()
                            if cancel.original_client_order_id == key
                        )
                    ),
                )
            )
        return tuple(views)

    def checked_query(
        self,
        submissions: tuple[Submission, ...],
        orders: tuple[BrokerOrder, ...],
        trades: tuple[BrokerTrade, ...],
    ) -> OrderProjection:
        """Authoritative complete query, including active orders; old facts cannot disappear."""
        self._check()
        validate_order_query_result(orders)
        validate_trade_query_result(trades)
        candidate = OrderProjection(capacity=self._capacity, trade_capacity=self._trade_capacity)
        for item in submissions:
            candidate.register(item)
        if {o.client_order_id for o in orders} != candidate._submissions.keys():
            raise ValueError("query contains missing or unowned orders")
        if not self._orders.keys() <= {o.client_order_id for o in orders}:
            raise ValueError("query loses a previously observed order")
        for order in orders:
            old = self._orders.get(order.client_order_id)
            if old and (
                _identity(old) != _identity(order)
                or old.filled_quantity > order.filled_quantity
                or (
                    old.broker_order_id is not None and old.broker_order_id != order.broker_order_id
                )
            ):
                raise ValueError("query regresses order identity or confirmed fills")
            if old and old.status not in (S.UNKNOWN, S.ERROR_HOLD):
                merged = merge_order(old, order)
                if (merged.status, merged.filled_quantity, merged.broker_order_id) != (
                    order.status,
                    order.filled_quantity,
                    order.broker_order_id,
                ):
                    raise ValueError("query regresses previously observed order")
            candidate.observe_order(order)
        for trade in trades:
            if trade.client_order_id not in candidate._orders:
                raise ValueError("query contains unowned trade")
            candidate.observe_trade(trade)
        if any(candidate._trades.get(key) != value for key, value in self._trades.items()):
            raise ValueError("query loses or changes an observed trade")
        for view in candidate.views():
            assert view.order
            if (
                view.phase == "UNRESOLVED"
                or view.order.broker_order_id is None
                or view.traded_quantity != view.order.filled_quantity.value
            ):
                raise ValueError("query order and trade totals are not reconciled")
        return candidate
