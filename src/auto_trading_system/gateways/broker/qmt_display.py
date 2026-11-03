"""Validated display facts. This path never supplies recovery or order permission."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from math import isfinite

from pydantic import JsonValue

from auto_trading_system.domain import SecurityCode
from auto_trading_system.gateways.broker.cicc_qmt_facts import _field, _money
from auto_trading_system.monitoring.qmt_readonly import QmtReadOnlyAdapterError

ORDER_STATES = {
    48: "SUBMITTING",
    49: "SUBMITTING",
    50: "ACKED",
    51: "CANCEL_PENDING",
    52: "PARTIAL_FILL_CANCEL_PENDING",
    53: "PARTIALLY_FILLED_CANCELLED",
    54: "CANCELLED",
    55: "PARTIALLY_FILLED",
    56: "FILLED",
    57: "REJECTED",
    255: "UNKNOWN",
}


def _price(row: object, field: str) -> str:
    value: float = _field(row, field, float)
    if not isfinite(value) or value < 0 or value >= 1_000_000:
        raise QmtReadOnlyAdapterError("QMT_DISPLAY_PRICE_INVALID", "invalid broker price")
    return format(Decimal(str(value)), "f")


def _timestamp(row: object, field: str, now: datetime) -> str:
    raw: int = _field(row, field, int)
    try:
        value = datetime.fromtimestamp(raw, tz=now.tzinfo)
    except (ValueError, OSError, OverflowError) as exc:
        raise QmtReadOnlyAdapterError("QMT_DISPLAY_TIME_INVALID", "invalid broker time") from exc
    if value.date() != now.date():
        raise QmtReadOnlyAdapterError("QMT_DISPLAY_TIME_INVALID", "wrong trade date")
    return value.isoformat()


def display_order(row: object, now: datetime, *, buy: int, sell: int) -> dict[str, JsonValue]:
    code: str = _field(row, "stock_code", str)
    SecurityCode.parse(code)
    order_id: int = _field(row, "order_id", int)
    quantity: int = _field(row, "order_volume", int)
    filled: int = _field(row, "traded_volume", int)
    state: int = _field(row, "order_status", int)
    side: int = _field(row, "order_type", int)
    if (
        order_id <= 0
        or quantity <= 0
        or not 0 <= filled <= quantity
        or state not in ORDER_STATES
        or side not in (buy, sell)
    ):
        raise QmtReadOnlyAdapterError("QMT_DISPLAY_ORDER_INVALID", "invalid broker order")
    return {
        "client_order_id": f"ord_external_{order_id}",
        "broker_order_id": str(order_id),
        "symbol": code,
        "side": "BUY" if side == buy else "SELL",
        "status": ORDER_STATES[state],
        "quantity": quantity,
        "filled": filled,
        "remaining": quantity - filled,
        "limit_price": _price(row, "price"),
        "ordered_at": _timestamp(row, "order_time", now),
        "owned_by_system": False,
        "cancelable": False,
        "reason": "",
    }


def display_trade(row: object, now: datetime, *, buy: int, sell: int) -> dict[str, JsonValue]:
    code: str = _field(row, "stock_code", str)
    SecurityCode.parse(code)
    trade_id: str = _field(row, "traded_id", str)
    order_id: int = _field(row, "order_id", int)
    quantity: int = _field(row, "traded_volume", int)
    side: int = _field(row, "order_type", int)
    price = _price(row, "traded_price")
    amount = _money(row, "traded_amount")
    if (
        not trade_id
        or order_id <= 0
        or quantity <= 0
        or side not in (buy, sell)
        or Decimal(price) <= 0
        or Decimal(amount) <= 0
    ):
        raise QmtReadOnlyAdapterError("QMT_DISPLAY_TRADE_INVALID", "invalid broker trade")
    return {
        "trade_id": trade_id,
        "client_order_id": f"ord_external_{order_id}",
        "symbol": code,
        "side": "BUY" if side == buy else "SELL",
        "quantity": quantity,
        "price": price,
        "amount": amount,
        "traded_at": _timestamp(row, "traded_time", now),
        "owned_by_system": False,
    }
