from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

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

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = PROJECT_ROOT / "contracts" / "v1"
EXAMPLES_ROOT = CONTRACT_ROOT / "examples"
SCHEMA_FILES = {
    "asset": "asset-snapshot.schema.json",
    "position": "position-snapshot.schema.json",
    "reconciliation": "reconciliation-report.schema.json",
    "heartbeat": "runtime-heartbeat.schema.json",
    "command": "runtime-command.schema.json",
    "command_batch": "runtime-command-batch.schema.json",
    "command_result": "runtime-command-result.schema.json",
    "protocol_error": "protocol-error.schema.json",
}
EXAMPLE_CASES = (
    ("asset", "snapshots/asset.available.json", validate_asset_snapshot_semantics),
    ("asset", "snapshots/asset.query-failed.json", validate_asset_snapshot_semantics),
    ("position", "snapshots/position.available.json", validate_position_snapshot_semantics),
    ("position", "snapshots/position.query-failed.json", validate_position_snapshot_semantics),
    (
        "reconciliation",
        "reconciliation/reconciliation.matched.json",
        validate_reconciliation_report_semantics,
    ),
    (
        "reconciliation",
        "reconciliation/reconciliation.discrepancies.json",
        validate_reconciliation_report_semantics,
    ),
    (
        "reconciliation",
        "reconciliation/reconciliation.indeterminate.json",
        validate_reconciliation_report_semantics,
    ),
    ("heartbeat", "runtime/heartbeat.healthy.json", validate_runtime_heartbeat_semantics),
    ("heartbeat", "runtime/heartbeat.degraded.json", validate_runtime_heartbeat_semantics),
    ("command_batch", "runtime/commands.batch.json", validate_runtime_command_batch_semantics),
    (
        "command_result",
        "runtime/command-result.applied.json",
        validate_runtime_command_result_semantics,
    ),
    (
        "command_result",
        "runtime/command-result.rejected.json",
        validate_runtime_command_result_semantics,
    ),
    ("protocol_error", "errors/protocol-error.validation.json", validate_protocol_error_semantics),
)


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    assert isinstance(value, dict)
    return value


@pytest.fixture(scope="module")
def schemas() -> dict[str, dict[str, Any]]:
    loaded = {name: load_json(CONTRACT_ROOT / filename) for name, filename in SCHEMA_FILES.items()}
    loaded["common"] = load_json(CONTRACT_ROOT / "common.schema.json")
    for schema in loaded.values():
        Draft202012Validator.check_schema(schema)
    return loaded


@pytest.fixture(scope="module")
def registry(schemas: dict[str, dict[str, Any]]) -> Registry[Any]:
    result: Registry[Any] = Registry()
    for schema in schemas.values():
        result = cast(
            Registry[Any], result.with_resource(schema["$id"], Resource.from_contents(schema))
        )
    return result


@pytest.fixture(scope="module")
def validators(
    schemas: dict[str, dict[str, Any]], registry: Registry[Any]
) -> dict[str, Draft202012Validator]:
    return {
        name: Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())
        for name, schema in schemas.items()
        if name != "common"
    }


@pytest.mark.parametrize(("schema_name", "relative_path", "semantic_validator"), EXAMPLE_CASES)
def test_node_message_examples_match_schema_and_semantics(
    validators: dict[str, Draft202012Validator],
    schema_name: str,
    relative_path: str,
    semantic_validator: Any,
) -> None:
    example = load_json(EXAMPLES_ROOT / relative_path)

    assert list(validators[schema_name].iter_errors(example)) == []
    semantic_validator(example)


def test_each_runtime_command_matches_its_own_schema(
    validators: dict[str, Draft202012Validator],
) -> None:
    batch = load_json(EXAMPLES_ROOT / "runtime/commands.batch.json")

    assert all(validators["command"].is_valid(command) for command in batch["commands"])
    for command in batch["commands"]:
        validate_runtime_command_semantics(command)


@pytest.mark.parametrize(
    ("schema_name", "relative_path"), [(case[0], case[1]) for case in EXAMPLE_CASES]
)
def test_node_message_schemas_reject_unknown_major(
    validators: dict[str, Draft202012Validator],
    schema_name: str,
    relative_path: str,
) -> None:
    example = load_json(EXAMPLES_ROOT / relative_path)
    example["schema_version"] = "2.0"

    assert not validators[schema_name].is_valid(example)


