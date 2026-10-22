"""Conservative BUY exposure: recycle only reconciled, state-bound terminal shares.

Keep the entire estimated fee for each attempt, including rejected children.
Final fees remain a separate accounting concern; price improvement is not reused.
Legacy submissions retain their original attempted-quantity ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from auto_trading_system.gateways.broker.models import BrokerOrder, BrokerTrade
from auto_trading_system.journal.critical_models import Submission


@dataclass(frozen=True)
class BuyExposure:
    client_order_id: str
    symbol: str
    quantity: int
    amount: Decimal


def buy_exposures(
    submissions: tuple[Submission, ...],
    orders: tuple[BrokerOrder, ...],
    trades: tuple[BrokerTrade, ...],
    held_ids: frozenset[str],
    *,
    state_guard_installed: bool,
) -> tuple[BuyExposure, ...]:
    """Inputs must come from the engine's checked account observation, not callbacks."""
    by_id = {o.client_order_id: o for o in orders}
    totals: dict[str, int] = {}
    for trade in trades:
        totals[trade.client_order_id] = totals.get(trade.client_order_id, 0) + trade.quantity.value
    result = []
    for item in submissions:
        if item.action_type != "NEW" or item.side != "BUY":
            continue
        quantity, amount = item.quantity, Decimal(item.maximum_amount)
        order = by_id.get(item.client_order_id)
        if (
            state_guard_installed
            and item.entry_binding is not None
            and item.entry_binding.state_hash is not None
            and item.client_order_id not in held_ids
            and order is not None
            and order.is_terminal
            and totals.get(item.client_order_id, 0) == order.filled_quantity.value
        ):
            quantity = order.filled_quantity.value
            amount -= Decimal(item.limit_price) * (item.quantity - quantity)
        result.append(BuyExposure(item.client_order_id, item.symbol, quantity, amount))
    return tuple(result)
