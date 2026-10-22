from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from auto_trading_system.protocol import (
    validate_asset_snapshot_semantics,
    validate_position_snapshot_semantics,
    validate_protocol_error_semantics,
    validate_reconciliation_report_semantics,
    validate_runtime_command_batch_semantics,
    validate_runtime_command_result_semantics,
    validate_runtime_command_semantics,
    validate_runtime_heartbeat_semantics,
)
from auto_trading_system.protocol.node_messages import NodeMessageSemanticError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_ROOT = PROJECT_ROOT / "contracts" / "v1" / "examples"


def load_example(relative_path: str) -> dict[str, Any]:
    with (EXAMPLES_ROOT / relative_path).open(encoding="utf-8") as handle:
        value = json.load(handle)
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize("schema_version", ["2.0", "invalid", 1])
def test_node_message_semantics_rejects_invalid_version(schema_version: object) -> None:
    snapshot = load_example("snapshots/asset.available.json")
    snapshot["schema_version"] = schema_version

    with pytest.raises(NodeMessageSemanticError) as error:
        validate_asset_snapshot_semantics(snapshot)

    assert error.value.code in {"PROTOCOL_VERSION_UNSUPPORTED", "INVALID_SCHEMA_VERSION"}


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("as_of", 1, "INVALID_TIMESTAMP"),
        ("as_of", "bad-time", "INVALID_TIMESTAMP"),
        ("as_of", "2026-09-04T15:10:00Z", "INVALID_TIMEZONE_OFFSET"),
        ("trade_date", 1, "INVALID_TRADE_DATE"),
        ("trade_date", "2026-02-30", "INVALID_TRADE_DATE"),
        ("trade_date", "2026-09-03", "SNAPSHOT_TRADE_DATE_MISMATCH"),
    ],
)
def test_snapshot_semantics_rejects_invalid_time_or_date(
    field: str, value: object, code: str
) -> None:
    snapshot = load_example("snapshots/asset.available.json")
    snapshot[field] = value

    with pytest.raises(NodeMessageSemanticError, match=code):
        validate_asset_snapshot_semantics(snapshot)


@pytest.mark.parametrize("query_status", [None, "EMPTY", 1])
def test_snapshot_semantics_rejects_invalid_query_status(query_status: object) -> None:
    snapshot = load_example("snapshots/asset.available.json")
    snapshot["query_status"] = query_status

    with pytest.raises(NodeMessageSemanticError, match="INVALID_QUERY_STATUS"):
        validate_asset_snapshot_semantics(snapshot)


def test_available_asset_requires_data_without_error() -> None:
    snapshot = load_example("snapshots/asset.available.json")
    del snapshot["asset"]

    with pytest.raises(NodeMessageSemanticError, match="AVAILABLE_QUERY_PAYLOAD_INVALID"):
        validate_asset_snapshot_semantics(snapshot)

    snapshot["asset"] = {"cash": "1.00"}
    snapshot["error"] = {"code": "ERROR"}
    with pytest.raises(NodeMessageSemanticError, match="AVAILABLE_QUERY_PAYLOAD_INVALID"):
        validate_asset_snapshot_semantics(snapshot)


def test_unavailable_asset_requires_error_without_data() -> None:
    snapshot = load_example("snapshots/asset.query-failed.json")
    del snapshot["error"]

    with pytest.raises(NodeMessageSemanticError, match="FAILED_QUERY_PAYLOAD_INVALID"):
        validate_asset_snapshot_semantics(snapshot)

    snapshot["error"] = []
    with pytest.raises(NodeMessageSemanticError, match="INVALID_OBJECT"):
        validate_asset_snapshot_semantics(snapshot)


def test_available_asset_requires_object() -> None:
    snapshot = load_example("snapshots/asset.available.json")
    snapshot["asset"] = []

    with pytest.raises(NodeMessageSemanticError, match="INVALID_OBJECT"):
        validate_asset_snapshot_semantics(snapshot)


