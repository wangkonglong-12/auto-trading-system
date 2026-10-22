"""Kind-aware dispatcher that keeps transport concerns outside the Outbox worker."""

from __future__ import annotations

from collections.abc import Mapping

from auto_trading_system.journal import OutboxItem, OutboxKind
from auto_trading_system.sync.models import OutboxDeliveryError, OutboxDeliveryReceipt
from auto_trading_system.sync.outbox_worker import OutboxDispatcher


class RoutedOutboxDispatcher:
    """Route homogeneous item groups while preserving at-least-once batch semantics."""

    def __init__(self, handlers: Mapping[OutboxKind, OutboxDispatcher]) -> None:
        if not isinstance(handlers, Mapping) or not handlers:
            raise ValueError("handlers must be a non-empty mapping")
        validated: dict[OutboxKind, OutboxDispatcher] = {}
        for kind, handler in handlers.items():
            if not isinstance(kind, OutboxKind):
                raise ValueError("handler keys must be OutboxKind values")
            if not isinstance(handler, OutboxDispatcher):
                raise ValueError("handlers must implement OutboxDispatcher")
            validated[kind] = handler
        self._handlers = validated

    async def deliver(self, items: tuple[OutboxItem, ...]) -> tuple[OutboxDeliveryReceipt, ...]:
        if not isinstance(items, tuple) or not items:
            raise OutboxDeliveryError(
                "OUTBOX_BATCH_INVALID",
                "outbox delivery batch must be a non-empty tuple",
                retryable=False,
            )
        groups: dict[OutboxKind, list[OutboxItem]] = {}
        for item in items:
            if not isinstance(item, OutboxItem):
                raise OutboxDeliveryError(
                    "OUTBOX_BATCH_INVALID",
                    "outbox batch contains an invalid item",
                    retryable=False,
                )
            groups.setdefault(item.kind, []).append(item)

        receipts: list[OutboxDeliveryReceipt] = []
        for kind, group in groups.items():
            handler = self._handlers.get(kind)
            if handler is None:
                raise OutboxDeliveryError(
                    "OUTBOX_KIND_UNSUPPORTED",
                    "no delivery handler is registered for an outbox kind",
                    retryable=False,
                )
            receipts.extend(await handler.deliver(tuple(group)))
        return tuple(receipts)
