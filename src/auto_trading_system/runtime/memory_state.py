"""Bounded owner-thread market state and conservative resource reservations.

No database, filesystem, broker or HTTP dependency is reachable from this module.
Snapshots are immutable. Missing observations never turn into zero balances.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from threading import get_ident

from auto_trading_system.domain import Money, Price
from auto_trading_system.journal.critical_models import Submission


@dataclass(frozen=True, slots=True)
class QuotePoint:
    sequence: int
    received_ns: int
    price: Price


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    state_version: int
    points: tuple[QuotePoint, ...]
    path_complete: bool


class MarketWindow:
    def __init__(self, *, capacity: int = 32768, horizon_ns: int = 60_000_000_000) -> None:
        if capacity < 1 or horizon_ns < 1:
            raise ValueError("window bounds must be positive")
        self._owner = get_ident()
        self._capacity = capacity
        self._horizon_ns = horizon_ns
        self._points: deque[QuotePoint] = deque()
        self._complete = True
        self._version = 0

    def add(self, point: QuotePoint) -> None:
        if get_ident() != self._owner:
            raise RuntimeError("market state has a single owner")
        if (
            type(point.sequence) is not int
            or type(point.received_ns) is not int
            or point.sequence <= 0
            or point.received_ns < 0
        ):
            raise ValueError("quote sequence/time invalid")
        if self._points:
            last = self._points[-1]
            if point.sequence <= last.sequence or point.received_ns < last.received_ns:
                self._complete = False
                self._version += 1
                return
            if point.sequence != last.sequence + 1:
                self._complete = False
        while self._points and point.received_ns - self._points[0].received_ns > self._horizon_ns:
            self._points.popleft()
        if len(self._points) == self._capacity:
            self._points.popleft()
            self._complete = False
        self._points.append(point)
        self._version += 1

    def snapshot(self) -> MarketSnapshot:
        if get_ident() != self._owner:
            raise RuntimeError("market state has a single owner")
        return MarketSnapshot(self._version, tuple(self._points), self._complete)


@dataclass(frozen=True, slots=True)
class AccountObservation:
    snapshot_id: str
    cash: Money
    observed_order_ids: frozenset[str]
    sellable: tuple[tuple[str, int], ...]
    coherent: bool


class ReservationBook:
    def __init__(self, *, capacity: int = 2048) -> None:
        if capacity < 1:
            raise ValueError("reservation capacity must be positive")
        self._owner = get_ident()
        self._capacity = capacity
        self._reservations: dict[str, Submission] = {}

    def _check_owner(self) -> None:
        if get_ident() != self._owner:
            raise RuntimeError("reservations have a single owner")

    def available_cash(self, observation: AccountObservation) -> Money:
        self._check_owner()
        if not observation.coherent:
            raise ValueError("account observation is not coherent")
        unreflected = sum(
            (
                Money.parse(item.maximum_amount).amount
                for key, item in self._reservations.items()
                if item.side == "BUY" and key not in observation.observed_order_ids
            ),
            Decimal(0),
        )
        return Money(max(Decimal(0), observation.cash.amount - unreflected))

    def reserve(self, submission: Submission, observation: AccountObservation) -> None:
        self._check_owner()
        if submission.action_type != "NEW":
            raise ValueError("a cancellation retains its original reservation")
        if submission.client_order_id in self._reservations:
            raise ValueError("reservation already exists")
        if len(self._reservations) >= self._capacity:
            raise ValueError("reservation capacity exhausted")
        if any(item.symbol == submission.symbol for item in self._reservations.values()):
            raise ValueError("symbol has an active or uncertain order")
        if not observation.coherent:
            raise ValueError("account observation is not coherent")
        if submission.side == "BUY":
            if Money.parse(submission.maximum_amount) > self.available_cash(observation):
                raise ValueError("insufficient unreserved cash")
        else:
            # Broker sellable already accounts for reflected freezes. Unreflected
            # reservations are deducted separately; a D0 lot also needs the policy gate.
            sellable = dict(observation.sellable).get(submission.symbol)
            if sellable is None or sellable < submission.quantity:
                raise ValueError("insufficient confirmed sellable quantity")
        self._reservations[submission.client_order_id] = submission

    def resolve_terminal(self, client_order_id: str, *, reconciled: bool) -> None:
        self._check_owner()
        if not reconciled:
            raise ValueError("terminal broker evidence and account reconciliation required")
        self._reservations.pop(client_order_id, None)

    def snapshot(self) -> tuple[Submission, ...]:
        self._check_owner()
        return tuple(self._reservations.values())
