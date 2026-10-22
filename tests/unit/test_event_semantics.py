from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from auto_trading_system.protocol import (
    calculate_event_hash,
    new_boot_id,
    new_event_id,
    new_request_id,
    validate_domain_event_semantics,
    validate_event_batch_result_semantics,
    validate_event_batch_semantics,
    verify_event_hash,
)
from auto_trading_system.protocol.events import MAX_LOCAL_SEQUENCE, EventSemanticError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_ROOT = PROJECT_ROOT / "contracts" / "v1" / "examples" / "events"


def load_example(name: str) -> dict[str, Any]:
    with (EXAMPLES_ROOT / name).open(encoding="utf-8") as handle:
        value = json.load(handle)
    assert isinstance(value, dict)
    return value


def rehash(event: dict[str, Any]) -> None:
    event["event_hash"] = calculate_event_hash(event)


@pytest.mark.parametrize(
    ("factory", "pattern"),
    [
        (new_event_id, r"^evt_[0-9a-f]{32}$"),
        (new_boot_id, r"^boot_[0-9a-f]{32}$"),
        (new_request_id, r"^req_[0-9a-f]{32}$"),
    ],
)
def test_generated_protocol_ids_are_canonical_and_unique(
    factory: Any,
    pattern: str,
) -> None:
    first = factory()
    second = factory()

    assert re.fullmatch(pattern, first)
    assert re.fullmatch(pattern, second)
    assert first != second


def test_event_hash_is_deterministic_and_excludes_digest_field() -> None:
    event = load_example("event.runtime-state-changed.json")
    expected = calculate_event_hash(event)
    event["event_hash"] = "sha256:" + "f" * 64

    assert calculate_event_hash(event) == expected
    assert not verify_event_hash(event)


def test_event_hash_verifier_rejects_missing_or_non_string_digest() -> None:
    event = load_example("event.runtime-state-changed.json")
    del event["event_hash"]
    assert not verify_event_hash(event)

    event["event_hash"] = 1
    assert not verify_event_hash(event)


@pytest.mark.parametrize("schema_version", ["2.0", "invalid", 1])
def test_event_semantics_rejects_unsupported_or_invalid_version(schema_version: object) -> None:
    event = load_example("event.runtime-state-changed.json")
    event["schema_version"] = schema_version
    rehash(event)

    with pytest.raises(EventSemanticError) as error:
        validate_domain_event_semantics(event)

    assert error.value.code in {"EVENT_VERSION_UNSUPPORTED", "INVALID_SCHEMA_VERSION"}


@pytest.mark.parametrize(
    ("field", "value", "error_code"),
    [
        ("occurred_at", 1, "INVALID_TIMESTAMP"),
        ("occurred_at", "not-a-time", "INVALID_TIMESTAMP"),
        ("occurred_at", "2026-09-04T00:45:00Z", "INVALID_TIMEZONE_OFFSET"),
        ("recorded_at", "2026-09-04T00:45:00Z", "INVALID_TIMEZONE_OFFSET"),
    ],
)
def test_event_semantics_rejects_invalid_timestamp(
    field: str,
    value: object,
    error_code: str,
) -> None:
    event = load_example("event.runtime-state-changed.json")
    event[field] = value
    rehash(event)

    with pytest.raises(EventSemanticError, match=error_code):
        validate_domain_event_semantics(event)


@pytest.mark.parametrize("trade_date", [1, "2026-02-30"])
def test_event_semantics_rejects_invalid_trade_date(trade_date: object) -> None:
    event = load_example("event.runtime-state-changed.json")
    event["trade_date"] = trade_date
    rehash(event)

    with pytest.raises(EventSemanticError, match="INVALID_TRADE_DATE"):
        validate_domain_event_semantics(event)


def test_event_semantics_rejects_time_regression() -> None:
    event = load_example("event.runtime-state-changed.json")
    event["recorded_at"] = "2026-09-04T08:44:59+08:00"
    rehash(event)

    with pytest.raises(EventSemanticError, match="EVENT_TIME_REGRESSION"):
        validate_domain_event_semantics(event)


def test_event_semantics_rejects_trade_date_mismatch() -> None:
    event = load_example("event.runtime-state-changed.json")
    event["trade_date"] = "2026-09-03"
    rehash(event)

    with pytest.raises(EventSemanticError, match="EVENT_TRADE_DATE_MISMATCH"):
        validate_domain_event_semantics(event)