def test_position_semantics_rejects_invalid_collection_or_item() -> None:
    snapshot = load_example("snapshots/position.available.json")
    snapshot["positions"] = "not-an-array"
    with pytest.raises(NodeMessageSemanticError, match="INVALID_ARRAY"):
        validate_position_snapshot_semantics(snapshot)

    snapshot["positions"] = [None]
    with pytest.raises(NodeMessageSemanticError, match="INVALID_OBJECT"):
        validate_position_snapshot_semantics(snapshot)


def test_position_symbols_are_unique() -> None:
    snapshot = load_example("snapshots/position.available.json")
    snapshot["positions"].append(copy.deepcopy(snapshot["positions"][0]))

    with pytest.raises(NodeMessageSemanticError, match="DUPLICATE_POSITION"):
        validate_position_snapshot_semantics(snapshot)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("volume", 0, "INVALID_POSITION_VOLUME"),
        ("volume", True, "INVALID_POSITION_VOLUME"),
        ("can_use_volume", 10001, "POSITION_QUANTITY_EXCEEDS_VOLUME"),
        ("frozen_volume", -1, "POSITION_QUANTITY_EXCEEDS_VOLUME"),
        ("yesterday_volume", "100", "POSITION_QUANTITY_EXCEEDS_VOLUME"),
    ],
)
def test_position_quantities_respect_total(field: str, value: object, code: str) -> None:
    snapshot = load_example("snapshots/position.available.json")
    snapshot["positions"][0][field] = value

    with pytest.raises(NodeMessageSemanticError, match=code):
        validate_position_snapshot_semantics(snapshot)


def test_failed_position_query_does_not_validate_false_empty_collection() -> None:
    snapshot = load_example("snapshots/position.query-failed.json")
    snapshot["positions"] = []

    with pytest.raises(NodeMessageSemanticError, match="FAILED_QUERY_PAYLOAD_INVALID"):
        validate_position_snapshot_semantics(snapshot)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("started_at", "2026-09-04T07:00:00Z", "INVALID_TIMEZONE_OFFSET"),
        ("trade_date", "2026-09-03", "RECONCILIATION_TRADE_DATE_MISMATCH"),
        ("completed_at", "2026-09-04T15:09:59+08:00", "RECONCILIATION_TIME_REGRESSION"),
    ],
)
def test_reconciliation_semantics_rejects_invalid_time(
    field: str, value: object, code: str
) -> None:
    report = load_example("reconciliation/reconciliation.matched.json")
    report[field] = value

    with pytest.raises(NodeMessageSemanticError, match=code):
        validate_reconciliation_report_semantics(report)


def test_reconciliation_requires_input_array_and_objects() -> None:
    report = load_example("reconciliation/reconciliation.matched.json")
    report["inputs"] = None
    with pytest.raises(NodeMessageSemanticError, match="INVALID_ARRAY"):
        validate_reconciliation_report_semantics(report)

    report["inputs"] = [None]
    with pytest.raises(NodeMessageSemanticError, match="INVALID_OBJECT"):
        validate_reconciliation_report_semantics(report)


def test_reconciliation_requires_each_input_kind_once() -> None:
    report = load_example("reconciliation/reconciliation.matched.json")
    report["inputs"][1]["kind"] = "ASSET"

    with pytest.raises(NodeMessageSemanticError, match="DUPLICATE_RECONCILIATION_INPUT"):
        validate_reconciliation_report_semantics(report)

    report = load_example("reconciliation/reconciliation.matched.json")
    report["inputs"][1]["kind"] = "OTHER"
    with pytest.raises(NodeMessageSemanticError, match="INCOMPLETE_RECONCILIATION_INPUTS"):
        validate_reconciliation_report_semantics(report)