@pytest.mark.parametrize(
    ("schema_name", "relative_path", "required_field"),
    [
        ("asset", "snapshots/asset.available.json", "snapshot_id"),
        ("position", "snapshots/position.available.json", "positions"),
        ("reconciliation", "reconciliation/reconciliation.matched.json", "inputs"),
        ("heartbeat", "runtime/heartbeat.healthy.json", "health"),
        ("command_batch", "runtime/commands.batch.json", "high_watermark"),
        ("command_result", "runtime/command-result.applied.json", "completed_at"),
        ("protocol_error", "errors/protocol-error.validation.json", "error"),
    ],
)
def test_node_message_schemas_reject_missing_required_field(
    validators: dict[str, Draft202012Validator],
    schema_name: str,
    relative_path: str,
    required_field: str,
) -> None:
    example = load_json(EXAMPLES_ROOT / relative_path)
    del example[required_field]

    assert not validators[schema_name].is_valid(example)


@pytest.mark.parametrize(
    ("schema_name", "relative_path"),
    [
        ("asset", "snapshots/asset.available.json"),
        ("position", "snapshots/position.available.json"),
        ("reconciliation", "reconciliation/reconciliation.matched.json"),
        ("heartbeat", "runtime/heartbeat.healthy.json"),
        ("command", "runtime/commands.batch.json"),
        ("command_result", "runtime/command-result.applied.json"),
        ("protocol_error", "errors/protocol-error.validation.json"),
    ],
)
def test_node_message_schemas_reject_unknown_fields(
    validators: dict[str, Draft202012Validator],
    schema_name: str,
    relative_path: str,
) -> None:
    example = load_json(EXAMPLES_ROOT / relative_path)
    if schema_name == "command":
        example = copy.deepcopy(example["commands"][0])
    example["unexpected"] = "value"

    assert not validators[schema_name].is_valid(example)


def test_successful_empty_position_snapshot_is_explicit_and_valid(
    validators: dict[str, Draft202012Validator],
) -> None:
    snapshot = load_json(EXAMPLES_ROOT / "snapshots/position.available.json")
    snapshot["positions"] = []

    assert validators["position"].is_valid(snapshot)
    validate_position_snapshot_semantics(snapshot)


@pytest.mark.parametrize(
    ("schema_name", "relative_path", "payload_field"),
    [
        ("asset", "snapshots/asset.query-failed.json", "asset"),
        ("position", "snapshots/position.query-failed.json", "positions"),
    ],
)
def test_failed_query_cannot_carry_false_empty_or_zero_payload(
    validators: dict[str, Draft202012Validator],
    schema_name: str,
    relative_path: str,
    payload_field: str,
) -> None:
    snapshot = load_json(EXAMPLES_ROOT / relative_path)
    snapshot[payload_field] = {} if payload_field == "asset" else []

    assert not validators[schema_name].is_valid(snapshot)


def test_available_query_cannot_carry_error(
    validators: dict[str, Draft202012Validator],
) -> None:
    snapshot = load_json(EXAMPLES_ROOT / "snapshots/asset.available.json")
    snapshot["error"] = {"code": "UNEXPECTED", "message": "not allowed", "retryable": False}

    assert not validators["asset"].is_valid(snapshot)


@pytest.mark.parametrize(
    ("relative_path", "field_path"),
    [
        ("snapshots/asset.available.json", ("asset", "cash")),
        ("snapshots/position.available.json", ("positions", 0, "average_price")),
    ],
)
def test_decimal_wire_values_cannot_be_binary_float(
    validators: dict[str, Draft202012Validator],
    relative_path: str,
    field_path: tuple[Any, ...],
) -> None:
    schema_name = "asset" if relative_path.startswith("snapshots/asset") else "position"
    example = load_json(EXAMPLES_ROOT / relative_path)
    target: Any = example
    for component in field_path[:-1]:
        target = target[component]
    target[field_path[-1]] = 1.25

    assert not validators[schema_name].is_valid(example)


def test_rejected_command_result_requires_error(
    validators: dict[str, Draft202012Validator],
) -> None:
    result = load_json(EXAMPLES_ROOT / "runtime/command-result.rejected.json")
    del result["error"]

    assert not validators["command_result"].is_valid(result)


def test_matched_reconciliation_cannot_contain_discrepancy(
    validators: dict[str, Draft202012Validator],
) -> None:
    matched = load_json(EXAMPLES_ROOT / "reconciliation/reconciliation.matched.json")
    difference = load_json(EXAMPLES_ROOT / "reconciliation/reconciliation.discrepancies.json")
    matched["discrepancies"] = difference["discrepancies"]

    assert not validators["reconciliation"].is_valid(matched)
