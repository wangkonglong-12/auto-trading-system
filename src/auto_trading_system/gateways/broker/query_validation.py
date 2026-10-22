"""Fail-closed validation for authoritative broker recovery queries."""

from __future__ import annotations

from typing import cast

from auto_trading_system.gateways.broker.errors import BrokerQueryError
from auto_trading_system.gateways.broker.models import BrokerOrder, BrokerTrade


def validate_order_query_result(value: object) -> tuple[BrokerOrder, ...]:
    """Accept an immutable, identity-consistent order snapshot or fail closed."""

    if not isinstance(value, tuple) or not all(isinstance(item, BrokerOrder) for item in value):
        raise BrokerQueryError("broker order query did not return authoritative typed facts")
    orders = cast(tuple[BrokerOrder, ...], value)
    client_ids = {item.client_order_id for item in orders}
    broker_ids = {item.broker_order_id for item in orders if item.broker_order_id is not None}
    non_null_broker_ids = sum(item.broker_order_id is not None for item in orders)
    if len(client_ids) != len(orders) or len(broker_ids) != non_null_broker_ids:
        raise BrokerQueryError("broker order query returned duplicate order identities")
    return orders


def validate_trade_query_result(value: object) -> tuple[BrokerTrade, ...]:
    """Accept an immutable, identity-consistent trade snapshot or fail closed."""

    if not isinstance(value, tuple) or not all(isinstance(item, BrokerTrade) for item in value):
        raise BrokerQueryError("broker trade query did not return authoritative typed facts")
    trades = cast(tuple[BrokerTrade, ...], value)
    if len({item.trade_id for item in trades}) != len(trades):
        raise BrokerQueryError("broker trade query returned duplicate trade identities")
    return trades