@pytest.mark.parametrize(
    ("query_status", "remove_field", "code"),
    [
        ("AVAILABLE", "reference_id", "AVAILABLE_INPUT_INVALID"),
        ("QUERY_FAILED", "error", "UNAVAILABLE_INPUT_INVALID"),
        ("NOT_A_STATUS", "reference_id", "INVALID_QUERY_STATUS"),
    ],
)
def test_reconciliation_input_status_controls_evidence(
    query_status: str, remove_field: str, code: str
) -> None:
    report = load_example("reconciliation/reconciliation.indeterminate.json")
    item = report["inputs"][1]
    item["query_status"] = query_status
    item.pop(remove_field, None)

    with pytest.raises(NodeMessageSemanticError, match=code):
        validate_reconciliation_report_semantics(report)


def test_reconciliation_input_time_stays_inside_report() -> None:
    report = load_example("reconciliation/reconciliation.matched.json")
    report["inputs"][0]["as_of"] = "2026-09-04T15:10:09+08:00"

    with pytest.raises(NodeMessageSemanticError, match="INVALID_RECONCILIATION_INPUT_TIME"):
        validate_reconciliation_report_semantics(report)


@pytest.mark.parametrize(
    ("relative_path", "status", "code"),
    [
        (
            "reconciliation/reconciliation.indeterminate.json",
            "MATCHED",
            "MATCHED_RECONCILIATION_INVALID",
        ),
        (
            "reconciliation/reconciliation.indeterminate.json",
            "DISCREPANCIES",
            "DISCREPANCY_RECONCILIATION_INVALID",
        ),
        (
            "reconciliation/reconciliation.matched.json",
            "INDETERMINATE",
            "INDETERMINATE_RECONCILIATION_INVALID",
        ),
        ("reconciliation/reconciliation.matched.json", "OTHER", "INVALID_RECONCILIATION_STATUS"),
    ],
)
def test_reconciliation_status_matches_input_certainty(
    relative_path: str, status: str, code: str
) -> None:
    report = load_example(relative_path)
    report["status"] = status

    with pytest.raises(NodeMessageSemanticError, match=code):
        validate_reconciliation_report_semantics(report)


def test_reconciliation_discrepancy_ids_are_unique() -> None:
    report = load_example("reconciliation/reconciliation.discrepancies.json")
    report["discrepancies"].append(copy.deepcopy(report["discrepancies"][0]))

    with pytest.raises(NodeMessageSemanticError, match="DUPLICATE_DISCREPANCY"):
        validate_reconciliation_report_semantics(report)


def test_reconciliation_discrepancy_subjects_are_unique() -> None:
    report = load_example("reconciliation/reconciliation.discrepancies.json")
    duplicate_subject = copy.deepcopy(report["discrepancies"][0])
    duplicate_subject["discrepancy_id"] = "diff_position_600000_002"
    report["discrepancies"].append(duplicate_subject)
    report["summary"]["total_discrepancies"] = 2
    report["summary"]["open_discrepancies"] = 2
    report["summary"]["critical_discrepancies"] = 2

    with pytest.raises(
        NodeMessageSemanticError,
        match="DUPLICATE_DISCREPANCY_SUBJECT",
    ):
        validate_reconciliation_report_semantics(report)


@pytest.mark.parametrize(
    ("summary_field", "value"),
    [
        ("total_discrepancies", 0),
        ("open_discrepancies", 0),
        ("critical_discrepancies", 0),
    ],
)
def test_reconciliation_summary_is_derived(summary_field: str, value: int) -> None:
    report = load_example("reconciliation/reconciliation.discrepancies.json")
    report["summary"][summary_field] = value

    with pytest.raises(NodeMessageSemanticError, match="RECONCILIATION_SUMMARY_MISMATCH"):
        validate_reconciliation_report_semantics(report)


