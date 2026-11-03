"""Broker contract evidence survives missing SDK IDs without guessed ownership."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_qmt_trading_gateway import REMARK, make_gateway
from test_qmt_zero_order_id import order
from test_recovery_v2 import evidence

from auto_trading_system.domain import Price
from auto_trading_system.gateways.broker.models import BrokerOrderIdentity, CancelOrder
from auto_trading_system.gateways.broker.qmt_command_transport import QmtCommandError
from auto_trading_system.journal.apsw_store import APSWCriticalStore
from auto_trading_system.journal.critical_models import RecoveryRecord
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest
from auto_trading_system.runtime.qmt_order_identities import (
    identity_key,
    load_order_identities,
    order_identity,
)


def test_contract_evidence_is_bound_to_exact_j1_and_detects_cross_account(tmp_path: Path) -> None:
    records, snapshot = evidence()
    original = records[0].submission
    observed = replace(snapshot.orders[0], broker_contract_id="1292891679")
    body = order_identity(original, observed)
    assert body is not None
    store = APSWCriticalStore(tmp_path / "identity.db", original.account_id)
    store.open()
    try:
        store.commit_submission(original)
        saved_record = store.read_submissions(0, 10)[0]
        store.append_evidence("broker_facts", identity_key(original.client_order_id), body)
        assert (
            load_order_identities(store, (saved_record,))[0].client_order_id
            == original.client_order_id
        )
        altered = original.model_copy(update={"account_id": "other-account"})
        changed = RecoveryRecord(
            record_seq=1,
            payload_hash=digest(canonical_bytes(altered.model_dump(mode="json"))),
            submission=altered,
        )
        with pytest.raises(ValueError, match="IDENTITY_CONFLICT"):
            load_order_identities(store, (changed,))
        with pytest.raises(ValueError, match="IDENTITY_CONFLICT"):
            order_identity(original, replace(observed, limit_price=Price.parse("9.99")))
    finally:
        store.close()


def test_callback_contract_restores_zero_local_id_and_cancels_only_owned_target(
    tmp_path: Path,
) -> None:
    gateway, sdk = make_gateway(tmp_path)
    events = []
    gateway.start(events.append)
    try:
        raw = order(
            order_id=81234,
            order_remark=REMARK,
            strategy_name="personal-auto-trading",
            order_status=50,
            traded_volume=0,
            order_type=24,
        )
        sdk.orders = [raw]
        first = gateway.query_orders()[0]
        assert first.broker_contract_id == "1292891679"
        sdk.callback.on_stock_order(raw)
        gateway.query_orders()
        assert any(isinstance(e, BrokerOrderIdentity) for e in events)
        from test_qmt_trading_gateway import NOW

        from auto_trading_system.protocol.qmt_recovery import QmtOrderRecoveryBinding

        binding = QmtOrderRecoveryBinding(
            client_order_id=first.client_order_id,
            trade_date=NOW.date(),
            symbol="600698.SH",
            order_sysid="1292891679",
            side="SELL",
            quantity=600,
            limit_price="7.53",
        )
        gateway.restore_order_identities((binding,))
        raw.order_id, raw.order_remark, raw.strategy_name = 0, "", ""
        restored = gateway.query_orders()[0]
        assert (
            restored.client_order_id == first.client_order_id
            and restored.broker_order_id.startswith("qmt_sys_")
        )

        def cancel_sysid(*args: Any) -> int:
            sdk.calls.append(("cancel_sysid", *args))
            return 72

        dynamic_sdk: Any = sdk
        dynamic_sdk.cancel_order_stock_sysid_async = cancel_sysid
        gateway.cancel_order(CancelOrder(restored.client_order_id, restored.broker_order_id))
        assert sdk.calls[-1][0] == "cancel_sysid" and sdk.calls[-1][-2:] == (0, "1292891679")
        with pytest.raises(QmtCommandError):
            gateway.cancel_order(CancelOrder("ord_external_unknown01", restored.broker_order_id))
    finally:
        gateway.stop()