@pytest.mark.parametrize("local_seq", [True, 0, -1, MAX_LOCAL_SEQUENCE + 1])
def test_event_semantics_rejects_invalid_local_sequence(local_seq: object) -> None:
    event = load_example("event.runtime-state-changed.json")
    event["local_seq"] = local_seq
    rehash(event)

    with pytest.raises(EventSemanticError, match="INVALID_LOCAL_SEQUENCE"):
        validate_domain_event_semantics(event)


def test_event_semantics_rejects_unknown_event_type() -> None:
    event = load_example("event.runtime-state-changed.json")
    event["event_type"] = "UNKNOWN_EVENT"
    rehash(event)

    with pytest.raises(EventSemanticError, match="EVENT_TYPE_UNSUPPORTED"):
        validate_domain_event_semantics(event)


def test_event_semantics_rejects_aggregate_type_mismatch() -> None:
    event = load_example("event.trade-recorded.json")
    event["aggregate_type"] = "ORDER"
    rehash(event)

    with pytest.raises(EventSemanticError, match="AGGREGATE_TYPE_MISMATCH"):
        validate_domain_event_semantics(event)


@pytest.mark.parametrize(
    "example_name",
    [
        "event.runtime-state-changed.json",
        "event.plan-status-changed.json",
        "event.order-intent-created.json",
        "event.order-status-changed.json",
        "event.trade-recorded.json",
        "event.risk-decision.json",
        "event.control-command-applied.json",
        "event.reconciliation-discrepancy.json",
        "event.alert-raised.json",
    ],
)
def test_event_semantics_binds_aggregate_id_to_payload(example_name: str) -> None:
    event = load_example(example_name)
    event["aggregate_id"] = "wrong-aggregate"
    rehash(event)

    with pytest.raises(EventSemanticError, match="AGGREGATE_ID_MISMATCH"):
        validate_domain_event_semantics(event)


def test_event_semantics_requires_payload_object() -> None:
    event = load_example("event.runtime-state-changed.json")
    event["payload"] = []
    rehash(event)

    with pytest.raises(EventSemanticError, match="INVALID_EVENT_PAYLOAD"):
        validate_domain_event_semantics(event)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("order_quantity", True),
        ("filled_quantity", "0"),
        ("remaining_quantity", 100.0),
    ],
)
def test_order_status_semantics_requires_integer_quantities(field: str, value: object) -> None:
    event = load_example("event.order-status-changed.json")
    event["payload"][field] = value
    rehash(event)

    with pytest.raises(EventSemanticError, match="INVALID_ORDER_QUANTITY"):
        validate_domain_event_semantics(event)


def test_order_status_semantics_reconciles_total_quantity() -> None:
    event = load_example("event.order-status-changed.json")
    event["payload"]["filled_quantity"] = 10
    rehash(event)

    with pytest.raises(EventSemanticError, match="ORDER_QUANTITY_MISMATCH"):
        validate_domain_event_semantics(event)


def test_reconciliation_discrepancy_requires_a_reference() -> None:
    event = load_example("event.reconciliation-discrepancy.json")
    del event["payload"]["broker_reference"]
    rehash(event)

    with pytest.raises(EventSemanticError, match="RECONCILIATION_REFERENCE_MISSING"):
        validate_domain_event_semantics(event)


def test_event_semantics_rejects_stale_hash() -> None:
    event = load_example("event.alert-raised.json")
    event["payload"]["message"] = "changed without updating the digest"

    with pytest.raises(EventSemanticError, match="EVENT_HASH_MISMATCH"):
        validate_domain_event_semantics(event)


def test_batch_semantics_does_not_require_delivery_order() -> None:
    first = load_example("event.runtime-state-changed.json")
    second = load_example("event.plan-status-changed.json")
    batch = load_example("event-batch.request.json")
    batch["events"] = [second, first]

    validate_event_batch_semantics(batch)


@pytest.mark.parametrize("events", [None, "event", [], [None], [None] * 51])
def test_batch_semantics_rejects_invalid_event_array(events: object) -> None:
    batch = load_example("event-batch.request.json")
    batch["events"] = events

    with pytest.raises(EventSemanticError) as error:
        validate_event_batch_semantics(batch)

    assert error.value.code in {
        "INVALID_EVENT_BATCH",
        "INVALID_EVENT_BATCH_SIZE",
        "INVALID_EVENT",
    }


