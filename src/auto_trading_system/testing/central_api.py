"""In-process deterministic mock of the quantitative service HTTP API."""

from __future__ import annotations

import copy
import json
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from threading import RLock
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.responses import Response

from auto_trading_system.protocol.signing import (
    DEFAULT_MAX_CLOCK_SKEW_SECONDS,
    HEADER_CONTENT_SHA256,
    HEADER_SIGNATURE,
    InMemoryNonceStore,
    MappingKeyResolver,
    SignatureError,
    VerifiedRequestSignature,
    sign_response,
    verify_request,
)

API_PREFIX = "/api/v1/trading"
SHANGHAI_ZONE = timezone(timedelta(hours=8))
SECURITY_HEADER_NAMES = frozenset(
    {
        b"x-ats-key-id",
        b"x-ats-timestamp",
        b"x-ats-nonce",
        b"x-ats-request-id",
        b"x-ats-content-sha256",
        b"x-ats-signature",
    }
)


class MockCentralDisconnectedError(ConnectionError):
    """Raised before request processing to simulate an unreachable central service."""


class MockCentralTimeoutError(TimeoutError):
    """Raised before request processing to simulate a network timeout."""


class MockCentralNetworkFault(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    TIMEOUT = "TIMEOUT"


class MockResponseFault(StrEnum):
    MISSING_SIGNATURE = "MISSING_SIGNATURE"
    INVALID_SIGNATURE = "INVALID_SIGNATURE"
    INVALID_BODY_HASH = "INVALID_BODY_HASH"
    WRONG_REQUEST_BINDING = "WRONG_REQUEST_BINDING"
    STALE_TIMESTAMP = "STALE_TIMESTAMP"


class ReceiptOutcome(StrEnum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    RETRYABLE_REJECTED = "RETRYABLE_REJECTED"
    PERMANENT_REJECTED = "PERMANENT_REJECTED"


@dataclass(frozen=True, slots=True)
class EventBatchScenario:
    """Per-event outcomes; missing positions use default_outcome."""

    outcomes: tuple[ReceiptOutcome, ...] = ()
    default_outcome: ReceiptOutcome = ReceiptOutcome.ACCEPTED

    def outcome_at(self, index: int) -> ReceiptOutcome:
        return self.outcomes[index] if index < len(self.outcomes) else self.default_outcome


@dataclass(frozen=True, slots=True)
class ReceivedMessage:
    method: str
    request_target: str
    request_id: str
    payload: Mapping[str, Any]


class MockCentralService:
    """Programmable signed FastAPI replacement for central-service client tests."""

    def __init__(
        self,
        *,
        key_id: str,
        secret: bytes,
        node_id: str,
        account_id: str,
        epoch_seconds: int,
        max_clock_skew_seconds: int = DEFAULT_MAX_CLOCK_SKEW_SECONDS,
    ) -> None:
        self._key_id = key_id
        self._secret = bytes(secret)
        self._key_resolver = MappingKeyResolver({key_id: self._secret})
        self._nonce_store = InMemoryNonceStore()
        self._node_id = node_id
        self._account_id = account_id
        self._epoch_seconds = epoch_seconds
        self._max_clock_skew_seconds = max_clock_skew_seconds
        self._plan: dict[str, Any] | None = None
        self._commands: list[dict[str, Any]] = []
        self._network_faults: deque[MockCentralNetworkFault] = deque()
        self._response_faults: deque[MockResponseFault] = deque()
        self._batch_scenarios: deque[EventBatchScenario] = deque()
        self._received: list[ReceivedMessage] = []
        self._lock = RLock()
        self.app = self._build_app()

    @property
    def received_messages(self) -> tuple[ReceivedMessage, ...]:
        with self._lock:
            return tuple(copy.deepcopy(self._received))

    @property
    def epoch_seconds(self) -> int:
        with self._lock:
            return self._epoch_seconds

    def set_epoch_seconds(self, value: int) -> None:
        if type(value) is not int or value < 0:
            raise ValueError("epoch_seconds must be a non-negative integer")
        with self._lock:
            self._epoch_seconds = value

    def advance_time(self, seconds: int) -> None:
        if type(seconds) is not int or seconds < 0:
            raise ValueError("seconds must be a non-negative integer")
        with self._lock:
            self._epoch_seconds += seconds

    def set_plan(self, plan: Mapping[str, Any] | None) -> None:
        with self._lock:
            self._plan = None if plan is None else copy.deepcopy(dict(plan))

    def set_commands(self, commands: list[Mapping[str, Any]]) -> None:
        copied = [copy.deepcopy(dict(command)) for command in commands]
        copied.sort(key=lambda command: cast(int, command["command_seq"]))
        with self._lock:
            self._commands = copied

    def queue_network_fault(self, fault: MockCentralNetworkFault) -> None:
        with self._lock:
            self._network_faults.append(fault)

    def queue_response_fault(self, fault: MockResponseFault) -> None:
        with self._lock:
            self._response_faults.append(fault)

    def queue_event_batch_scenario(self, scenario: EventBatchScenario) -> None:
        with self._lock:
            self._batch_scenarios.append(scenario)

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="Auto Trading Mock Central API", docs_url=None, redoc_url=None)

        @app.exception_handler(MockAuthenticationError)
        async def authentication_error(
            _request: Request, error: MockAuthenticationError
        ) -> Response:
            payload = {
                "schema_version": "1.0",
                "request_id": None,
                "occurred_at": self._wire_time(),
                "error": {
                    "code": error.code,
                    "message": "Request authentication failed",
                    "retryable": False,
                },
            }
            return Response(
                content=self._json_bytes(payload),
                status_code=401,
                media_type="application/json",
            )

        @app.exception_handler(MockPayloadError)
        async def payload_error(_request: Request, error: MockPayloadError) -> Response:
            return self._signed_error(
                error.verified,
                status_code=422,
                code=error.code,
                message="Authenticated request body must be a JSON object",
                retryable=False,
            )

        @app.get(f"{API_PREFIX}/plans/current")
        async def current_plan(request: Request) -> Response:
            verified, _ = await self._authenticate(request)
            with self._lock:
                plan = copy.deepcopy(self._plan)
            if plan is None:
                return self._signed_response(verified, status_code=204, payload=None)
            headers = {"ETag": f'"{plan["plan_hash"]}"'}
            return self._signed_response(
                verified, status_code=200, payload=plan, extra_headers=headers
            )

        @app.get(f"{API_PREFIX}/runtime/commands")
        async def current_commands(request: Request) -> Response:
            verified, _ = await self._authenticate(request)
            raw_after = request.query_params.get("after_command_seq", "0")
            try:
                after = int(raw_after)
            except ValueError:
                return self._signed_error(
                    verified,
                    status_code=422,
                    code="COMMAND_SEQUENCE_INVALID",
                    message="after_command_seq must be an integer",
                    retryable=False,
                )
            if after < 0:
                return self._signed_error(
                    verified,
                    status_code=422,
                    code="COMMAND_SEQUENCE_INVALID",
                    message="after_command_seq must be non-negative",
                    retryable=False,
                )
            with self._lock:
                commands = [
                    copy.deepcopy(command)
                    for command in self._commands
                    if cast(int, command["command_seq"]) > after
                ][:100]
                high_watermark = max(
                    (cast(int, command["command_seq"]) for command in self._commands),
                    default=after,
                )
            payload = {
                "schema_version": "1.0",
                "request_id": verified.request_id,
                "node_id": self._node_id,
                "account_id": self._account_id,
                "generated_at": self._wire_time(),
                "after_command_seq": after,
                "high_watermark": max(after, high_watermark),
                "next_poll_after_ms": 2000,
                "commands": commands,
            }
            return self._signed_response(verified, status_code=200, payload=payload)

        @app.post(f"{API_PREFIX}/events/batch")
        async def event_batch(request: Request) -> Response:
            verified, body = await self._authenticate(request)
            payload = self._parse_authenticated_json(verified, body)
            error = self._identity_error(verified, payload)
            if error is not None:
                return error
            events = payload.get("events")
            if not isinstance(events, list) or not events:
                return self._signed_error(
                    verified,
                    status_code=422,
                    code="EVENT_BATCH_INVALID",
                    message="events must be a non-empty array",
                    retryable=False,
                )
            self._record(request, verified, payload)
            with self._lock:
                scenario = (
                    self._batch_scenarios.popleft()
                    if self._batch_scenarios
                    else EventBatchScenario()
                )
            receipts = [
                self._event_receipt(event, scenario.outcome_at(index))
                for index, event in enumerate(events)
            ]
            accepted = sum(receipt["status"] == "ACCEPTED" for receipt in receipts)
            duplicates = sum(receipt["status"] == "DUPLICATE" for receipt in receipts)
            rejected = sum(receipt["status"] == "REJECTED" for receipt in receipts)
            persisted_sequences = [
                event["local_seq"]
                for event, receipt in zip(events, receipts, strict=True)
                if receipt["status"] in {"ACCEPTED", "DUPLICATE"}
            ]
            result = {
                "schema_version": "1.0",
                "request_id": verified.request_id,
                "received_at": self._wire_time(),
                "accepted": accepted,
                "duplicates": duplicates,
                "rejected": rejected,
                "high_watermark": max(persisted_sequences, default=0),
                "results": receipts,
            }
            return self._signed_response(verified, status_code=200, payload=result)

        @app.post(f"{API_PREFIX}/snapshots/assets")
        async def asset_snapshot(request: Request) -> Response:
            return await self._accept_message(request)

        @app.post(f"{API_PREFIX}/snapshots/positions")
        async def position_snapshot(request: Request) -> Response:
            return await self._accept_message(request)

        @app.post(f"{API_PREFIX}/reconciliations")
        async def reconciliation(request: Request) -> Response:
            return await self._accept_message(request)

        @app.post(f"{API_PREFIX}/runtime/heartbeat")
        async def heartbeat(request: Request) -> Response:
            return await self._accept_message(request)

        @app.post(f"{API_PREFIX}/runtime/commands/{{command_id}}/result")
        async def command_result(command_id: str, request: Request) -> Response:
            verified, body = await self._authenticate(request)
            payload = self._parse_authenticated_json(verified, body)
            error = self._identity_error(verified, payload)
            if error is not None:
                return error
            if payload.get("command_id") != command_id:
                return self._signed_error(
                    verified,
                    status_code=409,
                    code="COMMAND_ID_MISMATCH",
                    message="path command_id must match body command_id",
                    retryable=False,
                )
            self._record(request, verified, payload)
            return self._signed_response(
                verified,
                status_code=202,
                payload={"accepted": True, "request_id": verified.request_id},
            )

        return app

    async def _accept_message(self, request: Request) -> Response:
        verified, body = await self._authenticate(request)
        payload = self._parse_authenticated_json(verified, body)
        error = self._identity_error(verified, payload)
        if error is not None:
            return error
        self._record(request, verified, payload)
        return self._signed_response(
            verified,
            status_code=202,
            payload={"accepted": True, "request_id": verified.request_id},
        )

    async def _authenticate(self, request: Request) -> tuple[VerifiedRequestSignature, bytes]:
        self._raise_queued_network_fault()
        body = await request.body()
        target = self._request_target(request)
        try:
            security_headers = self._security_headers(request)
            verified = verify_request(
                headers=security_headers,
                method=request.method,
                request_target=target,
                body=body,
                key_resolver=self._key_resolver,
                nonce_store=self._nonce_store,
                now_epoch_seconds=self.epoch_seconds,
                max_clock_skew_seconds=self._max_clock_skew_seconds,
            )
        except SignatureError as exc:
            raise MockAuthenticationError(exc.code) from exc
        return verified, body

    def _raise_queued_network_fault(self) -> None:
        with self._lock:
            fault = self._network_faults.popleft() if self._network_faults else None
        if fault is MockCentralNetworkFault.DISCONNECTED:
            raise MockCentralDisconnectedError("mock central service is disconnected")
        if fault is MockCentralNetworkFault.TIMEOUT:
            raise MockCentralTimeoutError("mock central service timed out")

    @staticmethod
    def _request_target(request: Request) -> str:
        raw_path = cast(bytes, request.scope.get("raw_path", request.url.path.encode("ascii")))
        query = cast(bytes, request.scope.get("query_string", b""))
        target = raw_path.decode("ascii")
        return target if not query else f"{target}?{query.decode('ascii')}"

    @staticmethod
    def _security_headers(request: Request) -> dict[str, str]:
        result: dict[str, str] = {}
        for raw_name, raw_value in cast(list[tuple[bytes, bytes]], request.scope["headers"]):
            lowered = raw_name.lower()
            if lowered not in SECURITY_HEADER_NAMES:
                continue
            name = lowered.decode("ascii")
            if name in result:
                raise SignatureError("DUPLICATE_SIGNATURE_HEADER", f"duplicate header {name}")
            result[name] = raw_value.decode("ascii")
        return result

    @staticmethod
    def _parse_authenticated_json(
        verified: VerifiedRequestSignature, body: bytes
    ) -> dict[str, Any]:
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MockPayloadError(verified, "REQUEST_JSON_INVALID") from exc
        if not isinstance(value, dict):
            raise MockPayloadError(verified, "REQUEST_JSON_OBJECT_REQUIRED")
        return value

    def _identity_error(
        self, verified: VerifiedRequestSignature, payload: Mapping[str, Any]
    ) -> Response | None:
        if payload.get("request_id") != verified.request_id:
            return self._signed_error(
                verified,
                status_code=409,
                code="REQUEST_ID_MISMATCH",
                message="signed request id must match body request_id",
                retryable=False,
            )
        if payload.get("node_id") not in {None, self._node_id}:
            return self._signed_error(
                verified,
                status_code=403,
                code="NODE_NOT_AUTHORIZED",
                message="node is not authorized for this mock service",
                retryable=False,
            )
        if payload.get("account_id") not in {None, self._account_id}:
            return self._signed_error(
                verified,
                status_code=403,
                code="ACCOUNT_NOT_AUTHORIZED",
                message="account is not authorized for this mock service",
                retryable=False,
            )
        return None

    def _record(
        self,
        request: Request,
        verified: VerifiedRequestSignature,
        payload: Mapping[str, Any],
    ) -> None:
        message = ReceivedMessage(
            method=request.method,
            request_target=self._request_target(request),
            request_id=verified.request_id,
            payload=copy.deepcopy(dict(payload)),
        )
        with self._lock:
            self._received.append(message)

    @staticmethod
    def _event_receipt(event: object, outcome: ReceiptOutcome) -> dict[str, Any]:
        event_id: object
        event_hash: object
        if not isinstance(event, Mapping):
            event_id = "evt_00000000000000000000000000000000"
            event_hash = "sha256:" + "0" * 64
        else:
            event_id = event.get("event_id")
            event_hash = event.get("event_hash")
        if outcome in {ReceiptOutcome.ACCEPTED, ReceiptOutcome.DUPLICATE}:
            return {
                "event_id": event_id,
                "event_hash": event_hash,
                "status": outcome.value,
                "retryable": False,
            }
        retryable = outcome is ReceiptOutcome.RETRYABLE_REJECTED
        return {
            "event_id": event_id,
            "event_hash": event_hash,
            "status": "REJECTED",
            "retryable": retryable,
            "error_code": (
                "TEMPORARY_STORAGE_FAILURE" if retryable else "EVENT_REJECTED_BY_SCENARIO"
            ),
            "error_message": (
                "Mock central storage is temporarily unavailable"
                if retryable
                else "Mock scenario permanently rejected the event"
            ),
        }

    def _signed_error(
        self,
        verified: VerifiedRequestSignature,
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool,
    ) -> Response:
        payload = {
            "schema_version": "1.0",
            "request_id": verified.request_id,
            "occurred_at": self._wire_time(),
            "error": {"code": code, "message": message, "retryable": retryable},
        }
        return self._signed_response(verified, status_code=status_code, payload=payload)

    def _signed_response(
        self,
        verified: VerifiedRequestSignature,
        *,
        status_code: int,
        payload: Mapping[str, Any] | None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> Response:
        body = b"" if payload is None else self._json_bytes(payload)
        with self._lock:
            fault = self._response_faults.popleft() if self._response_faults else None
            timestamp = self._epoch_seconds
        request_nonce = verified.nonce
        if fault is MockResponseFault.STALE_TIMESTAMP:
            timestamp -= self._max_clock_skew_seconds + 1
        if fault is MockResponseFault.WRONG_REQUEST_BINDING:
            request_nonce = "f" * 32 if verified.nonce != "f" * 32 else "e" * 32
        headers = sign_response(
            secret=self._secret,
            key_id=self._key_id,
            timestamp=timestamp,
            request_nonce=request_nonce,
            request_id=verified.request_id,
            status_code=status_code,
            body=body,
        )
        if fault is MockResponseFault.MISSING_SIGNATURE:
            del headers[HEADER_SIGNATURE]
        elif fault is MockResponseFault.INVALID_SIGNATURE:
            headers[HEADER_SIGNATURE] = "v1=" + "0" * 64
        elif fault is MockResponseFault.INVALID_BODY_HASH:
            headers[HEADER_CONTENT_SHA256] = "sha256:" + "0" * 64
        if extra_headers:
            headers.update(extra_headers)
        return Response(
            content=body, status_code=status_code, media_type="application/json", headers=headers
        )

    @staticmethod
    def _json_bytes(payload: Mapping[str, Any]) -> bytes:
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def _wire_time(self) -> str:
        with self._lock:
            value = self._epoch_seconds
        return datetime.fromtimestamp(value, tz=SHANGHAI_ZONE).isoformat(timespec="seconds")


class MockAuthenticationError(RuntimeError):
    """Internal marker used by the ASGI exception handler."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class MockPayloadError(RuntimeError):
    """Internal marker retaining authenticated response binding."""

    def __init__(self, verified: VerifiedRequestSignature, code: str) -> None:
        super().__init__(code)
        self.verified = verified
        self.code = code
