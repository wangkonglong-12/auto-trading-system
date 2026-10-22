"""Semantic validation for snapshots, reconciliation, heartbeat, and commands."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any, NoReturn, cast

from auto_trading_system.protocol.compat import ProtocolVersionError, require_supported_major

SHANGHAI_OFFSET = timedelta(hours=8)
EXPECTED_RECONCILIATION_INPUTS = {"ASSET", "POSITION", "ORDER", "TRADE"}
COMMAND_SCOPES: dict[str, frozenset[str]] = {
    "PAUSE_BUY": frozenset({"NODE", "ACCOUNT"}),
    "SELL_ONLY": frozenset({"NODE", "ACCOUNT"}),
    "MANUAL_TAKEOVER": frozenset({"NODE", "ACCOUNT"}),
    "EMERGENCY_HALT": frozenset({"NODE", "ACCOUNT"}),
    "REQUEST_RECONCILIATION": frozenset({"NODE", "ACCOUNT", "SYMBOL", "ORDER"}),
    "CANCEL_ORDER": frozenset({"ORDER"}),
    "CANCEL_ACTIVE_ORDERS": frozenset({"NODE", "ACCOUNT"}),
    "FREEZE_SYMBOL": frozenset({"SYMBOL"}),
    "UNFREEZE_SYMBOL": frozenset({"SYMBOL"}),
    "REQUEST_AUTO_RESUME": frozenset({"NODE", "ACCOUNT"}),
}


class NodeMessageSemanticError(ValueError):
    """Raised when a schema-valid node message violates a cross-field invariant."""

    def __init__(self, code: str, field: str, message: str) -> None:
        super().__init__(f"{code} at {field}: {message}")
        self.code = code
        self.field = field


def _fail(code: str, field: str, message: str) -> NoReturn:
    raise NodeMessageSemanticError(code, field, message)


def _require_supported_version(message: Mapping[str, Any]) -> None:
    value = message.get("schema_version")
    if not isinstance(value, str):
        _fail("INVALID_SCHEMA_VERSION", "schema_version", "must be a string")
    try:
        require_supported_major(value)
    except ProtocolVersionError as exc:
        raise NodeMessageSemanticError(
            "PROTOCOL_VERSION_UNSUPPORTED", "schema_version", str(exc)
        ) from exc


def _parse_shanghai_datetime(message: Mapping[str, Any], field: str) -> datetime:
    raw = message.get(field)
    if not isinstance(raw, str):
        _fail("INVALID_TIMESTAMP", field, "must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise NodeMessageSemanticError("INVALID_TIMESTAMP", field, str(exc)) from exc
    if parsed.utcoffset() != SHANGHAI_OFFSET:
        _fail("INVALID_TIMEZONE_OFFSET", field, "must use +08:00")
    return parsed


def _parse_trade_date(message: Mapping[str, Any]) -> date:
    raw = message.get("trade_date")
    if not isinstance(raw, str):
        _fail("INVALID_TRADE_DATE", "trade_date", "must be an ISO date string")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise NodeMessageSemanticError("INVALID_TRADE_DATE", "trade_date", str(exc)) from exc


def _require_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("INVALID_OBJECT", field, "must be an object")
    return cast(Mapping[str, Any], value)


def _require_sequence(value: object, field: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        _fail("INVALID_ARRAY", field, "must be an array")
    return value


def _validate_snapshot_header(snapshot: Mapping[str, Any]) -> tuple[date, datetime, str]:
    _require_supported_version(snapshot)
    trade_date = _parse_trade_date(snapshot)
    as_of = _parse_shanghai_datetime(snapshot, "as_of")
    if as_of.date() != trade_date:
        _fail("SNAPSHOT_TRADE_DATE_MISMATCH", "trade_date", "must match as_of date")

    query_status = snapshot.get("query_status")
    if query_status not in {"AVAILABLE", "QUERY_FAILED", "UNKNOWN"}:
        _fail("INVALID_QUERY_STATUS", "query_status", "is not supported")
    return trade_date, as_of, cast(str, query_status)


def _validate_query_payload(
    message: Mapping[str, Any],
    *,
    query_status: str,
    payload_field: str,
) -> None:
    has_payload = payload_field in message
    has_error = "error" in message
    if query_status == "AVAILABLE":
        if not has_payload or has_error:
            _fail(
                "AVAILABLE_QUERY_PAYLOAD_INVALID",
                payload_field,
                "available result requires data and forbids an error",
            )
        return
    if has_payload or not has_error:
        _fail(
            "FAILED_QUERY_PAYLOAD_INVALID",
            payload_field,
            "unavailable result requires an error and forbids data",
        )
    _require_mapping(message["error"], "error")


def validate_asset_snapshot_semantics(snapshot: Mapping[str, Any]) -> None:
    """Validate an asset snapshot without treating query failure as zero assets."""
    _, _, query_status = _validate_snapshot_header(snapshot)
    _validate_query_payload(snapshot, query_status=query_status, payload_field="asset")
    if query_status == "AVAILABLE":
        _require_mapping(snapshot["asset"], "asset")


def validate_position_snapshot_semantics(snapshot: Mapping[str, Any]) -> None:
    """Validate a position snapshot and per-symbol quantity invariants."""
    _, _, query_status = _validate_snapshot_header(snapshot)
    _validate_query_payload(snapshot, query_status=query_status, payload_field="positions")
    if query_status != "AVAILABLE":
        return

    positions = _require_sequence(snapshot["positions"], "positions")
    symbols: set[str] = set()
    for index, raw_position in enumerate(positions):
        position = _require_mapping(raw_position, f"positions[{index}]")
        symbol = position.get("symbol")
        if not isinstance(symbol, str) or symbol in symbols:
            _fail("DUPLICATE_POSITION", f"positions[{index}].symbol", "must be unique")
        symbols.add(symbol)

        volume = position.get("volume")
        components = {
            "can_use_volume": position.get("can_use_volume"),
            "frozen_volume": position.get("frozen_volume"),
            "yesterday_volume": position.get("yesterday_volume"),
        }
        if type(volume) is not int or volume <= 0:
            _fail("INVALID_POSITION_VOLUME", f"positions[{index}].volume", "must be positive")
        for field, value in components.items():
            if type(value) is not int or value < 0 or value > volume:
                _fail(
                    "POSITION_QUANTITY_EXCEEDS_VOLUME",
                    f"positions[{index}].{field}",
                    "must be a non-negative integer no greater than volume",
                )


def validate_reconciliation_report_semantics(report: Mapping[str, Any]) -> None:
    """Validate completeness and outcome consistency of a broker reconciliation."""
    _require_supported_version(report)
    trade_date = _parse_trade_date(report)
    started_at = _parse_shanghai_datetime(report, "started_at")
    completed_at = _parse_shanghai_datetime(report, "completed_at")
    if started_at.date() != trade_date or completed_at.date() != trade_date:
        _fail("RECONCILIATION_TRADE_DATE_MISMATCH", "trade_date", "must match report times")
    if completed_at < started_at:
        _fail("RECONCILIATION_TIME_REGRESSION", "completed_at", "must not precede started_at")

    inputs = _require_sequence(report.get("inputs"), "inputs")
    input_kinds: set[str] = set()
    has_unknown_input = False
    for index, raw_input in enumerate(inputs):
        item = _require_mapping(raw_input, f"inputs[{index}]")
        kind = item.get("kind")
        if not isinstance(kind, str) or kind in input_kinds:
            _fail("DUPLICATE_RECONCILIATION_INPUT", f"inputs[{index}].kind", "must be unique")
        input_kinds.add(kind)
        input_time = _parse_shanghai_datetime(item, "as_of")
        if input_time.date() != trade_date or input_time > completed_at:
            _fail(
                "INVALID_RECONCILIATION_INPUT_TIME",
                f"inputs[{index}].as_of",
                "is outside the report",
            )
        query_status = item.get("query_status")
        if query_status == "AVAILABLE":
            if "reference_id" not in item or "error" in item:
                _fail("AVAILABLE_INPUT_INVALID", f"inputs[{index}]", "requires a reference only")
        elif query_status in {"QUERY_FAILED", "UNKNOWN"}:
            has_unknown_input = True
            if "error" not in item or "reference_id" in item:
                _fail("UNAVAILABLE_INPUT_INVALID", f"inputs[{index}]", "requires an error only")
        else:
            _fail("INVALID_QUERY_STATUS", f"inputs[{index}].query_status", "is not supported")
    if input_kinds != EXPECTED_RECONCILIATION_INPUTS:
        _fail("INCOMPLETE_RECONCILIATION_INPUTS", "inputs", "must contain each required input once")

    discrepancies = _require_sequence(report.get("discrepancies"), "discrepancies")
    discrepancy_ids: set[str] = set()
    discrepancy_subjects: set[tuple[object, object, object]] = set()
    open_count = 0
    critical_count = 0
    for index, raw_discrepancy in enumerate(discrepancies):
        discrepancy = _require_mapping(raw_discrepancy, f"discrepancies[{index}]")
        discrepancy_id = discrepancy.get("discrepancy_id")
        if not isinstance(discrepancy_id, str) or discrepancy_id in discrepancy_ids:
            _fail(
                "DUPLICATE_DISCREPANCY", f"discrepancies[{index}].discrepancy_id", "must be unique"
            )
        discrepancy_ids.add(discrepancy_id)
        subject = (
            discrepancy.get("category"),
            discrepancy.get("subject_key"),
            discrepancy.get("field"),
        )
        if subject in discrepancy_subjects:
            _fail(
                "DUPLICATE_DISCREPANCY_SUBJECT",
                f"discrepancies[{index}]",
                "category, subject_key and field must be unique",
            )
        discrepancy_subjects.add(subject)
        open_count += discrepancy.get("resolution_status") == "OPEN"
        critical_count += discrepancy.get("severity") == "CRITICAL"

    status = report.get("status")
    if status == "MATCHED" and (has_unknown_input or discrepancies):
        _fail(
            "MATCHED_RECONCILIATION_INVALID",
            "status",
            "requires complete inputs and no differences",
        )
    if status == "DISCREPANCIES" and (has_unknown_input or not discrepancies):
        _fail(
            "DISCREPANCY_RECONCILIATION_INVALID",
            "status",
            "requires complete inputs and differences",
        )
    if status == "INDETERMINATE" and not has_unknown_input:
        _fail("INDETERMINATE_RECONCILIATION_INVALID", "status", "requires an unavailable input")
    if status not in {"MATCHED", "DISCREPANCIES", "INDETERMINATE"}:
        _fail("INVALID_RECONCILIATION_STATUS", "status", "is not supported")

    summary = _require_mapping(report.get("summary"), "summary")
    expected = {
        "total_discrepancies": len(discrepancies),
        "open_discrepancies": open_count,
        "critical_discrepancies": critical_count,
    }
    for field, count in expected.items():
        if summary.get(field) != count:
            _fail("RECONCILIATION_SUMMARY_MISMATCH", f"summary.{field}", f"must equal {count}")


def validate_runtime_heartbeat_semantics(heartbeat: Mapping[str, Any]) -> None:
    """Validate heartbeat counters and AUTO safety preconditions."""
    _require_supported_version(heartbeat)
    trade_date = _parse_trade_date(heartbeat)
    sent_at = _parse_shanghai_datetime(heartbeat, "sent_at")
    if sent_at.date() != trade_date:
        _fail("HEARTBEAT_TRADE_DATE_MISMATCH", "trade_date", "must match sent_at date")
    heartbeat_seq = heartbeat.get("heartbeat_seq")
    if type(heartbeat_seq) is not int or heartbeat_seq <= 0:
        _fail("INVALID_HEARTBEAT_SEQUENCE", "heartbeat_seq", "must be positive")

    health = _require_mapping(heartbeat.get("health"), "health")
    for prefix in ("event_queue", "writer_queue"):
        depth = health.get(f"{prefix}_depth")
        capacity = health.get(f"{prefix}_capacity")
        if type(depth) is not int or type(capacity) is not int or not 0 <= depth <= capacity:
            _fail(
                "QUEUE_DEPTH_EXCEEDS_CAPACITY", f"health.{prefix}_depth", "must not exceed capacity"
            )

    outbox_pending = health.get("outbox_pending")
    oldest_outbox = health.get("oldest_outbox_at")
    if type(outbox_pending) is not int or outbox_pending < 0:
        _fail("INVALID_OUTBOX_COUNT", "health.outbox_pending", "must be non-negative")
    if (outbox_pending == 0) != (oldest_outbox is None):
        _fail(
            "OUTBOX_AGE_MISMATCH",
            "health.oldest_outbox_at",
            "must be null exactly when outbox is empty",
        )
    if oldest_outbox is not None:
        oldest_at = _parse_shanghai_datetime(health, "oldest_outbox_at")
        if oldest_at > sent_at:
            _fail("OUTBOX_TIME_IN_FUTURE", "health.oldest_outbox_at", "must not follow sent_at")

    if heartbeat.get("runtime_state") == "AUTO":
        connectivity = _require_mapping(heartbeat.get("connectivity"), "connectivity")
        required_health = {
            "connectivity.qmt": (connectivity.get("qmt"), "CONNECTED"),
            "connectivity.broker_account": (connectivity.get("broker_account"), "OK"),
            "connectivity.market_data": (connectivity.get("market_data"), "HEALTHY"),
            "health.journal": (health.get("journal"), "HEALTHY"),
        }
        for field, (actual, required) in required_health.items():
            if actual != required:
                _fail("AUTO_HEALTH_PRECONDITION_FAILED", field, f"must be {required}")


def validate_runtime_command_semantics(command: Mapping[str, Any]) -> None:
    """Validate command lifetime, target identity, and type-specific scope."""
    _require_supported_version(command)
    issued_at = _parse_shanghai_datetime(command, "issued_at")
    expires_at = _parse_shanghai_datetime(command, "expires_at")
    if expires_at <= issued_at:
        _fail("INVALID_COMMAND_LIFETIME", "expires_at", "must follow issued_at")
    command_seq = command.get("command_seq")
    if type(command_seq) is not int or command_seq <= 0:
        _fail("INVALID_COMMAND_SEQUENCE", "command_seq", "must be positive")

    command_type = command.get("command_type")
    if not isinstance(command_type, str) or command_type not in COMMAND_SCOPES:
        _fail("COMMAND_TYPE_UNSUPPORTED", "command_type", "is not supported")
    scope = _require_mapping(command.get("scope"), "scope")
    scope_type = scope.get("type")
    if scope_type not in COMMAND_SCOPES[command_type]:
        _fail("COMMAND_SCOPE_MISMATCH", "scope.type", f"is invalid for {command_type}")
    if scope_type == "NODE" and scope.get("id") != command.get("node_id"):
        _fail("COMMAND_TARGET_MISMATCH", "scope.id", "must match node_id")
    if scope_type == "ACCOUNT" and scope.get("id") != command.get("account_id"):
        _fail("COMMAND_TARGET_MISMATCH", "scope.id", "must match account_id")

    parameters = command.get("parameters")
    if parameters is not None:
        parameters = _require_mapping(parameters, "parameters")
    if command_type == "REQUEST_RECONCILIATION":
        if not parameters or "reconciliation_scope" not in parameters:
            _fail("COMMAND_PARAMETERS_MISSING", "parameters.reconciliation_scope", "is required")
    elif parameters and "reconciliation_scope" in parameters:
        _fail("COMMAND_PARAMETERS_INVALID", "parameters.reconciliation_scope", "is not applicable")
    if parameters and "cancel_active_orders" in parameters and command_type != "EMERGENCY_HALT":
        _fail(
            "COMMAND_PARAMETERS_INVALID",
            "parameters.cancel_active_orders",
            "is only valid for emergency halt",
        )


def validate_runtime_command_batch_semantics(batch: Mapping[str, Any]) -> None:
    """Validate a monotonically ordered, target-bound command response."""
    _require_supported_version(batch)
    generated_at = _parse_shanghai_datetime(batch, "generated_at")
    after = batch.get("after_command_seq")
    high = batch.get("high_watermark")
    if type(after) is not int or type(high) is not int or after < 0 or high < after:
        _fail(
            "INVALID_COMMAND_WATERMARK", "high_watermark", "must be no lower than after_command_seq"
        )
    commands = _require_sequence(batch.get("commands"), "commands")
    previous = after
    command_ids: set[str] = set()
    for index, raw_command in enumerate(commands):
        command = _require_mapping(raw_command, f"commands[{index}]")
        validate_runtime_command_semantics(command)
        command_id = command.get("command_id")
        sequence = command.get("command_seq")
        if not isinstance(command_id, str) or command_id in command_ids:
            _fail("DUPLICATE_COMMAND_ID", f"commands[{index}].command_id", "must be unique")
        if type(sequence) is not int or not previous < sequence <= high:
            _fail(
                "COMMAND_SEQUENCE_OUT_OF_ORDER",
                f"commands[{index}].command_seq",
                "must increase within watermarks",
            )
        if command.get("node_id") != batch.get("node_id") or command.get("account_id") != batch.get(
            "account_id"
        ):
            _fail("COMMAND_BATCH_TARGET_MISMATCH", f"commands[{index}]", "must match batch target")
        if _parse_shanghai_datetime(command, "issued_at") > generated_at:
            _fail(
                "COMMAND_ISSUED_IN_FUTURE",
                f"commands[{index}].issued_at",
                "must not follow generated_at",
            )
        command_ids.add(command_id)
        previous = sequence


def validate_runtime_command_result_semantics(
    result: Mapping[str, Any],
    *,
    command: Mapping[str, Any] | None = None,
) -> None:
    """Validate final command outcome and optionally bind it to the command."""
    _require_supported_version(result)
    received_at = _parse_shanghai_datetime(result, "received_at")
    completed_at = _parse_shanghai_datetime(result, "completed_at")
    if completed_at < received_at:
        _fail("COMMAND_RESULT_TIME_REGRESSION", "completed_at", "must not precede received_at")
    status = result.get("status")
    has_error = "error" in result
    if status in {"FAILED", "REJECTED"}:
        if not has_error:
            _fail("COMMAND_RESULT_ERROR_MISSING", "error", "is required for failure")
    elif status in {"APPLIED", "NOOP", "DUPLICATE"}:
        if has_error or result.get("retryable") is not False:
            _fail(
                "SUCCESSFUL_COMMAND_RESULT_INVALID",
                "status",
                "cannot contain an error or be retryable",
            )
    else:
        _fail("INVALID_COMMAND_RESULT_STATUS", "status", "is not supported")
    if status == "APPLIED" and "resulting_state" not in result:
        _fail("COMMAND_RESULT_STATE_MISSING", "resulting_state", "is required when applied")

    if command is not None:
        identity_fields = ("command_id", "command_seq", "node_id", "account_id")
        for field in identity_fields:
            if result.get(field) != command.get(field):
                _fail("COMMAND_RESULT_IDENTITY_MISMATCH", field, "must match the command")


def validate_protocol_error_semantics(error_envelope: Mapping[str, Any]) -> None:
    """Validate the version and timestamp of a standard protocol error."""
    _require_supported_version(error_envelope)
    _parse_shanghai_datetime(error_envelope, "occurred_at")
    _require_mapping(error_envelope.get("error"), "error")
