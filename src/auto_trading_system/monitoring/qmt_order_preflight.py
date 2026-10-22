"""QMT-004 offline approval validation with no broker execution capability."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from auto_trading_system.domain import (
    Clock,
    Money,
    Price,
    Quantity,
    SecurityCode,
    ShanghaiTimestamp,
    SystemClock,
)
from auto_trading_system.monitoring.qmt_readonly import (
    QmtProbeOutcome,
    write_immutable_probe_payload,
)
from auto_trading_system.protocol.canonical_json import canonical_json_bytes

_MAX_APPROVAL_BYTES = 65_536
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CONFIG_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{16}$")
_APPROVAL_ID_PATTERN = re.compile(r"^QMT004-[0-9]{8}-[0-9]{4}$")
_IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$")
_TICKET_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{2,95}$")
_NONCE_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_REMARK_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,24}$")


class QmtOrderPreflightError(RuntimeError):
    """Stable approval or report failure without rejected values."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class QmtOrderSide(StrEnum):
    """Only the two directions that may be explicitly approved for the PoC."""

    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True, slots=True)
class QmtOrderPocApproval:
    approval_id: str
    revision: int
    single_use_nonce: str
    target_host_fingerprint: str
    target_account_fingerprint: str
    qmt001_report_sha256: str
    qmt002_report_sha256: str
    qmt003_report_sha256: str
    symbol: SecurityCode
    side: QmtOrderSide
    quantity: Quantity
    limit_price: Price
    maximum_notional: Money
    cancel_after_seconds: int
    valid_from: ShanghaiTimestamp
    valid_until: ShanghaiTimestamp
    operator_id: str
    reviewer_id: str
    change_ticket: str
    order_remark: str
    manual_supervision_confirmed: bool
    qmt_emergency_control_confirmed: bool
    single_order_only_confirmed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.approval_id, str):
            raise QmtOrderPreflightError(
                "QMT_ORDER_APPROVAL_FIELD_TYPE_INVALID", "approval id type is invalid"
            )
        if _APPROVAL_ID_PATTERN.fullmatch(self.approval_id) is None:
            raise QmtOrderPreflightError("QMT_ORDER_APPROVAL_ID_INVALID", "approval id is invalid")
        if self.revision != 1:
            raise QmtOrderPreflightError(
                "QMT_ORDER_APPROVAL_REVISION_INVALID", "approval revision must be one"
            )
        if (
            not isinstance(self.single_use_nonce, str)
            or _NONCE_PATTERN.fullmatch(self.single_use_nonce) is None
        ):
            raise QmtOrderPreflightError(
                "QMT_ORDER_APPROVAL_NONCE_INVALID", "single-use nonce is invalid"
            )
        for value in (self.target_host_fingerprint, self.target_account_fingerprint):
            if not isinstance(value, str) or _FINGERPRINT_PATTERN.fullmatch(value) is None:
                raise QmtOrderPreflightError(
                    "QMT_ORDER_APPROVAL_FINGERPRINT_INVALID", "approval fingerprint is invalid"
                )
        for value in (
            self.qmt001_report_sha256,
            self.qmt002_report_sha256,
            self.qmt003_report_sha256,
        ):
            if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
                raise QmtOrderPreflightError(
                    "QMT_ORDER_APPROVAL_EVIDENCE_HASH_INVALID",
                    "QMT prerequisite evidence hash is invalid",
                )
        if not isinstance(self.symbol, SecurityCode):
            raise QmtOrderPreflightError(
                "QMT_ORDER_APPROVAL_SYMBOL_INVALID", "approved symbol is invalid"
            )
        if not isinstance(self.side, QmtOrderSide):
            raise QmtOrderPreflightError(
                "QMT_ORDER_APPROVAL_SIDE_INVALID", "approved side is invalid"
            )
        if not isinstance(self.quantity, Quantity):
            raise QmtOrderPreflightError(
                "QMT_ORDER_APPROVAL_QUANTITY_INVALID", "approved quantity is invalid"
            )
        if self.quantity.is_zero:
            raise QmtOrderPreflightError(
                "QMT_ORDER_APPROVAL_QUANTITY_INVALID", "approved quantity must be positive"
            )
        if not isinstance(self.limit_price, Price) or not isinstance(self.maximum_notional, Money):
            raise QmtOrderPreflightError(
                "QMT_ORDER_APPROVAL_NOTIONAL_INVALID", "approved price or notional is invalid"
            )
        if type(self.cancel_after_seconds) is not int or not 5 <= self.cancel_after_seconds <= 120:
            raise QmtOrderPreflightError(
                "QMT_ORDER_APPROVAL_CANCEL_WINDOW_INVALID",
                "cancel deadline must be between 5 and 120 seconds",
            )
        if not isinstance(self.valid_from, ShanghaiTimestamp) or not isinstance(
            self.valid_until, ShanghaiTimestamp
        ):
            raise QmtOrderPreflightError(
                "QMT_ORDER_APPROVAL_TIME_WINDOW_INVALID", "approval timestamps are invalid"
            )
        window = self.valid_until.value - self.valid_from.value
        if window < timedelta(minutes=1) or window > timedelta(minutes=30):
            raise QmtOrderPreflightError(
                "QMT_ORDER_APPROVAL_TIME_WINDOW_INVALID",
                "approval window must be between 1 and 30 minutes",
            )
        if (
            not isinstance(self.operator_id, str)
            or not isinstance(self.reviewer_id, str)
            or _IDENTITY_PATTERN.fullmatch(self.operator_id) is None
            or _IDENTITY_PATTERN.fullmatch(self.reviewer_id) is None
            or hmac.compare_digest(self.operator_id, self.reviewer_id)
        ):
            raise QmtOrderPreflightError(
                "QMT_ORDER_APPROVAL_REVIEW_INVALID",
                "operator and independent reviewer must be distinct valid identities",
            )
        if (
            not isinstance(self.change_ticket, str)
            or _TICKET_PATTERN.fullmatch(self.change_ticket) is None
        ):
            raise QmtOrderPreflightError(
                "QMT_ORDER_APPROVAL_TICKET_INVALID", "change ticket is invalid"
            )
        if (
            not isinstance(self.order_remark, str)
            or _REMARK_PATTERN.fullmatch(self.order_remark) is None
            or not self.order_remark.isascii()
        ):
            raise QmtOrderPreflightError(
                "QMT_ORDER_APPROVAL_REMARK_INVALID", "order remark must be 1-24 ASCII characters"
            )
        if not all(
            type(value) is bool and value
            for value in (
                self.manual_supervision_confirmed,
                self.qmt_emergency_control_confirmed,
                self.single_order_only_confirmed,
            )
        ):
            raise QmtOrderPreflightError(
                "QMT_ORDER_APPROVAL_ACKNOWLEDGEMENT_MISSING",
                "all live PoC safety acknowledgements must be true",
            )
        if self.calculated_notional > self.maximum_notional.amount:
            raise QmtOrderPreflightError(
                "QMT_ORDER_APPROVAL_NOTIONAL_INCONSISTENT",
                "approved price and quantity exceed the approval cap",
            )

    @property
    def calculated_notional(self) -> Decimal:
        return self.limit_price.amount * self.quantity.value


