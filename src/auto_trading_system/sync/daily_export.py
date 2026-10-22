"""Pure conversion of frozen internal facts into the V2 financial wire models.

The caller freezes the observation window first. These functions perform no
broker query, network request or SQLite access and never infer unknown IDs,
submission outcomes, or the ownership of manual trades.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from auto_trading_system.gateways.broker.models import BrokerOrder, BrokerTrade
from auto_trading_system.journal.critical_models import RecoveryRecord
from auto_trading_system.protocol.v2.canonical import canonical_bytes
from auto_trading_system.protocol.v2.daily_sync import DailyRecord
from auto_trading_system.protocol.v2.records import (
    BrokerOrderRecord,
    BrokerTradeRecord,
    Observation,
    SubmissionRecord,
)


def daily_record(record_id: str, value: Observation) -> DailyRecord:
    return DailyRecord(record_id=record_id, data=value.model_dump(mode="json"))


def export_submission(
    item: RecoveryRecord,
    *,
    observed_at: datetime,
    status: Literal[
        "SEND_REQUESTED", "NOT_DISPATCHED", "SDK_ACCEPTED", "REJECTED", "UNCERTAIN", "RESOLVED"
    ],
) -> DailyRecord:
    submission = item.submission
    record = SubmissionRecord(
        account_id=submission.account_id,
        trade_date=submission.trade_date,
        observed_at=observed_at,
        client_order_id=submission.client_order_id,
        action_type=submission.action_type,
        action_id=submission.target.action_id,
        plan_hash=submission.plan_hash,
        symbol=submission.symbol,
        side=submission.side,
        quantity=submission.quantity,
        limit_price=submission.limit_price,
        maximum_amount=submission.maximum_amount,
        payload_hash=item.payload_hash,
        record_seq=item.record_seq,
        engine_boot_id=submission.engine_boot_id,
        permission_epoch=submission.permission_epoch,
        target_order_id=submission.original_order_id,
        status=status,
    )
    return daily_record(submission.client_order_id, record)


def export_order(
    item: BrokerOrder, *, account_id: str, trade_date: date, observed_at: datetime
) -> DailyRecord:
    if item.broker_order_id is None:
        raise ValueError(
            "unconfirmed broker order identity requires an incident, not a fabricated ID"
        )
    statuses = {
        "SUBMITTING": "UNKNOWN",
        "ACKED": "ACTIVE",
        "CANCEL_PENDING": "ACTIVE",
        "PARTIAL_FILL_CANCEL_PENDING": "PARTIAL",
        "PARTIALLY_FILLED": "PARTIAL",
        "PARTIALLY_FILLED_CANCELLED": "CANCELLED",
        "FILLED": "FILLED",
        "CANCELLED": "CANCELLED",
        "REJECTED": "REJECTED",
        "UNKNOWN": "UNKNOWN",
        "ERROR_HOLD": "UNKNOWN",
    }
    status = statuses[item.status.value]
    record = BrokerOrderRecord.model_validate_json(
        canonical_bytes(
            {
                "account_id": account_id,
                "trade_date": trade_date.isoformat(),
                "observed_at": observed_at.isoformat(),
                "broker_order_id": item.broker_order_id,
                "client_order_id": item.client_order_id,
                "symbol": item.symbol.to_wire(),
                "market": item.symbol.market.value,
                "side": item.side.value,
                "quantity": item.order_quantity.value,
                "filled_quantity": item.filled_quantity.value,
                "cancelled_quantity": item.remaining_quantity.value if status == "CANCELLED" else 0,
                "limit_price": item.limit_price.to_wire(),
                "status": status,
                "raw_status": str(item.broker_status_code)
                if item.broker_status_code is not None
                else item.status.value,
            }
        )
    )
    return daily_record(item.broker_order_id, record)


def export_trade(
    item: BrokerTrade,
    *,
    account_id: str,
    trade_date: date,
    observed_at: datetime,
    origin: Literal["STRATEGY", "MANUAL", "UNKNOWN"],
) -> DailyRecord:
    record = BrokerTradeRecord.model_validate_json(
        canonical_bytes(
            {
                "account_id": account_id,
                "trade_date": trade_date.isoformat(),
                "observed_at": observed_at.isoformat(),
                "broker_trade_id": item.trade_id,
                "broker_order_id": item.broker_order_id,
                "client_order_id": item.client_order_id,
                "market": item.symbol.market.value,
                "symbol": item.symbol.to_wire(),
                "side": item.side.value,
                "quantity": item.quantity.value,
                "price": item.price.to_wire(),
                "gross_amount": item.notional.to_wire(),
                "traded_at": item.traded_at.to_wire(),
                "origin": origin,
            }
        )
    )
    return daily_record(item.trade_id, record)
