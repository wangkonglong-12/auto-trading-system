from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response as HttpxResponse

from auto_trading_system.protocol import (
    MappingKeyResolver,
    SignatureError,
    sign_request,
    validate_event_batch_result_semantics,
    validate_runtime_command_batch_semantics,
    verify_response,
)
from auto_trading_system.protocol.signing import HEADER_SIGNATURE
from auto_trading_system.testing import (
    EventBatchScenario,
    MockCentralDisconnectedError,
    MockCentralNetworkFault,
    MockCentralService,
    MockCentralTimeoutError,
    MockResponseFault,
    ReceiptOutcome,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_ROOT = PROJECT_ROOT / "contracts" / "v1" / "examples"
SECRET = b"mock-central-test-secret-at-least-32-bytes"
KEY_ID = "key_mock_central"
NODE_ID = "node-shanghai-001"
ACCOUNT_ID = "acct_primary_001"
EPOCH_SECONDS = 1_788_504_310


def load_example(relative_path: str) -> dict[str, Any]:
    with (EXAMPLES_ROOT / relative_path).open(encoding="utf-8") as handle:
        value = json.load(handle)
    assert isinstance(value, dict)
    return value


@dataclass(frozen=True, slots=True)
class Attempt:
    response: HttpxResponse
    request_id: str
    nonce: str


class SignedClient:
    def __init__(self, service: MockCentralService, client: TestClient) -> None:
        self.service = service
        self.client = client
        self.sequence = 0

    def request(
        self,
        method: str,
        target: str,
        *,
        payload: dict[str, Any] | None = None,
        raw_body: bytes | None = None,
        preserve_request_id: bool = False,
        header_overrides: dict[str, str] | None = None,
    ) -> Attempt:
        self.sequence += 1
        nonce = f"{self.sequence:032x}"
        request_id = f"req_{self.sequence:032x}"
        if raw_body is None:
            if payload is None:
                body = b""
            else:
                payload = copy.deepcopy(payload)
                if not preserve_request_id and "request_id" in payload:
                    payload["request_id"] = request_id
                body = json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
        else:
            body = raw_body
        headers = sign_request(
            secret=SECRET,
            key_id=KEY_ID,
            timestamp=self.service.epoch_seconds,
            nonce=nonce,
            request_id=request_id,
            method=method,
            request_target=target,
            body=body,
        )
        if header_overrides:
            headers.update(header_overrides)
        response = self.client.request(method, target, content=body, headers=headers)
        return Attempt(response=response, request_id=request_id, nonce=nonce)

    def verify(self, attempt: Attempt) -> Any:
        return verify_response(
            headers=dict(attempt.response.headers),
            status_code=attempt.response.status_code,
            body=attempt.response.content,
            expected_request_id=attempt.request_id,
            expected_request_nonce=attempt.nonce,
            key_resolver=MappingKeyResolver({KEY_ID: SECRET}),
            now_epoch_seconds=self.service.epoch_seconds,
        )


@pytest.fixture
def service() -> MockCentralService:
    return MockCentralService(
        key_id=KEY_ID,
        secret=SECRET,
        node_id=NODE_ID,
        account_id=ACCOUNT_ID,
        epoch_seconds=EPOCH_SECONDS,
    )


@pytest.fixture
def signed_client(service: MockCentralService) -> Iterator[SignedClient]:
    with TestClient(service.app) as client:
        yield SignedClient(service, client)


def test_no_plan_returns_signed_empty_response(signed_client: SignedClient) -> None:
    attempt = signed_client.request("GET", "/api/v1/trading/plans/current")

    assert attempt.response.status_code == 204
    assert attempt.response.content == b""
    assert signed_client.verify(attempt).request_id == attempt.request_id


def test_current_plan_is_signed_and_has_etag(signed_client: SignedClient) -> None:
    plan = load_example("trade-plan.ready.json")
    signed_client.service.set_plan(plan)
    plan["status"] = "MUTATED_AFTER_SET"

    attempt = signed_client.request("GET", "/api/v1/trading/plans/current")

    assert attempt.response.status_code == 200
    assert attempt.response.json()["status"] == "READY"
    assert attempt.response.headers["etag"] == f'"{attempt.response.json()["plan_hash"]}"'
    signed_client.verify(attempt)


def test_command_poll_filters_sequence_and_returns_signed_contract(
    signed_client: SignedClient,
) -> None:
    command_batch = load_example("runtime/commands.batch.json")
    commands = command_batch["commands"]
    signed_client.service.set_commands(commands)
    commands[0]["command_seq"] = 999

    attempt = signed_client.request("GET", "/api/v1/trading/runtime/commands?after_command_seq=41")
    response = attempt.response.json()

    assert attempt.response.status_code == 200
    assert [command["command_seq"] for command in response["commands"]] == [42]
    assert response["high_watermark"] == 42
    assert response["request_id"] == attempt.request_id
    validate_runtime_command_batch_semantics(response)
    signed_client.verify(attempt)


def test_empty_command_poll_preserves_high_watermark(signed_client: SignedClient) -> None:
    signed_client.service.set_commands(load_example("runtime/commands.batch.json")["commands"])

    attempt = signed_client.request("GET", "/api/v1/trading/runtime/commands?after_command_seq=42")

    assert attempt.response.json()["commands"] == []
    assert attempt.response.json()["high_watermark"] == 42
    signed_client.verify(attempt)


@pytest.mark.parametrize("raw_after", ["bad", "-1"])
def test_invalid_command_sequence_returns_signed_error(
    signed_client: SignedClient, raw_after: str
) -> None:
    attempt = signed_client.request(
        "GET", f"/api/v1/trading/runtime/commands?after_command_seq={raw_after}"
    )

    assert attempt.response.status_code == 422
    assert attempt.response.json()["error"]["code"] == "COMMAND_SEQUENCE_INVALID"
    signed_client.verify(attempt)


@pytest.mark.parametrize(
    ("outcome", "status", "retryable", "error_code"),
    [
        (ReceiptOutcome.ACCEPTED, "ACCEPTED", False, None),
        (ReceiptOutcome.DUPLICATE, "DUPLICATE", False, None),
        (
            ReceiptOutcome.RETRYABLE_REJECTED,
            "REJECTED",
            True,
            "TEMPORARY_STORAGE_FAILURE",
        ),
        (
            ReceiptOutcome.PERMANENT_REJECTED,
            "REJECTED",
            False,
            "EVENT_REJECTED_BY_SCENARIO",
        ),
    ],
)
def test_event_batch_scenarios_return_per_item_signed_receipts(
    signed_client: SignedClient,
    outcome: ReceiptOutcome,
    status: str,
    retryable: bool,
    error_code: str | None,
) -> None:
    request = load_example("events/event-batch.request.json")
    request["node_id"] = NODE_ID
    request["account_id"] = ACCOUNT_ID
    signed_client.service.queue_event_batch_scenario(EventBatchScenario((outcome,)))

    attempt = signed_client.request("POST", "/api/v1/trading/events/batch", payload=request)
    result = attempt.response.json()
    receipt = result["results"][0]
    request["request_id"] = attempt.request_id

    assert receipt["status"] == status
    assert receipt["retryable"] is retryable
    assert receipt.get("error_code") == error_code
    validate_event_batch_result_semantics(result, request=request)
    signed_client.verify(attempt)


def test_event_batch_can_mix_outcomes_and_default_remaining_items(
    signed_client: SignedClient,
) -> None:
    request = load_example("events/event-batch.request.json")
    second_event = copy.deepcopy(request["events"][0])
    second_event["event_id"] = "evt_ffffffffffffffffffffffffffffffff"
    second_event["event_hash"] = "sha256:" + "f" * 64
    request["events"].append(second_event)
    request["node_id"] = NODE_ID
    request["account_id"] = ACCOUNT_ID
    signed_client.service.queue_event_batch_scenario(
        EventBatchScenario(
            outcomes=(ReceiptOutcome.DUPLICATE,),
            default_outcome=ReceiptOutcome.RETRYABLE_REJECTED,
        )
    )

    attempt = signed_client.request("POST", "/api/v1/trading/events/batch", payload=request)

    assert [item["status"] for item in attempt.response.json()["results"]] == [
        "DUPLICATE",
        "REJECTED",
    ]
    assert attempt.response.json()["results"][1]["retryable"] is True
    signed_client.verify(attempt)


def test_invalid_event_batch_returns_signed_error(signed_client: SignedClient) -> None:
    request = load_example("events/event-batch.request.json")
    request["node_id"] = NODE_ID
    request["account_id"] = ACCOUNT_ID
    request["events"] = []

    attempt = signed_client.request("POST", "/api/v1/trading/events/batch", payload=request)

    assert attempt.response.status_code == 422
    assert attempt.response.json()["error"]["code"] == "EVENT_BATCH_INVALID"
    signed_client.verify(attempt)


@pytest.mark.parametrize(
    ("target", "relative_example"),
    [
        ("/api/v1/trading/snapshots/assets", "snapshots/asset.available.json"),
        ("/api/v1/trading/snapshots/positions", "snapshots/position.available.json"),
        (
            "/api/v1/trading/reconciliations",
            "reconciliation/reconciliation.matched.json",
        ),
        ("/api/v1/trading/runtime/heartbeat", "runtime/heartbeat.healthy.json"),
    ],
)
def test_node_uploads_are_signed_accepted_and_recorded(
    signed_client: SignedClient, target: str, relative_example: str
) -> None:
    payload = load_example(relative_example)

    attempt = signed_client.request("POST", target, payload=payload)

    assert attempt.response.status_code == 202
    assert attempt.response.json() == {"accepted": True, "request_id": attempt.request_id}
    signed_client.verify(attempt)
    recorded = signed_client.service.received_messages[-1]
    assert recorded.request_target == target
    assert recorded.request_id == attempt.request_id


def test_received_message_view_does_not_mutate_service_state(signed_client: SignedClient) -> None:
    payload = load_example("runtime/heartbeat.healthy.json")
    signed_client.request("POST", "/api/v1/trading/runtime/heartbeat", payload=payload)

    first = signed_client.service.received_messages[0]
    cast_payload = first.payload
    assert isinstance(cast_payload, dict)
    cast_payload["runtime_state"] = "MUTATED"

    assert signed_client.service.received_messages[0].payload["runtime_state"] == "AUTO"


def test_command_result_path_and_body_must_match(signed_client: SignedClient) -> None:
    payload = load_example("runtime/command-result.applied.json")
    target = "/api/v1/trading/runtime/commands/cmd_wrong_00000001/result"

    attempt = signed_client.request("POST", target, payload=payload)

    assert attempt.response.status_code == 409
    assert attempt.response.json()["error"]["code"] == "COMMAND_ID_MISMATCH"
    signed_client.verify(attempt)


def test_matching_command_result_is_recorded(signed_client: SignedClient) -> None:
    payload = load_example("runtime/command-result.applied.json")
    target = f"/api/v1/trading/runtime/commands/{payload['command_id']}/result"

    attempt = signed_client.request("POST", target, payload=payload)

    assert attempt.response.status_code == 202
    assert (
        signed_client.service.received_messages[-1].payload["command_id"] == payload["command_id"]
    )
    signed_client.verify(attempt)


@pytest.mark.parametrize(
    ("field", "value", "code", "status"),
    [
        ("request_id", "req_ffffffffffffffffffffffffffffffff", "REQUEST_ID_MISMATCH", 409),
        ("node_id", "node-other-001", "NODE_NOT_AUTHORIZED", 403),
        ("account_id", "acct_other_001", "ACCOUNT_NOT_AUTHORIZED", 403),
    ],
)
def test_authenticated_identity_mismatch_returns_signed_error(
    signed_client: SignedClient,
    field: str,
    value: str,
    code: str,
    status: int,
) -> None:
    payload = load_example("runtime/heartbeat.healthy.json")
    payload[field] = value

    attempt = signed_client.request(
        "POST",
        "/api/v1/trading/runtime/heartbeat",
        payload=payload,
        preserve_request_id=field == "request_id",
    )

    assert attempt.response.status_code == status
    assert attempt.response.json()["error"]["code"] == code
    signed_client.verify(attempt)


@pytest.mark.parametrize(
    ("body", "code"),
    [(b"not-json", "REQUEST_JSON_INVALID"), (b"[]", "REQUEST_JSON_OBJECT_REQUIRED")],
)
def test_authenticated_invalid_json_returns_signed_error(
    signed_client: SignedClient, body: bytes, code: str
) -> None:
    attempt = signed_client.request("POST", "/api/v1/trading/runtime/heartbeat", raw_body=body)

    assert attempt.response.status_code == 422
    assert attempt.response.json()["error"]["code"] == code
    signed_client.verify(attempt)


def test_invalid_request_signature_returns_unsigned_authentication_error(
    signed_client: SignedClient,
) -> None:
    attempt = signed_client.request(
        "GET",
        "/api/v1/trading/plans/current",
        header_overrides={HEADER_SIGNATURE: "v1=" + "0" * 64},
    )

    assert attempt.response.status_code == 401
    assert attempt.response.json()["error"]["code"] == "SIGNATURE_MISMATCH"
    assert HEADER_SIGNATURE not in attempt.response.headers


def test_request_replay_is_rejected_before_business_processing(
    service: MockCentralService,
) -> None:
    target = "/api/v1/trading/plans/current"
    headers = sign_request(
        secret=SECRET,
        key_id=KEY_ID,
        timestamp=service.epoch_seconds,
        nonce="a" * 32,
        request_id="req_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        method="GET",
        request_target=target,
        body=b"",
    )
    with TestClient(service.app) as client:
        assert client.get(target, headers=headers).status_code == 204
        replay = client.get(target, headers=headers)

    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "SIGNATURE_REPLAYED"


@pytest.mark.parametrize(
    ("fault", "error_type"),
    [
        (MockCentralNetworkFault.DISCONNECTED, MockCentralDisconnectedError),
        (MockCentralNetworkFault.TIMEOUT, MockCentralTimeoutError),
    ],
)
def test_network_fault_is_deterministic_and_one_shot(
    service: MockCentralService,
    fault: MockCentralNetworkFault,
    error_type: type[Exception],
) -> None:
    target = "/api/v1/trading/plans/current"
    headers = sign_request(
        secret=SECRET,
        key_id=KEY_ID,
        timestamp=service.epoch_seconds,
        nonce="b" * 32,
        request_id="req_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        method="GET",
        request_target=target,
        body=b"",
    )
    service.queue_network_fault(fault)
    with TestClient(service.app) as client:
        with pytest.raises(error_type):
            client.get(target, headers=headers)
        assert client.get(target, headers=headers).status_code == 204


@pytest.mark.parametrize(
    ("fault", "verification_code"),
    [
        (MockResponseFault.MISSING_SIGNATURE, "SIGNATURE_HEADER_MISSING"),
        (MockResponseFault.INVALID_SIGNATURE, "SIGNATURE_MISMATCH"),
        (MockResponseFault.INVALID_BODY_HASH, "CONTENT_DIGEST_MISMATCH"),
        (MockResponseFault.WRONG_REQUEST_BINDING, "RESPONSE_REQUEST_MISMATCH"),
        (MockResponseFault.STALE_TIMESTAMP, "SIGNATURE_TIMESTAMP_OUT_OF_WINDOW"),
    ],
)
def test_response_signature_faults_are_explicit_and_one_shot(
    signed_client: SignedClient,
    fault: MockResponseFault,
    verification_code: str,
) -> None:
    signed_client.service.queue_response_fault(fault)
    bad_attempt = signed_client.request("GET", "/api/v1/trading/plans/current")

    with pytest.raises(SignatureError, match=verification_code):
        signed_client.verify(bad_attempt)

    good_attempt = signed_client.request("GET", "/api/v1/trading/plans/current")
    signed_client.verify(good_attempt)


def test_duplicate_raw_security_header_is_rejected(service: MockCentralService) -> None:
    target = "/api/v1/trading/plans/current"
    headers = sign_request(
        secret=SECRET,
        key_id=KEY_ID,
        timestamp=service.epoch_seconds,
        nonce="c" * 32,
        request_id="req_cccccccccccccccccccccccccccccccc",
        method="GET",
        request_target=target,
        body=b"",
    )
    raw_headers = [*headers.items(), ("x-ats-signature", headers[HEADER_SIGNATURE])]

    with TestClient(service.app) as client:
        response = client.get(target, headers=raw_headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "DUPLICATE_SIGNATURE_HEADER"


def test_mock_clock_validation_and_advance(service: MockCentralService) -> None:
    service.advance_time(5)
    assert service.epoch_seconds == EPOCH_SECONDS + 5
    service.set_epoch_seconds(EPOCH_SECONDS)
    assert service.epoch_seconds == EPOCH_SECONDS

    with pytest.raises(ValueError, match="epoch_seconds"):
        service.set_epoch_seconds(-1)
    with pytest.raises(ValueError, match="seconds"):
        service.advance_time(-1)
