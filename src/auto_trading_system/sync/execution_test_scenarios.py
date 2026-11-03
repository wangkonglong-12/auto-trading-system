"""Explicit synthetic executions for real historical picks; never submit an order."""

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal

from pydantic import JsonValue

from auto_trading_system.protocol.executions.models import Event, Identity
from auto_trading_system.protocol.tail_picks import CN, TailPicksPreview
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest


def scenario(
    feed: TailPicksPreview,
    *,
    node_account: str,
    strategy: str,
    maximum: int,
    amount: str,
    now: datetime,
) -> tuple[Event, ...]:
    if feed.trade_date is None or feed.status != "FOUND":
        raise ValueError("real picks required")
    events: list[Event] = []
    sequences: dict[str, int] = {}

    def add(identity: Identity, fact: dict[str, JsonValue], at: datetime) -> None:
        seq = sequences.get(identity.execution_id, 0) + 1
        sequences[identity.execution_id] = seq
        event = Event.model_validate_json(
            canonical_bytes(
                {
                    "identity": identity.model_dump(mode="json"),
                    "seq": seq,
                    "source_key": digest(
                        canonical_bytes(
                            {"execution": identity.execution_id, "seq": seq, "fact": fact}
                        )
                    ),
                    "occurred_at": at.isoformat(),
                    "fact": fact,
                }
            )
        )
        events.append(event)

    stocks = {s.code: s for b in feed.batches for s in b.stocks}
    buy_at = min(datetime.combine(feed.trade_date, time(14, 51), CN), now)
    for index, (symbol, stock) in enumerate(stocks.items()):
        identity = Identity(
            account_id=node_account,
            strategy_id=strategy,
            selection_date=feed.trade_date,
            symbol=symbol,
            environment="TEST",
        )
        for batch in feed.batches:
            if not any(s.code == symbol for s in batch.stocks):
                continue
            add(
                identity,
                {
                    "kind": "CANDIDATE",
                    "phase": batch.phase,
                    "source_run_id": batch.source_run_id,
                    "name": stock.name,
                    "target_amount": amount,
                },
                batch.completed_at or buy_at,
            )
        quantity = min(200, int(Decimal(amount) // Decimal("1000")) * 100)
        progress = (
            "POSITION_CAPACITY"
            if index >= maximum
            else "SKIPPED_MIN_ORDER"
            if quantity == 0
            else "READY"
        )
        add(
            identity,
            {
                "kind": "PROGRESS",
                "plan_hash": digest(identity.execution_id.encode()),
                "status": progress,
                "target_quantity": quantity if progress == "READY" else 0,
            },
            buy_at,
        )
        if progress != "READY":
            continue
        prefix = identity.execution_id[5:29]

        def intent(
            key: str,
            side: str,
            at: datetime,
            identity: Identity = identity,
            quantity: int = quantity,
            prefix: str = prefix,
        ) -> None:
            add(
                identity,
                {
                    "kind": "SUBMISSION",
                    "client_order_id": key,
                    "side": side,
                    "action": "NEW",
                    "original_client_order_id": None,
                    "quantity": quantity,
                    "price": "10.00" if side == "BUY" else "9.50",
                    "status": "SDK_ACCEPTED",
                    "lot_id": "test-lot-" + prefix if side == "BUY" else None,
                },
                at,
            )

        def order(
            key: str,
            side: str,
            status: str,
            filled: int,
            at: datetime,
            identity: Identity = identity,
            quantity: int = quantity,
        ) -> None:
            add(
                identity,
                {
                    "kind": "ORDER",
                    "client_order_id": key,
                    "broker_order_id": "test-" + key,
                    "side": side,
                    "quantity": quantity,
                    "filled_quantity": filled,
                    "price": "10.00" if side == "BUY" else "9.50",
                    "status": status,
                    "reason_code": "TEST_" + side + "_REJECTED"
                    if status == "REJECTED"
                    else "TEST_" + status,
                },
                at,
            )

        def trade(
            key: str,
            side: str,
            shares: int,
            suffix: str,
            at: datetime,
            identity: Identity = identity,
        ) -> None:
            price = "10.00" if side == "BUY" else "11.00"
            add(
                identity,
                {
                    "kind": "TRADE",
                    "client_order_id": key,
                    "broker_order_id": "test-" + key,
                    "broker_trade_id": key + suffix,
                    "side": side,
                    "quantity": shares,
                    "price": price,
                    "amount": f"{Decimal(price) * shares:.2f}",
                },
                at,
            )

        failed_buy, buy = "tb-fail-" + prefix, "tb-" + prefix
        intent(failed_buy, "BUY", buy_at)
        order(failed_buy, "BUY", "REJECTED", 0, buy_at)
        intent(buy, "BUY", buy_at)
        order(buy, "BUY", "ACTIVE", 0, buy_at)
        partial = quantity // 2
        trade(buy, "BUY", partial, "-1", buy_at)
        order(buy, "BUY", "PARTIAL", partial, buy_at)
        add(
            identity,
            {
                "kind": "CANCEL_ERROR",
                "client_order_id": buy,
                "broker_order_id": "test-" + buy,
                "side": "BUY",
                "reason_code": "TEST_CANCEL_FAILED",
            },
            buy_at,
        )
        trade(buy, "BUY", quantity - partial, "-2", buy_at)
        order(buy, "BUY", "FILLED", quantity, buy_at)
        sell_at = max(buy_at, now)
        failed_sell, sell = "ts-fail-" + prefix, "ts-" + prefix
        intent(failed_sell, "SELL", sell_at)
        order(failed_sell, "SELL", "REJECTED", 0, sell_at)
        intent(sell, "SELL", sell_at)
        order(sell, "SELL", "ACTIVE", 0, sell_at)
        trade(sell, "SELL", quantity, "-1", sell_at)
        order(sell, "SELL", "FILLED", quantity, sell_at)
    return tuple(events)
