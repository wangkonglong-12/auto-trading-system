from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from auto_trading_system.protocol.v2.records import RECORD_MODELS, validate_record

BASE = {
    "account_id": "test-account",
    "trade_date": "2026-09-07",
    "observed_at": "2026-09-07T15:30:00+08:00",
}
HASH = "sha256:" + "1" * 64
EXAMPLES: dict[str, dict[str, Any]] = {
    "plan_results": {
        "plan_id": "plan-1",
        "plan_hash": HASH,
        "allocation_id": "allocation-1",
        "symbol": "000001.SZ",
        "target_quantity": 100,
        "filled_quantity": 100,
        "status": "COMPLETED",
        "reason_code": "TARGET_FILLED",
    },
    "submission_records": {
        "client_order_id": "order-1",
        "action_type": "NEW",
        "action_id": "action-1",
        "plan_hash": HASH,
        "symbol": "000001.SZ",
        "side": "BUY",
        "quantity": 100,
        "limit_price": "10.00",
        "maximum_amount": "1005.00",
        "payload_hash": HASH,
        "record_seq": 1,
        "engine_boot_id": "boot-1",
        "permission_epoch": 1,
        "status": "UNCERTAIN",
    },
    "broker_orders": {
        "broker_order_id": "order-1",
        "market": "SZ",
        "symbol": "000001.SZ",
        "side": "BUY",
        "quantity": 100,
        "filled_quantity": 50,
        "cancelled_quantity": 50,
        "limit_price": "10.00",
        "status": "CANCELLED",
        "raw_status": "PARTIALLY_FILLED_CANCELLED",
    },
    "broker_trades": {
        "broker_trade_id": "trade-1",
        "broker_order_id": "order-1",
        "market": "SZ",
        "symbol": "000001.SZ",
        "side": "BUY",
        "quantity": 100,
        "price": "10.00",
        "gross_amount": "1000.00",
        "traded_at": "2026-09-07T14:45:00+08:00",
        "origin": "MANUAL",
    },
    "positions": {
        "snapshot_id": "snapshot-1",
        "symbol": "000001.SZ",
        "total_quantity": 100,
        "available_quantity": 50,
        "frozen_quantity": 50,
        "strategy_quantity": 100,
        "market_value": "1000",
        "cost_amount": None,
        "attribution_status": "UNEXPLAINED",
    },
    "assets": {
        "snapshot_id": "snapshot-1",
        "cash_available": "1000",
        "cash_frozen": "100",
        "market_value": "2000",
        "total_equity": "3100",
        "quality": "CONFIRMED",
    },
    "reconciliations": {
        "reconciliation_id": "rec-1",
        "scope": "CASH",
        "status": "MATCHED",
        "amount_difference": "0.00",
        "reason_code": "MATCHED",
    },
    "incidents": {
        "incident_id": "incident-1",
        "severity": "CRITICAL",
        "status": "OPEN",
        "code": "UNMATCHED_TRADE",
        "summary": "Manual trade awaiting attribution",
    },
}


@pytest.mark.parametrize("dataset", tuple(EXAMPLES))
def test_all_eight_typed_datasets_and_scope_binding(dataset: str) -> None:
    value = {**BASE, **EXAMPLES[dataset]}
    result = validate_record(dataset, value, account_id="test-account", trade_date=date(2026, 9, 7))
    assert isinstance(result, RECORD_MODELS[dataset])
    for mutated in (
        {**value, "account_id": "wrong"},
        {**value, "trade_date": "2026-09-08"},
        {**value, "extra_field": "unrecognized"},
    ):
        with pytest.raises(ValueError):
            validate_record(
                dataset, mutated, account_id="test-account", trade_date=date(2026, 9, 7)
            )


@pytest.mark.parametrize(
    ("dataset", "change"),
    [
        ("plan_results", {"filled_quantity": 101}),
        ("plan_results", {"filled_quantity": 99}),
        ("submission_records", {"action_type": "CANCEL"}),
        ("submission_records", {"quantity": 0}),
        ("submission_records", {"limit_price": "0"}),
        ("broker_orders", {"market": "SH"}),
        ("broker_orders", {"quantity": 0}),
        ("broker_orders", {"filled_quantity": 60}),
        ("broker_orders", {"status": "FILLED"}),
        ("broker_orders", {"cancelled_quantity": 0}),
        ("broker_trades", {"quantity": 0}),
        ("broker_trades", {"market": "SH"}),
        ("broker_trades", {"price": "0"}),
        ("broker_trades", {"gross_amount": "0"}),
        ("broker_trades", {"traded_at": "2026-09-07T15:31:00+08:00"}),
        ("broker_trades", {"traded_at": "2026-09-06T14:45:00+08:00"}),
        ("positions", {"available_quantity": 51}),
        ("positions", {"strategy_quantity": 101}),
        ("assets", {"cash_available": 1000.1}),
        ("assets", {"cash_available": "-1"}),
        ("reconciliations", {"amount_difference": "0.01"}),
        ("reconciliations", {"expected_quantity": 1, "observed_quantity": None}),
    ],
)
def test_financial_inconsistencies_are_rejected_before_upload(
    dataset: str, change: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        validate_record(
            dataset,
            {**BASE, **EXAMPLES[dataset], **change},
            account_id="test-account",
            trade_date=date(2026, 9, 7),
        )


def test_unknown_dataset_cannot_select_a_generic_payload() -> None:
    with pytest.raises(ValueError, match="unknown daily"):
        validate_record("arbitrary", {}, account_id="test-account", trade_date=date(2026, 9, 7))
