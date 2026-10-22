"""Single-use, supervised QMT order/cancel diagnostic.

This is a field-debug path, not the automatic strategy gateway. It never retries
an uncertain submission and never reports an async request as a fill.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep

from auto_trading_system.domain import ShanghaiTimestamp
from auto_trading_system.gateways.broker.models import CancelOrder, OrderSide, SubmitOrder
from auto_trading_system.gateways.broker.qmt_command_transport import (
    QmtCommandError,
    QmtCommandTransport,
    QmtOrderFact,
)
from auto_trading_system.monitoring.qmt_order_preflight import (
    QmtOrderPocApproval,
    QmtOrderPreflightReport,
)
from auto_trading_system.monitoring.qmt_readonly import QmtProbeOutcome

_TERMINAL = frozenset((53, 54, 56, 57))


@dataclass(frozen=True, slots=True)
class QmtCommandProbeResult:
    outcome: str
    reason: str
    submit_sequence: int | None
    broker_order_id: int | None
    cancel_sequence: int | None
    broker_status: int | None
    filled_quantity: int | None


def _reserve_nonce(directory: Path, approval: QmtOrderPocApproval) -> None:
    if not directory.is_absolute():
        raise QmtCommandError("QMT_NONCE_DIRECTORY_INVALID")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (approval.single_use_nonce + ".used")
    try:
        with target.open("xb") as handle:
            handle.write(approval.approval_id.encode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise QmtCommandError("QMT_APPROVAL_ALREADY_USED") from exc
    except OSError as exc:
        raise QmtCommandError("QMT_NONCE_RESERVATION_FAILED") from exc


def _matches(fact: QmtOrderFact, approval: QmtOrderPocApproval, buy: int, sell: int) -> bool:
    expected_side = buy if approval.side.value == "BUY" else sell
    return (
        fact.symbol == approval.symbol.to_wire()
        and fact.order_type == expected_side
        and fact.quantity == approval.quantity.value
        and abs(fact.price - float(approval.limit_price.amount)) < 0.00001
    )


def execute_qmt_command_probe(
    transport: QmtCommandTransport,
    approval: QmtOrderPocApproval,
    report: QmtOrderPreflightReport,
    nonce_directory: Path,
    *,
    buy_type: int,
    sell_type: int,
    clock: Callable[[], ShanghaiTimestamp],
    elapsed: Callable[[], float] = monotonic,
    pause: Callable[[float], None] = sleep,
) -> QmtCommandProbeResult:
    if report.outcome is not QmtProbeOutcome.PASS or report.approval_id != approval.approval_id:
        raise QmtCommandError("QMT_PREFLIGHT_NOT_PASSED")
    if not approval.valid_from <= clock() <= approval.valid_until:
        raise QmtCommandError("QMT_APPROVAL_WINDOW_CLOSED")
    _reserve_nonce(nonce_directory, approval)
    submit_sequence: int | None = None
    broker_id: int | None = None
    cancel_sequence: int | None = None
    observed: QmtOrderFact | None = None
    try:
        transport.start()
        if clock() > approval.valid_until:
            raise QmtCommandError("QMT_APPROVAL_WINDOW_CLOSED")
        side = OrderSide.BUY if approval.side.value == "BUY" else OrderSide.SELL
        client_id = "ord_" + approval.single_use_nonce
        command = SubmitOrder(
            client_id,
            approval.symbol,
            side,
            approval.quantity,
            approval.limit_price,
            approval.order_remark,
        )
        transport.preflight(command)
        submit_sequence = transport.submit(command)
        submitted = elapsed()
        deadline = submitted + approval.cancel_after_seconds + 30
        while elapsed() < deadline:
            for fact in transport.drain():
                if fact.kind == "ORDER_RESPONSE" and fact.sequence == submit_sequence:
                    if fact.order_id is None or fact.order_id <= 0:
                        return QmtCommandProbeResult(
                            "UNKNOWN",
                            "QMT_ORDER_RESPONSE_INVALID",
                            submit_sequence,
                            None,
                            None,
                            None,
                            None,
                        )
                    if broker_id is not None and fact.order_id != broker_id:
                        return QmtCommandProbeResult(
                            "UNKNOWN",
                            "QMT_ORDER_ID_CONFLICT",
                            submit_sequence,
                            broker_id,
                            None,
                            None,
                            None,
                        )
                    broker_id = fact.order_id
                if fact.kind == "ORDER_ERROR":
                    return QmtCommandProbeResult(
                        "UNKNOWN",
                        "QMT_ORDER_ERROR_CALLBACK",
                        submit_sequence,
                        broker_id,
                        None,
                        None,
                        None,
                    )
            if broker_id is None:
                # The SDK callback is advisory and may be delayed or lost.  The
                # unique, preflight-checked remark lets the broker query repair
                # that gap without sending the order a second time.
                recovered = tuple(
                    fact
                    for fact in transport.query_orders()
                    if fact.remark == approval.order_remark
                )
                if len(recovered) > 1:
                    return QmtCommandProbeResult(
                        "UNKNOWN",
                        "QMT_ORDER_REMARK_AMBIGUOUS",
                        submit_sequence,
                        None,
                        None,
                        None,
                        None,
                    )
                if recovered:
                    broker_id = recovered[0].order_id
            if broker_id is not None:
                observed = transport.query_order(broker_id, approval.order_remark)
                if observed is not None:
                    if not _matches(observed, approval, buy_type, sell_type):
                        return QmtCommandProbeResult(
                            "UNKNOWN",
                            "QMT_ORDER_IDENTITY_CONFLICT",
                            submit_sequence,
                            broker_id,
                            cancel_sequence,
                            observed.status,
                            observed.filled,
                        )
                    if observed.status in _TERMINAL:
                        return QmtCommandProbeResult(
                            "ORDER_TERMINAL",
                            "QMT_TRADE_FACTS_REQUIRE_RECONCILIATION",
                            submit_sequence,
                            broker_id,
                            cancel_sequence,
                            observed.status,
                            observed.filled,
                        )
                    if (
                        elapsed() - submitted >= approval.cancel_after_seconds
                        and cancel_sequence is None
                    ):
                        cancel_sequence = transport.cancel(CancelOrder(client_id, str(broker_id)))
            pause(0.5)
        return QmtCommandProbeResult(
            "UNKNOWN",
            "QMT_ORDER_OR_CANCEL_UNCONFIRMED",
            submit_sequence,
            broker_id,
            cancel_sequence,
            observed.status if observed else None,
            observed.filled if observed else None,
        )
    except QmtCommandError as exc:
        return QmtCommandProbeResult(
            "UNKNOWN",
            exc.code,
            submit_sequence,
            broker_id,
            cancel_sequence,
            observed.status if observed else None,
            observed.filled if observed else None,
        )
    finally:
        transport.stop()