def test_reconciliation_requires_discrepancy_array_and_summary_object() -> None:
    report = load_example("reconciliation/reconciliation.matched.json")
    report["discrepancies"] = None
    with pytest.raises(NodeMessageSemanticError, match="INVALID_ARRAY"):
        validate_reconciliation_report_semantics(report)

    report = load_example("reconciliation/reconciliation.matched.json")
    report["summary"] = None
    with pytest.raises(NodeMessageSemanticError, match="INVALID_OBJECT"):
        validate_reconciliation_report_semantics(report)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("heartbeat_seq", 0, "INVALID_HEARTBEAT_SEQUENCE"),
        ("heartbeat_seq", True, "INVALID_HEARTBEAT_SEQUENCE"),
        ("trade_date", "2026-09-03", "HEARTBEAT_TRADE_DATE_MISMATCH"),
    ],
)
def test_heartbeat_validates_identity_time(field: str, value: object, code: str) -> None:
    heartbeat = load_example("runtime/heartbeat.healthy.json")
    heartbeat[field] = value

    with pytest.raises(NodeMessageSemanticError, match=code):
        validate_runtime_heartbeat_semantics(heartbeat)


@pytest.mark.parametrize("prefix", ["event_queue", "writer_queue"])
def test_heartbeat_queue_depth_cannot_exceed_capacity(prefix: str) -> None:
    heartbeat = load_example("runtime/heartbeat.healthy.json")
    heartbeat["health"][f"{prefix}_depth"] = heartbeat["health"][f"{prefix}_capacity"] + 1

    with pytest.raises(NodeMessageSemanticError, match="QUEUE_DEPTH_EXCEEDS_CAPACITY"):
        validate_runtime_heartbeat_semantics(heartbeat)


@pytest.mark.parametrize(("pending", "oldest"), [(0, "2026-09-04T14:44:00+08:00"), (1, None)])
def test_heartbeat_outbox_count_controls_oldest_time(pending: int, oldest: str | None) -> None:
    heartbeat = load_example("runtime/heartbeat.healthy.json")
    heartbeat["health"]["outbox_pending"] = pending
    heartbeat["health"]["oldest_outbox_at"] = oldest

    with pytest.raises(NodeMessageSemanticError, match="OUTBOX_AGE_MISMATCH"):
        validate_runtime_heartbeat_semantics(heartbeat)


def test_heartbeat_rejects_invalid_outbox_count_and_future_time() -> None:
    heartbeat = load_example("runtime/heartbeat.healthy.json")
    heartbeat["health"]["outbox_pending"] = True
    with pytest.raises(NodeMessageSemanticError, match="INVALID_OUTBOX_COUNT"):
        validate_runtime_heartbeat_semantics(heartbeat)

    heartbeat = load_example("runtime/heartbeat.degraded.json")
    heartbeat["health"]["oldest_outbox_at"] = "2026-09-04T14:45:06+08:00"
    with pytest.raises(NodeMessageSemanticError, match="OUTBOX_TIME_IN_FUTURE"):
        validate_runtime_heartbeat_semantics(heartbeat)


@pytest.mark.parametrize(
    ("container", "field", "value"),
    [
        ("connectivity", "qmt", "DISCONNECTED"),
        ("connectivity", "broker_account", "UNKNOWN"),
        ("connectivity", "market_data", "STALE"),
        ("health", "journal", "DEGRADED"),
    ],
)
def test_auto_heartbeat_requires_safe_local_health(container: str, field: str, value: str) -> None:
    heartbeat = load_example("runtime/heartbeat.healthy.json")
    heartbeat[container][field] = value

    with pytest.raises(NodeMessageSemanticError, match="AUTO_HEALTH_PRECONDITION_FAILED"):
        validate_runtime_heartbeat_semantics(heartbeat)