@pytest.mark.parametrize("field", ["node_id", "account_id"])
def test_batch_semantics_requires_matching_header_identity(field: str) -> None:
    batch = load_example("event-batch.request.json")
    event = batch["events"][0]
    event[field] = "node-other-001" if field == "node_id" else "acct_other_001"
    if field == "node_id":
        event["aggregate_id"] = event[field]
    rehash(event)

    with pytest.raises(EventSemanticError, match=f"BATCH_{field.split('_')[0].upper()}_MISMATCH"):
        validate_event_batch_semantics(batch)


def test_batch_semantics_rejects_duplicate_event_id() -> None:
    batch = load_example("event-batch.request.json")
    batch["events"].append(copy.deepcopy(batch["events"][0]))

    with pytest.raises(EventSemanticError, match="DUPLICATE_EVENT_ID"):
        validate_event_batch_semantics(batch)


def test_batch_semantics_rejects_duplicate_boot_sequence() -> None:
    batch = load_example("event-batch.request.json")
    duplicate_sequence = copy.deepcopy(batch["events"][0])
    duplicate_sequence["event_id"] = "evt_ffffffffffffffffffffffffffffffff"
    rehash(duplicate_sequence)
    batch["events"].append(duplicate_sequence)

    with pytest.raises(EventSemanticError, match="DUPLICATE_LOCAL_SEQUENCE"):
        validate_event_batch_semantics(batch)


def test_batch_semantics_rejects_invalid_sequence_identity() -> None:
    batch = load_example("event-batch.request.json")
    event = batch["events"][0]
    event["boot_id"] = None
    rehash(event)

    with pytest.raises(EventSemanticError, match="INVALID_EVENT_SEQUENCE"):
        validate_event_batch_semantics(batch)


def test_batch_result_semantics_rejects_wrong_request_id() -> None:
    request = load_example("event-batch.request.json")
    result = load_example("event-batch.result.json")
    result["request_id"] = "req_ffffffffffffffffffffffffffffffff"

    with pytest.raises(EventSemanticError, match="BATCH_RESULT_REQUEST_MISMATCH"):
        validate_event_batch_result_semantics(result, request=request)


@pytest.mark.parametrize(
    ("request_events", "results", "error_code"),
    [
        ("invalid", [], "INVALID_EVENT_BATCH"),
        ([], "invalid", "INVALID_BATCH_RESULT"),
        ([], [None], "INVALID_EVENT_RECEIPT"),
    ],
)
def test_batch_result_semantics_rejects_invalid_collections(
    request_events: object,
    results: object,
    error_code: str,
) -> None:
    request = load_example("event-batch.request.json")
    result = load_example("event-batch.result.json")
    request["events"] = request_events
    result["results"] = results

    with pytest.raises(EventSemanticError, match=error_code):
        validate_event_batch_result_semantics(result, request=request)


def test_batch_result_semantics_rejects_duplicate_receipt() -> None:
    request = load_example("event-batch.request.json")
    result = load_example("event-batch.result.json")
    result["results"].append(copy.deepcopy(result["results"][0]))

    with pytest.raises(EventSemanticError, match="DUPLICATE_EVENT_RECEIPT"):
        validate_event_batch_result_semantics(result, request=request)


@pytest.mark.parametrize("field", ["event_id", "event_hash"])
def test_batch_result_semantics_requires_exact_receipt_identity(field: str) -> None:
    request = load_example("event-batch.request.json")
    result = load_example("event-batch.result.json")
    result["results"][0][field] = "wrong"

    with pytest.raises(EventSemanticError, match="INCOMPLETE_BATCH_RESULT"):
        validate_event_batch_result_semantics(result, request=request)


@pytest.mark.parametrize("field", ["accepted", "duplicates", "rejected"])
def test_batch_result_semantics_requires_exact_summary_counts(field: str) -> None:
    request = load_example("event-batch.request.json")
    result = load_example("event-batch.result.json")
    result[field] += 1

    with pytest.raises(EventSemanticError, match="BATCH_RESULT_COUNT_MISMATCH"):
        validate_event_batch_result_semantics(result, request=request)


def test_batch_result_semantics_requires_persisted_sequence_watermark() -> None:
    request = load_example("event-batch.request.json")
    result = load_example("event-batch.result.json")
    result["high_watermark"] = 0

    with pytest.raises(EventSemanticError, match="BATCH_RESULT_WATERMARK_MISMATCH"):
        validate_event_batch_result_semantics(result, request=request)
