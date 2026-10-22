"""Pure bounded sizing for a single protected limit-buy child order.

Rules and limits are supplied by a verified plan/instrument rulebook; the
function does not infer board rules from a stock-code prefix or rebalance cash.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from auto_trading_system.domain import Money, Price


@dataclass(frozen=True, slots=True)
class BuyRule:
    min_quantity: int
    quantity_step: int
    max_order_quantity: int
    price_tick: Price

    def __post_init__(self) -> None:
        if (
            self.min_quantity < 1
            or self.quantity_step < 1
            or self.max_order_quantity < self.min_quantity
        ):
            raise ValueError("invalid verified buy rule")


@dataclass(frozen=True, slots=True)
class EntryCapacity:
    cash_limit: Money
    fee_reserve: Money
    remaining_target: int
    recent_volume_delta: int
    visible_ask_quantity: int
    path_complete: bool


@dataclass(frozen=True, slots=True)
class EntrySize:
    quantity: int
    price: Price | None
    reason: str


def size_entry(
    ask: Price,
    price_ceiling: Price,
    rule: BuyRule,
    capacity: EntryCapacity,
    *,
    participation_bps: int = 300,
    depth_bps: int = 1000,
) -> EntrySize:
    if not 0 < participation_bps <= 10000 or not 0 < depth_bps <= 10000:
        raise ValueError("capacity ratios must be in (0, 10000] basis points")
    if not capacity.path_complete or capacity.recent_volume_delta < 0:
        return EntrySize(0, None, "MARKET_PATH_UNCERTAIN")
    if capacity.remaining_target < 0 or capacity.visible_ask_quantity < 0:
        raise ValueError("quantity bounds must be nonnegative")
    limit = Price(
        (ask.amount / rule.price_tick.amount).to_integral_value(rounding=ROUND_CEILING)
        * rule.price_tick.amount
    )
    if limit > price_ceiling:
        return EntrySize(0, None, "PRICE_CEILING")
    usable = max(Decimal(0), capacity.cash_limit.amount - capacity.fee_reserve.amount)
    raw = min(
        int(usable / limit.amount),
        capacity.remaining_target,
        rule.max_order_quantity,
        capacity.recent_volume_delta * participation_bps // 10000,
        capacity.visible_ask_quantity * depth_bps // 10000,
    )
    if raw < rule.min_quantity:
        return EntrySize(0, None, "SKIPPED_MIN_ORDER")
    quantity = (
        rule.min_quantity + ((raw - rule.min_quantity) // rule.quantity_step) * rule.quantity_step
    )
    return EntrySize(quantity, limit, "ELIGIBLE")
