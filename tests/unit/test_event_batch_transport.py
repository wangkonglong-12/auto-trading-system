from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from auto_trading_system.domain import ShanghaiTimestamp
from auto_trading_system.journal import (
    CanonicalJsonDocument,
    OutboxItem,
    OutboxKind,
    OutboxState,
)
from auto_trading_system.protocol import calculate_event_hash, sign_response
from auto_trading_system.protocol.canonical_json import canonical_json_bytes
from auto_trading_system.protocol.signing import HEADER_NONCE, HEADER_REQUEST_ID
from auto_trading_system.sync import (
    OutboxDeliveryDisposition,
    OutboxDeliveryError,
    SignedEventBatchHttpTransport,
    SystemEpochClock,
)
from auto_trading_system.testing import (
    EventBatchScenario,
    MockCentralNetworkFault,
    MockCentralService,
    MockResponseFault,
    ReceiptOutcome,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVENT_EXAMPLE = (
    PROJECT_ROOT / "contracts" / "v1" / "examples" / "events" / "event.runtime-state-changed.json"
)
SECRET = b"event-transport-test-secret-at-least-32-bytes"
KEY_ID = "key_event_transport"
NODE_ID = "node-demo-001"
ACCOUNT_ID = "acct_demo_001"
EPOCH_SECONDS = 1_788_504_301
BASE_URL = "https://central.test/api/v1/trading"


@dataclass(frozen=True, slots=True)
class FixedClock:
    value: ShanghaiTimestamp = field(
        default_factory=lambda: ShanghaiTimestamp.parse("2026-09-04T08:45:01+08:00")
    )

    def now(self) -> ShanghaiTimestamp:
        return self.value


@dataclass(frozen=True, slots=True)
class FixedEpochClock:
    value: int = EPOCH_SECONDS

    def now_epoch_seconds(self) -> int:
        return self.value


@dataclass
class IdentitySequence:
    value: int = 0
    requests: list[str] = field(default_factory=list)
    nonces: list[str] = field(default_factory=list)

    def request_id(self) -> str:
        self.value += 1
        result = f"req_{self.value:032x}"
        self.requests.append(result)
        return result

    def nonce(self) -> str:
        result = f"{self.value:032x}"
        self.nonces.append(result)
        return result


def event_mapping(number: int = 1) -> dict[str, Any]:
    with EVENT_EXAMPLE.open(encoding="utf-8") as handle:
        event = json.load(handle)
    assert isinstance(event, dict)
    event = copy.deepcopy(event)
    event["event_id"] = f"evt_{number:032x}"
    event["local_seq"] = number
    event["event_hash"] = calculate_event_hash(event)
    return event


def outbox_item(number: int = 1, *, kind: OutboxKind = OutboxKind.DOMAIN_EVENT) -> OutboxItem:
    event = event_mapping(number)
    body = canonical_json_bytes(event, exclude=frozenset())
    document = CanonicalJsonDocument(event["event_id"], event["event_hash"], body)
    created = ShanghaiTimestamp.parse(event["recorded_at"])
    return OutboxItem(
        number,
        kind,
        OutboxState.PENDING,
        document,
        0,
        created,
        created,
    )


def service() -> MockCentralService:
    return MockCentralService(
        key_id=KEY_ID,
        secret=SECRET,
        node_id=NODE_ID,
        account_id=ACCOUNT_ID,
        epoch_seconds=EPOCH_SECONDS,
    )


def transport(
    client: httpx.AsyncClient,
    *,
    identities: IdentitySequence | None = None,
) -> SignedEventBatchHttpTransport:
    sequence = identities or IdentitySequence()
    return SignedEventBatchHttpTransport(
        client,
        base_url=BASE_URL,
        node_id=NODE_ID,
        account_id=ACCOUNT_ID,
        key_id=KEY_ID,
        secret=SECRET,
        clock=FixedClock(),
        epoch_clock=FixedEpochClock(),
        request_id_factory=sequence.request_id,
        nonce_factory=sequence.nonce,
    )


def test_signed_transport_maps_mixed_authenticated_per_item_receipts() -> None:
    async def scenario() -> None:
        mock = service()
        mock.queue_event_batch_scenario(
            EventBatchScenario(
                (
                    ReceiptOutcome.ACCEPTED,
                    ReceiptOutcome.DUPLICATE,
                    ReceiptOutcome.RETRYABLE_REJECTED,
                    ReceiptOutcome.PERMANENT_REJECTED,
                )
            )
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mock.app),
            base_url="https://central.test",
        ) as client:
            receipts = await transport(client).deliver(tuple(outbox_item(i) for i in range(1, 5)))

        assert [receipt.disposition for receipt in receipts] == [
            OutboxDeliveryDisposition.ACKNOWLEDGE,
            OutboxDeliveryDisposition.ACKNOWLEDGE,
            OutboxDeliveryDisposition.RETRY,
            OutboxDeliveryDisposition.DEAD_LETTER,
        ]
        assert receipts[2].reason_code == "TEMPORARY_STORAGE_FAILURE"
        assert receipts[3].reason_code == "EVENT_REJECTED_BY_SCENARIO"
        received = mock.received_messages
        assert len(received) == 1
        assert received[0].request_target == "/api/v1/trading/events/batch"
        assert [event["event_id"] for event in received[0].payload["events"]] == [
            f"evt_{number:032x}" for number in range(1, 5)
        ]

    asyncio_run(scenario())


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        (MockCentralNetworkFault.DISCONNECTED, "OUTBOX_TRANSPORT_UNAVAILABLE"),
        (MockCentralNetworkFault.TIMEOUT, "OUTBOX_TRANSPORT_TIMEOUT"),
    ],
)
def test_network_failures_are_retryable_and_each_attempt_uses_new_identity(
    fault: MockCentralNetworkFault,
    code: str,
) -> None:
    async def scenario() -> None:
        mock = service()
        mock.queue_network_fault(fault)
        identities = IdentitySequence()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mock.app),
            base_url="https://central.test",
        ) as client:
            current = transport(client, identities=identities)
            with pytest.raises(OutboxDeliveryError) as captured:
                await current.deliver((outbox_item(),))
            assert captured.value.code == code
            assert captured.value.retryable is True
            assert len(await current.deliver((outbox_item(),))) == 1
        assert identities.requests == [
            "req_00000000000000000000000000000001",
            "req_00000000000000000000000000000002",
        ]
        assert identities.nonces == [
            "00000000000000000000000000000001",
            "00000000000000000000000000000002",
        ]

    asyncio_run(scenario())


