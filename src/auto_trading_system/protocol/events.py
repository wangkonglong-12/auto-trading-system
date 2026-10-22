"""Semantic checks for immutable domain events and event-upload batches."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any, NoReturn, cast
from uuid import uuid4

from auto_trading_system.protocol.canonical_json import verify_event_hash
from auto_trading_system.protocol.compat import ProtocolVersionError, require_supported_major

SHANGHAI_OFFSET = timedelta(hours=8)
MAX_LOCAL_SEQUENCE = 9_223_372_036_854_775_807

EVENT_AGGREGATES: dict[str, tuple[str, str | None]] = {
    "RUNTIME_STATE_CHANGED": ("RUNTIME", None),
    "PLAN_STATUS_CHANGED": ("PLAN", "plan_id"),
    "ORDER_INTENT_CREATED": ("ORDER", "client_order_id"),
    "ORDER_STATUS_CHANGED": ("ORDER", "client_order_id"),
    "TRADE_RECORDED": ("TRADE", "trade_id"),
    "RISK_DECISION": ("RISK", "decision_id"),
    "CONTROL_COMMAND_APPLIED": ("CONTROL", "command_id"),
    "RECONCILIATION_DISCREPANCY": ("RECONCILIATION", "reconciliation_id"),
    "ALERT_RAISED": ("ALERT", "alert_id"),
}


class EventSemanticError(ValueError):
    """Raised when a schema-valid event violates a cross-field invariant."""

    def __init__(self, code: str, field: str, message: str) -> None:
        super().__init__(f"{code} at {field}: {message}")
        self.code = code
        self.field = field


def _fail(code: str, field: str, message: str) -> NoReturn:
    raise EventSemanticError(code, field, message)


def new_event_id() -> str:
    """Create an opaque event id that remains stable across upload retries."""
    return f"evt_{uuid4().hex}"


def new_boot_id() -> str:
    """Create a new process-incarnation id at each Runtime start."""
    return f"boot_{uuid4().hex}"


def new_request_id() -> str:
    """Create an event-batch transport request id."""
    return f"req_{uuid4().hex}"


def _require_supported_version(container: Mapping[str, Any], field: str) -> None:
    version = container.get("schema_version")
    if not isinstance(version, str):
        _fail("INVALID_SCHEMA_VERSION", field, "must be a string")
    try:
        require_supported_major(version)
    except ProtocolVersionError as exc:
        raise EventSemanticError("EVENT_VERSION_UNSUPPORTED", field, str(exc)) from exc


def _parse_shanghai_timestamp(event: Mapping[str, Any], field: str) -> datetime:
    raw = event.get(field)
    if not isinstance(raw, str):
        _fail("INVALID_TIMESTAMP", field, "must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise EventSemanticError("INVALID_TIMESTAMP", field, str(exc)) from exc
    if parsed.utcoffset() != SHANGHAI_OFFSET:
        _fail("INVALID_TIMEZONE_OFFSET", field, "must use +08:00")
    return parsed


def _parse_trade_date(event: Mapping[str, Any]) -> date:
    raw = event.get("trade_date")
    if not isinstance(raw, str):
        _fail("INVALID_TRADE_DATE", "trade_date", "must be an ISO date string")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise EventSemanticError("INVALID_TRADE_DATE", "trade_date", str(exc)) from exc


def _validate_aggregate_binding(event: Mapping[str, Any]) -> None:
    event_type = event.get("event_type")
    if not isinstance(event_type, str) or event_type not in EVENT_AGGREGATES:
        _fail("EVENT_TYPE_UNSUPPORTED", "event_type", "event type is not supported")
    expected_aggregate_type, payload_id_field = EVENT_AGGREGATES[event_type]
    if event.get("aggregate_type") != expected_aggregate_type:
        _fail(
            "AGGREGATE_TYPE_MISMATCH",
            "aggregate_type",
            f"{event_type} requires {expected_aggregate_type}",
        )

    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        _fail("INVALID_EVENT_PAYLOAD", "payload", "must be an object")
    expected_aggregate_id = (
        event.get("node_id") if payload_id_field is None else payload.get(payload_id_field)
    )
    if (
        not isinstance(expected_aggregate_id, str)
        or event.get("aggregate_id") != expected_aggregate_id
    ):
        _fail(
            "AGGREGATE_ID_MISMATCH",
            "aggregate_id",
            "must identify the event payload aggregate",
        )


def _validate_payload_invariants(event: Mapping[str, Any]) -> None:
    event_type = event["event_type"]
    payload = cast(Mapping[str, Any], event["payload"])

    if event_type == "ORDER_STATUS_CHANGED":
        order_quantity = payload.get("order_quantity")
        filled_quantity = payload.get("filled_quantity")
        remaining_quantity = payload.get("remaining_quantity")
        quantities = (order_quantity, filled_quantity, remaining_quantity)
        if not all(type(value) is int for value in quantities):
            _fail("INVALID_ORDER_QUANTITY", "payload", "order quantities must be integers")
        order_quantity = cast(int, order_quantity)
        filled_quantity = cast(int, filled_quantity)
        remaining_quantity = cast(int, remaining_quantity)
        if filled_quantity + remaining_quantity != order_quantity:
            _fail(
                "ORDER_QUANTITY_MISMATCH",
                "payload.filled_quantity",
                "filled plus remaining quantity must equal order quantity",
            )

    if event_type == "RECONCILIATION_DISCREPANCY" and not (
        payload.get("local_reference") or payload.get("broker_reference")
    ):
        _fail(
            "RECONCILIATION_REFERENCE_MISSING",
            "payload",
            "at least one local or broker reference is required",
        )


def validate_domain_event_semantics(event: Mapping[str, Any]) -> None:
    """Validate version, time, identity, payload and digest invariants."""
    _require_supported_version(event, "schema_version")
    occurred_at = _parse_shanghai_timestamp(event, "occurred_at")
    recorded_at = _parse_shanghai_timestamp(event, "recorded_at")
    trade_date = _parse_trade_date(event)
    if recorded_at < occurred_at:
        _fail("EVENT_TIME_REGRESSION", "recorded_at", "must not precede occurred_at")
    if occurred_at.date() != trade_date:
        _fail("EVENT_TRADE_DATE_MISMATCH", "trade_date", "must match occurred_at date")

    local_seq = event.get("local_seq")
    if type(local_seq) is not int or not 1 <= local_seq <= MAX_LOCAL_SEQUENCE:
        _fail("INVALID_LOCAL_SEQUENCE", "local_seq", "must be a positive 64-bit integer")

    _validate_aggregate_binding(event)
    _validate_payload_invariants(event)
    if not verify_event_hash(event):
        _fail("EVENT_HASH_MISMATCH", "event_hash", "digest does not match event content")


def validate_event_batch_semantics(batch: Mapping[str, Any]) -> None:
    """Validate an upload batch without requiring delivery order."""
    _require_supported_version(batch, "schema_version")
    events = batch.get("events")
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        _fail("INVALID_EVENT_BATCH", "events", "must be an array")
    if not 1 <= len(events) <= 50:
        _fail("INVALID_EVENT_BATCH_SIZE", "events", "must contain between 1 and 50 events")

    event_ids: set[str] = set()
    sequence_keys: set[tuple[str, int]] = set()
    for index, event in enumerate(events):
        if not isinstance(event, Mapping):
            _fail("INVALID_EVENT", f"events[{index}]", "must be an object")
        validate_domain_event_semantics(event)
        if event.get("node_id") != batch.get("node_id"):
            _fail("BATCH_NODE_MISMATCH", f"events[{index}].node_id", "must match batch node_id")
        if event.get("account_id") != batch.get("account_id"):
            _fail(
                "BATCH_ACCOUNT_MISMATCH",
                f"events[{index}].account_id",
                "must match batch account_id",
            )

        event_id = event.get("event_id")
        boot_id = event.get("boot_id")
        local_seq = event.get("local_seq")
        if not isinstance(event_id, str) or event_id in event_ids:
            _fail("DUPLICATE_EVENT_ID", f"events[{index}].event_id", "must be unique in batch")
        if not isinstance(boot_id, str) or type(local_seq) is not int:
            _fail("INVALID_EVENT_SEQUENCE", f"events[{index}]", "boot_id/local_seq are invalid")
        sequence_key = (boot_id, local_seq)
        if sequence_key in sequence_keys:
            _fail(
                "DUPLICATE_LOCAL_SEQUENCE",
                f"events[{index}].local_seq",
                "must be unique within each boot_id",
            )
        event_ids.add(event_id)
        sequence_keys.add(sequence_key)


def validate_event_batch_result_semantics(
    result: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
) -> None:
    """Ensure a per-item batch result corresponds exactly to its request."""
    _require_supported_version(result, "schema_version")
    if result.get("request_id") != request.get("request_id"):
        _fail("BATCH_RESULT_REQUEST_MISMATCH", "request_id", "must match the request")
    requested_events = request.get("events")
    receipts = result.get("results")
    if not isinstance(requested_events, Sequence) or isinstance(requested_events, (str, bytes)):
        _fail("INVALID_EVENT_BATCH", "request.events", "must be an array")
    if not isinstance(receipts, Sequence) or isinstance(receipts, (str, bytes)):
        _fail("INVALID_BATCH_RESULT", "results", "must be an array")

    expected = {
        (event.get("event_id"), event.get("event_hash"))
        for event in requested_events
        if isinstance(event, Mapping)
    }
    actual: set[tuple[Any, Any]] = set()
    status_counts = {"ACCEPTED": 0, "DUPLICATE": 0, "REJECTED": 0}
    persisted_identities: list[tuple[Any, Any]] = []
    request_sequences = {
        (event.get("event_id"), event.get("event_hash")): event.get("local_seq")
        for event in requested_events
        if isinstance(event, Mapping)
    }
    for index, receipt in enumerate(receipts):
        if not isinstance(receipt, Mapping):
            _fail("INVALID_EVENT_RECEIPT", f"results[{index}]", "must be an object")
        identity = (receipt.get("event_id"), receipt.get("event_hash"))
        if identity in actual:
            _fail("DUPLICATE_EVENT_RECEIPT", f"results[{index}]", "receipt is duplicated")
        actual.add(identity)
        status = receipt.get("status")
        if status not in status_counts:
            _fail("INVALID_EVENT_RECEIPT", f"results[{index}].status", "status is invalid")
        status_counts[status] += 1
        if status in {"ACCEPTED", "DUPLICATE"}:
            persisted_identities.append(identity)
    if actual != expected:
        _fail("INCOMPLETE_BATCH_RESULT", "results", "must contain exactly one receipt per event")

    persisted_sequence_values = [request_sequences[identity] for identity in persisted_identities]
    if not all(type(local_seq) is int for local_seq in persisted_sequence_values):
        _fail("INVALID_EVENT_SEQUENCE", "request.events", "local_seq must be an integer")
    persisted_sequences = [cast(int, value) for value in persisted_sequence_values]

    for field, status in (
        ("accepted", "ACCEPTED"),
        ("duplicates", "DUPLICATE"),
        ("rejected", "REJECTED"),
    ):
        if result.get(field) != status_counts[status]:
            _fail("BATCH_RESULT_COUNT_MISMATCH", field, "must match per-event receipts")
    expected_watermark = max(persisted_sequences, default=0)
    if result.get("high_watermark") != expected_watermark:
        _fail(
            "BATCH_RESULT_WATERMARK_MISMATCH",
            "high_watermark",
            "must be the maximum accepted or duplicate local_seq in this batch",
        )