class QmtOrderPreflightClock(Clock, Protocol):
    """Wall clock used to validate the short approval window."""


@dataclass(frozen=True, slots=True)
class QmtOrderPreflightReport:
    started_at: str
    completed_at: str
    outcome: QmtProbeOutcome
    failure_code: str | None
    approval_id: str
    approval_revision: int
    approval_sha256: str
    config_hash: str
    target_host_fingerprint: str
    target_account_fingerprint: str
    symbol_fingerprint: str
    side: str
    quantity: int
    limit_price: str
    calculated_notional: str
    approval_maximum_notional: str
    runtime_maximum_notional: str
    cancel_after_seconds: int
    valid_from: str
    valid_until: str
    operator_id: str
    reviewer_id: str
    change_ticket: str
    qmt001_report_sha256: str
    qmt002_report_sha256: str
    qmt003_report_sha256: str
    nonce_fingerprint: str
    execution_binding_sha256: str

    @property
    def exit_code(self) -> int:
        if self.outcome is QmtProbeOutcome.PASS:
            return 0
        if self.outcome is QmtProbeOutcome.BLOCKED:
            return 2
        return 3

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": "qmt-order-preflight-report/v1",
            "collection_mode": "OFFLINE_APPROVAL_VALIDATION",
            "work_item": "QMT-004",
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "outcome": self.outcome.value,
            "failure_code": self.failure_code,
            "approval": {
                "approval_id": self.approval_id,
                "revision": self.approval_revision,
                "approval_sha256": self.approval_sha256,
                "operator_id": self.operator_id,
                "reviewer_id": self.reviewer_id,
                "change_ticket": self.change_ticket,
                "valid_from": self.valid_from,
                "valid_until": self.valid_until,
                "nonce_fingerprint": self.nonce_fingerprint,
            },
            "target": {
                "host_fingerprint": self.target_host_fingerprint,
                "account_fingerprint": self.target_account_fingerprint,
                "symbol_fingerprint": self.symbol_fingerprint,
            },
            "order": {
                "side": self.side,
                "quantity": self.quantity,
                "limit_price": self.limit_price,
                "calculated_notional": self.calculated_notional,
                "approval_maximum_notional": self.approval_maximum_notional,
                "runtime_maximum_notional": self.runtime_maximum_notional,
                "cancel_after_seconds": self.cancel_after_seconds,
            },
            "prerequisite_evidence": {
                "qmt001_report_sha256": self.qmt001_report_sha256,
                "qmt002_report_sha256": self.qmt002_report_sha256,
                "qmt003_report_sha256": self.qmt003_report_sha256,
            },
            "config_hash": self.config_hash,
            "execution_binding_sha256": self.execution_binding_sha256,
            "live_order_submitted": False,
            "privacy": {
                "full_broker_account_omitted": True,
                "security_code_replaced_by_fingerprint": True,
                "broker_credentials_omitted": True,
                "single_use_nonce_replaced_by_fingerprint": True,
                "exception_messages_omitted": True,
            },
        }


