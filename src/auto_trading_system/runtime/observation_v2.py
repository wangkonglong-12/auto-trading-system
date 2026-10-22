"""Validate a coherent live query without granting or restoring trading authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from auto_trading_system.domain.trading_time import SHANGHAI_TIMEZONE
from auto_trading_system.journal.critical_models import Submission
from auto_trading_system.orders.projection import OrderProjection
from auto_trading_system.reconcile.position_lots import (
    ExchangeCalendar,
    LotAllocation,
    LotIntent,
    PortfolioProjection,
    PositionLot,
    assert_position_match,
    project_lots,
)
from auto_trading_system.runtime.recovery_v2 import BrokerRecoverySnapshot


@dataclass(frozen=True)
class CheckedObservation:
    snapshot: BrokerRecoverySnapshot
    orders: OrderProjection
    portfolio: PortfolioProjection
    terminal_ids: frozenset[str]


def check_empty_observation(
    *,
    snapshot: BrokerRecoverySnapshot,
    previous: OrderProjection,
    account_id: str,
    trade_date: date,
    now: datetime,
    event_version: int,
    allow_unmanaged: bool = False,
) -> CheckedObservation:
    """Validate a genuinely empty account before today's plan is available.

    The engine also requires empty J1/opening sets. No strategy identity is
    fabricated and no old facts are discarded; any nonempty observation still
    requires an explicit execution context and its regular lot projection.
    """
    account = snapshot.account
    if (
        not snapshot.stable
        or not account.coherent
        or snapshot.event_version != event_version
        or account.account_id != account_id
        or account.observed_at.astimezone(SHANGHAI_TIMEZONE).date() != trade_date
        or not 0 <= (now - account.observed_at).total_seconds() <= 5
        or any(
            (
                snapshot.orders,
                snapshot.trades,
                account.positions if not allow_unmanaged else (),
                account.observed_order_ids,
                previous.orders(),
                previous.trades(),
            )
        )
    ):
        raise ValueError("unassigned account must be fresh, coherent and empty")
    return CheckedObservation(
        snapshot,
        previous.checked_query((), (), ()),
        PortfolioProjection(
            (),
            frozenset(position.symbol for position in account.positions),
            (),
            (),
            tuple(sorted((position.symbol, position.quantity) for position in account.positions)),
        ),
        frozenset(),
    )


def check_observation(
    *,
    snapshot: BrokerRecoverySnapshot,
    previous: OrderProjection,
    submissions: tuple[Submission, ...],
    account_id: str,
    trade_date: date,
    strategy_id: str,
    plan_hash: str,
    calendar: ExchangeCalendar,
    now: datetime,
    event_version: int,
    opening: tuple[PositionLot, ...] = (),
    opening_hash: str | None = None,
    context_type: Literal["ENTRY_PLAN", "EXIT_SESSION"] = "ENTRY_PLAN",
    accepted_scopes: frozenset[tuple[str, str]] | None = None,
    allow_unmanaged: bool = False,
) -> CheckedObservation:
    """Project today's immutable J1 allocations over a verified opening checkpoint."""
    account = snapshot.account
    if (
        not snapshot.stable
        or not account.coherent
        or snapshot.event_version != event_version
        or account.account_id != account_id
        or account.observed_at.astimezone(SHANGHAI_TIMEZONE).date() != trade_date
        or not 0 <= (now - account.observed_at).total_seconds() <= 5
    ):
        raise ValueError("account observation is stale, crossed or out of scope")
    if set(account.observed_order_ids) != {o.client_order_id for o in snapshot.orders}:
        raise ValueError("account and order query windows differ")
    if any(
        o.updated_at.value.date() != trade_date or o.updated_at.value > now for o in snapshot.orders
    ):
        raise ValueError("order observation timestamp is out of scope")
    for item in submissions:
        if (
            item.account_id != account_id
            or item.trade_date != trade_date
            or (
                (item.plan_hash, item.context_type)
                not in (accepted_scopes or frozenset({(plan_hash, context_type)}))
            )
        ):
            raise ValueError("submission scope requires explicit historical ownership")
    orders = previous.checked_query(submissions, snapshot.orders, snapshot.trades)
    terminal = frozenset(
        v.client_order_id for v in orders.views() if v.phase == "TERMINAL_CONFIRMED"
    )
    intents: list[LotIntent] = []
    for item in submissions:
        if item.side == "SELL":
            binding = item.sell_binding
            if (
                binding is None
                or binding.opening_hash != opening_hash
                or binding.strategy_id != strategy_id
            ):
                raise ValueError("SELL requires matching durable lot ownership")
            if item.action_type == "NEW":
                intents.append(
                    LotIntent(
                        item,
                        strategy_id,
                        tuple(
                            LotAllocation(lot_id=a.lot_id, quantity=a.quantity)
                            for a in binding.allocations
                        ),
                    )
                )
        elif item.action_type == "NEW":
            intents.append(LotIntent(item, strategy_id))
    portfolio = project_lots(
        account_id=account_id,
        as_of=trade_date,
        calendar=calendar,
        intents=tuple(intents),
        trades=snapshot.trades,
        terminal_order_ids=terminal,
        opening=opening,
    )
    if allow_unmanaged:
        managed = portfolio.quantities()
        observed_positions = {p.symbol: p.quantity for p in account.positions}
        if any(
            observed_positions.get(symbol, 0) < quantity for symbol, quantity in managed.items()
        ):
            raise ValueError("broker holding is below strategy owned quantity")
        residual = tuple(
            sorted(
                (symbol, quantity - managed.get(symbol, 0))
                for symbol, quantity in observed_positions.items()
                if quantity - managed.get(symbol, 0) > 0
            )
        )
        if residual:
            portfolio = project_lots(
                account_id=account_id,
                as_of=trade_date,
                calendar=calendar,
                intents=tuple(intents),
                trades=snapshot.trades,
                terminal_order_ids=terminal,
                opening=opening,
                unmanaged_positions=residual,
            )
    assert_position_match(portfolio, {p.symbol: p.quantity for p in account.positions})
    return CheckedObservation(snapshot, orders, portfolio, terminal)