def command_example(index: int = 0) -> dict[str, Any]:
    return copy.deepcopy(load_example("runtime/commands.batch.json")["commands"][index])


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("expires_at", "2026-09-04T14:45:08+08:00", "INVALID_COMMAND_LIFETIME"),
        ("command_seq", 0, "INVALID_COMMAND_SEQUENCE"),
        ("command_seq", True, "INVALID_COMMAND_SEQUENCE"),
        ("command_type", "UNKNOWN", "COMMAND_TYPE_UNSUPPORTED"),
    ],
)
def test_command_validates_lifetime_sequence_and_type(field: str, value: object, code: str) -> None:
    command = command_example()
    command[field] = value

    with pytest.raises(NodeMessageSemanticError, match=code):
        validate_runtime_command_semantics(command)


@pytest.mark.parametrize(
    ("command_type", "scope_type"),
    [("CANCEL_ORDER", "ACCOUNT"), ("FREEZE_SYMBOL", "NODE"), ("PAUSE_BUY", "ORDER")],
)
def test_command_scope_is_bound_to_type(command_type: str, scope_type: str) -> None:
    command = command_example()
    command["command_type"] = command_type
    command["scope"]["type"] = scope_type

    with pytest.raises(NodeMessageSemanticError, match="COMMAND_SCOPE_MISMATCH"):
        validate_runtime_command_semantics(command)


@pytest.mark.parametrize(
    ("scope_type", "scope_id"), [("NODE", "node-other-001"), ("ACCOUNT", "acct_other")]
)
def test_command_scope_identity_matches_target(scope_type: str, scope_id: str) -> None:
    command = command_example()
    command["scope"] = {"type": scope_type, "id": scope_id}

    with pytest.raises(NodeMessageSemanticError, match="COMMAND_TARGET_MISMATCH"):
        validate_runtime_command_semantics(command)


def test_reconciliation_command_requires_scope_parameter() -> None:
    command = command_example(1)
    del command["parameters"]

    with pytest.raises(NodeMessageSemanticError, match="COMMAND_PARAMETERS_MISSING"):
        validate_runtime_command_semantics(command)


@pytest.mark.parametrize("parameter", ["reconciliation_scope", "cancel_active_orders"])
def test_command_rejects_parameters_for_wrong_type(parameter: str) -> None:
    command = command_example()
    command["parameters"] = {parameter: "FULL" if parameter == "reconciliation_scope" else True}

    with pytest.raises(NodeMessageSemanticError, match="COMMAND_PARAMETERS_INVALID"):
        validate_runtime_command_semantics(command)


def test_command_requires_scope_object() -> None:
    command = command_example()
    command["scope"] = None

    with pytest.raises(NodeMessageSemanticError, match="INVALID_OBJECT"):
        validate_runtime_command_semantics(command)


@pytest.mark.parametrize(
    ("after", "high"),
    [(10, 9), (-1, 10), (True, 10), (10, "11")],
)
def test_command_batch_validates_watermarks(after: object, high: object) -> None:
    batch = load_example("runtime/commands.batch.json")
    batch["after_command_seq"] = after
    batch["high_watermark"] = high

    with pytest.raises(NodeMessageSemanticError, match="INVALID_COMMAND_WATERMARK"):
        validate_runtime_command_batch_semantics(batch)


def test_command_batch_requires_array_and_command_objects() -> None:
    batch = load_example("runtime/commands.batch.json")
    batch["commands"] = None
    with pytest.raises(NodeMessageSemanticError, match="INVALID_ARRAY"):
        validate_runtime_command_batch_semantics(batch)

    batch["commands"] = [None]
    with pytest.raises(NodeMessageSemanticError, match="INVALID_OBJECT"):
        validate_runtime_command_batch_semantics(batch)


def test_command_batch_rejects_duplicate_id_and_sequence_regression() -> None:
    batch = load_example("runtime/commands.batch.json")
    duplicate = copy.deepcopy(batch["commands"][0])
    duplicate["command_seq"] = 42
    batch["commands"].append(duplicate)
    with pytest.raises(NodeMessageSemanticError, match="DUPLICATE_COMMAND_ID"):
        validate_runtime_command_batch_semantics(batch)

    batch = load_example("runtime/commands.batch.json")
    batch["commands"][1]["command_seq"] = 41
    with pytest.raises(NodeMessageSemanticError, match="COMMAND_SEQUENCE_OUT_OF_ORDER"):
        validate_runtime_command_batch_semantics(batch)


