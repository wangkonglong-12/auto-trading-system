from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from auto_trading_system.protocol import (
    calculate_event_hash,
    validate_domain_event_semantics,
    validate_event_batch_result_semantics,
    validate_event_batch_semantics,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = PROJECT_ROOT / "contracts" / "v1"
EXAMPLES_ROOT = CONTRACT_ROOT / "examples" / "events"
EVENT_SCHEMA_PATH = CONTRACT_ROOT / "domain-event.schema.json"
BATCH_REQUEST_SCHEMA_PATH = CONTRACT_ROOT / "event-batch-request.schema.json"
BATCH_RESULT_SCHEMA_PATH = CONTRACT_ROOT / "event-batch-result.schema.json"

EXPECTED_EVENT_EXAMPLES = {
    "event.alert-raised.json",
    "event.control-command-applied.json",
    "event.order-intent-created.json",
    "event.order-status-changed.json",
    "event.plan-status-changed.json",
    "event.reconciliation-discrepancy.json",
    "event.risk-decision.json",
    "event.runtime-state-changed.json",
    "event.trade-recorded.json",
}


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    assert isinstance(value, dict)
    return value


def event_example_paths() -> tuple[Path, ...]:
    return tuple(sorted(EXAMPLES_ROOT.glob("event.*.json")))


@pytest.fixture(scope="module")
def event_schema() -> dict[str, Any]:
    return load_json(EVENT_SCHEMA_PATH)


@pytest.fixture(scope="module")
def schema_registry(event_schema: dict[str, Any]) -> Registry[Any]:
    return cast(
        Registry[Any],
        Registry().with_resource(event_schema["$id"], Resource.from_contents(event_schema)),
    )


@pytest.fixture(scope="module")
def event_validator(event_schema: dict[str, Any]) -> Draft202012Validator:
    Draft202012Validator.check_schema(event_schema)
    return Draft202012Validator(event_schema, format_checker=FormatChecker())


@pytest.fixture(scope="module")
def batch_request_validator(schema_registry: Registry[Any]) -> Draft202012Validator:
    schema = load_json(BATCH_REQUEST_SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(
        schema,
        registry=schema_registry,
        format_checker=FormatChecker(),
    )


@pytest.fixture(scope="module")
def batch_result_validator() -> Draft202012Validator:
    schema = load_json(BATCH_RESULT_SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_expected_event_example_set_is_complete() -> None:
    assert {path.name for path in event_example_paths()} == EXPECTED_EVENT_EXAMPLES


@pytest.mark.parametrize("example_path", event_example_paths(), ids=lambda path: path.stem)
def test_event_example_matches_schema_and_semantics(
    event_validator: Draft202012Validator,
    example_path: Path,
) -> None:
    event = load_json(example_path)

    assert list(event_validator.iter_errors(event)) == []
    assert event["event_hash"] == calculate_event_hash(event)
    validate_domain_event_semantics(event)


def test_event_examples_cover_every_declared_type(event_schema: dict[str, Any]) -> None:
    declared = set(event_schema["properties"]["event_type"]["enum"])
    represented = {load_json(path)["event_type"] for path in event_example_paths()}

    assert represented == declared


def test_event_schema_rejects_unknown_top_level_and_payload_fields(
    event_validator: Draft202012Validator,
) -> None:
    event = load_json(EXAMPLES_ROOT / "event.order-intent-created.json")
    event["unexpected"] = "value"
    event["payload"]["unexpected"] = "value"

    errors = list(event_validator.iter_errors(event))

    assert len(errors) >= 2


def test_event_schema_binds_type_to_aggregate_and_payload(
    event_validator: Draft202012Validator,
) -> None:
    event = load_json(EXAMPLES_ROOT / "event.trade-recorded.json")
    event["aggregate_type"] = "ORDER"
    event["payload"] = load_json(EXAMPLES_ROOT / "event.order-status-changed.json")["payload"]

    assert not event_validator.is_valid(event)


@pytest.mark.parametrize("command_type", ["MANUAL_TAKEOVER", "CLEAR_HALT"])
def test_control_event_supports_local_override_lifecycle(
    event_validator: Draft202012Validator,
    command_type: str,
) -> None:
    event = load_json(EXAMPLES_ROOT / "event.control-command-applied.json")
    event["payload"]["command_type"] = command_type
    event["event_hash"] = calculate_event_hash(event)

    assert event_validator.is_valid(event)
    validate_domain_event_semantics(event)


@pytest.mark.parametrize(
    ("example_name", "payload_field", "invalid_value"),
    [
        ("event.order-intent-created.json", "limit_price", 12.34),
        ("event.order-intent-created.json", "max_notional", 1500.0),
        ("event.order-intent-created.json", "quantity", True),
        ("event.trade-recorded.json", "symbol", "000001.sz"),
    ],
)
def test_event_schema_rejects_unsafe_wire_values(
    event_validator: Draft202012Validator,
    example_name: str,
    payload_field: str,
    invalid_value: object,
) -> None:
    event = load_json(EXAMPLES_ROOT / example_name)
    event["payload"][payload_field] = invalid_value

    assert not event_validator.is_valid(event)


@pytest.mark.parametrize("field", ["occurred_at", "recorded_at"])
def test_event_schema_requires_canonical_shanghai_timestamp(
    event_validator: Draft202012Validator,
    field: str,
) -> None:
    event = load_json(EXAMPLES_ROOT / "event.runtime-state-changed.json")
    event[field] = "2026-09-04T00:45:00Z"

    assert not event_validator.is_valid(event)


def test_event_batch_examples_match_schema_and_semantics(
    batch_request_validator: Draft202012Validator,
    batch_result_validator: Draft202012Validator,
) -> None:
    request = load_json(EXAMPLES_ROOT / "event-batch.request.json")
    result = load_json(EXAMPLES_ROOT / "event-batch.result.json")

    assert batch_request_validator.is_valid(request)
    assert batch_result_validator.is_valid(result)
    validate_event_batch_semantics(request)
    validate_event_batch_result_semantics(result, request=request)


def test_event_batch_schema_caps_batch_at_fifty_events(
    batch_request_validator: Draft202012Validator,
) -> None:
    request = load_json(EXAMPLES_ROOT / "event-batch.request.json")
    request["events"] = request["events"] * 51

    assert not batch_request_validator.is_valid(request)


def test_non_rejected_receipt_cannot_contain_error(
    batch_result_validator: Draft202012Validator,
) -> None:
    result = load_json(EXAMPLES_ROOT / "event-batch.result.json")
    result["results"][0]["error_code"] = "UNEXPECTED_ERROR"

    assert not batch_result_validator.is_valid(result)


def test_rejected_receipt_requires_error_details(
    batch_result_validator: Draft202012Validator,
) -> None:
    result = load_json(EXAMPLES_ROOT / "event-batch.result.json")
    result["results"][0]["status"] = "REJECTED"
    result["results"][0]["retryable"] = True

    assert not batch_result_validator.is_valid(result)

    result["results"][0]["error_code"] = "TEMPORARY_STORAGE_FAILURE"
    result["results"][0]["error_message"] = "Event storage is temporarily unavailable"
    assert batch_result_validator.is_valid(result)


def test_duplicate_receipt_is_a_successful_idempotent_outcome(
    batch_result_validator: Draft202012Validator,
) -> None:
    result = copy.deepcopy(load_json(EXAMPLES_ROOT / "event-batch.result.json"))
    result["results"][0]["status"] = "DUPLICATE"

    assert batch_result_validator.is_valid(result)