@pytest.mark.parametrize("fault", tuple(MockResponseFault))
def test_unauthenticated_or_misbound_response_never_generates_receipts(
    fault: MockResponseFault,
) -> None:
    async def scenario() -> None:
        mock = service()
        mock.queue_response_fault(fault)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mock.app),
            base_url="https://central.test",
        ) as client:
            with pytest.raises(OutboxDeliveryError) as captured:
                await transport(client).deliver((outbox_item(),))
        assert captured.value.code == "OUTBOX_RESPONSE_AUTH_FAILED"
        assert captured.value.retryable is False

    asyncio_run(scenario())


@pytest.mark.parametrize(
    ("status_code", "expected_code", "retryable"),
    [
        (400, "OUTBOX_HTTP_REJECTED", False),
        (429, "OUTBOX_HTTP_RETRYABLE", True),
        (503, "OUTBOX_HTTP_RETRYABLE", True),
    ],
)
def test_authenticated_http_error_retains_batch_for_worker_policy(
    status_code: int,
    expected_code: str,
    retryable: bool,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = canonical_json_bytes(
            {
                "schema_version": "1.0",
                "error_code": "TEST_HTTP_ERROR",
                "message": "test error",
            },
            exclude=frozenset(),
        )
        headers = sign_response(
            secret=SECRET,
            key_id=KEY_ID,
            timestamp=EPOCH_SECONDS,
            request_nonce=request.headers[HEADER_NONCE],
            request_id=request.headers[HEADER_REQUEST_ID],
            status_code=status_code,
            body=body,
        )
        return httpx.Response(status_code, headers=headers, content=body)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(OutboxDeliveryError) as captured:
                await transport(client).deliver((outbox_item(),))
        assert captured.value.code == expected_code
        assert captured.value.retryable is retryable

    asyncio_run(scenario())


def test_authenticated_malformed_success_response_is_rejected() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = b"{}"
        headers = sign_response(
            secret=SECRET,
            key_id=KEY_ID,
            timestamp=EPOCH_SECONDS,
            request_nonce=request.headers[HEADER_NONCE],
            request_id=request.headers[HEADER_REQUEST_ID],
            status_code=200,
            body=body,
        )
        return httpx.Response(200, headers=headers, content=body)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(OutboxDeliveryError) as captured:
                await transport(client).deliver((outbox_item(),))
        assert captured.value.code == "OUTBOX_RESPONSE_INVALID"
        assert captured.value.retryable is False

    asyncio_run(scenario())


@pytest.mark.parametrize(
    "items",
    [
        (),
        tuple(outbox_item(i) for i in range(1, 52)),
        (outbox_item(1, kind=OutboxKind.ASSET_SNAPSHOT),),
    ],
)
def test_invalid_local_batch_is_not_sent(items: tuple[OutboxItem, ...]) -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(OutboxDeliveryError) as captured:
                await transport(client).deliver(items)
        assert captured.value.code == "OUTBOX_EVENT_INVALID"

    asyncio_run(scenario())
    assert calls == 0


def test_transport_validates_configuration_and_redacts_secret() -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(500))
        ) as client:
            current = transport(client)
            assert "event-transport-test-secret" not in repr(current)
            assert "**REDACTED**" in repr(current)
            assert isinstance(SystemEpochClock().now_epoch_seconds(), int)
            for field, value, message in (
                ("base_url", "http://central.test/api/v1/trading", "base_url"),
                ("node_id", "bad", "node_id"),
                ("account_id", "bad", "account_id"),
                ("secret", bytearray(SECRET), "secret"),
            ):
                kwargs: dict[str, object] = {
                    "base_url": BASE_URL,
                    "node_id": NODE_ID,
                    "account_id": ACCOUNT_ID,
                    "key_id": KEY_ID,
                    "secret": SECRET,
                    "clock": FixedClock(),
                    "epoch_clock": FixedEpochClock(),
                }
                kwargs[field] = value
                with pytest.raises(ValueError, match=message):
                    SignedEventBatchHttpTransport(client, **kwargs)  # type: ignore[arg-type]

    asyncio_run(scenario())


def asyncio_run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)