@pytest.mark.parametrize("field", ["node_id", "account_id"])
def test_command_batch_binds_target(field: str) -> None:
    batch = load_example("runtime/commands.batch.json")
    batch["commands"][0][field] = "node-other-001" if field == "node_id" else "acct_other"
    if field == "account_id":
        batch["commands"][0]["scope"]["id"] = "acct_other"

    with pytest.raises(NodeMessageSemanticError, match="COMMAND_BATCH_TARGET_MISMATCH"):
        validate_runtime_command_batch_semantics(batch)


def test_command_batch_rejects_command_issued_after_response() -> None:
    batch = load_example("runtime/commands.batch.json")
    batch["commands"][0]["issued_at"] = "2026-09-04T14:45:11+08:00"

    with pytest.raises(NodeMessageSemanticError, match="COMMAND_ISSUED_IN_FUTURE"):
        validate_runtime_command_batch_semantics(batch)


def test_command_result_time_is_monotonic() -> None:
    result = load_example("runtime/command-result.applied.json")
    result["completed_at"] = "2026-09-04T14:45:09+08:00"

    with pytest.raises(NodeMessageSemanticError, match="COMMAND_RESULT_TIME_REGRESSION"):
        validate_runtime_command_result_semantics(result)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("FAILED", "COMMAND_RESULT_ERROR_MISSING"),
        ("OTHER", "INVALID_COMMAND_RESULT_STATUS"),
    ],
)
def test_command_result_status_controls_error(status: str, code: str) -> None:
    result = load_example("runtime/command-result.applied.json")
    result["status"] = status
    result.pop("resulting_state", None)

    with pytest.raises(NodeMessageSemanticError, match=code):
        validate_runtime_command_result_semantics(result)


def test_successful_command_result_is_not_retryable_and_has_no_error() -> None:
    result = load_example("runtime/command-result.applied.json")
    result["retryable"] = True
    with pytest.raises(NodeMessageSemanticError, match="SUCCESSFUL_COMMAND_RESULT_INVALID"):
        validate_runtime_command_result_semantics(result)

    result["retryable"] = False
    result["error"] = {"code": "ERROR"}
    with pytest.raises(NodeMessageSemanticError, match="SUCCESSFUL_COMMAND_RESULT_INVALID"):
        validate_runtime_command_result_semantics(result)


def test_applied_command_result_requires_resulting_state() -> None:
    result = load_example("runtime/command-result.applied.json")
    del result["resulting_state"]

    with pytest.raises(NodeMessageSemanticError, match="COMMAND_RESULT_STATE_MISSING"):
        validate_runtime_command_result_semantics(result)


@pytest.mark.parametrize("field", ["command_id", "command_seq", "node_id", "account_id"])
def test_command_result_binds_to_command_identity(field: str) -> None:
    command = command_example()
    result = load_example("runtime/command-result.applied.json")
    result[field] = "wrong" if field != "command_seq" else 999

    with pytest.raises(NodeMessageSemanticError, match="COMMAND_RESULT_IDENTITY_MISMATCH"):
        validate_runtime_command_result_semantics(result, command=command)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("schema_version", "2.0", "PROTOCOL_VERSION_UNSUPPORTED"),
        ("occurred_at", "2026-09-04T06:45:30Z", "INVALID_TIMEZONE_OFFSET"),
        ("error", [], "INVALID_OBJECT"),
    ],
)
def test_protocol_error_semantics(field: str, value: object, code: str) -> None:
    envelope = load_example("errors/protocol-error.validation.json")
    envelope[field] = value

    with pytest.raises(NodeMessageSemanticError, match=code):
        validate_protocol_error_semantics(envelope)
