from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from auto_trading_system.domain import (
    DomainValueError,
    Money,
    Price,
    Quantity,
    SecurityCode,
    ShanghaiTimestamp,
)
from auto_trading_system.gateways.broker import (
    BrokerCancelError,
    BrokerOrder,
    BrokerOrderStatus,
    BrokerTrade,
    CancellationReceipt,
    CancelOrder,
    OrderSide,
    SubmissionReceipt,
    SubmitOrder,
)


def timestamp() -> ShanghaiTimestamp:
    return ShanghaiTimestamp.parse("2026-09-04T14:35:00+08:00")


def submit_order() -> SubmitOrder:
    return SubmitOrder(
        client_order_id="ord_01K43TAIL000000001",
        symbol=SecurityCode.parse("000001.SZ"),
        side=OrderSide.BUY,
        quantity=Quantity(100),
        limit_price=Price.parse("12.34"),
        remark_token="ATS000000000000000000001",
    )


def broker_order() -> BrokerOrder:
    command = submit_order()
    return BrokerOrder(
        client_order_id=command.client_order_id,
        broker_order_id="MOCK0000000000000001",
        symbol=command.symbol,
        side=command.side,
        status=BrokerOrderStatus.ACKED,
        order_quantity=command.quantity,
        filled_quantity=Quantity(0),
        remaining_quantity=command.quantity,
        limit_price=command.limit_price,
        broker_status_code=50,
        status_reason="MOCK_ORDER_ACCEPTED",
        updated_at=timestamp(),
    )


def broker_trade() -> BrokerTrade:
    order = broker_order()
    assert order.broker_order_id is not None
    return BrokerTrade(
        trade_id="trade_MOCK0000000000000001",
        client_order_id=order.client_order_id,
        broker_order_id=order.broker_order_id,
        symbol=order.symbol,
        side=order.side,
        price=Price.parse("12.35"),
        quantity=Quantity(50),
        notional=Money.parse("617.50"),
        traded_at=timestamp(),
    )


def test_submit_and_cancel_commands_accept_canonical_values() -> None:
    command = submit_order()
    cancel = CancelOrder(command.client_order_id, "MOCK0000000000000001")

    assert command.quantity == Quantity(100)
    assert cancel.broker_order_id == "MOCK0000000000000001"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("client_order_id", "bad"),
        ("remark_token", "has space"),
        ("remark_token", "X" * 25),
        ("quantity", Quantity(0)),
    ],
)
def test_submit_command_rejects_invalid_identity_or_quantity(field: str, value: object) -> None:
    with pytest.raises(DomainValueError):
        replace(submit_order(), **{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("client_order_id", "broker_order_id"),
    [("bad", "MOCK1"), ("ord_01K43TAIL000000001", "bad broker id")],
)
def test_cancel_command_rejects_invalid_identity(
    client_order_id: str, broker_order_id: str
) -> None:
    with pytest.raises(DomainValueError, match="INVALID_BROKER_VALUE"):
        CancelOrder(client_order_id, broker_order_id)


@pytest.mark.parametrize("receipt_type", [SubmissionReceipt, CancellationReceipt])
@pytest.mark.parametrize("request_seq", [True, 0, -1])
def test_receipt_requires_positive_non_boolean_sequence(
    receipt_type: type[SubmissionReceipt] | type[CancellationReceipt],
    request_seq: object,
) -> None:
    with pytest.raises(DomainValueError, match="INVALID_REQUEST_SEQUENCE"):
        receipt_type(request_seq=request_seq, submitted_at=timestamp())  # type: ignore[arg-type]


def test_broker_order_exposes_terminal_state() -> None:
    assert not broker_order().is_terminal
    assert replace(broker_order(), status=BrokerOrderStatus.FILLED).is_terminal


@pytest.mark.parametrize(
    ("changes", "error_code"),
    [
        ({"client_order_id": "bad"}, "INVALID_BROKER_VALUE"),
        ({"broker_order_id": "bad broker id"}, "INVALID_BROKER_VALUE"),
        ({"status_reason": "bad-reason"}, "INVALID_BROKER_VALUE"),
        (
            {"order_quantity": Quantity(0), "remaining_quantity": Quantity(0)},
            "INVALID_ORDER_QUANTITY",
        ),
        ({"filled_quantity": Quantity(10)}, "ORDER_QUANTITY_MISMATCH"),
        ({"broker_status_code": True}, "INVALID_BROKER_STATUS_CODE"),
        ({"broker_status_code": -1}, "INVALID_BROKER_STATUS_CODE"),
    ],
)
def test_broker_order_rejects_invalid_facts(changes: dict[str, object], error_code: str) -> None:
    with pytest.raises(DomainValueError, match=error_code):
        replace(broker_order(), **changes)  # type: ignore[arg-type]


def test_broker_order_allows_missing_broker_id_during_submission() -> None:
    assert replace(broker_order(), broker_order_id=None).broker_order_id is None


def test_broker_trade_accepts_exact_values() -> None:
    trade = broker_trade()

    assert trade.quantity == Quantity(50)
    assert trade.notional == Money.parse("617.50")


@pytest.mark.parametrize(
    ("changes", "error_code"),
    [
        ({"trade_id": "bad"}, "INVALID_BROKER_VALUE"),
        ({"client_order_id": "bad"}, "INVALID_BROKER_VALUE"),
        ({"broker_order_id": "bad broker id"}, "INVALID_BROKER_VALUE"),
        ({"quantity": Quantity(0)}, "INVALID_TRADE_QUANTITY"),
        ({"notional": Money(Decimal(0))}, "INVALID_TRADE_NOTIONAL"),
    ],
)
def test_broker_trade_rejects_invalid_facts(changes: dict[str, object], error_code: str) -> None:
    with pytest.raises(DomainValueError, match=error_code):
        replace(broker_trade(), **changes)  # type: ignore[arg-type]


def test_cancel_error_validates_code_and_message() -> None:
    valid = BrokerCancelError(
        client_order_id="ord_01K43TAIL000000001",
        broker_order_id="MOCK0000000000000001",
        error_code="MOCK_CANCEL_REJECTED",
        message="cancel rejected",
        occurred_at=timestamp(),
    )
    assert valid.error_code == "MOCK_CANCEL_REJECTED"

    with pytest.raises(DomainValueError, match="INVALID_BROKER_VALUE"):
        replace(valid, error_code="bad-code")
    with pytest.raises(DomainValueError, match="INVALID_ERROR_MESSAGE"):
        replace(valid, message="")
    with pytest.raises(DomainValueError, match="INVALID_ERROR_MESSAGE"):
        replace(valid, message="x" * 257)
