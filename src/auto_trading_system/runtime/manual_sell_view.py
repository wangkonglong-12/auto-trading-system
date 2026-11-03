"""Read-only stock sale rows. A buy-history snapshot is never sell authority."""

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal

from pydantic import JsonValue

from auto_trading_system.reconcile.position_lots import PortfolioProjection
from auto_trading_system.runtime.recovery_v2 import BrokerRecoverySnapshot


def sell_positions(
    portfolio: PortfolioProjection,
    snapshot: BrokerRecoverySnapshot,
    *,
    now: datetime,
    ready: bool,
    locks: set[str],
    owned_sell_ids: frozenset[str],
) -> list[JsonValue]:
    names = dict(snapshot.display_names)
    symbols = dict.fromkeys(lot.symbol for lot in portfolio.lots)
    views: list[JsonValue] = []
    for symbol in symbols:
        lots = [owned_lot for owned_lot in portfolio.lots if owned_lot.symbol == symbol]
        remaining = sum(owned_lot.remaining for owned_lot in lots)
        trades = [
            t
            for t in snapshot.trades
            if str(t.symbol) == symbol
            and t.side.value == "SELL"
            and t.client_order_id in owned_sell_ids
        ]
        sold = sum(t.quantity.value for t in trades)
        if not remaining and not sold:
            continue
        broker_available = next(
            (p.sellable for p in snapshot.account.positions if p.symbol == symbol), 0
        )
        available = portfolio.sellable(
            symbol=symbol,
            strategy_id=lots[0].strategy_id,
            as_of=now.date(),
            broker_sellable=broker_available,
            reflected_order_ids=frozenset(o.client_order_id for o in snapshot.orders),
        )
        conflict = any(str(o.symbol) == symbol and not o.is_terminal for o in snapshot.orders)
        eligible = sum(
            owned_lot.remaining for owned_lot in lots if owned_lot.eligible_sell_date <= now.date()
        )
        reason = (
            "NO_POSITION"
            if not remaining
            else "T_PLUS_ONE"
            if not eligible
            else "RESULT_UNKNOWN"
            if symbol in portfolio.locked_symbols
            else "ORDER_CONFLICT"
            if conflict
            else "BUSY"
            if symbol in locks
            else "NO_SELLABLE"
            if available <= 0
            else "OUTSIDE_WINDOW"
            if not (
                time(9, 30) <= now.time() < time(11, 30) or time(13) <= now.time() < time(14, 55)
            )
            else "NOT_READY"
            if not ready
            else None
        )
        amount = sum((t.notional.amount for t in trades), Decimal(0))
        views.append(
            {
                "symbol": symbol,
                "name": names.get(symbol),
                "remaining": remaining,
                "sellable": available,
                "reserved": sum(owned_lot.reserved_quantity for owned_lot in lots),
                "sold_today": sold,
                "sell_amount": format(amount, ".2f"),
                "average_sell_price": format(amount / sold, ".4f") if sold else None,
                "eligible_sell_date": min(
                    (owned_lot.eligible_sell_date for owned_lot in lots if owned_lot.remaining),
                    default=now.date(),
                ).isoformat(),
                "can_sell": reason is None,
                "disabled_reason": reason,
                "state": "SOLD"
                if not remaining and sold
                else "SELLING"
                if conflict or symbol in locks
                else "PARTIAL_SOLD"
                if sold
                else "HOLDING",
            }
        )
    return views[:100]
