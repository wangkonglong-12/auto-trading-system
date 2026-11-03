"""Journal-backed broker contracts, bound to the exact original J1 digest."""

from auto_trading_system.domain import Price
from auto_trading_system.gateways.broker.models import BrokerOrder
from auto_trading_system.journal.critical_models import RecoveryRecord, Submission
from auto_trading_system.journal.critical_writer import CriticalStorePort
from auto_trading_system.protocol.qmt_recovery import QmtOrderRecoveryBinding
from auto_trading_system.protocol.v2.base import Sha256, WireModel
from auto_trading_system.protocol.v2.canonical import canonical_bytes, digest


class StoredOrderIdentity(WireModel):
    account_id: str
    submission_hash: Sha256
    binding: QmtOrderRecoveryBinding


def identity_key(client_id: str) -> str:
    return "qmt-order-identity:" + client_id


def order_identity(original: Submission, order: BrokerOrder) -> bytes | None:
    if not order.broker_contract_id or original.action_type != "NEW":
        return None
    if (
        original.client_order_id,
        original.symbol,
        original.side,
        original.quantity,
        Price.parse(original.limit_price),
        original.trade_date,
    ) != (
        order.client_order_id,
        str(order.symbol),
        order.side.value,
        order.order_quantity.value,
        order.limit_price,
        order.updated_at.value.date(),
    ):
        raise ValueError("QMT_ORDER_IDENTITY_CONFLICT")
    binding = QmtOrderRecoveryBinding(
        client_order_id=original.client_order_id,
        symbol=original.symbol,
        side=original.side,
        quantity=original.quantity,
        limit_price=original.limit_price,
        trade_date=original.trade_date,
        order_sysid=order.broker_contract_id,
    )
    return canonical_bytes(
        StoredOrderIdentity(
            account_id=original.account_id,
            submission_hash=digest(canonical_bytes(original.model_dump(mode="json"))),
            binding=binding,
        ).model_dump(mode="json")
    )


def load_order_identities(
    store: CriticalStorePort, records: tuple[RecoveryRecord, ...]
) -> tuple[QmtOrderRecoveryBinding, ...]:
    bindings = []
    for record in records:
        original = record.submission
        if original.action_type != "NEW":
            continue
        body = store.read_evidence("broker_facts", identity_key(original.client_order_id))
        if body is None:
            continue
        saved = StoredOrderIdentity.model_validate_json(body)
        b = saved.binding
        if saved.submission_hash != record.payload_hash or (
            saved.account_id,
            b.client_order_id,
            b.symbol,
            b.side,
            b.quantity,
            b.limit_price,
            b.trade_date,
        ) != (
            original.account_id,
            original.client_order_id,
            original.symbol,
            original.side,
            original.quantity,
            original.limit_price,
            original.trade_date,
        ):
            raise ValueError("QMT_ORDER_IDENTITY_CONFLICT")
        bindings.append(b)
    return tuple(bindings)
