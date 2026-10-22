"""Recovery proofs must explain every order, fill and held share before approval."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from auto_trading_system.apps.trade_runtime.engine_simulation import DEMO_NOW
from auto_trading_system.domain import Money, Price, Quantity, SecurityCode, ShanghaiTimestamp
from auto_trading_system.gateways.broker.errors import BrokerQueryError
from auto_trading_system.gateways.broker.models import (
    BrokerOrder,
    BrokerOrderStatus,
    BrokerTrade,
    OrderSide,
)
from auto_trading_system.journal.critical_models import ActionTarget, RecoveryRecord, Submission
from auto_trading_system.runtime.recovery_v2 import (
    AccountFact,
    BrokerRecoverySnapshot,
    PositionFact,
    RecoveryReport,
    reconcile,
)


def evidence() -> tuple[tuple[RecoveryRecord, ...], BrokerRecoverySnapshot]:
    submission = Submission(
        client_order_id="ord_recovery001",
        account_id="test-account",
        trade_date=DEMO_NOW.date(),
        action_type="NEW",
        plan_hash="sha256:" + "a" * 64,
        symbol="000001.SZ",
        side="BUY",
        quantity=100,
        limit_price="10",
        maximum_amount="1005",
        remark_token="recovery001",
        engine_boot_id="old-boot",
        permission_epoch=1,
        target=ActionTarget(action_id="action1", target_remaining=100, rule_id="rule1"),
    )
    order = BrokerOrder(
        client_order_id=submission.client_order_id,
        broker_order_id="broker1",
        symbol=SecurityCode.parse(submission.symbol),
        side=OrderSide.BUY,
        status=BrokerOrderStatus.FILLED,
        order_quantity=Quantity(100),
        filled_quantity=Quantity(100),
        remaining_quantity=Quantity(0),
        limit_price=Price.parse("10"),
        broker_status_code=56,
        status_reason="FILLED",
        updated_at=ShanghaiTimestamp.from_instant(DEMO_NOW),
    )
    trade = BrokerTrade(
        trade_id="trade_recovery001",
        client_order_id=submission.client_order_id,
        broker_order_id="broker1",
        symbol=order.symbol,
        side=OrderSide.BUY,
        price=Price.parse("10"),
        quantity=Quantity(100),
        notional=Money.parse("1000"),
        traded_at=order.updated_at,
    )
    account = AccountFact(
        account_id="test-account",
        snapshot_id="account1",
        observed_at=DEMO_NOW,
        cash="8995",
        positions=(PositionFact(symbol=submission.symbol, quantity=100, sellable=0),),
        observed_order_ids=(submission.client_order_id,),
        coherent=True,
    )
    record = RecoveryRecord(record_seq=1, payload_hash="sha256:" + "b" * 64, submission=submission)
    return (record,), BrokerRecoverySnapshot((order,), (trade,), account, 1, True)


def report(records: tuple[RecoveryRecord, ...], snapshot: BrokerRecoverySnapshot) -> RecoveryReport:
    return reconcile(
        records,
        snapshot,
        account_id="test-account",
        boot_id="new-boot",
        trade_date=DEMO_NOW.date(),
        now=DEMO_NOW,
        report_id="report1",
    )


def test_terminal_facts_and_current_holding_explain_journal_and_bind_hash() -> None:
    records, snapshot = evidence()
    good = report(records, snapshot)
    assert good.status == "CLEAR" and good.unresolved_count == 0
    assert good.reserved_buy_value == "1005.00"
    assert good.submission_high_watermark == 1
    changed = report(
        records, replace(snapshot, account=snapshot.account.model_copy(update={"cash": "8994"}))
    )
    assert good.evidence_hash != changed.evidence_hash
    assert good.payload_hash != changed.payload_hash
    assert snapshot.account.observation().cash == Money.parse("8995")


@pytest.mark.parametrize(
    ("case", "issue"),
    [
        ("crossed", "OBSERVATION_CROSSED_EVENT"),
        ("incoherent", "OBSERVATION_CROSSED_EVENT"),
        ("stale", "ACCOUNT_OBSERVATION_STALE"),
        ("future", "ACCOUNT_OBSERVATION_STALE"),
        ("window", "ACCOUNT_ORDER_WINDOW_MISMATCH"),
        ("unowned_trade", "UNOWNED_BROKER_TRADE"),
        ("unowned_order", "UNOWNED_BROKER_ORDER"),
        ("trade_identity", "BROKER_TRADE_IDENTITY_CONFLICT"),
        ("trade_day", "BROKER_TRADE_IDENTITY_CONFLICT"),
        ("trade_amount", "TRADE_AMOUNT_MISMATCH"),
        ("historical", "HISTORICAL_OR_ACCOUNT_SCOPE_UNRESOLVED"),
        ("missing", "ORDER_NOT_OBSERVED_IS_NOT_PROOF"),
        ("order_price", "ORDER_IDENTITY_CONFLICT"),
        ("active", "ORDER_ACTIVE_OR_TRADE_MISMATCH"),
        ("missing_fill", "ORDER_ACTIVE_OR_TRADE_MISMATCH"),
        ("cancelled_with_fill", "ORDER_ACTIVE_OR_TRADE_MISMATCH"),
        ("partial_cancel_full", "ORDER_ACTIVE_OR_TRADE_MISMATCH"),
        ("partial_cancel_zero", "ORDER_ACTIVE_OR_TRADE_MISMATCH"),
        ("position", "POSITION_OWNERSHIP_UNRESOLVED"),
        ("cancel_target", "CANCEL_TARGET_CONFLICT"),
    ],
)
def test_incomplete_or_conflicting_recovery_evidence_blocks(case: str, issue: str) -> None:
    records, snapshot = evidence()
    order, trade, account = snapshot.orders[0], snapshot.trades[0], snapshot.account
    if case == "crossed":
        snapshot = replace(snapshot, stable=False)
    elif case == "incoherent":
        snapshot = replace(snapshot, account=account.model_copy(update={"coherent": False}))
    elif case in ("stale", "future"):
        at = DEMO_NOW + timedelta(seconds=-6 if case == "stale" else 1)
        snapshot = replace(snapshot, account=account.model_copy(update={"observed_at": at}))
    elif case == "window":
        snapshot = replace(snapshot, account=account.model_copy(update={"observed_order_ids": ()}))
    elif case == "unowned_trade":
        snapshot = replace(snapshot, trades=(replace(trade, client_order_id="ord_external001"),))
    elif case == "unowned_order":
        records = ()
    elif case == "trade_identity":
        snapshot = replace(snapshot, trades=(replace(trade, broker_order_id="wrong-order"),))
    elif case == "trade_day":
        snapshot = replace(
            snapshot,
            trades=(
                replace(
                    trade, traded_at=ShanghaiTimestamp.from_instant(DEMO_NOW - timedelta(days=1))
                ),
            ),
        )
    elif case == "trade_amount":
        snapshot = replace(snapshot, trades=(replace(trade, notional=Money.parse("999")),))
    elif case == "historical":
        records = (
            records[0].model_copy(
                update={
                    "submission": records[0].submission.model_copy(
                        update={"trade_date": DEMO_NOW.date() - timedelta(days=1)}
                    )
                }
            ),
        )
    elif case == "missing":
        snapshot = replace(snapshot, orders=(), trades=())
    elif case == "order_price":
        snapshot = replace(snapshot, orders=(replace(order, limit_price=Price.parse("9.99")),))
    elif case == "active":
        snapshot = replace(snapshot, orders=(replace(order, status=BrokerOrderStatus.ACKED),))
    elif case == "missing_fill":
        snapshot = replace(snapshot, trades=())
    elif case == "cancelled_with_fill":
        snapshot = replace(snapshot, orders=(replace(order, status=BrokerOrderStatus.CANCELLED),))
    elif case == "partial_cancel_full":
        snapshot = replace(
            snapshot, orders=(replace(order, status=BrokerOrderStatus.PARTIALLY_FILLED_CANCELLED),)
        )
    elif case == "partial_cancel_zero":
        snapshot = replace(
            snapshot,
            orders=(
                replace(
                    order,
                    status=BrokerOrderStatus.PARTIALLY_FILLED_CANCELLED,
                    filled_quantity=Quantity(0),
                    remaining_quantity=Quantity(100),
                ),
            ),
            trades=(),
            account=account.model_copy(update={"positions": ()}),
        )
    elif case == "position":
        snapshot = replace(snapshot, account=account.model_copy(update={"positions": ()}))
    elif case == "cancel_target":
        cancel = records[0].submission.model_copy(
            update={
                "client_order_id": "ord_cancel0001",
                "action_type": "CANCEL",
                "original_client_order_id": order.client_order_id,
                "original_order_id": "wrong-target",
            }
        )
        records += (records[0].model_copy(update={"record_seq": 2, "submission": cancel}),)
    bad = report(records, snapshot)
    assert bad.status == "BLOCKED" and issue in bad.issue_codes


@pytest.mark.parametrize("case", ["orders", "trades", "records", "sequence", "account", "capacity"])
def test_invalid_recovery_input_never_collapses_into_success(case: str) -> None:
    records, snapshot = evidence()
    if case == "orders":
        snapshot = replace(snapshot, orders=snapshot.orders * 2)
    elif case == "trades":
        snapshot = replace(snapshot, trades=snapshot.trades * 2)
    elif case == "records":
        records *= 2
    elif case == "sequence":
        second = records[0].model_copy(
            update={
                "submission": records[0].submission.model_copy(
                    update={"client_order_id": "ord_recovery002"}
                )
            }
        )
        records += (second,)
    elif case == "account":
        snapshot = replace(
            snapshot, account=snapshot.account.model_copy(update={"account_id": "wrong"})
        )
    else:
        records *= 2049
    with pytest.raises((ValueError, BrokerQueryError)):
        report(records, snapshot)


def test_account_validation_rejects_ambiguous_holdings() -> None:
    _, snapshot = evidence()
    with pytest.raises(ValueError, match="sellable"):
        PositionFact(symbol="000001.SZ", quantity=100, sellable=101)
    for updates in (
        {"positions": snapshot.account.positions * 2},
        {"observed_order_ids": snapshot.account.observed_order_ids * 2},
    ):
        with pytest.raises(ValueError, match="duplicate"):
            AccountFact.model_validate_json(
                snapshot.account.model_copy(update=updates).model_dump_json()
            )
