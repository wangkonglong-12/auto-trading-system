"""QMT terminal orders without SDK IDs keep stable, strictly external identities."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_qmt_trading_gateway import ACCOUNT, NOW, REMARK, make_gateway
from test_recovery_v2 import evidence

from auto_trading_system.config.node_v2 import NodeProcessConfig
from auto_trading_system.gateways.broker.models import CancelOrder
from auto_trading_system.gateways.broker.qmt_command_transport import QmtCommandError
from auto_trading_system.journal.critical_models import RecoveryRecord
from auto_trading_system.protocol.qmt_recovery import QmtOrderRecoveryBinding
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.runtime.recovery_v2 import BrokerRecoverySnapshot, reconcile


def order(**changes: Any) -> SimpleNamespace:
    return SimpleNamespace(
        **{
            "account_id": ACCOUNT,
            "account_type": 2,
            "order_id": 0,
            "order_sysid": "1292891679",
            "stock_code": "600698.SH",
            "order_time": int(NOW.timestamp()),
            "order_type": 23,
            "order_volume": 600,
            "traded_volume": 600,
            "price": 7.53,
            "order_status": 56,
            "order_remark": "",
            "strategy_name": "",
            **changes,
        }
    )


def trade(**changes: Any) -> SimpleNamespace:
    return SimpleNamespace(
        **{
            "account_id": ACCOUNT,
            "account_type": 2,
            "order_id": 0,
            "order_sysid": "1292891679",
            "stock_code": "600698.SH",
            "traded_id": "80007163608",
            "traded_time": int(NOW.timestamp()) + 3,
            "order_type": 23,
            "traded_volume": 600,
            "traded_price": 7.53,
            "traded_amount": 4518.0,
            "order_remark": "",
            "strategy_name": "",
            **changes,
        }
    )


def test_zero_id_order_and_fill_are_preserved_matched_and_not_cancellable(tmp_path: Path) -> None:
    gateway, sdk = make_gateway(tmp_path)
    gateway.start(lambda _: None)
    try:
        sdk.orders, sdk.trades = [order()], [trade()]
        orders, trades = gateway.query_orders(), gateway.query_trades()
        assert len(orders) == len(trades) == 1
        first, fill = orders[0], trades[0]
        assert first.client_order_id.startswith("ord_external_")
        assert first.client_order_id == fill.client_order_id
        assert first.broker_order_id == fill.broker_order_id
        assert first.broker_order_id is not None and first.broker_order_id.startswith("qmt_sys_")
        assert fill.quantity.value == 600 and fill.notional.amount == Decimal("4518.00")
        account = gateway.read_account()
        assert account.observed_order_ids == (first.client_order_id,)
        report = reconcile(
            (),
            BrokerRecoverySnapshot(orders, trades, account, 1, True),
            account_id=gateway._account_alias,
            boot_id="engine-zero-id-test",
            trade_date=NOW.date(),
            now=NOW,
            report_id="report-zero-id-test",
            allow_unmanaged=True,
        )
        assert report.status == "BLOCKED"
        assert {"UNOWNED_BROKER_ORDER", "UNOWNED_BROKER_TRADE"} <= set(report.issue_codes)
        assert gateway.query_orders() == orders
        with pytest.raises(QmtCommandError):
            gateway.cancel_order(CancelOrder(first.client_order_id, first.broker_order_id))
        with pytest.raises(QmtCommandError):
            gateway._transport.query_order(0, "")
        assert sdk.calls == []
    finally:
        gateway.stop()


def test_multiple_zero_local_ids_use_distinct_stable_contracts(tmp_path: Path) -> None:
    gateway, sdk = make_gateway(tmp_path)
    gateway.start(lambda _: None)
    try:
        sdk.orders = [order(), order(order_sysid="1292891680")]
        sdk.trades = [trade(), trade(order_sysid="1292891680", traded_id="fill-two")]
        before = gateway.query_orders()
        assert len({row.client_order_id for row in before}) == 2
        assert {row.client_order_id for row in gateway.query_trades()} == {
            row.client_order_id for row in before
        }
        sdk.orders.reverse()
        assert set(gateway.query_orders()) == set(before)
        sdk.orders.append(order())
        with pytest.raises(QmtCommandError, match="QMT_ORDER_QUERY_INVALID"):
            gateway.query_orders()
    finally:
        gateway.stop()


@pytest.mark.parametrize(
    "changes",
    [
        {"order_sysid": ""},
        {"order_sysid": None},
        {"order_sysid": " "},
        {"order_sysid": "x" * 65},
        {"order_id": -1},
        {"order_id": False},
        {"strategy_name": "personal-auto-trading"},
        {"order_remark": REMARK},
        {"strategy_name": None},
    ],
)
@pytest.mark.parametrize("dataset", ["orders", "trades"])
def test_zero_id_never_weakens_missing_or_system_identity_checks(
    tmp_path: Path,
    changes: dict[str, Any],
    dataset: str,
) -> None:
    gateway, sdk = make_gateway(tmp_path)
    gateway.start(lambda _: None)
    try:
        setattr(sdk, dataset, [(order if dataset == "orders" else trade)(**changes)])
        with pytest.raises(QmtCommandError, match=r"QMT_(ORDER|TRADE)_QUERY_INVALID"):
            (gateway.query_orders if dataset == "orders" else gateway.query_trades)()
        assert sdk.calls == []
    finally:
        gateway.stop()


def binding(**changes: Any) -> QmtOrderRecoveryBinding:
    return QmtOrderRecoveryBinding(
        **{
            "trade_date": NOW.date(),
            "symbol": "600698.SH",
            "order_sysid": "1292891679",
            "client_order_id": "ord_confirmed_manual01",
            "side": "BUY",
            "quantity": 600,
            "limit_price": "7.53",
            **changes,
        },
    )


def test_explicit_binding_restores_original_j1_but_grants_no_trading_permission(
    tmp_path: Path,
) -> None:
    bound = binding()
    gateway, sdk = make_gateway(tmp_path, order_recovery_bindings=(bound,))
    gateway.start(lambda _: None)
    try:
        sdk.orders, sdk.trades = [order()], [trade()]
        sdk.positions = [
            SimpleNamespace(
                account_id=ACCOUNT,
                account_type=2,
                stock_code="600698.SH",
                volume=600,
                can_use_volume=0,
                frozen_volume=0,
                market_value=4518.0,
                avg_price=7.53,
            )
        ]
        orders, trades = gateway.query_orders(), gateway.query_trades()
        assert orders[0].client_order_id == trades[0].client_order_id == bound.client_order_id
        template = evidence()[0][0].submission
        item = template.model_copy(
            update={
                "client_order_id": bound.client_order_id,
                "account_id": gateway._account_alias,
                "trade_date": NOW.date(),
                "symbol": bound.symbol,
                "side": "BUY",
                "quantity": 600,
                "limit_price": "7.53",
                "maximum_amount": "4523.00",
            }
        )
        record = RecoveryRecord(
            record_seq=1,
            payload_hash=digest(canonical_bytes(item.model_dump(mode="json"))),
            submission=item,
        )
        snapshot = BrokerRecoverySnapshot(orders, trades, gateway.read_account(), 1, True)
        kwargs: dict[str, Any] = {
            "account_id": gateway._account_alias,
            "boot_id": "engine-binding-test",
            "trade_date": NOW.date(),
            "now": NOW,
            "report_id": "report-binding-test",
            "allow_unmanaged": True,
        }
        assert reconcile((record,), snapshot, **kwargs).status == "CLEAR"
        assert reconcile((), snapshot, **kwargs).status == "BLOCKED"
        assert sdk.calls == []
    finally:
        gateway.stop()


@pytest.mark.parametrize(
    "changes",
    [{"symbol": "600580.SH"}, {"quantity": 100}, {"limit_price": "7.54"}, {"side": "SELL"}],
)
def test_binding_conflict_does_not_guess_ownership(tmp_path: Path, changes: dict[str, Any]) -> None:
    gateway, sdk = make_gateway(tmp_path, order_recovery_bindings=(binding(**changes),))
    gateway.start(lambda _: None)
    try:
        sdk.orders = [order()]
        with pytest.raises(QmtCommandError, match="QMT_RECOVERY_BINDING_CONFLICT"):
            gateway.query_orders()
    finally:
        gateway.stop()


def test_duplicate_explicit_contract_mapping_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(QmtCommandError, match="QMT_RECOVERY_BINDING_CONFLICT"):
        make_gateway(
            tmp_path,
            order_recovery_bindings=(binding(), binding(client_order_id="ord_other_manual01")),
        )


def test_recovery_binding_preserves_account_scope_but_terminal_change_does_not(
    tmp_path: Path,
) -> None:
    value = json.loads(Path("config/v2/node.mock.example.json").read_text(encoding="utf-8"))
    value["broker"] = "QMT_READ_ONLY"
    value["account_id"] = "qmt-" + "a" * 16
    value.pop("paper_session", None)
    value["data_root"] = str(tmp_path / "node-data")
    value["qmt_read_only"] = {
        "userdata_mini": str(tmp_path / "userdata_mini"),
        "account_fingerprint": "a" * 16,
    }
    original = NodeProcessConfig.model_validate_json(json.dumps(value))
    value["qmt_read_only"]["order_recovery_bindings"] = [binding().model_dump(mode="json")]
    restored = NodeProcessConfig.model_validate_json(json.dumps(value))
    assert restored.scope() == original.scope()
    value["qmt_read_only"]["userdata_mini"] = str(tmp_path / "different" / "userdata_mini")
    assert NodeProcessConfig.model_validate_json(json.dumps(value)).scope() != original.scope()