_APPROVAL_KEYS = {
    "schema",
    "work_item",
    "approval_id",
    "revision",
    "single_use_nonce",
    "target_host_fingerprint",
    "target_account_fingerprint",
    "qmt001_report_sha256",
    "qmt002_report_sha256",
    "qmt003_report_sha256",
    "symbol",
    "side",
    "quantity",
    "limit_price",
    "maximum_notional",
    "cancel_after_seconds",
    "valid_from",
    "valid_until",
    "operator_id",
    "reviewer_id",
    "change_ticket",
    "order_remark",
    "manual_supervision_confirmed",
    "qmt_emergency_control_confirmed",
    "single_order_only_confirmed",
}


def load_qmt_order_poc_approval(
    path: Path,
    *,
    expected_sha256: str,
) -> tuple[QmtOrderPocApproval, str]:
    if not isinstance(path, Path) or not path.is_absolute():
        raise QmtOrderPreflightError(
            "QMT_ORDER_APPROVAL_PATH_INVALID", "approval path must be absolute"
        )
    if _SHA256_PATTERN.fullmatch(expected_sha256) is None:
        raise QmtOrderPreflightError(
            "QMT_ORDER_APPROVAL_EXPECTED_HASH_INVALID", "expected approval hash is invalid"
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise QmtOrderPreflightError(
            "QMT_ORDER_APPROVAL_READ_FAILED", "approval file cannot be read"
        ) from exc
    if not 1 <= len(raw) <= _MAX_APPROVAL_BYTES:
        raise QmtOrderPreflightError(
            "QMT_ORDER_APPROVAL_SIZE_INVALID", "approval file has an invalid size"
        )
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise QmtOrderPreflightError(
            "QMT_ORDER_APPROVAL_HASH_MISMATCH", "approval file hash does not match"
        )

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise QmtOrderPreflightError(
                    "QMT_ORDER_APPROVAL_DUPLICATE_KEY", "approval contains duplicate keys"
                )
            value[key] = item
        return value

    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except QmtOrderPreflightError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QmtOrderPreflightError(
            "QMT_ORDER_APPROVAL_JSON_INVALID", "approval is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != _APPROVAL_KEYS:
        raise QmtOrderPreflightError(
            "QMT_ORDER_APPROVAL_SHAPE_INVALID", "approval fields do not match the closed schema"
        )
    if payload["schema"] != "qmt-order-poc-approval/v1" or payload["work_item"] != "QMT-004":
        raise QmtOrderPreflightError(
            "QMT_ORDER_APPROVAL_PROTOCOL_INVALID", "approval protocol is invalid"
        )
    try:
        approval = QmtOrderPocApproval(
            approval_id=_string(payload, "approval_id"),
            revision=_integer(payload, "revision"),
            single_use_nonce=_string(payload, "single_use_nonce"),
            target_host_fingerprint=_string(payload, "target_host_fingerprint"),
            target_account_fingerprint=_string(payload, "target_account_fingerprint"),
            qmt001_report_sha256=_string(payload, "qmt001_report_sha256"),
            qmt002_report_sha256=_string(payload, "qmt002_report_sha256"),
            qmt003_report_sha256=_string(payload, "qmt003_report_sha256"),
            symbol=SecurityCode.parse(_string(payload, "symbol")),
            side=_order_side(payload, "side"),
            quantity=Quantity(_integer(payload, "quantity")),
            limit_price=Price.parse(_string(payload, "limit_price")),
            maximum_notional=Money.parse(_string(payload, "maximum_notional")),
            cancel_after_seconds=_integer(payload, "cancel_after_seconds"),
            valid_from=ShanghaiTimestamp.parse(_string(payload, "valid_from")),
            valid_until=ShanghaiTimestamp.parse(_string(payload, "valid_until")),
            operator_id=_string(payload, "operator_id"),
            reviewer_id=_string(payload, "reviewer_id"),
            change_ticket=_string(payload, "change_ticket"),
            order_remark=_string(payload, "order_remark"),
            manual_supervision_confirmed=_boolean(payload, "manual_supervision_confirmed"),
            qmt_emergency_control_confirmed=_boolean(payload, "qmt_emergency_control_confirmed"),
            single_order_only_confirmed=_boolean(payload, "single_order_only_confirmed"),
        )
    except QmtOrderPreflightError:
        raise
    except (TypeError, ValueError) as exc:
        raise QmtOrderPreflightError(
            "QMT_ORDER_APPROVAL_VALUE_INVALID", "approval contains an invalid value"
        ) from exc
    return approval, actual_sha256


def evaluate_qmt_order_preflight(
    approval: QmtOrderPocApproval,
    *,
    approval_sha256: str,
    config_hash: str,
    broker_account_id: str,
    host_fingerprint: str,
    runtime_maximum_notional: Money,
    clock: QmtOrderPreflightClock | None = None,
) -> QmtOrderPreflightReport:
    if not isinstance(approval, QmtOrderPocApproval):
        raise QmtOrderPreflightError(
            "QMT_ORDER_PREFLIGHT_APPROVAL_INVALID", "approval input is invalid"
        )
    if not isinstance(approval_sha256, str) or _SHA256_PATTERN.fullmatch(approval_sha256) is None:
        raise QmtOrderPreflightError(
            "QMT_ORDER_PREFLIGHT_APPROVAL_HASH_INVALID", "approval hash input is invalid"
        )
    if not isinstance(config_hash, str) or _CONFIG_HASH_PATTERN.fullmatch(config_hash) is None:
        raise QmtOrderPreflightError(
            "QMT_ORDER_PREFLIGHT_CONFIG_HASH_INVALID", "config hash input is invalid"
        )
    if (
        not isinstance(broker_account_id, str)
        or not 4 <= len(broker_account_id) <= 64
        or not broker_account_id.isascii()
        or not broker_account_id.isalnum()
    ):
        raise QmtOrderPreflightError(
            "QMT_ORDER_PREFLIGHT_ACCOUNT_INVALID", "broker account input is invalid"
        )
    if not isinstance(runtime_maximum_notional, Money):
        raise QmtOrderPreflightError(
            "QMT_ORDER_PREFLIGHT_RUNTIME_LIMIT_INVALID", "runtime notional limit is invalid"
        )
    effective_clock = SystemClock() if clock is None else clock
    started = effective_clock.now()
    if not isinstance(started, ShanghaiTimestamp):
        raise QmtOrderPreflightError(
            "QMT_ORDER_PREFLIGHT_CLOCK_INVALID", "preflight clock returned an invalid timestamp"
        )
    account_fingerprint = hashlib.sha256(broker_account_id.encode("utf-8")).hexdigest()[:16]
    outcome = QmtProbeOutcome.PASS
    failure_code: str | None = None
    if _FINGERPRINT_PATTERN.fullmatch(host_fingerprint) is None:
        outcome = QmtProbeOutcome.ERROR
        failure_code = "QMT_ORDER_HOST_FINGERPRINT_INVALID"
    elif not hmac.compare_digest(host_fingerprint, approval.target_host_fingerprint):
        outcome = QmtProbeOutcome.BLOCKED
        failure_code = "QMT_ORDER_HOST_MISMATCH"
    elif not hmac.compare_digest(account_fingerprint, approval.target_account_fingerprint):
        outcome = QmtProbeOutcome.BLOCKED
        failure_code = "QMT_ORDER_ACCOUNT_MISMATCH"
    elif approval.calculated_notional > runtime_maximum_notional.amount:
        outcome = QmtProbeOutcome.BLOCKED
        failure_code = "QMT_ORDER_RUNTIME_NOTIONAL_EXCEEDED"
    elif started < approval.valid_from:
        outcome = QmtProbeOutcome.BLOCKED
        failure_code = "QMT_ORDER_APPROVAL_NOT_YET_VALID"
    elif started > approval.valid_until:
        outcome = QmtProbeOutcome.BLOCKED
        failure_code = "QMT_ORDER_APPROVAL_EXPIRED"

    binding_payload = {
        "approval_sha256": approval_sha256,
        "config_hash": config_hash,
        "host_fingerprint": host_fingerprint,
        "account_fingerprint": account_fingerprint,
        "symbol": approval.symbol.to_wire(),
        "side": approval.side.value,
        "quantity": approval.quantity.value,
        "limit_price": approval.limit_price.to_wire(),
        "single_use_nonce": approval.single_use_nonce,
    }
    execution_binding = hashlib.sha256(
        canonical_json_bytes(binding_payload, exclude=frozenset())
    ).hexdigest()
    completed = effective_clock.now()
    if not isinstance(completed, ShanghaiTimestamp):
        raise QmtOrderPreflightError(
            "QMT_ORDER_PREFLIGHT_CLOCK_INVALID", "preflight clock returned an invalid timestamp"
        )
    return QmtOrderPreflightReport(
        started_at=started.to_wire(),
        completed_at=completed.to_wire(),
        outcome=outcome,
        failure_code=failure_code,
        approval_id=approval.approval_id,
        approval_revision=approval.revision,
        approval_sha256=approval_sha256,
        config_hash=config_hash,
        target_host_fingerprint=host_fingerprint,
        target_account_fingerprint=account_fingerprint,
        symbol_fingerprint=hashlib.sha256(approval.symbol.to_wire().encode("ascii")).hexdigest()[
            :16
        ],
        side=approval.side.value,
        quantity=approval.quantity.value,
        limit_price=approval.limit_price.to_wire(),
        calculated_notional=format(approval.calculated_notional, "f"),
        approval_maximum_notional=approval.maximum_notional.to_wire(),
        runtime_maximum_notional=runtime_maximum_notional.to_wire(),
        cancel_after_seconds=approval.cancel_after_seconds,
        valid_from=approval.valid_from.to_wire(),
        valid_until=approval.valid_until.to_wire(),
        operator_id=approval.operator_id,
        reviewer_id=approval.reviewer_id,
        change_ticket=approval.change_ticket,
        qmt001_report_sha256=approval.qmt001_report_sha256,
        qmt002_report_sha256=approval.qmt002_report_sha256,
        qmt003_report_sha256=approval.qmt003_report_sha256,
        nonce_fingerprint=hashlib.sha256(approval.single_use_nonce.encode("ascii")).hexdigest()[
            :16
        ],
        execution_binding_sha256=execution_binding,
    )


def write_qmt_order_preflight_report(
    destination: Path,
    report: QmtOrderPreflightReport,
    *,
    forbidden_values: tuple[str, ...] = (),
) -> str:
    return write_immutable_probe_payload(
        destination,
        report.to_payload(),
        forbidden_values=forbidden_values,
    )


def _string(payload: Mapping[str, object], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str):
        raise QmtOrderPreflightError(
            "QMT_ORDER_APPROVAL_FIELD_TYPE_INVALID", "approval string field is invalid"
        )
    return value


def _integer(payload: Mapping[str, object], key: str) -> int:
    value = payload[key]
    if type(value) is not int:
        raise QmtOrderPreflightError(
            "QMT_ORDER_APPROVAL_FIELD_TYPE_INVALID", "approval integer field is invalid"
        )
    return value


def _boolean(payload: Mapping[str, object], key: str) -> bool:
    value = payload[key]
    if type(value) is not bool:
        raise QmtOrderPreflightError(
            "QMT_ORDER_APPROVAL_FIELD_TYPE_INVALID", "approval boolean field is invalid"
        )
    return value


def _order_side(payload: Mapping[str, object], key: str) -> QmtOrderSide:
    try:
        return QmtOrderSide(_string(payload, key))
    except ValueError as exc:
        raise QmtOrderPreflightError(
            "QMT_ORDER_APPROVAL_SIDE_INVALID", "approved side is invalid"
        ) from exc
