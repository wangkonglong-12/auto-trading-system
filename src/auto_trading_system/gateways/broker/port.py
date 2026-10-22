"""Broker gateway port owned by the trading application."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from auto_trading_system.gateways.broker.models import (
    BrokerEvent,
    BrokerOrder,
    BrokerTrade,
    CancellationReceipt,
    CancelOrder,
    SubmissionReceipt,
    SubmitOrder,
)

BrokerEventSink = Callable[[BrokerEvent], None]


class BrokerGateway(Protocol):
    """Broker-neutral command, callback, and recovery-query boundary."""

    def start(self, event_sink: BrokerEventSink) -> None: ...

    def stop(self) -> None: ...

    def submit_order(self, command: SubmitOrder) -> SubmissionReceipt: ...

    def cancel_order(self, command: CancelOrder) -> CancellationReceipt: ...

    def query_orders(self) -> tuple[BrokerOrder, ...]: ...

    def query_trades(self) -> tuple[BrokerTrade, ...]: ...

    def find_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None: ...
