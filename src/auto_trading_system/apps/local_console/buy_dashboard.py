"""Pure display projection. Broker fills, not signal/submit states, prove a buy."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal

from pydantic import JsonValue


def _rows(value: object) -> list[dict[str, JsonValue]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def transaction_rows(value: object) -> list[JsonValue]:
    """Exclude pure candidates, including legacy zero-target display snapshots."""
    result: list[JsonValue] = []
    for row in _rows(value):
        proof = row.get("has_trade_activity")
        quantity = row.get("quantity")
        if proof is True:
            result.append(row)
        elif proof is None and (
            row.get("source") == "MANUAL"
            or (isinstance(quantity, int) and quantity > 0)
            or row.get("state")
            in {
                "FILLED",
                "PARTIAL",
                "PENDING",
                "UNKNOWN",
                "CANCELLED",
                "REJECTED",
                "PARTIALLY_FILLED_CANCELLED",
            }
        ):
            # Old snapshots have no explicit marker; these states could only be
            # projected from submissions/orders/fills, never from a plan alone.
            result.append(row)
    return result


def buy_dashboard(
    details: Mapping[str, JsonValue], engine: Mapping[str, JsonValue], *, maximum: int | None
) -> dict[str, JsonValue]:
    """System-owned automatic and confirmed manual buys share daily slots.

    External account orders never establish ownership. Partial fills occupy a
    slot; an unavailable/truncated query is never evidence of zero executions.
    """
    plan = engine.get("plan")
    market = engine.get("market_session")
    plan_symbols = plan.get("symbols", []) if isinstance(plan, dict) else []
    if not isinstance(plan_symbols, list):
        plan_symbols = []
    targets = {
        str(row["symbol"]): row
        for row in _rows(market.get("entry_targets") if isinstance(market, dict) else None)
    }
    progress = {
        str(row["symbol"]): row.get("state")
        for row in _rows(market.get("entry_progress") if isinstance(market, dict) else None)
    }
    submissions = _rows(details.get("submissions"))
    orders = [
        row
        for row in _rows(details.get("orders"))
        if row.get("side") == "BUY" and row.get("owned_by_system") is True
    ]
    trades = [
        row
        for row in _rows(details.get("trades"))
        if row.get("side") == "BUY" and row.get("owned_by_system") is True
    ]
    symbols = dict.fromkeys(
        [
            *(str(s) for s in plan_symbols if isinstance(s, str)),
            *targets,
            *(str(row["symbol"]) for row in orders),
            *(str(row["symbol"]) for row in trades),
            *(str(row["symbol"]) for row in submissions),
        ]
    )
    names = details.get("names")
    current = details.get("available") is True and details.get("truncated") is not True
    output: list[JsonValue] = []
    completed = partial = pending = unbought = 0
    terminal = {"FILLED", "CANCELLED", "PARTIALLY_FILLED_CANCELLED", "REJECTED"}
    for symbol in symbols:
        fills = [row for row in trades if row["symbol"] == symbol]
        stock_orders = [row for row in orders if row["symbol"] == symbol]
        attempts = [row for row in submissions if row["symbol"] == symbol]
        manual = [row for row in attempts if row.get("source") == "MANUAL"]
        quantity = sum(int(str(row["quantity"])) for row in fills)
        amount = sum(
            (
                Decimal(
                    str(row.get("amount") or Decimal(str(row["price"])) * int(str(row["quantity"])))
                )
                for row in fills
            ),
            Decimal(0),
        )
        target = targets.get(symbol, {}).get("target_quantity")
        if manual:
            by_id = {row["client_order_id"]: row for row in stock_orders}
            target = sum(
                int(str(row["quantity"]))
                for row in manual
                if not (
                    by_id.get(row["client_order_id"], {}).get("status") in terminal
                    and by_id[row["client_order_id"]].get("filled") == 0
                )
            )
        active = any(row["status"] not in terminal for row in stock_orders)
        unobserved = [
            row
            for row in attempts
            if not any(order["client_order_id"] == row["client_order_id"] for order in stock_orders)
        ]
        if not current:
            state = "UNAVAILABLE"
        elif quantity > 0 and isinstance(target, int) and target > 0 and quantity >= target:
            state = "FILLED"
            completed += 1
        elif quantity > 0:
            state = (
                "PARTIALLY_FILLED_CANCELLED"
                if manual and not active and not unobserved
                else "PARTIAL"
            )
            partial += 1
        elif active or unobserved:
            uncertain = any(
                row.get("status")
                in {
                    "UNCERTAIN",
                    "PERSISTENCE_UNCERTAIN",
                    "PERSISTENCE_FAILED",
                    "NOT_DISPATCHED",
                    "RECOVERED",
                    "RECOVERY_REQUIRED",
                    "BEGIN_FAILED",
                    "EVIDENCE_MISMATCH",
                }
                for row in unobserved
            )
            state = "UNKNOWN" if uncertain else "PENDING"
            pending += 1
        elif stock_orders and (manual or stock_orders[-1]["status"] in {"REJECTED", "CANCELLED"}):
            state = "REJECTED" if stock_orders[-1]["status"] == "REJECTED" else "CANCELLED"
            unbought += 1
        else:
            state = "NOT_BOUGHT"
            unbought += 1
        output.append(
            {
                "symbol": symbol,
                "has_trade_activity": bool(attempts or stock_orders or fills),
                "name": (names.get(symbol) if isinstance(names, dict) else None)
                or next((row.get("name") for row in manual if row.get("name")), None),
                "source": "MANUAL"
                if manual
                else "DEBUG"
                if isinstance(plan, dict)
                and plan.get("source") == "DEBUG"
                and symbol in plan_symbols
                else "AUTO",
                "trade_date": details.get("trade_date")
                or (plan.get("trade_date") if isinstance(plan, dict) else None),
                "sell_state": "RULES_NOT_CONFIGURED" if quantity else None,
                "submitted_at": next((row.get("submitted_at") for row in manual), None),
                "state": state,
                "signal_state": progress.get(symbol),
                "quantity": quantity if current else None,
                "bought_at": min((str(row["traded_at"]) for row in fills), default=None)
                if current
                else None,
                "average_price": format(
                    (amount / quantity).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP), "f"
                )
                if quantity and current
                else None,
                "amount": format(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")
                if quantity and current
                else None,
            }
        )
    occupied = completed + partial + pending
    return {
        "available": current,
        "observed_at": details.get("observed_at"),
        "rows": output,
        "completed": completed if current else None,
        "partial": partial if current else None,
        "pending": pending if current else None,
        "not_bought": unbought if current else None,
        "occupied": occupied if current else None,
        "remaining": max(0, maximum - occupied) if current and maximum is not None else None,
        "maximum": maximum,
    }
