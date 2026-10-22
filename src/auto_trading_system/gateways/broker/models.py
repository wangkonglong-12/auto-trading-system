"""Broker-neutral commands, facts, and callback events."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

from auto_trading_system.domain import (
    DomainValueError,
    Money,
    Price,
    Quantity,
    SecurityCode,
    ShanghaiTimestamp,
)

_CLIENT_ORDER_PATTERN = re.compile(r"ord_[A-Za-z0-9_-]{8,64}")
_BROKER_REFERENCE_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_TRADE_ID_PATTERN = re.compile(r"trade_[A-Za-z0-9_.:-]{4,80}")
_REMARK_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,24}")
_REASON_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{2,63}")


def _require_pattern(value: str, pattern: re.Pattern[str], *, field: str) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise DomainValueError("INVALID_BROKER_VALUE", field, "has an invalid format")


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class BrokerOrderStatus(StrEnum):
    SUBMITTING = "SUBMITTING"
    ACKED = "ACKED"
    CANCEL_PENDING = "CANCEL_PENDING"
    PARTIAL_FILL_CANCEL_PENDING = "PARTIAL_FILL_CANCEL_PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    PARTIALLY_FILLED_CANCELLED = "PARTIALLY_FILLED_CANCELLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    ERROR_HOLD = "ERROR_HOLD"


TERMINAL_ORDER_STATUSES = frozenset(
    {
        BrokerOrderStatus.PARTIALLY_FILLED_CANCELLED,
        BrokerOrderStatus.FILLED,
        BrokerOrderStatus.CANCELLED,
        BrokerOrderStatus.REJECTED,
    }
)


@dataclass(frozen=True, slots=True)
class SubmitOrder:
    client_order_id: str
    symbol: SecurityCode
    side: OrderSide
    quantity: Quantity
    limit_price: Price
    remark_token: str

    def __post_init__(self) -> None:
        _require_pattern(self.client_order_id, _CLIENT_ORDER_PATTERN, field="client_order_id")
        _require_pattern(self.remark_token, _REMARK_PATTERN, field="remark_token")
        if self.quantity.is_zero:
            raise DomainValueError("INVALID_ORDER_QUANTITY", "quantity", "must be positive")


@dataclass(frozen=True, slots=True)
class CancelOrder:
    client_order_id: str
    broker_order_id: str

    def __post_init__(self) -> None:
        _require_pattern(self.client_order_id, _CLIENT_ORDER_PATTERN, field="client_order_id")
        _require_pattern(self.broker_order_id, _BROKER_REFERENCE_PATTERN, field="broker_order_id")


@dataclass(frozen=True, slots=True)
class SubmissionReceipt:
    request_seq: int
    submitted_at: ShanghaiTimestamp

    def __post_init__(self) -> None:
        if type(self.request_seq) is not int or self.request_seq <= 0:
            raise DomainValueError(
                "INVALID_REQUEST_SEQUENCE", "request_seq", "must be a positive integer"
            )


@dataclass(frozen=True, slots=True)
class CancellationReceipt:
    request_seq: int
    submitted_at: ShanghaiTimestamp

    def __post_init__(self) -> None:
        if type(self.request_seq) is not int or self.request_seq <= 0:
            raise DomainValueError(
                "INVALID_REQUEST_SEQUENCE", "request_seq", "must be a positive integer"
            )


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    client_order_id: str
    broker_order_id: str | None
    symbol: SecurityCode
    side: OrderSide
    status: BrokerOrderStatus
    order_quantity: Quantity
    filled_quantity: Quantity
    remaining_quantity: Quantity
    limit_price: Price
    broker_status_code: int | None
    status_reason: str
    updated_at: ShanghaiTimestamp

    def __post_init__(self) -> None:
        _require_pattern(self.client_order_id, _CLIENT_ORDER_PATTERN, field="client_order_id")
        if self.broker_order_id is not None:
            _require_pattern(
                self.broker_order_id,
                _BROKER_REFERENCE_PATTERN,
                field="broker_order_id",
            )
        _require_pattern(self.status_reason, _REASON_PATTERN, field="status_reason")
        if self.order_quantity.is_zero:
            raise DomainValueError("INVALID_ORDER_QUANTITY", "order_quantity", "must be positive")
        if self.filled_quantity.value + self.remaining_quantity.value != self.order_quantity.value:
            raise DomainValueError(
                "ORDER_QUANTITY_MISMATCH",
                "filled_quantity",
                "filled plus remaining must equal order quantity",
            )
        if self.broker_status_code is not None and (
            type(self.broker_status_code) is not int or self.broker_status_code < 0
        ):
            raise DomainValueError(
                "INVALID_BROKER_STATUS_CODE",
                "broker_status_code",
                "must be a non-negative integer",
            )

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_ORDER_STATUSES


@dataclass(frozen=True, slots=True)
class BrokerOrderUpdate:
    order: BrokerOrder


@dataclass(frozen=True, slots=True)
class BrokerTrade:
    trade_id: str
    client_order_id: str
    broker_order_id: str
    symbol: SecurityCode
    side: OrderSide
    price: Price
    quantity: Quantity
    notional: Money
    traded_at: ShanghaiTimestamp

    def __post_init__(self) -> None:
        _require_pattern(self.trade_id, _TRADE_ID_PATTERN, field="trade_id")
        _require_pattern(self.client_order_id, _CLIENT_ORDER_PATTERN, field="client_order_id")
        _require_pattern(self.broker_order_id, _BROKER_REFERENCE_PATTERN, field="broker_order_id")
        if self.quantity.is_zero:
            raise DomainValueError("INVALID_TRADE_QUANTITY", "quantity", "must be positive")
        if self.notional.amount.is_zero():
            raise DomainValueError("INVALID_TRADE_NOTIONAL", "notional", "must be positive")


@dataclass(frozen=True, slots=True)
class BrokerCancelError:
    client_order_id: str
    broker_order_id: str
    error_code: str
    message: str
    occurred_at: ShanghaiTimestamp

    def __post_init__(self) -> None:
        _require_pattern(self.client_order_id, _CLIENT_ORDER_PATTERN, field="client_order_id")
        _require_pattern(self.broker_order_id, _BROKER_REFERENCE_PATTERN, field="broker_order_id")
        _require_pattern(self.error_code, _REASON_PATTERN, field="error_code")
        if not isinstance(self.message, str) or not 1 <= len(self.message) <= 256:
            raise DomainValueError(
                "INVALID_ERROR_MESSAGE", "message", "must contain between 1 and 256 characters"
            )


BrokerEvent: TypeAlias = BrokerOrderUpdate | BrokerTrade | BrokerCancelError
